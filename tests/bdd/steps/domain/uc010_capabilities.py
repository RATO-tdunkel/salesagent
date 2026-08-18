"""Domain step definitions for UC-010: the account/sandbox honesty capability.

Wires ONLY the BR-UC-010 ``@T-UC-010-v31-account-sandbox`` grader (the account section's
sandbox flag) against CapabilitiesEnv, so get_adcp_capabilities' honest sandbox declaration
executes and asserts on the real a2a/mcp/rest wire — mirroring the UC-030 governance wiring
(``dispatch_request`` + ``wire_dict``). The rest of BR-UC-010 stays dormant (routed to xfail
in the conftest UC-010 branch).

Honesty contract (#1329 gap 13): this seller declares ``account.sandbox=false``
UNCONDITIONALLY. A media buy under a sandbox account routes to the exact same live adapter
path as production, so ``account.sandbox`` is the seller's honest "no behavioral isolation"
declaration, not a reflection of any account's stored flag. get_adcp_capabilities is a
TENANT-level, no-argument discovery endpoint (it does not read account rows or perform
provisioning), so the outline's four boundary rows map onto two graded outcomes (see
``then_capabilities_sandbox_flag``): the three ``expected=valid`` rows (sandbox true / absent /
explicit-false) are graded against the honest, unconditional ``sandbox=false`` on the wire
(falsifiable — a dishonest ``sandbox=true`` reddens them across a2a/mcp/rest), and the single
``expected=invalid`` row (row 4, a rejected sandbox-provisioning request) is xfailed as not
observable through this discovery call rather than allowed to collapse into a silent
guaranteed-pass.

ctx["env"] is a CapabilitiesEnv (bound by the conftest UC-010 branch).
#1329 (UC-010).
"""

from __future__ import annotations

import re

from pytest_bdd import given, parsers, then, when

from tests.bdd.steps._outcome_helpers import wire_dict
from tests.bdd.steps.generic._dispatch import dispatch_request
from tests.helpers import assert_declared_capabilities

# specialism -> parent AdCP protocol rollup (AdCP 3.1.1 compliance index). This seller emits
# only ``sales-non-guaranteed`` (audit-derived — see _DECLARED_SPECIALISMS), whose parent
# protocol is ``media_buy``, which IS in supported_protocols. A newly-emitted specialism absent
# from this map KeyErrors loudly in then_specialisms_roll_up, forcing its rollup to be verified
# before it can pass rather than silently accepted (#1329 R9-F1).
_SPECIALISM_PARENT_PROTOCOL = {"sales-non-guaranteed": "media_buy"}
_KEBAB_CASE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@given(parsers.parse("the tenant account is configured for {boundary_point}"))
def given_account_configured_for(ctx: dict, boundary_point: str) -> None:
    """Record the boundary point under test.

    get_adcp_capabilities is a TENANT-level discovery endpoint — ``account.sandbox`` on the
    response is the seller's honest capability declaration (#1329), NOT a reflection of any
    account's stored sandbox flag, so the account configuration named here deliberately does
    not alter the response. The unconditional honest ``sandbox=false`` is what the Then grades.
    """
    ctx["sandbox_boundary"] = boundary_point


@when("the Buyer Agent calls get_adcp_capabilities MCP tool")
def when_call_get_capabilities(ctx: dict) -> None:
    """Dispatch get_adcp_capabilities through the parametrized wire transport.

    The "MCP tool" wording is the Gherkin's; the actual transport (a2a/mcp/rest) is driven by
    ``pytest_generate_tests`` via ctx["transport"] (the scenario carries no transport tag), so
    the honesty declaration is graded on every wire — mirrors the UC-030 dispatch convention.
    get_adcp_capabilities is an auth-optional, no-argument discovery call (no request body).
    """
    dispatch_request(ctx)
    # Discovery is read-only and auth-optional (POST-F1): it must SUCCEED for every boundary
    # point (all four resolve to the same honest response). Assert that here so an auth/wiring
    # regression surfaces as this step, not as a confusing missing-account-section in the Then.
    assert ctx.get("error") is None, f"get_adcp_capabilities discovery must not error: {ctx.get('error')!r}"


