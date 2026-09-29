"""
Real Microservice Mesh API Router
Exposes live process health, Prometheus metrics, real fault injection, and process remediation.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from app.mesh.manager import get_mesh_manager

router = APIRouter(prefix="/mesh", tags=["Real Microservice Mesh"])

class ChaosFaultRequest(BaseModel):
    service: str
    experiment_type: str  # PodFailure, StressChaos, NetworkLatency
    tenant_id: Optional[str] = "tenant_a"

class RemediateServiceRequest(BaseModel):
    service: str

@router.get("/status")
def get_mesh_status() -> List[Dict[str, Any]]:
    """Returns the live health status of all 5 real local microservices."""
    mgr = get_mesh_manager()
    return mgr.get_mesh_status()

@router.get("/metrics")
def get_mesh_prometheus_metrics():
    """Returns raw Prometheus metric exposition format scraped from all running microservices."""
    from fastapi.responses import PlainTextResponse
    mgr = get_mesh_manager()
    raw_metrics = mgr.scrape_prometheus_metrics()
    return PlainTextResponse(content=raw_metrics, media_type="text/plain; version=0.0.4")

@router.get("/telemetry/summary")
def get_mesh_telemetry_summary():
    """Returns aggregated real metrics for dashboard charts."""
    mgr = get_mesh_manager()
    status_list = mgr.get_mesh_status()
    
    total_cpu = sum(s.get("cpu_percent", 0.0) for s in status_list)
    total_mem = sum(s.get("memory_mb", 0.0) for s in status_list)
    healthy_count = sum(1 for s in status_list if s.get("status") == "HEALTHY")
    crashed_count = sum(1 for s in status_list if s.get("status") == "CRASHED")

    return {
        "services_total": len(status_list),
        "services_healthy": healthy_count,
        "services_crashed": crashed_count,
        "total_cpu_percent": round(total_cpu, 1),
        "total_memory_mb": round(total_mem, 1),
        "services": status_list
    }

@router.post("/chaos/inject")
def inject_real_chaos(req: ChaosFaultRequest):
    """Executes a REAL fault injection against a local microservice process."""
    mgr = get_mesh_manager()
    result = mgr.inject_real_fault(req.service, req.experiment_type)
    return result

@router.post("/remediate")
def remediate_service(req: RemediateServiceRequest):
    """Executes real self-healing restart or reset of the local microservice."""
    mgr = get_mesh_manager()
    result = mgr.remediate_service(req.service)
    return result

@router.post("/start-all")
def start_all_mesh_services():
    """Ensure all microservices are running."""
    mgr = get_mesh_manager()
    mgr.start_all()
    return {"status": "SUCCESS", "message": "All real microservices started."}
