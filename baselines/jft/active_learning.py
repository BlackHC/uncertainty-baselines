# coding=utf-8
# Copyright 2021 The Uncertainty Baselines Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
r"""Active learning loop.

This script implements a basic Active Learning loop using predictive entropy as
acquisition function.

The below command is for running this script on a TPU-VM.

Execute in `baselines/jft`:

python3 active_learning.py \
  --config="experiments/imagenet21k_vit_base16_finetune_cifar10.py" \
  --config.model_init="gs://ub-data/ImageNet21k_ViT-B16_ImagetNet21k_ViT-B_16_28592399.npz"  \
  --config.batch_size=256 \
  --config.total_steps=50 \
  --initial_training_set_size=0 \
  --acquisition_batch_size=5 \
  --max_training_set_size=100 \
  --acquisition_method=uniform

Note the strongly reduced total_steps.

To reformat before committing: yapf -i  baselines/jft/active_learning.py --style=yapf
"""

from functools import partial  # pylint: disable=g-importing-member standard use
import math
import numbers

from absl import app
from absl import flags
from clu import preprocess_spec
import flax
import flax.jax_utils as flax_utils
import jax
import jax.numpy as jnp
from ml_collections.config_flags import config_flags
import numpy as np
import tensorflow_datasets as tfds
import tqdm
import uncertainty_baselines as ub
import al_utils  # pylint: disable=unused-import # to register Cifar10Subset as dataset  # local file import from baselines.jft
import checkpoint_utils  # local file import from baselines.jft
import input_utils  # local file import from baselines.jft
import ood_utils  # local file import from baselines.jft
import preprocess_utils  # local file import from baselines.jft
import train_utils  # local file import from baselines.jft

import wandb

config_flags.DEFINE_config_file(
    "config", None, "Training configuration.", lock_config=False)
flags.DEFINE_enum(
    "acquisition_method",
    default="uniform",
    enum_values=["uniform", "density", "margin", "entropy"],
    help="Choose an acquisition method.")
flags.DEFINE_integer(
    "acquisition_batch_size",
    default=None,
    help="Acquisition batch size per active learning iteration.")
flags.DEFINE_integer(
    "max_training_set_size", default=None, help="Maximum training set size.")
flags.DEFINE_integer(
    "initial_training_set_size",
    default=None,
    help="Initial training set size.")
flags.DEFINE_integer(
    "early_stopping_patience", default=None, help="Early stopping patience.")
flags.DEFINE_integer("seed", default=None, help="Random seed.")

FLAGS = flags.FLAGS

# TODO(joost,andreas): can we use float("-inf") here?
NINF_SCORE = float("-inf")


def get_ids_logits_masks(*,
                         model,
                         opt_repl,
                         ds,
                         pre_logits=False,
                         prefetch_to_device=1):
  """Obtain (pre) logits for each datapoint.

  This can be then used to compute entropies, and so on.

  Args:
    model: a initialized model.
    opt_repl: an optimizer with parameters.
    ds: a dataset.
    pre_logits: if True, return pre logit instead of logit
    prefetch_to_device: how many batches to prefix

  Returns:
    a tuple of jnp arrays of ids, logits, labels and masks.
  """

  @partial(jax.pmap, axis_name="batch")
  def compute_batch_outputs(params, images):
    logits, out = model.apply({"params": flax.core.freeze(params)},
                              images,
                              train=False)
    if pre_logits:
      output = out["pre_logits"]
    else:
      output = logits

    # TODO(joost,andreas): For multi host this requires:
    # output = jax.lax.all_gather(output, axis_name='batch')
    return output

  iter_ds = input_utils.start_input_pipeline(ds, prefetch_to_device)

  outputs = []
  ids = []
  labels = []
  masks = []
  for batch in iter_ds:
    batch_id = batch["id"]
    batch_label = batch["labels"]
    batch_mask = batch["mask"]
    batch_output = compute_batch_outputs(opt_repl.target, batch["image"])

    # TODO(joost,andreas): if we run on multi host, this needs to be used as
    # batch_outputs[0]
    ids.append(batch_id)
    outputs.append(batch_output)
    labels.append(batch_label)
    masks.append(batch_mask)

  ids = jnp.concatenate(ids, axis=1)
  outputs = jnp.concatenate(outputs, axis=1)
  labels = jnp.concatenate(labels, axis=1)
  masks = jnp.concatenate(masks, axis=1)

  # NOTE(joost,andreas): due to batch padding, entropies/ids will be of size:
  # if training set size % batch size > 0:
  # (training set size // batch size + 1) * batch size
  # else:
  # just training set size

  return ids, outputs, labels, masks


