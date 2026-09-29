import logging
from typing import List, Dict, Any, Optional
from app.rag.vectorstore import get_chroma_manager

logger = logging.getLogger(__name__)

class RunbookRetriever:
    def __init__(self):
        self.chroma_mgr = get_chroma_manager()

    def search_relevant_runbooks(
        self,
        query: str,
        n_results: int = 3,
        category: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        collection = self.chroma_mgr.collection
        
        where_filter = {"category": category} if category else None

        results = collection.query(
            query_texts=[query],
            n_results=n_results,
            where=where_filter,
            include=["documents", "metadatas", "distances"]
        )

        formatted_results: List[Dict[str, Any]] = []
        if not results or not results["documents"] or not results["documents"][0]:
            return formatted_results

        docs = results["documents"][0]
        metas = results["metadatas"][0] if results.get("metadatas") else [{}] * len(docs)
        distances = results["distances"][0] if results.get("distances") else [0.0] * len(docs)
        ids = results["ids"][0] if results.get("ids") else [f"doc_{i}" for i in range(len(docs))]

        for doc_id, doc, meta, dist in zip(ids, docs, metas, distances):
            # Chroma returns L2 / cosine distance; lower distance = higher similarity
            similarity_score = max(0.0, 1.0 - (dist / 2.0)) if dist is not None else 1.0
            formatted_results.append({
                "id": doc_id,
                "title": meta.get("title", "Unknown Runbook"),
                "category": meta.get("category", "general"),
                "filename": meta.get("filename", ""),
                "content": doc,
                "distance": dist,
                "similarity_score": round(similarity_score, 4)
            })

        return formatted_results

def get_runbook_retriever() -> RunbookRetriever:
    return RunbookRetriever()

