import pytest
from pathlib import Path
from app.rag.indexer import index_all_runbooks, parse_runbook_content
from app.rag.retriever import get_runbook_retriever
from app.config import settings

def test_parse_runbook_content():
    runbook_path = settings.RUNBOOKS_DIR / "network_latency.md"
    assert runbook_path.exists(), f"Runbook {runbook_path} must exist"
    
    parsed = parse_runbook_content(runbook_path)
    assert parsed["id"] == "runbook_network_latency"
    assert "Network Latency" in parsed["title"]
    assert parsed["category"] == "network"
    assert len(parsed["content"]) > 100

def test_index_and_retrieve_runbooks():
    # Index runbooks from directory
    count = index_all_runbooks(settings.RUNBOOKS_DIR)
    assert count >= 4

    retriever = get_runbook_retriever()
    
    # Query for memory / oom issues
    results = retriever.search_relevant_runbooks(query="pod out of memory crashloopbackoff exit code 137", n_results=2)
    assert len(results) > 0
    top_result = results[0]
    assert "oom" in top_result["category"] or "OOM" in top_result["title"] or "memory" in top_result["content"].lower()

    # Query for network latency
    net_results = retriever.search_relevant_runbooks(query="packet loss high p99 latency timeout", n_results=1)
    assert len(net_results) > 0
    assert "network" in net_results[0]["category"] or "Latency" in net_results[0]["title"]

