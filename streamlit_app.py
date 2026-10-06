"""AIOps Platform - Streamlit dashboard (Track T6, steps 1-5)."""
import os
from collections import Counter

import httpx
import streamlit as st
import plotly.graph_objects as go

API_BASE = os.getenv("STREAMLIT_API_BASE", "http://localhost:8000/api/v1")

st.set_page_config(page_title="AIOps Platform", page_icon="📊", layout="wide")


@st.cache_data(ttl=30)          # step 4: auto-refresh without restarting
def get(path: str):
    """Fetch from the backend; return {'_error': ...} instead of crashing."""
    try:
        r = httpx.get(f"{API_BASE}{path}", timeout=5.0)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"_error": str(exc)}


def bar(x, y, title, note=""):
    """Tiny bar-chart helper so every figure looks the same."""
    fig = go.Figure(go.Bar(x=x, y=y, marker_color="#0f3460"))
    fig.update_layout(
        title={"text": title + (f"<br><sup>{note}</sup>" if note else ""),
               "x": 0.02, "xanchor": "left", "font": {"size": 14}},
        height=320, margin=dict(t=60, b=20, l=20, r=20),
        yaxis=dict(title=""), xaxis=dict(title=""),
    )
    return fig


st.title("📊 AIOps Platform — Autonomous Incident Detection & Self-Healing")
st.caption(f"API: `{API_BASE}`  ·  data refreshes every 30 s")

# step 2: five tabs
tabs = st.tabs(["📈 Overview", "🚨 Incidents", "🤖 Anomalies", "🧠 RCA", "💡 Insights"])

# --- Overview (steps 1-3) --------------------------------------------------
with tabs[0]:
    sc = get("/benchmarks/scorecard")
    if "_error" in sc:
        st.error(f"Cannot reach the API: `{sc['_error']}` — is `./run.sh dev-backend` running?")
    else:
        c1, c2, c3, c4 = st.columns(4)
        acc = sc.get("rca_accuracy_percentage")
        c1.metric("RCA accuracy", "n/a" if acc is None else f"{acc}%")
        c2.metric("Experiments", sc.get("experiments_evaluated", 0))
        c3.metric("Incidents", sc.get("incidents_created", 0))
        c4.metric("MTTD (s)", "n/a" if sc.get("mean_time_to_detect_seconds") is None
                  else sc["mean_time_to_detect_seconds"])

        st.divider()

        # --- step 3 chart 1: RCA accuracy gauge ---
        col_a, col_b = st.columns(2)
        with col_a:
            if acc is None:
                st.info(f"**No accuracy yet** — {sc.get('rca_accuracy_note', 'run a chaos experiment.')}")
            else:
                g = go.Figure(go.Indicator(
                    mode="gauge+number", value=acc,
                    title={"text": "RCA accuracy %", "font": {"size": 15}},
                    number={"suffix": "%"},
                    gauge={
                        "axis": {"range": [0, 100]},
                        "bar": {"color": "#0f3460"},
                        "steps": [
                            {"range": [0, 60], "color": "#ffe1e6"},
                            {"range": [60, 85], "color": "#fff4d6"},
                            {"range": [85, 100], "color": "#e3f6ea"},
                        ],
                        "threshold": {"line": {"color": "#e94560", "width": 4},
                                      "thickness": 0.85, "value": 85},
                    },
                ))
                g.update_layout(height=320, margin=dict(t=50, b=20, l=30, r=30))
                st.plotly_chart(g, width="stretch")

        # --- step 3 chart 2: telemetry tool usage (REAL counters) ---
        with col_b:
            tools = (sc.get("investigation_efficiency") or {}).get("tool_calls") or {}
            if any(tools.values()):
                st.plotly_chart(
                    bar(list(tools), list(tools.values()),
                        "Telemetry tool calls",
                        "live counters from tools/counters.py — includes failed attempts"),
                    width="stretch")
            else:
                st.info("**No tool calls yet** — the agent has not run an investigation.")

        # --- step 3 chart 3: alert compression pipeline ---
        col_c, col_d = st.columns(2)
        with col_c:
            raw, uniq, inc_n = (sc.get("raw_alerts_ingested", 0),
                                sc.get("unique_alert_ids", 0),
                                sc.get("incidents_created", 0))
            if raw or inc_n:
                st.plotly_chart(
                    bar(["Raw alerts", "Unique alert IDs", "Incidents"],
                        [raw, uniq, inc_n],
                        "Alert compression",
                        f"noise reduction: {sc.get('noise_reduction_percentage', 'n/a')}%"),
                    width="stretch")
            else:
                st.info("**No alerts ingested yet** — inject chaos to create some.")

        # --- step 3 chart 4: investigation efficiency ---
        with col_d:
            ie = sc.get("investigation_efficiency") or {}
            st.plotly_chart(
                bar(["Investigations", "Tool calls in RCA", "Total tool calls"],
                    [ie.get("investigations_completed", 0),
                     ie.get("tool_calls_in_rca", 0),
                     ie.get("total_tool_calls", 0)],
                    "Investigation efficiency",
                    f"avg tool calls per RCA: {ie.get('avg_tool_calls_per_rca', 0)}"),
                width="stretch")

        with st.expander("Full scorecard JSON"):
            st.json(sc)

