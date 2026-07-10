"""
mythos/orchestration/ingest.py
------------------------------
Knowledge-base ingestion into the Data Matrix.

Turns a hierarchical taxonomy / outline document into graph-linked
``MemoryNode``s so the swarm can navigate curated domain knowledge as
ground truth: agents ``navigate`` the matrix, land on a relevant topic, and
traverse its ``belongs_to`` edge up to the domain for broader context.

Parsing is format-tolerant. A *category* line is either a Markdown heading
(``##``…) or a numbered section (``3. Physics``); everything else under it
becomes a *topic* node linked to that category, which in turn links to a
single KB root node. A single leading level-1 heading (``# Title``) is read as
the document's title (the root already carries the KB name) rather than a
category, so a well-formed KB document parses cleanly. Content is stored
verbatim (``verbatim_required``) at user/reference trust, below system
instructions but above agent artifacts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .matrix import DataMatrix
from .schemas import MemoryNode, TRUST_USER

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\.\s+(.*)$")


@dataclass
class IngestResult:
    kb_name: str
    root_id: str
    categories: int
    topics: int

    def summary(self) -> str:
        return (
            f"Ingested '{self.kb_name}': {self.categories} categories, "
            f"{self.topics} topics ({self.categories + self.topics + 1} nodes)."
        )


def _clean(text: str) -> str:
    """Strip Markdown bullet/emphasis noise from a line."""
    text = text.strip()
    text = re.sub(r"^[-*+•]\s+", "", text)          # bullets
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)     # bold
    text = re.sub(r"\*(.+?)\*", r"\1", text)         # italics
    return text.strip()


def parse_taxonomy(text: str):
    """
    Parse *text* into ``(categories, topics)`` where each category is
    ``(title, [topic_line, ...])``.  Robust to headings, numbered sections,
    and free bullet/paragraph lines.
    """
    categories: "List[tuple[str, List[str]]]" = []
    current: Optional[str] = None
    seen_content = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        heading = _HEADING.match(line.strip())
        numbered = _NUMBERED.match(line.strip())
        first = not seen_content
        seen_content = True
        if heading and len(heading.group(1)) == 1 and first and not categories:
            # A single leading "# Title" is the document title, not a category
            # (the KB root already carries the name).
            continue
        if heading:
            title = _clean(heading.group(2))
            categories.append((title, []))
            current = title
        elif numbered and not numbered.group(1).count("."):
            # Top-level "N. Title" (not "N.M. ...") is a category.
            title = _clean(numbered.group(2))
            categories.append((title, []))
            current = title
        else:
            topic = _clean(line)
            if not topic:
                continue
            if current is None:
                categories.append(("General", []))
                current = "General"
            categories[-1][1].append(topic)
    topic_count = sum(len(t) for _, t in categories)
    return categories, topic_count


def ingest_taxonomy(
    matrix: DataMatrix,
    text: str,
    kb_name: str,
    trust_score: float = TRUST_USER,
) -> IngestResult:
    """Parse *text* and upsert it into *matrix* as a linked knowledge graph."""
    categories, topic_count = parse_taxonomy(text)

    root = MemoryNode.create(
        node_type="kb_root",
        content=f"Knowledge base: {kb_name}",
        source=f"kb:{kb_name}",
        trust_score=trust_score,
        verbatim_required=True,
    )
    matrix.upsert(root)

    cat_count = 0
    for title, topics in categories:
        category = MemoryNode.create(
            node_type="kb_category",
            content=title,
            source=f"kb:{kb_name}",
            trust_score=trust_score,
            verbatim_required=True,
            edges=[{"relation": "part_of", "target_id": root.node_id}],
        )
        matrix.upsert(category)
        cat_count += 1
        for topic in topics:
            node = MemoryNode.create(
                node_type="kb_topic",
                content=topic,
                source=f"kb:{kb_name}",
                trust_score=trust_score,
                verbatim_required=True,
                edges=[{"relation": "belongs_to", "target_id": category.node_id}],
            )
            matrix.upsert(node)

    return IngestResult(kb_name, root.node_id, cat_count, topic_count)
