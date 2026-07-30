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
- ``@bva`` abstract-verdict outlines are covered concretely by the ``T-UC-030-sync-*``
  scenarios; their generic verdict-step wiring is a follow-up.

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

from pytest_bdd import given, parsers, then, when

from tests.bdd.steps._outcome_helpers import _require_response, wire_dict
from tests.bdd.steps.generic._dispatch import dispatch_request
from tests.factories import AccountFactory, AgentAccountAccessFactory
from tests.helpers.governance import governance_agent_dict, grant_account_access, url_eq

# A valid, well-formed idempotency_key (pattern ^[A-Za-z0-9_.:-]{16,255}$) and
# Bearer credentials (minLength 32) for scenarios that need a well-formed request
# so the assertion-under-test (auth, account resolution) is what actually fires.
_VALID_KEY = "uuid-v4-bdd-00000000000001"
_DEFAULT_URL = "https://governance.example.com"


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _tenant_principal(ctx: dict) -> tuple[Any, Any]:
    """Return the tenant/principal the shared auth Given set up in ctx."""
    return ctx["tenant"], ctx["principal"]


def _owned_account(ctx: dict, account_id: str) -> Any:
    """Create an account the authenticated agent has authority over (access grant)."""
    tenant, principal = _tenant_principal(ctx)
    account = grant_account_access(tenant, principal, account_id)
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
    return {"account": {"account_id": account_id}, "governance_agents": agents}


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
    tautological against this lookup (#1682 review H).
    """
    for acct in _wire_accounts(ctx):
        ref = acct.get("account") or {}
        if ref.get("account_id") == account_id:
            return acct
    available = [(a.get("account") or {}).get("account_id") for a in _wire_accounts(ctx)]
    raise AssertionError(f"No wire account {account_id!r}. Available: {available}")


def _persisted_binding(ctx: dict, account_id: str) -> dict[str, Any]:
    """Read the persisted account row back through a fresh AccountUoW on the same DB the
    dispatch committed to, returning ``{"account_id", "urls"}`` extracted BEFORE the session
    closes (accessing ORM attributes after close raises DetachedInstanceError — attributes
    expire on commit). Lets the replace/absent Then steps grade what was actually persisted
    (below the wire), not just the response echo. ``account_id`` is None when no row exists.
    """
    from src.core.database.repositories.uow import AccountUoW

    tenant_id = ctx["tenant"].tenant_id
    with AccountUoW(tenant_id) as uow:
        account = uow.accounts.get_by_id(account_id)
        if account is None:
            return {"account_id": None, "urls": []}
        return {"account_id": account.account_id, "urls": [str(a.url) for a in (account.governance_agents or [])]}


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
    natural key, so the row must carry the operator + brand.domain the request references.
    """
    tenant, principal = _tenant_principal(ctx)
    account_id = "acc-nk-" + f"{brand}-{operator}".replace(".", "-")
    account = AccountFactory(
        tenant=tenant, account_id=account_id, operator=operator, brand={"domain": brand}, sandbox=False
    )
    AgentAccountAccessFactory(tenant=tenant, principal=principal, account=account)
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
        accounts=[{"account": ref, "governance_agents": [_agent(url, cred_len=n)]}],
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
    _dispatch(ctx, transport, accounts=[_account_entry(account_id, [_agent(_DEFAULT_URL)])])


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
        accounts=[_account_entry(account_id, [_agent(_DEFAULT_URL)])],
    )


@when(
    parsers.parse(
        'the Buyer Agent sends a sync_governance request via {transport} with idempotency_key "{key}" '
        'and one account "{account_id}"'
    )
)
def when_sync_key_boundary(ctx: dict, transport: str, key: str, account_id: str) -> None:
    # idempotency_key boundary scenarios: vary only the key; keep the rest well-formed.
    _dispatch(ctx, transport, idempotency_key=key, accounts=[_account_entry(account_id, [_agent(_DEFAULT_URL)])])


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
    # Success (partial-failure) variant: per-account errors live under accounts[].errors,
    # never as a top-level operation-level errors[] (spec oneOf: accounts XOR errors).
    body = wire_dict(ctx)
    # Falsifiable form of the oneOf: the ERROR variant WOULD carry adcp_error, so pinning
    # its absence grades that this is genuinely the success variant. `"errors" not in body`
    # alone is vacuous — SyncGovernanceResponse (extra='forbid') has no top-level errors
    # field, so it can never be present (#1682 review H).
    assert body.get("adcp_error") is None, f"expected success variant, got an error envelope: {body}"
    assert "errors" not in body, f"success variant must not carry a top-level errors array: {body}"


