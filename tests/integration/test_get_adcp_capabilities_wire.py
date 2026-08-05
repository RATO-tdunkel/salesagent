"""Cross-transport WIRE coverage for the get_adcp_capabilities `account` capability.

The `account`/`sandbox` honesty declaration (#1329: sandbox=False until behavioral
isolation ships; supported_billing = the seller's accepted billing parties) is
otherwise asserted only by `model_dump()` unit tests. These tests assert it on the
ACTUAL serialized wire across MCP, A2A, and REST — the shape a buyer receives —
so a serialization regression (e.g. an omitted/renamed account section, or a
dishonest `sandbox=true`) is caught at the transport boundary.

Covers the BR-UC-010 obligation "the response should include account section with
sandbox flag and billing models" at the wire level.
"""

from __future__ import annotations

import pytest

from tests.harness.capabilities import CapabilitiesEnv
from tests.harness.transport import Transport

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

# The default tenant (setup_default_data) configures no ``supported_billing``, so the
# honest declaration is the seller's permitted set — the exact billing parties
# sync_accounts also accepts (see ``resolve_supported_billing``). Pinning the exact set
# (not just non-empty) makes a dishonest over/under-declaration a wire regression, and
# matches the BR-UC-010 BDD sibling.
_DEFAULT_BILLING = {"operator", "agent"}


def _assert_account_section(account: dict) -> None:
    """Assert the honest account capability shape on a serialized wire body."""
    assert account is not None, "capabilities response must include an account section"
    # Honesty declaration: sandbox is False until behavioral isolation ships (#1329).
    assert account.get("sandbox") is False, f"account.sandbox must be an honest False, got {account.get('sandbox')!r}"
    # require_operator_auth is honestly False (accounts are buyer-declared via sync_accounts,
    # operators do not authenticate) — grade it on the wire too, else the honesty declaration
    # is ungraded and a dishonest True would slip through (#1682 review NIT).
    assert account.get("require_operator_auth") is False, (
        f"account.require_operator_auth must be an honest False, got {account.get('require_operator_auth')!r}"
    )
    # Billing models the seller accepts — the exact permitted set for the default tenant.
    billing = account.get("supported_billing")
    assert isinstance(billing, list), f"account.supported_billing must be a list, got {billing!r}"
    assert set(billing) == _DEFAULT_BILLING, f"account.supported_billing must be {_DEFAULT_BILLING}, got {billing!r}"


class TestGetAdcpCapabilitiesAccountWire:
    """The account/sandbox capability is honestly declared on the real wire."""

    @pytest.mark.parametrize("transport", [Transport.MCP, Transport.A2A, Transport.REST])
    def test_account_section_on_wire(self, transport, integration_db):
        with CapabilitiesEnv() as env:
            env.setup_default_data()
            result = env.call_via(transport)

        assert result.is_success, f"{transport}: expected success, got {result.error!r}"
        assert result.wire_response is not None, f"{transport}: env did not stash success-path wire"
        _assert_account_section(result.wire_response.get("account"))
