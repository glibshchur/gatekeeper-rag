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


class ResolvedAcl(BaseModel):
    """The effective access attributes for one document."""

    sensitivity: Sensitivity
    min_clearance: Clearance
    allowed_groups: list[str]
    owner_group: str | None
    need_to_know_tags: list[str]
    jurisdiction: list[str]
    rule: str


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


class AclRuleSet(BaseModel):
    version: int
    source: str
    defaults: AclRule
    rules: list[AclRule]

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


def load_rules(path: Path) -> AclRuleSet:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return AclRuleSet.model_validate(data)


@lru_cache
def cached_rules(path: Path) -> AclRuleSet:
    return load_rules(path)
