"""Wire assertion for the get_adcp_capabilities declared-honesty envelope (#1329).

``assert_declared_capabilities`` is the SINGLE grader for the fields
``_build_capabilities_response`` declares as honesty signals — the account section,
``adcp.idempotency``, and ``specialisms``. Every wire consumer (the BDD
``then_capabilities_*`` steps across a2a/mcp/rest/e2e_rest and the integration wire
test) routes through it, so "the builder emits field X" is COUPLED to "a wire grader
reads field X": the helper fails on an emitted-but-unasserted account/idempotency
field. Before this, a field could be added to the builder while its designated grader
stayed dark — which is exactly how ``adcp.idempotency`` was withdrawn from
``supported=true`` to ``supported=false`` on every transport with nothing on the wire
noticing (#1329 finding 1).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# Every field ``_build_account_capability`` emits on the wire. The completeness check
# below fails if the builder emits a field NOT in this set, forcing a wire assertion to
# be added here rather than the field shipping ungraded.
_ASSERTED_ACCOUNT_FIELDS = frozenset(
    {"sandbox", "require_operator_auth", "required_for_products", "account_financials", "supported_billing"}
)
# Every field ``_adcp_metadata`` emits under ``adcp.idempotency`` (the honest
# supported=False variant carries ONLY ``supported`` — no ``replay_ttl_seconds``).
_ASSERTED_IDEMPOTENCY_FIELDS = frozenset({"supported"})

# The seller's honest default account-billable set (default tenant, no supported_billing
# configured) — the exact parties sync_accounts accepts (resolve_supported_billing).
_DEFAULT_BILLING = frozenset({"operator", "agent"})


def assert_declared_capabilities(body: dict[str, Any], *, expected_billing: Iterable[str] = _DEFAULT_BILLING) -> None:
    """Assert the honesty-declared capability fields on a serialized wire body.

    ``body`` is the serialized get_adcp_capabilities response (dict). Asserts:

    * ``account.sandbox is False`` and ``require_operator_auth is False`` and
      ``required_for_products is False`` and ``account_financials is False`` (all honest
      until the corresponding behavior ships — #1329 gap 13);
    * ``account.supported_billing`` equals ``expected_billing`` (exact set);
    * ``adcp.idempotency.supported is False`` with NO ``replay_ttl_seconds`` (the honest
      Idempotency3 variant — #1329 finding 1/R9-F2);
    * ``specialisms`` is a non-empty, unique array of kebab-case ids;
    * COMPLETENESS: the emitted ``account`` / ``adcp.idempotency`` objects carry no field
      this helper does not assert — a new declared field must be graded here.
    """
    expected = set(expected_billing)

    account = body.get("account")
    assert account is not None, f"capabilities response must include an account section: {body}"
    unasserted = set(account) - _ASSERTED_ACCOUNT_FIELDS
    assert not unasserted, (
        f"account emits field(s) {sorted(unasserted)} that assert_declared_capabilities does not grade — "
        "add a wire assertion here so the declaration is not shipped ungraded (#1329)"
    )
    assert account.get("sandbox") is False, f"account.sandbox must be an honest False, got {account.get('sandbox')!r}"
    assert account.get("require_operator_auth") is False, (
        f"account.require_operator_auth must be an honest False, got {account.get('require_operator_auth')!r}"
    )
    assert account.get("required_for_products") is False, (
        f"account.required_for_products must be an honest False, got {account.get('required_for_products')!r}"
    )
    assert account.get("account_financials") is False, (
        f"account.account_financials must be an honest False, got {account.get('account_financials')!r}"
    )
    billing = account.get("supported_billing")
    assert isinstance(billing, list), f"account.supported_billing must be a list on the wire, got {billing!r}"
    assert set(billing) == expected, f"account.supported_billing must be {sorted(expected)}, got {billing!r}"

    adcp = body.get("adcp") or {}
    idempotency = adcp.get("idempotency")
    assert idempotency is not None, f"adcp.idempotency must be declared (v3.1 required): {body}"
    unasserted_idem = set(idempotency) - _ASSERTED_IDEMPOTENCY_FIELDS
    assert not unasserted_idem, (
        f"adcp.idempotency emits field(s) {sorted(unasserted_idem)} that assert_declared_capabilities does not "
        "grade — add a wire assertion here (#1329)"
    )
    assert idempotency.get("supported") is False, (
        f"adcp.idempotency.supported must be an honest False (only create_media_buy dedups), "
        f"got {idempotency.get('supported')!r}"
    )
    assert "replay_ttl_seconds" not in idempotency, (
        f"adcp.idempotency must not carry replay_ttl_seconds when supported=False: {idempotency}"
    )

    specialisms = body.get("specialisms")
    assert isinstance(specialisms, list) and specialisms, f"specialisms must be a non-empty array: {body}"
    assert len(specialisms) == len(set(specialisms)), f"specialisms must be unique, got {specialisms}"