@then(parsers.parse('the account "{account_id}" has status "{status}"'))
@then(parsers.parse('account "{account_id}" has status "{status}" and echoes the governance_agents URL'))
def then_account_status(ctx: dict, account_id: str, status: str) -> None:
    # The wire MUST echo the requested account ref — graded explicitly here against the full
    # wire (a dropped/wrong ref fails this), THEN _wire_account fetches the matching entry.
    assert account_id in {a.get("account", {}).get("account_id") for a in _wire_accounts(ctx)}, (
        f"wire must echo the requested account ref {account_id}"
    )
    acct = _wire_account(ctx, account_id)
    assert acct["status"] == status, f"account {account_id}: expected status {status}, got {acct['status']}"
    if status == "synced":
        agents = acct.get("governance_agents") or []
        assert agents and agents[0].get("url"), f"synced account {account_id} must echo a governance_agents url"
        # Credentials are write-only: a synced echo MUST NOT carry authentication (wire-level).
        assert "authentication" not in agents[0], f"synced echo must not carry credentials: {agents[0]}"


@then(parsers.parse('account "{account_id}" has status "{status}" and carries a per-account errors array'))
def then_account_status_with_errors(ctx: dict, account_id: str, status: str) -> None:
    assert account_id in {a.get("account", {}).get("account_id") for a in _wire_accounts(ctx)}, (
        f"wire must echo the requested account ref {account_id}"
    )
    acct = _wire_account(ctx, account_id)
    assert acct["status"] == status, f"account {account_id}: expected status {status}, got {acct['status']}"
    assert acct.get("errors"), f"failed account {account_id} must carry a per-account errors array: {acct}"


@then(parsers.parse('the response account "{account_id}" echoes governance_agents[{idx:d}].url "{url}"'))
def then_echo_url(ctx: dict, account_id: str, idx: int, url: str) -> None:
    assert account_id in {a.get("account", {}).get("account_id") for a in _wire_accounts(ctx)}, (
        f"wire must echo the requested account ref {account_id}"
    )
    acct = _wire_account(ctx, account_id)
    agents = acct.get("governance_agents") or []
    actual = agents[idx]["url"]
    assert url_eq(actual, url), f"account {account_id}: expected echoed url {url}, got {actual}"


@then(parsers.parse('the response account "{account_id}" does NOT echo governance_agents[{idx:d}].authentication'))
def then_no_echo_auth(ctx: dict, account_id: str, idx: int) -> None:
    assert account_id in {a.get("account", {}).get("account_id") for a in _wire_accounts(ctx)}, (
        f"wire must echo the requested account ref {account_id}"
    )
    acct = _wire_account(ctx, account_id)
    agents = acct.get("governance_agents") or []
    assert "authentication" not in agents[idx], f"credentials must not be echoed: {agents[idx]}"