# --- Incidents (step 3: status chart) --------------------------------------
with tabs[1]:
    inc = get("/incidents")
    if "_error" in inc:
        st.error(inc["_error"])
    elif not inc:
        st.info("No incidents yet. Run `make chaos-inject` to create one.")
    else:
        by_status = Counter(i.get("status", "UNKNOWN") for i in inc)
        st.plotly_chart(
            bar(list(by_status), list(by_status.values()), "Incidents by status"),
            width="stretch")
        by_svc = Counter(i.get("primary_service", "?") for i in inc)
        st.plotly_chart(
            bar(list(by_svc), list(by_svc.values()), "Incidents by service"),
            width="stretch")
        st.dataframe(inc, width="stretch")

# --- Anomalies (step 3: anomaly timeline) ----------------------------------
with tabs[2]:
    an = get("/anomalies")
    if "_error" in an:
        st.error(an["_error"])
    else:
        st.caption(f"Model status: **{an['status']}** — owned by {an['owner']}")
        pts = an.get("anomalies") or []
        if not pts:
            st.info("No anomalies yet (T5 will populate this).")
        else:
            normals = [p for p in pts if not p.get("is_anomaly")]
            bad = [p for p in pts if p.get("is_anomaly")]
            tl = go.Figure()
            if normals:
                tl.add_trace(go.Scatter(
                    x=[p.get("timestamp") for p in normals],
                    y=[p.get("anomaly_score") for p in normals],
                    mode="markers", name="Normal", marker=dict(color="#2eb872", size=8)))
            if bad:
                tl.add_trace(go.Scatter(
                    x=[p.get("timestamp") for p in bad],
                    y=[p.get("anomaly_score") for p in bad],
                    mode="markers", name="Anomaly", marker=dict(color="#e94560", size=12,
                                                                symbol="x")))
            tl.update_layout(title={"text": "Anomaly timeline", "x": 0.02, "xanchor": "left"},
                             height=340, margin=dict(t=55, b=20, l=20, r=20),
                             xaxis=dict(title="timestamp"), yaxis=dict(title="anomaly score"))
            st.plotly_chart(tl, width="stretch")
            st.dataframe(pts, width="stretch")

# --- RCA ------------------------------------------------------------------
with tabs[3]:
    h = get("/llm/health")
    if "_error" in h:
        st.error(h["_error"])
    else:
        st.write(f"**LLM:** `{h['model']}` @ `{h['host']}` — status **{h['status']}**")
        st.caption("T3 will fill this tab with generated root-cause narratives.")

