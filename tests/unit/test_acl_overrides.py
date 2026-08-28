"""Chunk-level overrides may only tighten. These tests are the proof of that claim."""

from __future__ import annotations

from gatekeeper.config import REPO_ROOT
from gatekeeper.core.principal import Clearance, Sensitivity
from gatekeeper.ingest.acl import ChunkOverride, ResolvedAcl, load_rules, tighten

RULES = load_rules(REPO_ROOT / "corpus" / "acl_rules.yaml")


def base(**kwargs: object) -> ResolvedAcl:
    defaults: dict[str, object] = {
        "sensitivity": Sensitivity.INTERNAL,
        "min_clearance": Clearance.EMPLOYEE,
        "allowed_groups": ["engineering", "all-employees"],
        "owner_group": "engineering",
        "need_to_know_tags": [],
        "jurisdiction": [],
        "rule": "engineering",
    }
    return ResolvedAcl(**(defaults | kwargs))  # type: ignore[arg-type]


def test_sensitivity_and_clearance_take_the_maximum() -> None:
    result = tighten(
        base(),
        ChunkOverride(
            name="x",
            heading_matches=".",
            sensitivity=Sensitivity.RESTRICTED,
            min_clearance=Clearance.MANAGER,
        ),
    )
    assert result.sensitivity == Sensitivity.RESTRICTED
    assert result.min_clearance == Clearance.MANAGER


def test_an_override_cannot_lower_sensitivity_or_clearance() -> None:
    """The safety property. A typo in a YAML file must not be able to grant access."""
    strict = base(sensitivity=Sensitivity.RESTRICTED, min_clearance=Clearance.EXECUTIVE)
    result = tighten(
        strict,
        ChunkOverride(
            name="loosen",
            heading_matches=".",
            sensitivity=Sensitivity.PUBLIC,
            min_clearance=Clearance.EXTERNAL,
        ),
    )
    assert result.sensitivity == Sensitivity.RESTRICTED
    assert result.min_clearance == Clearance.EXECUTIVE


def test_tags_union_and_groups_intersect() -> None:
    result = tighten(
        base(need_to_know_tags=["pii"]),
        ChunkOverride(
            name="x",
            heading_matches=".",
            allowed_groups=["engineering", "people-ops"],
            need_to_know_tags=["compensation"],
        ),
    )
    assert result.need_to_know_tags == ["compensation", "pii"]
    # people-ops was never granted by the document, so it is not granted by the chunk.
    assert result.allowed_groups == ["engineering"]


def test_an_override_naming_no_groups_leaves_the_documents_intact() -> None:
    result = tighten(base(), ChunkOverride(name="x", heading_matches=".", need_to_know_tags=["a"]))
    assert result.allowed_groups == ["engineering", "all-employees"]


def test_override_marks_its_provenance() -> None:
    result = tighten(base(), ChunkOverride(name="comp", heading_matches="."))
    assert result.source == "override"
    assert result.rule == "engineering+comp"


# --- matching --------------------------------------------------------------


def test_heading_match_is_case_insensitive_and_scoped_by_path() -> None:
    override = ChunkOverride(
        name="x", within=["handbook/finance/**"], heading_matches="compensation"
    )
    assert override.applies_to("handbook/finance/a.md", ["Finance", "COMPENSATION Bands"])
    assert not override.applies_to("handbook/sales/a.md", ["Sales", "Compensation"])
    assert not override.applies_to("handbook/finance/a.md", ["Finance", "Travel"])


def test_public_documents_are_never_overridden() -> None:
    """A public document has no access control to tighten -- the content is readable at
    the source. Restricting one of its chunks hides it from search and protects nothing.
    This is also where heading regexes go wrong: "Equity not just equality" is a
    diversity heading on a published values page."""
    public = base(sensitivity=Sensitivity.PUBLIC, allowed_groups=[], rule="public-company")
    result = RULES.resolve_chunk(
        public, "handbook/values/_index.md", ["GitLab Values", "Equity not just equality"]
    )
    assert result.sensitivity == Sensitivity.PUBLIC
    assert result.source == "inherited"


def test_shipped_overrides_never_grant_to_an_empty_audience() -> None:
    """A tightened chunk with no groups and non-public sensitivity is readable by nobody.
    Safe, but always a mistake -- the ingest report counts these for that reason."""
    for override in RULES.chunk_overrides:
        result = tighten(base(), override)
        assert result.allowed_groups or result.sensitivity == Sensitivity.PUBLIC


def test_shipped_overrides_express_restriction_as_tags_not_groups() -> None:
    # Naming groups on an override intersects them with the document's, which empties out
    # whenever the two disagree. Tags are the right mechanism; see the rules file header.
    for override in RULES.chunk_overrides:
        assert not override.allowed_groups, f"{override.name} should use need_to_know_tags"
        assert override.need_to_know_tags, f"{override.name} tightens nothing"
