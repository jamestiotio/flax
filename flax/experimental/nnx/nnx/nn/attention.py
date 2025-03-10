# Copyright 2023 The Flax Authors.
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

"""Attention core modules for Flax."""

from __future__ import annotations

import functools
from typing import Any, Callable, Optional, Tuple, overload

import jax
import jax.numpy as jnp
from jax import lax, random

from flax.experimental import nnx
from flax.experimental.nnx.nnx import rnglib
from flax.experimental.nnx.nnx.flaglib import flags
from flax.experimental.nnx.nnx.module import Module, first_from
from flax.experimental.nnx.nnx.nn import initializers
from flax.experimental.nnx.nnx.nn.dtypes import promote_dtype
from flax.experimental.nnx.nnx.nn.linear import (
  DotGeneralT,
  LinearGeneral,
  PrecisionLike,
  default_kernel_init,
)
from flax.experimental.nnx.nnx.nn.normalization import LayerNorm

Array = jax.Array
Shape = Tuple[int, ...]
Dtype = Any


def dot_product_attention_weights(
  query: Array,
  key: Array,
  bias: Optional[Array] = None,
  mask: Optional[Array] = None,
  broadcast_dropout: bool = True,
  dropout_rng: Optional[Array] = None,
  dropout_rate: float = 0.0,
  deterministic: bool = False,
  dtype: Optional[Dtype] = None,
  precision: PrecisionLike = None,
):
  """Computes dot-product attention weights given query and key.

  Used by :func:`dot_product_attention`, which is what you'll most likely use.
  But if you want access to the attention weights for introspection, then
  you can directly call this function and call einsum yourself.

  Args:
    query: queries for calculating attention with shape of `[batch..., q_length,
      num_heads, qk_depth_per_head]`.
    key: keys for calculating attention with shape of `[batch..., kv_length,
      num_heads, qk_depth_per_head]`.
    bias: bias for the attention weights. This should be broadcastable to the
      shape `[batch..., num_heads, q_length, kv_length]`. This can be used for
      incorporating causal masks, padding masks, proximity bias, etc.
    mask: mask for the attention weights. This should be broadcastable to the
      shape `[batch..., num_heads, q_length, kv_length]`. This can be used for
      incorporating causal masks. Attention weights are masked out if their
      corresponding mask value is `False`.
    broadcast_dropout: bool: use a broadcasted dropout along batch dims.
    dropout_rng: JAX PRNGKey: to be used for dropout
    dropout_rate: dropout rate
    deterministic: bool, deterministic or not (to apply dropout)
    dtype: the dtype of the computation (default: infer from inputs and params)
    precision: numerical precision of the computation see `jax.lax.Precision`
      for details.

  Returns:
    Output of shape `[batch..., num_heads, q_length, kv_length]`.
  """
  query, key = promote_dtype(query, key, dtype=dtype)
  dtype = query.dtype

  assert query.ndim == key.ndim, 'q, k must have same rank.'
  assert query.shape[:-3] == key.shape[:-3], 'q, k batch dims must match.'
  assert query.shape[-2] == key.shape[-2], 'q, k num_heads must match.'
  assert query.shape[-1] == key.shape[-1], 'q, k depths must match.'

  # calculate attention matrix
  depth = query.shape[-1]
  query = query / jnp.sqrt(depth).astype(dtype)
  # attn weight shape is (batch..., num_heads, q_length, kv_length)
  attn_weights = jnp.einsum(
    '...qhd,...khd->...hqk', query, key, precision=precision
  )

  # apply attention bias: masking, dropout, proximity bias, etc.
  if bias is not None:
    attn_weights = attn_weights + bias
  # apply attention mask
  if mask is not None:
    big_neg = jnp.finfo(dtype).min
    attn_weights = jnp.where(mask, attn_weights, big_neg)

  # normalize the attention weights
  attn_weights = jax.nn.softmax(attn_weights).astype(dtype)

  # apply attention dropout
  if not deterministic and dropout_rate > 0.0:
    keep_prob = 1.0 - dropout_rate
    if broadcast_dropout:
      # dropout is broadcast across the batch + head dimensions
      dropout_shape = tuple([1] * (key.ndim - 2)) + attn_weights.shape[-2:]
      keep = random.bernoulli(dropout_rng, keep_prob, dropout_shape)  # type: ignore
    else:
      keep = random.bernoulli(dropout_rng, keep_prob, attn_weights.shape)  # type: ignore
    multiplier = keep.astype(dtype) / jnp.asarray(keep_prob, dtype=dtype)
    attn_weights = attn_weights * multiplier

  return attn_weights


