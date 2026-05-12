"""Knowledge base retriever using ChromaDB for vector similarity search."""

from typing import Sequence
from src.config import settings
from src.knowledge.models import KnowledgeEntry
from src.utils.logging import get_logger

logger = get_logger(__name__)


class KnowledgeBaseRetriever:
    def __init__(self):
        self._entries: list[KnowledgeEntry] = []
        self._collection = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize ChromaDB collection and seed data."""
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            self._client = chromadb.PersistentClient(
                path=settings.chroma_persist_dir,
                settings=ChromaSettings(anonymized_telemetry=False),
            )

            self._collection = self._client.get_or_create_collection(
                name="parameter_knowledge",
                metadata={"hnsw:space": "cosine"},
            )
            self._initialized = True
            logger.info("chromadb initialized", path=settings.chroma_persist_dir)
        except ImportError:
            logger.warning("chromadb not installed — using in-memory fallback")
            self._client = None
            self._collection = None

    async def seed(self, entries: list[KnowledgeEntry]) -> None:
        """Seed the knowledge base with initial entries."""
        self._entries.extend(entries)

        if self._collection and entries:
            ids = [e.id for e in entries]
            documents = [e.to_document() for e in entries]
            metadatas = [
                {
                    "system": e.system,
                    "parameter_name": e.parameter_name,
                    "category": e.category,
                    "confidence": e.confidence,
                }
                for e in entries
            ]

            existing = set(self._collection.get()["ids"])
            new_entries = [
                (id_, doc, meta)
                for id_, doc, meta in zip(ids, documents, metadatas)
                if id_ not in existing
            ]

            if new_entries:
                self._collection.add(
                    ids=[e[0] for e in new_entries],
                    documents=[e[1] for e in new_entries],
                    metadatas=[e[2] for e in new_entries],
                )

            logger.info("knowledge seeded", count=len(new_entries))

    async def query(
        self,
        query: str,
        system: str | None = None,
        n_results: int = 5,
    ) -> list[KnowledgeEntry]:
        """Query the knowledge base for relevant entries."""
        results: list[KnowledgeEntry] = []

        if self._collection:
            where_filter = {"system": system} if system else None
            chroma_results = self._collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where_filter,
            )
            retrieved_ids = set(chroma_results["ids"][0])
            results = [e for e in self._entries if e.id in retrieved_ids]

        # Fallback: keyword search on in-memory entries
        query_lower = query.lower()
        if not results:
            scored = []
            for entry in self._entries:
                if system and entry.system != system:
                    continue
                score = 0
                for term in query_lower.split():
                    if term in entry.parameter_name.lower():
                        score += 3
                    if term in entry.content.lower():
                        score += 1
                    if term in entry.category.lower():
                        score += 2
                if score > 0:
                    scored.append((score, entry))
            scored.sort(key=lambda x: x[0], reverse=True)
            results = [e for _, e in scored[:n_results]]

        return results

    async def get_by_parameter(self, system: str, parameter_name: str) -> list[KnowledgeEntry]:
        """Get all knowledge entries for a specific parameter."""
        return [
            e for e in self._entries
            if e.system == system and e.parameter_name == parameter_name
        ]


# Global singleton
knowledge_base = KnowledgeBaseRetriever()
