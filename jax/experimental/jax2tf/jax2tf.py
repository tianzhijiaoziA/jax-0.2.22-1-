# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Experimental module transforms JAX functions to be executed by TensorFlow."""
from functools import partial
import contextlib
import os
import re
import string
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import jax
from jax._src import ad_util
from jax._src import api_util
from jax import config
from jax._src import api
from jax import core, custom_derivatives
from jax._src import dtypes
from jax import linear_util as lu
from jax import random, tree_util
from jax import numpy as jnp
from jax._src import source_info_util
from jax._src import util
from jax._src.lax import control_flow as lax_control_flow
from jax._src.lax import fft as lax_fft
from jax._src.lax import lax
from jax._src.lax import linalg as lax_linalg
import jax._src.prng
import jax._src.random
from jax.experimental import maps
from jax.experimental import pjit
from jax.interpreters import ad
from jax.interpreters import pxla
from jax.interpreters import sharded_jit
from jax.interpreters import xla
from jax._src.lib import xla_client

from . import shape_poly

import numpy as np
import tensorflow as tf  # type: ignore[import]

# These don't have public equivalents.
# pylint: disable=g-direct-tensorflow-import
from tensorflow.compiler.tf2xla.python import xla as tfxla  # type: ignore[import]
from tensorflow.compiler.xla import xla_data_pb2  # type: ignore[import]
from tensorflow.core.framework import attr_value_pb2  # type: ignore[import]
from tensorflow.compiler.xla.experimental.xla_sharding import xla_sharding  # type: ignore[import]
from tensorflow.python.framework import ops as tf_ops  # type: ignore[import]
# pylint: enable=g-direct-tensorflow-import

PolyShape = shape_poly.PolyShape

# The scope name need to be a valid TensorFlow name. See
# https://github.com/tensorflow/tensorflow/blob/r2.3/tensorflow/core/framework/node_def_util.cc#L731
_VALID_SCOPE_REGEX = re.compile("^[A-Za-z0-9.][A-Za-z0-9_.\\/>-]*$")
_INVALID_SCOPE_CHAR = re.compile("[^A-Za-z0-9_.\\/>-]")

map = util.safe_map
zip = util.safe_zip


def _sanitize_scope_name(name):
  scope_name = _INVALID_SCOPE_CHAR.sub("_", name)
  if not _VALID_SCOPE_REGEX.match(scope_name):
    scope_name = ".{}".format(scope_name)
  return scope_name


# A value suitable in a TF tracing context: tf.Tensor, tf.Variable,
# or Python scalar or numpy.ndarray. (A tf.EagerTensor is a tf.Tensor.)
TfVal = Any
DType = Any
PrecisionType = int  # Enum xla_data.PrecisionConfig.Precision

def _is_tfval(v: TfVal) -> bool:
  if isinstance(v, (tf.Tensor, tf.Variable)):
    return True
  try:
    # Include all convertible types, even if not supported on accelerators.
    with tf.device("CPU"):
      tf.constant(v)
    return True
  except:
    return False


# The implementation rules for primitives. The rule will be called with the
# arguments (TfVal) and must return TfVal (or a sequence thereof,
# if primitive.multiple_results). The vast majority of primitives do not need
# to worry about core.unit inputs or results. The exception are primarily the
# control-flow primitives.
tf_impl: Dict[core.Primitive, Callable[..., Any]] = {}

# Some primitive implementation rules need the abstract values of arguments
# and the results. This is the case for the primitives implemented using
# _convert_jax_impl and those that need to adjust the shape of the outputs
# due to missing TF shape inference rules for TFXLA ops. The rules for these
# primitives should be added to `tf_impl_with_avals`.
# The abstract value are passed to the implementation as two special kwargs
# `_in_avals` (a tuple of core.ShapedArray) and `_out_aval` (a
# core.ShapedArray, or a tuple thereof when primitive.multiple_results).
tf_impl_with_avals: Dict[core.Primitive, Callable[..., Any]] = {}

# In order to ensure that JAX picks up the proper user-frame for source
# locations we will register the TensorFlow source path as an internal
# path with source_info_util. The typical stack when a JAX primitive
# conversion happens is:
#    jax2tf.process_primitive  (top of stack)
#    jax tracing machinery ...
#    tf.custom_gradient machinery ...
#    jax2tf.converted_fun
#    tf function machinery ...
#    user code invokes the converted function on TF tensors
#
# We need to skip over not only JAX internal frames, but TF internal frames
# also.
# We register the TensorFlow source path lazily
_has_registered_tf_source_path = False

class _ThreadLocalState(threading.local):
  def __init__(self):
    self.name_stack = ""
    # XLA is not linked in all environments; when converting a primitive, if this
    # variable is disabled, we try harder to use only standard TF ops if they are
    # applicable to the concrete use case; if the resulting conversion path ends up
    # requiring a TFXLA operation, an exception is thrown instead.
    self.enable_xla = True

    # Keep track if we are inside a call_tf. In that context we disable the
    # safety check that we are not inside JAX transformations.
    self.inside_call_tf = False

    # Maps dimension variables to TF expressions
    self.shape_env: shape_poly.ShapeEnv = {}

    # Whether to actually include XLA op metadata in the generated TF ops
    self.include_xla_op_metadata = True

    # A cache for the tf.convert_to_tensor for constants. We try to preserve
    # sharing for constants, to enable tf.Graph to take advantage of it.
    # See https://github.com/google/jax/issues/7992.
    self.constant_cache = None  # None means that we don't use a cache. We
                                # may be outside a conversion scope.


_thread_local_state = _ThreadLocalState()

def _get_current_name_stack():
  return _thread_local_state.name_stack
def _xla_disabled_error(primitive_name: str,
                        extra_msg: Optional[str] = None) -> Exception:
  assert not _thread_local_state.enable_xla
  msg = f"Call to {primitive_name} cannot be converted with enable_xla=False."
  if extra_msg:
    msg += f" {extra_msg}"
  return NotImplementedError(msg)

@contextlib.contextmanager
def inside_call_tf():
  # Set the inside_call_tf flag for a context.
  prev = _thread_local_state.inside_call_tf
  _thread_local_state.inside_call_tf = True
  try:
    yield
  finally:
    _thread_local_state.inside_call_tf = prev

@partial(api_util.api_hook, tag="jax2tf_convert")
def convert(fun: Callable,
            *,
            polymorphic_shapes=None,
            with_gradient=True,
            enable_xla=True
            ) -> Callable:
  """Transforms `fun` to be executed by TensorFlow.

  See
  [README](https://github.com/google/jax/blob/main/jax/experimental/jax2tf/README.md)
  for more details about usage and common problems.

  Args:
    fun: Function to be transformed. Its arguments and return value should be
      JAX arrays, or nested standard Python containers (tuple/list/dict) thereof
      (pytrees).
    polymorphic_shapes: Specifies input shapes to be treated polymorphically
      during conversion.

      .. warning:: The shape-polymorphic conversion is an experimental feature.
        It is meant to be sound, but it is known to reject some JAX programs
        that are shape polymorphic. The details of this feature can change.

      It should be `None` (all arguments are monomorphic), a single PolyShape
      or string (applies to all arguments), or a tuple/list of the same length
      as the function arguments. For each argument the shape specification
      should be `None` (monomorphic argument), or a Python object with the
      same pytree structure as the argument.
      See [how optional parameters are matched to
      arguments](https://jax.readthedocs.io/en/latest/pytrees.html#applying-optional-parameters-to-pytrees).

      A shape specification for an array argument should be an object
      `PolyShape(dim0, dim1, ..., dimn)`
      where each `dim` is a dimension specification: a positive integer denoting
      a monomorphic dimension of the given size, or a string denoting a
      dimension variable assumed to range over non-zero dimension sizes, or
      the special placeholder string "_" denoting a monomorphic dimension
      whose size is given by the actual argument. As a shortcut, an Ellipsis
      suffix in the list of dimension specifications stands for a list of "_"
      placeholders.

      For convenience, a shape specification can also be given as a string
      representation, e.g.: "batch, ...", "batch, height, width, _", possibly
      with surrounding parentheses: "(batch, ...)".

      The conversion fails if it cannot ensure that the it would produce the same
      sequence of TF ops for any non-zero values of the dimension variables.

      polymorphic_shapes are only supported for positional arguments; shape
      polymorphism is not supported for keyword arguments.

      See [the README](https://github.com/google/jax/blob/main/jax/experimental/jax2tf/README.md#shape-polymorphic-conversion)
      for more details.

    in_shapes: DEPRECATED in favor of `polymorphic_shapes`.
    with_gradient: if set (default), add a tf.custom_gradient to the converted
      function, by converting the ``jax.vjp(fun)``. This means that reverse-mode
      TensorFlow AD is supported for the output TensorFlow function, and the
      value of the gradient will be JAX-accurate.
    enable_xla: if set (default), the converter will use the simplest conversion
      and use XLA TF ops when necessary. These ops are known to create issues
      for the TFLite and TFjs converters. For those cases, unset this parameter
      so the converter tries harder to use non-XLA TF ops to convert the
      function and aborts if this is not possible.

  Returns:
    A version of `fun` that expects TfVals as arguments (or
    tuple/lists/dicts) thereof, and returns TfVals as outputs, and uses
    only TensorFlow ops.
  """
  api._check_callable(fun)
  fun_name = getattr(fun, "__name__", "unknown")
  name_stack = util.extend_name_stack(util.wrap_name(fun_name, "jax2tf"))
  def converted_fun(*args: TfVal, **kwargs: TfVal) -> TfVal:
    # TODO: is there a better way to check if we are inside a transformation?
    if not core.trace_state_clean() and not _thread_local_state.inside_call_tf:
      # It is Ok to nest convert when we are inside a call_tf
      raise ValueError("convert must be used outside all JAX transformations." +
                       f"Trace state: {core.thread_local_state.trace_state.trace_stack}")

    # We support kwargs by wrapping the function to take only positional arguments.
    # This is in part because jax.vjp does not support kwargs.
    nr_positional_args = len(args)
    kw_names = kwargs.keys()
    args = tuple(args) + tuple(kwargs[kw] for kw in kw_names)

    def fun_no_kwargs(*args_and_kwargs):
      assert len(args_and_kwargs) == nr_positional_args + len(kw_names)
      args = args_and_kwargs[:nr_positional_args]
      kwargs = {kw: args_and_kwargs[nr_positional_args + i]
                for i, kw in enumerate(kw_names)}
      return fun(*args, **kwargs)

    def check_arg(a):
      if not _is_tfval(a):
        msg = (f"Argument {a} of type {type(a)} of jax2tf.convert(f) should "
               "be NumPy array, scalar, tf.Variable, or tf.Tensor")
        raise TypeError(msg)

    tree_util.tree_map(check_arg, args)

    args_flat, in_tree = tree_util.tree_flatten((args, {}))
    # May need to cast the arguments to have the type assumed by JAX
    args_and_dtypes_flat = tuple(map(_tfval_to_tensor_jax_dtype, args_flat))
    args_flat, arg_dtypes_flat = util.unzip2(args_and_dtypes_flat)
    # Name input tensors; do this after we have cast the arguments
    def _apply_name(a: TfVal, suffix) -> TfVal:
      return tf.identity(a, f"jax2tf_arg_{suffix}")
    args_flat = tuple(_apply_name(a, i) for i, a in enumerate(args_flat))

    if polymorphic_shapes is None:
      polymorphic_shapes_ = (polymorphic_shapes,) * len(args)
    elif isinstance(polymorphic_shapes, (PolyShape, str)):
      polymorphic_shapes_ = (polymorphic_shapes,) * len(args)  # type: ignore
    else:
      if not isinstance(polymorphic_shapes, Sequence) or len(polymorphic_shapes) != len(args) - len(kw_names):
        msg = ("polymorphic_shapes must be a sequence with the same length as the positional argument list "
               f"({len(args)}). Got polymorphic_shapes={repr(polymorphic_shapes)}.")
        raise TypeError(msg)
      polymorphic_shapes_ = tuple(polymorphic_shapes) + (None,) * len(kw_names)

    # Expand the polymorphic_shapes to match the argument pytree
    polymorphic_shapes_flat = tuple(api_util.flatten_axes("jax2tf.convert polymorphic_shapes",
                                                          in_tree.children()[0],
                                                          polymorphic_shapes_))

    # Construct the abstract values for the flat arguments, possibly based on
    # the input shapes and the polymorphic_shapes if given. May create new shape
    # variables. May cast the args_flat to JAX types, using JAX's interpretation
    # of types of constants.
    args_avals_flat, shapeenv = _args_to_avals_and_env(
        args_flat, arg_dtypes_flat, polymorphic_shapes_flat)

    # This function may take pytrees of TfVals. We can only set
    # tf.custom_gradient on functions that take a flat argument list.
    f = lu.wrap_init(fun_no_kwargs)
    # out_tree_thunk() will be the output tree, after running _interpret_fun.
    flat_fun, out_tree_thunk = api_util.flatten_fun(f, in_tree)
    # out_tree_thunk will be ready after _interpret_fun below.

    # Prepare the grad_fn for tf.custom_gradient.
    def converted_grad_fn(*out_cts_flat: TfVal,
                          _out_cts_avals: Sequence[core.ShapedArray],
                          variables=None):
      if variables:
        raise ValueError(
            "Unexpected variables used in forward pass. "
            "This should not happen for first-order differentiation. "
            f"variables={variables}")

      out_tree = out_tree_thunk()
      if polymorphic_shapes is None:
        vjp_polymorphic_shapes = None
      else:
        args_flat_polymorphic_shapes = polymorphic_shapes_flat
        out_cts_flat_polymorphic_shapes = tuple(str(out_aval.shape)  # Note: may be polynomials, not just DimVar
                                           for out_aval in _out_cts_avals)  # type: ignore
        vjp_polymorphic_shapes = [
            args_flat_polymorphic_shapes, out_cts_flat_polymorphic_shapes
        ]

      def fun_vjp_jax(args_flat_jax, out_cts_flat_jax):
        # One may think that we can get the pullback while we are converting
        # the main function in the first place. That is problematic, because the
        # pullback may contain captured tracers from the conversion of the
        # main function. Those tracers will confuse the conversion of the
        # pullback. So, we construct the vjp anew and we convert it separately.
        args_jax, kwargs_jax = tree_util.tree_unflatten(in_tree, args_flat_jax)
        assert not kwargs_jax
        _, pullback_jax = jax.vjp(fun_no_kwargs, *args_jax)

        def fix_out_ct(out_ct_jax, out_ct_aval: core.ShapedArray):
          # If the primal function has outputs of integer or bool types, and if we are
          # under a tf.function context, then TF will pass None in _out_cts_flat
          # in place of these values. We should change these to float0 or
          # else JAX gets unhappy. See issue #6975.
          if out_ct_jax is not None:
            return out_ct_jax
          assert core.primal_dtype_to_tangent_dtype(out_ct_aval.dtype) == dtypes.float0, f"out_ct={out_ct_jax}"
          # Note that out_ct_aval.shape contains dimension variable from the
          # primal function scope. It is Ok to use them here because we
          # use the same shape variables for the VJP function.
          return jnp.zeros(out_ct_aval.shape, dtype=_tf_np_dtype_for_float0)

        out_cts_fixed_flat = tuple(map(fix_out_ct, out_cts_flat_jax, _out_cts_avals))

        out_cts_fixed = tree_util.tree_unflatten(out_tree, out_cts_fixed_flat)
        in_cts_jax = pullback_jax(out_cts_fixed)

        in_cts_flat_jax, in_cts_tree = tree_util.tree_flatten(in_cts_jax)
        def fix_in_ct(in_ct, arg_aval: core.ShapedArray):
          if np.issubdtype(arg_aval.dtype, np.inexact):
            return in_ct
          else:
            assert in_ct.dtype == dtypes.float0
            return jnp.zeros(arg_aval.shape, _tf_np_dtype_for_float0)

        in_cts_fixed_flat_jax = tuple(map(fix_in_ct, in_cts_flat_jax, args_avals_flat))
        return in_cts_fixed_flat_jax

      # TODO: enable higher-order gradients
      with tf.name_scope("jax2tf_vjp"):
        in_cts_flat = convert(
            fun_vjp_jax,
            with_gradient=False,
            polymorphic_shapes=vjp_polymorphic_shapes)(args_flat, out_cts_flat)
        in_cts, kwin_cts = tree_util.tree_unflatten(in_tree, in_cts_flat)
        assert not kwin_cts
      return in_cts

    try:
      assert not _thread_local_state.shape_env, f"Unexpected shape environment {_thread_local_state.shape_env}"

      prev_enable_xla = _thread_local_state.enable_xla
      _thread_local_state.enable_xla = enable_xla

      prev_include_xla_op_metadata = _thread_local_state.include_xla_op_metadata
      _thread_local_state.include_xla_op_metadata = False

      _thread_local_state.shape_env = shapeenv
      global _has_registered_tf_source_path
      if not _has_registered_tf_source_path:
        source_info_util.register_exclusion(os.path.dirname(tf.__file__))
        _has_registered_tf_source_path = True

      if with_gradient:

        @tf.custom_gradient
        def converted_fun_flat_with_custom_gradient(*args_flat: TfVal) -> TfVal:
          out_with_avals = _interpret_fun(flat_fun, args_flat, args_avals_flat,
                                          name_stack)
          outs, out_avals = util.unzip2(out_with_avals)
          return (tuple(outs),
                  partial(converted_grad_fn, _out_cts_avals=tuple(out_avals)))

        out_flat = converted_fun_flat_with_custom_gradient(*args_flat)
      else:
        out_with_avals = _interpret_fun(flat_fun, args_flat, args_avals_flat,
                                        name_stack)
        outs, out_avals = util.unzip2(out_with_avals)
        message = ("The jax2tf-converted function does not support gradients. "
                   "Use `with_gradient` parameter to enable gradients")
        # We use PreventGradient, which is propagated through a SavedModel.
        out_flat = [
            tf.raw_ops.PreventGradient(input=o, message=message)
            for o in outs
        ]
    finally:
      _thread_local_state.shape_env = {}
      _thread_local_state.enable_xla = prev_enable_xla
      _thread_local_state.include_xla_op_metadata = prev_include_xla_op_metadata

    out_flat = [tf.identity(x, "jax2tf_out") for x in out_flat]
    out = tree_util.tree_unflatten(out_tree_thunk(), out_flat)
    return out

  return converted_fun


