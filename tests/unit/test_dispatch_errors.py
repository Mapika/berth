from __future__ import annotations

import httpx
import pytest

from serve_engine.daemon.dispatch_errors import (
    NodeUnreachableError,
    RetryableError,
    classify_pre_first_byte,
    classify_pre_first_byte_status,
)


def test_connect_error_is_retryable():
    err = classify_pre_first_byte(httpx.ConnectError("nope"))
    assert isinstance(err, RetryableError)
    assert err.reason == "connect"


def test_timeout_is_retryable():
    err = classify_pre_first_byte(httpx.ReadTimeout("slow"))
    assert isinstance(err, RetryableError)
    assert err.reason == "timeout"


@pytest.mark.parametrize("status", [502, 503, 504])
def test_5xx_pre_first_byte_is_retryable(status):
    err = classify_pre_first_byte_status(status)
    assert isinstance(err, RetryableError)
    assert err.reason == "upstream_5xx"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_4xx_is_not_retryable(status):
    assert classify_pre_first_byte_status(status) is None


def test_500_with_no_clear_signal_is_not_retryable():
    """500 = model crash; retrying won't help and adds load."""
    assert classify_pre_first_byte_status(500) is None


def test_node_unreachable_error_is_retryable():
    err = classify_pre_first_byte(NodeUnreachableError(node_id=5))
    assert isinstance(err, RetryableError)
    assert err.reason == "node_unreachable"


def test_unknown_exception_is_not_retryable():
    err = classify_pre_first_byte(ValueError("weird"))
    assert err is None
