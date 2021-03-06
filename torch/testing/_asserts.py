import collections.abc
import functools
import numbers
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple, Type, Union, cast
from types import SimpleNamespace

import torch
from torch import Tensor

from ._core import _unravel_index

__all__ = ["assert_close"]


# The UsageError should be raised in case the test function is not used correctly. With this the user is able to
# differentiate between a test failure (there is a bug in the tested code) and a test error (there is a bug in the
# test).
class UsageError(Exception):
    pass


_TestingError = Union[AssertionError, UsageError]


class _TestingErrorMeta(NamedTuple):
    type: Type[_TestingError]
    msg: str

    def amend_msg(self, prefix: str = "", postfix: str = "") -> "_TestingErrorMeta":
        return self._replace(msg=f"{prefix}{self.msg}{postfix}")

    def to_error(self) -> _TestingError:
        return self.type(self.msg)


# This is copy-pasted from torch.testing._internal.common_utils.TestCase.dtype_precisions. With this we avoid a
# dependency on torch.testing._internal at import. See
# https://github.com/pytorch/pytorch/pull/54769#issuecomment-813174256 for details.
# {dtype: (rtol, atol)}
_DTYPE_PRECISIONS = {
    torch.float16: (0.001, 1e-5),
    torch.bfloat16: (0.016, 1e-5),
    torch.float32: (1.3e-6, 1e-5),
    torch.float64: (1e-7, 1e-7),
    torch.complex32: (0.001, 1e-5),
    torch.complex64: (1.3e-6, 1e-5),
    torch.complex128: (1e-7, 1e-7),
}


def _get_default_rtol_and_atol(actual: Tensor, expected: Tensor) -> Tuple[float, float]:
    dtype = actual.dtype if actual.dtype == expected.dtype else torch.promote_types(actual.dtype, expected.dtype)
    return _DTYPE_PRECISIONS.get(dtype, (0.0, 0.0))


def _check_complex_components_individually(
    check_tensors: Callable[..., Optional[_TestingErrorMeta]]
) -> Callable[..., Optional[_TestingErrorMeta]]:
    """Decorates real-valued tensor check functions to handle complex components individually.

    If the inputs are not complex, this decorator is a no-op.

    Args:
        check_tensors (Callable[[Tensor, Tensor], Optional[_TestingErrorMeta]]): Tensor check function for real-valued
        tensors.
    """

    @functools.wraps(check_tensors)
    def wrapper(
        actual: Tensor, expected: Tensor, *, equal_nan: Union[str, bool], **kwargs: Any
    ) -> Optional[_TestingErrorMeta]:
        if equal_nan == "relaxed":
            relaxed_complex_nan = True
            equal_nan = True
        else:
            relaxed_complex_nan = False

        if actual.dtype not in (torch.complex32, torch.complex64, torch.complex128):
            return check_tensors(actual, expected, equal_nan=equal_nan, **kwargs)
        if relaxed_complex_nan:
            actual, expected = [
                t.clone().masked_fill(
                    t.real.isnan() | t.imag.isnan(), complex(float("NaN"), float("NaN"))  # type: ignore[call-overload]
                )
                for t in (actual, expected)
            ]

        error_meta = check_tensors(actual.real, expected.real, equal_nan=equal_nan, **kwargs)
        if error_meta:
            return error_meta.amend_msg(postfix="\n\nThe failure occurred for the real part.")

        error_meta = check_tensors(actual.imag, expected.imag, equal_nan=equal_nan, **kwargs)
        if error_meta:
            return error_meta.amend_msg(postfix="\n\nThe failure occurred for the imaginary part.")

        return None

    return wrapper


def _check_supported_tensor(input: Tensor) -> Optional[_TestingErrorMeta]:
    """Checks if the tensors are supported by the current infrastructure.

    All checks are temporary and will be relaxed in the future.

    Returns:
        (Optional[_TestingErrorMeta]): If check did not pass.
    """
    if input.is_quantized:
        return _TestingErrorMeta(UsageError, "Comparison for quantized tensors is not supported yet.")
    if input.is_sparse:
        return _TestingErrorMeta(UsageError, "Comparison for sparse tensors is not supported yet.")

    return None