def get_entropy_scores(logits, masks):
  """Obtain scores using entropy scoring.

  Args:
    logits: the logits of the pool set.
    masks: the masks belonging to the pool set.

  Returns:
    a list of scores belonging to the pool set.
  """
  log_probs = jax.nn.log_softmax(logits)
  probs = jax.nn.softmax(logits)

  weighted_nats = -probs * log_probs
  # One simple trick to avoid NaNs later on.
  weighted_nats = jnp.where(jnp.isnan(weighted_nats), 0, weighted_nats)
  entropy = jnp.sum(weighted_nats, axis=-1, keepdims=False)
  entropy = jnp.where(masks, entropy, NINF_SCORE)

  return entropy


def get_margin_scores(logits, masks):
  """Obtain scores using margin scoring.

  Args:
    logits: the logits of the pool set.
    masks: the masks belonging to the pool set.

  Returns:
    a list of scores belonging to the pool set.
  """
  probs = jax.nn.softmax(logits)
  sorted_probs = jnp.take_along_axis(
      probs, jnp.argsort(probs, axis=-1), axis=-1)
  margins = sorted_probs[..., -1] - sorted_probs[..., -2]

  # Higher is better, so we invert the scores.
  margin_scores = -margins
  margin_scores = jnp.where(masks, margin_scores, NINF_SCORE)

  return margin_scores


def get_uniform_scores(masks, rng):
  """Obtain scores using uniform sampling.

  Args:
    masks: the masks belonging to the pool set.
    rng: the RNG to use for uniform sampling.

  Returns:
    a list of scores belonging to the pool set.
  """
  uniform_scores = jax.random.uniform(key=rng, shape=masks.shape)
  uniform_scores = jnp.where(masks, uniform_scores, NINF_SCORE)

  return uniform_scores


def get_density_scores(*, model, opt_repl, train_ds, pool_pre_logits,
                       pool_masks):
  """Obtain scores using density method.

  Args:
    model: an initialized model.
    opt_repl: the current optimizer.
    train_ds: the dataset to fit the density estimator on.
    pool_pre_logits: the pre logits (features) of the pool set.
    pool_masks: the masks belonging to the pool_pre_logits.

  Returns:
    a list of scores belonging to the pool set.
  """
  # Fit LDA
  _, train_pre_logits, train_labels, train_masks = get_ids_logits_masks(
      model=model, opt_repl=opt_repl, ds=train_ds, pre_logits=True)

  train_masks_bool = train_masks.astype(bool)
  train_pre_logits = train_pre_logits[train_masks_bool].reshape(
      -1, train_pre_logits.shape[-1])
  train_labels = np.argmax(train_labels[train_masks_bool], axis=-1).ravel()

  mean_list, cov = ood_utils.compute_mean_and_cov(train_pre_logits,
                                                  train_labels)

  # Evaluate LDA on pool set
  pool_pre_logits = pool_pre_logits.reshape(-1, pool_pre_logits.shape[-1])
  dists = ood_utils.compute_mahalanobis_distance(pool_pre_logits, mean_list,
                                                 cov)
  scores = np.array(jax.nn.logsumexp(-dists / 2, axis=-1))

  # Convert likelihood to AL score
  pool_masks_bool = np.array(pool_masks.ravel(), dtype=bool)
  scores[pool_masks_bool] = (
      scores[pool_masks_bool].max() - scores[pool_masks_bool])
  scores[~pool_masks_bool] = NINF_SCORE

  return scores


