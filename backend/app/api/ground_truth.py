"""
Ground Truth API (owned by Track T2).

PHASE_PLAN T2 step 3: expose the ledger over HTTP so the dashboard and the
evaluation harness can read experiment history *across restarts*, which is
exactly what the in-memory list could not do.

    GET /api/v1/ground-truth          -> every experiment, newest first
    GET /api/v1/ground-truth?limit=10 -> most recent N

Reads the database first and falls back to the live in-memory ledger if the
store is empty, so a process that has just started still reports what it
injected in this session. ``source`` says which one answered.
"""
from typing import Any, Dict

from fastapi import APIRouter, Query

from app.db.repo import count_ground_truth, load_ground_truth
from app.db.session import backend_name

router = APIRouter(prefix="/ground-truth", tags=["Ground Truth"])


@router.get("", response_model=Dict[str, Any])
def list_ground_truth(
    limit: int = Query(default=100, ge=1, le=1000),
) -> Dict[str, Any]:
    entries = load_ground_truth(limit=limit)
    source = "database"

    if not entries:
        # Nothing persisted (or the store failed): report the live ledger
        # rather than pretending there has never been an experiment.
        from app.api.chaos import ground_truth_ledger
        entries = ground_truth_ledger[:limit]
        source = "memory"

    return {
        "count": len(entries),
        "total_persisted": count_ground_truth(),
        "source": source,
        "backend": backend_name(),
        "entries": entries,
    }
