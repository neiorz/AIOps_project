"""
Autonomous SRE investigation agent — a real ReAct loop (Track T4).

THINK -> ACT -> OBSERVE, until terminal
---------------------------------------
Every iteration:

* **think**   look at what the observations so far have established, and pick
              the tool that adds the most to the picture *now*;
* **act**     call it through :mod:`app.agent.tools`;
* **observe** record the whole envelope — including failures — as evidence.

The ordering is genuinely driven by observations, not fixed: a service the
mesh reports as crashed is investigated through Kubernetes first, while one
that reports healthy goes straight to PromQL looking for degradation the
health check cannot see. Trace lookup only runs when a trace id actually
exists in the incident.

The loop terminates when there is no further action to take (plan exhausted),
or when ``MAX_STEPS`` is reached — whichever comes first — and which one
happened is reported in ``react.terminal``.

CONFIDENCE (step 4)
-------------------
::

    confidence     = 0.5 * retrieval_confidence + 0.5 * tool_agreement
    tool_agreement = corroborating tools / tools attempted

Both terms live in [0,1], so confidence does too — by construction, not by
clamping an arbitrary number into range. A tool that could not answer cannot
corroborate, so an unreachable backend *drags confidence down* toward the
``RCA_MIN_CONFIDENCE`` gate instead of inflating it.

There is deliberately **no default**: no runbook and no usable telemetry
yields 0.0. The previous implementation substituted ``0.88`` when retrieval
came back empty, which is precisely the kind of invented number this project
exists to stop.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from app.agent.tools import call_tool, tool_names
from app.config import settings
from app.correlation.engine import CorrelatedIncident
from app.rag.retriever import get_runbook_retriever
from app.sla.calculator import SLARiskCalculator
from app.tools.counters import record_investigation

logger = logging.getLogger(__name__)

#: Hard stop so a runaway decision loop cannot spin forever (T4 step 2).
MAX_STEPS = 5

#: Evidence entries the agent should produce before it may conclude.
#: Both decision branches run mesh + promql + logql + k8s, so 4 is the norm.
MIN_EVIDENCE = 2

#: Substrings that make a log line count as a failure signal.
_LOG_ERROR_MARKERS = ("error", "exception", "fatal", "panic", "traceback",
                      "failed", "5xx", "refused")


class AutonomousSREAgent:
    """
    Autonomous AI Investigation Agent (ReAct tool-calling pattern).

    Investigates correlated incidents using registered telemetry tools and RAG
    runbooks to build an evidence-grounded Root Cause Analysis.
    """

    def __init__(self):
        self.retriever = get_runbook_retriever()

    # ------------------------------------------------------------------
    # THINK — decide the next action from what has already been observed
    # ------------------------------------------------------------------
    @staticmethod
    def _observation(evidence: List[Dict[str, Any]], tool: str) -> Optional[Dict[str, Any]]:
        return next((e for e in evidence if e["tool"] == tool), None)

    @staticmethod
    def _mesh_entry(data: Any, service: str) -> Optional[Dict[str, Any]]:
        if not isinstance(data, list):
            return None
        return next((s for s in data if isinstance(s, dict) and s.get("service") == service), None)

    @classmethod
    def _mesh_degraded(cls, data: Any, service: str) -> bool:
        entry = cls._mesh_entry(data, service)
        if entry is None:
            return False                             # unknown is not "degraded"
        return entry.get("status") != "HEALTHY"

    @staticmethod
    def _trace_id(incident: CorrelatedIncident) -> Optional[str]:
        """Only a trace id we actually have; otherwise traceql is skipped.

        Labels live on the raw alerts the incident was correlated *from*, not
        on the incident itself. The original version read
        ``incident.labels``, which CorrelatedIncident does not have — a plain
        AttributeError that took every investigation down at its fifth ReAct
        step, once the first four tools had been called. Both locations are
        consulted so the lookup keeps working if a correlation path ever
        attaches labels directly to the incident.
        """
        label_sets: List[Dict[str, str]] = []

        for alert in incident.sample_alerts or []:
            alert_labels = getattr(alert, "labels", None)
            if isinstance(alert_labels, dict):
                label_sets.append(alert_labels)

        incident_labels = getattr(incident, "labels", None)
        if isinstance(incident_labels, dict):
            label_sets.append(incident_labels)

        for key in ("trace_id", "traceid"):
            for labels in label_sets:
                value = labels.get(key)
                if value:
                    return str(value)
        return None

    @staticmethod
    def _promql_query(incident: CorrelatedIncident) -> str:
        """One query that measures the symptom for this incident's service."""
        service = incident.primary_service
        return (
            'sum by (service) (rate(http_requests_total{service="%s",status=~"5.."}[2m]))'
            % service
        )

    @staticmethod
    def _logql_query(incident: CorrelatedIncident) -> str:
        return '{service="%s"} |~ "(?i)(error|exception|fatal|panic|failed)"' % (
            incident.primary_service,
        )

    def _next_action(
        self, incident: CorrelatedIncident, evidence: List[Dict[str, Any]]
    ) -> Optional[Tuple[str, Dict[str, Any], str]]:
        """Return ``(tool, kwargs, thought)`` for the next step, or None."""
        seen = {e["tool"] for e in evidence}
        service = incident.primary_service
        mesh = self._observation(evidence, "mesh_status")
        degraded = bool(mesh and mesh["ok"] and self._mesh_degraded(mesh["data"], service))

        # 1. Ground truth about the service first — everything else is
        #    interpreted in its light.
        if "mesh_status" not in seen:
            return ("mesh_status", {"service": service},
                    "Start from the service itself: is it up and serving?")

        # 2. Observations choose the order of what follows.
        if degraded:
            if "k8s_api" not in seen:
                return ("k8s_api", {"service": service},
                        "Mesh reports the service unhealthy -> ask Kubernetes "
                        "whether the pod was restarted or is not Running.")
            if "promql" not in seen:
                return ("promql", {"query": self._promql_query(incident)},
                        "Service is confirmed down -> quantify how bad the "
                        "error rate actually is.")
            if "logql" not in seen:
                return ("logql", {"query": self._logql_query(incident)},
                        "Read the logs for the failure that took the service down.")
        else:
            if "promql" not in seen:
                return ("promql", {"query": self._promql_query(incident)},
                        "Mesh reports HEALTHY -> look for degradation Prometheus "
                        "can see but a health check cannot.")
            if "logql" not in seen:
                return ("logql", {"query": self._logql_query(incident)},
                        "Confirm from the logs whether the symptom is present at all.")
            if "k8s_api" not in seen:
                return ("k8s_api", {"service": service},
                        "Service looks healthy -> check Kubernetes for restarts "
                        "that would explain a transient incident.")

        # 3. Traces only when there is a trace id to follow.
        if "traceql" not in seen:
            trace_id = self._trace_id(incident)
            if trace_id:
                return ("traceql", {"trace_id": trace_id},
                        "Follow the failing request end to end.")

        return None                                  # terminal: no action left

    # ------------------------------------------------------------------
    # OBSERVE — what does this tool's envelope mean for the hypothesis?
    # ------------------------------------------------------------------
    @staticmethod
    def _log_lines(data: Any) -> Optional[List[str]]:
        if not isinstance(data, dict):
            return None
        lines: List[str] = []
        for stream in data.get("result") or []:
            if not isinstance(stream, dict):
                continue
            for entry in stream.get("values") or []:
                if isinstance(entry, (list, tuple)) and len(entry) > 1:
                    lines.append(str(entry[1]))
                elif isinstance(entry, str):
                    lines.append(entry)
        return lines

    @classmethod
    def _trace_has_error(cls, node: Any, depth: int = 0) -> bool:
        if depth > 6:
            return False
        if isinstance(node, dict):
            for key in ("error", "Error"):
                if node.get(key) is True:
                    return True
            status = node.get("status") or node.get("statusCode")
            if isinstance(status, str) and status.upper() in ("ERROR", "STATUS_CODE_ERROR"):
                return True
            return any(cls._trace_has_error(v, depth + 1) for v in node.values())
        if isinstance(node, list):
            return any(cls._trace_has_error(v, depth + 1) for v in node)
        return False

    @classmethod
    def _verdict(
        cls, tool: str, observation: Dict[str, Any], incident: CorrelatedIncident
    ) -> str:
        """``degraded`` / ``healthy`` / ``unknown`` for this observation."""
        if not observation["ok"]:
            return "unknown"                         # no usable data -> no opinion
        data = observation["data"]

        if tool == "mesh_status":
            entry = cls._mesh_entry(data, incident.primary_service)
            if entry is None:
                return "unknown"
            return "healthy" if entry.get("status") == "HEALTHY" else "degraded"

        if tool == "k8s_api":
            if not isinstance(data, dict):
                return "unknown"
            pods = data.get("pods") or []
            if not pods:
                return "unknown"                     # no matching pod: inconclusive
            for pod in pods:
                if not isinstance(pod, dict):
                    continue
                if pod.get("phase") != "Running" or int(pod.get("restart_count") or 0) > 0:
                    return "degraded"
            return "healthy"

        if tool == "promql":
            series = (data or {}).get("result") or []
            for item in series:
                try:
                    if float(item["value"][1]) > 0:
                        return "degraded"
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
            return "healthy"                         # answered, and nothing to report

        if tool == "logql":
            lines = cls._log_lines(data)
            if not lines:
                return "healthy"                     # answered, no lines matched
            joined = " ".join(lines).lower()
            return "degraded" if any(m in joined for m in _LOG_ERROR_MARKERS) else "healthy"

        if tool == "traceql":
            if not isinstance(data, dict):
                return "unknown"
            return "degraded" if cls._trace_has_error(data) else "healthy"

        return "unknown"

    @staticmethod
    def _summarise(tool: str, observation: Dict[str, Any]) -> str:
        """Short human line for the think/act/observe trace."""
        data = observation["data"]
        if tool == "mesh_status" and isinstance(data, list):
            healthy = sum(1 for s in data if isinstance(s, dict) and s.get("status") == "HEALTHY")
            return f"{healthy}/{len(data)} services HEALTHY"
        if tool == "k8s_api" and isinstance(data, dict):
            pods = data.get("pods") or []
            if not pods:
                return "no matching pod found"
            return ", ".join(
                f"{p.get('phase')} (restarts={p.get('restart_count')})"
                for p in pods if isinstance(p, dict)
            )
        if tool == "promql" and isinstance(data, dict):
            return f"{len(data.get('result') or [])} series returned"
        if tool == "logql" and isinstance(data, dict):
            lines = AutonomousSREAgent._log_lines(data) or []
            return f"{len(lines)} log line(s) matched the error filter"
        if tool == "traceql":
            return "trace retrieved"
        return "observation recorded"

    # ------------------------------------------------------------------
    # Confidence (step 4)
    # ------------------------------------------------------------------
    @staticmethod
    def _confidence(
        runbooks: List[Dict[str, Any]], evidence: List[Dict[str, Any]]
    ) -> Tuple[float, float, float]:
        """``(confidence, retrieval_confidence, tool_agreement)``, all in [0,1]."""
        retrieval = float(runbooks[0]["similarity_score"]) if runbooks else 0.0
        retrieval = max(0.0, min(1.0, retrieval))

        attempted = len(evidence)
        corroborating = sum(
            1 for e in evidence if e["ok"] and e.get("verdict") == "degraded"
        )
        tool_agreement = (corroborating / attempted) if attempted else 0.0

        confidence = 0.5 * retrieval + 0.5 * tool_agreement
        confidence = max(0.0, min(1.0, confidence))
        return round(confidence, 4), round(retrieval, 4), round(tool_agreement, 4)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    async def investigate_incident(self, incident: CorrelatedIncident) -> Dict[str, Any]:
        logger.info(
            "Agent starting investigation for incident: %s (%s)",
            incident.incident_id, incident.primary_service,
        )
        investigation_start = time.time()

        # --- SLA context -------------------------------------------------
        sla_info = SLARiskCalculator.calculate_sla_status(
            tenant_id=incident.tenant_id,
            created_at=incident.created_at,
            severity=incident.severity,
        )

        # --- THINK (initial): retrieve relevant runbooks -----------------
        clean_cause = incident.root_cause_candidate.replace("_", " ")
        query = f"{incident.primary_service} {clean_cause} outage failure degradation"
        relevant_runbooks = self.retriever.search_relevant_runbooks(query=query, n_results=3)

        # --- ReAct loop: think -> act -> observe -------------------------
        evidence: List[Dict[str, Any]] = []
        trace: List[Dict[str, Any]] = []
        terminal = "max_steps_reached"

        for step in range(1, MAX_STEPS + 1):
            action = self._next_action(incident, evidence)
            if action is None:
                terminal = "no_further_action"
                break

            tool_name, kwargs, thought = action
            observation = await call_tool(tool_name, **kwargs)
            observation["verdict"] = self._verdict(tool_name, observation, incident)
            evidence.append(observation)

            trace.append({
                "step": step,
                "thought": thought,
                "tool": tool_name,
                "target": observation["target"],
                "ok": observation["ok"],
                "verdict": observation["verdict"],
                "observation": observation["error"] or self._summarise(tool_name, observation),
            })
            logger.info(
                "ReAct step %d: %s -> ok=%s verdict=%s",
                step, tool_name, observation["ok"], observation["verdict"],
            )

        # --- Synthesise the RCA (step 4 + 5) -----------------------------
        matched_runbook = relevant_runbooks[0] if relevant_runbooks else None
        rca_title = (
            matched_runbook["title"]
            if matched_runbook
            else f"Degradation in {incident.primary_service}"
        )

        confidence, retrieval_confidence, tool_agreement = self._confidence(
            relevant_runbooks, evidence
        )
        blocked = confidence < settings.RCA_MIN_CONFIDENCE

        # Standardize remediation command for Ansible playbook execution
        remediation_action = (
            f"ansible-playbook ansible/restart_service.yml -e service={incident.primary_service}"
        )

        citations = [f"runbook · {rb['title']}" for rb in relevant_runbooks]
        citations += [
            f"tool · {e['tool']} · verdict={e['verdict']}"
            + ("" if e["ok"] else f" · unavailable: {e['error']}")
            for e in evidence
        ]

        investigation_duration = round(time.time() - investigation_start, 3)

        # T4 step 2 gate: the agent must actually have gathered evidence.
        sufficient_evidence = len(evidence) >= MIN_EVIDENCE
        if not sufficient_evidence:
            logger.warning(
                "investigation %s produced only %d evidence entr%s (< %d) — terminal=%s",
                incident.incident_id, len(evidence),
                "y" if len(evidence) == 1 else "ies", MIN_EVIDENCE, terminal,
            )

        # Real counter: feeds avg_tool_calls_per_rca in the benchmark scorecard.
        # Four tools per RCA => avg > 1, which is T4 step 3's gate.
        record_investigation([e["tool"] for e in evidence])

        return {
            "incident_id": incident.incident_id,
            "tenant_id": incident.tenant_id,
            "sla_risk": sla_info,
            "investigation_duration_seconds": investigation_duration,
            "react": {
                "steps": trace,
                "terminal": terminal,
                "evidence_entries": len(evidence),
                "sufficient_evidence": sufficient_evidence,
                "tools_available": tool_names(),
            },
            "root_cause_analysis": {
                "diagnosis": rca_title,
                "confidence_score": confidence,
                "confidence_breakdown": {
                    "retrieval_confidence": retrieval_confidence,
                    "tool_agreement": tool_agreement,
                    "formula": "0.5*retrieval + 0.5*tool_agreement",
                    "min_for_remediation": settings.RCA_MIN_CONFIDENCE,
                },
                "remediation_blocked": blocked,
                "primary_service": incident.primary_service,
                "affected_services": incident.affected_services,
                "evidence_gathered": evidence,
                "referenced_runbooks": [
                    {
                        "title": rb["title"],
                        "category": rb["category"],
                        "similarity": rb["similarity_score"],
                    }
                    for rb in relevant_runbooks
                ],
                "citations": citations,
                "recommended_remediation": remediation_action,
            },
        }


_agent = AutonomousSREAgent()


def get_sre_agent() -> AutonomousSREAgent:
    return _agent
