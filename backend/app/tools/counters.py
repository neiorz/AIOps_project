"""
Real, monotonic counters for AIOps tool usage and agent investigations.

These replace the previously hardcoded/fabricated values in the benchmark
scorecard. Every number reported by /api/v1/benchmarks/scorecard that describes
tool usage is derived from this module at read time -- nothing is invented.
"""
import threading
from typing import Dict, List

_LOCK = threading.Lock()

# Per-tool invocation counters (real, incremented at call time)
_TOOL_CALLS: Dict[str, int] = {
    "promql": 0,
    "logql": 0,
    "traceql": 0,
    "k8s_api": 0,
}

# Investigation bookkeeping (used for avg tool calls per RCA)
_INVESTIGATIONS: int = 0
_RCA_TOOL_CALLS: int = 0


def record_tool_call(tool: str) -> None:
    """Record one telemetry tool invocation."""
    with _LOCK:
        _TOOL_CALLS[tool] = _TOOL_CALLS.get(tool, 0) + 1


def record_investigation(tools_used: List[str]) -> None:
    """Record one completed agent investigation and the tools it consumed."""
    global _INVESTIGATIONS, _RCA_TOOL_CALLS
    with _LOCK:
        _INVESTIGATIONS += 1
        _RCA_TOOL_CALLS += len(tools_used)


def get_tool_call_ledger() -> Dict[str, int]:
    """Immutable snapshot of real per-tool counters."""
    with _LOCK:
        return dict(_TOOL_CALLS)


def get_investigation_stats() -> Dict[str, int]:
    """Immutable snapshot of investigation counters."""
    with _LOCK:
        return {
            "investigations_completed": _INVESTIGATIONS,
            "tool_calls_in_rca": _RCA_TOOL_CALLS,
        }


def reset_counters() -> None:
    """Reset all counters (tests only)."""
    global _INVESTIGATIONS, _RCA_TOOL_CALLS
    with _LOCK:
        for key in _TOOL_CALLS:
            _TOOL_CALLS[key] = 0
        _INVESTIGATIONS = 0
        _RCA_TOOL_CALLS = 0