def dot_product_attention(
  query: Array,
  key: Array,
  value: Array,
  bias: Optional[Array] = None,
  mask: Optional[Array] = None,
  broadcast_dropout: bool = True,
  dropout_rng: Optional[Array] = None,
  dropout_rate: float = 0.0,
  deterministic: bool = False,
  dtype: Optional[Dtype] = None,
  precision: PrecisionLike = None,
):
  """Computes dot-product attention given query, key, and value.

  This is the core function for applying attention based on
  https://arxiv.org/abs/1706.03762. It calculates the attention weights given
  query and key and combines the values using the attention weights.

  Note: query, key, value needn't have any batch dimensions.

  Args:
    query: queries for calculating attention with shape of `[batch..., q_length,
      num_heads, qk_depth_per_head]`.
    key: keys for calculating attention with shape of `[batch..., kv_length,
      num_heads, qk_depth_per_head]`.
    value: values to be used in attention with shape of `[batch..., kv_length,
      num_heads, v_depth_per_head]`.
    bias: bias for the attention weights. This should be broadcastable to the
      shape `[batch..., num_heads, q_length, kv_length]`. This can be used for
      incorporating causal masks, padding masks, proximity bias, etc.
    mask: mask for the attention weights. This should be broadcastable to the
      shape `[batch..., num_heads, q_length, kv_length]`. This can be used for
      incorporating causal masks. Attention weights are masked out if their
      corresponding mask value is `False`.
    broadcast_dropout: bool: use a broadcasted dropout along batch dims.
    dropout_rng: JAX PRNGKey: to be used for dropout
    dropout_rate: dropout rate
    deterministic: bool, deterministic or not (to apply dropout)
    dtype: the dtype of the computation (default: infer from inputs)
    precision: numerical precision of the computation see `jax.lax.Precision`
      for details.

  Returns:
    Output of shape `[batch..., q_length, num_heads, v_depth_per_head]`.
  """
  query, key, value = promote_dtype(query, key, value, dtype=dtype)
  dtype = query.dtype
  assert key.ndim == query.ndim == value.ndim, 'q, k, v must have same rank.'
  assert (
    query.shape[:-3] == key.shape[:-3] == value.shape[:-3]
  ), 'q, k, v batch dims must match.'
  assert (
    query.shape[-2] == key.shape[-2] == value.shape[-2]
  ), 'q, k, v num_heads must match.'
  assert key.shape[-3] == value.shape[-3], 'k, v lengths must match.'

  # compute attention weights
  attn_weights = dot_product_attention_weights(
    query,
    key,
    bias,
    mask,
    broadcast_dropout,
    dropout_rng,
    dropout_rate,
    deterministic,
    dtype,
    precision,
  )

  # return weighted sum over values for each query position
  return jnp.einsum(
    '...hqk,...khd->...qhd', attn_weights, value, precision=precision
  )