# --- Insights (step 5) ------------------------------------------------------
with tabs[4]:
    sc = get("/benchmarks/scorecard")
    an = get("/anomalies")
    h = get("/llm/health")
    inc = get("/incidents")

    if "_error" in sc:
        st.error(f"Cannot reach the API: `{sc['_error']}`")
    else:
        ie = sc.get("investigation_efficiency") or {}
        tools = ie.get("tool_calls") or {}
        acc = sc.get("rca_accuracy_percentage")

        # ---------- track readiness: auto-derived from API signals ----------
        st.subheader("🚦 Track readiness")
        st.caption("Derived from live API responses — updates itself as tracks complete.")
        signals = [
            ("T1 Observability", tools.get("promql", 0) > 0,
             f"PromQL calls = {tools.get('promql', 0)} (needs > 0)"),
            ("T2 Persistence", sc.get("sla_protection_rate") is not None,
             f"sla_protection_rate = {sc.get('sla_protection_rate')} (needs a number)"),
            ("T3 Local LLM", h.get("status") not in (None, "STUB", "NOT_IMPLEMENTED"),
             f"llm status = {h.get('status')}"),
            ("T4 Agent tools", ie.get("avg_tool_calls_per_rca", 0) > 0,
             f"avg tool calls/RCA = {ie.get('avg_tool_calls_per_rca', 0)}"),
            ("T5 Anomaly detection", an.get("status") not in (None, "STUB", "NOT_IMPLEMENTED"),
             f"anomaly status = {an.get('status')}"),
            ("T6 Streamlit dashboard", True, "you are looking at it ✅"),
            ("T7 Chaos Mesh", None, "not auto-detectable — verify with `kubectl get crd`"),
        ]
        done = 0
        for name, state, note in signals:
            if state is True:
                done += 1
                st.success(f"**{name}** — {note}")
            elif state is None:
                st.warning(f"**{name}** — {note}")
            else:
                st.error(f"**{name}** — {note}")

        st.progress(done / len(signals),
                    text=f"{done} of {len(signals)} tracks complete")

        # ---------- the actual plain-language summary ----------
        st.subheader("📝 Summary")

        if not sc.get("experiments_evaluated"):
            st.info(
                f"**No chaos experiments evaluated yet.** {sc.get('rca_accuracy_note', '')}\n\n"
                "Run `make chaos-inject` a few times, then this panel will describe "
                "measured accuracy instead of saying 'no data'."
            )
        else:
            correct = sc.get("rca_correct", 0)
            total = sc.get("experiments_evaluated", 0)
            line = (f"The agent was evaluated on **{total}** chaos experiment(s) and "
                    f"diagnosed **{correct}** correctly — **{acc}%** accuracy.")
            if acc is not None and acc < 60:
                line += " ⚠️ That is below the 80% bar: the RCA needs better evidence or retrieval."
            elif acc is not None and acc >= 85:
                line += " ✅ Above the 85% target."
            st.markdown(line)

            mttd = sc.get("mean_time_to_detect_seconds")
            if mttd is not None:
                st.markdown(f"- **Mean time to detect:** {mttd} s per incident.")

        raw, inc_n = sc.get("raw_alerts_ingested", 0), sc.get("incidents_created", 0)
        if raw and inc_n:
            st.markdown(
                f"- **Alert noise:** {raw} raw alerts were folded into {inc_n} incident(s) "
                f"— **{sc.get('noise_reduction_percentage')}%** less noise to look at."
            )

        if ie.get("investigations_completed"):
            st.markdown(
                f"- **Investigation cost:** {ie['investigations_completed']} investigation(s), "
                f"avg **{ie.get('avg_tool_calls_per_rca')}** tool calls each."
            )

        if any(tools.values()):
            busiest = max(tools, key=tools.get)
            st.markdown(
                f"- **Most-used tool:** `{busiest}` ({tools[busiest]} calls). "
                + ("That count includes failed attempts, so a high number can mean the "
                   "backend was unreachable — not that the agent was busy."
                   if busiest == "k8s_api" and tools[busiest] > 100 else "")
            )

        # ---------- what is still missing ----------
        st.subheader("🧩 Still to come")
        pending = [n for n, s, _ in signals if s is False]
        if pending:
            st.markdown("Waiting on: " + ", ".join(f"**{p}**" for p in pending))
        else:
            st.success("All detectable tracks are complete.")
        if not inc:
            st.caption("No incidents in this session yet (the ledger is in-memory — "
                       "it resets when the backend restarts).")
