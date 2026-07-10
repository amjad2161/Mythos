"""
mythos/orchestration/matrix.py
------------------------------
The Data Matrix: the swarm's shared long-term memory and ground truth.

A hybrid of two retrieval models over one store:

* **Vector search** – every node's verbatim content is embedded; agents find
  conceptually relevant nodes with K-nearest-neighbour search.
* **Knowledge graph** – nodes carry typed edges (``{"relation", "target_id"}``);
  after a semantic hit, traversal follows edges to pull adjacent, necessary
  context (e.g. from an artifact to the spec it implements).

``DataMatrix.navigate`` composes the two into the vision's autonomous
navigation loop: embed the need → KNN → edge traversal → trust-ranked fusion.

Drivers:

* ``QdrantDataMatrix``   – production; the whole node (content, metadata,
  edges) lives in the point payload, so the graph needs no second service.
* ``InMemoryDataMatrix`` – brute-force cosine over a dict, for tests/offline.

Embedders:

* ``FastEmbedEmbedder``  – local ONNX model (BAAI/bge-small-en-v1.5, 384-d).
* ``HashEmbedder``       – deterministic feature hashing at the same
  dimensionality; no model download, used in tests and offline demos.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Sequence

from .schemas import MemoryNode

EMBEDDING_DIM = 384


# ---------------------------------------------------------------------------
# Embedders
# ---------------------------------------------------------------------------

class Embedder(ABC):
    """Turns text into a fixed-size vector."""

    dimension: int = EMBEDDING_DIM

    @abstractmethod
    def embed(self, text: str) -> List[float]:
        """Return the embedding vector for *text*."""


class HashEmbedder(Embedder):
    """
    Deterministic feature-hashing embedder.

    Each token is hashed onto one of ``dimension`` buckets (with a hashed
    sign) and the result is L2-normalised.  No semantics beyond token
    overlap – sufficient for exercising KNN mechanics deterministically in
    tests and offline runs, with zero downloads and zero dependencies.
    """

    _TOKEN_RE = re.compile(r"[a-z0-9]+")

    def __init__(self, dimension: int = EMBEDDING_DIM) -> None:
        self.dimension = dimension

    def embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dimension
        for token in self._TOKEN_RE.findall(text.lower()):
            digest = hashlib.md5(token.encode("utf-8")).digest()  # noqa: S324 – not cryptographic
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[index] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]


class FastEmbedEmbedder(Embedder):
    """Local semantic embeddings via fastembed (ONNX, no API key)."""

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        try:
            from fastembed import TextEmbedding  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "The 'fastembed' package is required for semantic embeddings. "
                "Install it with: pip install mythos[orchestration]"
            ) from exc
        self._model = TextEmbedding(model_name=model_name)
        self.dimension = EMBEDDING_DIM

    def embed(self, text: str) -> List[float]:
        vector = next(iter(self._model.embed([text])))
        return [float(v) for v in vector]


def create_embedder(kind: str) -> Embedder:
    """Instantiate an embedder by config name ('fastembed' | 'hash')."""
    kind = kind.lower()
    if kind == "fastembed":
        return FastEmbedEmbedder()
    if kind == "hash":
        return HashEmbedder()
    raise ValueError(f"Unknown embedder: '{kind}'. Choose 'fastembed' or 'hash'.")


# ---------------------------------------------------------------------------
# DataMatrix interface + shared navigation logic
# ---------------------------------------------------------------------------

class DataMatrix(ABC):
    """Hybrid vector + graph long-term memory."""

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder

    # -- storage primitives (driver-specific) ----------------------------

    @abstractmethod
    def upsert(self, node: MemoryNode) -> str:
        """Store *node* (embedding computed from its content); return its id."""

    @abstractmethod
    def get(self, node_ids: Sequence[str]) -> List[MemoryNode]:
        """Fetch nodes by id (missing ids are silently omitted)."""

    @abstractmethod
    def search(self, text: str, top_k: int = 3) -> List[MemoryNode]:
        """KNN search: the *top_k* nodes semantically closest to *text*."""

    @abstractmethod
    def close(self) -> None:
        """Release driver resources."""

    # -- autonomous navigation (shared) ----------------------------------

    def navigate(
        self,
        need: str,
        top_k: int = 3,
        hops: int = 1,
        seed_ids: Optional[Sequence[str]] = None,
        trace_id: Optional[str] = None,
    ) -> List[MemoryNode]:
        """
        Autonomously assemble the context for *need*.

        1. Semantic query – KNN search for the closest nodes (plus any
           explicitly pointed-to *seed_ids*, e.g. a payload's
           ``context_pointers``).
        2. Graph traversal – follow each hit's edges for *hops* levels to
           pull in adjacent, necessary context.
        3. Fusion ordering – deduplicate and rank by trust score (system
           instructions first) so higher-trust content overrides lower.

        When *trace_id* is given, semantically-found nodes tagged with a
        DIFFERENT trace are excluded – a persistent collection accumulates
        goals/artifacts across runs, and a stale goal must not surface as
        high-trust context for the current one.  Untagged nodes (shared
        ground truth like system instructions) always pass; explicit
        *seed_ids* always pass.
        """
        found: Dict[str, MemoryNode] = {}
        frontier: List[MemoryNode] = []

        def admit(node: MemoryNode) -> bool:
            if trace_id is None:
                return True
            node_trace = node.metadata.get("trace_id")
            return node_trace is None or node_trace == trace_id

        for node in self.get(list(seed_ids or [])):
            found[node.node_id] = node
            frontier.append(node)
        if need.strip():
            for node in self.search(need, top_k=top_k):
                if node.node_id not in found and admit(node):
                    found[node.node_id] = node
                    frontier.append(node)

        for _ in range(max(0, hops)):
            targets = list(dict.fromkeys(
                edge.get("target_id", "")
                for node in frontier
                for edge in node.edges
                if edge.get("target_id") and edge["target_id"] not in found
            ))
            if not targets:
                break
            frontier = []
            for node in self.get(targets):
                if node.node_id not in found and admit(node):
                    found[node.node_id] = node
                    frontier.append(node)

        return sorted(found.values(), key=lambda n: -n.trust_score)


def fuse_context(nodes: Sequence[MemoryNode]) -> str:
    """
    Data fusion: render navigated nodes into one context block.

    Nodes arrive trust-ranked (see ``DataMatrix.navigate``); content flagged
    ``verbatim_required`` is delimited and reproduced exactly.
    """
    if not nodes:
        return ""
    parts: List[str] = ["CONTEXT FROM THE DATA MATRIX (highest trust first):"]
    for node in nodes:
        header = (
            f"--- node {node.node_id} | type={node.node_type} "
            f"| trust={node.trust_score:.2f} ---"
        )
        if node.verbatim_required:
            parts.append(f"{header}\n<<<VERBATIM>>>\n{node.content}\n<<<END VERBATIM>>>")
        else:
            parts.append(f"{header}\n{node.content}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# In-memory driver
# ---------------------------------------------------------------------------

class InMemoryDataMatrix(DataMatrix):
    """
    Brute-force cosine search over an in-process dict.

    Shared by every agent thread in the swarm, so all access is serialized
    with a lock – an unlocked dict would raise "dictionary changed size
    during iteration" when a worker upserts while another searches.
    """

    def __init__(self, embedder: Embedder) -> None:
        super().__init__(embedder)
        self._nodes: Dict[str, MemoryNode] = {}
        self._vectors: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def upsert(self, node: MemoryNode) -> str:
        vector = self._embedder.embed(node.content)
        with self._lock:
            self._nodes[node.node_id] = node
            self._vectors[node.node_id] = vector
        return node.node_id

    def get(self, node_ids: Sequence[str]) -> List[MemoryNode]:
        with self._lock:
            return [self._nodes[i] for i in node_ids if i in self._nodes]

    def search(self, text: str, top_k: int = 3) -> List[MemoryNode]:
        query = self._embedder.embed(text)
        with self._lock:
            if not self._nodes:
                return []
            scored = [
                (_cosine(query, self._vectors[node_id]), node_id)
                for node_id in self._nodes
            ]
            scored.sort(key=lambda pair: -pair[0])
            return [self._nodes[node_id] for _, node_id in scored[:top_k]]

    def close(self) -> None:
        pass


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Qdrant driver
# ---------------------------------------------------------------------------

class QdrantDataMatrix(DataMatrix):
    """
    Production Data Matrix on Qdrant.

    Each ``MemoryNode`` is one point: the embedding is the vector; node type,
    verbatim content, metadata, and graph edges live in the payload.  Storing
    edges in the payload keeps the knowledge graph inside the same store –
    traversal is a ``retrieve`` by ids per hop.
    """

    def __init__(self, embedder: Embedder, url: str, collection: str) -> None:
        super().__init__(embedder)
        try:
            from qdrant_client import QdrantClient, models  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "The 'qdrant-client' package is required for the Qdrant matrix. "
                "Install it with: pip install mythos[orchestration]"
            ) from exc
        self._models = models
        # ":memory:" runs the full client in-process (no server) – used by
        # tests to exercise this driver without infrastructure.
        if url == ":memory:":
            self._client = QdrantClient(location=":memory:")
        else:
            self._client = QdrantClient(url=url)
        self._collection = collection
        if not self._client.collection_exists(collection):
            self._client.create_collection(
                collection_name=collection,
                vectors_config=models.VectorParams(
                    size=embedder.dimension,
                    distance=models.Distance.COSINE,
                ),
            )

    def upsert(self, node: MemoryNode) -> str:
        self._client.upsert(
            collection_name=self._collection,
            points=[
                self._models.PointStruct(
                    id=node.node_id,
                    vector=self._embedder.embed(node.content),
                    payload={
                        "node_type": node.node_type,
                        "content": node.content,
                        "metadata": node.metadata,
                        "edges": node.edges,
                    },
                )
            ],
        )
        return node.node_id

    def get(self, node_ids: Sequence[str]) -> List[MemoryNode]:
        if not node_ids:
            return []
        points = self._client.retrieve(
            collection_name=self._collection,
            ids=list(node_ids),
            with_payload=True,
        )
        return [self._node_from_payload(str(p.id), p.payload or {}) for p in points]

    def search(self, text: str, top_k: int = 3) -> List[MemoryNode]:
        response = self._client.query_points(
            collection_name=self._collection,
            query=self._embedder.embed(text),
            limit=top_k,
            with_payload=True,
        )
        return [
            self._node_from_payload(str(p.id), p.payload or {})
            for p in response.points
        ]

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _node_from_payload(node_id: str, payload: Dict) -> MemoryNode:
        return MemoryNode(
            node_id=node_id,
            node_type=str(payload.get("node_type", "unknown")),
            content=str(payload.get("content", "")),
            metadata=dict(payload.get("metadata") or {}),
            edges=[dict(e) for e in (payload.get("edges") or [])],
        )
