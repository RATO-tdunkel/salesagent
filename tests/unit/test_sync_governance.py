"""Unit tests for the sync_governance tool (UC-030, #1329).

Covers the seller-side governance-binding contract per AdCP 3.1.1
(account/sync-governance-request.json + sync-governance-response.json +
accounts/tasks/sync_governance.mdx):

- Success variant: envelope status=completed, per-account status=synced,
  governance_agents[].url echoed, credentials NEVER echoed.
- Persistence: routed through the repository's single governance-write path
  (``set_governance_binding``), which owns url-only projection + replace semantics.
- Authority MUST: an unknown account AND an existing-but-unowned account BOTH ->
  failed ACCOUNT_NOT_FOUND (terminal) — indistinguishable per the *_NOT_FOUND
  uniform-response MUST (no cross-principal enumeration oracle). Partial failure
  stays the success variant.
- Auth required (operation-level) and empty-accounts validation.

These are _impl-level tests, so they assert on the typed response (per
tests/CLAUDE.md, wire-envelope assertions are for error-path transport tests;
the success/persistence contract is verified against the typed payload here). The
url-only credential strip itself is a repository guarantee, verified end-to-end
against a real DB in tests/integration/test_sync_governance.py (#1329).
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from src.core.exceptions import (
    AdCPAccountNotFoundError,
    AdCPAuthenticationError,
    AdCPAuthorizationError,
)
from src.core.schemas.account import SyncGovernanceRequest, SyncGovernanceResponse
from tests.harness.transport import _pinned_error_metadata
from tests.helpers.governance import (
    BEARER_CREDS,
    GOV_URL,
    account_entry,
    governance_agent_dict,
    governance_binding_stub,
)

# Recovery expected on the uniform ACCOUNT_NOT_FOUND per-account error, DERIVED from
# the pinned spec enum (the authority) — not the literal "terminal" and not the
# production constant (that would be vacuous). If production drifts from the pinned
# recovery, these tests catch it (#1329).
_ACCOUNT_NOT_FOUND_RECOVERY = _pinned_error_metadata()["ACCOUNT_NOT_FOUND"]["recovery"]


def _make_identity(principal_id: str | None = "principal-1", tenant_id: str = "tenant-1"):
    from tests.factories import PrincipalFactory

    tenant = {"tenant_id": tenant_id, "name": "Test Publisher", "subdomain": "testpub"}
    return PrincipalFactory.make_identity(
        principal_id=principal_id,
        tenant_id=tenant_id,
        tenant=tenant,
        protocol="mcp",
    )


def _first_schema_error(build) -> dict:
    """Run a request-schema construction expected to fail; return its first error dict.

    Asserting on ``ValidationError.errors()[0]`` (loc + type) pins the SPECIFIC rule
    each test names — a bare ``pytest.raises(ValueError)`` stays green when an
    unrelated field is what actually broke (#1329).
    """
    with pytest.raises(ValidationError) as exc_info:
        build()
    return exc_info.value.errors()[0]


def _make_request(
    *,
    account_ref: dict | None = None,
    url: str = GOV_URL,
    idempotency_key: str = "uuid-v4-unit-00000000000000001",
    accounts: list[dict] | None = None,
) -> SyncGovernanceRequest:
    if accounts is None:
        accounts = [account_entry(account_ref or {"account_id": "acc_1"}, agents=[governance_agent_dict(url)])]
    return SyncGovernanceRequest(idempotency_key=idempotency_key, accounts=accounts)


def _patch_deps(*, resolve_side_effect=None, repo: MagicMock | None = None) -> tuple[ExitStack, MagicMock]:
    """Patch AccountUoW, resolve_account, and the audit logger.

    Returns (stack, repo_mock) — enter the stack in a `with` block. The repo's
    ``set_governance_binding`` mirrors production: it projects agents to url-only and
    returns the stored list (which the tool echoes). The strip itself is the repository's
    guarantee (integration-tested); the mock reproduces it so the tool's echo is exercised.
    """
    stack = ExitStack()
    repo = repo or MagicMock()
    repo.set_governance_binding.side_effect = governance_binding_stub()

    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)
    mock_uow.accounts = repo
    stack.enter_context(patch("src.core.tools.governance.AccountUoW", return_value=mock_uow))
    stack.enter_context(patch("src.core.tools.governance.get_audit_logger"))
    stack.enter_context(patch("src.core.tools.governance.resolve_account", side_effect=resolve_side_effect))
    return stack, repo


class TestSyncGovernanceSuccess:
    """Happy-path contract: synced, persisted via the repo write path, echoed without credentials."""

    @pytest.mark.asyncio
    async def test_synced_status_and_completed_envelope(self):
        from src.core.tools.governance import _sync_governance_impl

        stack, repo = _patch_deps(resolve_side_effect=lambda ref, ident, r: "acc_1")
        with stack:
            resp = await _sync_governance_impl(_make_request(), _make_identity())

        assert isinstance(resp, SyncGovernanceResponse)
        assert len(resp.accounts) == 1
        assert resp.accounts[0].status == "synced"
        # Envelope status is the synchronous-success protocol status.
        assert resp.model_dump(mode="json")["status"] == "completed"

    @pytest.mark.asyncio
    async def test_persists_binding_via_repository_write_path(self):
        from src.core.tools.governance import _sync_governance_impl

        req = _make_request(url=GOV_URL)
        stack, repo = _patch_deps(resolve_side_effect=lambda ref, ident, r: "acc_1")
        with stack:
            await _sync_governance_impl(req, _make_identity())

        # The tool persists through the repository's single governance-write path, which
        # owns the url-only credential strip + replace semantics (#1329; strip
        # verified end-to-end in tests/integration/test_sync_governance.py). It must pass
        # the raw request agents (repo projects) and must NOT reach for generic update_fields.
        repo.set_governance_binding.assert_called_once_with("acc_1", req.accounts[0].governance_agents)
        repo.update_fields.assert_not_called()

    @pytest.mark.asyncio
    async def test_credentials_never_echoed(self):
        from src.core.tools.governance import _sync_governance_impl

        stack, _repo = _patch_deps(resolve_side_effect=lambda ref, ident, r: "acc_1")
        with stack:
            resp = await _sync_governance_impl(_make_request(), _make_identity())

        dumped = resp.model_dump(mode="json")
        serialized = str(dumped)
        assert "authentication" not in serialized
        assert BEARER_CREDS not in serialized
        assert "credentials" not in serialized
        # But the URL IS echoed (from the repo's stored url-only list).
        assert dumped["accounts"][0]["governance_agents"][0]["url"] == GOV_URL + "/"


class TestSyncGovernanceAuthorityContract:
    """The normative MUST: verify authority before persisting; per-account failures."""

    @pytest.mark.asyncio
    async def test_unknown_account_fails_with_account_not_found(self):
        from src.core.tools.governance import _sync_governance_impl

        def _raise(ref, ident, r):
            raise AdCPAccountNotFoundError("Account 'acc_x' not found.", suggestion="Use list_accounts.")

        stack, repo = _patch_deps(resolve_side_effect=_raise)
        with stack:
            resp = await _sync_governance_impl(_make_request(account_ref={"account_id": "acc_x"}), _make_identity())

        assert resp.accounts[0].status == "failed"
        assert resp.accounts[0].errors[0].code == "ACCOUNT_NOT_FOUND"
        # ACCOUNT_NOT_FOUND recovery is pinned by the enumMetadata (terminal) — the
        # per-account error MUST carry it so a receiver does not auto-retry (else it
        # defaults to transient). Derived from the pinned enum. See #1329.
        assert resp.accounts[0].errors[0].recovery == _ACCOUNT_NOT_FOUND_RECOVERY
        # A failed account never persists a binding.
        repo.set_governance_binding.assert_not_called()

    @pytest.mark.asyncio
    async def test_unowned_account_fails_with_account_not_found(self):
        from src.core.tools.governance import _sync_governance_impl

        def _raise(ref, ident, r):
            raise AdCPAuthorizationError("Agent lacks access to 'acc_1'.", suggestion="Use list_accounts.")

        stack, repo = _patch_deps(resolve_side_effect=_raise)
        with stack:
            resp = await _sync_governance_impl(_make_request(), _make_identity())

        assert resp.accounts[0].status == "failed"
        # An existing account the agent has no authority over is collapsed to the
        # SAME ACCOUNT_NOT_FOUND result as a nonexistent account — the *_NOT_FOUND
        # uniform-response MUST (no cross-principal enumeration oracle). It is NOT
        # SCOPE_INSUFFICIENT (a task-scope / allowed_tasks code this seller does not
        # model) nor the AdCPAuthorizationError wire default (AUTH_REQUIRED). #1329.
        err = resp.accounts[0].errors[0]
        assert err.code == "ACCOUNT_NOT_FOUND"
        assert err.recovery == _ACCOUNT_NOT_FOUND_RECOVERY
        # Uniform: the message MUST NOT reveal that the account exists.
        assert "does not have access" not in err.message
        repo.set_governance_binding.assert_not_called()

    @pytest.mark.asyncio
    async def test_partial_failure_stays_success_variant(self):
        from src.core.tools.governance import _sync_governance_impl

        def _resolve(ref, ident, r):
            if ref.root.account_id == "acc_ok":
                return "acc_ok"
            raise AdCPAccountNotFoundError("nope", suggestion="s")

        accounts = [
            account_entry({"account_id": "acc_ok"}, agents=[governance_agent_dict(GOV_URL)]),
            account_entry({"account_id": "acc_bad"}, agents=[governance_agent_dict(GOV_URL)]),
        ]
        req = _make_request(accounts=accounts)
        stack, repo = _patch_deps(resolve_side_effect=_resolve)
        with stack:
            resp = await _sync_governance_impl(req, _make_identity())

        assert len(resp.accounts) == 2
        statuses = {a.account.root.account_id: a.status for a in resp.accounts}
        assert statuses == {"acc_ok": "synced", "acc_bad": "failed"}
        # Only the synced account persisted, via the repository write path.
        repo.set_governance_binding.assert_called_once_with("acc_ok", req.accounts[0].governance_agents)


class TestSyncGovernanceOperationLevel:
    """Operation-level failures: auth and empty accounts."""

    @pytest.mark.asyncio
    async def test_missing_auth_raises_auth_required(self):
        from src.core.tools.governance import _sync_governance_impl

        stack, _repo = _patch_deps(resolve_side_effect=lambda *a: "acc_1")
        with stack, pytest.raises(AdCPAuthenticationError):
            await _sync_governance_impl(_make_request(), identity=None)

    def test_empty_accounts_rejected_at_schema(self):
        # The request schema enforces accounts minItems:1, so an empty array is a
        # construction-time validation error — it never reaches the impl.
        err = _first_schema_error(
            lambda: SyncGovernanceRequest(idempotency_key="uuid-v4-unit-00000000000000001", accounts=[])
        )
        assert err["type"] == "too_short", err
        assert err["loc"][-1] == "accounts", err


class TestSyncGovernanceRequestSchema:
    """Request schema enforces the spec's idempotency_key + agent constraints."""

    def test_idempotency_key_required(self):
        err = _first_schema_error(
            lambda: SyncGovernanceRequest(
                accounts=[account_entry({"account_id": "acc_1"}, agents=[governance_agent_dict(GOV_URL)])]
            )
        )
        assert err["type"] == "missing", err
        assert err["loc"][-1] == "idempotency_key", err

    def test_idempotency_key_too_short_rejected(self):
        err = _first_schema_error(lambda: _make_request(idempotency_key="short"))  # < 16 chars
        assert err["type"] == "string_too_short", err
        assert err["loc"][-1] == "idempotency_key", err

    def test_non_https_agent_url_rejected(self):
        # url gates raise field-located errors at accounts[i].governance_agents[j].url (#1329).
        err = _first_schema_error(lambda: _make_request(url="http://governance.pinnacle-media.com"))
        assert err["type"] == "value_error", err
        assert "https" in err["msg"], err
        assert err["loc"] == ("accounts", 0, "governance_agents", 0, "url"), err

    def test_non_https_url_message_strips_userinfo(self):
        # The https gate is ordered AFTER the userinfo gate, so it only ever renders
        # userinfo-free urls; even so, the rendered url is userinfo-stripped as defense
        # in depth — the message must never echo a credential (#1329).
        err = _first_schema_error(lambda: _make_request(url="http://svc:SuperSecret456@governance.example.com/hook"))
        # userinfo gate fires first — but assert the secret is absent regardless of which gate.
        assert "SuperSecret456" not in err["msg"], err

    def test_agent_url_with_userinfo_rejected(self):
        # A credential embedded in the url (userinfo) must be rejected — it bypasses
        # SSRF hostname checks and would be persisted/echoed (#1329). The
        # rejection message must NOT echo the secret.
        err = _first_schema_error(lambda: _make_request(url="https://svc:SuperSecret123@governance.example.com/hook"))
        assert err["type"] == "value_error", err
        assert "userinfo" in err["msg"], err
        assert "SuperSecret123" not in err["msg"], err
        assert err["loc"] == ("accounts", 0, "governance_agents", 0, "url"), err

    def test_agent_url_password_only_userinfo_rejected(self):
        # Password-only userinfo (username absent) must still be rejected — grades the
        # second operand of the userinfo check, which a username-only test never exercises
        # (#1329).
        err = _first_schema_error(lambda: _make_request(url="https://:SuperSecret789@governance.example.com/hook"))
        assert err["type"] == "value_error" and "userinfo" in err["msg"], err
        assert "SuperSecret789" not in err["msg"], err

    def test_agent_url_username_only_userinfo_rejected(self):
        # Username-only userinfo (no password) must also be rejected — grades the first
        # operand independently (#1329).
        err = _first_schema_error(lambda: _make_request(url="https://serviceacct@governance.example.com/hook"))
        assert err["type"] == "value_error" and "userinfo" in err["msg"], err

    @pytest.mark.parametrize(
        "url",
        [
            "https://localhost/hook",
            "https://127.0.0.1:8000/hook",
            "https://169.254.169.254/latest/meta-data",
        ],
    )
    def test_agent_url_ssrf_target_rejected(self, url):
        # A persisted governance url is a future check_governance target; internal /
        # loopback / metadata hosts are rejected at bind time (#1329).
        err = _first_schema_error(lambda: _make_request(url=url))
        assert err["type"] == "value_error", err
        assert "disallowed host" in err["msg"], err

    def test_credentials_below_min_length_rejected(self):
        err = _first_schema_error(
            lambda: SyncGovernanceRequest(
                idempotency_key="uuid-v4-unit-00000000000000001",
                accounts=[
                    account_entry({"account_id": "acc_1"}, agents=[governance_agent_dict(GOV_URL, credentials="short")])
                ],
            )
        )
        assert err["type"] == "string_too_short", err
        assert err["loc"][-1] == "credentials", err

    def test_more_than_one_agent_per_account_rejected(self):
        err = _first_schema_error(
            lambda: SyncGovernanceRequest(
                idempotency_key="uuid-v4-unit-00000000000000001",
                accounts=[
                    account_entry(
                        {"account_id": "acc_1"},
                        agents=[
                            governance_agent_dict(GOV_URL),
                            governance_agent_dict("https://other.example.com"),
                        ],
                    )
                ],
            )
        )
        assert err["type"] == "too_long", err
        assert err["loc"][-1] == "governance_agents", err


class TestNonRestRequestBuilder:
    """``build_sync_governance_request`` is the single non-REST field list (MCP + A2A).

    Pins the two properties the shared builder guarantees so the two hand-assembling
    transports cannot drift again (#1329): ``ext`` is forwarded (the MCP wrapper
    previously dropped it) and an absent ``idempotency_key`` is OMITTED, not passed as
    ``None`` (so it renders as the "missing" error REST produces, not a NoneType error).
    """

    _KEY = "uuid-v4-unit-00000000000000001"

    def _account(self) -> dict:
        return account_entry({"account_id": "acc_1"}, agents=[governance_agent_dict(GOV_URL)])

    def test_ext_is_forwarded(self):
        # Reddens if the builder drops ``ext`` — the exact fork the MCP wrapper had
        # (A2A forwarded ext, MCP did not), a spec-valid field vanishing off one transport.
        from src.core.tools.governance import build_sync_governance_request

        req = build_sync_governance_request(
            accounts=[self._account()], context=None, ext={"vendor_flag": True}, idempotency_key=self._KEY
        )
        # ext coerces to an ExtensionObject; compare its serialized (wire) form.
        assert req.model_dump(mode="json").get("ext") == {"vendor_flag": True}

    def test_absent_idempotency_key_omitted_renders_missing(self):
        # OMIT (not None): a missing key renders as "Required field is missing" (matching
        # REST's model_dump(exclude_none)), not "Expected string, got NoneType" (#1329 H1).
        from src.core.exceptions import AdCPValidationError
        from src.core.tools.governance import build_sync_governance_request

        with pytest.raises(AdCPValidationError) as exc:
            build_sync_governance_request(accounts=[self._account()], context=None, ext=None, idempotency_key=None)
        assert "missing" in str(exc.value).lower(), exc.value

    async def test_mcp_wrapper_threads_ext_through_to_impl(self):
        # Pin the fork at the wrapper boundary: the MCP wrapper must pass ``ext`` into the
        # request the _impl receives. Reddens if the wrapper drops ext before the builder.
        from src.core.tools import governance as gov

        captured: dict = {}

        async def _capture(req, identity):
            captured["ext"] = req.model_dump(mode="json").get("ext")
            return SyncGovernanceResponse(accounts=[], context=None)

        with patch.object(gov, "_sync_governance_impl", side_effect=_capture):
            await gov.sync_governance(
                idempotency_key=self._KEY, accounts=[self._account()], ext={"vendor_flag": True}, ctx=None
            )
        assert captured["ext"] == {"vendor_flag": True}


class TestSyncGovernanceBoundaryValues:
    """Construction-time schema asserts for the @bva boundary VALUES (#1329).

    The UC-030 ``@bva`` outlines are xfailed (abstract verdict-step wiring is deferred),
    and the old conftest reason claimed the boundary values are "covered concretely by the
    T-UC-030-sync-* scenarios" — which they are NOT (those exercise one nominal value each).
    These construction-time asserts are the honest coverage the corrected reason points at:
    the request schema enforces every boundary structurally.
    """

    _KEY = "uuid-v4-unit-00000000000000001"

    def _reject(self, agent: dict) -> dict:
        return _first_schema_error(
            lambda: SyncGovernanceRequest(
                idempotency_key=self._KEY,
                accounts=[account_entry({"account_id": "a"}, agents=[agent])],
            )
        )

    @pytest.mark.parametrize(
        ("authentication", "loc_token", "err_type"),
        [
            ({"schemes": [], "credentials": "x" * 40}, "schemes", "too_short"),
            ({"schemes": ["Bearer", "Basic"], "credentials": "x" * 40}, "schemes", "too_long"),
            ({"schemes": ["Nonsense"], "credentials": "x" * 40}, "schemes", "enum"),
            ({"credentials": "x" * 40}, "schemes", "missing"),
            ({"schemes": ["Bearer"]}, "credentials", "missing"),
        ],
        ids=["schemes-empty", "schemes-two", "schemes-outside-enum", "schemes-absent", "credentials-absent"],
    )
    def test_authentication_boundary_rejected(self, authentication: dict, loc_token: str, err_type: str):
        err = self._reject({"url": "https://gov.example.com", "authentication": authentication})
        assert err["type"] == err_type, err
        assert loc_token in err["loc"], err

    @pytest.mark.parametrize(
        ("agent", "err_type"),
        [
            ({"url": "not a uri", "authentication": {"schemes": ["Bearer"], "credentials": "x" * 40}}, "url_parsing"),
            ({"authentication": {"schemes": ["Bearer"], "credentials": "x" * 40}}, "missing"),
        ],
        ids=["url-non-uri", "url-absent"],
    )
    def test_url_boundary_rejected(self, agent: dict, err_type: str):
        err = self._reject(agent)
        assert err["type"] == err_type, err
        assert "url" in err["loc"], err

    def test_accounts_exactly_100_is_valid(self):
        # maxItems 100 — the inclusive upper boundary is accepted (the @bva "valid" row).
        req = SyncGovernanceRequest(
            idempotency_key=self._KEY,
            accounts=[
                account_entry({"account_id": f"a{i}"}, agents=[governance_agent_dict(GOV_URL)]) for i in range(100)
            ],
        )
        assert len(req.accounts) == 100
