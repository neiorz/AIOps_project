# Phase 1–3 Execution Plan

Each row is one step. Work **top → bottom** inside a track, but tracks run **in parallel**.

**Done when** = the verification command in that row passes.

---

## ⚠️ Parallel-safety rules

1. **Only your track's rows touch your files.** Never edit a file you don't own (see ownership table).
2. Phase 0 froze `requirements.txt`, `config.py`, `.env.example`, `Dockerfile`, `Makefile`, `Jenkinsfile`, `main.py`. Need a new setting or dependency? **Ask first** — it gets batched into Phase 2.
3. Your branch must stay green: `make test` before every push.
4. Contract stubs (`api/llm.py`, `api/anomalies.py`) keep their **schemas** — only fill in the handler bodies.

### Quick reference

| | |
|---|---|
| Test | `make test` — **must stay 27/27** (add your own) |
| Lint | `make lint` |
| Server | `./run.sh dev-backend` → `http://localhost:8000` |
| Logs | `/tmp/opencode/aiops-server.log` |
| Stop server | `pkill -f "$(printf 'uvi''corn app.main')"` |
| Ports | backend `8000`, mesh `8081-8084` + `6380`, Streamlit `8501`, Prometheus `9091`, Loki `3101`, Tempo `3201` |

### File ownership (don't touch another track's file)

| File / dir | Owner |
|---|---|
| `app/tools/telemetry.py`, `observability/**` | **T1** |
| `app/db/**`, `app/api/ground_truth.py` | **T2** |
| `app/api/llm.py`, `app/rag/**` | **T3** |
| `app/agent/sre_agent.py`, `app/tools/**` (except telemetry) | **T4** |
| `app/api/anomalies.py`, `app/ml/**` | **T5** |
| `streamlit_app.py`, `ui/**` | **T6** |
| `app/api/chaos.py`, `k8s/`, `terraform/`, `ansible/` | **T7** |
| `app/correlation/engine.py` | **Phase 2** |
| `app/api/benchmarks.py` | **Phase 3** |
| `requirements.txt`, `config.py`, `main.py`, `Makefile`, `Dockerfile`, `Jenkinsfile` | **Frozen (Phase 0)** |

---

# Phase 1 — seven parallel tracks

## T1 · Observability (Prometheus / Loki / Tempo)

| # | Step | Tool / command | Done when |
|---|---|---|---|
| 1 | Create `observability/docker-compose.yml` — Prometheus `:9091`, Loki `:3101`, Tempo `:3201` | `docker compose` | `make observability-up` starts 3 containers |
| 2 | Add `/metrics` to FastAPI (Prometheus `Counter`/`Gauge` for incidents, tool calls) | `pip` already has `prometheus-client`; add route in a **new** `app/api/metrics.py` | `curl :8000/metrics` returns `# HELP` lines |
| 3 | Ship mesh + backend logs to Loki | `app/tools/counters.py` hook or `logging` handler | Log lines visible in Loki |
| 4 | Point `TelemetryTools` at the local backends | `.env` → `PROMETHEUS_LOCAL_URL` etc. | `query_prometheus` returns real JSON, not a fallback |
| 5 | Prove PromQL/LogQL/TraceQL are actually called | `pytest` asserting non-empty results | `promql`/`logql`/`traceql` counters > 0 in scorecard |

## T2 · Persistence (SQLAlchemy ground truth)

| # | Step | Tool / command | Done when |
|---|---|---|---|
| 1 | Create `app/db/session.py` + `app/db/models.py` (`Incident`, `GroundTruth`) | SQLAlchemy 2.1 | import succeeds |
| 2 | Store `ground_truth_ledger` in DB, keep in-memory list as read fallback | `POSTGRES_URL` → falls back to SQLite if unreachable | restart server → experiments still there |
| 3 | Add `GET /api/v1/ground-truth` | new router file | returns the ledger |
| 4 | Fill `sla_protection_rate` in the scorecard (currently `null`) | compute from persisted incidents | value is a number, not `null` |
| 5 | Migration script | `alembic` or plain SQL DDL in `app/db/init.py` | fresh DB auto-creates tables |

## T3 · Local LLM + RAG generation

| # | Step | Tool / command | Done when |
|---|---|---|---|
| 1 | Start the model | `ollama pull llama3.2:3b` | `curl :8000/api/v1/llm/health` → `reachable: true` |
| 2 | Implement `/llm/rca`: retrieve runbooks → build prompt → call Ollama | `ollama` SDK, `app/rag/` | returns `diagnosis` + `narrative` |
| 3 | Enforce the **0.65 confidence gate** | `settings.RCA_MIN_CONFIDENCE` | conf `< 0.65` → `remediation_blocked: true` |
| 4 | Implement `/llm/postmortem` | same path | sections populated |
| 5 | Keep tests offline | `monkeypatch` the Ollama client | `make test` passes with **no network** |

## T4 · AI Agent with tools (ReAct loop)

