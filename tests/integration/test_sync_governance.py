"""Integration tests for _sync_governance (UC-030, #1329) with real PostgreSQL.

Verifies the seller-side governance-binding contract end-to-end against a real
DB: authority check (the normative MUST) -> url-only persistence (replace
semantics) on the accounts.governance_agents column -> synced/failed results,
plus a REST wire-path roundtrip.

Idempotency replay / IDEMPOTENCY_CONFLICT and the full UC-030 boundary matrix
are the richer BDD ledger (deferred follow-up); these tests pin the working
tool the capabilities honesty pass depends on.
"""

from __future__ import annotations

import pytest

from src.core.database.repositories.account import AccountRepository
from src.core.database.repositories.uow import AccountUoW
from src.core.schemas.account import SyncGovernanceRequest
from tests.factories import AccountFactory, TenantFactory
from tests.harness.governance_sync import GovernanceSyncEnv
from tests.harness.transport import Transport, _pinned_error_metadata
from tests.helpers.accounts import seed_account_with_access
from tests.helpers.governance import (
    BEARER_CREDS,
    GOV_URL,
    account_entry,
    governance_agent_dict,
    persisted_governance_urls,
    url_eq,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

GOV_URL_2 = "https://governance.new-buyer.com"

# Expected recovery on the uniform ACCOUNT_NOT_FOUND per-account error, derived from
# the pinned spec enum (the authority), not a copied literal (#1682 review C).
_ACCOUNT_NOT_FOUND_RECOVERY = _pinned_error_metadata()["ACCOUNT_NOT_FOUND"]["recovery"]


def _request(
    account_ref: dict, url: str = GOV_URL, key: str = "uuid-v4-int-000000000000000001"
) -> SyncGovernanceRequest:
    return SyncGovernanceRequest(
        idempotency_key=key,
        accounts=[account_entry(account_ref, agents=[governance_agent_dict(url)])],
    )


def _persisted_agents_raw(tenant_id: str, account_id: str) -> list | None:
    """Read the RAW stored governance_agents JSON via the repository, bypassing JSONType coercion.

    Reading through the typed ORM attribute re-validates each element against the url-only
    column model, which would RAISE on a credential-bearing row — masking the exact leak
    the strip test grades (the assertion would never execute). The repo's raw reader casts
    to plain JSONB so a persisted credential FAILS the key-set assertion instead of erroring
    on read (#1682 review B).
    """
    with AccountUoW(tenant_id) as uow:
        repo: AccountRepository = uow.accounts
        return repo.get_stored_governance_agents(account_id)


class TestSyncGovernancePersistence:
    """Authority-gated persistence: synced accounts store the binding url-only."""

    @pytest.mark.asyncio
    async def test_owned_account_synced_and_persisted_url_only(self, integration_db):
        with GovernanceSyncEnv(tenant_id="gov_t1", principal_id="gov_agent1") as env:
            tenant, principal = env.setup_default_data()
            seed_account_with_access(tenant, principal, account_id="acc_gov_1")

            resp = await env.call_impl_async(req=_request({"account_id": "acc_gov_1"}))

        assert resp.accounts[0].status == "synced"
        assert resp.accounts[0].governance_agents[0].url == GOV_URL + "/"
        # Persisted url-only (credentials are never stored — column model is url-only).
        persisted = persisted_governance_urls("gov_t1", "acc_gov_1")
        assert len(persisted) == 1
        assert persisted[0] == GOV_URL + "/"
        # Assert the RAW STORED JSON has exactly one key, `url`. Reading through the ORM
        # (above) re-coerces to the url-only column model, so a leaked credential would
        # RAISE on read rather than fail this assertion — the raw JSONB read makes the
        # strip assertion actually execute against what is on disk (#1682 review B).
        raw = _persisted_agents_raw("gov_t1", "acc_gov_1")
        assert raw == [{"url": GOV_URL + "/"}], f"raw stored governance agent must be url-only, got {raw}"

    @pytest.mark.asyncio
    async def test_replace_semantics_overwrites_prior_binding(self, integration_db):
        with GovernanceSyncEnv(tenant_id="gov_t2", principal_id="gov_agent2") as env:
            tenant, principal = env.setup_default_data()
            seed_account_with_access(tenant, principal, account_id="acc_gov_2")

            await env.call_impl_async(req=_request({"account_id": "acc_gov_2"}, url=GOV_URL))
            # Second sync with a different agent replaces the first.
            resp = await env.call_impl_async(
                req=_request({"account_id": "acc_gov_2"}, url=GOV_URL_2, key="uuid-v4-int-000000000000000002")
            )

        assert resp.accounts[0].status == "synced"
        persisted = persisted_governance_urls("gov_t2", "acc_gov_2")
        assert len(persisted) == 1
        assert persisted[0] == GOV_URL_2 + "/"


class TestSyncGovernanceAuthority:
    """The normative MUST: unknown/unowned accounts fail per-account, no persistence."""

    @pytest.mark.asyncio
    async def test_unknown_account_fails_account_not_found(self, integration_db):
        with GovernanceSyncEnv(tenant_id="gov_t3", principal_id="gov_agent3") as env:
            env.setup_default_data()

            resp = await env.call_impl_async(req=_request({"account_id": "acc_does_not_exist"}))

        assert resp.accounts[0].status == "failed"
        assert resp.accounts[0].errors[0].code == "ACCOUNT_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_existing_but_unowned_account_fails_account_not_found(self, integration_db):
        with GovernanceSyncEnv(tenant_id="gov_t4", principal_id="gov_agent4") as env:
            tenant, _principal = env.setup_default_data()
            # Account exists in the tenant but the agent has NO AgentAccountAccess grant.
            AccountFactory(tenant=tenant, account_id="acc_unowned")

            resp = await env.call_impl_async(req=_request({"account_id": "acc_unowned"}))

        assert resp.accounts[0].status == "failed"
        # Existing-but-unowned is collapsed to the SAME ACCOUNT_NOT_FOUND result as a
        # nonexistent account (uniform response → no cross-principal enumeration
        # oracle). NOT SCOPE_INSUFFICIENT (a task-scope code this seller does not
        # model). #1682 review A1.
        err = resp.accounts[0].errors[0]
        assert err.code == "ACCOUNT_NOT_FOUND"
        assert err.recovery == _ACCOUNT_NOT_FOUND_RECOVERY
        # Uniform generic message — must NOT reveal the account exists via the
        # authorization-specific "does not have access to account 'X'" phrasing.
        assert "does not have access" not in err.message
        # No binding persisted on a failed account.
        assert persisted_governance_urls("gov_t4", "acc_unowned") == []

    @pytest.mark.asyncio
    async def test_cross_tenant_account_fails_account_not_found(self, integration_db):
        # Account lives in tenant B; the agent authenticates in tenant A. The sync
        # is scoped to the agent's tenant (AccountUoW(tenant_id) → tenant-filtered
        # repo), so the account is unresolvable there → ACCOUNT_NOT_FOUND, with no
        # persistence and no cross-tenant existence leak (#1682 review NIT).
        with GovernanceSyncEnv(tenant_id="gov_ta", principal_id="gov_agent_a") as env:
            env.setup_default_data()
            tenant_b = TenantFactory(tenant_id="gov_tb")
            AccountFactory(tenant=tenant_b, account_id="acc_in_b")

            resp = await env.call_impl_async(req=_request({"account_id": "acc_in_b"}))

        assert resp.accounts[0].status == "failed"
        assert resp.accounts[0].errors[0].code == "ACCOUNT_NOT_FOUND"
        # The account was NOT touched in tenant B.
        assert persisted_governance_urls("gov_tb", "acc_in_b") == []

    @pytest.mark.asyncio
    async def test_natural_key_ambiguous_fails_account_ambiguous(self, integration_db):
        """Covers the ACCOUNT_AMBIGUOUS branch: a natural key matching several of the
        caller's OWN accounts (scoped to accessible — no oracle) fails per-account
        correctable, not synced. Last round's ambiguous fix was 0%-covered (#1682 review C).
        """
        with GovernanceSyncEnv(tenant_id="gov_amb", principal_id="gov_agent_amb") as env:
            tenant, principal = env.setup_default_data()
            # Two OWNED accounts sharing one natural key (operator + brand.domain + sandbox).
            for aid in ("acc_amb_1", "acc_amb_2"):
                seed_account_with_access(
                    tenant, principal, account_id=aid, operator="pinnacle.com", brand_domain="spark", sandbox=False
                )
            ref = {"brand": {"domain": "spark"}, "operator": "pinnacle.com", "sandbox": False}
            resp = await env.call_impl_async(
                req=SyncGovernanceRequest(
                    idempotency_key="uuid-v4-int-000000000000000amb",
                    accounts=[account_entry(ref, agents=[governance_agent_dict(GOV_URL)])],
                )
            )

        assert resp.accounts[0].status == "failed"
        err = resp.accounts[0].errors[0]
        assert err.code == "ACCOUNT_AMBIGUOUS"
        assert err.recovery == _pinned_error_metadata()["ACCOUNT_AMBIGUOUS"]["recovery"]
        # No binding persisted on a failed account.
        assert persisted_governance_urls("gov_amb", "acc_amb_1") == []

    @pytest.mark.asyncio
    async def test_status_blocked_account_fails_with_status_code(self, integration_db):
        """Covers the ``except AdCPError`` fallthrough: an OWNED but status-blocked account
        surfaces the resolver's own (canonical) code + recovery — an honest per-account
        failure, not a silent success. Last round's fallthrough was 0%-covered (#1682 review C).
        """
        with GovernanceSyncEnv(tenant_id="gov_susp", principal_id="gov_agent_susp") as env:
            tenant, principal = env.setup_default_data()
            seed_account_with_access(tenant, principal, account_id="acc_susp", status="suspended")

            resp = await env.call_impl_async(req=_request({"account_id": "acc_susp"}))

        assert resp.accounts[0].status == "failed"
        err = resp.accounts[0].errors[0]
        # ACCOUNT_SUSPENDED is a canonical pinned code; recovery agrees by construction.
        assert err.code == "ACCOUNT_SUSPENDED"
        assert err.recovery == _pinned_error_metadata()["ACCOUNT_SUSPENDED"]["recovery"]
        assert persisted_governance_urls("gov_susp", "acc_susp") == []


class TestSyncGovernanceCrossTransportWire:
    """Happy-path synced/url-echo/no-credentials shape on the real MCP + A2A wire.

    Governance's only non-BDD wire test was REST happy-path; the synced shape on
    A2A/MCP otherwise rode entirely on the BDD sync-partial scenario. This mirrors
    the capabilities cross-transport wire test (#1682 review A5).
    """

    @pytest.mark.parametrize("transport", [Transport.MCP, Transport.A2A])
    def test_happy_path_synced_on_mcp_and_a2a_wire(self, transport, integration_db):
        tid = f"gov_wire_{transport.value}"
        with GovernanceSyncEnv(tenant_id=tid, principal_id=f"{tid}_agent") as env:
            tenant, principal = env.setup_default_data()
            seed_account_with_access(tenant, principal, account_id="acc_wire")

            result = env.call_via(
                transport,
                idempotency_key="uuid-v4-wire-0000000000000001",
                accounts=[account_entry({"account_id": "acc_wire"}, agents=[governance_agent_dict(GOV_URL)])],
            )

        assert result.is_success, f"{transport}: expected success, got {result.error!r}"
        assert result.wire_response is not None, f"{transport}: env did not stash success-path wire"
        accounts = result.wire_response.get("accounts") or []
        assert len(accounts) == 1, f"{transport}: expected 1 account on the wire, got {accounts}"
        acct = accounts[0]
        assert acct["status"] == "synced"
        agents = acct.get("governance_agents") or []
        assert agents and url_eq(agents[0].get("url"), GOV_URL), f"{transport}: url not echoed: {agents}"
        # Credentials are write-only — the wire echo MUST NOT carry authentication.
        assert "authentication" not in agents[0], f"{transport}: credentials echoed on wire: {agents[0]}"
        assert BEARER_CREDS not in str(result.wire_response), f"{transport}: credentials leaked on wire"

    @pytest.mark.parametrize("transport", [Transport.MCP, Transport.A2A])
    def test_context_echoed_on_wire(self, transport, integration_db):
        """The application ``context`` is echoed unchanged on the wire (what the specialism
        storyboards grade). Previously exercised by zero tests — no test sent a context
        (#1682 review C). ContextObject allows extra fields, so a conversation id round-trips.
        """
        tid = f"gov_ctx_{transport.value}"
        with GovernanceSyncEnv(tenant_id=tid, principal_id=f"{tid}_agent") as env:
            tenant, principal = env.setup_default_data()
            seed_account_with_access(tenant, principal, account_id="acc_ctx")

            result = env.call_via(
                transport,
                idempotency_key="uuid-v4-ctx-00000000000000001",
                context={"conversation_id": "conv-gov-xyz"},
                accounts=[account_entry({"account_id": "acc_ctx"}, agents=[governance_agent_dict(GOV_URL)])],
            )

        assert result.is_success, f"{transport}: expected success, got {result.error!r}"
        echoed = (result.wire_response or {}).get("context") or {}
        assert echoed.get("conversation_id") == "conv-gov-xyz", f"{transport}: context not echoed: {echoed}"


# A 32+ char secret carried on a mistyped, credential-bearing extra field. It must NEVER
# reach the wire — format_validation_error redacts the extra_forbidden echo under a
# credential-bearing loc (#1329). Distinct from BEARER_CREDS so the grade pins the extra
# field's value specifically.
_REDACTION_SECRET = "leaked-bearer-" + "z" * 40


class TestSyncGovernanceCredentialRedactionWire:
    """A credential-bearing ``extra_forbidden`` rejection redacts the secret on the REAL wire.

    The #1329 redaction (``format_validation_error`` withholds the ``Received value:`` echo under
    a credential-bearing extra field) feeds ``errors[0].message``, which reaches the REST and A2A
    buyer wire — but was otherwise graded only by in-process unit tests. This dispatches a
    governance agent with a mistyped ``authentication.credential`` extra field carrying a 32+ char
    secret and asserts the VALIDATION_ERROR envelope does NOT echo the secret — the error-path
    mirror of the success-echo credential grade at ``test_happy_path_synced_on_mcp_and_a2a_wire``
    (#1682 review item 1).

    Parametrized over A2A + REST only: those carry ``format_validation_error``'s full message on
    the wire, so disabling the redaction reddens them (verified by mutation). MCP emits only the
    leaf Pydantic message ("Extra inputs are not permitted"), which never echoes the input value,
    so a redaction grade there would pass vacuously (the reviewer named REST + A2A specifically).
    """

    @pytest.mark.parametrize("transport", [Transport.A2A, Transport.REST])
    def test_credential_bearing_extra_field_redacted_on_wire(self, transport, integration_db):
        tid = f"gov_redact_{transport.value}"
        with GovernanceSyncEnv(tenant_id=tid, principal_id=f"{tid}_agent") as env:
            tenant, principal = env.setup_default_data()
            seed_account_with_access(tenant, principal, account_id="acc_redact")

            # `credential` (singular) is not in the Authentication schema -> extra_forbidden; it
            # carries a 32+ char secret, and the credential-bearing loc makes the boundary redact
            # the echoed value so it never reaches the wire (#1329).
            leaky_agent = {
                "url": GOV_URL,
                "authentication": {
                    "schemes": ["Bearer"],
                    "credentials": BEARER_CREDS,
                    "credential": _REDACTION_SECRET,
                },
            }
            result = env.call_via(
                transport,
                idempotency_key="uuid-v4-redact-0000000000001",
                accounts=[account_entry({"account_id": "acc_redact"}, agents=[leaky_agent])],
            )

        assert result.is_error, f"{transport}: expected a validation rejection, got {result.payload!r}"
        result.assert_wire_error("VALIDATION_ERROR")
        envelope = result.wire_error_envelope
        assert _REDACTION_SECRET not in str(envelope), f"{transport}: redacted secret leaked on the wire: {envelope}"


class TestSyncGovernanceRestWire:
    """REST wire-path roundtrip: the tool works across the transport boundary."""

    def test_rest_happy_path_synced(self, integration_db):
        with GovernanceSyncEnv(tenant_id="gov_t5", principal_id="gov_agent5") as env:
            tenant, principal = env.setup_default_data()
            seed_account_with_access(tenant, principal, account_id="acc_gov_5")

            resp = env.call_rest(req=_request({"account_id": "acc_gov_5"}))

        assert resp.accounts[0].status == "synced"
        assert resp.accounts[0].governance_agents[0].url == GOV_URL + "/"
