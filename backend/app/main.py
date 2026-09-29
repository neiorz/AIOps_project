import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.rag.indexer import index_all_runbooks
from app.api.health import router as health_router
from app.api.alerts import router as alerts_router
from app.api.incidents import router as incidents_router
from app.api.rag import router as rag_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up AIOps Platform Engine...")
    # Initialize & index runbooks in ChromaDB
    try:
        count = index_all_runbooks()
        logger.info(f"RAG Knowledge Base initialized with {count} runbooks.")
    except Exception as e:
        logger.error(f"Failed to auto-index runbooks at startup: {e}")
    yield
    logger.info("Shutting down AIOps Platform Engine...")

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Chaos-Grounded Autonomous AIOps Platform for Multi-Tenant Incident Detection, Correlation & AI Root-Cause Investigation",
    version="1.0.0",
    lifespan=lifespan
)

# Enable CORS for the React Dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API routers under /api/v1
app.include_router(health_router, prefix=settings.API_V1_PREFIX)
app.include_router(alerts_router, prefix=settings.API_V1_PREFIX)
app.include_router(incidents_router, prefix=settings.API_V1_PREFIX)
app.include_router(rag_router, prefix=settings.API_V1_PREFIX)

@app.get("/")
def root():
    return {
        "platform": settings.PROJECT_NAME,
        "version": "1.0.0",
        "docs_url": "/docs",
        "health_check": f"{settings.API_V1_PREFIX}/health"
    }