def select_acquisition_batch_indices(*, acquisition_batch_size, scores, ids,
                                     ignored_ids):
  """Select what data points to acquire from the pool set.

  Args:
    acquisition_batch_size: the number of data point to acquire.
    scores: acquisition scores assigned to data points.
    ids: the ids belonging to the scores.
    ignored_ids: the ids to ignore (previously acquired).

  Returns:
    a tuple of lists with the ids to be acquired and their scores.
  """
  scores = np.array(scores.ravel())
  ids = np.array(ids.ravel())

  # Ignore already acquired ids
  # TODO(joost,andreas): vectorize this
  ids_list = ids.tolist()
  for ignored_id in ignored_ids:
    scores[ids_list.index(ignored_id)] = NINF_SCORE

  f_ent = scores[scores > NINF_SCORE]
  print(f"Score statistics pool set - "
        f"min: {f_ent.min()}, mean: {f_ent.mean()}, max: {f_ent.max()}")

  partitioned_scorers = np.argpartition(-scores, acquisition_batch_size)
  top_scorers = partitioned_scorers[:acquisition_batch_size]

  top_ids = ids[top_scorers].tolist()
  top_scores = scores[top_scorers].tolist()

  print(f"Data selected - ids: {top_ids}, with scores: {top_scores}")

  return top_ids, top_scores


def get_accuracy(*, evaluation_fn, opt_repl, ds, prefetch_to_device=1):
  """A helper function to obtain accuracy over a dataset.

  Args:
    evaluation_fn: a function that evaluates a forward pass in a model.
    opt_repl: an optimizer with parameters.
    ds: a dataset.
    prefetch_to_device: number of batches to prefetc (default: 1).

  Returns:
    The accuracy as a float between 0 and 1.
  """
  iter_ds = input_utils.start_input_pipeline(ds, prefetch_to_device)

  ncorrect, nseen = [], []
  for batch in iter_ds:
    batch_ncorrect, _, batch_n, _ = evaluation_fn(opt_repl.target,
                                                  batch["image"],
                                                  batch["labels"],
                                                  batch["mask"])

    ncorrect += [batch_ncorrect[0]]
    nseen += [batch_n[0]]

  ncorrect = np.sum(ncorrect)
  nseen = np.sum(nseen)

  return ncorrect / nseen


def finetune(*,
             update_fn,
             opt_repl,
             lr_fn,
             ds,
             rngs_loop,
             total_steps,
             train_eval_ds,
             val_ds,
             evaluation_fn,
             early_stopping_patience,
             prefetch_to_device=1):
  """Finetunes a model on a dataset.

  Args:
    update_fn: a function that updates the model given relevant inputs.
    opt_repl: the optimizer.
    lr_fn: a function that returns the learning rate given a step.
    ds: the dataset to finetune on.
    rngs_loop: the rng for the loop.
    total_steps: the total number of fine-tuning steps to take.
    train_eval_ds: train dataset in eval mode (no augmentation or shuffling).
    val_ds: validation dataset for early stopping.
    evaluation_fn: function used for evaluation on validation set.
    early_stopping_patience: number of steps to wait before stopping training.
    prefetch_to_device: number of batches to prefetc (default: 1).

  Returns:
    The optimizer with updated parameters and the updated rng.
  """
  iter_ds = input_utils.start_input_pipeline(ds, prefetch_to_device)
  lr_iter = train_utils.prefetch_scalar(
      map(lr_fn, range(total_steps)), prefetch_to_device)

  best_opt_accuracy = -1
  best_step = 1

  train_accuracies = []
  val_accuracies = []

  for current_step, train_batch, lr_repl in zip(
      tqdm.trange(1, total_steps + 1), iter_ds, lr_iter):
    opt_repl, _, rngs_loop, _ = update_fn(opt_repl, lr_repl,
                                          train_batch["image"],
                                          train_batch["labels"], rngs_loop)

    if current_step % 5 == 0:
      train_accuracy = get_accuracy(
          evaluation_fn=evaluation_fn, opt_repl=opt_repl, ds=train_eval_ds)
      val_accuracy = get_accuracy(
          evaluation_fn=evaluation_fn, opt_repl=opt_repl, ds=val_ds)
      print(f"Current accuracy - train:{train_accuracy}, val: {val_accuracy}")
      train_accuracies.append((current_step, train_accuracy))
      val_accuracies.append((current_step, val_accuracy))

      if val_accuracy >= best_opt_accuracy:
        best_step = current_step
        best_opt_accuracy = val_accuracy
        best_opt_repl = jax.device_get(opt_repl)
      else:
        print("Current val accuracy", val_accuracy, f"(vs {best_opt_accuracy})")
        if current_step - best_step >= early_stopping_patience:
          print("Early stopping, returning best opt_repl!")
          break

  # best_opt_repl could be unassigned, but we should error out then

  info = dict(
      best_val_accuracy=best_opt_accuracy,
      best_step=best_step,
      train_accuracies=train_accuracies,
      val_accuracies=val_accuracies)

  return best_opt_repl, rngs_loop, info


