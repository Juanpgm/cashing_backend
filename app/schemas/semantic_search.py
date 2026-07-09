"""Schemas for semantic search over obligation embeddings."""

import uuid

from pydantic import BaseModel


class ObligacionSimilar(BaseModel):
    """One obligation matched by embedding similarity, with its closeness score."""

    obligacion_id: uuid.UUID
    descripcion: str
    score: float  # cosine similarity in [-1, 1]; higher = closer
