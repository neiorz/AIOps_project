import httpx
import logging
from typing import Dict, Any, Optional
from app.config import settings

logger = logging.getLogger(__name__)

class TelemetryTools:
    """
    Diagnostic toolset used by Autonomous SRE Agent to query observability backends:
    - Prometheus (PromQL metrics)
    - Grafana Loki (LogQL container logs)
    - Grafana Tempo (TraceQL distributed traces)
    - Kubernetes API (pods, deployments, events)
    """

    @classmethod
    async def query_prometheus(cls, query: str) -> Dict[str, Any]:
        """Execute a PromQL instant query against Prometheus."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{settings.PROMETHEUS_URL}/api/v1/query",
                    params={"query": query}
                )
                if resp.status_code == 200:
                    return resp.json().get("data", {})
                return {"error": f"Prometheus query failed with HTTP {resp.status_code}: {resp.text}"}
        except Exception as e:
            logger.warning(f"Prometheus query error: {e}")
            return {"error": str(e), "mock_data": {"resultType": "vector", "result": []}}

    @classmethod
    async def query_loki(cls, query: str, limit: int = 50) -> Dict[str, Any]:
        """Execute a LogQL query against Grafana Loki."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{settings.LOKI_URL}/loki/api/v1/query_range",
                    params={"query": query, "limit": limit}
                )
                if resp.status_code == 200:
                    return resp.json().get("data", {})
                return {"error": f"Loki query failed with HTTP {resp.status_code}: {resp.text}"}
        except Exception as e:
            logger.warning(f"Loki query error: {e}")
            return {"error": str(e), "mock_data": {"resultType": "streams", "result": []}}

    @classmethod
    async def query_tempo(cls, trace_id: str) -> Dict[str, Any]:
        """Fetch trace details from Grafana Tempo."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{settings.TEMPO_URL}/api/traces/{trace_id}")
                if resp.status_code == 200:
                    return resp.json()
                return {"error": f"Tempo query failed with HTTP {resp.status_code}"}
        except Exception as e:
            logger.warning(f"Tempo query error: {e}")
            return {"error": str(e)}

    @classmethod
    def get_k8s_pod_status(cls, service_name: str, namespace: str = "default") -> Dict[str, Any]:
        """Fetch pod statuses and restart counts from Kubernetes API."""
        try:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
            
            v1 = client.CoreV1Api()
            pods = v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={service_name}")
            items = []
            for pod in pods.items:
                items.append({
                    "name": pod.metadata.name,
                    "phase": pod.status.phase,
                    "restart_count": sum(c.restart_count for c in (pod.status.container_statuses or [])),
                    "node": pod.spec.node_name
                })
            return {"service": service_name, "namespace": namespace, "pods": items}
        except Exception as e:
            logger.warning(f"Kubernetes query error: {e}")
            return {"error": str(e), "service": service_name, "pods": []}