def _check_attributes_equal(
    actual: Tensor,
    expected: Tensor,
    *,
    check_device: bool = True,
    check_dtype: bool = True,
    check_stride: bool = True,
) -> Optional[_TestingErrorMeta]:
    """Checks if the attributes of two tensors match.

    Always checks the :attr:`~torch.Tensor.shape`. Checks for :attr:`~torch.Tensor.device`,
    :attr:`~torch.Tensor.dtype`, and :meth:`~torch.Tensor.stride` are optional and can be disabled.

    Args:
        actual (Tensor): Actual tensor.
        expected (Tensor): Expected tensor.
        check_device (bool): If ``True`` (default), checks that both :attr:`actual` and :attr:`expected` are on the
            same :attr:`~torch.Tensor.device`.
        check_dtype (bool): If ``True`` (default), checks that both :attr:`actual` and :attr:`expected` have the same
            ``dtype``.
        check_stride (bool): If ``True`` (default), checks that both :attr:`actual` and :attr:`expected` have the same
            stride.

    Returns:
        (Optional[_TestingErrorMeta]): If checks did not pass.
    """
    msg_fmtstr = "The values for attribute '{}' do not match: {} != {}."

    if actual.shape != expected.shape:
        return _TestingErrorMeta(AssertionError, msg_fmtstr.format("shape", actual.shape, expected.shape))

    if check_device and actual.device != expected.device:
        return _TestingErrorMeta(AssertionError, msg_fmtstr.format("device", actual.device, expected.device))

    if check_dtype and actual.dtype != expected.dtype:
        return _TestingErrorMeta(AssertionError, msg_fmtstr.format("dtype", actual.dtype, expected.dtype))

    if check_stride and actual.stride() != expected.stride():
        return _TestingErrorMeta(AssertionError, msg_fmtstr.format("stride()", actual.stride(), expected.stride()))

    return None


def _equalize_attributes(actual: Tensor, expected: Tensor) -> Tuple[Tensor, Tensor]:
    """Equalizes some attributes of two tensors for value comparison.

    If :attr:`actual` and :attr:`expected`
    - are not on the same :attr:`~torch.Tensor.device`, they are moved CPU memory, and
    - do not have the same ``dtype``, they are promoted  to a common ``dtype`` (according to
        :func:`torch.promote_types`)

    Args:
        actual (Tensor): Actual tensor.
        expected (Tensor): Expected tensor.

    Returns:
        Tuple(Tensor, Tensor): Equalized tensors.
    """
    if actual.device != expected.device:
        actual = actual.cpu()
        expected = expected.cpu()

    if actual.dtype != expected.dtype:
        dtype = torch.promote_types(actual.dtype, expected.dtype)
        actual = actual.to(dtype)
        expected = expected.to(dtype)

    return actual, expected


DiagnosticInfo = SimpleNamespace


