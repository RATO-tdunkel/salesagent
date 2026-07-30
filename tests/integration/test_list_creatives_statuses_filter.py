"""Integration tests for the list_creatives statuses filter (#1502).

`list_creatives` accepts a structured ``CreativeFilters.statuses`` array with match-any
semantics — a creative matches if its status is any one of the requested statuses. Before
this fix, ``_list_creatives_impl`` derived the DB-query status from only the FIRST element
of the structured list (``enum_value(req_filters.statuses[0])``) while
``query_summary.filters_applied`` echoed the whole array — so a buyer sending a multi-status
filter like ``["approved", "rejected"]`` had it silently narrowed to ``["approved"]`` and
received FEWER creatives than ``filters_applied`` claimed. The report did not match the
scoped result set.

The fix mirrors the ``concept_ids`` thread-into-query pattern (#1493): the structured
``statuses`` value (into which the flat ``status`` is already folded, flat-wins, by
``_build_list_creatives_request``) is threaded in full into
``CreativeRepository.get_by_principal`` and applied via ``Creative.status.in_(...)``.

Spec: AdCP 3.1.1 ``core/creative-filters.json`` — ``statuses`` is an array filter with
match-any semantics (grounded in the array/minItems:1 shape and the file's top-level
archived-by-default description, not a verbatim per-field phrase). Verified on every wire
transport (a2a/mcp/rest); the structured filters object reaches all three after #1493.
"""

import pytest

from tests.harness import CreativeListEnv
from tests.harness.transport import ALL_WIRE, TransportResult
from tests.helpers.creative_test_helpers import assert_empty_array_filter_rejected, seed_creative_in_status

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _returned_creative_ids(result: TransportResult) -> set[str]:
    """The set of creative_ids in the success-path wire response."""
    return {c["creative_id"] for c in result.wire_response["creatives"]}


class TestStatusesFilterApplied:
    """filters.statuses actually scopes the result set — not merely reported as applied."""

    @pytest.mark.parametrize("transport", ALL_WIRE)
    def test_statuses_filter_scopes_results(self, integration_db, transport):
        """statuses=["approved"] returns only approved; a rejected creative is excluded.

        This single-value case pins the clause end-to-end: the rejected decoy is the
        falsifiable negative control that leaks back if ``Creative.status.in_(...)`` (or
        the effective_statuses threading in _impl) is removed entirely. It does NOT grade
        the specific bug this PR fixes — a single-element filter was already applied on the
        unfixed code, so this case alone passes there; the multi-value case below is the
        one that reddens on the real regression.
        """
        with CreativeListEnv() as env:
            tenant, principal = env.setup_default_data()
            keep = seed_creative_in_status(tenant, principal, "approved")
            seed_creative_in_status(tenant, principal, "rejected")  # decoy — must be excluded

            result = env.call_via(transport, filters={"statuses": ["approved"]})

            assert not result.is_error, f"{transport}: {result.error!r}"
            assert _returned_creative_ids(result) == {keep}, (
                f"{transport}: rejected decoy leaked — statuses filter not applied"
            )

    @pytest.mark.parametrize("transport", ALL_WIRE)
    def test_multi_value_statuses_matches_any(self, integration_db, transport):
        """statuses=["approved","rejected"] returns both; a third-status creative is excluded.

        This is the case that grades the fix: pre-fix the array was narrowed to its first
        element (``["approved"]``), so the rejected creative was dropped and the buyer got
        fewer creatives than filters_applied claimed. Reverting the full-list threading
        (back to ``statuses[0]``) reddens here.
        """
        with CreativeListEnv() as env:
            tenant, principal = env.setup_default_data()
            approved = seed_creative_in_status(tenant, principal, "approved")
            rejected = seed_creative_in_status(tenant, principal, "rejected")
            seed_creative_in_status(tenant, principal, "pending_review")  # third status — excluded

            result = env.call_via(transport, filters={"statuses": ["approved", "rejected"]})

            assert not result.is_error, f"{transport}: {result.error!r}"
            assert _returned_creative_ids(result) == {approved, rejected}, (
                f"{transport}: expected only the two matching statuses"
            )


class TestStatusesFilterReportedTruthfully:
    """filters_applied reports statuses AND that report is now truthful — it matches the
    actually-scoped result set. query_summary.filters_applied is an ordinary success-envelope
    field, parametrized over every wire transport (a2a/mcp/rest) because the three paths are
    not interchangeable: input coercion differs (``coerce_creative_filters`` for REST/A2A vs
    the FastMCP TypeAdapter), each wrapper forwards a different subset of ``list_creatives_raw``'s
    params, and a future ``model_dump()`` override on QuerySummary/ListCreativesResponse would
    be honored on REST/A2A yet bypassed by MCP's ``structured_content`` path. A per-transport
    regression in any of those would slip an asserted-on-REST-only test."""

    @pytest.mark.parametrize("transport", ALL_WIRE)
    def test_filters_applied_matches_scoped_results(self, integration_db, transport):
        with CreativeListEnv() as env:
            tenant, principal = env.setup_default_data()
            keep = seed_creative_in_status(tenant, principal, "approved")
            seed_creative_in_status(tenant, principal, "rejected")

            result = env.call_via(transport, filters={"statuses": ["approved"]})

            assert not result.is_error, f"{transport}: {result.error!r}"
            filters_applied = result.wire_response["query_summary"]["filters_applied"]
            # Reported as the enum value ("approved") — the coerced list the query used.
            assert "statuses=approved" in filters_applied, f"{transport}: {filters_applied}"
            # ...and the report is truthful: the scoped set matches what was claimed.
            assert _returned_creative_ids(result) == {keep}, f"{transport}: scoped set != reported filters_applied"


class TestStatusesFilterValidation:
    """A malformed statuses filter (empty array) is rejected with a spec envelope on every
    wire transport — the structurally identical sibling of the concept_ids ``minItems:1``
    case (``test_list_creatives_concept_filter.py``). Gives the repository's "``[]`` never
    legitimately reaches here" comment a wire oracle: ``[]`` is a ``minItems:1``
    VALIDATION_ERROR upstream, never a "match nothing" narrowing."""

    @pytest.mark.parametrize("transport", ALL_WIRE)
    def test_empty_statuses_array_emits_validation_envelope(self, integration_db, transport):
        """filters={'statuses': []} violates minItems:1 → two-layer VALIDATION_ERROR on every transport."""
        with CreativeListEnv() as env:
            env.setup_default_data()
            assert_empty_array_filter_rejected(env, transport, "statuses")