def make_init_fn(model, image_shape, local_batch_size, config):
  """Make the init function.

  Args:
    model: The model to init.
    image_shape: The shape of the input images.
    local_batch_size: the local device batch size.
    config: the full config for the experiment.

  Returns:
    The init function
  """

  @partial(jax.jit, backend="cpu")
  def init(rng):
    dummy_input = jnp.zeros((local_batch_size,) + image_shape, jnp.float32)

    params = flax.core.unfreeze(model.init(rng, dummy_input,
                                           train=False))["params"]

    # Set bias in the head to a low value, such that loss is small initially.
    params["head"]["bias"] = jnp.full_like(params["head"]["bias"],
                                           config.get("init_head_bias", 0))

    # init head kernel to all zeros for fine-tuning
    if config.get("model_init"):
      params["head"]["kernel"] = jnp.full_like(params["head"]["kernel"], 0)

    return params

  return init


def make_update_fn(model, config):
  """Make the update function.

  Args:
    model: The model to be used in updates.
    config: The config of the experiment.

  Returns:
    The function that updates the model for one step.
  """

  @partial(jax.pmap, axis_name="batch", donate_argnums=(0,))
  def update_fn(opt, lr, images, labels, rng):
    """Update step."""

    measurements = {}

    # Get device-specific loss rng.
    rng, rng_model = jax.random.split(rng, 2)
    rng_model_local = jax.random.fold_in(rng_model, jax.lax.axis_index("batch"))

    def loss_fn(params, images, labels):
      logits, _ = model.apply(
          {"params": flax.core.freeze(params)},
          images,
          train=True,
          rngs={"dropout": rng_model_local},
      )
      return getattr(train_utils, config.get("loss", "sigmoid_xent"))(
          logits=logits, labels=labels)

    # Implementation considerations compared and summarized at
    # https://docs.google.com/document/d/1g3kMEvqu1DOawaflKNyUsIoQ4yIVEoyE5ZlIPkIl4Lc/edit?hl=en#
    l, g = train_utils.accumulate_gradient(
        jax.value_and_grad(loss_fn),
        opt.target,
        images,
        labels,
        config.get("grad_accum_steps"),
    )
    l, g = jax.lax.pmean((l, g), axis_name="batch")

    # Log the gradient norm only if we need to compute it anyways (clipping)
    # or if we don't use grad_accum_steps, as they interact badly.
    if config.get("grad_accum_steps", 1) == 1 or config.get("grad_clip_norm"):
      grads, _ = jax.tree_flatten(g)
      l2_g = jnp.sqrt(sum([jnp.vdot(p, p) for p in grads]))
      measurements["l2_grads"] = l2_g

    # Optionally resize the global gradient to a maximum norm. We found this
    # useful in some cases across optimizers, hence it's in the main loop.
    if config.get("grad_clip_norm"):
      g_factor = jnp.minimum(1.0, config.grad_clip_norm / l2_g)
      g = jax.tree_util.tree_map(lambda p: g_factor * p, g)
    opt = opt.apply_gradient(g, learning_rate=lr)

    decay_rules = config.get("weight_decay", []) or []
    if isinstance(decay_rules, numbers.Number):
      decay_rules = [(".*kernel.*", decay_rules)]
    sched_m = lr / config.lr.base if config.get("weight_decay_decouple") else lr

    def decay_fn(v, wd):
      return (1.0 - sched_m * wd) * v

    opt = opt.replace(
        target=train_utils.tree_map_with_regex(decay_fn, opt.target,
                                               decay_rules))

    params, _ = jax.tree_flatten(opt.target)
    measurements["l2_params"] = jnp.sqrt(sum([jnp.vdot(p, p) for p in params]))

    return opt, l, rng, measurements

  return update_fn