def dtype_of_val(val: TfVal) -> DType:
  """Computes the TensorFlow dtype using JAX's typing rules.

  If the value is a tf.Tensor, it starts with its dtype. If the value is a
  constant it uses JAX to infer its dtype. The resulting dtype follows the
  JAX type inference rules, and depends on the value of the
  JAX_ENABLE_X64 flag.

  See README.md for how 64-bit values are treated.
  """
  tval, _ = _tfval_to_tensor_jax_dtype(val)
  return tval.dtype

# Internals

@contextlib.contextmanager
def _extended_name_stack(extra_name_stack: Optional[str]):
  prev_name_stack = _thread_local_state.name_stack
  if extra_name_stack:
    if not prev_name_stack:
      _thread_local_state.name_stack = extra_name_stack
    else:
      _thread_local_state.name_stack = util.extend_name_stack(
          _thread_local_state.name_stack, extra_name_stack)
  try:
    yield
  finally:
    _thread_local_state.name_stack = prev_name_stack


def _interpret_fun(
    fun: lu.WrappedFun, in_vals: Sequence[TfVal],
    in_avals: Sequence[core.ShapedArray],
    extra_name_stack: Optional[str]
) -> Sequence[Tuple[TfVal, core.ShapedArray]]:
  try:
    prev_constant_cache = _thread_local_state.constant_cache
    _thread_local_state.constant_cache = {}  # Start a new cache, so that we
                                             # don't share constants across
                                             # tf.function boundaries.

    with core.new_base_main(TensorFlowTrace) as main:  # type: ignore
      fun = _interpret_subtrace(fun, main, in_avals)
      with _extended_name_stack(extra_name_stack):
        with core.new_sublevel():
          out_vals: Sequence[Tuple[TfVal, core.ShapedArray]] = \
              fun.call_wrapped(*in_vals)
        del main
  finally:
    _thread_local_state.constant_cache = prev_constant_cache

  return tuple(out_vals)


def _convert_jax_impl(jax_impl: Callable, *,
                      multiple_results=True,
                      extra_name_stack: Optional[str] = None) -> Callable:
  """Convert the JAX implementation of a primitive.

  Args:
    jax_impl: typically the impl-rule for a primitive, with signature
      `(*args: JaxVal, **kwargs) -> Sequence[JaxVal]`. This function implements
        a primitive in terms of other primitives.
    multiple_results: whether `jax_impl` returns a sequence of results.
    extra_name_stack: additional element to add to the name stack for the
      converted ops.

  Returns:
     a function with signature `(*args: TfVal, _in_avals, _out_aval, **kwargs)
     -> Sequence[TfVal]`.
  """

  def wrapped(*tf_args: TfVal, _in_avals: Sequence[core.ShapedArray],
              _out_aval: core.ShapedArray,
              **kwargs) -> Sequence[TfVal]:

    # We wrap the jax_impl under _interpret_fun to abstract the TF values
    # from jax_impl and turn them into JAX abstract values.
    def jax_impl_jax_args(*jax_args):
      jax_results = jax_impl(*jax_args, **kwargs)
      return jax_results if multiple_results else [jax_results]

    tf_results_with_avals = _interpret_fun(
        lu.wrap_init(jax_impl_jax_args), tf_args, _in_avals,
        extra_name_stack)
    tf_results, _ = util.unzip2(tf_results_with_avals)
    return tf_results if multiple_results else tf_results[0]

  return wrapped


@lu.transformation
def _interpret_subtrace(main: core.MainTrace,
                        in_avals: Sequence[core.ShapedArray],
                        *in_vals: TfVal):
  trace = TensorFlowTrace(main, core.cur_sublevel())
  in_tracers = tuple(
      TensorFlowTracer(trace, val, aval)
      for val, aval in zip(in_vals, in_avals))
  # The outs may be core.unit, see comment in TensorFlowTrace.pure.
  outs = yield in_tracers, {}  # type: Sequence[Union[TfVal, core.Unit]]
  out_tracers: Iterable[TensorFlowTracer] = (
      map(trace.full_raise, outs))  # type: ignore
  out_vals_with_avals: Sequence[Tuple[TfVal, core.ShapedArray]] = (
      tuple((t.val, t.aval) for t in out_tracers))
  yield out_vals_with_avals


def _interpret_jaxpr(jaxpr: core.ClosedJaxpr, *args: TfVal,
                     extra_name_stack: Optional[str]) -> Sequence[TfVal]:
  """Evaluates a Jaxpr with tf.Tensor arguments.

  The output is a sequence of TfVal (no `core.unit`), suitable for use with TF.
  """
  fun: lu.WrappedFun = lu.wrap_init(core.jaxpr_as_fun(jaxpr))
  out_with_avals = _interpret_fun(fun, args, jaxpr.in_avals, extra_name_stack)
  return tuple(v for v, _ in out_with_avals)


def _aval_to_tf_shape(aval: core.ShapedArray) -> Tuple[Optional[int], ...]:
  """Generate a TF shape, possibly containing None for polymorphic dimensions."""
  return tuple(map(lambda d: None if shape_poly.is_poly_dim(d) else d,
                   aval.shape))  # type: ignore[attr-defined]

# In the TF world, we represent float0 as zeros of this type.
_tf_np_dtype_for_float0 = np.int32

def _to_tf_dtype(jax_dtype):
  # Note that converting _to_tf_dtype and _to_jax_dtype are not inverses,
  # due to float0 and 64-bit behavior.
  if jax_dtype == dtypes.float0:
    jax_dtype = _tf_np_dtype_for_float0
  return tf.dtypes.as_dtype(jax_dtype)


def _to_jax_dtype(tf_dtype):
  # Note that converting _to_tf_dtype and _to_jax_dtype are not inverses,
  # due to float0 and 64-bit behavior.
  return dtypes.canonicalize_dtype(tf_dtype.as_numpy_dtype)


def _tfval_to_tensor_jax_dtype(val: TfVal,
                               jax_dtype: Optional[DType] = None,
                               memoize_constants=False) -> Tuple[TfVal, DType]:
  """Converts a scalar, ndarray, or tf.Tensor to a tf.Tensor with proper type.

  If `jax_dtype` is missing, uses JAX typing rules.
  See README.md for details regarding 64-bit values.

  Args:
    val: a scalar, ndarray, tf.Tensor, or tf.Variable
    jax_dtype: an optional dtype to use. If missing, uses JAX type inference
      rules for constants.
    memoize_constants: whether to memoize TF constants. We can't do this
      everywhere, we may be outside of a conversion scope.

  Returns:
    a tuple with a tf.Tensor with the type as needed by JAX, and the JAX type.
  """
  if isinstance(val, (tf.Tensor, tf.Variable)):
    jax_dtype = jax_dtype or _to_jax_dtype(val.dtype)  # Give JAX a chance to pick the type
    conversion_dtype = _to_tf_dtype(jax_dtype)
    if conversion_dtype != val.dtype:
      return tf.cast(val, conversion_dtype), jax_dtype
    else:
      return val, jax_dtype
  else:  # A constant
    jax_dtype = jax_dtype or xla.abstractify(val).dtype
    # TODO(document): We assume that the value of a constant does not
    # change through the scope of the function. But it may be an ndarray, ...
    # JAX has the same problem when generating HLO.
    const_key = (id(val), jax_dtype)
    # Since we use id(val) as a cache key, we have to make sure that we keep
    # the previous `val` alive. Otherwise, for an ndarray, it can get garbage
    # collected and reused for a different value, which would create correctness
    # issues. We keep the `val` alive by storing in the cache the pair
    # `(val, tf_val)`.
    if memoize_constants and _thread_local_state.constant_cache is not None:
      _, tf_val = _thread_local_state.constant_cache.get(const_key, (None, None))
    else:
      tf_val = None
    if tf_val is None:
      conversion_dtype = _to_tf_dtype(jax_dtype)
      # The float0 type is not known to TF.
      if jax_dtype == dtypes.float0:
        val = np.zeros(np.shape(val), conversion_dtype.as_numpy_dtype)
      tf_val = tf.convert_to_tensor(val, dtype=conversion_dtype)
      if memoize_constants and _thread_local_state.constant_cache is not None:
        _thread_local_state.constant_cache[const_key] = (val, tf_val)
    return tf_val, jax_dtype

def _args_to_avals_and_env(
    args: Sequence[TfVal],
    arg_jax_dtypes: Sequence[DType],
    polymorphic_shapes: Sequence[Optional[Union[str, PolyShape]]]) -> \
  Tuple[Sequence[core.ShapedArray], shape_poly.ShapeEnv]:
  """Computes canonicalized args, abstract values and a dimension environment for arguments.

  Args:
    args: the arguments, TF inputs. Must be tf.Tensor or tf.Variable.
    arg_dtypes: the inferred JAX dtypes for the args.
    polymorphic_shapes: the polymorphic specifications for the arguments.
  Returns: a tuple of: a sequence of abstract values corresponding to the
    arguments, and a dimension variable environment.
  """
  dim_equations: List[shape_poly.DimEquation] = []

  def input_aval(arg: TfVal,
                 arg_jax_dtype: DType,
                 polymorphic_shape: Optional[str]) -> core.ShapedArray:
    """The abstract value for an input."""
    arg_shape = np.shape(arg)
    aval_shape = shape_poly.parse_spec(polymorphic_shape, arg_shape)
    arg_tf_shape = tf.shape(arg)
    for i, d in enumerate(aval_shape):
      dim_size = arg_shape[i]
      if isinstance(dim_size, tf.compat.v1.Dimension):
        dim_size = dim_size.value
      if not shape_poly.is_poly_dim(d):
        assert d == dim_size
      else:
        dim_equations.append(shape_poly.DimEquation(
            poly=d, tf_expr=arg_tf_shape[i]))  # type: ignore


    return core.ShapedArray(aval_shape, arg_jax_dtype)

  avals = tuple(map(input_aval, args, arg_jax_dtypes, polymorphic_shapes))  # type: ignore

  shapeenv = shape_poly.solve_dim_equations(dim_equations)
  return avals, shapeenv


def _eval_shape(shape: Sequence[shape_poly.DimSize]) -> Sequence[TfVal]:
  assert all(map(lambda x: x is not None, shape)), (
      f"Argument shape should be a valid JAX shape but got {shape}")
  return shape_poly.eval_shape(shape, _thread_local_state.shape_env)


# TODO(b/26854495): pylint doesn't understand slots and inheritance.
# pylint: disable=assigning-non-slot


class TensorFlowTracer(core.Tracer):
  """Tracer class that boxes a TF value and a JAX abstract value.

  In addition to the TF value we carry the JAX abstract value because there are
  two cases when it cannot be recovered from the value: (a) when the abstract
  value is core.abstract_unit, in which case the value is tf.nan; (b) when we
  are converting with polymorphic shapes, in which case the shape of the value
  may have dimensions set to `None`, which the JAX abstract value may contain
  more precise information.

  When the value has a partially-known shape, the dimensions marked as `None`
  must correspond to non-constant dimensions in the abstract value.

  See README.md for details.
  """
  # val: TfVal
  # _aval: core.ShapedArray
  __slots__ = ["val", "_aval"]

  def __init__(self, trace: "TensorFlowTrace", val: TfVal,
               aval: core.AbstractValue):
    self._trace = trace
    self._aval = aval
    if aval is core.abstract_unit:
      self.val = val
      return

    if isinstance(val, (tf.Tensor, tf.Variable)):
      val_shape = val.shape

      if config.jax_enable_checks:
        assert len(self._aval.shape) == len(val_shape), f"_aval.shape={self._aval.shape} different rank than val_shape={val_shape}"
        # To compare types, we must handle float0 in JAX and x64 in TF
        if self._aval.dtype == dtypes.float0:
          assert _to_tf_dtype(self._aval.dtype) == val.dtype, f"expected {self._aval.dtype} == {val.dtype}"
        else:
          assert self._aval.dtype == _to_jax_dtype(val.dtype), f"expected {self._aval.dtype} == {val.dtype}"

        for aval_dim, val_dim in zip(self._aval.shape, val_shape):  # type: ignore[attr-defined]
          if val_dim is None:
            assert shape_poly.is_poly_dim(aval_dim), f"expected {self._aval.shape} == {val_shape}"  # type: ignore[attr-defined]
          elif not shape_poly.is_poly_dim(aval_dim):
            assert aval_dim == val_dim, f"expected {self._aval.shape} == {val_shape}"  # type: ignore[attr-defined]
          else:
            # We have a TF value with known shape, and the abstract shape is a shape variable.
            try:
              aval_int = int(_eval_shape([aval_dim]))  # type: ignore
            except TypeError:
              continue
            assert aval_int == val_dim, f"expected {self._aval.shape} == {val_shape}. Found {aval_int} != {val_dim}."  # type: ignore

    self.val = _tfval_to_tensor_jax_dtype(val,
                                          self._aval.dtype,
                                          memoize_constants=True)[0]  # type: ignore[attr-defined]

  @property
  def aval(self):
    return self._aval

  def full_lower(self):
    return self


