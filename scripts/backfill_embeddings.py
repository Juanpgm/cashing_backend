# ruff: noqa: T201 — backfill CLI: los print son la salida esperada.
"""Backfill obligation embeddings that are missing or stuck at zero vectors.

Before the embeddings fix, every obligation embedding fell back to a zero vector
(the service hardcoded an OpenAI model with no key). `generate_embeddings_for_contrato`
skips rows where `embedding IS NULL`, but a stored zero-vector is NOT null, so those
rows never regenerate on their own. This script re-embeds them with the now-correct
Gemini model.

SAFE: idempotent, and NEVER overwrites a good vector or writes a zero vector. A row is
touched only when its current embedding is NULL or all-zeros AND the fresh embedding is
non-zero. Default is DRY-RUN; pass --apply to write.

Run against PRODUCTION via Railway (injects prod DATABASE_URL + GEMINI_API_KEY):
    railway run -- uv run python scripts/backfill_embeddings.py            # dry-run
    railway run -- uv run python scripts/backfill_embeddings.py --apply    # write

Run locally (uses .env): drop the `railway run --` prefix.
"""

from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, ".")  # run from cashing-backend/

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.obligacion import Obligacion
from app.services.embedding_service import EMBEDDING_DIM, _call_embedding_api

BATCH = 50


def _mask_db_url(url: str) -> str:
    """Hide credentials but keep host/db visible so we can confirm the target."""
    import re

    return re.sub(r"://[^@]*@", "://***:***@", url)


def _is_zero_or_missing(embedding: str | None) -> bool:
    """True when the stored embedding is NULL or an all-zeros vector."""
    if embedding is None:
        return True
    try:
        vec = json.loads(embedding)
    except (ValueError, TypeError):
        return True  # unparseable → treat as missing, regenerate
    return not vec or all(x == 0.0 for x in vec)


async def backfill(apply: bool) -> None:
    db_url = settings.DATABASE_URL
    print(f"DB target: {_mask_db_url(db_url)}")
    if "sqlite" in db_url:
        print("WARNING: pointing at a SQLite DB, not prod Postgres. "
              "Use `railway run --` to inject the prod DATABASE_URL.")
    print(f"Mode: {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}\n")

    engine = create_async_engine(db_url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    total = 0
    candidatas = 0
    regeneradas = 0
    sin_descripcion = 0
    fallidas_aun_cero = 0

    async with async_session() as db:
        result = await db.execute(select(Obligacion))
        obligaciones = list(result.scalars().all())
        total = len(obligaciones)

        pendientes = [ob for ob in obligaciones if _is_zero_or_missing(ob.embedding)]
        candidatas = len(pendientes)
        print(f"Obligaciones totales: {total}  |  con embedding nulo/cero: {candidatas}\n")

        if not apply:
            print("DRY-RUN: no se llama a Gemini ni se escribe nada. "
                  "Corré con --apply para regenerar.")
            pendientes = []  # skip the embed loop entirely in dry-run

        for i in range(0, len(pendientes), BATCH):
            chunk = pendientes[i : i + BATCH]
            textos = [ob.descripcion or "" for ob in chunk]
            embeddings = await _call_embedding_api(textos)

            for ob, emb in zip(chunk, embeddings, strict=True):
                if not ob.descripcion:
                    sin_descripcion += 1
                    continue
                if not emb or all(x == 0.0 for x in emb):
                    # Fresh embedding STILL zero (API failed) — never overwrite with zeros.
                    fallidas_aun_cero += 1
                    continue
                if apply:
                    ob.embedding = json.dumps(emb)
                    db.add(ob)
                regeneradas += 1

            if apply:
                await db.commit()
            print(f"  procesadas {min(i + BATCH, candidatas)}/{candidatas} "
                  f"(regeneradas {regeneradas}, aún-cero {fallidas_aun_cero})")
            await asyncio.sleep(0.5)  # gentle on Gemini RPM

    await engine.dispose()

    print(
        f"\n{'=== APPLIED ===' if apply else '=== DRY-RUN (no changes written) ==='}\n"
        f"  Obligaciones totales:      {total}\n"
        f"  Candidatas (nulo/cero):    {candidatas}\n"
        f"  Regeneradas (Gemini real): {regeneradas}\n"
        f"  Sin descripción (saltadas):{sin_descripcion}\n"
        f"  Aún en cero (API falló):   {fallidas_aun_cero}\n"
        f"  Dimensión esperada:        {EMBEDDING_DIM}\n"
    )
    if not apply and candidatas > 0:
        print("Re-run with --apply to write the regenerated embeddings.")


if __name__ == "__main__":
    asyncio.run(backfill(apply="--apply" in sys.argv))