@then("the response carries an echoed adcp_version envelope")
def then_adcp_version(ctx: dict) -> None:
    body = wire_dict(ctx)
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
    it on the wire would let the exact value the fix removed pass (#1682 review C).
    """
    allowed = {"ACCOUNT_NOT_FOUND"}
    failed_codes = {
        ((acct.get("errors") or [{}])[0]).get("code") for acct in _wire_accounts(ctx) if acct.get("status") == "failed"
    }
    # Set comparisons (not count checks): a failed per-account entry must exist
    # (non-empty set) AND every failed code must be an allowed authority code — an
    # absent errors[] surfaces as None, which is not a subset of `allowed`.
    assert failed_codes != set(), f"expected a failed per-account entry, got {_wire_accounts(ctx)}"
    assert failed_codes <= allowed, f"per-account authority code(s) {failed_codes} not all in {allowed}"


@then("the per-account error message does not reveal whether the account exists")
def then_per_account_message_uniform(ctx: dict) -> None:
    """Grade the uniform-response MUST on the wire: the failed per-account message MUST
    NOT carry the authorization-specific ``does not have access to account 'X'`` phrasing
    (which would distinguish exists-but-unowned from not-found — a cross-principal
    enumeration oracle). Restoring the leaky message now reddens a WIRE test, not just
    off-wire unit/integration (#1682 review C)."""
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
    for acct in accounts:
        if acct["status"] != "failed":
            continue
        errs = acct.get("errors") or []
        assert errs, f"failed account {acct.get('account')} must carry a per-account errors array: {acct}"
        assert all(e.get(field) for e in errs), f"each per-account error must include a non-empty {field!r}: {acct}"


# ═══════════════════════════════════════════════════════════════════════
# Then — persisted binding (replace semantics; reads below the wire)
# ═══════════════════════════════════════════════════════════════════════
#
# The wire echo shows the current sync's result, so proving REPLACE (prior binding gone)
# and per-account SCOPE (an unnamed account untouched) requires reading the persisted row.
# These read it back via _persisted_binding, matching the below-wire integration test
# test_replace_semantics_overwrites_prior_binding (#1682 review item 3).


@then(parsers.parse('the persisted governance agent on "{account_id}" is "{url}"'))
@then(parsers.parse('the binding on account "{account_id}" remains "{url}" unchanged'))
def then_persisted_binding_is(ctx: dict, account_id: str, url: str) -> None:
    # One body, two phrasings (replace-overwrites vs per-account-scope-unchanged): both
    # assert the persisted binding on the account is EXACTLY [url]. Stacked parsers rather
    # than two identical bodies (#1682 review H; mirrors the stacked @when parsers).
    persisted = _persisted_binding(ctx, account_id)
    assert persisted["account_id"] == account_id, f"account {account_id!r} not persisted"
    urls = persisted["urls"]
    assert len(urls) == 1 and url_eq(urls[0], url), f"expected {account_id} persisted binding == [{url!r}], got {urls}"


@then(parsers.parse('the previous binding to "{url}" is no longer present'))
def then_previous_binding_absent(ctx: dict, url: str) -> None:
    # The replace scenario binds exactly one account; read it back and confirm the old url
    # is gone (replace overwrote, not appended).
    prior = ctx.get("prior_bindings") or {}
    assert prior, "no prior binding recorded by the pre-binding Given"
    account_id = next(iter(prior))
    urls = _persisted_binding(ctx, account_id)["urls"]
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
# wrong for the scenario (#1682 review D). field is transport-stable (the MCP TypeAdapter
# boundary diverges on message, not field). The url https-requirement is a model validator, so
# its field is empty → assert the message there. Every request-validation rejection also carries
# a top-level suggestion (require_suggestion=True). Verified against the real per-transport
# envelopes (#1682 review item 2/D).
_CREDENTIALS_FIELD = "accounts[0].governance_agents[0].authentication.credentials"
_AGENTS_FIELD = "accounts[0].governance_agents"


@then("the error references the url field and indicates https is required")
def then_error_url_https(ctx: dict) -> None:
    ctx["result"].assert_wire_error("VALIDATION_ERROR", message_substr="url must use https", require_suggestion=True)


@then("the error references the credentials field")
def then_error_credentials(ctx: dict) -> None:
    ctx["result"].assert_wire_error("VALIDATION_ERROR", field=_CREDENTIALS_FIELD, require_suggestion=True)


@then("the error references the governance_agents maximum cardinality")
def then_error_cardinality_max(ctx: dict) -> None:
    # maxItems 1 and minItems 1 both point at the same field (accounts[0].governance_agents);
    # the distinguishing token lives in the message ("at most" / "at least 1 item") on all
    # three transports (#1682 review D).
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