class MultiHeadAttention(Module):
  """Multi-head attention.

  Example usage::

    >>> import flax.linen as nn
    >>> import jax

    >>> layer = nn.MultiHeadAttention(num_heads=8, qkv_features=16)
    >>> key1, key2, key3, key4, key5, key6 = jax.random.split(jax.random.key(0), 6)
    >>> shape = (4, 3, 2, 5)
    >>> q, k, v = jax.random.uniform(key1, shape), jax.random.uniform(key2, shape), jax.random.uniform(key3, shape)
    >>> variables = layer.init(jax.random.key(0), q)

    >>> # different inputs for inputs_q, inputs_k and inputs_v
    >>> out = layer.apply(variables, q, k, v)
    >>> # equivalent to layer.apply(variables, inputs_q=q, inputs_k=k, inputs_v=k)
    >>> out = layer.apply(variables, q, k)
    >>> # equivalent to layer.apply(variables, inputs_q=q, inputs_k=q) and layer.apply(variables, inputs_q=q, inputs_k=q, inputs_v=q)
    >>> out = layer.apply(variables, q)

    >>> attention_kwargs = dict(
    ...     num_heads=8,
    ...     qkv_features=16,
    ...     kernel_init=nn.initializers.ones,
    ...     bias_init=nn.initializers.zeros,
    ...     dropout_rate=0.5,
    ...     deterministic=False,
    ...     )
    >>> class Module(nn.Module):
    ...   attention_kwargs: dict
    ...
    ...   @nn.compact
    ...   def __call__(self, x, dropout_rng=None):
    ...     out1 = nn.MultiHeadAttention(**self.attention_kwargs)(x, dropout_rng=dropout_rng)
    ...     out2 = nn.MultiHeadAttention(**self.attention_kwargs)(x, dropout_rng=dropout_rng)
    ...     return out1, out2
    >>> module = Module(attention_kwargs)
    >>> variables = module.init({'params': key1, 'dropout': key2}, q)

    >>> # out1 and out2 are different.
    >>> out1, out2 = module.apply(variables, q, rngs={'dropout': key3})
    >>> # out3 and out4 are different.
    >>> # out1 and out3 are different. out2 and out4 are different.
    >>> out3, out4 = module.apply(variables, q, rngs={'dropout': key4})
    >>> # out1 and out2 are the same.
    >>> out1, out2 = module.apply(variables, q, dropout_rng=key5)
    >>> # out1 and out2 are the same as out3 and out4.
    >>> # providing a `dropout_rng` arg will take precedence over the `rngs` arg in `.apply`
    >>> out3, out4 = module.apply(variables, q, rngs={'dropout': key6}, dropout_rng=key5)

  Attributes:
    num_heads: number of attention heads. Features (i.e. inputs_q.shape[-1])
      should be divisible by the number of heads.
    dtype: the dtype of the computation (default: infer from inputs and params)
    param_dtype: the dtype passed to parameter initializers (default: float32)
    qkv_features: dimension of the key, query, and value.
    out_features: dimension of the last projection
    broadcast_dropout: bool: use a broadcasted dropout along batch dims.
    dropout_rate: dropout rate
    deterministic: if false, the attention weight is masked randomly using
      dropout, whereas if true, the attention weights are deterministic.
    precision: numerical precision of the computation see `jax.lax.Precision`
      for details.
    kernel_init: initializer for the kernel of the Dense layers.
    bias_init: initializer for the bias of the Dense layers.
    use_bias: bool: whether pointwise QKVO dense transforms use bias.
    attention_fn: dot_product_attention or compatible function. Accepts query,
      key, value, and returns output of shape `[bs, dim1, dim2, ..., dimN,,
      num_heads, value_channels]``
    decode: whether to prepare and use an autoregressive cache.
    normalize_qk: should QK normalization be applied (arxiv.org/abs/2302.05442).
  """

  def __init__(
    self,
    num_heads: int,
    features_in: int,
    out_features: int | None = None,
    *,
    dtype: Dtype | None = None,
    param_dtype: Dtype = jnp.float32,
    broadcast_dropout: bool = True,
    dropout_rate: float = 0.0,
    deterministic: bool | None = None,
    precision: PrecisionLike = None,
    kernel_init: initializers.Initializer = default_kernel_init,
    bias_init: initializers.Initializer = initializers.zeros(),
    use_bias: bool = True,
    attention_fn: Callable[..., Array] = dot_product_attention,
    decode: bool = False,
    normalize_qk: bool = False,
    # Deprecated, will be removed.
    qkv_dot_general: DotGeneralT | None = None,
    out_dot_general: DotGeneralT | None = None,
    qkv_dot_general_cls: Any = None,
    out_dot_general_cls: Any = None,
    rngs: rnglib.Rngs,
  ):
    self.num_heads = num_heads
    self.features_in = features_in
    self.features_out = (
      out_features if out_features is not None else features_in
    )
    self.dtype = dtype
    self.param_dtype = param_dtype
    self.broadcast_dropout = broadcast_dropout
    self.dropout_rate = dropout_rate
    self.deterministic = deterministic
    self.precision = precision
    self.kernel_init = kernel_init
    self.bias_init = bias_init
    self.use_bias = use_bias
    self.attention_fn = attention_fn
    self.decode = decode
    self.normalize_qk = normalize_qk
    self.qkv_dot_general = qkv_dot_general
    self.out_dot_general = out_dot_general
    self.qkv_dot_general_cls = qkv_dot_general_cls
    self.out_dot_general_cls = out_dot_general_cls

    if self.features_out % self.num_heads != 0:
      raise ValueError(
        f"'features_out' ({self.features_out}) must be divisible by "
        f"'num_heads' heads ({self.num_heads})."
      )

    self.head_dim = self.features_out // self.num_heads

    linear_general = functools.partial(
      LinearGeneral,
      features_in=self.features_in,
      features_out=(self.num_heads, self.head_dim),
      dtype=self.dtype,
      param_dtype=self.param_dtype,
      kernel_init=self.kernel_init,
      bias_init=self.bias_init,
      use_bias=self.use_bias,
      precision=self.precision,
      dot_general=self.qkv_dot_general,
      dot_general_cls=self.qkv_dot_general_cls,
    )
    # project inputs_q to multi-headed q/k/v
    # dimensions are then [batch..., length, n_heads, n_features_per_head]
    self.query = linear_general(rngs=rngs)
    self.key = linear_general(rngs=rngs)
    self.value = linear_general(rngs=rngs)

    if self.normalize_qk:
      # Normalizing query and key projections stabilizes training with higher
      # LR. See ViT-22B paper http://arxiv.org/abs/2302.05442 for analysis.
      self.query_ln = LayerNorm(self.head_dim, use_bias=False, rngs=rngs)
      self.key_ln = LayerNorm(self.head_dim, use_bias=False, rngs=rngs)
    else:
      self.query_ln = None
      self.key_ln = None

    self.out = LinearGeneral(
      features_in=(self.num_heads, self.head_dim),
      features_out=self.features_out,
      axis=(-2, -1),
      kernel_init=self.kernel_init,
      bias_init=self.bias_init,
      use_bias=self.use_bias,
      dtype=self.dtype,
      param_dtype=self.param_dtype,
      precision=self.precision,
      dot_general=self.out_dot_general,
      dot_general_cls=self.out_dot_general_cls,
      rngs=rngs,
    )

  @overload
  def __call__(
    self,
    inputs_q: Array,
    inputs_k: Optional[Array] = None,
    inputs_v: Optional[Array] = None,
    *,
    mask: Optional[Array] = None,
    deterministic: Optional[bool] = None,
    dropout_rng: Optional[Array] = None,
    rngs: rnglib.Rngs | None = None,
  ):
    ...

  @overload
  def __call__(
    self,
    inputs_q: Array,
    *,
    inputs_kv: Array | None = None,
    mask: Array | None = None,
    deterministic: bool | None = None,
    dropout_rng: Array | None = None,
    rngs: rnglib.Rngs | None = None,
  ):
    ...

  def __call__(
    self,
    inputs_q: Array,
    inputs_k: Array | None = None,
    inputs_v: Array | None = None,
    *,
    mask: Array | None = None,
    deterministic: bool | None = None,
    rngs: rnglib.Rngs | None = None,
  ):
    """Applies multi-head dot product attention on the input data.

    Projects the inputs into multi-headed query, key, and value vectors,
    applies dot-product attention and project the results to an output vector.

    If both inputs_k and inputs_v are None, they will both copy the value of
    inputs_q (self attention).
    If only inputs_v is None, it will copy the value of inputs_k.

    Args:
      inputs_q: input queries of shape `[batch_sizes..., length, features]`.
      inputs_k: key of shape `[batch_sizes..., length, features]`. If None,
        inputs_k will copy the value of inputs_q.
      inputs_v: values of shape `[batch_sizes..., length, features]`. If None,
        inputs_v will copy the value of inputs_k.
      mask: attention mask of shape `[batch_sizes..., num_heads, query_length,
        key/value_length]`. Attention weights are masked out if their
        corresponding mask value is `False`.
      deterministic: if false, the attention weight is masked randomly using
        dropout, whereas if true, the attention weights are deterministic.
      rngs: container for random number generators to generate the dropout
        mask when `deterministic` is False. The `rngs` container should have a
        `dropout` key.

    Returns:
      output of shape `[batch_sizes..., length, features]`.
    """

    if inputs_k is None:
      if inputs_v is not None:
        raise ValueError(
          '`inputs_k` cannot be None if `inputs_v` is not None. '
          'To have both `inputs_k` and `inputs_v` be the same value, pass in the '
          'value to `inputs_k` and leave `inputs_v` as None.'
        )
      inputs_k = inputs_q
    if inputs_v is None:
      inputs_v = inputs_k

    if inputs_q.shape[-1] != self.features_in:
      raise ValueError(
        f'Incompatible input dimension, got {inputs_q.shape[-1]} '
        f'but module expects {self.features_in}.'
      )

    query = self.query(inputs_q)
    key = self.key(inputs_k)
    value = self.value(inputs_v)

    if self.normalize_qk:
      assert self.query_ln is not None and self.key_ln is not None
      # Normalizing query and key projections stabilizes training with higher
      # LR. See ViT-22B paper http://arxiv.org/abs/2302.05442 for analysis.
      query = self.query_ln(query)
      key = self.key_ln(key)

    # During fast autoregressive decoding, we feed one position at a time,
    # and cache the keys and values step by step.
    if self.decode:
      (
        *batch_dims,
        max_length,
        num_heads,
        depth_per_head,
      ) = self.cached_key.shape
      # shape check of cached keys against query input
      expected_shape = tuple(batch_dims) + (1, num_heads, depth_per_head)
      if expected_shape != query.shape:
        raise ValueError(
          'Autoregressive cache shape error, '
          'expected query shape %s instead got %s.'
          % (expected_shape, query.shape)
        )
      # update key, value caches with our new 1d spatial slices
      cur_index = self.cache_index
      indices = (0,) * len(batch_dims) + (cur_index, 0, 0)
      key = lax.dynamic_update_slice(self.cached_key, key, indices)
      value = lax.dynamic_update_slice(self.cached_value, value, indices)
      self.cached_key = key
      self.cached_value = value
      self.cache_index += 1
      # causal mask for cached decoder self-attention:
      # our single query position should only attend to those key
      # positions that have already been generated and cached,
      # not the remaining zero elements.
      mask = combine_masks(
        mask,
        jnp.broadcast_to(
          jnp.arange(max_length) <= cur_index,
          tuple(batch_dims) + (1, 1, max_length),
        ),
      )

    if (
      self.dropout_rate > 0.0
    ):  # Require `deterministic` only if using dropout.
      deterministic = first_from(
        'deterministic',
        deterministic,
        self.deterministic,
        flags.get('deterministic'),
      )
      if not deterministic:
        if rngs is None:
          raise ValueError(
            "'rngs' must be provided if 'dropout_rng' is not given."
          )
        dropout_rng = rngs.dropout()
      else:
        dropout_rng = None
    else:
      deterministic = True
      dropout_rng = None

    # apply attention
    x = self.attention_fn(
      query,
      key,
      value,
      mask=mask,
      dropout_rng=dropout_rng,
      dropout_rate=self.dropout_rate,
      broadcast_dropout=self.broadcast_dropout,
      deterministic=deterministic,
      dtype=self.dtype,
      precision=self.precision,
    )
    # back to the original inputs dimensions
    out = self.out(x)
    return out

  def init_cache(self, input_shape: Shape, dtype: Dtype = jnp.float32):
    """Initializes cache for fast autoregressive decoding."""
    cache_shape = (*input_shape[:-1], self.num_heads, self.features_out)
    self.cached_key = nnx.Cache(jnp.zeros(cache_shape, dtype))
    self.cached_value = nnx.Cache(jnp.zeros(cache_shape, dtype))
    self.cache_index = nnx.Cache(jnp.array(0, dtype=jnp.int32))


