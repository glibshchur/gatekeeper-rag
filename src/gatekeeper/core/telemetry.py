"""Tracing that cannot become a side channel.

Distributed tracing is unusually dangerous in a system whose entire purpose is that some
people cannot read some rows. A span is a copy of what happened, written to a different
store, with a different retention policy and — almost always — a different, weaker access
policy than the database it describes. The default instinct when instrumenting a retrieval
pipeline is to record the query text and the document titles that came back, because that
is what makes a trace useful. Do that here and the collector becomes an unauthorized
mirror of the corpus: an operator with Jaeger access can read what the CFO searched for
and which restricted documents matched, without ever touching Postgres, and RLS will never
see the read.

So the rule this module enforces is: **spans carry shapes, never contents.**

* Query text — never. `query.chars` and `query.terms`, which are enough to correlate a
  slow trace with a heavy query, are recorded instead.
* Document ids, titles, paths, chunk content — never. Counts and latencies only.
* Principal identity — never. The *entitlement fingerprint* already computed for the query
  cache is recorded instead: it is a hash, it is stable, so two traces from the same
  entitlement bucket group together for comparison, and it names nobody.
* `withheld` — recorded, because the count of denied rows is the single most useful
  number for debugging an authorization complaint, and it discloses no content.

:func:`attributes` is the only sanctioned way to build span attributes, and
`test_no_span_carries_query_text_or_identity` asserts against the whole pipeline that
nothing bypasses it.

Tracing is **off unless an endpoint is configured**. `span()` is a null context manager in
that case — not a no-op wrapper around a real SDK object, an actual nothing — so the
default `make ask` path pays no cost and the demo needs no collector.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from gatekeeper.config import get_settings

if TYPE_CHECKING:
    from gatekeeper.core.principal import Principal

_tracer: Any | None = None
_configured = False

SERVICE_NAME = "gatekeeper-rag"

# Attribute keys that must never appear. Enforced by a test rather than by hope: the
# failure mode is silent and only discovered by someone reading a trace they should not
# have been able to read.
FORBIDDEN_KEYS = frozenset(
    {
        "query",
        "query.text",
        "question",
        "principal",
        "principal.id",
        "principal.email",
        "principal.handle",
        "subject",
        "document.title",
        "document.path",
        "chunk.content",
        "answer",
    }
)


def configure() -> bool:
    """Wire up OTLP export if an endpoint is set. Returns whether tracing is on.

    Idempotent, and safe to call from the API, the CLI and the worker alike — each is a
    separate process and each needs its own provider.
    """
    global _tracer, _configured
    if _configured:
        return _tracer is not None
    _configured = True

    endpoint = get_settings().otel_endpoint
    if not endpoint:
        return False

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(SERVICE_NAME)
    return True


def attributes(**values: Any) -> dict[str, Any]:
    """Build span attributes, refusing any key on the deny list.

    A hard failure rather than a silent drop. An attribute quietly discarded in production
    is indistinguishable from one that was never added, and the person who added it would
    keep believing the trace was richer than it is.
    """
    for key in values:
        if key in FORBIDDEN_KEYS:
            raise ValueError(
                f"span attribute {key!r} would copy protected content into the trace store; "
                "record a shape (a count, a length, a fingerprint) instead"
            )
    return {k: v for k, v in values.items() if v is not None}


@contextlib.contextmanager
def span(name: str, **values: Any) -> Iterator[Any]:
    """One pipeline stage. A null context manager when tracing is off."""
    attrs = attributes(**values)
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name, attributes=attrs) as current:
        yield current


def set_attributes(current: Any, **values: Any) -> None:
    """Record what a stage learned, after it has run. Safe when tracing is off."""
    if current is None:
        return
    for key, value in attributes(**values).items():
        current.set_attribute(key, value)


def principal_attrs(principal: Principal, epoch: int = 0) -> dict[str, Any]:
    """Everything about the caller a trace may know.

    The fingerprint is the same hash the query cache keys on, and reusing it is the point:
    traces group by the thing that actually determines what a query can return, which is
    more useful for debugging a latency difference than a username would be, and it names
    nobody.
    """
    from gatekeeper.retrieval.cache import entitlement_fingerprint

    return {
        "tenant.id": str(principal.tenant_id),
        "entitlement.fingerprint": entitlement_fingerprint(principal, epoch)[:16],
        "entitlement.clearance": int(principal.clearance),
        "entitlement.groups": len(principal.groups),
    }