class TensorFlowTrace(core.Trace):
  """Trace class that underlies the jax2tf transformation.

  We are going to ensure that jax2tf.convert is never nested inside other
  transformations. This is sufficient for intended use cases (converting
  fully-transformed JAX code). It also simplifies our job because we do not have
  to handle situations where we apply primitives on a mix of TF values and
  JAX tracers from an outer transformation. E.g., for addition both the TF
  values
  and the JAX tracers have an override and they get confused if they see values
  from the other world.

  Hence a TFT trace does not interact with non-TFT traces at lower-level. For
  higher-order control-flow primitives we invoke recursively
  _interpret_fun on the body of the conditional, which will create a nested TFT.

  We do want to allow transformations nested inside a TensorFlowTrace (TFT), but
  those will introduce their own MainTrace, and any operations involving those
  will be done on those traces, i.e., not a concern for TFT.
  """
  def pure(self, val: Union[TfVal, core.Unit]) -> TensorFlowTracer:
    """Lifts a non-Tracer into the TensorFlowTracer.

    This function may be called by way of trace.full_raise.

    The value may be a core.unit. During JAX transformations we sometimes
    produce a Jaxpr that has arguments of abstract value core.abstract_unit
    and results equal to core.unit. These are arguments and results that are
    not used in the computation.

    In TF world, we represent core.unit as NaN. This is safe, as these values
    should never be used.
    """
    if val is core.unit:
      return TensorFlowTracer(self, tf.constant(np.nan, tf.float32),
                              core.abstract_unit)
    else:
      tf_val, jax_dtype = _tfval_to_tensor_jax_dtype(val, memoize_constants=True)
      return TensorFlowTracer(
        self, val, core.ShapedArray(tf_val.shape, jax_dtype,
                                    weak_type=dtypes.is_weakly_typed(val)))

  def lift(self, val: core.Tracer) -> TensorFlowTracer:
    # This would be called when we need to raise a tracer from a lower-level
    # main into the TensorFlowTrace. Since the TensorFlowTrace is never nested
    # inside another transform, there are no lower-level main traces.
    assert False

  def sublift(self, val: TensorFlowTracer) -> TensorFlowTracer:
    # This is called when we need to raise a tracer from the same main,
    # but a lower sublevel. This could come from a nested jit.
    return TensorFlowTracer(self, val.val, val._aval)

  def process_primitive(self, primitive: core.Primitive,
                        tracers: Sequence[TensorFlowTracer],
                        params) -> TensorFlowTracer:
    impl, impl_needs_avals = self.get_primitive_impl(primitive)
    args_avals: Sequence[core.ShapedArray] = tuple(t.aval for t in tracers)
    # This is a bit conservative, doing abstract_eval even in op-by-op execution
    # but we needed it for, e.g., shape_polymorphism where only JAX's
    # abstract evaluation rules can properly track polymorphic shapes.
    # Unfortunately under op-by-op execution this is a rare occasion where we
    # need abstract evaluation.
    out_aval = primitive.abstract_eval(*args_avals, **params)
    args_tf: Sequence[TfVal] = [t.val for t in tracers]
    def invoke_impl() -> TfVal:
      if impl_needs_avals:
        return impl(
            *args_tf,
            _in_avals=args_avals,  # type: ignore
            _out_aval=out_aval,
            **params)
      else:
        return impl(*args_tf, **params)

    if _thread_local_state.include_xla_op_metadata:
      op_metadata = xla.make_op_metadata(primitive, params,
                                         name_stack=_get_current_name_stack(),
                                         source_info=source_info_util.current())
      op_metadata_proto = xla_data_pb2.OpMetadata(
          op_type=op_metadata.op_type,
          op_name=op_metadata.op_name,
          source_file=op_metadata.source_file,
          source_line=op_metadata.source_line
      )
      with tf_ops.get_default_graph()._attr_scope(
          {"_XlaOpMetadata": attr_value_pb2.AttrValue(
              s=op_metadata_proto.SerializeToString())}):
        val_out = invoke_impl()
    else:
      val_out = invoke_impl()

    if primitive.multiple_results:
      out = [
          TensorFlowTracer(self, v, a)
          for v, a in zip(val_out, out_aval)
      ]  # type: ignore
    else:
      out = TensorFlowTracer(self, val_out, out_aval)  # type: ignore

    # Check that the impl rule returned a value of expected shape and dtype
    # TODO: adapt this to match polymorphic shapes
    if config.jax_enable_checks:
      if primitive.multiple_results:
        for o, expected_aval in zip(out, out_aval):  # type: ignore
          assert o.aval.strip_weak_type() == expected_aval.strip_weak_type(), (
              f"{primitive}: out.aval = {o.aval}; expected {expected_aval}")
      else:
        assert out.aval == out_aval, (  # type: ignore
            f"{primitive}: out.aval = {out.aval}; expected {out_aval}"
        )  # type: ignore
    return out  # type: ignore

  def process_call(self, call_primitive: core.Primitive, fun: lu.WrappedFun,
                   tracers: Sequence[TensorFlowTracer], params):
    assert call_primitive.multiple_results
    vals: Sequence[TfVal] = [t.val for t in tracers]
    avals: Sequence[core.ShapedArray] = tuple(t.aval for t in tracers)
    fun = _interpret_subtrace(fun, self.main, avals)
    extra_name_stack = None
    if call_primitive == core.named_call_p:
      extra_name_stack = util.wrap_name(params["name"], "named")
    elif call_primitive == xla.xla_call_p:
      extra_name_stack = util.wrap_name(params["name"], "jit")
    with _extended_name_stack(extra_name_stack):
      with core.new_sublevel():
        if call_primitive == core.named_call_p:
          with tf.name_scope(_sanitize_scope_name(params["name"])):
            vals_out: Sequence[Tuple[TfVal, core.ShapedArray]] = \
                fun.call_wrapped(*vals)
        elif call_primitive == sharded_jit.sharded_call_p:
          vals_out = _sharded_call(fun, vals, **params)
        else:
          vals_out = fun.call_wrapped(*vals)
    return [TensorFlowTracer(self, v, a) for v, a in vals_out]

  def post_process_call(self, call_primitive: core.Primitive,
                        out_tracers: Sequence[TensorFlowTracer], params):
    # We encountered a call primitive, e.g., remat_call_p, whose result
    # (out_tracers) include TensorFlowTracer that were not passed through
    # its arguments (captured from the environment).
    vals = tuple(t.val for t in out_tracers)
    main = self.main

    def todo(vals: Sequence[TfVal]):
      # TODO: is name_stack correct?
      trace = TensorFlowTrace(main, core.cur_sublevel())
      return [
          TensorFlowTracer(trace, v, out_tracer.aval)
          for v, out_tracer in zip(vals, out_tracers)
      ]

    return vals, todo

  def process_map(self, map_primitive, f, tracers, params):
    raise NotImplementedError("process_map")

  def post_process_map(self, map_primitive, out_tracers, params):
    raise NotImplementedError("post_process_map")

  def process_custom_jvp_call(self, prim, fun, jvp, tracers):
    # Drop the custom differentiation rule and act like a call primitive. This
    # behavior is desirable because jax2tf stages code out of the JAX system, so
    # there are no more JAX differentiation transformations to be applied.
    del jvp  # Unused.
    return self.process_call(core.call_p, fun, tracers, {})

  def post_process_custom_jvp_call(self, out_tracers, params):
    assert False  # unreachable assuming jax2tf runs with clean trace state

  def process_custom_vjp_call(self, prim, fun, fwd, bwd, tracers, out_trees):
    # Drop the custom differentiation rule and act like a call primitive. This
    # behavior is desirable because jax2tf stages code out of the JAX system, so
    # there are no more JAX differentiation transformations to be applied.
    del fwd, bwd, out_trees  # Unused.
    return self.process_call(core.call_p, fun, tracers, {})

  def post_process_custom_vjp_call(self, out_tracers, params):
    assert False  # unreachable assuming jax2tf runs with clean trace state

  def get_primitive_impl(self, p: core.Primitive) -> Tuple[Callable, bool]:
    # Returns the primitive implementation and whether the implementation
    # takes abstract values (see definition of tf_impl_with_avals)
    try:
      return tf_impl[p], False
    except KeyError:
      try:
        return tf_impl_with_avals[p], True
      except KeyError as err:
        msg = "TensorFlow interpretation rule for '{}' not implemented"
        raise NotImplementedError(msg.format(p)) from err

def _unexpected_primitive(p: core.Primitive, *args, **kwargs):
  assert False, f"Encountered unexpected primitive {p}"


for unexpected in xla.call_translations:  # Call primitives are inlined
  if unexpected is pjit.pjit_p:
    continue
  tf_impl[unexpected] = partial(_unexpected_primitive, unexpected)

# Primitives that are not yet implemented must be explicitly declared here.
tf_not_yet_impl = [
    "rng_uniform",
    "clz",
    "igamma_grad_a",
    "random_gamma_grad",
    "reduce_precision",
    "schur",

    # Not high priority?
    "after_all",
    "all_to_all",
    "create_token",
    "infeed",
    "linear_call",
    "outfeed",
    "pmax_p",
    "pmin",
    "ppermute",
    "psum",
    "pmax",
    "pgather",
    "reduce_scatter",
    "axis_index",
    "pdot",
    "all_gather",
    "lu_pivots_to_permutation",
    "xla_pmap",
]

tf_impl[ad_util.stop_gradient_p] = tf.stop_gradient
tf_impl[ad_util.zeros_like_p] = tf.zeros_like


def _add(x: TfVal, y: TfVal) -> TfVal:
  return tf.raw_ops.AddV2(x=x, y=y)


tf_impl[ad_util.add_jaxvals_p] = _add
tf_impl[xla.device_put_p] = lambda x, device=None: x

def _neg(x: TfVal) -> TfVal:
  if x.dtype.is_unsigned:
    signed_dtype = _UNSIGNED_TO_SIGNED_TABLE[x.dtype]
    x_signed = tf.cast(x, signed_dtype)
    res_signed = tf.math.negative(x_signed)
    return tf.cast(res_signed, x.dtype)
  else:
    return tf.math.negative(x)

tf_impl[lax.neg_p] = _neg


def _sign(x: TfVal) -> TfVal:
  if x.dtype.is_unsigned:
    # TF and XLA do not support tf.math.sign for unsigned types.
    return tf.where(
        tf.math.equal(x, 0), tf.constant(0, dtype=x.dtype),
        tf.constant(1, dtype=x.dtype))
  else:
    return tf.math.sign(x)


tf_impl[lax.sign_p] = _sign
tf_impl[lax.floor_p] = tf.math.floor
tf_impl[lax.ceil_p] = tf.math.ceil


def _round(operand, *, rounding_method,
           _in_avals: Sequence[core.ShapedArray],
           _out_aval: core.ShapedArray):
  if rounding_method is lax.RoundingMethod.AWAY_FROM_ZERO:
    # JAX uses a single HLO op Round here
    sign = _sign(operand)
    operand *= sign
    floor = tf.math.floor(operand)
    operand -= floor
    cond = tf.math.equal(operand, tf.constant(np.array(0.5), operand.dtype))
    return sign * (
        tf.where(cond, tf.constant(np.array(1), operand.dtype),
                 tf.math.round(operand)) + floor)
  else:  # rounding_method is RoundingMethod.TO_NEAREST_EVEN
    rounding_fun = _convert_jax_impl(
        lax._round_to_nearest_even, multiple_results=False)
    return rounding_fun(operand, _in_avals=_in_avals, _out_aval=_out_aval)

tf_impl_with_avals[lax.round_p] = _round
tf_impl[lax.nextafter_p] = tf.math.nextafter


def _population_count(x):
  orig_dtype = x.dtype
  return tf.cast(tf.raw_ops.PopulationCount(x=x), orig_dtype)


tf_impl[lax.population_count_p] = _population_count
tf_impl[lax.is_finite_p] = tf.math.is_finite


def _abs(x: TfVal) -> TfVal:
  # TF and XLA do not support tf.math.abs for unsigned types.
  return tf.math.abs(x) if not x.dtype.is_unsigned else x


tf_impl[lax.abs_p] = _abs
tf_impl[lax.pow_p] = tf.math.pow


def _integer_pow(x, *, y: int, _in_avals: Sequence[core.ShapedArray],
                 _out_aval: core.ShapedArray):
  # Follows the implementation in lax._integer_pow_translation_rule
  if y == 0:
    return tf.broadcast_to(
        tf.constant(1, dtype=x.dtype, shape=()), _eval_shape(_out_aval.shape))
  is_reciprocal = y < 0
  if is_reciprocal:
    y = -y
  acc = None
  while y > 0:
    if y & 1:
      acc = x if acc is None else tf.math.multiply(acc, x)
    y >>= 1
    if y > 0:
      x = tf.math.multiply(x, x)
  return tf.math.reciprocal(acc) if is_reciprocal else acc


tf_impl_with_avals[lax.integer_pow_p] = _integer_pow
tf_impl[lax.exp_p] = tf.math.exp
tf_impl[lax.expm1_p] = tf.math.expm1
tf_impl[lax.log_p] = tf.math.log
tf_impl[lax.log1p_p] = tf.math.log1p
tf_impl[lax.tan_p] = tf.math.tan
tf_impl[lax.tanh_p] = tf.math.tanh
tf_impl[lax.sin_p] = tf.math.sin
tf_impl[lax.sinh_p] = tf.math.sinh
tf_impl[lax.cos_p] = tf.math.cos
tf_impl[lax.cosh_p] = tf.math.cosh
tf_impl_with_avals[lax.acos_p] = _convert_jax_impl(lax.acos_translation_rule,
                                                   multiple_results=False)
tf_impl_with_avals[lax.asin_p] = _convert_jax_impl(lax.asin_translation_rule,
                                                   multiple_results=False)
tf_impl_with_avals[lax.atan_p] = _convert_jax_impl(lax.atan_translation_rule,
                                                   multiple_results=False)

def _atan2(y, x, **kwargs):
  if x.dtype.is_complex or y.dtype.is_complex:
    complex_component_dtype = {
      tf.complex64: tf.float32,
      tf.complex128: tf.float64
    }.get(y.dtype)
    zero = tf.constant(0, complex_component_dtype)
    one = tf.constant(1, complex_component_dtype)
    i = tf.complex(zero, one)
    return -i * tf.math.log((x + i * y)/tf.math.sqrt(x * x + y * y))
  else:
    return tf.math.atan2(y, x)


tf_impl[lax.atan2_p] = _atan2
tf_impl[lax.acosh_p] = tf.math.acosh
tf_impl[lax.atanh_p] = tf.math.atanh
tf_impl[lax.asinh_p] = tf.math.asinh

tf_impl[lax.sqrt_p] = tf.math.sqrt
tf_impl[lax.rsqrt_p] = tf.math.rsqrt

def _cbrt(x):
  return tf.math.sign(x) * tf.math.pow(tf.math.abs(x), 1/3)

tf_impl[lax.cbrt_p] = _cbrt

tf_impl[lax.lgamma_p] = tf.math.lgamma
tf_impl[lax.digamma_p] = tf.math.digamma
tf_impl[lax.igamma_p] = tf.math.igamma
tf_impl[lax.igammac_p] = tf.math.igammac
tf_impl[lax.regularized_incomplete_beta_p] = tf.math.betainc
tf_impl[lax.erf_p] = tf.math.erf
tf_impl[lax.erfc_p] = tf.math.erfc
tf_impl[lax.erf_inv_p] = tf.math.erfinv
tf_impl[lax.bessel_i0e_p] = tf.math.bessel_i0e
tf_impl[lax.bessel_i1e_p] = tf.math.bessel_i1e

tf_impl[lax.complex_p] = tf.complex


def _conj(x, **kwargs):
  # The only dtypes that are allowed are: float32, float64, complex64, and
  # complex128.
  if x.dtype == tf.float32:
    return tf.cast(x, tf.complex64)
  elif x.dtype == tf.float64:
    return tf.cast(x, tf.complex128)
  else:
    return tf.math.conj(x)


tf_impl[lax.conj_p] = _conj
tf_impl[lax.real_p] = tf.math.real
tf_impl[lax.imag_p] = tf.math.imag

tf_impl[lax.add_p] = _add
tf_impl[lax.sub_p] = tf.math.subtract
tf_impl[lax.mul_p] = tf.math.multiply


def _iota(*, dtype, shape, dimension):
  dtype = _to_tf_dtype(dtype)
  # Some dtypes are unsupported, like uint32, so we just fall back to int32.
  # TODO(mattjj, necula): improve tf.range dtype handling
  shape_tf = _eval_shape(shape)
  vec = tf.range(tf.cast(shape_tf[dimension], tf.int32), dtype=tf.int32)
  vec_shape = [-1 if i == dimension else 1 for i in range(len(shape))]
  return tf.cast(tf.broadcast_to(tf.reshape(vec, vec_shape), shape_tf), dtype)


tf_impl[lax.iota_p] = _iota


def _div(lhs, rhs):
  if lhs.dtype.is_integer:
    quotient = tf.math.floordiv(lhs, rhs)
    select = tf.math.logical_and(
        tf.not_equal(_sign(lhs), _sign(rhs)),
        tf.not_equal(tf.math.floormod(lhs, rhs), 0))
    return tf.where(select, quotient + 1, quotient)
  else:
    return tf.math.truediv(lhs, rhs)


def _rem(lhs, rhs):
  return _sign(lhs) * tf.math.floormod(_abs(lhs), _abs(rhs))


tf_impl[lax.div_p] = _div
tf_impl[lax.rem_p] = _rem


def _minmax(x: TfVal, y: TfVal, *, is_min: bool,
            _in_avals: Sequence[core.ShapedArray],
            _out_aval: core.ShapedArray,) -> TfVal:
  # For complex numbers use lexicographic ordering, like JAX
  if dtypes.issubdtype(x.dtype.as_numpy_dtype, np.complexfloating):
    return _convert_jax_impl(
        partial(lax._minmax_complex_lowering,
                          lax_cmp_pick_x=lax.lt if is_min else lax.gt),
        multiple_results=False)(x, y, _in_avals=_in_avals, _out_aval=_out_aval)
  elif x.dtype.as_numpy_dtype == np.bool_:
    return (tf.math.logical_and if is_min else tf.math.logical_or)(x, y)
  else:
    return (tf.math.minimum if is_min else tf.math.maximum)(x, y)

