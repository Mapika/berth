from berth.observability.metrics import format_daemon_metrics


def test_format_daemon_metrics_empty():
    text = format_daemon_metrics(
        deployments_by_status={},
        models_total=0,
        api_keys_active=0,
        request_count=0,
    )
    assert "# TYPE berth_deployments gauge" in text
    assert "berth_deployments{status=" not in text  # no rows
    assert "berth_models_total 0" in text
    assert "berth_api_keys_active 0" in text
    assert "berth_proxy_requests_total 0" in text


def test_format_daemon_metrics_with_rows():
    text = format_daemon_metrics(
        deployments_by_status={"ready": 2, "loading": 1},
        models_total=3,
        api_keys_active=5,
        request_count=42,
    )
    assert 'berth_deployments{status="ready"} 2' in text
    assert 'berth_deployments{status="loading"} 1' in text
    assert "berth_models_total 3" in text
    assert "berth_api_keys_active 5" in text
    assert "berth_proxy_requests_total 42" in text
