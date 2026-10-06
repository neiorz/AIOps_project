"""AIOps Platform - Streamlit dashboard (Track T6, steps 1-2)."""
import os
import httpx
import streamlit as st

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


st.title("📊 AIOps Platform — Autonomous Incident Detection & Self-Healing")
st.caption(f"API: `{API_BASE}`  ·  data refreshes every 30 s")

# step 2: five tabs
tabs = st.tabs(["📈 Overview", "🚨 Incidents", "🤖 Anomalies", "🧠 RCA", "💡 Insights"])

# --- Overview -------------------------------------------------------------
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
        with st.expander("Full scorecard JSON"):
            st.json(sc)

# --- Incidents ------------------------------------------------------------
with tabs[1]:
    inc = get("/incidents")
    if "_error" in inc:
        st.error(inc["_error"])
    elif not inc:
        st.info("No incidents yet. Run `make chaos-inject` to create one.")
    else:
        st.dataframe(inc, use_container_width=True)

# --- Anomalies ------------------------------------------------------------
with tabs[2]:
    an = get("/anomalies")
    if "_error" in an:
        st.error(an["_error"])
    else:
        st.caption(f"Model status: **{an['status']}** — owned by {an['owner']}")
        if not an.get("anomalies"):
            st.info("No anomalies yet (T5 will populate this).")
        else:
            st.dataframe(an["anomalies"], use_container_width=True)

# --- RCA ------------------------------------------------------------------
with tabs[3]:
    h = get("/llm/health")
    if "_error" in h:
        st.error(h["_error"])
    else:
        st.write(f"**LLM:** `{h['model']}` @ `{h['host']}` — status **{h['status']}**")
        st.caption("T3 will fill this tab with generated root-cause narratives.")

# --- Insights -------------------------------------------------------------
with tabs[4]:
    st.write("Placeholder — step 5 adds the plain-language summary.")