def _trace_mismatches(actual: Tensor, expected: Tensor, mismatches: Tensor) -> DiagnosticInfo:
    """Traces mismatches and returns diagnostic information.

    Args:
        actual (Tensor): Actual tensor.
        expected (Tensor): Expected tensor.
        mismatches (Tensor): Boolean mask of the same shape as :attr:`actual` and :attr:`expected` that indicates
            the location of mismatches.

    Returns:
        (DiagnosticInfo): Mismatch diagnostics with the following attributes:

            - ``number_of_elements`` (int): Number of elements in each tensor being compared.
            - ``total_mismatches`` (int): Total number of mismatches.
            - ``mismatch_ratio`` (float): Total mismatches divided by number of elements.
            - ``max_abs_diff`` (Union[int, float]): Greatest absolute difference of the inputs.
            - ``max_abs_diff_idx`` (Union[int, Tuple[int, ...]]): Index of greatest absolute difference.
            - ``max_rel_diff`` (Union[int, float]): Greatest relative difference of the inputs.
            - ``max_rel_diff_idx`` (Union[int, Tuple[int, ...]]): Index of greatest relative difference.

            For ``max_abs_diff`` and ``max_rel_diff`` the type depends on the :attr:`~torch.Tensor.dtype` of the inputs.
    """
    number_of_elements = mismatches.numel()
    total_mismatches = torch.sum(mismatches).item()
    mismatch_ratio = total_mismatches / number_of_elements

    dtype = torch.float64 if actual.dtype.is_floating_point else torch.int64
    a_flat = actual.flatten().to(dtype)
    b_flat = expected.flatten().to(dtype)
    matches_flat = ~mismatches.flatten()

    abs_diff = torch.abs(a_flat - b_flat)
    # Ensure that only mismatches are used for the max_abs_diff computation
    abs_diff[matches_flat] = 0
    max_abs_diff, max_abs_diff_flat_idx = torch.max(abs_diff, 0)

    rel_diff = abs_diff / torch.abs(b_flat)
    # Ensure that only mismatches are used for the max_rel_diff computation
    rel_diff[matches_flat] = 0
    max_rel_diff, max_rel_diff_flat_idx = torch.max(rel_diff, 0)

    return SimpleNamespace(
        number_of_elements=number_of_elements,
        total_mismatches=cast(int, total_mismatches),
        mismatch_ratio=mismatch_ratio,
        max_abs_diff=max_abs_diff.item(),
        max_abs_diff_idx=_unravel_index(max_abs_diff_flat_idx.item(), mismatches.shape),
        max_rel_diff=max_rel_diff.item(),
        max_rel_diff_idx=_unravel_index(max_rel_diff_flat_idx.item(), mismatches.shape),
    )


@_check_complex_components_individually
def _check_values_close(
    actual: Tensor,
    expected: Tensor,
    *,
    rtol: float,
    atol: float,
    equal_nan: bool,
    msg: Optional[Union[str, Callable[[Tensor, Tensor, SimpleNamespace], str]]],
) -> Optional[_TestingErrorMeta]:
    """Checks if the values of two tensors are close up to a desired tolerance.

    Args:
        actual (Tensor): Actual tensor.
        expected (Tensor): Expected tensor.
        rtol (float): Relative tolerance.
        atol (float): Absolute tolerance.
        equal_nan (bool): If ``True``, two ``NaN`` values will be considered equal.
        msg (Optional[Union[str, Callable[[Tensor, Tensor, SimpleNamespace], str]]]): Optional error message. Can be
            passed as callable in which case it will be called with the inputs and the result of
            :func:`_trace_mismatches`.

    Returns:
        (Optional[AssertionError]): If check did not pass.
    """

    mismatches = ~torch.isclose(actual, expected, rtol=rtol, atol=atol, equal_nan=equal_nan)
    if not torch.any(mismatches):
        return None

    trace = _trace_mismatches(actual, expected, mismatches)
    if msg is None:
        msg = (
            f"Tensors are not close!\n\n"
            f"Mismatched elements: {trace.total_mismatches} / {trace.number_of_elements} ({trace.mismatch_ratio:.1%})\n"
            f"Greatest absolute difference: {trace.max_abs_diff} at {trace.max_abs_diff_idx} (up to {atol} allowed)\n"
            f"Greatest relative difference: {trace.max_rel_diff} at {trace.max_rel_diff_idx} (up to {rtol} allowed)"
        )
    elif callable(msg):
        msg = msg(actual, expected, trace)
    return _TestingErrorMeta(AssertionError, msg)