def make_evaluation_fn(model, config):
  """Make evaluation function.

  Args:
    model: The model to be used in evaluation.
    config: The config of the experiment.

  Returns:
    The evaluation function.
  """

  @partial(jax.pmap, axis_name="batch")
  def evaluation_fn(params, images, labels, mask):
    # Ignore the entries with all zero labels for evaluation.
    mask *= labels.max(axis=1)
    logits, out = model.apply({"params": flax.core.freeze(params)},
                              images,
                              train=False)

    losses = getattr(train_utils, config.get("loss", "sigmoid_xent"))(
        logits=logits, labels=labels, reduction=False)
    loss = jax.lax.psum(losses * mask, axis_name="batch")

    top1_idx = jnp.argmax(logits, axis=1)
    # Extracts the label at the highest logit index for each image.
    top1_correct = jnp.take_along_axis(labels, top1_idx[:, None], axis=1)[:, 0]
    ncorrect = jax.lax.psum(top1_correct * mask, axis_name="batch")
    n = jax.lax.psum(mask, axis_name="batch")

    # NOTE: this works on multi host devices already
    metric_args = jax.lax.all_gather([logits, labels, out["pre_logits"], mask],
                                     axis_name="batch")

    return ncorrect, loss, n, metric_args

  return evaluation_fn