| # | Step | Tool / command | Done when |
|---|---|---|---|
| 1 | Build a tool registry: `k8s_api`, `promql`, `logql`, `traceql` + mesh status | new `app/agent/tools.py` | tools callable by name |
| 2 | ReAct loop: think → act → observe until terminal | edit `sre_agent.py` (you own it) | agent produces ≥2 evidence entries |
| 3 | Record evidence via `record_investigation()` (already wired) | `app/tools/counters.py` | `avg_tool_calls_per_rca` > 1 |
| 4 | Derive confidence from retrieval + tool agreement, pass to gate | — | confidence in `[0,1]` |
| 5 | Return structured RCA dict with citations | `pytest` | matches the contract shape T3/T6 read |

## T5 · Anomaly Detection (Isolation Forest)

| # | Step | Tool / command | Done when |
|---|---|---|---|
| 1 | Feature collector: cpu / memory / requests / latency from mesh at `ML_SCRAPE_INTERVAL_SECONDS` | `psutil` or mesh `/status` | rows written to `app/ml/data/` |
| 2 | Train script → `IsolationForest(contamination=ML_CONTAMINATION)` | `sklearn`, save to `settings.ML_MODEL_PATH` | `model.joblib` exists |
| 3 | Fill `api/anomalies.py` handlers (**schemas frozen**) | `POST /anomalies/train`, `/score` | `status` no longer `NOT_IMPLEMENTED` |
| 4 | Stream scoring + persist anomalies | `joblib.load` | `GET /anomalies` returns real points |
| 5 | Test with synthetic spike | `pytest` | spike flagged, flat line not flagged |

## T6 · Streamlit dashboard

| # | Step | Tool / command | Done when |
|---|---|---|---|
| 1 | Create `streamlit_app.py` at repo root (the `make ui` guard requires this path) | `streamlit` 1.65 | `make ui` starts |
| 2 | Tabs: Overview · Incidents · Anomalies · RCA · Insights | `st.tabs` | all 5 render |
| 3 | Charts: scorecard trend, MTTD, alert compression, anomaly timeline | `plotly` 7.1 | charts draw from live API |
| 4 | Auto-refresh + config from `STREAMLIT_API_BASE` | `st.cache_data(ttl=…)` | updates without restart |
| 5 | Insights panel: plain-language summary of what changed | reads scorecard + incidents | text updates as data changes |

## T7 · Chaos Mesh (real fault injection)

| # | Step | Tool / command | Done when |
|---|---|---|---|
| 1 | Install Chaos Mesh + CRDs in minikube | `helm repo add chaos-mesh …` / `helm install` | `kubectl get crd` lists `chaosmeshes`/`pods` |
| 2 | Make `api/chaos.py` create real `ChaosExperiment` CRs in-cluster, fall back to simulation locally | `kubernetes` client | in-cluster inject kills a real pod |
| 3 | Align `k8s/manifests/online-boutique.yaml` ports to `8081-8084`/`6380` | edit manifest | ports match the app |
| 4 | Add `helm_release` + missing namespaces to `terraform/main.tf` | `terraform validate` | passes |
| 5 | Verify inject → incident → real pod restart | `make chaos-inject` | pod `RESTARTS` increments |

---

# Phase 2 · Integration (sequential, after all tracks merge)

| # | Step | Tool / command | Done when |
|---|---|---|---|
| 1 | Merge T1–T7 into `main`, resolve conflicts in owned files only | `git merge` per branch | `make test` green |
| 2 | Wire the pipeline end-to-end: **anomaly → agent → LLM → confidence gate → remediation** | edit `app/correlation/engine.py` + `sre_agent.py` | one incident flows through all stages |
| 3 | Block remediation below 0.65 confidence | `settings.RCA_MIN_CONFIDENCE` | low-confidence incident does **not** auto-heal |
| 4 | Backfill any new settings/deps deferred from Phase 1 | `config.py` / `requirements.txt` unfrozen here | `.env.example` still matches config (no silent no-ops) |
| 5 | Full regression + lint | `make test && make lint` | 100% pass, no warnings |

---

# Phase 3 · Evaluation & demo (sequential, last)

| # | Step | Tool / command | Done when |
|---|---|---|---|
| 1 | Run a full honest benchmark | `GET /api/v1/benchmarks/scorecard` | accuracy is a real 0–100 number |
| 2 | Run ≥5 varied chaos experiments (different services/types) | `make chaos-inject` + API | ≥5 rows in ground truth |
| 3 | Verify no fabrication returned | grep for `max(…542)`, `accurate_matches += 1`, clamps | zero hits |
| 4 | Update `AI_UNDERSTANDING.md` / README with **measured** numbers only | — | every figure traceable to an API response |
| 5 | Demo prep: screenshots, scorecard JSON, video/PR | — | reproducible from a clean clone |
| 6 | Push + final PR | `git push` | CI green |

---

### Suggested order
```
Phase 0 ✅  ──►  T1 T2 T3 T4 T5 T6 T7   (all 7 in parallel, branches exist)
                          │
                          ▼
                     Phase 2 (merge + wire)
                          │
                          ▼
                     Phase 3 (measure + demo)
```