def _minmax_scalar(x: TfVal, y: TfVal, *, is_min: bool) -> TfVal:
  # For reducers we will need min/max for scalars only. In that case we
  # can construct the AbstractValues outselves, even in the presence of
  # shape polymorphism.
  assert len(x.shape) == 0 and len(y.shape) == 0, f"x: {x.shape}, y: {y.shape}"
  aval = core.ShapedArray((), _to_jax_dtype(x.dtype))
  return _minmax(x, y, is_min=is_min,
                 _in_avals=[aval, aval], _out_aval=aval)

tf_impl_with_avals[lax.max_p] = partial(_minmax, is_min=False)
tf_impl_with_avals[lax.min_p] = partial(_minmax, is_min=True)

# Map from TF signed types to TF unsigned types.
_SIGNED_TO_UNSIGNED_TABLE = {
    tf.int8: tf.uint8,
    tf.int16: tf.uint16,
    tf.int32: tf.uint32,
    tf.int64: tf.uint64,
}

# Map from TF unsigned types to TF signed types.
_UNSIGNED_TO_SIGNED_TABLE = {u: s for s, u in _SIGNED_TO_UNSIGNED_TABLE.items()}


# Note: Bitwise operations only yield identical results on unsigned integers!
# pylint: disable=protected-access
def _shift_right_arithmetic_raw(x, y):
  if x.dtype.is_unsigned:
    assert x.dtype == y.dtype
    orig_dtype = x.dtype
    signed_dtype = _UNSIGNED_TO_SIGNED_TABLE[orig_dtype]
    x = tf.cast(x, signed_dtype)
    y = tf.cast(y, signed_dtype)
    res = tf.bitwise.right_shift(x, y)
    return tf.cast(res, orig_dtype)
  else:
    return tf.bitwise.right_shift(x, y)


def _shift_right_arithmetic(x, y):
  # TF shift is "implementation defined" if the shift amount is negative
  # or larger or equal to the size of the value. We implement the XLA
  # semantics to return the shift by the max value (x_bits - 1).
  # TODO: it is likely better to add XlaOps for shifts
  x_bits = 8 * x.dtype.size
  clamp_y = tf.where(_shift_in_bounds(x, y), y, x_bits - 1)
  return _shift_right_arithmetic_raw(x, clamp_y)


tf_impl[lax.shift_right_arithmetic_p] = _shift_right_arithmetic


def _shift_right_logical_raw(x, y):
  if x.dtype.is_unsigned:
    return tf.bitwise.right_shift(x, y)
  else:
    assert x.dtype == y.dtype
    orig_dtype = x.dtype
    unsigned_dtype = _SIGNED_TO_UNSIGNED_TABLE[orig_dtype]
    x = tf.cast(x, unsigned_dtype)
    y = tf.cast(y, unsigned_dtype)
    res = tf.bitwise.right_shift(x, y)
    return tf.cast(res, orig_dtype)


def _shift_right_logical(x, y):
  # TF shift is "implementation defined" if the shift amount is negative
  # or larger or equal to the size of the value. We implement the XLA semantics
  # to return 0.
  # TODO: it is likely better to add XlaOps for shifts
  return tf.where(
      _shift_in_bounds(x, y), _shift_right_logical_raw(x, y), tf.zeros_like(x))


tf_impl[lax.shift_right_logical_p] = _shift_right_logical


def _shift_left(x, y):
  # TF shift is "implementation defined" if the shift amount is negative
  # or larger or equal to the size of the value. We implement the XLA semantics
  # to return 0.
  # TODO: it is likely better to add XlaOps for shifts
  return tf.where(
      _shift_in_bounds(x, y), tf.bitwise.left_shift(x, y), tf.zeros_like(x))


tf_impl[lax.shift_left_p] = _shift_left


def _shift_in_bounds(x: TfVal, y: TfVal) -> TfVal:
  # Return the TF expression for when y is within bounds (0 <= y < |x|)
  x_bits = 8 * x.dtype.size
  # TF does not have comparisons for uint16 and uint32 (despite what the
  # documentation says)
  y_comp = tf.cast(
      y, _UNSIGNED_TO_SIGNED_TABLE[y.dtype]) if y.dtype.is_unsigned else y
  y_lt_x_bits = tf.math.less(y_comp, x_bits)
  y_ge_0 = tf.math.greater_equal(y_comp, 0)
  return tf.logical_and(y_lt_x_bits, y_ge_0)


def _not(x):
  """Computes bitwise not with support for booleans.

  Numpy and JAX support bitwise not for booleans by applying a logical not!
  This means that applying bitwise_not yields an unexpected result:
    jnp.bitwise_not(jnp.array([True, False]))
    >> DeviceArray([False,  True], dtype=bool)

  if you assume that booleans are simply casted to integers.
    jnp.bitwise_not(jnp.array([True, False]).astype(np.int32)).astype(bool)
    >> DeviceArray([True,  True], dtype=bool)
  """
  if x.dtype == tf.bool:
    return tf.logical_not(x)
  else:
    return tf.bitwise.invert(x)


tf_impl[lax.not_p] = _not


def bool_to_int8(f, argnums: Sequence[int]):
  """Computes functions with some bool args and bool results using int8.

  This is needed because some TF ops do not work for bool args, e.g.,
  inequalities, min/max.

  Args:
    f: a TF callable to wrap. It will be called with non-boolean arguments.
    argnums: the positional arguments that may be booleans.

  Returns: a TF callable that can take a mix of boolean positional arguments
    (in the positions specified by `argnums`) and some non-boolean positional
    arguments. If there are no boolean arguments, just calls `f`. Otherwise,
    casts the boolean arguments to `int8`, calls `f`, then casts the result to
    `bool`.
  """
  argnums = tf.nest.flatten(argnums)

  def wrapper(*args: TfVal, **kwargs):
    argnum_types = {args[i].dtype for i in argnums}
    if tf.bool not in argnum_types:
      return f(*args, **kwargs)
    else:
      # All argnums should be boolean
      assert len(argnum_types) == 1, argnum_types
      args_cast = [(tf.cast(a, tf.int8) if i in argnums else a)
                   for i, a in enumerate(args)]
      if "_in_avals" in kwargs:

        def cast_aval(aval):
          assert aval.dtype == np.bool_
          return core.ShapedArray(aval.shape, np.int8)

        _in_avals_cast = [
            cast_aval(aval) if i in argnums else aval
            for i, aval in enumerate(kwargs["_in_avals"])
        ]
        _out_aval_cast = tf.nest.map_structure(cast_aval, kwargs["_out_aval"])
        kwargs = dict(
            kwargs, _in_avals=_in_avals_cast, _out_aval=_out_aval_cast)
      out = f(*args_cast, **kwargs)
      return tf.nest.map_structure(lambda o: tf.cast(o, tf.bool), out)

  return wrapper


tf_impl[lax.or_p] = bool_to_int8(tf.bitwise.bitwise_or, argnums=(0, 1))
tf_impl[lax.and_p] = bool_to_int8(tf.bitwise.bitwise_and, argnums=(0, 1))
tf_impl[lax.xor_p] = bool_to_int8(tf.bitwise.bitwise_xor, argnums=(0, 1))

tf_impl[lax.eq_p] = tf.math.equal
tf_impl[lax.ne_p] = tf.math.not_equal

tf_impl[lax.ge_p] = bool_to_int8(tf.math.greater_equal, argnums=(0, 1))
tf_impl[lax.gt_p] = bool_to_int8(tf.math.greater, argnums=(0, 1))
tf_impl[lax.le_p] = bool_to_int8(tf.math.less_equal, argnums=(0, 1))
tf_impl[lax.lt_p] = bool_to_int8(tf.math.less, argnums=(0, 1))

tf_impl[lax_linalg.cholesky_p] = tf.linalg.cholesky


def _convert_element_type(operand, *, new_dtype, weak_type=False):
  old_dtype = operand.dtype.as_numpy_dtype
  if (dtypes.issubdtype(old_dtype, np.complexfloating) and
      not dtypes.issubdtype(new_dtype, np.complexfloating)):
    operand = tf.math.real(operand)
  if (dtypes.issubdtype(old_dtype, np.floating) and
      not (dtypes.issubdtype(new_dtype, np.floating) or dtypes.issubdtype(
          new_dtype, np.complexfloating) or new_dtype == np.bool_)):
    sign = _sign(operand)
    operand = sign * tf.math.floor(sign * operand)
  return tf.dtypes.cast(operand, _to_tf_dtype(new_dtype))


tf_impl[lax.convert_element_type_p] = _convert_element_type


def _bitcast_convert_type(operand, new_dtype):
  if operand.dtype == new_dtype:
    return operand
  return tf.bitcast(operand, _to_tf_dtype(new_dtype))


tf_impl[lax.bitcast_convert_type_p] = _bitcast_convert_type


def _clamp(minval, operand, maxval, *, _in_avals, _out_aval):
  # The below permits mirroring the behavior of JAX when maxval < minval
  op_shape_tf_val = _eval_shape(_in_avals[1].shape)
  maxval = tf.broadcast_to(maxval, op_shape_tf_val)
  minval = tf.math.minimum(tf.broadcast_to(minval, op_shape_tf_val), maxval)
  return tf.clip_by_value(operand, minval, maxval)


tf_impl_with_avals[lax.clamp_p] = _clamp


def _concatenate(*operands, dimension):
  return tf.concat(operands, axis=dimension)


tf_impl[lax.concatenate_p] = _concatenate


def _conv_general_dimension_numbers_proto(dimension_numbers):
  """Converts a ConvDimensionNumbers to an XLA ConvolutionDimensionNumbers."""
  assert isinstance(dimension_numbers, lax.ConvDimensionNumbers)
  lhs_spec, rhs_spec, out_spec = dimension_numbers
  proto = xla_data_pb2.ConvolutionDimensionNumbers()
  proto.input_batch_dimension = lhs_spec[0]
  proto.input_feature_dimension = lhs_spec[1]
  proto.output_batch_dimension = out_spec[0]
  proto.output_feature_dimension = out_spec[1]
  proto.kernel_output_feature_dimension = rhs_spec[0]
  proto.kernel_input_feature_dimension = rhs_spec[1]
  proto.input_spatial_dimensions.extend(lhs_spec[2:])
  proto.kernel_spatial_dimensions.extend(rhs_spec[2:])
  proto.output_spatial_dimensions.extend(out_spec[2:])
  return proto


def _precision_config_proto(precision: Optional[Tuple[PrecisionType,
                                                      PrecisionType]]):
  """Convert an integer to an XLA.PrecisionConfig."""
  if precision is None:
    return None

  proto = xla_data_pb2.PrecisionConfig()
  proto.operand_precision.append(int(precision[0]))
  proto.operand_precision.append(int(precision[1]))
  return proto


def _try_tf_conv(lhs, rhs, window_strides, padding, lhs_dilation, rhs_dilation,
                 dimension_numbers, feature_group_count, batch_group_count,
                 preferred_element_type: Optional[DType], out_shape) -> TfVal:

  def error(msg):
    suffix = ("See source code for the precise conditions under which "
              "convolutions can be converted without XLA.")
    return _xla_disabled_error("conv_general_dilated", f"{msg} - {suffix}")

  # TODO(bchetioui): this function is not exhaustive wrt which convolution cases
  # can be translated into TF primitives. Further investigation is needed to
  # fully flesh it out.
  if lhs.dtype not in [tf.float16, tf.float32, tf.float64]:
    raise error(f"tf.nn.convolution is not supported for dtype {lhs.dtype}")
  if feature_group_count != 1:
    raise error("tf.nn.convolution does not support grouped convolutions")
  # TODO(bchetioui): is there something to do with batch_group_count?
  if batch_group_count != 1:
    raise error("Unimplemented support for batch_group_count != 1")
  nb_spatial_dimensions = len(lhs.shape) - 2
  # TF can only deal with 1D, 2D and 3D convolution
  if nb_spatial_dimensions < 1 or nb_spatial_dimensions > 3:
    raise error("TensorFlow can only handle convolutions with 1, 2, or 3 "
                "spatial dimensions")
  # TODO(bchetioui): handle different stride cases
  if list(window_strides) != [1] * nb_spatial_dimensions:
    raise error("Unimplemented support for window_strides != "
                f"{tuple([1] * nb_spatial_dimensions)}")

  if preferred_element_type is not None and preferred_element_type != lhs.dtype:
    raise error("Unimplemented support for preferred_element_type")

  def convert_padding() -> str:
    # TODO(bchetioui): in this instance, we can not use padtype_to_pads as
    # string padding is not implemented for transposed convolution.
    if list(lhs_dilation) != [1] * nb_spatial_dimensions:
      raise error("Padding conversion is not supported for transposed "
                  "convolution.")
    lhs_perm, rhs_perm, _ = dimension_numbers
    effective_rhs_shape = [
        (k - 1) * r + 1
        for k, r in zip(np.take(rhs.shape, rhs_perm)[2:], rhs_dilation)
    ]
    lhs_shape = np.take(lhs.shape, lhs_perm)[2:]
    # TF only allows 'VALID' and 'SAME' padding
    for pad_str in ["VALID", "SAME"]:
      gen_padding = lax.padtype_to_pads(lhs_shape, effective_rhs_shape,
                                        window_strides, pad_str)
      if list(gen_padding) == list(padding):
        return pad_str
    raise error("Input padding not supported in TensorFlow.")

  def convert_dim_nums() -> str:
    lhs_spec, rhs_spec, out_spec = dimension_numbers
    # TF only allows filters with shape:
    # spatial_filter_shape + [in_channels, out_channels]. In JAX however,
    # rhs_spec is represented as a tuple containing the following:
    # [out_channels, in_channels] + spatial_filter_shape.
    supported_rhs_shape = ([nb_spatial_dimensions + 1, nb_spatial_dimensions] +
                           list(range(nb_spatial_dimensions)))
    if list(rhs_spec) != supported_rhs_shape:
      raise error("Input filter (RHS) shape format not supported in "
                  "TensorFlow.")
    # TF only supports same LHS and output data format
    if lhs_spec != out_spec:
      raise error("TensorFlow requires the same data format for LHS and "
                  "output.")
    # Alphabet extracted from the documentation of tf.conv{1,2,3}d
    spatial_dim_alphabet = "DHW"[-nb_spatial_dimensions:]
    # TF only supports the following data formats:
    # - [batch_size, in_channels] + input_spatial_shape

    # TODO(bchetioui): TF currently does not support the above on CPU. To avoid
    # failing on this platform, this path is commented out for now.
    # if list(lhs_spec) == list(range(len(lhs_spec))):
    #  return "NC" + spatial_dim_alphabet

    # - [batch_size] + input_spatial_shape + [in_channels]
    if list(lhs_spec) == ([0, len(lhs_spec) - 1] +
                          list(range(1,
                                     len(lhs_spec) - 1))):
      return "N" + spatial_dim_alphabet + "C"
    raise error("Data format is unsupported by TensorFlow.")

  def convert_dilation_and_compute_result(tf_padding: str,
                                          tf_dim_nums: str) -> TfVal:
    no_dilation = [1] * nb_spatial_dimensions
    # TODO(bchetioui): is there a generic way to do a transposed atrous
    # convolution in TensorFlow?
    if not (list(lhs_dilation) == no_dilation or
            list(rhs_dilation) == no_dilation):
      raise error("Both LHS and RHS dilations are set.")
    # This is a non-dilated or atrous convolution
    if list(lhs_dilation) == no_dilation:
      return tf.nn.convolution(
          lhs,
          rhs,
          strides=window_strides,
          padding=tf_padding,
          data_format=tf_dim_nums,
          dilations=rhs_dilation)
    # TODO(bchetioui): the below path is unreachable for now, as passing a lhs
    # dilation to this function will result in convert_padding returning None
    # systematically. This must be investigated further.
    # Dilation of the LHS is transposed convolution
    return tf.nn.conv_transpose(
        lhs,
        rhs,
        out_shape,
        window_strides,
        padding=tf_padding,
        data_format=tf_dim_nums,
        dilations=lhs_dilation)

  tf_padding = convert_padding()
  tf_dim_nums = convert_dim_nums()
  return convert_dilation_and_compute_result(tf_padding, tf_dim_nums)


