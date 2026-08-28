"""Derive document ACLs from corpus structure.

The rules live in ``corpus/acl_rules.yaml`` as data, never as Python branches. That file
is the whole authorization surface for ingestion and is meant to be read by someone who
does not read Python -- a reviewer should be able to audit who can see what without
opening a source file.

Rules are evaluated in order and the **first match wins**, so the file is ordered most
specific first. Anything unmatched falls through to ``defaults``.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from gatekeeper.core.principal import Clearance, Sensitivity


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob to a regex.

    ``**`` crosses directory separators, ``*`` and ``?`` do not. ``fnmatch`` is not used
    because its ``*`` matches ``/``, which would silently make every rule recursive.
    """
    out: list[str] = ["^"]
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


SENSITIVITY_RANK = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.INTERNAL: 1,
    Sensitivity.CONFIDENTIAL: 2,
    Sensitivity.RESTRICTED: 3,
}


class ResolvedAcl(BaseModel):
    """The effective access attributes for one document."""

    sensitivity: Sensitivity
    min_clearance: Clearance
    allowed_groups: list[str]
    owner_group: str | None
    need_to_know_tags: list[str]
    jurisdiction: list[str]
    rule: str
    source: str = "inherited"


class AclRule(BaseModel):
    name: str
    patterns: list[str]
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    min_clearance: Clearance = Clearance.EMPLOYEE
    allowed_groups: list[str] = Field(default_factory=list)
    owner_group: str | None = None
    need_to_know_tags: list[str] = Field(default_factory=list)
    jurisdiction: list[str] = Field(default_factory=list)
    # Substring token -> ISO codes. Lets one rule cover a per-country subtree without
    # writing a rule per country.
    jurisdiction_tokens: dict[str, list[str]] = Field(default_factory=dict)

    def compiled(self) -> list[re.Pattern[str]]:
        return [glob_to_regex(p) for p in self.patterns]

    def matches(self, path: str) -> bool:
        return any(rx.match(path) for rx in self.compiled())

    def jurisdiction_for(self, path: str) -> list[str]:
        lowered = path.lower()
        found = list(self.jurisdiction)
        for token, codes in self.jurisdiction_tokens.items():
            if token.lower() in lowered:
                found.extend(codes)
        return sorted(set(found))


class ChunkOverride(BaseModel):
    """A stricter ACL for the parts of a document that deserve one.

    A salary table inside an otherwise-ordinary handbook page should not be readable by
    everyone who may read the page. Overrides match on the chunk's heading path, so the
    unit of protection is the section the author wrote, not an arbitrary offset.
    """

    name: str
    within: list[str] = Field(default_factory=lambda: ["**"])
    heading_matches: str
    sensitivity: Sensitivity | None = None
    min_clearance: Clearance | None = None
    allowed_groups: list[str] = Field(default_factory=list)
    need_to_know_tags: list[str] = Field(default_factory=list)

    def applies_to(self, path: str, heading_path: list[str]) -> bool:
        if not any(glob_to_regex(p).match(path) for p in self.within):
            return False
        return re.search(self.heading_matches, " > ".join(heading_path), re.IGNORECASE) is not None


def tighten(base: ResolvedAcl, override: ChunkOverride) -> ResolvedAcl:
    """Combine a document ACL with an override so the result can only be *stricter*.

    This is a meet, not an assignment: clearance and sensitivity take the maximum, tags
    take the union, and groups take the **intersection**. Writing it this way means a
    malformed override cannot widen access -- the worst it can do is make a chunk
    unreachable, which the ingest report counts and surfaces. Letting an override simply
    replace the ACL would put "a typo in a YAML file grants access" on the table, and in
    this system that is the one outcome worth designing out entirely.
    """
    groups = (
        sorted(set(base.allowed_groups) & set(override.allowed_groups))
        if override.allowed_groups
        else list(base.allowed_groups)
    )
    sensitivity = base.sensitivity
    if (
        override.sensitivity is not None
        and SENSITIVITY_RANK[override.sensitivity] > SENSITIVITY_RANK[base.sensitivity]
    ):
        sensitivity = override.sensitivity
    clearance = base.min_clearance
    if override.min_clearance is not None and override.min_clearance > base.min_clearance:
        clearance = override.min_clearance
    return ResolvedAcl(
        sensitivity=sensitivity,
        min_clearance=clearance,
        allowed_groups=groups,
        owner_group=base.owner_group,
        need_to_know_tags=sorted(set(base.need_to_know_tags) | set(override.need_to_know_tags)),
        jurisdiction=list(base.jurisdiction),
        rule=f"{base.rule}+{override.name}",
        source="override",
    )


class AclRuleSet(BaseModel):
    version: int
    source: str
    defaults: AclRule
    rules: list[AclRule]
    chunk_overrides: list[ChunkOverride] = Field(default_factory=list)

    def resolve(self, path: str) -> ResolvedAcl:
        rule = next((r for r in self.rules if r.matches(path)), self.defaults)
        return ResolvedAcl(
            sensitivity=rule.sensitivity,
            min_clearance=rule.min_clearance,
            allowed_groups=sorted(set(rule.allowed_groups)),
            owner_group=rule.owner_group,
            need_to_know_tags=sorted(set(rule.need_to_know_tags)),
            jurisdiction=rule.jurisdiction_for(path),
            rule=rule.name,
        )

    def resolve_chunk(self, base: ResolvedAcl, path: str, heading_path: list[str]) -> ResolvedAcl:
        """Apply every matching override, in file order. Each one can only tighten.

        Overrides never apply to a **public** document. A public document has no access
        control to tighten: the content is world-readable at the source, so restricting
        one of its chunks hides it from search while changing nothing about who can read
        it. That is a retrieval regression dressed as a security control.

        It is also where heading regexes go wrong. On this corpus the rule caught
        "GitLab Values > … > Equity not just equality" -- a diversity heading, matched by
        a pattern meant for stock equity -- and would have made a published values page
        unsearchable to everyone.
        """
        if base.sensitivity == Sensitivity.PUBLIC:
            return base
        acl = base
        for override in self.chunk_overrides:
            if override.applies_to(path, heading_path):
                acl = tighten(acl, override)
        return acl


def load_rules(path: Path) -> AclRuleSet:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return AclRuleSet.model_validate(data)


@lru_cache
def cached_rules(path: Path) -> AclRuleSet:
    return load_rules(path)
