"""The RequestTracer on_finalize sink — the single chokepoint that persists
every request's outcome to request_metrics."""
from berth.daemon.request_tracer import RequestTracer


def test_on_finalize_called_once_with_completed_trace():
    seen = []
    tr = RequestTracer(on_finalize=lambda t: seen.append(t))
    trace = tr.start(method="POST", path="/v1/chat/completions")
    tr.finalize(trace, status_code=200)
    assert len(seen) == 1
    assert seen[0].status_code == 200
    assert seen[0].completed_at is not None


def test_on_finalize_exception_does_not_propagate():
    def boom(_t):
        raise RuntimeError("db down")
    tr = RequestTracer(on_finalize=boom)
    trace = tr.start(method="GET", path="/x")
    tr.finalize(trace, status_code=500)  # must not raise


def test_no_sink_is_fine():
    tr = RequestTracer()
    trace = tr.start(method="GET", path="/x")
    tr.finalize(trace, status_code=200)  # no on_finalize set; must not raise