def _conv_general_dilated(lhs, rhs, *,
                          window_strides, padding, lhs_dilation,
                          rhs_dilation,
                          dimension_numbers: lax.ConvDimensionNumbers,
                          feature_group_count: int,
                          batch_group_count: int,
                          lhs_shape: Sequence[int],
                          rhs_shape: Sequence[int],
                          precision: Optional[Tuple[PrecisionType, PrecisionType]],
                          preferred_element_type: Optional[DType],
                          _in_avals: Sequence[core.ShapedArray],
                          _out_aval: core.ShapedArray):
  """Implementation of lax.conv_general_dilated_p using XlaConv."""
  out_tf_shape = _aval_to_tf_shape(_out_aval)
  if not _thread_local_state.enable_xla:
    return _try_tf_conv(
        lhs, rhs, window_strides, padding, lhs_dilation, rhs_dilation,
        dimension_numbers, feature_group_count, batch_group_count,
        preferred_element_type, out_tf_shape)

  dnums_proto = _conv_general_dimension_numbers_proto(dimension_numbers)
  precision_config_proto = _precision_config_proto(precision)
  assert batch_group_count == 1  # TODO(necula): implement batch_group_count

  def gen_conv(lhs, rhs, preferred_element_type: Optional[DType]):
    out = tfxla.conv(
        lhs,
        rhs,
        window_strides,
        padding,
        lhs_dilation,
        rhs_dilation,
        dnums_proto,
        feature_group_count=feature_group_count,
        precision_config=precision_config_proto,
        preferred_element_type=preferred_element_type,
        use_v2=True)
    # TODO: implement shape inference for XlaConv
    out.set_shape(out_tf_shape)
    return out

  # Follow the lowering for complex convolutions from
  # lax._conv_general_dilated_translation. We can use the same conversion on all
  # platforms because on XLA:TPU the compiler does the same as a rewrite.
  preferred_float_et: Optional[Any]
  if np.issubdtype(_in_avals[0].dtype, np.complexfloating):
    if preferred_element_type is not None:
      # Convert complex dtype to types used for real and imaginary parts
      assert np.issubdtype(preferred_element_type, np.complexfloating)
      preferred_float_et = (
          np.float64 if preferred_element_type == np.complex128 else np.float32)
    else:
      preferred_float_et = None
    lhs_real, lhs_imag = tf.math.real(lhs), tf.math.imag(lhs)
    rhs_real, rhs_imag = tf.math.real(rhs), tf.math.imag(rhs)
    k1 = gen_conv(_add(lhs_real, lhs_imag), rhs_real, preferred_float_et)
    k2 = gen_conv(lhs_real, tf.math.subtract(rhs_imag, rhs_real),
                  preferred_float_et)
    k3 = gen_conv(lhs_imag, _add(rhs_real, rhs_imag), preferred_float_et)
    return tf.complex(tf.math.subtract(k1, k3), _add(k1, k2))
  else:
    return gen_conv(lhs, rhs, preferred_element_type)


tf_impl_with_avals[lax.conv_general_dilated_p] = _conv_general_dilated


def _dot_general(lhs, rhs, *, dimension_numbers,
                 precision: Optional[Tuple[PrecisionType, PrecisionType]],
                 preferred_element_type: Optional[DType],
                 _in_avals: Sequence[core.ShapedArray],
                 _out_aval: core.ShapedArray):
  """Implementation of lax.dot_general_p in terms of tf.linalg.einsum."""
  (lhs_contracting, rhs_contracting), (lhs_batch, rhs_batch) = dimension_numbers
  lhs_ndim, rhs_ndim = len(lhs.shape), len(rhs.shape)
  if _thread_local_state.enable_xla:
    dnums_proto = xla_data_pb2.DotDimensionNumbers()
    dnums_proto.lhs_contracting_dimensions.extend(lhs_contracting)
    dnums_proto.rhs_contracting_dimensions.extend(rhs_contracting)
    dnums_proto.lhs_batch_dimensions.extend(lhs_batch)
    dnums_proto.rhs_batch_dimensions.extend(rhs_batch)
    precision_config_proto = _precision_config_proto(precision)
    res = tfxla.dot_general(
        lhs,
        rhs,
        dnums_proto,
        precision_config_proto,
        preferred_element_type=preferred_element_type,
        use_v2=True)
    return res

  # This condition ensures that:
  # 1) the batch dimensions are ordered in the same way in lhs and rhs (this is
  #    not strictly necessary, but we would have to reshape the array if that
  #    were not the case;
  # 2) lhs and rhs have the same number of dimensions +/- 1
  # 3) the number of non-batch dimensions in both tensors is either 1 or 2
  # 4) the contracting dimensions are consistent with those of a classic
  #    matrix/matrix, vector/matrix or matrix/vector multiplication.
  if (lhs_batch == rhs_batch == tuple(range(len(lhs_batch))) and
      lhs_ndim - rhs_ndim in [-1, 0, 1] and
      1 <= lhs_ndim - len(lhs_batch) <= 2 and
      1 <= rhs_ndim - len(rhs_batch) <= 2 and
      lhs_contracting == (len(lhs.shape) - 1,) and
      rhs_contracting == (len(lhs_batch),)):
    # All the inputs to tf.linalg.matmul must have 2 inner dimensions,
    # after their batch dimensions, so we need to expand the dimensions
    # appropriately. We can get to this branch with three combinations of
    # inner shapes:
    # - lhs.inner_shape == [a, b], rhs.inner_shape == [b, c]
    #   - in this case, the resulting inner shape is [a, c];
    # - lhs.inner_shape == [b]   , rhs.inner_shape == [b, c]
    #   - in this case, we need to expand lhs to [1, b], and the resulting
    #     shape is [c]. We need to squeeze the result of tf.linalg.matmul
    #     as it will have shape [1, c];
    # - lhs.shape == [batch] + [a, b], rhs.shape == [batch] + [b]
    #   - in this case, we need to expand rhs to [b, 1], and the resulting
    #     shape is [a]. We need to squeeze the result of tf.linalg.matmul
    #     as it will have shape [a, 1];
    # - lhs.shape == [batch] + [b]   , rhs.shape == [batch] + [b]
    #   - in this case, we need to expand lhs to [1, b] and rhs to [b, 1],
    #     and the resulting shape is (). We need to squeeze the result of
    #     tf.linalg.matmul as it will have shape [1, 1].
    squeeze_idxs = []
    if lhs_ndim - len(lhs_batch) == 1:
      lhs = tf.expand_dims(lhs, lhs_ndim - 1)
      squeeze_idxs.append(len(lhs.shape) - 2)
    if rhs_ndim - len(rhs_batch) == 1:
      rhs = tf.expand_dims(rhs, rhs_ndim)
      squeeze_idxs.append(len(rhs.shape) - 1)
    result = tf.linalg.matmul(lhs, rhs)
    if len(squeeze_idxs) != 0:
      assert all([result.shape[i] == 1 for i in squeeze_idxs])
      result = tf.squeeze(result, squeeze_idxs)
    return result

  new_id = iter(string.ascii_letters)
  lhs_axis_ids = [next(new_id) for _ in lhs.shape]
  rhs_axis_ids = [next(new_id) for _ in rhs.shape]
  lhs_out_axis_ids = lhs_axis_ids[:]
  rhs_out_axis_ids = rhs_axis_ids[:]

  for lhs_axis, rhs_axis in zip(lhs_contracting, rhs_contracting):
    shared_id = next(new_id)
    lhs_axis_ids[lhs_axis] = shared_id
    rhs_axis_ids[rhs_axis] = shared_id
    lhs_out_axis_ids[lhs_axis] = None  # type: ignore[call-overload]
    rhs_out_axis_ids[rhs_axis] = None  # type: ignore[call-overload]

  batch_ids = []
  for lhs_axis, rhs_axis in zip(lhs_batch, rhs_batch):
    shared_id = next(new_id)
    lhs_axis_ids[lhs_axis] = shared_id
    rhs_axis_ids[rhs_axis] = shared_id
    lhs_out_axis_ids[lhs_axis] = None  # type: ignore[call-overload]
    rhs_out_axis_ids[rhs_axis] = None  # type: ignore[call-overload]
    batch_ids.append(shared_id)

  not_none = lambda x: x is not None
  out_axis_ids = list(
      filter(not_none, batch_ids + lhs_out_axis_ids + rhs_out_axis_ids))
  assert lhs.dtype == rhs.dtype
  spec = "{},{}->{}".format("".join(lhs_axis_ids), "".join(rhs_axis_ids),
                            "".join(out_axis_ids))
  return tf.linalg.einsum(spec, lhs, rhs)


tf_impl_with_avals[lax.dot_general_p] = _dot_general


def _broadcast_in_dim(operand, *, shape, broadcast_dimensions,
                      _in_avals: Sequence[core.ShapedArray],
                      _out_aval: core.ShapedArray):
  # for i in range(len(operand.shape)):
  #   result.shape[bcast_dims[i]] <- operand.shape[i]
  # bcast_dims must be strictly increasing.
  # len(bcast_dims) == len(operand.shape)
  op_shape = _in_avals[0].shape
  add_1s_shape = [1] * len(shape)
  for i, broadcast_dim_i in enumerate(broadcast_dimensions):
    add_1s_shape[broadcast_dim_i] = op_shape[i]
  with_1s = tf.reshape(operand, _eval_shape(add_1s_shape))
  return tf.broadcast_to(with_1s, _eval_shape(shape))


tf_impl_with_avals[lax.broadcast_in_dim_p] = _broadcast_in_dim


def _reshape(operand, *, new_sizes, dimensions):
  if dimensions is None:
    dimensions = tf.range(tf.rank(operand))
  new_sizes_tf = _eval_shape(new_sizes)
  return tf.reshape(tf.transpose(operand, dimensions), new_sizes_tf)


tf_impl[lax.reshape_p] = _reshape


def _squeeze(operand, *, dimensions, _in_avals, _out_aval):
  op_shape = _in_avals[0].shape
  new_shape = tuple(d for i, d in enumerate(op_shape) if i not in dimensions)
  new_shape_tf = _eval_shape(new_shape)
  return tf.reshape(operand, new_shape_tf)


tf_impl_with_avals[lax.squeeze_p] = _squeeze


def _pad(operand, padding_value, *, padding_config,
         _in_avals: Sequence[core.ShapedArray],
         _out_aval: core.ShapedArray):
  low, high, interior = util.unzip3(padding_config)
  if _thread_local_state.enable_xla:
    out = tfxla.pad(operand, padding_value, low, high, interior)
    return out

  # Do only the interior padding first. This is rarely needed.
  if any(i != 0 for _, _, i in padding_config):
    operand = _interior_padding(operand, padding_value, padding_config,
                                _eval_shape(_in_avals[0].shape))

  # Now do the non-negative edge padding. This is the common case, use tf.pad.
  non_negative_padding = [((lo if lo >= 0 else 0), (hi if hi >= 0 else 0))
                          for lo, hi, _ in padding_config]
  operand = tf.pad(operand, non_negative_padding,
                   mode="CONSTANT",
                   constant_values=padding_value)
  # Now the negative edge padding (this is also rare)
  if any(lo < 0 or hi < 0 for lo, hi, _ in padding_config):
    output_shape = _eval_shape(_out_aval.shape)
    begins = [(-lo if lo < 0 else 0) for lo, _, _ in padding_config]
    operand = tf.slice(operand, begins, output_shape)

  return operand

tf_impl_with_avals[lax.pad_p] = _pad

def _interior_padding(operand, padding_value, padding_config, operand_shape):
  # Used only when enable_xla=False
  # Applies only the interior padding from the padding_config.
  # We do this somewhat inefficiently, as as a scatter.
  # For each dimension we compute the indices_by_dim as [0, f, 2f, 3f, ...] where
  # f is the dilation factor for the dimension, i.e., 1 + interior_padding.
  # Then we compute the cartesian production of the indices (using broadcast
  # and concat).

  # We could make this code more complex and do all the padding at once, but
  # we prefer to keep it simple.
  indices_by_dim = []
  indices_shape = operand_shape + (1,)
  output_shape = []  # considering only interior padding
  for d, (dsz, (_, _, i)) in enumerate(zip(operand_shape, padding_config)):
    dilation_factor = i + 1
    output_shape.append(dsz * dilation_factor - i)
    indices = tf.range(dsz) * dilation_factor
    expansion = [None] * (1 + len(operand_shape))
    expansion[d] = slice(None, None, None)
    indices_by_dim.append(tf.broadcast_to(indices[expansion], indices_shape))

  indices_cartesian = tf.concat(indices_by_dim, axis=len(operand_shape))
  scattered = tf.scatter_nd(indices_cartesian, operand, output_shape)
  # What elements from the output array we use from
  mask = tf.scatter_nd(indices_cartesian, tf.ones_like(operand, dtype=np.bool_), output_shape)
  return tf.where(mask, scattered, padding_value)


def _rev(operand, *, dimensions):
  return tf.reverse(operand, dimensions)


tf_impl[lax.rev_p] = _rev

tf_impl[lax.select_p] = tf.where


def _transpose(operand, *, permutation):
  return tf.transpose(operand, perm=permutation)


tf_impl[lax.transpose_p] = _transpose

axes_to_axis = lambda func: lambda operand, axes: func(operand, axis=axes)

# reduce_sum and reduce_prod are not supported for bool
tf_impl[lax.reduce_sum_p] = axes_to_axis(tf.reduce_sum)
tf_impl[lax.reduce_prod_p] = axes_to_axis(tf.reduce_prod)
tf_impl[lax.reduce_max_p] = (
    bool_to_int8(axes_to_axis(tf.reduce_max), argnums=[0]))
tf_impl[lax.reduce_min_p] = (
    bool_to_int8(axes_to_axis(tf.reduce_min), argnums=[0]))
tf_impl[lax.reduce_or_p] = axes_to_axis(tf.reduce_any)
tf_impl[lax.reduce_and_p] = axes_to_axis(tf.reduce_all)


def _argminmax(is_min: bool, operand: TfVal, axes: Sequence[int],
               index_dtype: DType,
               _in_avals: Sequence[core.ShapedArray],
               _out_aval: core.ShapedArray):
  if _thread_local_state.enable_xla:
    # Follow the JAX implementation, using a XlaReduce with a custom comparator
    if is_min:
      extra_name_stack = "argmin"
      value_comparator = lax.lt
      get_identity = lax._get_min_identity
    else:
      extra_name_stack = "argmax"
      value_comparator = lax.gt
      get_identity = lax._get_max_identity

    res = _convert_jax_impl(
        partial(lax._compute_argminmax, value_comparator, get_identity),
        multiple_results=False, extra_name_stack=extra_name_stack)(
        operand, index_dtype=index_dtype, axes=axes,
        _in_avals=_in_avals, _out_aval=_out_aval)
    return res

  # The following is known to diverge from JAX behavior for NaN.
  axis, = axes
  output_type = tf.int32
  if dtypes.iinfo(index_dtype).bits > 32:
    output_type = tf.int64
  # TODO(phawkins): handle axes larger than 2^31.
  fn = tf.math.argmin if is_min else tf.math.argmax
  result = fn(operand, axis=axis, output_type=output_type)
  return tf.cast(result, _to_tf_dtype(index_dtype))


tf_impl_with_avals[lax.argmin_p] = partial(_argminmax, True)
tf_impl_with_avals[lax.argmax_p] = partial(_argminmax, False)


_add_fn = tf.function(_add, autograph=False)
_ge_fn = tf.function(tf.math.greater_equal, autograph=False)


