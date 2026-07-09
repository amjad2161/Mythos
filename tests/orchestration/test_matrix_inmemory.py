"""
tests/orchestration/test_matrix_inmemory.py
-------------------------------------------
Data Matrix behaviour: KNN search, graph traversal, trust-ranked fusion.
Runs entirely on the in-memory driver + deterministic hash embedder.
"""
from mythos.orchestration.matrix import (
    HashEmbedder,
    InMemoryDataMatrix,
    fuse_context,
)
from mythos.orchestration.schemas import (
    MemoryNode,
    TRUST_AGENT,
    TRUST_SYSTEM,
    TRUST_USER,
)


def make_matrix() -> InMemoryDataMatrix:
    return InMemoryDataMatrix(HashEmbedder())


def store(matrix, node_type, content, trust=TRUST_AGENT, verbatim=False, edges=None):
    node = MemoryNode.create(
        node_type=node_type,
        content=content,
        source="test",
        trust_score=trust,
        verbatim_required=verbatim,
        edges=edges,
    )
    matrix.upsert(node)
    return node


class TestEmbedder:
    def test_deterministic(self):
        emb = HashEmbedder()
        assert emb.embed("fibonacci sequence") == emb.embed("fibonacci sequence")

    def test_normalised(self):
        vec = HashEmbedder().embed("some text to embed")
        assert abs(sum(v * v for v in vec) - 1.0) < 1e-9

    def test_empty_text_is_zero_vector(self):
        assert not any(HashEmbedder().embed(""))


class TestSearchAndGet:
    def test_get_by_ids_omits_missing(self):
        matrix = make_matrix()
        node = store(matrix, "goal", "write fibonacci script")
        assert matrix.get([node.node_id, "no-such-id"]) == [node]

    def test_knn_finds_token_overlap(self):
        matrix = make_matrix()
        relevant = store(matrix, "spec", "fibonacci sequence python script")
        store(matrix, "spec", "database connection pooling configuration")
        results = matrix.search("write a fibonacci python script", top_k=1)
        assert results == [relevant]


class TestNavigate:
    def test_seed_pointers_are_included(self):
        matrix = make_matrix()
        node = store(matrix, "goal", "the goal text")
        found = matrix.navigate("something unrelated entirely", seed_ids=[node.node_id])
        assert node in found

    def test_graph_traversal_pulls_edge_targets(self):
        matrix = make_matrix()
        spec = store(matrix, "spec", "column layout for the users database table")
        code = store(
            matrix,
            "artifact",
            "fibonacci python script implementation",
            edges=[{"relation": "depends_on", "target_id": spec.node_id}],
        )
        found = matrix.navigate("fibonacci python script", top_k=1)
        assert code in found
        assert spec in found  # reached via the edge, not the query

    def test_traversal_respects_hop_limit(self):
        matrix = make_matrix()
        far = store(matrix, "spec", "zzz unrelated distant node")
        mid = store(
            matrix, "spec", "yyy also unrelated",
            edges=[{"relation": "next", "target_id": far.node_id}],
        )
        near = store(
            matrix, "artifact", "fibonacci python script",
            edges=[{"relation": "next", "target_id": mid.node_id}],
        )
        found = matrix.navigate("fibonacci python script", top_k=1, hops=1)
        assert near in found and mid in found
        assert far not in found

    def test_trust_ranking_puts_system_first(self):
        matrix = make_matrix()
        agent_node = store(matrix, "artifact", "fibonacci note", trust=TRUST_AGENT)
        system_node = store(
            matrix, "system_instruction", "fibonacci rules", trust=TRUST_SYSTEM
        )
        user_node = store(matrix, "goal", "fibonacci goal", trust=TRUST_USER)
        found = matrix.navigate("fibonacci", top_k=3)
        assert found.index(system_node) < found.index(user_node) < found.index(agent_node)


class TestTraceScoping:
    def test_search_hits_from_other_traces_are_excluded(self):
        matrix = make_matrix()
        stale = store(matrix, "goal", "fibonacci python script old goal", trust=TRUST_USER)
        stale.metadata["trace_id"] = "trace-OLD"
        matrix.upsert(stale)
        current = store(matrix, "goal", "fibonacci python script new goal", trust=TRUST_USER)
        current.metadata["trace_id"] = "trace-NEW"
        matrix.upsert(current)
        shared = store(matrix, "system_instruction", "fibonacci ground rules")

        found = matrix.navigate("fibonacci python script", top_k=5, trace_id="trace-NEW")
        assert current in found
        assert shared in found          # untagged ground truth always passes
        assert stale not in found       # other trace excluded

    def test_seed_pointers_bypass_trace_filter(self):
        matrix = make_matrix()
        node = store(matrix, "artifact", "explicitly pointed content")
        node.metadata["trace_id"] = "trace-OTHER"
        matrix.upsert(node)
        found = matrix.navigate("unrelated", seed_ids=[node.node_id], trace_id="trace-MINE")
        assert node in found            # explicit pointers are always honored


class TestFusion:
    def test_verbatim_content_is_delimited(self):
        node = MemoryNode.create(
            node_type="system_instruction",
            content="EXACT TEXT",
            source="test",
            trust_score=TRUST_SYSTEM,
            verbatim_required=True,
        )
        block = fuse_context([node])
        assert "<<<VERBATIM>>>\nEXACT TEXT\n<<<END VERBATIM>>>" in block

    def test_empty_nodes_render_empty(self):
        assert fuse_context([]) == ""
