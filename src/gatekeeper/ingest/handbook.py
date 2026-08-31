"""Ingest the GitLab Handbook corpus.

Phase 0 records document identity, structure, and derived ACLs. It deliberately does not
store document bodies: the shallow git clone is the raw store, and nothing consumes the
text until the Phase 1 chunker exists. Blob storage in MinIO lands with that consumer.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import frontmatter
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from gatekeeper.core.db import admin_session
from gatekeeper.core.models import Document, Tenant
from gatekeeper.ingest.acl import AclRuleSet, load_rules
from gatekeeper.retrieval.cache import bump_epoch

HANDBOOK_REPO = "https://gitlab.com/gitlab-com/content-sites/handbook.git"
SOURCE = "gitlab-handbook"

# The ACL-interesting subset, for fast iteration. Chosen so that every sensitivity level
# and both region-scoped subtrees are represented -- a single department would not
# exercise the access model.
SMALL_PROFILE_PREFIXES = (
    "handbook/board-meetings/",
    "handbook/ceo/",
    "handbook/leadership/",
    "handbook/total-rewards/",
    "handbook/people-policies/",
    "handbook/people-group/people-compliance/",
    "handbook/legal/",
    "handbook/finance/",
    "handbook/security/security-operations/",
    "handbook/values/",
    "handbook/company/",
)

_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ParsedDocument:
    path: str
    title: str
    content_hash: str
    byte_size: int
    frontmatter: dict[str, Any]


def fetch(dest: Path, repo: str = HANDBOOK_REPO) -> Path:
    """Shallow-clone the handbook, or fast-forward an existing clone."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if (dest / ".git").exists():
        subprocess.run(["git", "-C", str(dest), "pull", "--ff-only", "--depth", "1"], check=True)
    else:
        subprocess.run(["git", "clone", "--depth", "1", repo, str(dest)], check=True)
    return dest


def parse(md_path: Path, content_root: Path) -> ParsedDocument:
    raw = md_path.read_bytes()
    post = frontmatter.loads(raw.decode("utf-8", errors="replace"))
    rel = md_path.relative_to(content_root).as_posix()

    title = str(post.metadata.get("title") or "").strip()
    if not title:
        match = _H1.search(post.content)
        title = match.group(1).strip() if match else md_path.stem.replace("-", " ").title()

    return ParsedDocument(
        path=rel,
        title=title,
        content_hash=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        # Frontmatter is arbitrary user data; coerce to JSON-safe scalars.
        frontmatter={
            k: v for k, v in post.metadata.items() if isinstance(v, str | int | float | bool)
        },
    )


def iter_documents(content_root: Path, profile: str = "full") -> Iterator[ParsedDocument]:
    for md_path in sorted(content_root.rglob("*.md")):
        rel = md_path.relative_to(content_root).as_posix()
        if profile == "small" and not rel.startswith(SMALL_PROFILE_PREFIXES):
            continue
        yield parse(md_path, content_root)


async def load(
    clone_dir: Path,
    tenant_slug: str,
    rules_path: Path,
    profile: str = "full",
) -> tuple[int, dict[str, int]]:
    """Upsert every document into Postgres with its derived ACL.

    Returns the row count and a per-rule tally, which is the number worth eyeballing:
    if one rule is matching 4,000 documents, the rule file is wrong.
    """
    content_root = clone_dir / "content"
    if not content_root.is_dir():
        raise FileNotFoundError(f"{content_root} not found -- run `make fetch` first")

    rules: AclRuleSet = load_rules(rules_path)
    tally: dict[str, int] = {}
    count = 0

    async with admin_session() as session:
        tenant_id = (
            await session.execute(select(Tenant.id).where(Tenant.slug == tenant_slug))
        ).scalar_one()

        for doc in iter_documents(content_root, profile):
            acl = rules.resolve(doc.path)
            tally[acl.rule] = tally.get(acl.rule, 0) + 1

            values = {
                "tenant_id": tenant_id,
                "source": SOURCE,
                "path": doc.path,
                "source_uri": f"https://handbook.gitlab.com/{doc.path.removesuffix('.md')}",
                "title": doc.title,
                "doc_type": "markdown",
                "content_hash": doc.content_hash,
                "byte_size": doc.byte_size,
                "sensitivity": acl.sensitivity.value,
                "owner_group": acl.owner_group,
                "allowed_groups": acl.allowed_groups,
                "need_to_know_tags": acl.need_to_know_tags,
                "min_clearance": int(acl.min_clearance),
                "jurisdiction": acl.jurisdiction,
                "acl_rule": acl.rule,
                "frontmatter": doc.frontmatter,
            }
            stmt = insert(Document).values(**values)
            # Re-ingestion is idempotent and updates ACLs in place, so editing
            # acl_rules.yaml and re-running `make seed` is a safe, fast loop.
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_document_path",
                    set_={
                        k: stmt.excluded[k]
                        for k in values
                        if k not in ("tenant_id", "source", "path")
                    }
                    | {"updated_at": func.now()},
                )
            )
            count += 1

    # Document ACLs may have changed, so retire the cache.
    await bump_epoch("corpus load")
    return count, dict(sorted(tally.items(), key=lambda kv: -kv[1]))