def _select_and_gather_add(
    tangents: TfVal, operand: TfVal, select_prim: core.Primitive,
    window_dimensions: Sequence[int], window_strides: Sequence[int],
    base_dilation: Sequence[int], window_dilation: Sequence[int],
    padding: Sequence[Tuple[int, int]], _in_avals: Sequence[core.ShapedArray],
    _out_aval: core.ShapedArray):
  # Note: this function follows the pattern in
  # jax.lax._select_and_gather_add_translation.
  dtype = operand.dtype
  nbits = dtypes.finfo(dtype.as_numpy_dtype).bits

  # Specializing the function for 64 bits. Only up to 32 bits are supported on TPU,
  # we thus intend to let the code throw a different exception on this platform.
  max_bits = 64

  assert nbits <= max_bits
  double_word_reduction = nbits * 2 <= max_bits

  const = lambda dtype, x: tf.constant(np.array(x), dtype)

  if double_word_reduction:
    word_dtype = lax._UINT_DTYPES[nbits]
    double_word_dtype = lax._UINT_DTYPES[nbits * 2]

    # Packs two values into a tuple.
    def pack(a, b):
      a = _bitcast_convert_type(a, word_dtype)
      b = _bitcast_convert_type(b, word_dtype)
      a = _convert_element_type(a, new_dtype=double_word_dtype)
      b = _convert_element_type(b, new_dtype=double_word_dtype)
      a = tf.bitwise.left_shift(a, const(double_word_dtype, nbits))
      return tf.bitwise.bitwise_or(a, b)

    # Unpacks the first element of a tuple.
    def fst(t):
      assert t.dtype == double_word_dtype
      st = _shift_right_logical(t, const(double_word_dtype, nbits))
      return _bitcast_convert_type(
          _convert_element_type(st, new_dtype=word_dtype), dtype)

    # Unpacks the second element of a tuple.
    def snd(t):
      return _bitcast_convert_type(
          _convert_element_type(t, new_dtype=word_dtype), dtype)

  else:
    raise NotImplementedError(
        f"TODO: need to pack {nbits * 2} bits but this platform can only go up to {max_bits} bits."
    )

  assert select_prim is lax.ge_p or select_prim is lax.le_p, select_prim

  def reducer(x, y):
    which = tf_impl[select_prim]
    return tf_impl[lax.select_p](which(fst(x), fst(y)), x=x, y=y)

  init = -np.inf if select_prim is lax.ge_p else np.inf
  init_identity = lambda x: pack(const(dtype, init), const(dtype, 0))

  out = _specialized_reduce_window(
      reducer,
      init_identity,
      pack(operand, tangents),
      window_dimensions=window_dimensions,
      window_strides=window_strides,
      padding=padding,
      base_dilation=base_dilation,
      window_dilation=window_dilation,
      _in_avals=_in_avals,
      _out_aval=_out_aval)

  return snd(out)


tf_impl_with_avals[lax.select_and_gather_add_p] = _select_and_gather_add


def _get_shape_from_tensor_or_array(x):
  if isinstance(x.shape, tf.TensorShape):
    return tuple(x.shape.as_list())
  return tuple(x.shape)


def _common_reduce_window(operand, init_val, reducer, window_dimensions,
                          window_strides, padding, base_dilation,
                          window_dilation, _in_avals, _out_aval):
  o_spec = tf.TensorSpec((), dtype=operand.dtype)
  reducer_fn = tf.function(
      reducer, autograph=False).get_concrete_function(o_spec, o_spec)

  if not isinstance(init_val, (tf.Tensor, tf.Variable)):
    init_val = tf.constant(init_val, operand.dtype)
  out = tfxla.reduce_window(
      operand,
      init_val,
      reducer_fn,
      window_dimensions,
      window_strides,
      base_dilations=base_dilation,
      window_dilations=window_dilation,
      padding=padding)
  # TODO: implement shape inference for XlaReduceWindow
  out.set_shape(_aval_to_tf_shape(_out_aval))
  return out


def _reduce_window(operand, init_value, *, jaxpr, consts, window_dimensions,
                   window_strides, padding, base_dilation, window_dilation,
                   _in_avals, _out_aval):
  """TensorFlow implementation of reduce_window.

  Args:
    operand: N dimensional array containing elements of type T
    init_value: starting value of the reduction
    jaxpr: the jaxpr corresponding to the reduction function
    consts: the constants associated with jaxpr.
    window_dimensions: array of integers for window dimension values
    window_strides: array of integers for window stride values
    padding: array of pairs of integers for padding values
    base_dilation: array of integers for base dilation values
    window_dilation: array of integers for window dilation values

  Returns:
    The reduced operand.
  """
  assert len(consts) == 0, "Reduction computation cannot have constants"

  if not _thread_local_state.enable_xla:
    raise _xla_disabled_error("reduce_window")

  def reducer(arg1: TfVal, arg2: TfVal) -> TfVal:
    closed_jaxpr = core.ClosedJaxpr(jaxpr, consts)
    res, = _interpret_jaxpr(closed_jaxpr, arg1, arg2, extra_name_stack=None)
    return res

  return _common_reduce_window(operand, init_value, reducer, window_dimensions,
                               window_strides, padding, base_dilation,
                               window_dilation, _in_avals, _out_aval)


# _try_tf_pool currently only supports reduce_window_max and reduce_window_sum.
# TODO(bchetioui): this function is not exhaustive wrt which
# reduce_window_max or reduce_window_sum cases can be translated into a call to
# max_pool or avg_pool. Further investigation is needed to fully flesh it out.
def _try_tf_pool(op_name, operand, window_dimensions, window_strides, padding,
                 base_dilation, window_dilation) -> TfVal:

  def error(msg):
    suffix = ("See source code for the precise conditions under which "
              "reduce_window can be converted without XLA.")
    return _xla_disabled_error("reduce_window", f"{msg} - {suffix}")

  dtype = operand.dtype
  # Contrarily to the main path, tf.int8 is actually a valid type for
  # tf.nn.max_pool.
  if op_name == "reduce_window_max" and dtype in [
      tf.bool, tf.uint32, tf.uint64, tf.complex64, tf.complex128
  ]:
    raise error(f"tf.nn.max_pool does not support operands of type {dtype}")
  if op_name == "reduce_window_sum" and operand.dtype not in [
      tf.float16, tf.float32, tf.float64
  ]:
    raise error(f"tf.nn.avg_pool does not support operands of type {dtype}")
  has_batch_dim = window_dimensions[0] == 1
  has_channel_dim = window_dimensions[-1] == 1
  nb_spatial_dimensions = len(operand.shape) - has_batch_dim - has_channel_dim
  if nb_spatial_dimensions < 1 or nb_spatial_dimensions > 3:
    raise error("TensorFlow can only handle pooling for arrays with 1, 2, or "
                "3 spatial dimensions")
  # TODO(bchetioui): does a simple conversion with another base dilation exist?
  if list(base_dilation) != [1] * len(operand.shape):
    raise error("Unimplemented support for base dilation")
  # TODO(bchetioui): does a simple conversion with another window_dilation
  # exist? The whole story seems similar to convolution.
  if list(window_dilation) != [1] * len(operand.shape):
    raise error("Unimplemented support for window dilation")
  if list(padding) != [(0, 0)] * len(operand.shape):
    raise error("Unimplemented support for padding")
  # ReduceWindow in XLA takes an array of rank N as a parameter, but
  # tf.nn.max_pool / tf.nn.avg_pool take an array of rank N+2, with a default
  # shape of the form [batch_size] + input_spatial_shape + [num_channels]
  tf_operand = operand
  tf_window_dimensions = list(window_dimensions)
  tf_window_strides = list(window_strides)
  if not has_batch_dim:
    tf_operand = tf.expand_dims(tf_operand, 0)
    tf_window_dimensions = [1] + tf_window_dimensions
    tf_window_strides = [1] + tf_window_strides
  if not has_channel_dim:
    tf_operand = tf.expand_dims(tf_operand, -1)
    tf_window_dimensions.append(1)
    tf_window_strides.append(1)
  tf_data_format = "N" + "DHW"[-nb_spatial_dimensions:] + "C"
  tf_padding = "VALID"
  if op_name == "reduce_window_max":
    result = tf.nn.max_pool(tf_operand, tf_window_dimensions, tf_window_strides,
                            tf_padding, tf_data_format)
  elif op_name == "reduce_window_sum":
    avg = tf.nn.avg_pool(tf_operand, tf_window_dimensions, tf_window_strides,
                         tf_padding, tf_data_format)
    result = avg * np.prod(tf_window_dimensions)
  else:
    raise error(f"Unimplemented support for {op_name}")

  if not has_batch_dim:
    result = tf.squeeze(result, 0)
  if not has_channel_dim:
    result = tf.squeeze(result, -1)
  return result


def _specialized_reduce_window(reducer,
                               identity,
                               operand,
                               *,
                               window_dimensions,
                               window_strides,
                               padding,
                               base_dilation,
                               window_dilation,
                               _in_avals,
                               _out_aval,
                               name=None):
  """Wraps the TensorFlow reduce window operation based on a reducer and an

  identity function defining the initial value of the reduction depending on
  the dtype of the operand.

  Args:
    reducer: reduction function of type TfVal -> TfVal -> TfVal
    identity: function that takes a TensorFlow dtype as a parameter and returns
      the starting value of the reduction.
    operand: N dimensional array containing elements of type T
    window_dimensions: array of integers for window dimension values
    window_strides: array of integers for window stride values
    padding: array of pairs of integers for padding values
    base_dilation: array of integers for base dilation values
    window_dilation: array of integers for window dilation values
    name: the name of the specialized reduce window primitive for which this
      conversion function is called. This information may help to choose a
      different conversion path (optional)

  Returns:
    The reduced operand.
  """
  if not _thread_local_state.enable_xla and name in ["reduce_window_max", "reduce_window_sum"]:
    return _try_tf_pool(name, operand, window_dimensions, window_strides,
                        padding, base_dilation, window_dilation)

  return _common_reduce_window(operand, identity(operand.dtype), reducer,
                               window_dimensions, window_strides, padding,
                               base_dilation, window_dilation, _in_avals,
                               _out_aval)


def _get_max_identity(tf_dtype):
  numpy_tf_dtype = tf_dtype.as_numpy_dtype
  if tf_dtype == tf.bfloat16 or dtypes.issubdtype(numpy_tf_dtype, np.inexact):
    return numpy_tf_dtype(-np.inf)
  elif dtypes.issubdtype(numpy_tf_dtype, np.integer):
    return dtypes.iinfo(numpy_tf_dtype).min
  else:
    assert dtypes.issubdtype(
        numpy_tf_dtype, np.bool_), (f"{tf_dtype} has no defined max identity")
    return False


def _get_min_identity(tf_dtype):
  numpy_tf_dtype = tf_dtype.as_numpy_dtype
  if tf_dtype == tf.bfloat16 or dtypes.issubdtype(numpy_tf_dtype, np.inexact):
    return numpy_tf_dtype(np.inf)
  elif dtypes.issubdtype(numpy_tf_dtype, np.integer):
    return dtypes.iinfo(numpy_tf_dtype).max
  else:
    assert dtypes.issubdtype(
        numpy_tf_dtype, np.bool_), (f"{tf_dtype} has no defined min identity")
    return True


# pylint: disable=protected-access
tf_impl_with_avals[lax.reduce_window_sum_p] = (
    partial(_specialized_reduce_window, _add, lambda x: 0,
            name="reduce_window_sum"))
tf_impl_with_avals[lax.reduce_window_min_p] = (
    partial(_specialized_reduce_window,
            partial(_minmax_scalar, is_min=True),
            _get_min_identity,
            name="reduce_window_min"))
tf_impl_with_avals[lax.reduce_window_max_p] = (
    partial(_specialized_reduce_window,
            partial(_minmax_scalar, is_min=False),
            _get_max_identity,
            name="reduce_window_max"))
tf_impl_with_avals[lax.reduce_window_p] = _reduce_window
# pylint: enable=protected-access

def _reduce(*operands: TfVal,
            computation: Callable,
            jaxpr: core.Jaxpr,
            consts:  Sequence[Any],
            dimensions: Sequence[int],
            _in_avals: Sequence[core.ShapedArray],
            _out_aval: core.ShapedArray) -> Sequence[TfVal]:

  if not _thread_local_state.enable_xla:
    raise _xla_disabled_error("reduce")
  del computation
  assert not consts
  assert len(operands) % 2 == 0
  # operands: op1, op2, ..., init_val1, init_val2, ...
  # reducer takes op1[i], op2[i], ..., init_val1, init_val2, ...
  nr_operands = len(operands) // 2
  init_vals = operands[nr_operands:]
  operands = operands[0:nr_operands]

  reducer_arg_spec = tuple([tf.TensorSpec((), op.dtype) for op in init_vals] * 2)

  def reducer_computation(*args: TfVal) -> TfVal:
    closed_jaxpr = core.ClosedJaxpr(jaxpr, consts)
    res = _interpret_jaxpr(closed_jaxpr, *args, extra_name_stack=None)
    return res

  xla_reducer_computation = (
      tf.function(reducer_computation,
                  autograph=False).get_concrete_function(*reducer_arg_spec))

  out = tfxla.variadic_reduce(operands, init_vals,
                              dimensions_to_reduce=dimensions,
                              reducer=xla_reducer_computation)
  return out

tf_impl_with_avals[lax.reduce_p] = _reduce


# We use lax_control_flow._cumred_tpu_translation_rule to convert cummax,
# cummin, cumsum and cumprod. This is efficient on TPU, but the complexity is
# O(n^2) on other backends. This may be implemented using associative_scan
# instead to favor different backends.
tf_impl_with_avals[lax_control_flow.cummin_p] = _convert_jax_impl(
    partial(lax_control_flow._cumred_tpu_translation_rule,
            lax._reduce_window_min),
    multiple_results=False,
    extra_name_stack="cummin")
tf_impl_with_avals[lax_control_flow.cummax_p] = _convert_jax_impl(
    partial(lax_control_flow._cumred_tpu_translation_rule,
            lax._reduce_window_max),
    multiple_results=False,
    extra_name_stack="cummin")
# TODO(bchetioui): cumsum and cumprod can be converted using pure TF ops for
# certain dtypes: bfloat16, float16, float32, float64, and int32. Other dtypes
# will fail when running in compiled mode, but are otherwise compatible with
# the operation. A non-XLA path can thus be defined for all dtypes, though the
# tests will crash.
tf_impl_with_avals[lax_control_flow.cumsum_p] = _convert_jax_impl(
    partial(lax_control_flow._cumred_tpu_translation_rule,
            lax._reduce_window_sum),
    multiple_results=False,
    extra_name_stack="cumsum")
tf_impl_with_avals[lax_control_flow.cumprod_p] = _convert_jax_impl(
    partial(lax_control_flow._cumred_tpu_translation_rule,
            lax._reduce_window_prod),
    multiple_results=False,
    extra_name_stack="cumprod")


def _select_and_scatter(operand, source, init_value, select_jaxpr,
                        select_consts, scatter_jaxpr, scatter_consts,
                        window_dimensions, window_strides, padding):
  raise NotImplementedError("TODO: jax2tf can not convert _select_and_scatter")


tf_impl[lax.select_and_scatter_p] = _select_and_scatter


@partial(bool_to_int8, argnums=(0, 1))
def _select_and_scatter_add(source, operand, *, select_prim, window_dimensions,
                            window_strides, padding, _in_avals, _out_aval):
  if not _thread_local_state.enable_xla:
    raise _xla_disabled_error("select_and_scatter_add")
  init_value = tf.zeros((), operand.dtype)
  select_fn = (
      tf.function(tf_impl[select_prim], autograph=False).get_concrete_function(
          init_value, init_value))
  scatter_fn = _add_fn.get_concrete_function(init_value, init_value)
  out = tfxla.select_and_scatter(operand, window_dimensions, window_strides,
                                 padding, source, init_value, select_fn,
                                 scatter_fn)
  out.set_shape(_aval_to_tf_shape(_out_aval))
  return out


tf_impl_with_avals[lax.select_and_scatter_add_p] = _select_and_scatter_add


def _threefry2x32_jax_impl(*args: TfVal, _in_avals, _out_aval):
  res = _convert_jax_impl(
      partial(jax._src.prng._threefry2x32_lowering, use_rolled_loops=False),
      multiple_results=True, extra_name_stack="threefry")(
          *args, _in_avals=_in_avals, _out_aval=_out_aval)
  return res


tf_impl_with_avals[jax._src.prng.threefry2x32_p] = _threefry2x32_jax_impl

# Use the vmap implementation, otherwise on TPU the performance is really bad
# With use_vmap=True on, we get about the same performance for JAX and jax2tf.
tf_impl_with_avals[random.random_gamma_p] = _convert_jax_impl(
    partial(jax._src.random._gamma_impl, use_vmap=True),
    multiple_results=False, extra_name_stack="random_gamma")


def _rng_bit_generator(key: TfVal, *, shape, dtype, algorithm):
  if not _thread_local_state.enable_xla:
    raise _xla_disabled_error("rng_bit_generator")

  shape_tf = _eval_shape(shape)
  # JAX uses XLA algorithm enums; tfxla uses tf.random.Algorithm
  if algorithm == lax.RandomAlgorithm.RNG_THREE_FRY:
    algorithm_tf = tf.random.Algorithm.THREEFRY
  elif algorithm == lax.RandomAlgorithm.RNG_PHILOX:
    algorithm_tf = tf.random.Algorithm.PHILOX
  elif algorithm == lax.RandomAlgorithm.RNG_DEFAULT:
    algorithm_tf = tf.random.Algorithm.AUTO_SELECT
  else:
    assert False
  out = tfxla.rng_bit_generator(algorithm_tf.value, key, shape_tf,
                                dtype=_to_tf_dtype(dtype))
  return out


tf_impl[lax.rng_bit_generator_p] = _rng_bit_generator


