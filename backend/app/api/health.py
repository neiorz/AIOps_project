from fastapi import APIRouter
from typing import List, Dict, Any, Optional
from app.config import settings
from app.correlation.engine import get_correlation_engine

router = APIRouter(tags=["Health"])

# Services that run as real local processes in the AIOps microservice mesh.
LOCAL_MESH_SERVICES: List[Dict[str, Any]] = [
    {"name": "payment-service",      "language": "Go",           "port": 8081, "tier": "Transaction Core"},
    {"name": "cart-service",         "language": "C# (.NET)",    "port": 8082, "tier": "Core Service"},
    {"name": "frontend",             "language": "Go",           "port": 8083, "tier": "Ingress Gateway"},
    {"name": "productcatalog-service", "language": "Go",         "port": 8084, "tier": "Catalog API"},
    {"name": "redis-cart",           "language": "Redis",        "port": 6380, "tier": "Cache & State"},
]

# Declared in the Online Boutique manifest but NOT part of the local mesh.
# Reported as not-deployed rather than with invented latency/replica numbers.
REMOTE_ONLY_SERVICES: List[Dict[str, Any]] = [
    {"name": "checkout-service",         "language": "Go",      "tier": "Order Orchestration"},
    {"name": "recommendation-service",   "language": "Python",  "tier": "ML Recommender"},
    {"name": "shipping-service",         "language": "Go",      "tier": "Logistics Dispatch"},
    {"name": "email-service",            "language": "Python",  "tier": "Notification Queue"},
    {"name": "ad-service",               "language": "Java",    "tier": "Ad Delivery"},
]


@router.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": settings.PROJECT_NAME,
        "version": "1.0.0",
        "mode": "autonomous"
    }


def _k8s_cluster_name() -> Optional[str]:
    """Best-effort cluster name from the active kubeconfig; None if unavailable."""
    try:
        from kubernetes import config as k8s_config
        try:
            k8s_config.load_incluster_config()
            return "in-cluster"
        except Exception:
            pass
        rules = k8s_config.list_kube_config_contexts()
        if rules and rules[1]:
            return f"kubecontext:{rules[1].get('name', 'unknown')}"
    except Exception:
        return None
    return None


@router.get("/topology")
def get_service_topology() -> Dict[str, Any]:
    """
    Live topology built from ACTUAL process state.

    Every field is either read from the running local mesh (real CPU / memory /
    request counters / health) or explicitly marked as not-deployed. No
    replicas, latency or throughput values are invented.
    """
    engine = get_correlation_engine()
    incidents = engine.get_all_incidents()
    unresolved = [i for i in incidents if i.status not in ("RESOLVED", "REJECTED")]
    unresolved_services = set()
    for inc in unresolved:
        unresolved_services.add(inc.primary_service)
        unresolved_services.update(inc.affected_services)

    # Real, measured state of the local mesh
    mesh_state: Dict[str, Dict[str, Any]] = {}
    try:
        from app.mesh.manager import get_mesh_manager
        for entry in get_mesh_manager().get_mesh_status():
            mesh_state[entry.get("service")] = entry
    except Exception:
        pass

    services: List[Dict[str, Any]] = []
    for svc in LOCAL_MESH_SERVICES:
        live = mesh_state.get(svc["name"], {})
        live_status = live.get("status")
        degraded = svc["name"] in unresolved_services

        if live_status == "HEALTHY":
            status = "DEGRADED" if degraded else "HEALTHY"
        else:
            # Missing, UNKNOWN or CRASHED -> the process is not serving traffic.
            status = "NOT_RUNNING"

        bound = live_status is not None and live_status != "UNKNOWN"

        services.append({
            **svc,
            "source": "local-mesh",
            "status": status,
            "bound": bound,
            # Only populated when the process actually reported them:
            "cpu_percent": live.get("cpu_percent"),
            "memory_mb": live.get("memory_mb"),
            "requests_total": live.get("requests_total"),
            "uptime_seconds": live.get("uptime_seconds"),
        })

    for svc in REMOTE_ONLY_SERVICES:
        services.append({
            **svc,
            "port": None,
            "source": "manifest-only",
            "status": "NOT_DEPLOYED" if svc["name"] not in unresolved_services else "DEGRADED",
            "bound": False,
            "cpu_percent": None,
            "memory_mb": None,
            "requests_total": None,
            "uptime_seconds": None,
        })

    return {
        "cluster_name": _k8s_cluster_name(),
        "deployment_mode": "local-process-mesh",
        "total_microservices": len(services),
        "healthy_count": sum(1 for s in services if s["status"] == "HEALTHY"),
        "degraded_count": sum(1 for s in services if s["status"] == "DEGRADED"),
        "not_running_count": sum(1 for s in services if s["status"] == "NOT_RUNNING"),
        "not_deployed_count": sum(1 for s in services if s["status"] == "NOT_DEPLOYED"),
        "services": services,
    }