def main(config):
  print(config)
  acquisition_method = config.get("acquisition_method")

  # This used to be a hack for speed things up, but also we OOM without it now...
  config.pp_eval = config.pp_eval.replace("384", "224")
  config.pp_train = config.pp_train.replace("384", "224")
  config.grad_accum_steps = 8  # From ViT repo as sensible value - OoM otherwise

  # Keep the ID for filtering the pool set
  keep_id = 'keep(["image", "labels", "id"])'
  # HACK: assumes the keep is at the end
  id_pp_eval_split = config.pp_eval.split("|")
  id_pp_eval = "|".join(id_pp_eval_split[:-1] + [keep_id])

  # Download dataset
  data_builder = tfds.builder("cifar10")
  data_builder.download_and_prepare()

  seed = config.get("seed", 0)
  rng = jax.random.PRNGKey(seed)

  batch_size = config.batch_size
  batch_size_eval = config.get("batch_size_eval", batch_size)

  local_batch_size = batch_size // jax.process_count()
  local_batch_size_eval = batch_size_eval // jax.process_count()

  val_ds = input_utils.get_data(
      dataset=config.dataset,
      split=config.val_split,
      rng=None,
      process_batch_size=local_batch_size_eval,
      preprocess_fn=preprocess_spec.parse(
          spec=config.pp_eval, available_ops=preprocess_utils.all_ops()),
      shuffle=False,
      prefetch_size=config.get("prefetch_to_host", 2),
      num_epochs=1,  # Only repeat once.
  )

  test_ds = input_utils.get_data(
      dataset=config.dataset,
      split="test",
      rng=None,
      process_batch_size=local_batch_size_eval,
      preprocess_fn=preprocess_spec.parse(
          spec=config.pp_eval, available_ops=preprocess_utils.all_ops()),
      shuffle=False,
      prefetch_size=config.get("prefetch_to_host", 2),
      num_epochs=1,  # Only repeat once.
  )

  model = ub.models.vision_transformer(
      num_classes=config.num_classes, **config.get("model", {}))

  image_shape = tuple(test_ds.element_spec["image"].shape[2:])
  init = make_init_fn(model, image_shape, local_batch_size, config)

  rng, rng_init = jax.random.split(rng)
  params_cpu = init(rng_init)

  # Load the optimizer from flax.
  opt_name = config.get("optim_name")
  opt_def = getattr(flax.optim, opt_name)(**config.get("optim", {}))

  # We jit this, such that the arrays that are created on the same
  # device as the input is, in this case the CPU. Else they'd be on device[0].
  opt_cpu = jax.jit(opt_def.create)(params_cpu)

  reinit_params = config.get("model_reinit_params",
                             ("head/kernel", "head/bias"))
  loaded = checkpoint_utils.load_from_pretrained_checkpoint(
      params_cpu,
      config.model_init,
      config.model.representation_size,
      config.model.classifier,
      reinit_params,
  )

  opt_cpu = opt_cpu.replace(target=loaded)

  # TODO(joost,andreas): This shouldn't be needed but opt_cpu is being donated otherwise.
  # Ensure opt_cpu is really on the cpu this way.
  opt_cpu = jax.device_get(opt_cpu)

  update_fn = make_update_fn(model, config)
  evaluation_fn = make_evaluation_fn(model, config)

  pool_subset_data_builder = tfds.builder(
      "cifar10_subset", subset_ids={
          config.train_split: None,
          "test": None
      })
  pool_subset_data_builder.download_and_prepare()

  rng, pool_ds_rng = jax.random.split(rng)

  # NOTE: below line is necessary on multi host setup
  # pool_ds_rng = jax.random.fold_in(pool_ds_rng, jax.process_index())

  pool_train_ds = input_utils.get_data(
      dataset=pool_subset_data_builder,
      split=config.train_split,
      rng=pool_ds_rng,
      process_batch_size=local_batch_size,
      preprocess_fn=preprocess_spec.parse(
          spec=id_pp_eval, available_ops=preprocess_utils.all_ops()),
      shuffle=False,
      drop_remainder=False,
      prefetch_size=config.get("prefetch_to_host", 2),
      num_epochs=1,  # Don't repeat
  )

  # Potentially acquire an initial training set.
  initial_training_set_size = config.get("initial_training_set_size", 10)

  if initial_training_set_size > 0:
    current_opt_repl = flax_utils.replicate(opt_cpu)
    pool_ids, _, _, pool_masks = get_ids_logits_masks(
        model=model,
        opt_repl=current_opt_repl,
        ds=pool_train_ds,
    )

    rng, initial_uniform_rng = jax.random.split(rng)
    pool_scores = get_uniform_scores(pool_masks, initial_uniform_rng)

    initial_training_set_batch_ids, _ = select_acquisition_batch_indices(
        acquisition_batch_size=initial_training_set_size,
        scores=pool_scores,
        ids=pool_ids,
        ignored_ids=set(),
    )
  else:
    initial_training_set_batch_ids = []

  wandb.summary["initial_training_set_batch_ids"] = list(
      initial_training_set_batch_ids)

  # NOTE: if we could `enumerate` before `filter` in `create_dataset` of CLU
  # then this dataset creation could be simplified.
  # https://github.com/google/CommonLoopUtils/blob/main/clu/deterministic_data.py#L340
  # CLU is explicitly not accepting outside contributions at the moment.
  train_subset_data_builder = tfds.builder(
      "cifar10_subset",
      subset_ids={
          config.train_split: set(initial_training_set_batch_ids),
          "test": None
      })
  train_subset_data_builder.download_and_prepare()

  test_accuracies = []
  training_sizes = []

  rng, rng_loop = jax.random.split(rng)
  rngs_loop = flax_utils.replicate(rng_loop)

  # NOTE: it's VITAL train_ds_rng is used for all train_ds creations
  # TODO(andreas): fix the comment to explain instead of instilling fear :D
  rng, train_ds_rng = jax.random.split(rng)

  while True:
    # Python 3.8 would allow for := in the while expression to have this succinct and avoid code duplication.
    current_train_ds_length = len(
        train_subset_data_builder.subset_ids[config.train_split])
    if current_train_ds_length >= config.get("max_training_set_size", 150):
      break
    print(f"Training set size: {current_train_ds_length}")

    current_opt_repl = flax_utils.replicate(opt_cpu)

    # Only fine-tune if there is anything to fine-tune with.
    if current_train_ds_length > 0:
      # Repeat dataset to have oversampled epochs and bootstrap more batches
      number_of_batches = current_train_ds_length / config.batch_size
      num_repeats = math.ceil(config.total_steps / number_of_batches)
      print(f"Repeating dataset {num_repeats} times")

      repeated_train_ds = input_utils.get_data(
          dataset=train_subset_data_builder,
          split=config.train_split,
          rng=train_ds_rng,
          process_batch_size=local_batch_size,
          preprocess_fn=preprocess_spec.parse(
              spec=config.pp_train, available_ops=preprocess_utils.all_ops()),
          shuffle_buffer_size=config.shuffle_buffer_size,
          prefetch_size=config.get("prefetch_to_host", 2),
          # TODO(joost,andreas): double check if below leads to bootstrap sampling.
          num_epochs=num_repeats,
      )

      train_eval_ds = input_utils.get_data(
          dataset=train_subset_data_builder,
          split=config.train_split,
          rng=train_ds_rng,
          process_batch_size=local_batch_size,
          preprocess_fn=preprocess_spec.parse(
              spec=config.pp_eval, available_ops=preprocess_utils.all_ops()),
          shuffle=False,
          drop_remainder=False,
          prefetch_size=config.get("prefetch_to_host", 2),
          num_epochs=1,
      )

      # NOTE: warmup and decay are not a good fit for the small training set
      # lr_fn = train_utils.create_learning_rate_schedule(config.total_steps,
      #                                                   **config.get('lr', {})
      #                                                   )
      lr_fn = lambda x: config.lr.base

      early_stopping_patience = config.get("early_stopping_patience", 15)
      current_opt_repl, rngs_loop, info = finetune(
          update_fn=update_fn,
          opt_repl=current_opt_repl,
          lr_fn=lr_fn,
          ds=repeated_train_ds,
          rngs_loop=rngs_loop,
          total_steps=config.total_steps,
          train_eval_ds=train_eval_ds,
          val_ds=val_ds,
          evaluation_fn=evaluation_fn,
          early_stopping_patience=early_stopping_patience)

      train_accuracy_table = wandb.Table(
          data=info["train_accuracies"],
          columns=["batch step", "train_accuracy"])
      val_accuracy_table = wandb.Table(
          data=info["val_accuracies"], columns=["batch step", "val_accuracy"])
      wandb.log(
          {
              "finetune/train_accuracy":
                  wandb.plot.line(
                      train_accuracy_table,
                      "batch step",
                      "train accuracy",
                      title="Training Accuracy"),
              "finetune/val_accuracy":
                  wandb.plot.line(
                      val_accuracy_table,
                      "batch step",
                      "val accuracy",
                      title="Validation Accuracy"),
              "finetune/best_step":
                  info["best_step"],
              "finetune/best_val_accuracy":
                  info["best_val_accuracy"]
          },
          step=current_train_ds_length)

    test_accuracy = get_accuracy(
        evaluation_fn=evaluation_fn, opt_repl=current_opt_repl, ds=test_ds)

    print(f"Accuracy at {current_train_ds_length}: {test_accuracy}")

    test_accuracies.append(test_accuracy)
    training_sizes.append(current_train_ds_length)
    wandb.log(dict(test_accuracy=test_accuracy), step=current_train_ds_length)

    pool_ids, pool_outputs, _, pool_masks = get_ids_logits_masks(
        model=model,
        opt_repl=current_opt_repl,
        ds=pool_train_ds,
        pre_logits=acquisition_method == "density")

    if acquisition_method == "uniform":
      rng, rng_loop = jax.random.split(rng, 2)
      pool_scores = get_uniform_scores(pool_masks, rng)
    elif acquisition_method == "entropy":
      pool_scores = get_entropy_scores(pool_outputs, pool_masks)
    elif acquisition_method == "margin":
      pool_scores = get_margin_scores(pool_outputs, pool_masks)
    elif acquisition_method == "density":
      if current_train_ds_length > 0:
        feature_train_ds = input_utils.get_data(
            dataset=train_subset_data_builder,
            split=config.train_split,
            rng=train_ds_rng,
            process_batch_size=local_batch_size,
            preprocess_fn=preprocess_spec.parse(
                spec=id_pp_eval, available_ops=preprocess_utils.all_ops()),
            shuffle=False,
            drop_remainder=False,
            prefetch_size=config.get("prefetch_to_host", 2),
            num_epochs=1)

        pool_scores = get_density_scores(
            model=model,
            opt_repl=current_opt_repl,
            train_ds=feature_train_ds,
            pool_pre_logits=pool_outputs,
            pool_masks=pool_masks)
      else:
        rng, rng_loop = jax.random.split(rng, 2)
        pool_scores = get_uniform_scores(pool_masks, rng)
    else:
      raise ValueError("Acquisition method not found.")

    acquisition_batch_ids, acquisition_batch_scores = select_acquisition_batch_indices(
        acquisition_batch_size=config.get("acquisition_batch_size", 10),
        scores=pool_scores,
        ids=pool_ids,
        ignored_ids=train_subset_data_builder.subset_ids[config.train_split])

    train_subset_data_builder.subset_ids[config.train_split].update(
        acquisition_batch_ids)

    acquistion_batch_table = wandb.Table(
        data=list(zip(acquisition_batch_ids, acquisition_batch_scores)),
        columns=["Id", "Score"])
    wandb.log(
        dict(acquisition_batch=acquistion_batch_table,),
        step=current_train_ds_length)

  print(f"Final acquired training ids: "
        f"{train_subset_data_builder.subset_ids[config.train_split]}"
        f"Accuracies: {test_accuracies}")

  # TODO(joost,andreas): save the final checkpoint
  return (train_subset_data_builder.subset_ids[config.train_split],
          test_accuracies)


if __name__ == "__main__":
  jax.config.config_with_absl()

  def _main(argv):
    del argv
    config = FLAGS.config
    config.acquisition_method = FLAGS.acquisition_method
    config.max_training_set_size = FLAGS.max_training_set_size
    config.initial_training_set_size = FLAGS.initial_training_set_size
    config.acquisition_batch_size = FLAGS.acquisition_batch_size
    config.early_stopping_patience = FLAGS.early_stopping_patience
    config.seed = FLAGS.seed

    wandb_config = dict(
        acquisition_method=FLAGS.acquisition_method,
        max_training_set_size=FLAGS.max_training_set_size,
        initial_training_set_size=FLAGS.initial_training_set_size,
        acquisition_batch_size=FLAGS.acquisition_batch_size,
        early_stopping_patience=FLAGS.early_stopping_patience,
        seed=FLAGS.seed)

    wandb.init(
        project="rdl-active-learning",
        entity="oatml-andreas-kirsch",
        config=wandb_config)

    if jax.device_count() < 8:
      raise RuntimeError("Expected >=8 devices, only found ",
                         jax.device_count())

    main(config)

  app.run(_main)  # Ignore the returned values from `main`.