def _gather_dimensions_proto(indices_shape, dimension_numbers):
  proto = xla_data_pb2.GatherDimensionNumbers()
  proto.offset_dims.extend(dimension_numbers.offset_dims)
  proto.collapsed_slice_dims.extend(dimension_numbers.collapsed_slice_dims)
  proto.start_index_map.extend(dimension_numbers.start_index_map)
  assert indices_shape
  proto.index_vector_dim = len(indices_shape) - 1
  return proto


def _clip(max_indices: Sequence[TfVal], start_indices: Sequence[TfVal], slice_sizes: Sequence[TfVal]):
  """Simulates XLA clipping behavior with TF ops.

  Various TF ops have different clipping behavior than XLA:
  * If `start_indices` is out-of-bounds, then TF fails but XLA clips the indices to
    [0, max_len].
  * If `start_indices + slice_size` is out-of-bounds, then TF fails, but XLA adjust
    `start_indices` so that a full slice is returned.
  This function clips the start indices correctly.
  """
  max_start = tf.subtract(max_indices, slice_sizes)
  # If `start_indices` and `slice_sizes` are Python tuples of integers,
  # `tf.subtract` returns a Tensor of dtype tf.int32, which may conflict with
  # the dtype of `start_indices` if we run in x64 mode and throw an error when
  # calling `tf.clip_by_vaue`. Therefore we cast to the right dtype here
  # explicitly.
  max_start = tf.cast(max_start, dtype=start_indices.dtype)
  return tf.clip_by_value(start_indices, 0, max_start)


def _gather_using_tf_slice(operand: TfVal, start_indices: TfVal, *,
                           dimension_numbers, slice_sizes: core.Shape,
                           _in_avals: Sequence[core.ShapedArray],
                           _out_aval: core.ShapedArray):
  """Implements 'scalar indexing into arrays' cases of lax.gather using tf.slice.

  E.g., op[2], op[:, :5, :], jnp.take(op, 0, axis=0).
  """
  op_shape = _in_avals[0].shape
  indices = tf.expand_dims(dimension_numbers.start_index_map, 1)
  # lax.gather uses an "index map" which maps `start_indices` to the right axes
  # in `operand`. Since tf.strided_slice uses a single array for specifying the
  # start indices, we use a scatter to map the start indices to the right axes.
  begin = tf.scatter_nd(indices, start_indices, [len(op_shape)])
  slice_sizes_tf = _eval_shape(slice_sizes)
  begin = _clip(_eval_shape(op_shape), begin, slice_sizes_tf)
  end = slice_sizes_tf + begin

  # Convert from tuple of dimensions to shrink mask. e.g. (0, 2) --> 5.
  shrink_mask = sum(2 ** x for x in dimension_numbers.collapsed_slice_dims)
  res = tf.strided_slice(operand, begin, end, shrink_axis_mask=shrink_mask)
  # Shape inference doesn't work for tf.strided_slice.
  res.set_shape(_aval_to_tf_shape(_out_aval))
  return res


def _gather_using_tf_gather(operand: TfVal, start_indices: TfVal, *,
                            dimension_numbers, slice_sizes,
                            _in_avals: Sequence[core.ShapedArray]):
  """Implements 'multi-dimensional indexing into arrays' cases of lax.gather using tf.gather.

  E.g., jnp.take(op, [[0], [1]], axis=0).
  """
  # Handle only the case when tf.gather argument batch_dims=0.
  # Find axis to match the tf.gather semantics
  # Let I = len(start_indices_shape)
  # let O = len(op_shape)
  # slice_sizes == op_shape[:axis] + (1,) + op_shape[axis+1:]
  # collapsed_slice_dims == (axis,)
  # start_index_map == (axis,)
  # offset_dims == (0, 1, ..., axis - 1, axis + I, ..., O + I - 1)
  # We added a trailing dimension of size 1
  op_shape = _in_avals[0].shape
  start_indices_shape = _in_avals[1].shape
  assert len(op_shape) == len(slice_sizes)
  if not (len(op_shape) >= 1 and
          len(dimension_numbers.start_index_map) == 1 and
          len(dimension_numbers.collapsed_slice_dims) == 1 and
          dimension_numbers.collapsed_slice_dims[0] == dimension_numbers.start_index_map[0] and
          len(dimension_numbers.offset_dims) == len(op_shape) - 1):
    raise _xla_disabled_error(
        "gather",
        f"unsupported dimension_numbers '{dimension_numbers}'; op_shape={op_shape}.")
  # We added a trailing dimension of size 1
  if not core.symbolic_equal_dim(start_indices_shape[-1], 1):
    raise _xla_disabled_error("gather",
                              "trailing dimension for start_indices must be 1")
  # Guess the axis
  axis = dimension_numbers.collapsed_slice_dims[0]
  index_dims = len(start_indices_shape) - 1
  expected_offset_dims = tuple(
      list(range(axis)) +
      list(range(axis + index_dims, len(op_shape) + index_dims - 1)))
  if dimension_numbers.offset_dims != expected_offset_dims:
    raise _xla_disabled_error(
        "gather",
        f"unexpected dimension_numbers.offset_dims {dimension_numbers.offset_dims} != {expected_offset_dims}")
  expected_slice_sizes = op_shape[:axis] + (1,) + op_shape[axis + 1:]
  if not core.symbolic_equal_shape(slice_sizes, expected_slice_sizes):
    raise _xla_disabled_error(
        "gather",
        f"unexpected slice_sizes {slice_sizes} != {expected_slice_sizes}")

  squeezed_indices = tf.squeeze(start_indices, -1)
  start_indices = _clip((_eval_shape(op_shape)[axis],), squeezed_indices, (1,))
  return tf.gather(operand, start_indices, axis=axis, batch_dims=0)


@partial(bool_to_int8, argnums=[0])
def _gather(operand, start_indices, *, dimension_numbers, slice_sizes: core.Shape,
            indices_are_sorted, unique_indices, mode, fill_value,
            _in_avals: Sequence[core.ShapedArray],
            _out_aval: core.ShapedArray):
  """Tensorflow implementation of gather."""
  del unique_indices, fill_value

  if mode == lax.GatherScatterMode.FILL_OR_DROP:
    raise NotImplementedError("FILL_OR_DROP gather mode is not implemented in "
                              "jax2tf")

  if _thread_local_state.enable_xla:
    proto = _gather_dimensions_proto(start_indices.shape, dimension_numbers)
    slice_sizes_tf = _eval_shape(slice_sizes)
    out = tfxla.gather(operand, start_indices, proto, slice_sizes_tf,
                       indices_are_sorted)
    out.set_shape(_aval_to_tf_shape(_out_aval))
    return out

  # TODO(marcvanzee): Check if we need more tests in shape_poly for gather with
  # enable_xla=False.

  if len(_in_avals[1].shape) == 1:
    # Use tf.slice if `start_indices` is a 1D array.
    try:
      return _gather_using_tf_slice(operand, start_indices,
                                    dimension_numbers=dimension_numbers,
                                    slice_sizes=slice_sizes,
                                    _in_avals=_in_avals,
                                    _out_aval=_out_aval)
    except NotImplementedError:
      # If `_gather_using_tf_slice` fails, don't give up yet.
      pass

  return _gather_using_tf_gather(operand, start_indices,
                                 dimension_numbers=dimension_numbers,
                                 slice_sizes=slice_sizes,
                                 _in_avals=_in_avals)


tf_impl_with_avals[lax.gather_p] = _gather


def _slice(operand, start_indices, limit_indices, strides, _in_avals,
           _out_aval):
  if strides is None:
    strides = [1] * len(start_indices)
  slices = tuple(map(slice,
                     _eval_shape(start_indices),
                     _eval_shape(limit_indices),
                     _eval_shape(strides)))
  out = operand[slices]
  # TODO(b/184503314): improve shape inference for __getitem__
  # E.g., operand.shape=(b, 5, 3), start_indices=(0, 1, 1), limit_indices=(b, 5, 3), strides=(1, 2, 1)
  out.set_shape(_aval_to_tf_shape(_out_aval))
  return out


tf_impl_with_avals[lax.slice_p] = _slice


def _dynamic_slice(operand, *start_indices, slice_sizes: core.Shape,
                   _in_avals: Sequence[core.ShapedArray],
                   _out_aval: core.ShapedArray):
  start_indices = tf.stack(start_indices)
  slice_sizes_tf = _eval_shape(slice_sizes)

  if _thread_local_state.enable_xla:
    res = tfxla.dynamic_slice(operand, start_indices, size_indices=slice_sizes_tf)
    return res

  operand_shape = _eval_shape(_in_avals[0].shape)
  start_indices = _clip(operand_shape, start_indices, slice_sizes_tf)
  return tf.slice(operand, start_indices, size=slice_sizes_tf)


tf_impl_with_avals[lax.dynamic_slice_p] = _dynamic_slice


def _dynamic_update_slice(operand, update, *start_indices,
                          _in_avals: Sequence[core.ShapedArray],
                          _out_aval: core.ShapedArray):
  start_indices = tf.stack(start_indices)

  if _thread_local_state.enable_xla:
    return tfxla.dynamic_update_slice(operand, update, start_indices)

  # enable_xla==False.

  op_shape = _eval_shape(_in_avals[0].shape)
  op_size = tf.size(operand)
  update_shape_tf = _eval_shape(_in_avals[1].shape)

  start_indices = _clip(op_shape, start_indices, update_shape_tf)
  end_indices = tf.add(start_indices, update_shape_tf)
  flatten = tf.keras.backend.flatten

  # Get the cells to update in `operand` as an array of ids.
  id_tensor = tf.reshape(tf.range(op_size), op_shape)
  scattered_indices = tf.strided_slice(id_tensor, start_indices, end_indices)

  # Create an array containing updates at scattered_indices and zeros otherwise.
  flat_indices = tf.expand_dims(flatten(scattered_indices), -1)
  flat_update = flatten(update)
  update = tf.scatter_nd(flat_indices, flat_update, (op_size,))
  update = tf.reshape(update, op_shape)

  # Create a bool mask that is True only where `operand` should be updated.
  update_mask = tf.ones_like(flat_update, dtype=tf.bool)
  update_mask = tf.scatter_nd(flat_indices, update_mask, (op_size,))
  update_mask = tf.reshape(update_mask, op_shape)

  # Use the mask to only update `operand` with `update`.
  return tf.where(update_mask, update, operand)


tf_impl_with_avals[lax.dynamic_update_slice_p] = _dynamic_update_slice


def _scatter_dimensions_proto(indices_shape, dimension_numbers):
  proto = xla_data_pb2.ScatterDimensionNumbers()
  proto.update_window_dims.extend(dimension_numbers.update_window_dims)
  proto.inserted_window_dims.extend(dimension_numbers.inserted_window_dims)
  proto.scatter_dims_to_operand_dims.extend(
      dimension_numbers.scatter_dims_to_operand_dims)
  assert indices_shape
  proto.index_vector_dim = len(indices_shape) - 1
  return proto


def _scatter(operand, scatter_indices, updates, *, update_jaxpr, update_consts,
             dimension_numbers, indices_are_sorted, unique_indices, mode,
             _in_avals: Sequence[core.ShapedArray],
             _out_aval: core.ShapedArray):
  del unique_indices, _in_avals

  if mode == lax.GatherScatterMode.CLIP:
    raise NotImplementedError("CLIP scatter mode not implemented in jax2tf")

  assert len(update_consts) == 0, "Update computation cannot have constants"

  if not _thread_local_state.enable_xla:
    raise _xla_disabled_error("scatter")

  proto = _scatter_dimensions_proto(scatter_indices.shape, dimension_numbers)

  def update_computation(arg1: TfVal, arg2: TfVal) -> TfVal:
    closed_jaxpr = core.ClosedJaxpr(update_jaxpr, update_consts)
    res, = _interpret_jaxpr(closed_jaxpr, arg1, arg2, extra_name_stack=None)
    return res

  o_spec = tf.TensorSpec((), dtype=operand.dtype)
  xla_update_computation = (
      tf.function(update_computation,
                  autograph=False).get_concrete_function(o_spec, o_spec))
  out = tfxla.scatter(
      operand,
      scatter_indices,
      updates,
      xla_update_computation,
      proto,
      indices_are_sorted=indices_are_sorted)
  return out


tf_impl_with_avals[lax.scatter_p] = _scatter
tf_impl_with_avals[lax.scatter_min_p] = _scatter
tf_impl_with_avals[lax.scatter_max_p] = _scatter
tf_impl_with_avals[lax.scatter_mul_p] = _scatter
tf_impl_with_avals[lax.scatter_add_p] = _scatter


def _cond(index: TfVal, *operands: TfVal, branches: Sequence[core.ClosedJaxpr],
          linear: Sequence[bool]) -> Sequence[TfVal]:
  del linear
  # tf.cond needs lambdas with no arguments.
  branches_tf = [
      partial(_interpret_jaxpr, jaxpr, *operands,
              # Same name stack as the XLA translation of cond_p
              extra_name_stack=f"branch_{i}_fun")
      for jaxpr in branches
      for i, jaxpr in enumerate(branches)
  ]
  return tf.switch_case(index, branches_tf)


tf_impl[lax_control_flow.cond_p] = _cond


def _while(*args: TfVal, cond_nconsts: int, cond_jaxpr: core.ClosedJaxpr,
           body_nconsts: int, body_jaxpr: core.ClosedJaxpr) -> Sequence[TfVal]:
  cond_consts, body_consts, init_carry = util.split_list(
      args, [cond_nconsts, body_nconsts])
  if cond_jaxpr.out_avals[0].shape:  # type: ignore[attr-defined]
    # The conditional is not a scalar, this must be a batched while
    return _batched_cond_while(
        *args,
        cond_nconsts=cond_nconsts,
        cond_jaxpr=cond_jaxpr,
        body_nconsts=body_nconsts,
        body_jaxpr=body_jaxpr)

  # The conditional must return a single value to TF
  def cond_tf_func(*args: TfVal) -> TfVal:
    pred, = _interpret_jaxpr(cond_jaxpr, *cond_consts, *args,
                             # Same name stack as the XLA translation of while_p
                             extra_name_stack="while/cond")
    return pred

  body_tf_func = partial(_interpret_jaxpr, body_jaxpr, *body_consts,
                                   extra_name_stack="while/body")
  return tf.while_loop(cond_tf_func, body_tf_func, init_carry)


def _batched_cond_while(*args: TfVal, cond_nconsts: int,
                        cond_jaxpr: core.ClosedJaxpr, body_nconsts: int,
                        body_jaxpr: core.ClosedJaxpr) -> Sequence[TfVal]:
  """Interprets a while_loop with a batched condition.

  A batched while has a conditional that returns a tensor of booleans, and
  a body that returns a list of tensors whose leading dimensions match those
  of the conditional tensor.

  We need to turn it into a while with scalar boolean conditional. We will
  expand the loop carry to include a prefix with the current tensor boolean
  condition. We prepend to the loop the first calculation of the tensor boolean
  condition. The loop condition will use a "reduce_any" to calculate a scalar
  boolean from the tensor boolean condition. The end of the loop body will
  compute the new carry using a "tf.where", and we compute the new tensor
  boolean condition.
  """
  cond_consts, body_consts, init_carry = util.split_list(
      args, [cond_nconsts, body_nconsts])
  # Initial computation of batched condition
  init_pred_b, = _interpret_jaxpr(cond_jaxpr, *cond_consts, *init_carry,
                                  extra_name_stack="while/body_pred")
  assert init_pred_b is not core.unit

  def new_cond_tf_func(pred_b: TfVal, *carry: TfVal) -> TfVal:
    pred = tf.reduce_any(pred_b, axis=list(range(len(pred_b.shape))))
    return pred

  def new_body_tf_func(pred_b: TfVal, *carry: TfVal) -> Sequence[TfVal]:
    new_carry: Sequence[TfVal] = _interpret_jaxpr(body_jaxpr, *body_consts,
                                                  *carry,
                                                  extra_name_stack="while/body")
    # We repeat those carries for which the loop termination condition is false
    def select_one_carry(new_c: TfVal, c: TfVal, c_aval: core.ShapedArray) -> TfVal:
      pred_b_bcast = _broadcast_in_dim(
          pred_b,
          shape=c_aval.shape,  # a JAX shape
          broadcast_dimensions=list(range(len(pred_b.shape))),
          _in_avals=cond_jaxpr.out_avals,
          _out_aval=core.ShapedArray(c_aval.shape, np.bool_))
      return tf.where(pred_b_bcast, new_c, c)

    selected_carry: Sequence[TfVal] = list(map(select_one_carry, new_carry, carry, body_jaxpr.out_avals))
    next_pred_b, = _interpret_jaxpr(cond_jaxpr, *cond_consts, *selected_carry,
                                    extra_name_stack="body_pred")
    return (next_pred_b, *selected_carry)

  _, *res_carry = tf.while_loop(new_cond_tf_func, new_body_tf_func,
                                (init_pred_b, *init_carry))
  return res_carry


