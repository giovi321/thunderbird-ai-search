"""Qdrant vector store wrapper for Thunderbird AI Search."""

import logging
import uuid
from typing import Any, Optional

from qdrant_client import QdrantClient, models

logger = logging.getLogger(__name__)


def make_point_id(message_id: str) -> str:
    """Deterministic UUID from a Message-ID header value."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, message_id))


class VectorStore:
    """Thin wrapper around Qdrant for email vector storage."""

    def __init__(self, url: str = "http://localhost:6333", collection: str = "emails"):
        self.client = QdrantClient(url=url)
        self.collection = collection

    def ensure_collection(self, dimensions: int = 768) -> None:
        """Create the collection if it doesn't exist. Abort on dimension mismatch."""
        collections = self.client.get_collections().collections
        existing = [c for c in collections if c.name == self.collection]

        if existing:
            info = self.client.get_collection(self.collection)
            existing_dim = info.config.params.vectors.size
            if existing_dim != dimensions:
                logger.error(
                    "Collection '%s' exists with %d dimensions, expected %d. "
                    "Delete it manually or change the embedding model.",
                    self.collection, existing_dim, dimensions,
                )
                raise RuntimeError(
                    f"Dimension mismatch: collection has {existing_dim}, need {dimensions}"
                )
            logger.info("Collection '%s' already exists (%d dims)", self.collection, dimensions)
            return

        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=models.VectorParams(
                size=dimensions,
                distance=models.Distance.COSINE,
            ),
        )
        # Create payload indices for filtering
        for field, schema in [
            ("account", models.PayloadSchemaType.KEYWORD),
            ("folder", models.PayloadSchemaType.KEYWORD),
            ("from", models.PayloadSchemaType.KEYWORD),
            ("date", models.PayloadSchemaType.KEYWORD),
        ]:
            self.client.create_payload_index(
                collection_name=self.collection,
                field_name=field,
                field_schema=schema,
            )
        logger.info("Created collection '%s' (%d dims, cosine)", self.collection, dimensions)

    def upsert(self, points: list[models.PointStruct]) -> None:
        """Batch upsert points into the collection."""
        if not points:
            return
        self.client.upsert(
            collection_name=self.collection,
            points=points,
            wait=True,
        )
        logger.debug("Upserted %d points", len(points))

    def search(
        self,
        vector: list[float],
        limit: int = 10,
        account: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Search for similar emails. Returns list of dicts with payload + score."""
        query_filter = None
        if account:
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="account",
                        match=models.MatchValue(value=account),
                    )
                ]
            )

        results = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )

        return [
            {
                "id": str(point.id),
                "score": point.score,
                **(point.payload or {}),
            }
            for point in results.points
        ]

    def exists(self, point_id: str) -> bool:
        """Check if a point ID exists in the collection."""
        results, _ = self.client.scroll(
            collection_name=self.collection,
            scroll_filter=models.Filter(
                must=[
                    models.HasIdCondition(has_id=[point_id]),
                ]
            ),
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        return len(results) > 0

    def get_all_ids(self, account: Optional[str] = None) -> set[str]:
        """Return all point IDs in the collection, optionally filtered by account."""
        scroll_filter = None
        if account:
            scroll_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="account",
                        match=models.MatchValue(value=account),
                    )
                ]
            )

        all_ids: set[str] = set()
        offset = None
        while True:
            records, next_offset = self.client.scroll(
                collection_name=self.collection,
                scroll_filter=scroll_filter,
                limit=1000,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            all_ids.update(str(r.id) for r in records)
            if next_offset is None:
                break
            offset = next_offset

        return all_ids

    def count(self, account: Optional[str] = None) -> int:
        """Count points in the collection, optionally filtered by account."""
        if account:
            # Use scroll to count with filter (Qdrant count endpoint supports filters)
            result = self.client.count(
                collection_name=self.collection,
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="account",
                            match=models.MatchValue(value=account),
                        )
                    ]
                ),
                exact=True,
            )
            return result.count
        result = self.client.count(
            collection_name=self.collection,
            exact=True,
        )
        return result.count

    def delete(self, point_ids: list[str]) -> None:
        """Batch delete points by ID."""
        if not point_ids:
            return
        self.client.delete(
            collection_name=self.collection,
            points_selector=models.PointIdsList(points=point_ids),
            wait=True,
        )
        logger.debug("Deleted %d points", len(point_ids))

    def is_healthy(self) -> bool:
        """Check if Qdrant is reachable."""
        try:
            self.client.get_collections()
            return True
        except Exception:
            return False