def _check_tensors_close(
    actual: Tensor,
    expected: Tensor,
    *,
    rtol: Optional[float] = None,
    atol: Optional[float] = None,
    equal_nan: bool = False,
    check_device: bool = True,
    check_dtype: bool = True,
    check_stride: bool = True,
    msg: Optional[Union[str, Callable[[Tensor, Tensor, SimpleNamespace], str]]] = None,
) -> Optional[_TestingErrorMeta]:
    r"""Checks that the values of :attr:`actual` and :attr:`expected` are close.

    If :attr:`actual` and :attr:`expected` are real-valued and finite, they are considered close if

    .. code::

        torch.abs(actual - expected) <= (atol + rtol * expected)

    and they have the same device (if :attr:`check_device` is ``True``), same dtype (if :attr:`check_dtype` is
    ``True``), and the same stride (if :attr:`check_stride` is ``True``). Non-finite values (``-inf`` and ``inf``) are
    only considered close if and only if they are equal. ``NaN``'s are only considered equal to each other if
    :attr:`equal_nan` is ``True``.

    For a description of the parameters see :func:`assert_equal`.

    Returns:
        Optional[_TestingErrorMeta]: If checks did not pass.
    """
    if (rtol is None) ^ (atol is None):
        # We require both tolerance to be omitted or specified, because specifying only one might lead to surprising
        # results. Imagine setting atol=0.0 and the tensors still match because rtol>0.0.
        return _TestingErrorMeta(
            UsageError,
            f"Both 'rtol' and 'atol' must be either specified or omitted, but got rtol={rtol} and atol={atol} instead.",
        )
    elif rtol is None or atol is None:
        rtol, atol = _get_default_rtol_and_atol(actual, expected)

    error_meta = _check_attributes_equal(
        actual, expected, check_device=check_device, check_dtype=check_dtype, check_stride=check_stride
    )
    if error_meta:
        return error_meta
    actual, expected = _equalize_attributes(actual, expected)

    error_meta = _check_values_close(actual, expected, rtol=rtol, atol=atol, equal_nan=equal_nan, msg=msg)
    if error_meta:
        return error_meta

    return None


class _TensorPair(NamedTuple):
    actual: Tensor
    expected: Tensor


_SEQUENCE_MSG_FMTSTR = "The failure occurred at index {} of the sequences."
_MAPPING_MSG_FMTSTR = "The failure occurred for key '{}' of the mappings."


def _check_pair_close(
    pair: Union[_TensorPair, List, Dict],
    **kwargs: Any,
) -> Optional[_TestingErrorMeta]:
    """Checks input pairs.

    :class:`list`'s or :class:`dict`'s are checked elementwise. Checking is performed recursively and thus nested
    containers are supported.

    Args:
        pair (Union[_TensorPair, List, Dict]): Input pair.
        **kwargs (Any): Keyword arguments passed to :func:`__check_tensors_close`.

    Returns:
        (Optional[_TestingErrorMeta]): Return value of :attr:`check_tensors`.
    """
    if isinstance(pair, list):
        for idx, pair_item in enumerate(pair):
            error_meta = _check_pair_close(pair_item, **kwargs)
            if error_meta:
                return error_meta.amend_msg(postfix=f"\n\n{_SEQUENCE_MSG_FMTSTR.format(idx)}")
        else:
            return None
    elif isinstance(pair, dict):
        for key, pair_item in pair.items():
            error_meta = _check_pair_close(pair_item, **kwargs)
            if error_meta:
                return error_meta.amend_msg(postfix=f"\n\n{_MAPPING_MSG_FMTSTR.format(key)}")
        else:
            return None
    else:  # isinstance(pair, TensorPair)
        return _check_tensors_close(pair.actual, pair.expected, **kwargs)


def _to_tensor(array_or_scalar_like: Any) -> Tuple[Optional[_TestingErrorMeta], Optional[Tensor]]:
    """Converts a scalar-or-array-like to a :class:`~torch.Tensor`.
    Args:
        array_or_scalar_like (Any): Scalar-or-array-like.
    Returns:

        (Tuple[Optional[_TestingErrorMeta], Optional[Tensor]]): The two elements are orthogonal, i.e. if the first is
            ``None`` the second will be valid and vice versa. Returns :class:`_TestingErrorMeta` if no tensor can be
            constructed from :attr:`actual` or :attr:`expected`. Additionally, returns any error meta from
            :func:`_check_supported_tensor`.
    """
    error_meta: Optional[_TestingErrorMeta]

    if isinstance(array_or_scalar_like, Tensor):
        tensor = array_or_scalar_like
    else:
        try:
            tensor = torch.as_tensor(array_or_scalar_like)
        except Exception:
            error_meta = _TestingErrorMeta(
                UsageError, f"No tensor can be constructed from type {type(array_or_scalar_like)}."
            )
            return error_meta, None

    error_meta = _check_supported_tensor(tensor)
    if error_meta:
        return error_meta, None

    return None, tensor