tf_impl[lax_control_flow.while_p] = _while

# We use the scan impl rule to rewrite in terms of while.
tf_impl_with_avals[lax_control_flow.scan_p] = _convert_jax_impl(
    lax_control_flow._scan_impl,
    extra_name_stack="scan")


def _top_k(operand: TfVal, k: int) -> Tuple[TfVal, TfVal]:
  # Some types originally incompatible with tf.math.top_k can be promoted
  # to a compatible type without loss of precision.
  def promote_tf_dtype(tf_dtype):
    if tf_dtype in [tf.bool, tf.uint8, tf.uint16]:
      return tf.uint32
    if tf_dtype in [tf.int8, tf.int16]:
      return tf.int32
    if tf_dtype is tf.float16:
      return tf.float32
    return None

  conversion_dtype = promote_tf_dtype(operand.dtype)
  if conversion_dtype:
    values, indices = tf.math.top_k(
        tf.dtypes.cast(operand, conversion_dtype), k=k, sorted=True)
    return tf.dtypes.cast(values, operand.dtype), indices
  else:
    return tf.math.top_k(operand, k=k, sorted=True)


tf_impl[lax.top_k_p] = _top_k


def _sort(*operands: TfVal, dimension: int, is_stable: bool,
          num_keys: int) -> Tuple[TfVal, ...]:
  if not _thread_local_state.enable_xla:
    raise _xla_disabled_error("sort")
  assert 1 <= num_keys <= len(operands)
  assert 0 <= dimension < len(
      operands[0].shape
  ), f"Invalid {dimension} for ndim {len(operands[0].shape)}"

  comparator_spec: List[tf.TensorSpec] = []
  comparator_jax_in_avals: List[core.ShapedArray] = []
  for op in operands:
    o_spec = tf.TensorSpec((), dtype=op.dtype)
    comparator_spec.extend([o_spec, o_spec])
    o_aval = core.ShapedArray((), _to_jax_dtype(op.dtype))
    comparator_jax_in_avals.extend([o_aval, o_aval])

  # Use the same comparator that JAX uses when compiling to XLA, to get the
  # proper NaN/Inf total order, and the lexicographic ordering.
  # The comparator is a 2N-argument TF function, with arguments [2k] and [2k +1]
  # corresponding to two scalars from operand[k].
  def lexicographic_comparator(*tf_args: TfVal) -> TfVal:
    return _convert_jax_impl(
        lax._sort_lt_comparator, multiple_results=False)(
            *tf_args,
            _in_avals=comparator_jax_in_avals,
            _out_aval=core.ShapedArray((), np.bool_),
            num_keys=num_keys)

  xla_comparator_computation = (
      tf.function(lexicographic_comparator,
                  autograph=False).get_concrete_function(*comparator_spec))
  results = tfxla.variadic_sort(
      operands,
      dimension=dimension,
      is_stable=is_stable,
      comparator=xla_comparator_computation)
  return results


tf_impl[lax.sort_p] = _sort


def _fft(x, fft_type, fft_lengths):
  FFT, IFFT, RFFT, IRFFT = list(map(xla_client.FftType, [0, 1, 2, 3]))
  if fft_type == IRFFT:
    expected_lengths = x.shape[-len(fft_lengths):-1] + ((x.shape[-1] - 1) * 2,)
  else:
    expected_lengths = x.shape[-len(fft_lengths):]
  if expected_lengths != fft_lengths:
    raise NotImplementedError(
        f"Unsupported fft_lengths={fft_lengths} for fft_type={fft_type} of "
        f"array with shape={x.shape}.")
  tf_funcs = {
      FFT: [tf.signal.fft, tf.signal.fft2d, tf.signal.fft3d],
      IFFT: [tf.signal.ifft, tf.signal.ifft2d, tf.signal.ifft3d],
      RFFT: [tf.signal.rfft, tf.signal.rfft2d, tf.signal.rfft3d],
      IRFFT: [tf.signal.irfft, tf.signal.irfft2d, tf.signal.irfft3d]
  }
  return tf_funcs[fft_type][len(fft_lengths) - 1](x)


tf_impl[lax_fft.fft_p] = _fft


def _qr(operand, full_matrices):
  return tf.linalg.qr(operand, full_matrices=full_matrices)


tf_impl[lax_linalg.qr_p] = _qr


def _svd(operand, full_matrices, compute_uv):
  result = tf.linalg.svd(operand, full_matrices, compute_uv)
  if not compute_uv:
    return result,
  s, u, v = result
  return s, u, tf.linalg.adjoint(v)


tf_impl[lax_linalg.svd_p] = _svd


def _eig(operand: TfVal, compute_left_eigenvectors: bool,
         compute_right_eigenvectors: bool):
  if compute_left_eigenvectors and compute_right_eigenvectors:
    # TODO(bchetioui): didn't find a 100% reliable, easy and satisfying way to
    # sort the left eigenvectors in the right order. The jax.numpy.linalg API
    # suggests to me that left eigenvectors are anyway seldom used, so I
    # think it is acceptable to leave as unimplemented for now.
    msg = ("Conversion of eig is not implemented when both "
           "compute_left_eigenvectors and compute_right_eigenvectors are set "
           "to True.")
    raise NotImplementedError(msg)
  elif not (compute_left_eigenvectors or compute_right_eigenvectors):
    return tuple([tf.linalg.eigvals(operand)])
  elif compute_right_eigenvectors:
    return tuple(tf.linalg.eig(operand))
  else:  # compute_left_eigenvectors == True
    wH, vl = tf.linalg.eig(tf.linalg.adjoint(operand))
    wHH = tf.math.conj(wH)
    return tuple([wHH, vl])


tf_impl[lax_linalg.eig_p] = _eig


def _eigh(operand: TfVal, lower: bool, _in_avals, _out_aval):
  if operand.shape[-1] == 0:
    v, w = operand, tf.reshape(operand, _eval_shape(_in_avals[0].shape[:-1]))
  else:
    if not lower:
      operand = tf.linalg.adjoint(operand)
    w, v = tf.linalg.eigh(operand)
  cast_type = {
      tf.complex64: tf.float32,
      tf.complex128: tf.float64
  }.get(operand.dtype)
  if cast_type is not None:
    w = tf.cast(w, cast_type)
  return v, w


tf_impl_with_avals[lax_linalg.eigh_p] = _eigh


def _lu(operand: TfVal, _in_avals, _out_aval):
  return _convert_jax_impl(lax_linalg._lu_python, extra_name_stack="lu")(
      operand, _in_avals=_in_avals, _out_aval=_out_aval)


tf_impl_with_avals[lax_linalg.lu_p] = _lu


def _triangular_solve(a: TfVal, b: TfVal, *, left_side: bool, lower: bool,
                      transpose_a: bool, conjugate_a: bool, unit_diagonal: bool,
                      _in_avals: Sequence[core.ShapedArray],
                      _out_aval: core.ShapedArray):
  if unit_diagonal:
    a_aval, _ = _in_avals
    a_shape = _eval_shape(a_aval.shape)
    a = tf.linalg.set_diag(a, tf.ones(a_shape[:-1], dtype=a.dtype))
  if not left_side:
    rank = len(a.shape)
    transpose_dimensions = list(range(rank - 2)) + [rank - 1, rank - 2]
    a = tf.transpose(a, transpose_dimensions)
    b = tf.transpose(b, transpose_dimensions)
    lower = not lower
  # adjoint == transpose for real dtypes, so special care need only be taken
  # for complex types.
  if a.dtype in [tf.complex64, tf.complex128]:
    if (transpose_a and not conjugate_a) or (not transpose_a and conjugate_a):
      a = tf.math.conj(a)
  result = tf.linalg.triangular_solve(a, b, lower=lower, adjoint=transpose_a)
  if not left_side:
    result = tf.transpose(result, transpose_dimensions)
  return result


tf_impl_with_avals[lax_linalg.triangular_solve_p] = _triangular_solve


def _linear_solve(*args: TfVal, const_lengths, jaxprs, _in_avals, _out_aval):
  return _convert_jax_impl(lax_control_flow._custom_linear_solve_impl,
                           extra_name_stack="linear_solve")(
      *args,
      const_lengths=const_lengths,
      jaxprs=jaxprs,
      _in_avals=_in_avals,
      _out_aval=_out_aval)


tf_impl_with_avals[lax_control_flow.linear_solve_p] = _linear_solve

def _tridiagonal_solve(*args: TfVal, _in_avals, _out_aval, **params):
  return _convert_jax_impl(lax_linalg._tridiagonal_solve_jax,
                           multiple_results=False,
                           extra_name_stack="tridiagonal_solve")(
      *args,
      _in_avals=_in_avals,
      _out_aval=_out_aval)


tf_impl_with_avals[lax_linalg.tridiagonal_solve_p] = _tridiagonal_solve

def _custom_jvp_call_jaxpr(*args: TfVal, fun_jaxpr: core.ClosedJaxpr,
                           jvp_jaxpr_thunk: Callable,
                           num_consts: int) -> Sequence[TfVal]:
  # TODO(necula): ensure that there is no AD transformation in scope
  return _interpret_jaxpr(fun_jaxpr, *args, extra_name_stack="custom_jvp")


tf_impl[custom_derivatives.custom_jvp_call_jaxpr_p] = _custom_jvp_call_jaxpr


def _custom_vjp_call_jaxpr(*args: TfVal, fun_jaxpr: core.ClosedJaxpr,
                           **_) -> Sequence[TfVal]:
  # TODO(necula): ensure that there is no AD transformation in scope
  return _interpret_jaxpr(fun_jaxpr, *args, extra_name_stack="custom_vjp")


tf_impl[custom_derivatives.custom_vjp_call_jaxpr_p] = _custom_vjp_call_jaxpr


def _custom_lin(*args: TfVal, **_) -> Sequence[TfVal]:
  raise TypeError("can't apply forward-mode autodiff (jvp) to a custom_vjp "
                  "function.")


tf_impl[ad.custom_lin_p] = _custom_lin


def split_to_logical_devices(tensor: TfVal,
                             partition_dimensions: pxla.PartitionsOrReplicated):
  """Like TPUMPStrategy.experimental_split_to_logical_devices.

  For jax2tf purposes we want to avoid needing to thread the `strategy` object
  through the generated computation. It seems that the original function needs
  the strategy object only for error checking, which we assume is done upstream
  by JAX.

  Args:
    tensor: Input tensor to annotate.
    partition_dimensions: A list of integers, with one integer per tensor
      dimension, specifying in how many parts the dimension should be split. The
      product of integers must equal the number of devices per replica.
    use_sharding_op: whether to use a sharding op, or not.

  Returns:
    an annotated tensor.
  """
  # TODO: this is only for sharded_jit. Either remove, or implement in terms
  # of _shard_values.
  if partition_dimensions is None:
    return xla_sharding.replicate(tensor, use_sharding_op=True)
  num_partition_splits = np.prod(partition_dimensions)
  tile_assignment = np.arange(num_partition_splits).reshape(
      partition_dimensions)
  return xla_sharding.tile(tensor, tile_assignment, use_sharding_op=True)


def _shard_value(mesh: maps.Mesh,
                 val: TfVal,
                 aval: core.ShapedArray,
                 axis_resources: pjit.ParsedPartitionSpec) -> TfVal:
  """Apply sharding to a TfVal."""
  sharding_proto: xla_client.OpSharding = pjit.get_aval_sharding_proto(
      aval, axis_resources, mesh)
  # To use xla_sharding.py, we must have a xla_data_pb2.OpSharding.
  xla_sharding_proto: xla_data_pb2.OpSharding = (
      xla_data_pb2.OpSharding(
          type=int(sharding_proto.type),
          tile_assignment_dimensions=sharding_proto.tile_assignment_dimensions,
          tile_assignment_devices=sharding_proto.tile_assignment_devices,
          replicate_on_last_tile_dim=sharding_proto.replicate_on_last_tile_dim))
  return xla_sharding.Sharding(proto=xla_sharding_proto).apply_to_tensor(
      val, use_sharding_op=True)


def _sharded_call(f: lu.WrappedFun, vals: Sequence[TfVal],
                  in_parts: Sequence[pxla.PartitionsOrReplicated],
                  out_parts_thunk,
                  **_) -> Sequence[Tuple[TfVal, core.ShapedArray]]:
  sharded_vals = map(split_to_logical_devices, vals, in_parts)
  vals_out = f.call_wrapped(*sharded_vals)  # caller handles new_sublevel
  out_parts_flat = out_parts_thunk()
  assert len(out_parts_flat) == len(
      vals_out), f"expected {len(out_parts_flat)} == {len(vals_out)}"
  sharded_vals_out = [
      (split_to_logical_devices(val, val_part), val_aval)
      for (val, val_aval), val_part in zip(vals_out, out_parts_flat)
  ]
  return sharded_vals_out


def _sharded_jit_sharding_constraint(arg: TfVal, *,
                                     partitions: pxla.PartitionsOrReplicated,
                                     _in_avals: Sequence[core.ShapedArray],
                                     _out_aval: core.ShapedArray):
  del _in_avals, _out_aval
  return split_to_logical_devices(arg, partitions)


tf_impl_with_avals[sharded_jit.sharding_constraint_p] = _sharded_jit_sharding_constraint


def _pjit(*args: TfVal,
          jaxpr: core.ClosedJaxpr,
          in_axis_resources: Sequence[pjit.ParsedPartitionSpec],
          out_axis_resources: Sequence[pjit.ParsedPartitionSpec],
          resource_env: maps.ResourceEnv,
          donated_invars,
          name: str,
          positional_semantics,
          _in_avals: Sequence[core.ShapedArray],
          _out_aval: core.ShapedArray) -> TfVal:
  del donated_invars
  if resource_env.physical_mesh.is_multi_process:
    raise NotImplementedError("jax2tf translation for pjit over multi-process "
                              "meshes is not supported yet")
  # TODO: add `name` to the name stack
  shard_value_for_mesh = partial(_shard_value, resource_env.physical_mesh)
  # Apply sharding annotation to the arguments
  sharded_args: Sequence[TfVal] = tuple(
      map(shard_value_for_mesh, args, _in_avals, in_axis_resources))
  results = _interpret_jaxpr(jaxpr, *sharded_args,
                             extra_name_stack=util.wrap_name(name, "pjit"))
  sharded_results: Sequence[TfVal] = tuple(
      map(shard_value_for_mesh, results, _out_aval, out_axis_resources))
  return tuple(sharded_results)


tf_impl_with_avals[pjit.pjit_p] = _pjit


def _pjit_sharding_constraint(arg: TfVal, *,
                              axis_resources: pjit.ParsedPartitionSpec,
                              resource_env: maps.ResourceEnv,
                              _in_avals: Sequence[core.ShapedArray],
                              _out_aval: core.ShapedArray,
                              **kwargs) -> TfVal:
  return _shard_value(resource_env.physical_mesh, arg, _in_avals[0], axis_resources)


tf_impl_with_avals[pjit.sharding_constraint_p] = _pjit_sharding_constraint


def _register_checkpoint_pytrees():
  """Registers TF custom container types as pytrees."""
  m = tf.Module()
  # The types here are automagically changed by TensorFlow's checkpointing
  # infrastructure.
  m.a = (tf.Module(), tf.Module())
  m.b = [tf.Module(), tf.Module()]
  m.c = {"a": tf.Module()}
  tuple_wrapper = type(m.a)
  list_wrapper = type(m.b)
  dict_wrapper = type(m.c)

  # TF AutoTrackable swaps container types out for wrappers.
  assert tuple_wrapper is not tuple
  assert list_wrapper is not list
  assert dict_wrapper is not dict

  jax.tree_util.register_pytree_node(tuple_wrapper, lambda xs:
                                     (tuple(xs), None), lambda _, xs: tuple(xs))

  jax.tree_util.register_pytree_node(list_wrapper, lambda xs: (tuple(xs), None),
                                     lambda _, xs: list(xs))

  jax.tree_util.register_pytree_node(
      dict_wrapper,
      lambda s: (tuple(s.values()), tuple(s.keys())),
      lambda k, xs: dict(zip(k, xs)))


_register_checkpoint_pytrees()

shape_poly._register_conversion_rules()
