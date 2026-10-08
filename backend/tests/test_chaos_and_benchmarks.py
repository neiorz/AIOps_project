def test_chaos_injection_and_ground_truth(test_client):
    payload = {
        "service": "payment-service",
        "experiment_type": "NetworkLatency",
        "tenant_id": "tenant_a",
        "duration_seconds": 60
    }
    response = test_client.post("/api/v1/chaos/inject", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "CHAOS_INJECTED"
    assert "ground_truth" in data
    assert data["ground_truth"]["service"] == "payment-service"
    assert data["ground_truth"]["experiment_type"] == "NetworkLatency"
    assert "correlated_incident_id" in data

    # P0.1/P0.3 HONESTY: the reported alert count must equal the alerts that
    # were actually correlated into this incident. (The previous assertion,
    # `>= 500`, passed only because chaos.py padded the value with a
    # hardcoded floor of a few hundred.)
    assert data["compressed_alerts_count"] >= 1
    inc = test_client.get(
        f"/api/v1/incidents/{data['correlated_incident_id']}"
    ).json()
    assert data["compressed_alerts_count"] == len(inc["incident"]["alert_ids"])


def test_evaluation_scorecard(test_client):
    response = test_client.get("/api/v1/benchmarks/scorecard")
    assert response.status_code == 200
    scorecard = response.json()

    assert "rca_accuracy_percentage" in scorecard
    acc = scorecard["rca_accuracy_percentage"]
    # P0.1 HONESTY: 0..100 and unclamped, or None when nothing was evaluated.
    # Previously this was clamped into a flattering band regardless of
    # correctness.
    assert acc is None or (0.0 <= acc <= 100.0)

    assert "mean_time_to_detect_seconds" in scorecard
    assert "sla_protection_rate" in scorecard
    assert "investigation_efficiency" in scorecard

    # P0.2 HONESTY: tool counts must be read from the live counters, not
    # synthesised with a formula over the experiment count.
    from app.tools.counters import get_tool_call_ledger
    assert scorecard["investigation_efficiency"]["tool_calls"] == get_tool_call_ledger()


def test_rca_matcher_does_not_force_credit():
    """Regression: the old scorecard had an else-branch that counted every
    experiment as accurate no matter what the agent diagnosed."""
    from app.api.benchmarks import diagnosis_matches

    # Mismatched failure modes must NOT be scored as correct.
    assert diagnosis_matches(
        "Runbook: Cart Service CPU Saturation & Resource Starvation",
        "NetworkChaos: 300ms inter-service RTT delay & jitter",
    ) is False

    # A genuine failure-mode match is still credited.
    assert diagnosis_matches(
        "Pod OOMKilled (Out Of Memory) & CrashLoopBackOff",
        "PodChaos: Simulated node eviction or SIGKILL on container",
    ) is True


def test_tool_call_ledger_increments():
    """Real counters: one recorded PromQL call must increase the total by one."""
    from app.tools.counters import get_tool_call_ledger, record_tool_call

    before = get_tool_call_ledger()["promql"]
    record_tool_call("promql")
    assert get_tool_call_ledger()["promql"] == before + 1


def test_service_that_cannot_bind_is_not_reported_healthy():
    """
    Regression: a service that fails to bind previously kept `is_running=True`,
    so /mesh/status reported HEALTHY and /mesh/metrics emitted service_up 1
    for processes that never started.
    """
    import socket
    from app.mesh.manager import LiveMicroservice

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 0))
    port = blocker.getsockname()[1]
    blocker.listen(1)
    try:
        svc = LiveMicroservice("probe-service", port, {"role": "bind test"})
        assert svc.start() is False
        assert svc.is_running is False
    finally:
        blocker.close()


def test_incident_remediation_execution(test_client):
    # First inject chaos to get an incident
    inject_res = test_client.post("/api/v1/chaos/inject", json={
        "service": "cart-service",
        "experiment_type": "PodFailure",
        "tenant_id": "tenant_b"
    })
    incident_id = inject_res.json()["correlated_incident_id"]

    # Remediate
    rem_res = test_client.post(f"/api/v1/incidents/{incident_id}/remediate", json={
        "action_type": "ansible"
    })
    assert rem_res.status_code == 200
    rem_data = rem_res.json()
    assert rem_data["status"] == "SUCCESS"
    assert rem_data["verified_health"] is True
    assert len(rem_data["execution_output"]) > 0

    # Verify incident is now resolved
    inc_res = test_client.get(f"/api/v1/incidents/{incident_id}")
    assert inc_res.status_code == 200
    assert inc_res.json()["incident"]["status"] == "RESOLVED"
