import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple
from pydantic import BaseModel
from fastapi import APIRouter
from app.correlation.engine import RawAlert, get_correlation_engine
from app.agent.sre_agent import get_sre_agent
from app.db.repo import load_ground_truth, save_ground_truth, save_incident  # T2 seam

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chaos", tags=["Chaos Engineering & Ground Truth"])

class ChaosInjectionRequest(BaseModel):
    service: str  # payment-service, cart-service, frontend, productcatalog-service, redis-cart
    experiment_type: str  # NetworkLatency, PodFailure, StressChaos, NetworkPartition
    tenant_id: str = "tenant_a"  # tenant_a, tenant_b, tenant_c
    duration_seconds: int = 60


# ---------------------------------------------------------------------------
# Track T7 — real Chaos Mesh injection with an honest local fallback
# ---------------------------------------------------------------------------

CHAOS_MESH_GROUP = "chaos-mesh.org"
CHAOS_MESH_VERSION = "v1alpha1"
CHAOS_MESH_NAMESPACE = "chaos-mesh"

# Where k8s/manifests/online-boutique.yaml deploys the workload, and the label
# its pods carry that matches this platform's service names exactly
# (cart-service, payment-service, ...). See the manifest's
# `app.kubernetes.io/name` labels.
WORKLOAD_NAMESPACE = "default"
POD_LABEL_KEY = "app.kubernetes.io/name"

# The CRDs that must all exist before injection is considered real.
REQUIRED_CRDS = ("podchaos", "networkchaos", "stresschaos")

# Container names per service in k8s/manifests/online-boutique.yaml — the
# four python services run `server`, redis runs `redis`. Needed because the
# vpodchaos admission webhook requires containerNames on container-kill.
# test_manifest_container_names_match locks this to the manifest.
POD_CONTAINER_NAMES = {"redis-cart": "redis"}