def _to_tensor_pair(actual: Any, expected: Any) -> Tuple[Optional[_TestingErrorMeta], Optional[_TensorPair]]:
    """Converts a scalar-or-array-like pair to a :class:`_TensorPair`.

    Args:
        actual (Any): Actual array-or-scalar-like.
        expected (Any): Expected array-or-scalar-like.

    Returns:
        (Optional[_TestingErrorMeta], Optional[_TensorPair]): The two elements are orthogonal, i.e. if the first is
            ``None`` the second will not and vice versa. Returns :class:`_TestingErrorMeta` if :attr:`actual` and
            :attr:`expected` are not scalars and do not have the same type. Additionally, returns any error meta from
            :func:`_to_tensor`.
    """
    error_meta: Optional[_TestingErrorMeta]

    # We exclude numbers here, since numbers of different type, e.g. int vs. float, should be treated the same as
    # tensors with different dtypes. Without user input, passing numbers of different types will still fail, but this
    # can be disabled by setting `check_dtype=False`.
    if type(actual) is not type(expected) and not (
        isinstance(actual, numbers.Number) and isinstance(expected, numbers.Number)
    ):
        error_meta = _TestingErrorMeta(
            AssertionError,
            f"Except for scalars, type equality is required, but got {type(actual)} and {type(expected)} instead.",
        )
        return error_meta, None

    error_meta, actual = _to_tensor(actual)
    if error_meta:
        return error_meta, None

    error_meta, expected = _to_tensor(expected)
    if error_meta:
        return error_meta, None

    return None, _TensorPair(actual, expected)


def _parse_inputs(
    actual: Any, expected: Any
) -> Tuple[Optional[_TestingErrorMeta], Optional[Union[_TensorPair, List, Dict]]]:
    """Parses the positional inputs by constructing :class:`_TensorPair`'s from corresponding array-or-scalar-likes.


    :class:`~collections.abc.Sequence`'s or :class:`~collections.abc.Mapping`'s are parsed elementwise. Parsing is
    performed recursively and thus nested containers are supported. The hierarchy of the containers is preserved, but
    sequences are returned as :class:`list` and mappings as :class:`dict`.

    Args:
        actual (Any): Actual input.
        expected (Any): Expected input.

    Returns:
        (Tuple[Optional[_TestingErrorMeta], Optional[Union[_TensorPair, List, Dict]]]): The two elements are
            orthogonal, i.e. if the first is ``None`` the second will be valid and vice versa. Returns
            :class:`_TestingErrorMeta` if the length of two sequences or the keys of two mappings do not match.
            Additionally, returns any error meta from :func:`_to_tensor_pair`.

    """
    error_meta: Optional[_TestingErrorMeta]

    # We explicitly exclude str's here since they are self-referential and would cause an infinite recursion loop:
    # "a" == "a"[0][0]...
    if (
        isinstance(actual, collections.abc.Sequence)
        and not isinstance(actual, str)
        and isinstance(expected, collections.abc.Sequence)
        and not isinstance(expected, str)
    ):
        actual_len = len(actual)
        expected_len = len(expected)
        if actual_len != expected_len:
            error_meta = _TestingErrorMeta(
                AssertionError, f"The length of the sequences mismatch: {actual_len} != {expected_len}"
            )
            return error_meta, None

        pair_list = []
        for idx in range(actual_len):
            error_meta, pair = _parse_inputs(actual[idx], expected[idx])
            if error_meta:
                error_meta = error_meta.amend_msg(postfix=f"\n\n{_SEQUENCE_MSG_FMTSTR.format(idx)}")
                return error_meta, None

            pair_list.append(pair)
        else:
            return None, pair_list

    elif isinstance(actual, collections.abc.Mapping) and isinstance(expected, collections.abc.Mapping):
        actual_keys = set(actual.keys())
        expected_keys = set(expected.keys())
        if actual_keys != expected_keys:
            missing_keys = expected_keys - actual_keys
            additional_keys = actual_keys - expected_keys
            error_meta = _TestingErrorMeta(
                AssertionError,
                f"The keys of the mappings do not match:\n"
                f"Missing keys in the actual mapping: {sorted(missing_keys)}\n"
                f"Additional keys in the actual mapping: {sorted(additional_keys)}",
            )
            return error_meta, None

        pair_dict = {}
        for key in sorted(actual_keys):
            error_meta, pair = _parse_inputs(actual[key], expected[key])
            if error_meta:
                error_meta = error_meta.amend_msg(postfix=f"\n\n{_MAPPING_MSG_FMTSTR.format(key)}")
                return error_meta, None

            pair_dict[key] = pair
        else:
            return None, pair_dict

    else:
        return _to_tensor_pair(actual, expected)


