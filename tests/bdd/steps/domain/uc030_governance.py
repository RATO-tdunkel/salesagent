"""Domain step definitions for UC-030: Manage Governance Binding (sync_governance).

Wires the in-scope BR-UC-030 ``@sync`` scenarios — the seller-side governance
binding — against the shared cross-transport harness (GovernanceSyncEnv), so the
core success path, the per-account authority failure, the partial-failure model,
and the request-validation boundary all execute and assert on the wire across
a2a/mcp/rest (no IMPL — BDD grades wire conformance).

Out of scope (routed to ``_UC030_XFAIL_TAGS`` in conftest, not stepped here):
- ``@check`` scenarios grade ``check_governance`` (enforcement), a capability this
  agent deliberately does not declare (``governance-aware-seller``).
- Idempotency replay / IDEMPOTENCY_CONFLICT and per-operation scope
  (PERMISSION_DENIED) grade behavior this PR defers.
- ``@sync @bva`` request-validation boundary outlines (cardinality, schemes, url) ARE
  wired here (``when_bva_*`` + ``then_request_verdict``); the ``@bva`` outlines that need
  account seeding (response-shape rows) or an unimplemented feature (idempotency replay)
  stay deferred in the conftest UC-030 branch.

Reuses the shared auth Givens ("the Buyer Agent has an authenticated/unauthenticated
connection") and the generic ``the error code is "X"`` step (uc011_accounts), which
are registered globally — this module defines only governance-specific steps.

ctx["env"] is a GovernanceSyncEnv (bound by the conftest UC-030 branch).
ctx["response"] / ctx["error"] / ctx["wire_response"] / ctx["wire_error_envelope"]
are populated by dispatch_request.

#1329 (UC-030)
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from tests.bdd.steps._outcome_helpers import _require_response, wire_dict
from tests.bdd.steps.generic._dispatch import dispatch_request
from tests.factories import AccountFactory
from tests.harness.transport import Transport, _pinned_error_metadata
from tests.helpers.accounts import seed_account_with_access
from tests.helpers.governance import (
    DEFAULT_URL,
    LEAK_SECRET,
    account_entry,
    governance_agent_dict,
    leaky_governance_agent,
    persisted_governance_urls,
    url_eq,
)

# A valid, well-formed idempotency_key (pattern ^[A-Za-z0-9_.:-]{16,255}$) and
# Bearer credentials (minLength 32) for scenarios that need a well-formed request
# so the assertion-under-test (auth, account resolution) is what actually fires.
_VALID_KEY = "uuid-v4-bdd-00000000000001"


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _tenant_principal(ctx: dict) -> tuple[Any, Any]:
    """Return the tenant/principal the shared auth Given set up in ctx."""
    return ctx["tenant"], ctx["principal"]


def _owned_account(ctx: dict, account_id: str) -> Any:
    """Create an account the authenticated agent has authority over (access grant)."""
    tenant, principal = _tenant_principal(ctx)
    account = seed_account_with_access(tenant, principal, account_id=account_id)
    ctx.setdefault("gov_accounts", {})[account_id] = account
    return account


def _unowned_account(ctx: dict, account_id: str) -> Any:
    """Create an account WITHOUT an access grant (agent has no authority over it)."""
    tenant, _principal = _tenant_principal(ctx)
    account = AccountFactory(tenant=tenant, account_id=account_id)
    ctx.setdefault("gov_accounts", {})[account_id] = account
    return account


def _agent(url: str, *, cred_len: int = 64, credentials: str | None = None, scheme: str = "Bearer") -> dict[str, Any]:
    """Build a request-side governance agent dict (url + authentication)."""
    return governance_agent_dict(url, cred_len=cred_len, credentials=credentials, scheme=scheme)


def _account_entry(account_id: str, agents: list[dict[str, Any]]) -> dict[str, Any]:
    # Thin id-form wrapper over the shared request-element builder (#1329).
    return account_entry({"account_id": account_id}, agents=agents)


# The credential-channel scenarios' leak secret + agent builder are the SHARED
# tests.helpers.governance.LEAK_SECRET / leaky_governance_agent — one home for the leak
# contract so this transport-blind BDD grade and the A2A+REST integration grade cannot drift
# on the secret value or the mistyped-key shape (#1329 R9-K4).


def _dispatch(ctx: dict, transport: str, *, identity: Any = "__keep__", **kwargs: Any) -> None:
    """Dispatch raw kwargs through the parametrized wire transport.

    ``transport`` (the "via MCP"/"via REST" token from the Gherkin) is accepted
    but IGNORED — pytest_generate_tests controls the actual transport
    (a2a/mcp/rest) via ``ctx["transport"]``, so each scenario executes across all
    wire transports (mirrors the shared auth Given's convention). Raw kwargs (not
    a pre-built request) are sent so request validation happens at the transport
    boundary and produces a real AdCP wire envelope.
    """
    if identity == "__keep__":
        dispatch_request(ctx, **kwargs)
    else:
        dispatch_request(ctx, identity=identity, **kwargs)


def _wire_accounts(ctx: dict) -> list[dict[str, Any]]:
    return wire_dict(ctx).get("accounts") or []


def _wire_account(ctx: dict, account_id: str) -> dict[str, Any]:
    """Return the wire per-account entry whose echoed ref matches ``account_id``.

    Finding the entry by its requested id IS the account-ref echo grade: if the wire
    did not echo the requested ref, this raises "No wire account". Callers therefore do
    NOT re-assert ``acct["account"]["account_id"] == account_id`` — that would be
    tautological against this lookup (#1329).
    """
    for acct in _wire_accounts(ctx):
        ref = acct.get("account") or {}
        if ref.get("account_id") == account_id:
            return acct
    available = [(a.get("account") or {}).get("account_id") for a in _wire_accounts(ctx)]
    raise AssertionError(f"No wire account {account_id!r}. Available: {available}")


# ═══════════════════════════════════════════════════════════════════════
# Given — authority setup (governance-specific)
# ═══════════════════════════════════════════════════════════════════════


@given(parsers.parse('the agent has authority over account "{account_id}"'))
def given_authority_over(ctx: dict, account_id: str) -> None:
    _owned_account(ctx, account_id)


@given(parsers.parse('the agent has authority over accounts "{a}" and "{b}"'))
def given_authority_over_two(ctx: dict, a: str, b: str) -> None:
    _owned_account(ctx, a)
    _owned_account(ctx, b)


@given(parsers.parse('the agent does NOT have authority over account "{account_id}"'))
def given_no_authority_over(ctx: dict, account_id: str) -> None:
    # The account exists in the tenant but carries no access grant for this agent,
    # so resolve_account raises AdCPAuthorizationError, which _sync_one_account
    # collapses to the uniform per-account ACCOUNT_NOT_FOUND (no enumeration oracle).
    _unowned_account(ctx, account_id)


@given(parsers.parse('no governance agent is currently bound to "{account_id}"'))
def given_no_binding(ctx: dict, account_id: str) -> None:
    account = ctx.get("gov_accounts", {}).get(account_id)
    assert account is not None, f"account {account_id!r} must be set up by a prior authority step"
    assert not account.governance_agents, (
        f"expected no prior binding on {account_id!r}, got {account.governance_agents}"
    )


@given(parsers.parse('account "{account_id}" is currently bound to governance agent "{url}"'))
def given_currently_bound(ctx: dict, account_id: str, url: str) -> None:
    """Seed a prior binding by dispatching a FIRST sync (the real write path), so the
    scenario's When exercises genuine replace-over-existing, not bind-from-empty. The
    account must already be owned (a prior authority Given created it). Records the prior
    url so the "no longer present" Then knows which account was replaced.
    """
    _dispatch(
        ctx,
        ctx.get("transport"),
        idempotency_key="uuid-v4-prebind-00000000000001",
        accounts=[_account_entry(account_id, [_agent(url)])],
    )
    acct = _wire_account(ctx, account_id)
    assert acct["status"] == "synced", f"pre-binding first sync must succeed on {account_id!r}: {acct}"
    ctx.setdefault("prior_bindings", {})[account_id] = url


@given(parsers.parse('the agent has authority over the implicit account for brand "{brand}" on operator "{operator}"'))
def given_authority_over_implicit(ctx: dict, brand: str, operator: str) -> None:
    """Seed a natural-key account (brand.domain + operator, non-sandbox) the agent owns.

    Unlike ``_owned_account`` (account_id only), the implicit-account scenario resolves by
    natural key, so the row must carry the operator + brand.domain the request references —
    the canonical seeder carries both (#1329).
    """
    tenant, principal = _tenant_principal(ctx)
    account_id = "acc-nk-" + f"{brand}-{operator}".replace(".", "-")
    account = seed_account_with_access(
        tenant, principal, account_id=account_id, operator=operator, brand_domain=brand, sandbox=False
    )
    ctx.setdefault("gov_accounts", {})[account_id] = account


# ═══════════════════════════════════════════════════════════════════════
# When — sync_governance dispatch (governance-specific)
# ═══════════════════════════════════════════════════════════════════════


@when(
    parsers.parse(
        'the Buyer Agent sends a sync_governance request via {transport} with idempotency_key "{key}" '
        'and one account "{account_id}" bound to governance agent "{url}" with Bearer credentials of length {n:d}'
    )
)
@when(
    parsers.parse(
        'the Buyer Agent sends a sync_governance request via {transport} with idempotency_key "{key}" '
        'and account "{account_id}" bound to governance agent "{url}" with Bearer credentials of length {n:d}'
    )
)
@when(
    parsers.parse(
        'the Buyer Agent sends a sync_governance request via {transport} with idempotency_key "{key}" '
        'naming only account "{account_id}" bound to governance agent "{url}" with Bearer credentials of length {n:d}'
    )
)
def when_sync_one_account(ctx: dict, transport: str, key: str, account_id: str, url: str, n: int) -> None:
    """Sync a single named account. Three Gherkin phrasings share one body — "one account"
    / "account" / "naming only account" (the last is the per-account replace-scope scenario,
    which names only one of two owned accounts)."""
    _dispatch(ctx, transport, idempotency_key=key, accounts=[_account_entry(account_id, [_agent(url, cred_len=n)])])


@when(
    parsers.parse(
        'the Buyer Agent sends a sync_governance request via {transport} with idempotency_key "{key}" '
        'and one account referenced by brand "{brand}" on operator "{operator}" bound to governance agent "{url}" '
        "with Bearer credentials of length {n:d}"
    )
)
def when_sync_natural_key_account(
    ctx: dict, transport: str, key: str, brand: str, operator: str, url: str, n: int
) -> None:
    """Sync governance to an account referenced by natural key (brand + operator), not id."""
    ref = {"brand": {"domain": brand}, "operator": operator, "sandbox": False}
    _dispatch(
        ctx,
        transport,
        idempotency_key=key,
        accounts=[account_entry(ref, agents=[_agent(url, cred_len=n)])],
    )


@when(
    parsers.parse(
        'the Buyer Agent sends a sync_governance request via {transport} with idempotency_key "{key}" '
        'and account "{account_id}" bound to governance agent "{url}" with Bearer credentials "{credentials}"'
    )
)
def when_sync_account_literal_creds(
    ctx: dict, transport: str, key: str, account_id: str, url: str, credentials: str
) -> None:
    _dispatch(
        ctx,
        transport,
        idempotency_key=key,
        accounts=[_account_entry(account_id, [_agent(url, credentials=credentials)])],
    )


@when(
    parsers.parse(
        'the Buyer Agent sends a sync_governance request with idempotency_key "{key}" and '
        'account "{account_id}" whose governance agent leaks a secret via {channel}'
    )
)
def when_sync_leaky_agent(ctx: dict, key: str, account_id: str, channel: str) -> None:
    """Dispatch a request whose governance agent carries a secret on the named credential channel.

    The request is rejected at the validation boundary (before account resolution), so no
    account seeding is needed — only the shared auth Given (so the request passes auth and
    reaches the boundary). The leaked secret is stashed for the absence assertion. Transport
    comes from parametrization (a2a/mcp/rest), so this grades every wire.
    """
    ctx["leaked_secret"] = LEAK_SECRET
    ctx["leak_channel"] = channel
    _dispatch(ctx, "", idempotency_key=key, accounts=[_account_entry(account_id, [leaky_governance_agent(channel)])])


@when(
    parsers.parse(
        'the Buyer Agent sends a sync_governance request via {transport} with idempotency_key "{key}" '
        'and two accounts "{a}" and "{b}" both bound to governance agent "{url}"'
    )
)
def when_sync_two_accounts(ctx: dict, transport: str, key: str, a: str, b: str, url: str) -> None:
    _dispatch(
        ctx,
        transport,
        idempotency_key=key,
        accounts=[_account_entry(a, [_agent(url)]), _account_entry(b, [_agent(url)])],
    )


@when(
    parsers.parse(
        'the Buyer Agent sends a sync_governance request via {transport} with idempotency_key "{key}" '
        'and account "{account_id}" bound to TWO governance agents "{u1}" and "{u2}"'
    )
)
def when_sync_two_agents(ctx: dict, transport: str, key: str, account_id: str, u1: str, u2: str) -> None:
    _dispatch(ctx, transport, idempotency_key=key, accounts=[_account_entry(account_id, [_agent(u1), _agent(u2)])])


@when(
    parsers.parse(
        'the Buyer Agent sends a sync_governance request via {transport} with idempotency_key "{key}" '
        'and account "{account_id}" with an empty governance_agents array'
    )
)
def when_sync_empty_agents(ctx: dict, transport: str, key: str, account_id: str) -> None:
    _dispatch(ctx, transport, idempotency_key=key, accounts=[_account_entry(account_id, [])])


@when(
    parsers.parse(
        'the Buyer Agent sends a sync_governance request via {transport} with idempotency_key "{key}" '
        'and {n:d} accounts each bound to "{url}"'
    )
)
def when_sync_n_accounts(ctx: dict, transport: str, key: str, n: int, url: str) -> None:
    accounts = [_account_entry(f"acct-{i}", [_agent(url)]) for i in range(n)]
    _dispatch(ctx, transport, idempotency_key=key, accounts=accounts)


@when(
    parsers.parse(
        "the Buyer Agent sends a sync_governance request via {transport} without an idempotency_key "
        'and one account "{account_id}"'
    )
)
def when_sync_no_key(ctx: dict, transport: str, account_id: str) -> None:
    # Well-formed agent so the ONLY defect is the missing key.
    _dispatch(ctx, transport, accounts=[_account_entry(account_id, [_agent(DEFAULT_URL)])])


@when(
    parsers.parse(
        "the Buyer Agent sends a sync_governance request via {transport} without an authentication token "
        'and one account "{account_id}"'
    )
)
def when_sync_no_auth(ctx: dict, transport: str, account_id: str) -> None:
    # Well-formed request so the operation-level failure is AUTH_REQUIRED, not validation.
    _dispatch(
        ctx,
        transport,
        identity=None,
        idempotency_key=_VALID_KEY,
        accounts=[_account_entry(account_id, [_agent(DEFAULT_URL)])],
    )


@when(
    parsers.parse(
        'the Buyer Agent sends a sync_governance request via {transport} with idempotency_key "{key}" '
        'and one account "{account_id}"'
    )
)
def when_sync_key_boundary(ctx: dict, transport: str, key: str, account_id: str) -> None:
    # idempotency_key boundary scenarios: vary only the key; keep the rest well-formed.
    _dispatch(ctx, transport, idempotency_key=key, accounts=[_account_entry(account_id, [_agent(DEFAULT_URL)])])


# ═══════════════════════════════════════════════════════════════════════
# When — @bva request-validation boundary outlines (#1329 R9-F1 / Konstantin item 1)
# ═══════════════════════════════════════════════════════════════════════
#
# These wire the @sync @bva boundary outlines whose rows are all REQUEST-VALIDATION cases (no
# account seeding): the request is dispatched over the parametrized wire and graded by
# then_request_verdict. The construction-time boundary grades in TestSyncGovernanceBoundaryValues
# stay; this adds the missing WIRE grade the round-8 xfail gate withheld. Outlines with a
# response-shape row (credentials "present on response", per-account status enum) or a deferred
# replay row (idempotency_key) stay xfailed in the conftest UC-030 branch — they need seeding or
# an unimplemented feature, not just request validation.

# A well-formed agent; only the boundary-under-test deviates from it.
_BVA_CREDS = "x" * 64


def _bva_agent(**overrides: Any) -> dict[str, Any]:
    agent: dict[str, Any] = {"url": DEFAULT_URL, "authentication": {"schemes": ["Bearer"], "credentials": _BVA_CREDS}}
    agent.update(overrides)
    return agent


@when(
    parsers.parse(
        'the Buyer Agent sends a sync_governance request exercising the governance_agents boundary case "{boundary}"'
    )
)
def when_bva_governance_agents(ctx: dict, boundary: str) -> None:
    agents = {
        "governance_agents has 0 entries": [],
        "governance_agents has 2 entries": [_bva_agent(), _bva_agent()],
    }[boundary]
    _dispatch(ctx, "", idempotency_key=_VALID_KEY, accounts=[_account_entry("acct-bva", agents)])


@when(
    parsers.parse('the Buyer Agent sends a sync_governance request exercising the accounts boundary case "{boundary}"')
)
def when_bva_accounts(ctx: dict, boundary: str) -> None:
    n = {"accounts has 0 entries": 0, "accounts has 100 entries": 100, "accounts has 101 entries": 101}[boundary]
    accounts = [_account_entry(f"acct-bva-{i}", [_bva_agent()]) for i in range(n)]
    _dispatch(ctx, "", idempotency_key=_VALID_KEY, accounts=accounts)


@when(
    parsers.parse(
        "the Buyer Agent sends a sync_governance request exercising the authentication.schemes "
        'boundary case "{boundary}"'
    )
)
def when_bva_auth_schemes(ctx: dict, boundary: str) -> None:
    auth: dict[str, Any] = {
        "exactly one valid scheme": {"schemes": ["Bearer"], "credentials": _BVA_CREDS},
        "empty array (0 items)": {"schemes": [], "credentials": _BVA_CREDS},
        "two items": {"schemes": ["Bearer", "Bearer"], "credentials": _BVA_CREDS},
        "single item outside enum": {"schemes": ["definitely-not-a-scheme"], "credentials": _BVA_CREDS},
        "schemes absent": {"credentials": _BVA_CREDS},
    }[boundary]
    _dispatch(
        ctx, "", idempotency_key=_VALID_KEY, accounts=[_account_entry("acct-bva", [_bva_agent(authentication=auth)])]
    )


@when(parsers.parse('the Buyer Agent sends a sync_governance request exercising the url boundary case "{boundary}"'))
def when_bva_url(ctx: dict, boundary: str) -> None:
    auth = {"schemes": ["Bearer"], "credentials": _BVA_CREDS}
    if boundary == "https:// URL":
        agent = _bva_agent()
    elif boundary == "http:// URL (plaintext)":
        agent = {"url": "http://governance.example.com/hook", "authentication": auth}
    elif boundary == "non-uri string":
        agent = {"url": "not-a-uri", "authentication": auth}
    elif boundary == "url absent":
        agent = {"authentication": auth}
    else:
        raise AssertionError(f"unknown url boundary {boundary!r}")
    _dispatch(ctx, "", idempotency_key=_VALID_KEY, accounts=[_account_entry("acct-bva", [agent])])


@then(parsers.parse('the request verdict is "{verdict}"'))
def then_request_verdict(ctx: dict, verdict: str) -> None:
    """Grade a @bva request-validation boundary on the real wire.

    ``invalid`` → a top-level VALIDATION_ERROR envelope (mutation: relax the boundary check and
    this reddens). ``valid`` → the request is ACCEPTED at the validation boundary; the response is
    the success variant (an unseeded account then fails per-account resolution, which is NOT a
    top-level error), so assert the dispatch did not error (#1329 R9-F1).
    """
    result = ctx["result"]
    if verdict == "invalid":
        result.assert_wire_error("VALIDATION_ERROR")
    elif verdict == "valid":
        assert not result.is_error, (
            "a boundary-valid request must be accepted at validation (per-account resolution may "
            "still fail on an unseeded account — the success variant, not a top-level error); "
            f"got wire error {result.wire_error_envelope!r}"
        )
    else:
        raise AssertionError(f"unknown request verdict {verdict!r}")


# ═══════════════════════════════════════════════════════════════════════
# Then — response variant / per-account / echo (wire assertions)
# ═══════════════════════════════════════════════════════════════════════


@then("the response variant is success")
@then(parsers.parse("the response variant is success and carries an accounts array with {n:d} item"))
def then_variant_success(ctx: dict, n: int | None = None) -> None:
    assert ctx.get("error") is None, f"expected success variant, got error {ctx.get('error')!r}"
    _require_response(ctx)
    accounts = _wire_accounts(ctx)
    assert accounts, "success variant must carry a non-empty accounts array"
    if n is not None:
        assert len(accounts) == n, f"expected {n} account(s), got {len(accounts)}: {accounts}"


@then(parsers.parse("the response accounts array has {n:d} items"))
def then_accounts_count(ctx: dict, n: int) -> None:
    accounts = _wire_accounts(ctx)
    assert len(accounts) == n, f"expected {n} accounts, got {len(accounts)}"


@then("the response variant is error")
def then_variant_error(ctx: dict) -> None:
    result = ctx["result"]
    assert result.is_error, f"expected error variant, got response {ctx.get('response')!r}"
    envelope = result.wire_error_envelope
    assert envelope is not None, "error variant must carry a two-layer wire error envelope"
    # Guard the envelope STRUCTURE (the specific code is pinned by the scenario's following
    # step — `the error code is "X"` / a `then_error_*`): both layers present, their codes
    # non-empty and AGREEING, and a recovery hint set. A single-layer or code-less envelope
    # ("flip the code to garbage and this stays green" no longer holds) now fails here.
    top = (envelope.get("adcp_error") or {}).get("code")
    leaf = (envelope.get("errors") or [{}])[0].get("code")
    assert top and leaf and top == leaf, f"malformed/disagreeing two-layer error codes: {envelope}"
    assert (envelope.get("errors") or [{}])[0].get("recovery"), f"error missing recovery hint: {envelope}"


@then("the response does NOT carry an operation-level errors array")
def then_no_operation_errors(ctx: dict) -> None:
    # Success (partial-failure) variant: per-account results live under accounts[], and
    # there is NO operation-level error envelope (spec oneOf: accounts XOR adcp_error).
    body = wire_dict(ctx)
    # Falsifiable: the ERROR variant carries a top-level adcp_error and NO accounts[], so
    # pinning adcp_error's absence AND accounts' presence grades that this is genuinely the
    # success variant. (The earlier `"errors" not in body` half was vacuous — the response
    # model is extra='forbid', so a top-level errors[] can never appear — #1329.)
    assert body.get("adcp_error") is None, f"expected success variant, got an error envelope: {body}"
    assert body.get("accounts") is not None, f"success variant must carry an accounts array: {body}"


@then(parsers.parse('the account "{account_id}" has status "{status}"'))
@then(parsers.parse('account "{account_id}" has status "{status}" and echoes the governance_agents URL'))
def then_account_status(ctx: dict, account_id: str, status: str) -> None:
    # _wire_account fetches the entry by its echoed ref — the by-id lookup IS the ref-echo
    # grade (it raises "No wire account {id}. Available: ..." if the ref was dropped/wrong),
    # so no separate membership pre-assert (that is redundant against the lookup, #1329).
    acct = _wire_account(ctx, account_id)
    assert acct["status"] == status, f"account {account_id}: expected status {status}, got {acct['status']}"
    if status == "synced":
        agents = acct.get("governance_agents") or []
        assert agents and agents[0].get("url"), f"synced account {account_id} must echo a governance_agents url"
        # Credentials are write-only: a synced echo MUST NOT carry authentication (wire-level).
        assert "authentication" not in agents[0], f"synced echo must not carry credentials: {agents[0]}"


@then(parsers.parse('account "{account_id}" has status "{status}" and carries a per-account errors array'))
def then_account_status_with_errors(ctx: dict, account_id: str, status: str) -> None:
    # _wire_account's by-id lookup IS the ref-echo grade (raises on a dropped/wrong ref);
    # no redundant membership pre-assert (#1329).
    acct = _wire_account(ctx, account_id)
    assert acct["status"] == status, f"account {account_id}: expected status {status}, got {acct['status']}"
    assert acct.get("errors"), f"failed account {account_id} must carry a per-account errors array: {acct}"


@then(parsers.parse('the response account "{account_id}" echoes governance_agents[{idx:d}].url "{url}"'))
def then_echo_url(ctx: dict, account_id: str, idx: int, url: str) -> None:
    # _wire_account's by-id lookup IS the ref-echo grade (raises on a dropped/wrong ref);
    # no redundant membership pre-assert (#1329).
    acct = _wire_account(ctx, account_id)
    agents = acct.get("governance_agents") or []
    actual = agents[idx]["url"]
    assert url_eq(actual, url), f"account {account_id}: expected echoed url {url}, got {actual}"


@then(parsers.parse('the response account "{account_id}" does NOT echo governance_agents[{idx:d}].authentication'))
def then_no_echo_auth(ctx: dict, account_id: str, idx: int) -> None:
    # _wire_account's by-id lookup IS the ref-echo grade (raises on a dropped/wrong ref);
    # no redundant membership pre-assert (#1329).
    acct = _wire_account(ctx, account_id)
    agents = acct.get("governance_agents") or []
    assert "authentication" not in agents[idx], f"credentials must not be echoed: {agents[idx]}"


@then("the response carries an echoed adcp_version envelope")
def then_adcp_version(ctx: dict) -> None:
    body = wire_dict(ctx)
    # POST-S4 (adcp_version echoed on every response) is not implemented on sync-tool
    # responses (systemic — sync_accounts has the same gap). xfail HERE (in-step) rather
    # than at the scenario level, so the sync-happy scenario's other wire graders execute
    # and stay falsifiable; graduates when production echoes the envelope field (#1329).
    if not body.get("adcp_version"):
        pytest.xfail("POST-S4 adcp_version not echoed on sync responses — spec-production gap (#1329)")
    assert body.get("adcp_version"), f"expected an echoed adcp_version envelope field, got keys {list(body)}"


@then("the per-account errors include an ACCOUNT_NOT_FOUND code")
def then_per_account_authority_code(ctx: dict) -> None:
    """Assert a failed per-account entry carries ACCOUNT_NOT_FOUND on the wire.

    Graduates BR-UC-030 ``sync-no-authority`` (feature line 179) from dormant (its
    ``Then`` was undefined → auto-xfail) to executing across a2a/mcp/rest, and makes
    the error-code choice wire-graded. Production emits the SINGLE uniform
    ``ACCOUNT_NOT_FOUND`` code — an existing-but-unowned account is indistinguishable
    from a nonexistent one (the ``*_NOT_FOUND`` uniform-response MUST). ``SCOPE_INSUFFICIENT``
    is deliberately NOT accepted here: ``governance.py`` emits it nowhere and admitting
    it on the wire would let the exact value the fix removed pass (#1329).
    """
    allowed = {"ACCOUNT_NOT_FOUND"}
    failed_errors = [(acct.get("errors") or [{}])[0] for acct in _wire_accounts(ctx) if acct.get("status") == "failed"]
    failed_codes = {e.get("code") for e in failed_errors}
    # Set comparisons (not count checks): a failed per-account entry must exist
    # (non-empty set) AND every failed code must be an allowed authority code — an
    # absent errors[] surfaces as None, which is not a subset of `allowed`.
    assert failed_codes != set(), f"expected a failed per-account entry, got {_wire_accounts(ctx)}"
    assert failed_codes <= allowed, f"per-account authority code(s) {failed_codes} not all in {allowed}"
    # Recovery is wire-graded against the pinned enum (not a literal): flipping
    # governance.py's per-account recovery terminal->transient reddens HERE, not just
    # off-wire unit/integration (#1329).
    expected_recovery = _pinned_error_metadata()["ACCOUNT_NOT_FOUND"]["recovery"]
    recoveries = {e.get("recovery") for e in failed_errors}
    assert recoveries == {expected_recovery}, (
        f"per-account ACCOUNT_NOT_FOUND recovery {recoveries} must equal the pinned enum {expected_recovery!r}"
    )


@then("the per-account error message does not reveal whether the account exists")
def then_per_account_message_uniform(ctx: dict) -> None:
    """Grade the uniform-response MUST on the wire: the failed per-account message MUST
    NOT carry the authorization-specific ``does not have access to account 'X'`` phrasing
    (which would distinguish exists-but-unowned from not-found — a cross-principal
    enumeration oracle). Restoring the leaky message now reddens a WIRE test, not just
    off-wire unit/integration (#1329)."""
    accounts = _wire_accounts(ctx)
    statuses = {a.get("status") for a in accounts}
    assert "failed" in statuses, f"expected a failed per-account entry, got statuses {statuses}"
    for acct in accounts:
        if acct.get("status") != "failed":
            continue
        for err in acct.get("errors") or []:
            message = err.get("message") or ""
            assert "does not have access" not in message, f"per-account message leaks account existence: {message!r}"


@then(parsers.parse('each per-account error should include a "{field}" field guiding remediation'))
def then_per_account_suggestion(ctx: dict, field: str) -> None:
    accounts = _wire_accounts(ctx)
    statuses = {a["status"] for a in accounts}
    assert "failed" in statuses, f"expected a failed per-account entry to carry {field!r}, got statuses {statuses}"
    # Grade the field CONTENT against the pinned enum (the authority) when the enum defines it —
    # presence-only (`e.get(field)`) is a serializer tautology that stays green if production ships
    # any non-empty value, so it would not surface a drift from the canonical ACCOUNT_NOT_FOUND
    # suggestion/recovery. This step is generic over the requested field ("suggestion", "recovery"),
    # so grade each against its own pinned value; a field the enum does not carry falls back to
    # presence (#1329 R9-D5).
    expected = _pinned_error_metadata().get("ACCOUNT_NOT_FOUND", {}).get(field)
    for acct in accounts:
        if acct["status"] != "failed":
            continue
        errs = acct.get("errors") or []
        assert errs, f"failed account {acct.get('account')} must carry a per-account errors array: {acct}"
        values = {e.get(field) for e in errs}
        if expected is not None:
            assert values == {expected}, (
                f"each per-account {field!r} must equal the pinned ACCOUNT_NOT_FOUND {field} {expected!r}, "
                f"got {values} for account {acct.get('account')}"
            )
        else:
            assert all(e.get(field) for e in errs), f"each per-account error must include a non-empty {field!r}: {acct}"


# ═══════════════════════════════════════════════════════════════════════
# Then — persisted binding (replace semantics; reads below the wire)
# ═══════════════════════════════════════════════════════════════════════
#
# The wire echo shows the current sync's result, so proving REPLACE (prior binding gone)
# and per-account SCOPE (an unnamed account untouched) requires reading the persisted row.
# These read it back via the shared session-safe persisted_governance_urls, matching the
# below-wire integration test test_replace_semantics_overwrites_prior_binding (#1329).


@then(parsers.parse('the persisted governance agent on "{account_id}" is "{url}"'))
@then(parsers.parse('the binding on account "{account_id}" remains "{url}" unchanged'))
def then_persisted_binding_is(ctx: dict, account_id: str, url: str) -> None:
    # One body, two phrasings (replace-overwrites vs per-account-scope-unchanged): both
    # assert the persisted binding on the account is EXACTLY [url]. Stacked parsers rather
    # than two identical bodies (#1329; mirrors the stacked @when parsers).
    # An absent/unbound account reads back as [], so the len==1 check also covers persistence.
    urls = persisted_governance_urls(ctx["tenant"].tenant_id, account_id)
    assert len(urls) == 1 and url_eq(urls[0], url), f"expected {account_id} persisted binding == [{url!r}], got {urls}"


@then(parsers.parse('the previous binding to "{url}" is no longer present'))
def then_previous_binding_absent(ctx: dict, url: str) -> None:
    # The replace scenario binds exactly one account; read it back and confirm the old url
    # is gone (replace overwrote, not appended).
    prior = ctx.get("prior_bindings") or {}
    assert prior, "no prior binding recorded by the pre-binding Given"
    account_id = next(iter(prior))
    urls = persisted_governance_urls(ctx["tenant"].tenant_id, account_id)
    assert not any(url_eq(p, url) for p in urls), f"stale binding {url!r} still present on {account_id}: {urls}"


@then(parsers.parse('the account for brand "{brand}" on operator "{operator}" has status "{status}"'))
def then_natural_key_account_status(ctx: dict, brand: str, operator: str, status: str) -> None:
    accounts = _wire_accounts(ctx)
    assert len(accounts) == 1, f"expected exactly one wire account for the natural-key request, got {accounts}"
    acct = accounts[0]
    assert acct["status"] == status, f"expected status {status!r}, got {acct.get('status')!r}: {acct}"
    ref = acct.get("account") or {}
    assert (ref.get("brand") or {}).get("domain") == brand and ref.get("operator") == operator, (
        f"wire must echo the requested natural key (brand={brand}, operator={operator}), got {ref}"
    )


# ═══════════════════════════════════════════════════════════════════════
# Then — validation / boundary wire errors (governance-specific)
# ═══════════════════════════════════════════════════════════════════════


# These route through the harness's guarded, transport-independent error grader
# (result.assert_wire_error) rather than scanning str(envelope): recovery defaults to the
# pinned AdCP enum (not a hardcoded "correctable"). Field-level violations pin the STRUCTURED
# errors[0].field EXACTLY (both layers, via field=) — a substring token like "accounts" or
# "governance_agents" is a prefix of several governance paths and would stay green on a field
# wrong for the scenario (#1329). field is transport-stable (the MCP TypeAdapter
# boundary diverges on message, not field). The url https-requirement is a model validator, so
# its field is empty → assert the message there. Every request-validation rejection also carries
# a top-level suggestion (require_suggestion=True). Verified against the real per-transport
# envelopes (#1329).
_CREDENTIALS_FIELD = "accounts[0].governance_agents[0].authentication.credentials"
_AGENTS_FIELD = "accounts[0].governance_agents"
# The url gates (userinfo/https/SSRF) now raise a field-located error here (was a bare
# ValueError with an empty field), so the step pins field= too (#1329).
_URL_FIELD = "accounts[0].governance_agents[0].url"
# The extra_forbidden gate for the mistyped `credential` (singular) key rejects HERE — the
# leak channel's exact field, transport-stable across a2a/mcp/rest.
_CREDENTIAL_EXTRA_FIELD = "accounts[0].governance_agents[0].authentication.credential"
# Exact field per credential leak channel — pinned so a leaf wrong for the scenario reddens
# (a shared ...governance_agents[0] prefix would stay green on either leaf) (#1329 R9-D4).
_LEAK_CHANNEL_FIELD = {
    "url-userinfo": _URL_FIELD,
    "extra-authentication-key": _CREDENTIAL_EXTRA_FIELD,
}


@then("the error references the url field and indicates https is required")
def then_error_url_https(ctx: dict) -> None:
    ctx["result"].assert_wire_error(
        "VALIDATION_ERROR", field=_URL_FIELD, message_substr="url must use https", require_suggestion=True
    )


@then("the error references the credentials field")
def then_error_credentials(ctx: dict) -> None:
    ctx["result"].assert_wire_error("VALIDATION_ERROR", field=_CREDENTIALS_FIELD, require_suggestion=True)


@then("the response is a VALIDATION_ERROR on the wire naming the governance agent field")
def then_secret_channel_wire_error(ctx: dict) -> None:
    # Wire-graded: a field-located VALIDATION_ERROR on the governance agent. Pin the EXACT
    # field per leak channel (transport-stable across a2a/mcp/rest) rather than a shared
    # ...governance_agents[0] prefix — a substring stays green even if the leaf is wrong for
    # the scenario, so dropping the trailing `url`/`credential` from the reported loc would
    # slip past it. require_suggestion=True matches every other request-validation grade in
    # this file (#1329 R9-D4).
    expected_field = _LEAK_CHANNEL_FIELD[ctx["leak_channel"]]
    ctx["result"].assert_wire_error("VALIDATION_ERROR", field=expected_field, require_suggestion=True)


@then("the wire envelope does NOT contain the leaked secret")
def then_wire_envelope_omits_secret(ctx: dict) -> None:
    # The security invariant: a rejected credential must never be echoed. Assert on the REAL
    # wire envelope (not a reconstruction) so disabling the strip/redaction reddens the wire.
    #
    # MCP + extra_forbidden is the ONE ungraded cell: MCP surfaces only the leaf pydantic
    # message ("Extra inputs are not permitted"), never the input value, so the secret is absent
    # there for a STRUCTURAL reason, not because the redaction ran — a redaction grade on mcp is
    # vacuous. Mutation-confirmed: disabling format_validation_error's redaction reddens
    # a2a-extra + rest-extra but NOT mcp-extra. The url-userinfo channel renders a field-located
    # message on every transport, so all three (incl. mcp) grade it — confirmed by the sibling
    # mutation reddening a2a/mcp/rest. So only this one cell is ungraded.
    if ctx["transport"] == Transport.MCP and ctx.get("leak_channel") == "extra-authentication-key":
        pytest.xfail(
            "MCP emits only the leaf pydantic message for an extra_forbidden field — secret-absence is vacuous"
        )
    secret = ctx["leaked_secret"]
    envelope = ctx["result"].wire_error_envelope
    assert secret not in str(envelope), f"leaked secret reached the wire envelope: {envelope!r}"


@then("the error references the governance_agents maximum cardinality")
def then_error_cardinality_max(ctx: dict) -> None:
    # maxItems 1 and minItems 1 both point at the same field (accounts[0].governance_agents);
    # the distinguishing token lives in the message ("at most" / "at least 1 item") on all
    # three transports (#1329).
    ctx["result"].assert_wire_error(
        "VALIDATION_ERROR", field=_AGENTS_FIELD, message_substr="at most 1 item", require_suggestion=True
    )


@then("the error references the governance_agents minimum cardinality")
def then_error_cardinality_min(ctx: dict) -> None:
    ctx["result"].assert_wire_error(
        "VALIDATION_ERROR", field=_AGENTS_FIELD, message_substr="at least 1 item", require_suggestion=True
    )


@then("the error references the accounts array size")
def then_error_accounts_size(ctx: dict) -> None:
    ctx["result"].assert_wire_error("VALIDATION_ERROR", field="accounts", require_suggestion=True)


@then("the error code indicates the missing idempotency_key")
def then_error_missing_key(ctx: dict) -> None:
    ctx["result"].assert_wire_error("VALIDATION_ERROR", field="idempotency_key", require_suggestion=True)


@then(parsers.parse('the response outcome is "{outcome}"'))
def then_response_outcome(ctx: dict, outcome: str) -> None:
    # idempotency_key boundary: "accepted" == request passed operation-level validation
    # (success variant, even if per-account resolution failed); "rejected" == a
    # request-validation wire error fired.
    if outcome == "accepted":
        assert ctx.get("error") is None, f"expected accepted, got error {ctx.get('error')!r}"
        _require_response(ctx)
    elif outcome == "rejected":
        # A too-short / malformed idempotency_key violates the request schema, so the
        # rejection is a VALIDATION_ERROR on the wire. Grade the code + pinned-enum recovery
        # + a non-empty top-level suggestion via the guarded helper, not just "an envelope exists".
        ctx["result"].assert_wire_error("VALIDATION_ERROR", field="idempotency_key", require_suggestion=True)
    else:
        raise AssertionError(f"unknown outcome {outcome!r}")
