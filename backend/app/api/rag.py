from fastapi import APIRouter, Query
from typing import Optional
from app.rag.retriever import get_runbook_retriever
from app.rag.indexer import index_all_runbooks

router = APIRouter(prefix="/rag", tags=["RAG Knowledge Base"])

@router.get("/search")
def search_runbooks(
    q: str = Query(..., description="Query symptom or alert description"),
    n: int = Query(3, description="Number of results"),
    category: Optional[str] = Query(None, description="Filter by category")
):
    """Query ChromaDB vector store for relevant DevOps runbooks & remediation steps."""
    retriever = get_runbook_retriever()
    results = retriever.search_relevant_runbooks(query=q, n_results=n, category=category)
    return {
        "query": q,
        "count": len(results),
        "results": results
    }

@router.post("/reindex")
def reindex_runbooks():
    """Trigger manual reindexing of markdown runbooks into ChromaDB vector store."""
    count = index_all_runbooks()
    return {
        "status": "success",
        "indexed_runbooks": count
    }

