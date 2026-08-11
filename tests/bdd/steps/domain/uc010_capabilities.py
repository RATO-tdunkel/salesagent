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

import pytest
from pytest_bdd import given, parsers, then, when

from tests.bdd.steps._outcome_helpers import wire_dict
from tests.bdd.steps.generic._dispatch import dispatch_request

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
    """Grade the #1329 sandbox honesty on the real wire, per the outline's ``expected`` verdict.

    This seller has no behavioral sandbox isolation, so get_adcp_capabilities (a tenant-level,
    no-argument discovery endpoint) declares ``account.sandbox=false`` UNCONDITIONALLY — it does
    not read per-account state, so the boundary_point's hypothetical response shape does not
    drive the grade. The four Examples rows map onto two graded outcomes via ``expected``:

    - ``expected == "valid"`` (rows 1-3: sandbox true / absent / explicit-false): the seller's
      honest declaration IS a spec-valid response — assert ``account.sandbox is False`` on the
      wire. Falsifiable: a regression to a dishonest ``sandbox=true`` reddens this across
      a2a/mcp/rest.
    - ``expected == "invalid"`` (row 4: "capability not declared, sandbox provisioning
      requested"): the outline's "invalid" is a REJECTED provisioning request, but this
      discovery endpoint takes no request body, performs no provisioning, and issues no
      rejection — so that outcome is not observable here. Xfailed as ungraded-at-this-boundary
      rather than left to collapse into a silent guaranteed-pass (#1329 — vacuous
      partition step).
    """
    if expected == "invalid":
        pytest.xfail(
            "get_adcp_capabilities is a no-argument, read-only discovery endpoint: it cannot grade "
            "a rejected sandbox-provisioning request (the outline's 'invalid' row). Its honest "
            "sandbox=false is a capability signal, not a rejection — that path, if modeled, belongs "
            "to a spend/provisioning tool, not this discovery call (#1329)."
        )
    if expected != "valid":
        raise AssertionError(f"unknown expected verdict {expected!r} for the sandbox-flag outline")
    body = wire_dict(ctx)
    account = body.get("account")
    assert account is not None, f"capabilities response must include an account section: {body}"
    assert account.get("sandbox") is False, (
        f"account.sandbox must be an honest False for boundary {ctx.get('sandbox_boundary')!r} "
        f"(expected valid); got {account.get('sandbox')!r}"
    )
    # The #1329 account obligation is sandbox=false AND supported_billing (BR-UC-010 names both
    # in one line); grade the billing half on the same wire so the grader closes the whole
    # obligation it is named for, not just the sandbox flag (#1329). This scenario
    # configures no tenant `supported_billing` policy, so production takes the deterministic
    # default-fallback branch (_build_account_capability -> resolve_supported_billing ->
    # account_helpers.SELLER_ACCOUNT_BILLING), whose
    # honest value is exactly {operator, agent} — pin the exact set, not mere non-emptiness, so
    # dropping/adding a party reddens the grade (order-insensitive).
    billing = account.get("supported_billing")
    assert isinstance(billing, list), f"account.supported_billing must be a list on the wire, got {billing!r}"
    assert set(billing) == {"operator", "agent"}, (
        "account.supported_billing must be the seller's honest default {'operator', 'agent'} for "
        f"this scenario (no tenant billing policy configured), got {billing!r}"
    )


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
