"""Console API — authenticated, with impersonation as an explicit privilege.

Until Phase 5 this surface took a principal handle in the request body and believed it.
It now requires a bearer token: the principal is whoever the token says, and the
entitlements are read from the database rather than from the token
(`gatekeeper.core.auth` explains why that split matters).

**Impersonation survives, because the side-by-side comparison is the point of this
console — but it is now a capability rather than a default.** Querying as someone else
requires a token carrying `gatekeeper.impersonate`, and every use is written to the audit
chain under the *real* principal's identity, so "who looked at the CFO's view" is a
question the log can answer. A token without that claim can only ever query as itself, no
matter what the request body says.

In `dev` auth mode a `/api/dev-login` endpoint mints tokens on request. That is an
unauthenticated token mint and is exactly as dangerous as it sounds, so it is refused
outside dev mode and the page says what it is.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from gatekeeper.config import get_settings
from gatekeeper.core import auth
from gatekeeper.core.db import dispose_engines, principal_session
from gatekeeper.core.models import Chunk, Document
from gatekeeper.core.principal import Principal
from gatekeeper.ingest import seed
from gatekeeper.llm.embeddings import Embedder, build_embedder
from gatekeeper.llm.generation import Generator, build_generator
from gatekeeper.llm.rerank import CrossEncoderReranker
from gatekeeper.retrieval.pipeline import DEFAULT_CONFIG, retrieve

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
    # The console should show the pipeline that actually ships, reranker included, so
    # what a reader sees here is what a deployment would return.
    _state["reranker"] = await asyncio.to_thread(CrossEncoderReranker)
    yield
    await dispose_engines()


app = FastAPI(title="gatekeeper-rag console", lifespan=lifespan)


def embedder() -> Embedder:
    return _state["embedder"]  # type: ignore[no-any-return]


def generator() -> Generator | None:
    return _state.get("generator")


def reranker() -> CrossEncoderReranker | None:
    return _state.get("reranker")


async def require_identity(
    authorization: str = Header(default=""),
) -> auth.Identity:
    """Verify the bearer token, or refuse.

    Every refusal is a 401 with the same generic message. Distinguishing "expired" from
    "bad signature" in a response tells a forger which half of the attempt worked.
    """
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "bearer token required")
    try:
        return auth.verify(token)
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=8, ge=1, le=25)
    generate: bool = True
    impersonate: list[str] = Field(default_factory=list, max_length=8)
    """Additional principals to answer as, for side-by-side comparison. Requires the
    `gatekeeper.impersonate` claim; ignored-with-403 rather than silently dropped, because
    silently answering a different question than the one asked is worse than refusing."""


class DevLoginRequest(BaseModel):
    handle: str = Field(min_length=1, max_length=64)


@app.post("/api/dev-login")
async def dev_login(request: DevLoginRequest) -> dict[str, Any]:
    """Mint a development token. Refused unless auth_mode is `dev`.

    This endpoint is an unauthenticated token mint, which is the single most dangerous
    thing in this codebase if it ever ships enabled. It exists so the console works from
    `make bootstrap` with no identity provider, and `auth.issue_dev_token` refuses outside
    dev mode rather than trusting this check alone.
    """
    settings = get_settings()
    if settings.auth_mode != "dev":
        raise HTTPException(404, "not available")
    try:
        # The demo console needs to compare principals, so dev tokens carry the
        # impersonation claim. A real IdP would grant it to a small set of operators.
        token = auth.issue_dev_token(request.handle, can_impersonate=True)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"token": token, "handle": request.handle, "mode": "dev"}


@app.get("/api/me")
async def me(identity: auth.Identity = Depends(require_identity)) -> dict[str, Any]:
    principal = await auth.resolve(identity)
    return {
        "handle": principal.external_id,
        "display_name": principal.display_name,
        "clearance": int(principal.clearance),
        "groups": principal.groups,
        "can_impersonate": identity.can_impersonate,
        "expires_at": identity.expires_at,
    }


@app.get("/api/principals")
async def list_principals(authorization: str = Header(default="")) -> list[dict[str, Any]]:
    """The demo cast. Open in dev mode because it *is* the login picker; authenticated
    otherwise, where an unauthenticated roster is a staff directory for anyone who can
    reach the port."""
    if get_settings().auth_mode != "dev":
        await require_identity(authorization)
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
async def surface(
    handle: str, identity: auth.Identity = Depends(require_identity)
) -> dict[str, Any]:
    """What this principal can see at rest, independent of any query."""
    if handle != identity.subject and not identity.can_impersonate:
        raise HTTPException(403, "this token may only query as its own principal")
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
async def ask(
    request: AskRequest, identity: auth.Identity = Depends(require_identity)
) -> dict[str, Any]:
    """Run one question as one or more principals.

    Multiple handles is the point of the console: the same query, the same ranking
    function, different results — with the difference attributable to authorization
    rather than to anything the application chose to hide.
    """
    try:
        caller = await auth.resolve(identity)
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc

    handles = [caller.external_id]
    if request.impersonate:
        if not identity.can_impersonate:
            raise HTTPException(403, "this token may not impersonate other principals")
        handles += [h for h in request.impersonate if h != caller.external_id]
        await _audit_impersonation(caller, handles[1:])

    results: list[dict[str, Any]] = []
    for handle in handles:
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

        found = await retrieve(
            principal,
            request.question,
            embedder(),
            config=DEFAULT_CONFIG,
            k=request.k,
            reranker=reranker(),
            count_withheld=True,
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
        "pipeline": DEFAULT_CONFIG.description,
        "results": results,
    }


async def _audit_impersonation(caller: Principal, targets: list[str]) -> None:
    """Record impersonation under the *real* principal.

    Attributing it to the impersonated identity would make the audit chain say the CFO
    read her own compensation data, which is precisely the wrong answer to "who looked at
    this".
    """
    from gatekeeper.core import audit as audit_log

    async with principal_session(caller) as session:
        await audit_log.append(
            session,
            caller,
            action="impersonate",
            query_text=",".join(sorted(targets)),
        )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")
