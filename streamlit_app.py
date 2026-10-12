"""AIOps Platform — Streamlit operator dashboard."""
import os
from collections import Counter
from datetime import datetime

import httpx
import streamlit as st
import plotly.graph_objects as go

API_BASE = os.getenv("STREAMLIT_API_BASE", "http://localhost:8000/api/v1")

st.set_page_config(page_title="AIOps Platform", page_icon="📊", layout="wide")


@st.cache_data(ttl=30)          # auto-refresh without restarting
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


def pct(value, digits=2):
    """Fraction -> '99.49%'; None -> 'n/a' (an undefined metric is not a zero)."""
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def when(ts):
    """Unix timestamp -> readable string; None -> 'unknown'."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "unknown"


st.title("📊 AIOps Platform — Autonomous Incident Detection & Self-Healing")
st.caption(f"API: `{API_BASE}`  ·  data refreshes every 30 s")

tabs = st.tabs(["📈 Overview", "🚨 Incidents", "🤖 Anomalies",
                "🏅 Models", "🧠 RCA", "💡 Insights"])

# --- Overview --------------------------------------------------------------
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

        # --- RCA accuracy gauge ---
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

        # --- telemetry tool usage (live counters) ---
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

        # --- alert compression pipeline ---
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

        # --- investigation efficiency ---
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

# --- Incidents -------------------------------------------------------------
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

# --- Anomalies -------------------------------------------------------------
with tabs[2]:
    an = get("/anomalies")
    if "_error" in an:
        st.error(an["_error"])
    else:
        st.caption(f"Model status: **{an['status']}** — {an.get('message', '')}")
        pts = an.get("anomalies") or []
        if not pts:
            st.info("No anomalies detected yet.")
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

# --- Models: measured accuracy & rating for every model in the stack -------
with tabs[3]:
    ev = get("/anomalies/evaluation")
    sc = get("/benchmarks/scorecard")
    h = get("/llm/health")

    if "_error" in ev:
        st.error(f"Cannot reach the API: `{ev['_error']}`")
    else:
        st.subheader("🏅 Model scorecards")
        st.caption("Every number here was computed by the platform itself: "
                   "a hold-out evaluation for the detector, the ground-truth "
                   "ledger for the agent.")

        # ---- 1. anomaly detector ----
        st.markdown("#### Anomaly detector — Isolation Forest (metrics only)")
        if not ev.get("available"):
            st.info(ev.get("note", "No evaluation recorded yet."))
        else:
            if ev.get("applies_to_production") is False:
                st.warning("These metrics were recorded for an earlier "
                           "production model — rerun `python -m app.ml.retrain "
                           "--force` to refresh them.")
            m = ev.get("metrics") or {}
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Accuracy", pct(m.get("accuracy")))
            c2.metric("Precision", pct(m.get("precision")))
            c3.metric("Recall", pct(m.get("recall")))
            c4.metric("F1", pct(m.get("f1")))
            c5.metric("AUROC", pct(m.get("auroc")))

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("TP", m.get("tp", "n/a"))
            c2.metric("FP", m.get("fp", "n/a"))
            c3.metric("TN", m.get("tn", "n/a"))
            c4.metric("FN", m.get("fn", "n/a"))

            pairs = [(label, m.get(key)) for label, key in (
                ("Accuracy", "accuracy"), ("Precision", "precision"),
                ("Recall", "recall"), ("F1", "f1"), ("AUROC", "auroc"))]
            plotted = [(n, v * 100) for n, v in pairs if v is not None]
            if plotted:
                st.plotly_chart(
                    bar([n for n, _ in plotted],
                        [round(v, 2) for _, v in plotted],
                        "Detector metrics on held-out data",
                        f"{ev.get('decision_view', 'faulted-service')} view: "
                        f"{m.get('n_normal_test', 0)} healthy + "
                        f"{m.get('n_chaos_test', 0)} fault rows the model "
                        f"never trained on"),
                    width="stretch")

            gates = ev.get("gates") or {}
            lg = gates.get("live_flag_rate") or {}
            dg = gates.get("detection") or {}
            gate_rows = [
                ("Live safety", lg.get("passed"),
                 f"flags {pct(lg.get('value'))} of held-out live traffic "
                 f"(limit {pct(lg.get('limit'))})"),
                ("Detection", dg.get("passed"),
                 f"faulted-service AUROC {dg.get('auroc')} "
                 f"(min {dg.get('auroc_min')}), recall {dg.get('recall')} "
                 f"(min {dg.get('recall_min')})"),
            ]
            passed = 0
            for label, ok, detail in gate_rows:
                if ok:
                    passed += 1
                    st.success(f"**{label} gate** — {detail}")
                else:
                    st.error(f"**{label} gate** — {detail}")
            st.markdown(
                f"**Rating: {passed}/{len(gate_rows)} promotion gates passed**"
                + (" — production-ready ✅" if passed == len(gate_rows)
                   else " — the model cannot be promoted until all gates pass.")
            )

            views = ev.get("views") or []
            if views:
                view_rows = [{
                    "view": v.get("view"),
                    "rows": v.get("rows"),
                    "accuracy": pct((v.get("candidate") or {}).get("accuracy")),
                    "AUROC": pct((v.get("candidate") or {}).get("auroc")),
                    "recall": pct((v.get("candidate") or {}).get("recall")),
                } for v in views]
                st.dataframe(view_rows, width="stretch")
                st.caption(
                    "All three hold-out views. The pooled view includes the "
                    "healthy bystanders of a chaos run — scoring them at "
                    "chance is correct behaviour, not a defect. The headline "
                    "above is the faulted-service view, which is also what "
                    "the promotion decision is judged on."
                )

            with st.expander("Model card — what was measured, and how"):
                feats = ev.get("features") or []
                cal = ev.get("calibration") or {}
                st.markdown(f"- **Algorithm:** `{ev.get('algorithm')}` · "
                            f"contamination = {ev.get('contamination')}")
                st.markdown(f"- **Trained:** {when(ev.get('trained_at'))} on "
                            f"**{ev.get('n_samples')}** rows")
                st.markdown(f"- **Features ({len(feats)}):** "
                            + ", ".join(f"`{f}`" for f in feats))
                if cal:
                    st.markdown(
                        f"- **Calibration:** target {pct(cal.get('target_rate'))} "
                        f"flag rate → measured {pct(cal.get('actual_rate'))} on "
                        f"{cal.get('n_rows')} live rows "
                        f"({cal.get('flagged')} flagged)"
                    )
                st.markdown(f"- **Promotion decision:** {ev.get('reason')}")
                st.caption(
                    f"evaluated {when(ev.get('evaluated_at'))} · "
                    f"decision view: {ev.get('decision_view')}"
                )

        st.divider()

        # ---- 2. log sequence detector ----
        st.markdown("#### Log sequence detector — DeepLog-style template "
                    "transitions")
        lev = ev.get("logs") or {}
        if not lev.get("available"):
            st.info(lev.get("note", "No log model trained yet."))
        else:
            held = lev.get("held_out") or {}
            fault = (lev.get("anomaly_sets") or [{}])[0]
            services = ", ".join(
                f"{s} {n}" for s, n in (lev.get("services") or {}).items())
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Next-event accuracy", pct(held.get("next_event_accuracy")))
            c2.metric("False-alarm rate", pct(held.get("flagged_rate")))
            c3.metric("Fault-window detection", pct(fault.get("flagged_rate")))
            c4.metric("Templates mined", lev.get("n_templates", "n/a"))
            st.caption(
                f"Trained on real pod logs ({services}). The false-alarm rate "
                f"is measured on held-out healthy traffic"
                + (f"; detection on {fault.get('events')} events captured "
                   f"while a fault was active, from `{fault.get('source')}`."
                   if fault else ".")
            )

            flagged = fault.get("flagged_templates") or {}
            if flagged:
                with st.expander(
                        f"What the detector flagged in the fault window "
                        f"({sum(flagged.values())} events)"):
                    for text, count in flagged.items():
                        st.markdown(f"- **{count}×** `{text}`")
                    st.caption(
                        "`<unseen template>` lines are shapes never seen in "
                        "training — the fault signature itself. Ordinary "
                        "request lines appear too: they follow a flagged line, "
                        "so the model's context is already abnormal — the "
                        "detector surfaces the whole disrupted interval, not "
                        "just the one bad line."
                    )

            with st.expander("Model card — how the log detector works"):
                st.markdown(
                    f"- **Algorithm:** `{lev.get('algorithm', 'LogTemplateTransitions')}` "
                    f"— DeepLog-style next-template prediction: log lines are "
                    f"mined into templates (identifiers become `<*>`), and an "
                    f"event is flagged when its actual next template is not "
                    f"among the model's top-k predictions. Predictions are "
                    f"empirical transition counts — no neural net."
                )
                st.markdown(
                    f"- **Window / top-k:** {lev.get('window')} / "
                    f"{lev.get('top_k')} · trained {when(lev.get('trained_at'))} "
                    f"on {lev.get('train_events')} events · held-out "
                    f"{lev.get('held_out_events')} events "
                    f"(corpus: {lev.get('corpus')})"
                )
                st.markdown(
                    "- **Blind spot:** a fault with no log signature (silent "
                    "CPU burn, pure packet delay) is invisible to this "
                    "detector — the metrics detector above covers those."
                )
                st.markdown(
                    "- **Scale-up path:** swap the count-based transitions for "
                    "an LSTM (DeepLog proper) or LogBERT when log volume "
                    "grows — same templates, same evaluation harness."
                )

        st.divider()

        # ---- 3. root-cause agent ----
        st.markdown("#### Root-cause agent — ReAct + retrieval")
        if "_error" in sc:
            st.error(sc["_error"])
        else:
            acc = sc.get("rca_accuracy_percentage")
            c1, c2, c3 = st.columns(3)
            c1.metric("Diagnosis accuracy", "n/a" if acc is None else f"{acc}%")
            c2.metric("Experiments evaluated", sc.get("experiments_evaluated", 0))
            c3.metric("MTTD (s)", "n/a" if sc.get("mean_time_to_detect_seconds") is None
                      else sc["mean_time_to_detect_seconds"])
            st.caption("Measured against the ground-truth ledger: what chaos "
                       "was actually injected vs what the agent diagnosed. "
                       "Target: 85%.")

        st.divider()

        # ---- 4. narrative LLM ----
        st.markdown("#### Narrative LLM — local Ollama")
        if "_error" in h:
            st.error(h["_error"])
        else:
            st.write(f"`{h.get('model')}` @ `{h.get('host')}` — status "
                     f"**{h.get('status')}**")
            st.caption("Writes the incident narrative only. By design it "
                       "never supplies a gate number or a confidence — those "
                       "come from retrieval agreement, so a creative answer "
                       "cannot raise a score.")

        st.divider()

        # ---- 5. vector retriever ----
        st.markdown("#### Vector retriever — ChromaDB")
        st.write("`all-MiniLM-L6-v2` embeddings over runbooks, topologies "
                 "and post-mortems (runs locally)")
        correct = None if "_error" in sc else sc.get("rca_correct")
        st.caption(
            "No independent accuracy metric by design — retrieval quality "
            "surfaces in the agent's diagnosis accuracy above"
            + (f" ({correct} of {sc.get('experiments_evaluated')} experiments "
               f"found the right evidence)." if correct is not None else ".")
        )

# --- RCA -------------------------------------------------------------------
with tabs[4]:
    h = get("/llm/health")
    if "_error" in h:
        st.error(h["_error"])
    else:
        st.write(f"**LLM:** `{h['model']}` @ `{h['host']}` — status **{h['status']}**")
        st.caption(
            "Live probe of the local Ollama server. "
            "Generation: `POST /api/v1/llm/rca`."
        )

# --- Insights --------------------------------------------------------------
with tabs[5]:
    sc = get("/benchmarks/scorecard")
    an = get("/anomalies")
    h = get("/llm/health")
    inc = get("/incidents")
    cm = get("/chaos/chaos-mesh")   # can this deployment really inject?

    if "_error" in sc:
        st.error(f"Cannot reach the API: `{sc['_error']}`")
    else:
        ie = sc.get("investigation_efficiency") or {}
        tools = ie.get("tool_calls") or {}
        acc = sc.get("rca_accuracy_percentage")

        # ---------- capability status: auto-derived from API signals ----------
        st.subheader("🟢 Platform capabilities")
        st.caption("Derived from live API responses — reflects this running deployment.")
        signals = [
            ("Observability pipelines", tools.get("promql", 0) > 0,
             f"PromQL queries served = {tools.get('promql', 0)} (needs > 0)"),
            ("Durable incident store", sc.get("sla_protection_rate") is not None,
             f"sla_protection_rate = {sc.get('sla_protection_rate')} (needs a number)"),
            ("Local LLM diagnostics", h.get("status") not in (None, "STUB", "NOT_IMPLEMENTED"),
             f"llm status = {h.get('status')}"),
            # One tool call per investigation is the baseline; only the
            # autonomous loop makes a second, so the check is > 1 — a single
            # call must not light this up by itself.
            ("Autonomous investigation agent", ie.get("avg_tool_calls_per_rca", 0) > 1,
             f"avg tool calls/RCA = {ie.get('avg_tool_calls_per_rca', 0)} (needs > 1)"),
            ("Anomaly detection engine", an.get("status") not in (None, "STUB", "NOT_IMPLEMENTED"),
             f"anomaly status = {an.get('status')}"),
            ("Operator dashboard", True, "you are looking at it ✅"),
            # The injector reports this itself: None (API unreachable) renders
            # as the warning state, True as green, False as red — always with
            # the injector's own reason, so a red row explains itself.
            ("Chaos engineering integration", cm.get("available"),
             f"mode = {cm.get('mode', 'unknown')} — "
             f"{str(cm.get('reason', cm.get('_error', 'unreachable')))[:110]}"),
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
                    text=f"{done} of {len(signals)} capabilities live in this deployment")

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

        if not inc:
            st.caption("No incidents in this session yet — the investigation "
                       "ledger is in-memory and resets when the backend "
                       "restarts.")
