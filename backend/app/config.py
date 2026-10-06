import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "Chaos-Grounded Autonomous AIOps Platform"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = False

    # Base paths
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    RUNBOOKS_DIR: Path = BASE_DIR / "runbooks"
    CHROMA_PERSIST_DIR: Path = BASE_DIR / "data" / "chroma"

    # Telemetry backends
    PROMETHEUS_URL: str = os.getenv("PROMETHEUS_URL", "http://prometheus-k8s.monitoring.svc.cluster.local:9090")
    LOKI_URL: str = os.getenv("LOKI_URL", "http://loki-gateway.monitoring.svc.cluster.local:3100")
    TEMPO_URL: str = os.getenv("TEMPO_URL", "http://tempo.monitoring.svc.cluster.local:3200")
    
    # Caching & Database
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    POSTGRES_URL: str = os.getenv("POSTGRES_URL", "postgresql://aiops:aiops_password@localhost:5432/aiops_ground_truth")

    # Multi-Tenant SLA response limits (in seconds)
    SLA_CLIENT_A_SECONDS: int = 300   # 5 minutes
    SLA_CLIENT_B_SECONDS: int = 900   # 15 minutes
    SLA_CLIENT_C_SECONDS: int = 1800  # 30 minutes

    # -------------------------------------------------------------------------
    # Pre-seeded in Phase 0 (P0.8). Phase 1 tracks read from here and must NOT
    # edit this file -- everything is overridable via .env / environment.
    # -------------------------------------------------------------------------

    # RCA safety gate: confidence below this blocks automated remediation.
    RCA_MIN_CONFIDENCE: float = float(os.getenv("RCA_MIN_CONFIDENCE", "0.65"))

    # Track T3 : Local LLM + RAG generation
    LLM_ENABLED: bool = os.getenv("LLM_ENABLED", "true").lower() == "true"
    OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "llama3.2:3b")
    LLM_TIMEOUT_SECONDS: int = int(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
    LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "512"))

    # Track T5 : Anomaly detection
    ML_MODEL_PATH: Path = Path(
        os.getenv("ML_MODEL_PATH", str(BASE_DIR / "app" / "ml" / "model.joblib"))
    )
    ML_FEATURE_WINDOW: int = int(os.getenv("ML_FEATURE_WINDOW", "30"))
    ML_CONTAMINATION: float = float(os.getenv("ML_CONTAMINATION", "0.05"))
    ML_SCRAPE_INTERVAL_SECONDS: int = int(os.getenv("ML_SCRAPE_INTERVAL_SECONDS", "5"))

    # Track T6 : Streamlit dashboard
    STREAMLIT_PORT: int = int(os.getenv("STREAMLIT_PORT", "8501"))
    STREAMLIT_API_BASE: str = os.getenv(
        "STREAMLIT_API_BASE", "http://localhost:8000/api/v1"
    )

    # Track T1 : Local observability backends (docker-compose on the host)
    PROMETHEUS_LOCAL_URL: str = os.getenv("PROMETHEUS_LOCAL_URL", "http://localhost:9091")
    LOKI_LOCAL_URL: str = os.getenv("LOKI_LOCAL_URL", "http://localhost:3101")
    TEMPO_LOCAL_URL: str = os.getenv("TEMPO_LOCAL_URL", "http://localhost:3201")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
