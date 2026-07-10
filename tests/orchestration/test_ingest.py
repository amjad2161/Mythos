"""
tests/orchestration/test_ingest.py
----------------------------------
Knowledge-base ingestion: parsing a hierarchical taxonomy into graph-linked
MemoryNodes and upserting them into the Data Matrix as trust-scored, verbatim
ground truth that the swarm can navigate.
"""
from mythos.orchestration.ingest import (
    IngestResult,
    ingest_taxonomy,
    parse_taxonomy,
)
from mythos.orchestration.matrix import HashEmbedder, InMemoryDataMatrix
from mythos.orchestration.schemas import TRUST_USER

MARKDOWN_KB = """\
# My Knowledge Base

## Physics
- Classical mechanics
- Quantum field theory

## Chemistry
- Organic synthesis
"""

NUMBERED_KB = """\
1. Physics
Classical mechanics
Quantum field theory
2. Chemistry
Organic synthesis
"""


class TestParseTaxonomy:
    def test_markdown_headings_become_categories(self):
        categories, topics = parse_taxonomy(MARKDOWN_KB)
        titles = [t for t, _ in categories]
        assert titles == ["Physics", "Chemistry"]
        assert topics == 3

    def test_leading_h1_is_title_not_category(self):
        categories, _ = parse_taxonomy(MARKDOWN_KB)
        # "# My Knowledge Base" must not appear as a category.
        assert "My Knowledge Base" not in [t for t, _ in categories]

    def test_topics_attach_to_current_category(self):
        categories, _ = parse_taxonomy(MARKDOWN_KB)
        by_title = dict(categories)
        assert by_title["Physics"] == ["Classical mechanics", "Quantum field theory"]
        assert by_title["Chemistry"] == ["Organic synthesis"]

    def test_numbered_sections_become_categories(self):
        categories, topics = parse_taxonomy(NUMBERED_KB)
        assert [t for t, _ in categories] == ["Physics", "Chemistry"]
        assert topics == 3

    def test_bullets_and_emphasis_are_stripped(self):
        categories, _ = parse_taxonomy("## Cat\n- **bold topic**\n* *italic topic*\n")
        assert dict(categories)["Cat"] == ["bold topic", "italic topic"]

    def test_orphan_topics_fall_under_general(self):
        categories, topics = parse_taxonomy("loose topic\nanother one\n")
        assert categories == [("General", ["loose topic", "another one"])]
        assert topics == 2

    def test_empty_document(self):
        categories, topics = parse_taxonomy("\n\n   \n")
        assert categories == []
        assert topics == 0


class TestIngestTaxonomy:
    def _matrix(self):
        return InMemoryDataMatrix(HashEmbedder())

    def test_result_counts(self):
        matrix = self._matrix()
        result = ingest_taxonomy(matrix, MARKDOWN_KB, "kb")
        assert isinstance(result, IngestResult)
        assert result.categories == 2
        assert result.topics == 3
        assert "2 categories, 3 topics" in result.summary()

    def test_node_types_and_counts(self):
        matrix = self._matrix()
        ingest_taxonomy(matrix, MARKDOWN_KB, "kb")
        by_type = {}
        for node in matrix._nodes.values():  # noqa: SLF001 – test introspection
            by_type.setdefault(node.node_type, []).append(node)
        assert len(by_type["kb_root"]) == 1
        assert len(by_type["kb_category"]) == 2
        assert len(by_type["kb_topic"]) == 3

    def test_graph_edges_link_topic_to_category_to_root(self):
        matrix = self._matrix()
        result = ingest_taxonomy(matrix, MARKDOWN_KB, "kb")
        nodes = list(matrix._nodes.values())  # noqa: SLF001
        categories = [n for n in nodes if n.node_type == "kb_category"]
        topics = [n for n in nodes if n.node_type == "kb_topic"]
        # Every category links up to the single root.
        for cat in categories:
            assert cat.edges == [{"relation": "part_of", "target_id": result.root_id}]
        # Every topic links up to some category.
        category_ids = {c.node_id for c in categories}
        for topic in topics:
            assert len(topic.edges) == 1
            assert topic.edges[0]["relation"] == "belongs_to"
            assert topic.edges[0]["target_id"] in category_ids

    def test_nodes_are_verbatim_and_user_trust(self):
        matrix = self._matrix()
        ingest_taxonomy(matrix, MARKDOWN_KB, "kb")
        for node in matrix._nodes.values():  # noqa: SLF001
            assert node.trust_score == TRUST_USER
            assert node.metadata["verbatim_required"] is True
            assert node.metadata["source"] == "kb:kb"

    def test_custom_trust_score(self):
        matrix = self._matrix()
        ingest_taxonomy(matrix, MARKDOWN_KB, "kb", trust_score=0.5)
        assert all(n.trust_score == 0.5 for n in matrix._nodes.values())  # noqa: SLF001

    def test_navigate_retrieves_topic_and_traverses_to_category(self):
        """A topic found semantically pulls in its category via the graph."""
        matrix = self._matrix()
        ingest_taxonomy(matrix, MARKDOWN_KB, "kb")
        nodes = matrix.navigate("Quantum field theory", top_k=1, hops=1)
        contents = {n.content for n in nodes}
        assert "Quantum field theory" in contents
        assert "Physics" in contents  # reached by the belongs_to edge
