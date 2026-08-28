"""Demo console API.

**This is a demo surface, not an authenticated one.** The principal switcher is
impersonation with no credential check: anyone who can reach this port can query as the
CFO. That is deliberate for a console whose purpose is to make the access model visible
side by side, and it is why the page says so in a banner. Real authentication (OIDC,
per-request principal resolution from a token) is Phase 5. Do not expose this port.

What it does demonstrate honestly is the layer underneath: every result on the page came
back through `principal_session`, filtered by row-level security, over a connection that
cannot bypass it. Impersonating a principal here grants exactly what that principal is
entitled to and nothing more.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from gatekeeper.config import get_settings
from gatekeeper.core.db import dispose_engines, principal_session
from gatekeeper.core.models import Chunk, Document
from gatekeeper.ingest import seed
from gatekeeper.llm.embeddings import Embedder, build_embedder
from gatekeeper.llm.generation import Generator, build_generator
from gatekeeper.retrieval.search import search

STATIC = Path(__file__).parent / "static"

_state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # Loading the ONNX session takes ~18 seconds. Doing it per request would make the
    # console feel broken; doing it at startup makes the first request honest.
    _state["embedder"] = await asyncio.to_thread(
        build_embedder, settings.embedding_backend, settings.openai_api_key
    )
    _state["generator"] = build_generator(settings.openai_api_key, settings.anthropic_api_key)
    yield
    await dispose_engines()


app = FastAPI(title="gatekeeper-rag console", lifespan=lifespan)


def embedder() -> Embedder:
    return _state["embedder"]  # type: ignore[no-any-return]


def generator() -> Generator | None:
    return _state.get("generator")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    handles: list[str] = Field(min_length=1, max_length=8)
    k: int = Field(default=8, ge=1, le=25)
    generate: bool = True


@app.get("/api/principals")
async def list_principals() -> list[dict[str, Any]]:
    return [
        {
            "handle": member["external_id"],
            "display_name": member["display_name"],
            "clearance": int(member["clearance"]),  # type: ignore[call-overload]
            "groups": member["groups"],
            "region": member["region"],
            "department": member["department"],
        }
        for member in seed.CAST
    ]


@app.get("/api/surface/{handle}")
async def surface(handle: str) -> dict[str, Any]:
    """What this principal can see at rest, independent of any query."""
    try:
        principal = await seed.load_principal(handle)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    if principal.is_expired:
        # Refusing is correct; a 500 is not. The console renders this as a message so an
        # expired grant reads as an access decision rather than a broken page.
        raise HTTPException(403, f"grant expired at {principal.valid_until:%Y-%m-%d}")

    async with principal_session(principal) as session:
        documents = (await session.execute(select(func.count(Document.id)))).scalar_one()
        chunks = (await session.execute(select(func.count(Chunk.id)))).scalar_one()
        by_label = {
            row.sensitivity: row.n
            for row in await session.execute(
                select(Chunk.sensitivity, func.count(Chunk.id).label("n")).group_by(
                    Chunk.sensitivity
                )
            )
        }
    return {
        "handle": handle,
        "documents": documents,
        "chunks": chunks,
        "by_sensitivity": {
            label: by_label.get(label, 0)
            for label in ("public", "internal", "confidential", "restricted")
        },
    }


@app.post("/api/ask")
async def ask(request: AskRequest) -> dict[str, Any]:
    """Run one question as one or more principals.

    Multiple handles is the point of the console: the same query, the same ranking
    function, different results — with the difference attributable to authorization
    rather than to anything the application chose to hide.
    """
    results: list[dict[str, Any]] = []
    for handle in request.handles:
        try:
            principal = await seed.load_principal(handle)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

        if principal.is_expired:
            results.append(
                {
                    "handle": handle,
                    "display_name": principal.display_name,
                    "clearance": int(principal.clearance),
                    "groups": principal.groups,
                    "latency_ms": 0,
                    "withheld": None,
                    "expired": f"grant expired {principal.valid_until:%Y-%m-%d}",
                    "answer": None,
                    "sources": [],
                }
            )
            continue

        found = await search(
            principal, request.question, embedder(), k=request.k, count_withheld=True
        )

        answer: dict[str, Any] | None = None
        gen = generator()
        if request.generate and gen is not None and found.chunks:
            written = await asyncio.to_thread(gen.answer, request.question, found.chunks)
            answer = {
                "text": written.text,
                "cited": written.cited,
                "grounded": written.is_grounded,
                "model": written.model,
                "input_tokens": written.input_tokens,
                "output_tokens": written.output_tokens,
            }

        results.append(
            {
                "handle": handle,
                "display_name": principal.display_name,
                "clearance": int(principal.clearance),
                "groups": principal.groups,
                "latency_ms": found.latency_ms,
                "withheld": found.withheld,
                "answer": answer,
                "sources": [
                    {
                        "title": chunk.title,
                        "label": chunk.label,
                        "path": chunk.path,
                        "source_uri": chunk.source_uri,
                        "sensitivity": chunk.sensitivity,
                        "score": round(chunk.score, 4),
                        "content": chunk.content,
                    }
                    for chunk in found.chunks
                ],
            }
        )

    return {
        "question": request.question,
        "generation_available": generator() is not None,
        "embedding_model": embedder().space.model,
        "results": results,
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")