# mask-making utility functions


def make_attention_mask(
  query_input: Array,
  key_input: Array,
  pairwise_fn: Callable[..., Any] = jnp.multiply,
  extra_batch_dims: int = 0,
  dtype: Dtype = jnp.float32,
):
  """Mask-making helper for attention weights.

  In case of 1d inputs (i.e., `[batch..., len_q]`, `[batch..., len_kv]`, the
  attention weights will be `[batch..., heads, len_q, len_kv]` and this
  function will produce `[batch..., 1, len_q, len_kv]`.

  Args:
    query_input: a batched, flat input of query_length size
    key_input: a batched, flat input of key_length size
    pairwise_fn: broadcasting elementwise comparison function
    extra_batch_dims: number of extra batch dims to add singleton axes for, none
      by default
    dtype: mask return dtype

  Returns:
    A `[batch..., 1, len_q, len_kv]` shaped mask for 1d attention.
  """
  mask = pairwise_fn(
    jnp.expand_dims(query_input, axis=-1), jnp.expand_dims(key_input, axis=-2)
  )
  mask = jnp.expand_dims(mask, axis=-3)
  mask = jnp.expand_dims(mask, axis=tuple(range(extra_batch_dims)))
  return mask.astype(dtype)


def make_causal_mask(
  x: Array, extra_batch_dims: int = 0, dtype: Dtype = jnp.float32
) -> Array:
  """Make a causal mask for self-attention.

  In case of 1d inputs (i.e., `[batch..., len]`, the self-attention weights
  will be `[batch..., heads, len, len]` and this function will produce a
  causal mask of shape `[batch..., 1, len, len]`.

  Args:
    x: input array of shape `[batch..., len]`
    extra_batch_dims: number of batch dims to add singleton axes for, none by
      default
    dtype: mask return dtype

  Returns:
    A `[batch..., 1, len, len]` shaped causal mask for 1d attention.
  """
  idxs = jnp.broadcast_to(jnp.arange(x.shape[-1], dtype=jnp.int32), x.shape)
  return make_attention_mask(
    idxs,
    idxs,
    jnp.greater_equal,
    extra_batch_dims=extra_batch_dims,
    dtype=dtype,
  )


def combine_masks(*masks: Optional[Array], dtype: Dtype = jnp.float32) -> Array:
  """Combine attention masks.

  Args:
    *masks: set of attention mask arguments to combine, some can be None.
    dtype: dtype for the returned mask.

  Returns:
    Combined mask, reduced by logical and, returns None if no masks given.
  """
  masks_list = [m for m in masks if m is not None]
  if not masks_list:
    return None
  assert all(
    map(lambda x: x.ndim == masks_list[0].ndim, masks_list)
  ), f'masks must have same rank: {tuple(map(lambda x: x.ndim, masks_list))}'
  mask, *other_masks = masks_list
  for other_mask in other_masks:
    mask = jnp.logical_and(mask, other_mask)
  return mask.astype(dtype)
