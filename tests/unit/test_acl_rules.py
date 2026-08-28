"""The ACL rule file is the authorization surface for ingestion. Test it like one."""

from __future__ import annotations

from pathlib import Path

import pytest

from gatekeeper.config import REPO_ROOT
from gatekeeper.core.principal import Clearance, Sensitivity
from gatekeeper.ingest.acl import AclRule, AclRuleSet, glob_to_regex, load_rules

RULES_PATH = REPO_ROOT / "corpus" / "acl_rules.yaml"


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        # `**` crosses separators, `*` does not. This is the whole reason we do not use
        # fnmatch, whose `*` would make every rule accidentally recursive.
        ("a/**", "a/b/c.md", True),
        ("a/*", "a/b/c.md", False),
        ("a/*", "a/b.md", True),
        ("a/**/*.md", "a/b/c/d.md", True),
        ("a/**", "ab/c.md", False),
        ("a/b.md", "a/b.md", True),
        ("a/b.md", "a/bXmd", False),  # the dot must be escaped
        ("**", "anything/at/all.md", True),
    ],
)
def test_glob_semantics(pattern: str, path: str, expected: bool) -> None:
    assert bool(glob_to_regex(pattern).match(path)) is expected


def test_first_match_wins() -> None:
    ruleset = AclRuleSet(
        version=1,
        source="test",
        defaults=AclRule(name="fallback", patterns=["**"]),
        rules=[
            AclRule(name="specific", patterns=["a/secret/**"], sensitivity=Sensitivity.RESTRICTED),
            AclRule(name="broad", patterns=["a/**"], sensitivity=Sensitivity.INTERNAL),
        ],
    )
    assert ruleset.resolve("a/secret/x.md").rule == "specific"
    assert ruleset.resolve("a/other/x.md").rule == "broad"
    assert ruleset.resolve("z/x.md").rule == "fallback"


def test_jurisdiction_tokens_are_derived_from_the_path() -> None:
    rules = load_rules(RULES_PATH)
    nl = rules.resolve(
        "handbook/total-rewards/benefits/general-and-entity-benefits/bv-benefits-netherlands.md"
    )
    assert nl.jurisdiction == ["NL"]

    india = rules.resolve("handbook/people-policies/india-ltd/leave-policy.md")
    assert india.jurisdiction == ["IN"]

    # A document with no country token in its path is global, not mis-assigned.
    assert rules.resolve("handbook/total-rewards/benefits/modern-health.md").jurisdiction == []


def test_shipped_rules_load_and_cover_the_sensitivity_range() -> None:
    rules = load_rules(RULES_PATH)
    assert rules.source == "gitlab-handbook"
    labels = {r.sensitivity for r in rules.rules}
    assert labels == set(Sensitivity), "every sensitivity level should be reachable"


def test_executive_material_requires_executive_clearance() -> None:
    rules = load_rules(RULES_PATH)
    for path in ("handbook/board-meetings/2024-q1.md", "handbook/ceo/shadow.md"):
        acl = rules.resolve(path)
        assert acl.min_clearance == Clearance.EXECUTIVE
        assert acl.sensitivity == Sensitivity.RESTRICTED
        assert acl.allowed_groups == ["executives"]


def test_public_documents_grant_no_groups() -> None:
    # Public material must be readable without group membership; if a rule labels
    # something public *and* group-gated, the two signals disagree.
    rules = load_rules(RULES_PATH)
    for rule in rules.rules:
        if rule.sensitivity == Sensitivity.PUBLIC:
            assert rule.allowed_groups == []
            assert rule.min_clearance == Clearance.EXTERNAL


def test_rules_file_is_the_only_source_of_truth() -> None:
    # Guards against someone "temporarily" hardcoding a path in Python.
    src = (REPO_ROOT / "src" / "gatekeeper" / "ingest" / "acl.py").read_text()
    assert "handbook/" not in src


def test_missing_rules_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_rules(tmp_path / "nope.yaml")