def build_chaos_mesh_cr(
    service: str, experiment_type: str, duration_seconds: int, name: str
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Build the Chaos Mesh custom resource for an experiment type.

    Returns ``(plural_name, body)``, or ``None`` when the requested type has
    no Chaos Mesh mapping — the caller must then say so rather than quietly
    simulating something else.

    Every experiment carries a ``duration`` so it self-expires: a fault left
    running because a process died mid-experiment is exactly the kind of
    surprise a chaos platform exists to prevent.
    """
    duration = f"{max(1, int(duration_seconds))}s"
    selector = {
        "namespaces": [WORKLOAD_NAMESPACE],
        "labelSelectors": {POD_LABEL_KEY: service},
    }
    metadata = {"name": name, "namespace": CHAOS_MESH_NAMESPACE}
    api = f"{CHAOS_MESH_GROUP}/{CHAOS_MESH_VERSION}"

    if experiment_type == "PodFailure":
        # container-kill, not pod-kill: killing the container makes the
        # kubelet restart it, so `kubectl get pods` shows RESTARTS climb on
        # the *same* pod — the literal step-5 check. pod-kill would replace
        # the pod object instead, resetting the counter to 0 on a new name
        # and proving less. containerNames IS required despite what
        # `kubectl explain` claims ("if not set, the first container will be
        # injected") — the vpodchaos admission webhook rejects a null.
        # The manifest runs `server` everywhere except redis-cart; a test
        # locks this map to the manifest's actual container names.
        return "podchaos", {
            "apiVersion": api, "kind": "PodChaos", "metadata": metadata,
            "spec": {
                "action": "container-kill", "mode": "one",
                "containerNames": [POD_CONTAINER_NAMES.get(service, "server")],
                "selector": selector, "duration": duration,
            },
        }

    if experiment_type == "NetworkLatency":
        return "networkchaos", {
            "apiVersion": api, "kind": "NetworkChaos", "metadata": metadata,
            "spec": {
                "action": "delay", "mode": "one",
                "selector": selector, "duration": duration,
                "delay": {"latency": "300ms", "jitter": "50ms",
                          "correlation": "75"},
            },
        }

    if experiment_type == "NetworkPartition":
        return "networkchaos", {
            "apiVersion": api, "kind": "NetworkChaos", "metadata": metadata,
            "spec": {
                "action": "partition", "mode": "one", "direction": "both",
                "selector": selector, "duration": duration,
            },
        }

    if experiment_type == "StressChaos":
        # No `action` field: StressChaos has none (the presence of
        # `stressors` is the action) — the API rejects it as unknown.
        return "stresschaos", {
            "apiVersion": api, "kind": "StressChaos", "metadata": metadata,
            "spec": {
                "mode": "one",
                "selector": selector, "duration": duration,
                "stressors": {"cpu": {"workers": 2, "load": 95}},
            },
        }

    return None


class ChaosMeshInjector:
    """Creates real Chaos Mesh CRs in-cluster, or reports that it cannot.

    HONESTY CONTRACT
    ----------------
    ``available()`` is the single source of truth for whether injection can
    be real. It is True only when **both** a usable Kubernetes config **and**
    every required Chaos Mesh CRD are present; anything else is False, and
    the endpoint falls back to local simulation.

    The mode is always surfaced in the response and in the ground-truth
    ledger. An experiment that silently simulated while claiming to inject
    would poison the ground truth the whole platform is measured against —
    a wrong label on a real measurement is worse than no measurement.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._probed = False
        self._available = False
        self._reason = "not probed yet"

    # -- capability detection ----------------------------------------------

    def probe(self, force: bool = False) -> Dict[str, Any]:
        """Determine whether real injection is possible. Cached."""
        with self._lock:
            if self._probed and not force:
                return {"available": self._available, "reason": self._reason}
            self._available, self._reason = self._detect()
            self._probed = True
            return {"available": self._available, "reason": self._reason}

    def available(self) -> bool:
        return self.probe()["available"]

    def _detect(self) -> Tuple[bool, str]:
        try:
            from kubernetes import client, config
        except Exception as exc:
            return False, f"kubernetes client unavailable: {exc}"

        loaded = None
        try:
            config.load_incluster_config()
            loaded = "in-cluster"
        except Exception:
            pass

        if loaded is None:
            import os
            for candidate in (
                os.environ.get("KUBECONFIG"),
                os.path.expanduser("~/.kube/config"),
            ):
                if not candidate or not os.path.exists(candidate):
                    continue
                try:
                    config.load_kube_config(config_file=candidate)
                    loaded = candidate
                    break
                except Exception:
                    continue

        if loaded is None:
            return False, "no usable Kubernetes config (not in-cluster, no kubeconfig)"

        apiextensions = client.ApiextensionsV1Api()
        missing = []
        for crd in REQUIRED_CRDS:
            try:
                apiextensions.read_custom_resource_definition(
                    name=f"{crd}.{CHAOS_MESH_GROUP}"
                )
            except Exception:
                missing.append(crd)

        if missing:
            return False, (
                f"Chaos Mesh CRDs not installed ({', '.join(missing)}). "
                f"Install with `helm install chaos-mesh chaos-mesh/chaos-mesh "
                f"--repo https://charts.chaos-mesh.org -n chaos-mesh`."
            )

        return True, f"kubernetes context '{loaded}', all Chaos Mesh CRDs present"

    # -- injection ----------------------------------------------------------

    def create(
        self, service: str, experiment_type: str, duration_seconds: int, name: str
    ) -> Dict[str, Any]:
        """Create a real Chaos Mesh CR. Never fabricates success."""
        if not self.available():
            return {
                "created": False,
                "reason": self.probe()["reason"],
            }

        built = build_chaos_mesh_cr(service, experiment_type, duration_seconds, name)
        if built is None:
            return {
                "created": False,
                "reason": f"no Chaos Mesh mapping for experiment type '{experiment_type}'",
            }

        plural, body = built

        # A CR that matches no pods injects nothing while still looking like
        # a running experiment — a silent no-op is worse than a fallback.
        # Verify a live target exists before claiming success.
        try:
            from kubernetes import client

            pods = client.CoreV1Api().list_namespaced_pod(
                namespace=WORKLOAD_NAMESPACE,
                label_selector=f"{POD_LABEL_KEY}={service}",
            )
            live = [
                p for p in pods.items
                if p.status.phase not in ("Succeeded", "Failed")
            ]
        except Exception as exc:
            return {"created": False, "reason": f"cannot list target pods: {exc}"}

        if not live:
            return {
                "created": False,
                "reason": (
                    f"no live pods match {POD_LABEL_KEY}={service} in namespace "
                    f"'{WORKLOAD_NAMESPACE}' — deploy k8s/manifests/"
                    f"online-boutique.yaml first, or fall back to simulation"
                ),
            }

        try:
            client.CustomObjectsApi().create_namespaced_custom_object(
                group=CHAOS_MESH_GROUP,
                version=CHAOS_MESH_VERSION,
                namespace=CHAOS_MESH_NAMESPACE,
                plural=plural,
                body=body,
            )
        except Exception as exc:
            return {"created": False, "reason": f"CR creation failed: {exc}"}

        return {
            "created": True,
            "cr_name": name,
            "plural": plural,
            "kind": body["kind"],
            "namespace": CHAOS_MESH_NAMESPACE,
            "target": {POD_LABEL_KEY: service},
            "target_pods": len(live),
            "duration_seconds": duration_seconds,
        }

    def delete(self, name: str, plural: str) -> Dict[str, Any]:
        """Remove a CR. Best-effort: a failed delete is reported, not hidden."""
        try:
            from kubernetes import client

            client.CustomObjectsApi().delete_namespaced_custom_object(
                group=CHAOS_MESH_GROUP,
                version=CHAOS_MESH_VERSION,
                namespace=CHAOS_MESH_NAMESPACE,
                plural=plural,
                name=name,
            )
            return {"deleted": True, "cr_name": name}
        except Exception as exc:
            return {"deleted": False, "reason": str(exc)}

    def list_experiments(self, plural: str) -> Dict[str, str]:
        """Map CR name -> "pending" | "injected" | "recovered".

        Chaos Mesh keeps the CR object after a fault self-recovers, so
        existence alone would overstate what is running. The conditions tell
        the truth: AllRecovered=True means the fault is over, AllInjected=True
        means it is live right now. {} when the list cannot be read.
        """
        try:
            from kubernetes import client

            result = client.CustomObjectsApi().list_namespaced_custom_object(
                group=CHAOS_MESH_GROUP,
                version=CHAOS_MESH_VERSION,
                namespace=CHAOS_MESH_NAMESPACE,
                plural=plural,
            )
        except Exception:
            return {}

        states: Dict[str, str] = {}
        for item in result.get("items", []):
            name = item.get("metadata", {}).get("name", "")
            conditions = {
                c.get("type"): c.get("status")
                for c in ((item.get("status") or {}).get("conditions") or [])
            }
            if conditions.get("AllRecovered") == "True":
                states[name] = "recovered"
            elif conditions.get("AllInjected") == "True":
                states[name] = "injected"
            else:
                states[name] = "pending"
        return states


_chaos_mesh_injector = ChaosMeshInjector()


def get_chaos_mesh_injector() -> ChaosMeshInjector:
    return _chaos_mesh_injector


# In-memory Ground Truth Ledger (synchronized with PostgreSQL in production)
ground_truth_ledger: List[Dict[str, Any]] = []

CHAOS_SIGNATURES = {
    "NetworkLatency": {
        "symptom": "HTTP 504 Gateway Timeout and P99 latency spike > 1200ms",
        "ground_truth_cause": "NetworkChaos: 300ms inter-service RTT delay & jitter",
        "severity": "critical"
    },
    "PodFailure": {
        "symptom": "Pod CrashLoopBackOff & Exit Code 137 (OOM / Kill)",
        "ground_truth_cause": "PodChaos: Simulated node eviction or SIGKILL on container",
        "severity": "critical"
    },
    "StressChaos": {
        "symptom": "CPU CFS Throttling surging > 85% & thread pool saturation",
        "ground_truth_cause": "StressChaos: 95% CPU & memory stress burner",
        "severity": "high"
    },
    "NetworkPartition": {
        "symptom": "RPC transport error: Dial failed & split-brain connection reset",
        "ground_truth_cause": "NetworkChaos: Bi-directional iptables packet drop",
        "severity": "critical"
    }
}

@router.post("/inject")
async def inject_chaos(req: ChaosInjectionRequest):
    """
    Injects a precision failure into the microservice mesh,
    records Ground Truth in the Ledger, and cascades telemetry alerts into the correlation engine.
    """
    sig = CHAOS_SIGNATURES.get(req.experiment_type, {
        "symptom": "Generic service degradation",
        "ground_truth_cause": req.experiment_type,
        "severity": "warning"
    })

    injected_at = time.time()
    # Ids must be unique per injection. `int(time.time())` has second
    # granularity, so two injects in the same second produced identical
    # experiment/alert ids: the ledger's primary key collided (the second
    # save is swallowed by the repo's error handler) and the correlation
    # engine counted the duplicates in total_alerts while deduplicating
    # them from alert_ids — breaking the honesty invariant
    # `compressed_alerts_count == len(incident.alert_ids)`.
    unique_ns = time.time_ns()
    experiment_id = f"chaos_{req.service}_{unique_ns}"

    # T7: create a real Chaos Mesh CR when the cluster can actually absorb
    # one; fall back to the local simulator otherwise. The mode is recorded
    # on the ledger entry — ground truth that claims "injected" when nothing
    # was injected would silently corrupt every metric computed from it.
    from app.mesh.manager import get_mesh_manager

    cr_result = get_chaos_mesh_injector().create(
        service=req.service,
        experiment_type=req.experiment_type,
        duration_seconds=req.duration_seconds,
        name=experiment_id.replace("_", "-"),
    )

    if cr_result.get("created"):
        injection_mode = "chaos_mesh"
        # The CR *is* the cluster-side fault, so the mirror below runs with
        # k8s_side_effects=False: pairing inject_real_fault's own
        # kubectl-level injection with a PodChaos CR risks killing the
        # replacement pod as well. The local half still runs so the
        # platform's telemetry reflects what the CR just did — without it,
        # mesh_status reports HEALTHY while the cluster burns, and the agent
        # diagnoses from evidence that isn't there.
        local_mirror = get_mesh_manager().inject_real_fault(
            req.service, req.experiment_type, k8s_side_effects=False
        )
        real_mesh_result = {
            "status": "SUCCESS",
            "action": f"CHAOS_MESH_{cr_result['kind'].upper()}",
            "service": req.service,
            "kubernetes_cr": f"{cr_result['plural']}/{cr_result['cr_name']}",
            "namespace": cr_result["namespace"],
            "message": (
                f"Real Chaos Mesh {cr_result['kind']} '{cr_result['cr_name']}' "
                f"created in namespace '{cr_result['namespace']}', targeting "
                f"{POD_LABEL_KEY}={req.service} for {req.duration_seconds}s"
            ),
            # Disclosed, not hidden: the local mesh was updated too (state
            # only — no cluster side effects, those belong to the CR).
            "local_mirror": local_mirror,
        }
        logger.info(
            "T7: created real Chaos Mesh CR %s/%s for %s (%s)",
            cr_result["plural"], cr_result["cr_name"], req.service,
            req.experiment_type,
        )
    else:
        injection_mode = "local_simulation"
        real_mesh_result = get_mesh_manager().inject_real_fault(
            req.service, req.experiment_type
        )
        # State the fallback and its cause — a fallback is never silent.
        real_mesh_result["injection_mode"] = injection_mode
        real_mesh_result["chaos_mesh_unavailable_reason"] = cr_result.get("reason", "")

    # Record Ground Truth in Ledger
    ledger_entry = {
        "experiment_id": experiment_id,
        "service": req.service,
        "experiment_type": req.experiment_type,
        "tenant_id": req.tenant_id,
        "ground_truth_cause": sig["ground_truth_cause"],
        "expected_symptom": sig["symptom"],
        "injected_at": injected_at,
        "status": "ACTIVE",
        "injection_mode": injection_mode,
        "real_mesh_action": real_mesh_result
    }
    ground_truth_ledger.insert(0, ledger_entry)
    save_ground_truth(ledger_entry)                 # T2 seam: durable copy

    # Trigger Cascading Alert Storm
    engine = get_correlation_engine()
    
    # Primary culprit alert
    primary_alert = RawAlert(
        id=f"alt-pri-{unique_ns}",
        alertname=f"{req.service.replace('-', '_').title()}_{req.experiment_type}",
        service=req.service,
        severity=sig["severity"],
        tenant_id=req.tenant_id,
        description=f"REAL FAULT: {real_mesh_result.get('message', sig['ground_truth_cause'])}"
    )
    incident = engine.correlate(primary_alert)

    # Cascading alerts from downstream dependents
    downstream_map = {
        "cart-service": ["frontend", "checkout-service"],
        "payment-service": ["checkout-service", "frontend"],
        "redis-cart": ["cart-service", "frontend"],
        "productcatalog-service": ["frontend", "recommendation-service"],
        "frontend": ["ingress-gateway"]
    }
    downstream = downstream_map.get(req.service, ["frontend"])
    
    for dep in downstream:
        dep_alert = RawAlert(
            id=f"alt-casc-{dep}-{unique_ns}",
            alertname=f"{dep.replace('-', '_').title()}CascadingFailure",
            service=dep,
            severity="warning" if sig["severity"] != "critical" else "critical",
            tenant_id=req.tenant_id,
            description=f"Downstream disruption: upstream {req.service} unresponsive"
        )
        incident = engine.correlate(dep_alert)

    # Alert count is whatever the correlation engine actually observed.
    # (Previously this was padded with a hardcoded floor to fake a 500+ storm.)

    save_incident(incident)                          # T2 seam: durable incident

    # Trigger Autonomous AI Agent diagnosis asynchronously
    agent = get_sre_agent()
    investigation = await agent.investigate_incident(incident)

    ledger_entry["ai_diagnosis"] = investigation["root_cause_analysis"]["diagnosis"]
    ledger_entry["confidence_score"] = investigation["root_cause_analysis"]["confidence_score"]
    ledger_entry["investigation_duration_seconds"] = investigation["investigation_duration_seconds"]
    save_ground_truth(ledger_entry)                  # T2 seam: store the diagnosis

    return {
        "status": "CHAOS_INJECTED",
        "experiment_id": experiment_id,
        "ground_truth": ledger_entry,
        "correlated_incident_id": incident.incident_id,
        "compressed_alerts_count": incident.total_alerts,
        "investigation": investigation
    }

@router.get("/ledger")
def get_ground_truth_ledger():
    """Retrieve Ground-Truth Ledger entries.

    Reads the persisted store first so history survives a restart; the
    in-memory list is the fallback when the store is empty or unreachable.
    """
    return load_ground_truth() or ground_truth_ledger


@router.get("/chaos-mesh")
def chaos_mesh_status():
    """Report whether real Chaos Mesh injection is possible right now.

    Always re-probes (the answer changes when CRDs are installed or the
    kubeconfig goes away), and lists any experiments still running so a
    wedged cluster can be diagnosed without kubectl.
    """
    injector = get_chaos_mesh_injector()
    probe = injector.probe(force=True)
    return {
        "mode": "chaos_mesh" if probe["available"] else "local_simulation",
        "available": probe["available"],
        "reason": probe["reason"],
        "required_crds": list(REQUIRED_CRDS),
        "workload_namespace": WORKLOAD_NAMESPACE,
        # name -> pending/injected/recovered: Chaos Mesh keeps finished CRs
        # around, so existence alone would claim a fault is live when its
        # duration already expired.
        "experiments": {
            plural: injector.list_experiments(plural) for plural in REQUIRED_CRDS
        },
    }


@router.post("/cleanup")
def cleanup_experiments():
    """Delete every Chaos Mesh experiment this platform can see.

    Experiments self-expire via their `duration`, so this exists for the
    case where a demo ends early: a fault left running is the exact surprise
    chaos engineering is supposed to prevent.
    """
    injector = get_chaos_mesh_injector()
    probe = injector.probe(force=True)
    if not probe["available"]:
        return {"status": "NOT_APPLICABLE", "deleted": [],
                "reason": probe["reason"]}

    deleted, failed = [], []
    for plural in REQUIRED_CRDS:
        # Every state, not just "injected": a recovered CR is leftover
        # clutter, and deleting a live one stops its fault too.
        for name in injector.list_experiments(plural):
            result = injector.delete(name, plural)
            (deleted if result.get("deleted") else failed).append(
                {"plural": plural, "name": name, **result}
            )
    return {
        "status": "CLEANED" if not failed else "PARTIAL",
        "deleted": deleted,
        "failed": failed,
    }