def assert_close(
    actual: Any,
    expected: Any,
    *,
    rtol: Optional[float] = None,
    atol: Optional[float] = None,
    equal_nan: Union[bool, str] = False,
    check_device: bool = True,
    check_dtype: bool = True,
    check_stride: bool = True,
    msg: Optional[Union[str, Callable[[Tensor, Tensor, SimpleNamespace], str]]] = None,
) -> None:
    r"""Asserts that :attr:`actual` and :attr:`expected` are close.

    If :attr:`actual` and :attr:`expected` are real-valued and finite, they are considered close if

    .. math::

        \lvert \text{actual} - \text{expected} \rvert \le \texttt{atol} + \texttt{rtol} \cdot \lvert \text{expected} \rvert

    and they have the same :attr:`~torch.Tensor.device` (if :attr:`check_device` is ``True``), same ``dtype`` (if
    :attr:`check_dtype` is ``True``), and the same stride (if :attr:`check_stride` is ``True``). Non-finite values
    (``-inf`` and ``inf``) are only considered close if and only if they are equal. ``NaN``'s are only considered equal
    to each other if :attr:`equal_nan` is ``True``.

    If :attr:`actual` and :attr:`expected` are complex-valued, they are considered close if both their real and
    imaginary components are considered close according to the definition above.

    :attr:`actual` and :attr:`expected` can be :class:`~torch.Tensor`'s or any array-or-scalar-like of the same type,
    from which :class:`torch.Tensor`'s can be constructed with :func:`torch.as_tensor`. In addition, :attr:`actual` and
    :attr:`expected` can be :class:`~collections.abc.Sequence`'s or :class:`~collections.abc.Mapping`'s in which case
    they are considered close if their structure matches and all their elements are considered close according to the
    above definition.

    Args:
        actual (Any): Actual input.
        expected (Any): Expected input.
        rtol (Optional[float]): Relative tolerance. If specified :attr:`atol` must also be specified. If omitted,
            default values based on the :attr:`~torch.Tensor.dtype` are selected with the below table.
        atol (Optional[float]): Absolute tolerance. If specified :attr:`rtol` must also be specified. If omitted,
            default values based on the :attr:`~torch.Tensor.dtype` are selected with the below table.
        equal_nan (Union[bool, str]): If ``True``, two ``NaN`` values will be considered equal. If ``"relaxed"``,
            complex values are considered as ``NaN`` if either the real **or** imaginary component is ``NaN``.
        check_device (bool): If ``True`` (default), asserts that corresponding tensors are on the same
            :attr:`~torch.Tensor.device`. If this check is disabled, tensors on different
            :attr:`~torch.Tensor.device`'s are moved to the CPU before being compared.
        check_dtype (bool): If ``True`` (default), asserts that corresponding tensors have the same ``dtype``. If this
            check is disabled, tensors with different ``dtype``'s are promoted  to a common ``dtype`` (according to
            :func:`torch.promote_types`) before being compared.
        check_stride (bool): If ``True`` (default), asserts that corresponding tensors have the same stride.
        msg (Optional[Union[str, Callable[[Tensor, Tensor, DiagnosticInfo], str]]]): Optional error message to use if
            the values of corresponding tensors mismatch. Can be passed as callable in which case it will be called
            with the mismatching tensors and a namespace of diagnostic info about the mismatches. See below for details.

    Raises:
        UsageError: If a :class:`torch.Tensor` can't be constructed from an array-or-scalar-like.
        UsageError: If any tensor is quantized or sparse. This is a temporary restriction and will be relaxed in the
            future.
        UsageError: If only :attr:`rtol` or :attr:`atol` is specified.
        AssertionError: If corresponding array-likes have different types.
        AssertionError: If the inputs are :class:`~collections.abc.Sequence`'s, but their length does not match.
        AssertionError: If the inputs are :class:`~collections.abc.Mapping`'s, but their set of keys do not match.
        AssertionError: If corresponding tensors do not have the same :attr:`~torch.Tensor.shape`.
        AssertionError: If :attr:`check_device`, but corresponding tensors are not on the same
            :attr:`~torch.Tensor.device`.
        AssertionError: If :attr:`check_dtype`, but corresponding tensors do not have the same ``dtype``.
        AssertionError: If :attr:`check_stride`, but corresponding tensors do not have the same stride.
        AssertionError: If the values of corresponding tensors are not close.

    The following table displays the default ``rtol`` and ``atol`` for different ``dtype``'s. Note that the ``dtype``
    refers to the promoted type in case :attr:`actual` and :attr:`expected` do not have the same ``dtype``.

    +---------------------------+------------+----------+
    | ``dtype``                 | ``rtol``   | ``atol`` |
    +===========================+============+==========+
    | :attr:`~torch.float16`    | ``1e-3``   | ``1e-5`` |
    +---------------------------+------------+----------+
    | :attr:`~torch.bfloat16`   | ``1.6e-2`` | ``1e-5`` |
    +---------------------------+------------+----------+
    | :attr:`~torch.float32`    | ``1.3e-6`` | ``1e-5`` |
    +---------------------------+------------+----------+
    | :attr:`~torch.float64`    | ``1e-7``   | ``1e-7`` |
    +---------------------------+------------+----------+
    | :attr:`~torch.complex32`  | ``1e-3``   | ``1e-5`` |
    +---------------------------+------------+----------+
    | :attr:`~torch.complex64`  | ``1.3e-6`` | ``1e-5`` |
    +---------------------------+------------+----------+
    | :attr:`~torch.complex128` | ``1e-7``   | ``1e-7`` |
    +---------------------------+------------+----------+
    | other                     | ``0.0``    | ``0.0``  |
    +---------------------------+------------+----------+

    The namespace of diagnostic information that will be passed to :attr:`msg` if its a callable has the following
    attributes:

    - ``number_of_elements`` (int): Number of elements in each tensor being compared.
    - ``total_mismatches`` (int): Total number of mismatches.
    - ``mismatch_ratio`` (float): Total mismatches divided by number of elements.
    - ``max_abs_diff`` (Union[int, float]): Greatest absolute difference of the inputs.
    - ``max_abs_diff_idx`` (Union[int, Tuple[int, ...]]): Index of greatest absolute difference.
    - ``max_rel_diff`` (Union[int, float]): Greatest relative difference of the inputs.
    - ``max_rel_diff_idx`` (Union[int, Tuple[int, ...]]): Index of greatest relative difference.

    For ``max_abs_diff`` and ``max_rel_diff`` the type depends on the :attr:`~torch.Tensor.dtype` of the inputs.

    .. note::

        :func:`~torch.testing.assert_close` is highly configurable with strict default settings. Users are encouraged
        to :func:`~functools.partial` it to fit their use case. For example, if an equality check is needed, one might
        define an ``assert_equal`` that uses zero tolrances for every ``dtype`` by default:

        >>> import functools
        >>> import torch
        >>> assert_equal = functools.partial(torch.testing.assert_close, rtol=0, atol=0)
        >>> assert_equal(1e-9, 1e-10)
        AssertionError: Tensors are not close!
        <BLANKLINE>
        Mismatched elements: 1 / 1 (100.0%)
        Greatest absolute difference: 8.999999703829253e-10 at 0 (up to 0 allowed)
        Greatest relative difference: 8.999999583666371 at 0 (up to 0 allowed)

    Examples:
        >>> # tensor to tensor comparison
        >>> expected = torch.tensor([1e0, 1e-1, 1e-2])
        >>> actual = torch.acos(torch.cos(expected))
        >>> torch.testing.assert_close(actual, expected)

        >>> # scalar to scalar comparison
        >>> import math
        >>> expected = math.sqrt(2.0)
        >>> actual = 2.0 / math.sqrt(2.0)
        >>> torch.testing.assert_close(actual, expected)

        >>> # numpy array to numpy array comparison
        >>> import numpy as np
        >>> expected = np.array([1e0, 1e-1, 1e-2])
        >>> actual = np.arccos(np.cos(expected))
        >>> torch.testing.assert_close(actual, expected)

        >>> # sequence to sequence comparison
        >>> import numpy as np
        >>> # The types of the sequences do not have to match. They only have to have the same
        >>> # length and their elements have to match.
        >>> expected = [torch.tensor([1.0]), 2.0, np.array(3.0)]
        >>> actual = tuple(expected)
        >>> torch.testing.assert_close(actual, expected)

        >>> # mapping to mapping comparison
        >>> from collections import OrderedDict
        >>> import numpy as np
        >>> foo = torch.tensor(1.0)
        >>> bar = 2.0
        >>> baz = np.array(3.0)
        >>> # The types and a possible ordering of mappings do not have to match. They only
        >>> # have to have the same set of keys and their elements have to match.
        >>> expected = OrderedDict([("foo", foo), ("bar", bar), ("baz", baz)])
        >>> actual = {"baz": baz, "bar": bar, "foo": foo}
        >>> torch.testing.assert_close(actual, expected)

        >>> # Different input types are never considered close.
        >>> expected = torch.tensor([1.0, 2.0, 3.0])
        >>> actual = expected.numpy()
        >>> torch.testing.assert_close(actual, expected)
        AssertionError: Except for scalars, type equality is required, but got
        <class 'numpy.ndarray'> and <class 'torch.Tensor'> instead.
        >>> # Scalars of different types are an exception and can be compared with
        >>> # check_dtype=False.
        >>> torch.testing.assert_close(1.0, 1, check_dtype=False)

        >>> # NaN != NaN by default.
        >>> expected = torch.tensor(float("Nan"))
        >>> actual = expected.clone()
        >>> torch.testing.assert_close(actual, expected)
        AssertionError: Tensors are not close!
        >>> torch.testing.assert_close(actual, expected, equal_nan=True)

        >>> # If equal_nan=True, the real and imaginary NaN's of complex inputs have to match.
        >>> expected = torch.tensor(complex(float("NaN"), 0))
        >>> actual = torch.tensor(complex(0, float("NaN")))
        >>> torch.testing.assert_close(actual, expected, equal_nan=True)
        AssertionError: Tensors are not close!
        >>> # If equal_nan="relaxed", however, then complex numbers are treated as NaN if any
        >>> # of the real or imaginary component is NaN.
        >>> torch.testing.assert_close(actual, expected, equal_nan="relaxed")

        >>> expected = torch.tensor([1.0, 2.0, 3.0])
        >>> actual = torch.tensor([1.0, 4.0, 5.0])
        >>> # The default mismatch message can be overwritten.
        >>> torch.testing.assert_close(actual, expected, msg="Argh, the tensors are not close!")
        AssertionError: Argh, the tensors are not close!
        >>> # The error message can also created at runtime by passing a callable.
        >>> def custom_msg(actual, expected, diagnostic_info):
        ...     return (
        ...         f"Argh, we found {diagnostic_info.total_mismatches} mismatches! "
        ...         f"That is {diagnostic_info.mismatch_ratio:.1%}!"
        ...     )
        >>> torch.testing.assert_close(actual, expected, msg=custom_msg)
        AssertionError: Argh, we found 2 mismatches! That is 66.7%!
    """
    # Hide this function from `pytest`'s traceback
    __tracebackhide__ = True

    error_meta, pair = _parse_inputs(actual, expected)
    if error_meta:
        raise error_meta.to_error()
    else:
        pair = cast(Union[_TensorPair, List, Dict], pair)

    error_meta = _check_pair_close(
        pair,
        rtol=rtol,
        atol=atol,
        equal_nan=equal_nan,
        check_device=check_device,
        check_dtype=check_dtype,
        check_stride=check_stride,
        msg=msg,
    )
    if error_meta:
        raise error_meta.to_error()
