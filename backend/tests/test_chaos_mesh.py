"""Track T7 — real Chaos Mesh injection with an honest local fallback.

Three things are locked down here:

1. The CR builder produces well-formed, self-expiring Chaos Mesh resources
   for every experiment type the API advertises.
2. The endpoint never lies about which mode it ran in. Ground truth that says
   "injected" when nothing was injected would corrupt every evaluation the
   platform computes from that ledger.
3. The IaC matches reality: the k8s manifest declares the app's real mesh
   ports (not upstream Online Boutique's), and terraform actually declares
   the Chaos Mesh helm_release step 4 asked for.
"""

import re
from pathlib import Path

from app.api.chaos import (
    REQUIRED_CRDS,
    ChaosMeshInjector,
    build_chaos_mesh_cr,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "k8s" / "manifests" / "online-boutique.yaml"
TERRAFORM_DIR = REPO_ROOT / "terraform"


def _manifest_docs():
    import yaml

    return [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]


def _container_port(deployment):
    ports = deployment["spec"]["template"]["spec"]["containers"][0].get("ports", [])
    assert ports, f"{deployment['metadata']['name']} declares no ports"
    return ports[0]["containerPort"]


# ---------------------------------------------------------------------------
# 1. The CR builder
# ---------------------------------------------------------------------------

def test_pod_failure_cr_kills_the_container():
    plural, body = build_chaos_mesh_cr("cart-service", "PodFailure", 60, "exp-1")

    assert plural == "podchaos"
    assert body["kind"] == "PodChaos"
    # container-kill, so the kubelet restarts the container and RESTARTS
    # climbs on the same pod — pod-kill would replace the pod and reset it.
    assert body["spec"]["action"] == "container-kill"
    assert body["spec"]["mode"] == "one"
    assert body["spec"]["duration"] == "60s"
    assert body["metadata"]["namespace"] == "chaos-mesh"
    assert body["spec"]["selector"]["namespaces"] == ["default"]
    assert body["spec"]["selector"]["labelSelectors"] == {
        "app.kubernetes.io/name": "cart-service"
    }
    # containerNames is mandatory (vpodchaos rejects null); the map matches
    # the manifest's container names — see the manifest lock test below.
    assert body["spec"]["containerNames"] == ["server"]

    _, redis_cr = build_chaos_mesh_cr("redis-cart", "PodFailure", 60, "exp-r")
    assert redis_cr["spec"]["containerNames"] == ["redis"]


def test_network_latency_cr_delays_traffic():
    plural, body = build_chaos_mesh_cr(
        "payment-service", "NetworkLatency", 30, "exp-2"
    )

    assert plural == "networkchaos"
    assert body["kind"] == "NetworkChaos"
    assert body["spec"]["action"] == "delay"
    assert body["spec"]["delay"]["latency"] == "300ms"
    assert body["spec"]["duration"] == "30s"


def test_network_partition_cr_blocks_both_directions():
    plural, body = build_chaos_mesh_cr(
        "frontend", "NetworkPartition", 45, "exp-3"
    )

    assert plural == "networkchaos"
    assert body["spec"]["action"] == "partition"
    assert body["spec"]["direction"] == "both"


def test_stress_chaos_cr_burns_cpu():
    plural, body = build_chaos_mesh_cr(
        "productcatalog-service", "StressChaos", 60, "exp-4"
    )

    assert plural == "stresschaos"
    # StressChaos has no `action` field — the API rejects it as unknown.
    assert "action" not in body["spec"]
    assert body["spec"]["stressors"]["cpu"]["load"] == 95


def test_every_experiment_type_self_expires():
    """No experiment may run forever.

    A fault left running because a process died mid-experiment is exactly
    the surprise a chaos platform exists to prevent.
    """
    for experiment_type in (
        "PodFailure", "NetworkLatency", "NetworkPartition", "StressChaos"
    ):
        _, body = build_chaos_mesh_cr("cart-service", experiment_type, 42, "exp")
        assert body["spec"].get("duration") == "42s", experiment_type


def test_unknown_experiment_type_has_no_mapping():
    """An unmapped type must return None so the caller falls back loudly
    instead of simulating something different from what was asked."""
    assert build_chaos_mesh_cr("cart-service", "DNSFailure", 60, "exp") is None


# ---------------------------------------------------------------------------
# 2. The injector's honesty contract
# ---------------------------------------------------------------------------

def test_injector_refuses_to_create_when_unavailable(monkeypatch):
    monkeypatch.setattr(
        ChaosMeshInjector, "_detect",
        lambda self: (False, "no usable Kubernetes config"),
    )
    injector = ChaosMeshInjector()

    assert injector.available() is False
    result = injector.create("cart-service", "PodFailure", 30, "exp-1")

    assert result["created"] is False
    assert "no usable Kubernetes config" in result["reason"]


def test_probe_is_cached_until_forced(monkeypatch):
    calls = []

    def _detect(self):
        calls.append(1)
        return False, "reason"

    monkeypatch.setattr(ChaosMeshInjector, "_detect", _detect)
    injector = ChaosMeshInjector()

    injector.probe()
    injector.probe()
    assert len(calls) == 1, "probe should not re-hit the API on every call"

    injector.probe(force=True)
    assert len(calls) == 2, "status/cleanup endpoints must be able to re-probe"


def test_inject_falls_back_to_simulation_and_says_so(test_client):
    """Local fallback is fine — a *silent* fallback is not."""
    res = test_client.post(
        "/api/v1/chaos/inject",
        json={
            "service": "payment-service",
            "experiment_type": "NetworkLatency",
            "tenant_id": "tenant_a",
            "duration_seconds": 60,
        },
    )
    assert res.status_code == 200

    ground_truth = res.json()["ground_truth"]
    assert ground_truth["injection_mode"] == "local_simulation"
    reason = ground_truth["real_mesh_action"].get("chaos_mesh_unavailable_reason")
    assert reason, "the fallback must state why the real path was not taken"


def test_inject_reports_chaos_mesh_mode_when_cr_is_created(test_client, monkeypatch):
    from app.mesh.manager import get_mesh_manager

    def _fake_create(self, service, experiment_type, duration_seconds, name):
        return {
            "created": True,
            "cr_name": name,
            "plural": "podchaos",
            "kind": "PodChaos",
            "namespace": "chaos-mesh",
            "target": {"app.kubernetes.io/name": service},
            "target_pods": 1,
            "duration_seconds": duration_seconds,
        }

    mirror_calls = []

    def _fake_inject(name, experiment_type, k8s_side_effects=True):
        mirror_calls.append((name, experiment_type, k8s_side_effects))
        return {"status": "SUCCESS", "message": "local mirror only"}

    monkeypatch.setattr(ChaosMeshInjector, "create", _fake_create)
    monkeypatch.setattr(get_mesh_manager(), "inject_real_fault", _fake_inject)

    res = test_client.post(
        "/api/v1/chaos/inject",
        json={
            "service": "cart-service",
            "experiment_type": "PodFailure",
            "tenant_id": "tenant_a",
        },
    )
    assert res.status_code == 200

    ground_truth = res.json()["ground_truth"]
    assert ground_truth["injection_mode"] == "chaos_mesh"
    action = ground_truth["real_mesh_action"]
    assert action["kubernetes_cr"].startswith("podchaos/")
    assert action["namespace"] == "chaos-mesh"
    assert "chaos-mesh" in action["message"]

    # The local mesh must be mirrored so telemetry reflects the fault, but
    # with k8s_side_effects=False — the CR owns the cluster side, and a
    # manual pod-delete beside a PodChaos would kill the replacement pod.
    assert mirror_calls == [("cart-service", "PodFailure", False)]
    # ...and the mirror is disclosed in the ledger, not silently applied.
    assert action["local_mirror"]["message"] == "local mirror only"


def test_two_injects_in_the_same_second_keep_the_alert_count_honest(test_client):
    """Regression: alert/experiment ids were built from int(time.time()).

    Second granularity meant two injects inside one second produced identical
    ids. The correlation engine then counted them in total_alerts while
    deduplicating them from alert_ids (breaking the P0 honesty invariant
    asserted by test_chaos_and_benchmarks), and the ledger's primary key
    collided so the second ground-truth row was silently dropped.
    """
    experiment_ids = []
    for _ in range(2):
        res = test_client.post(
            "/api/v1/chaos/inject",
            json={
                "service": "payment-service",
                "experiment_type": "NetworkLatency",
                "tenant_id": "tenant_a",
                "duration_seconds": 30,
            },
        )
        assert res.status_code == 200
        body = res.json()
        experiment_ids.append(body["ground_truth"]["experiment_id"])

        # Fetch immediately: incidents are cumulative, and the next inject
        # below appends to this one — comparing afterwards would race our
        # own test. Both injects land in the same correlation window, which
        # is exactly the scenario that used to collide.
        incident = test_client.get(
            f"/api/v1/incidents/{body['correlated_incident_id']}"
        ).json()["incident"]
        assert body["compressed_alerts_count"] == len(incident["alert_ids"])

    assert experiment_ids[0] != experiment_ids[1], "ids must not collide in one second"


def test_status_endpoint_reports_capability_and_active_crds(test_client):
    res = test_client.get("/api/v1/chaos/chaos-mesh")
    assert res.status_code == 200

    body = res.json()
    assert body["mode"] in ("chaos_mesh", "local_simulation")
    assert body["available"] == (body["mode"] == "chaos_mesh")
    assert set(body["required_crds"]) == set(REQUIRED_CRDS)
    assert set(body["experiments"]) == set(REQUIRED_CRDS)
    assert isinstance(body["reason"], str) and body["reason"]

    # Chaos Mesh keeps finished CRs, so each entry carries a state instead
    # of implying "still faulting" by merely existing.
    for plural, states in body["experiments"].items():
        assert isinstance(states, dict), plural
        for name, state in states.items():
            assert state in ("pending", "injected", "recovered"), (name, state)


def test_cleanup_refuses_honestly_when_chaos_mesh_is_unavailable(test_client):
    res = test_client.post("/api/v1/chaos/cleanup")
    assert res.status_code == 200

    body = res.json()
    # Under test the injector is forced off (see conftest), so the endpoint
    # must report NOT_APPLICABLE rather than pretend it cleaned anything.
    assert body["status"] == "NOT_APPLICABLE"
    assert body["deleted"] == []
    assert body["reason"]


# ---------------------------------------------------------------------------
# 3. The IaC matches the app
# ---------------------------------------------------------------------------

def test_manifest_ports_match_the_app_mesh():
    """T7 step 3: the manifest must declare the app's real ports.

    The upstream Online Boutique manifest shipped with 7070/50051/8080/3550/
    6379, while this platform's mesh runs on 8181/8182/8083/8084/6380. A
    re-upstreamed manifest would pass `kubectl apply` and still be wrong.
    """
    from app.mesh.manager import SERVICES_METADATA

    docs = _manifest_docs()
    deployments = [d for d in docs if d["kind"] == "Deployment"]
    services = [d for d in docs if d["kind"] == "Service"]

    matched = set()
    for dep in deployments:
        app_name = dep["metadata"]["labels"].get("app.kubernetes.io/name")
        if app_name in SERVICES_METADATA:
            assert _container_port(dep) == SERVICES_METADATA[app_name]["port"], (
                f"{app_name} container port does not match the app's mesh port"
            )
            matched.add(app_name)
    assert matched == set(SERVICES_METADATA), "every app service must be in the manifest"

    # Each Service must agree with its own pod template: port == targetPort
    # == what the container actually listens on.
    by_pod_app = {
        dep["spec"]["template"]["metadata"]["labels"]["app"]: dep
        for dep in deployments
    }
    checked = 0
    for svc in services:
        dep = by_pod_app.get((svc["spec"].get("selector") or {}).get("app"))
        if dep is None:
            continue
        expected = _container_port(dep)
        for port in svc["spec"]["ports"]:
            assert port["port"] == expected, f"{svc['metadata']['name']} service port"
            assert port["targetPort"] == expected, (
                f"{svc['metadata']['name']} targetPort"
            )
        checked += 1
    assert checked == len(SERVICES_METADATA)


def test_injector_selector_matches_pods_the_manifest_creates():
    """The injector selects pods by app.kubernetes.io/name=<service>.

    If the manifest stops carrying that label on the *pod template*, every
    experiment silently targets zero pods — the exact no-op the injector's
    pod check exists to catch, but caught earlier and cheaper here.
    """
    from app.mesh.manager import SERVICES_METADATA

    template_names = set()
    for dep in _manifest_docs():
        if dep["kind"] != "Deployment":
            continue
        labels = dep["spec"]["template"]["metadata"]["labels"]
        name = labels.get("app.kubernetes.io/name")
        if name in SERVICES_METADATA:
            template_names.add(name)

    assert template_names == set(SERVICES_METADATA)


def test_manifest_container_names_match_the_injector_map():
    """container-kill needs the exact container name.

    The builder defaults to `server` with an override for redis-cart; if the
    manifest's names ever drift, container-kill would target a container that
    not exist and inject nothing — a silent no-op.
    """
    from app.api.chaos import POD_CONTAINER_NAMES

    for dep in _manifest_docs():
        if dep["kind"] != "Deployment":
            continue
        app_name = dep["metadata"]["labels"].get("app.kubernetes.io/name")
        containers = dep["spec"]["template"]["spec"]["containers"]
        assert len(containers) == 1, f"{app_name} should run a single container"
        actual = containers[0]["name"]
        expected = POD_CONTAINER_NAMES.get(app_name, "server")
        assert actual == expected, (
            f"{app_name}: manifest container '{actual}' != builder's '{expected}'"
        )


def test_terraform_declares_helm_release_and_namespaces():
    """T7 step 4: `terraform validate` must pass, which means the legacy
    Terraform 0.11 `type = "string"` form has to stay gone."""
    main = (TERRAFORM_DIR / "main.tf").read_text()
    variables = (TERRAFORM_DIR / "variables.tf").read_text()

    assert 'resource "helm_release" "chaos_mesh"' in main
    assert 'resource "kubernetes_namespace" "chaos_mesh"' in main
    assert 'resource "kubernetes_namespace" "monitoring"' in main
    assert "chaosDaemon.runtime" in main

    assert not re.search(r'type\s*=\s*"string"', variables), (
        'legacy type = "string" fails terraform validate on modern Terraform'
    )
    # The old chaos-mesh.github.io/chaos-mesh repository 404s.
    assert "charts.chaos-mesh.org" in variables
