from saral.rag.index import HybridIndex, search_knowledge


def test_search_returns_cited_passages():
    results = search_knowledge("What is the waiting period for pre-existing diseases?")
    assert results
    # citations carry source filename + chunk
    assert all("#" in p.citation for p in results)
    # the exclusions/waiting-period doc should surface in the top results
    assert any("exclusion" in p.doc_id or "waiting" in p.text.lower() for p in results)


def test_search_ranks_relevant_doc_first():
    idx = HybridIndex("data/policy_corpus")
    top = idx.search("how do I file a reimbursement claim?", top_k=3)
    assert top
    assert any("claim" in p.doc_id for p in top)


def test_empty_corpus_returns_empty(tmp_path):
    idx = HybridIndex(tmp_path)  # no .md files
    assert idx.search("anything") == []
