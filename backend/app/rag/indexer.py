import logging
import re
from pathlib import Path
from typing import List, Dict, Any
from app.config import settings
from app.rag.vectorstore import get_chroma_manager

logger = logging.getLogger(__name__)

def parse_runbook_content(file_path: Path) -> Dict[str, Any]:
    content = file_path.read_text(encoding="utf-8")
    title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else file_path.stem.replace("_", " ").title()
    
    # Categorize based on file name or content
    category = "general"
    lower_content = content.lower()
    if "network" in lower_content or "latency" in lower_content:
        category = "network"
    elif "oom" in lower_content or "memory" in lower_content:
        category = "memory"
    elif "cpu" in lower_content or "throttl" in lower_content:
        category = "compute"
    elif "redis" in lower_content or "cache" in lower_content:
        category = "cache"
    elif "payment" in lower_content:
        category = "service"

    return {
        "id": f"runbook_{file_path.stem}",
        "title": title,
        "filename": file_path.name,
        "category": category,
        "content": content
    }

def index_all_runbooks(runbooks_dir: Path = None) -> int:
    if runbooks_dir is None:
        runbooks_dir = settings.RUNBOOKS_DIR

    chroma_mgr = get_chroma_manager()
    collection = chroma_mgr.collection

    md_files = list(runbooks_dir.glob("*.md"))
    if not md_files:
        logger.warning(f"No markdown runbooks found in {runbooks_dir}")
        return 0

    documents: List[str] = []
    metadatas: List[Dict[str, Any]] = []
    ids: List[str] = []

    for file_path in md_files:
        parsed = parse_runbook_content(file_path)
        documents.append(parsed["content"])
        metadatas.append({
            "title": parsed["title"],
            "filename": parsed["filename"],
            "category": parsed["category"],
        })
        ids.append(parsed["id"])

    # Upsert into ChromaDB
    collection.upsert(
        ids=ids,
        documents=documents,
        metadatas=metadatas
    )

    logger.info(f"Successfully indexed {len(ids)} runbooks into ChromaDB collection '{collection.name}'.")
    return len(ids)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    count = index_all_runbooks()
    print(f"Indexed {count} runbooks.")

