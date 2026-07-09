"""
tests/integration/test_qdrant_matrix.py
---------------------------------------
QdrantDataMatrix against a live Qdrant – KNN, payload round-trip, traversal.
Uses the deterministic HashEmbedder to avoid model downloads in CI.
"""
import pytest

from mythos.orchestration.matrix import HashEmbedder, QdrantDataMatrix
from mythos.orchestration.schemas import MemoryNode, TRUST_SYSTEM

pytestmark = pytest.mark.integration


@pytest.fixture()
def matrix(qdrant_url, unique_name):
    m = QdrantDataMatrix(
        embedder=HashEmbedder(),
        url=qdrant_url,
        collection=f"mythos_test_{unique_name}",
    )
    yield m
    m._client.delete_collection(m._collection)
    m.close()


def test_upsert_and_get_round_trip(matrix):
    node = MemoryNode.create(
        node_type="system_instruction",
        content="Never fabricate data.",
        source="orchestrator",
        trust_score=TRUST_SYSTEM,
        verbatim_required=True,
        edges=[{"relation": "refines", "target_id": "some-node"}],
    )
    matrix.upsert(node)
    [fetched] = matrix.get([node.node_id])
    assert fetched == node  # verbatim content, metadata, and edges survive


def test_knn_search_orders_by_similarity(matrix):
    relevant = MemoryNode.create(
        node_type="spec", content="fibonacci sequence python script", source="t"
    )
    irrelevant = MemoryNode.create(
        node_type="spec", content="database connection pooling", source="t"
    )
    matrix.upsert(relevant)
    matrix.upsert(irrelevant)

    results = matrix.search("write a fibonacci python script", top_k=1)
    assert [n.node_id for n in results] == [relevant.node_id]


def test_navigate_traverses_edges(matrix):
    spec = MemoryNode.create(
        node_type="spec", content="users table column layout", source="t"
    )
    matrix.upsert(spec)
    artifact = MemoryNode.create(
        node_type="artifact",
        content="fibonacci python script implementation",
        source="t",
        edges=[{"relation": "depends_on", "target_id": spec.node_id}],
    )
    matrix.upsert(artifact)

    found = matrix.navigate("fibonacci python script", top_k=1)
    ids = {n.node_id for n in found}
    assert artifact.node_id in ids
    assert spec.node_id in ids  # pulled in via the graph edge