@then(parsers.parse("the capabilities response should be {expected} for the sandbox flag"))
def then_capabilities_sandbox_flag(ctx: dict, expected: str) -> None:
    """Grade the #1329 sandbox honesty on the real wire for every boundary row.

    This seller has no behavioral sandbox isolation, so get_adcp_capabilities (a tenant-level,
    no-argument discovery endpoint) declares ``account.sandbox=false`` UNCONDITIONALLY — it does
    not read per-account state or perform provisioning, so the boundary_point does not drive the
    grade. The outline's fourth row was re-expressed from a (unobservable) provisioning-rejection
    to the honest "provisioning requested → still declares false" case, so all four rows are
    ``valid`` and grade the same unconditional honest declaration (#1329 finding 2) — no per-row
    xfail. Falsifiable: a dishonest ``sandbox=true`` reddens every row across a2a/mcp/rest.
    """
    if expected != "valid":
        raise AssertionError(f"unknown expected verdict {expected!r} for the sandbox-flag outline")
    # Grade the WHOLE declared-honesty envelope on the real wire through the single coupling
    # grader (#1329 finding 1): account.{sandbox, require_operator_auth, required_for_products,
    # account_financials, supported_billing} + adcp.idempotency.{supported, no
    # replay_ttl_seconds} + specialisms — and it fails on any emitted-but-ungraded field, so a
    # new declared capability cannot ship dark. This scenario configures no tenant
    # supported_billing, so production takes the default-fallback branch whose honest value is
    # exactly {operator, agent}. Falsifiable: a dishonest sandbox=true or a re-flipped
    # idempotency.supported=true reddens this across a2a/mcp/rest.
    assert_declared_capabilities(wire_dict(ctx))


@given("the tenant has full capabilities configured")
def given_tenant_full_capabilities(ctx: dict) -> None:
    """Record that the scenario intends a fully-configured tenant (informational).

    get_adcp_capabilities declares its idempotency posture UNCONDITIONALLY
    (_adcp_metadata → supported=False), so this Given does not drive the response — the
    Then grades the emitted posture on the wire (#1329 finding 1).
    """
    ctx["full_capabilities"] = True


@then("adcp.idempotency should be present in the response")
def then_idempotency_present(ctx: dict) -> None:
    """v3.1 REQUIRES adcp.idempotency on every capabilities response — grade its presence."""
    body = wire_dict(ctx)
    adcp_meta = body.get("adcp") or {}
    assert "idempotency" in adcp_meta, f"adcp.idempotency must be present (v3.1 required): {adcp_meta}"


@then("adcp.idempotency.supported should be a boolean discriminator")
def then_idempotency_supported_boolean(ctx: dict) -> None:
    """The union discriminator ``supported`` must be a real boolean on the wire.

    This seller declares the honest ``supported=false`` (Idempotency3) variant; grade that it
    is a boolean discriminator (not absent/null) — the withdrawal VALUE itself is graded by
    assert_declared_capabilities on the sandbox scenario + the integration wire test (#1329).
    """
    body = wire_dict(ctx)
    supported = (body.get("adcp") or {}).get("idempotency", {}).get("supported")
    assert isinstance(supported, bool), f"adcp.idempotency.supported must be a boolean discriminator, got {supported!r}"


@given(parsers.parse("the tenant claims specialisms {specialisms}"))
def given_tenant_claims_specialisms(ctx: dict, specialisms: str) -> None:
    """Record the storyboard's CLAIMED specialisms list (informational).

    This seller derives its specialisms from an HONESTY AUDIT — a hardcoded, audited set
    (_DECLARED_SPECIALISMS) — NOT from tenant config, so the claim here does NOT drive the
    response. The Thens grade the EMITTED list (what the seller honestly advertises), which is
    the real obligation for an honesty-pass seller (#1329 R9-F1).
    """
    ctx["claimed_specialisms"] = specialisms


@then("specialisms should be a unique array of kebab-case enum IDs")
def then_specialisms_kebab_unique(ctx: dict) -> None:
    """Grade the emitted specialisms on the real wire: a unique array of kebab-case enum IDs.

    Falsifiable: emptying _DECLARED_SPECIALISMS reddens (non-empty assertion); a non-kebab or
    duplicated id reddens the per-item checks (#1329 R9-F1).
    """
    body = wire_dict(ctx)
    specialisms = body.get("specialisms")
    assert isinstance(specialisms, list) and specialisms, f"specialisms must be a non-empty array: {body}"
    assert len(specialisms) == len(set(specialisms)), f"specialisms must be unique, got {specialisms}"
    for s in specialisms:
        assert isinstance(s, str) and _KEBAB_CASE.match(s), f"specialism {s!r} is not a kebab-case enum id"


@then("each specialism should roll up to a protocol in supported_protocols")
def then_specialisms_roll_up(ctx: dict) -> None:
    """Every emitted specialism's parent protocol must be in supported_protocols.

    The spec requires each declared specialism to map to a protocol the seller actually
    supports. Grading the EMITTED list against the wire's supported_protocols is what would
    surface a declaration defect (a specialism whose parent protocol is undeclared) — which is
    exactly why graduating this scenario is worthwhile, not merely green (#1329 R9-F1).
    """
    body = wire_dict(ctx)
    specialisms = body.get("specialisms") or []
    protocols = set(body.get("supported_protocols") or [])
    assert protocols, f"supported_protocols must be present to grade rollup: {body}"
    for s in specialisms:
        parent = _SPECIALISM_PARENT_PROTOCOL[s]  # loud KeyError on an unmapped emitted specialism
        assert parent in protocols, (
            f"specialism {s!r} rolls up to protocol {parent!r}, absent from supported_protocols {protocols}"
        )
