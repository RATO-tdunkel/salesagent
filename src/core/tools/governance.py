"""sync_governance tool implementation (UC-030, #1329 gap 14 dependency).

Binds a buyer-designated governance agent per account per AdCP 3.1.1
(account/sync-governance-request.json + accounts/tasks/sync_governance.mdx).

This is the SELLER side of the governance handshake: a buyer registers exactly
one governance agent per account, and the seller persists that binding
(replace semantics) so a governance-aware seller could later call it via
check_governance during the media-buy lifecycle. This sales agent registers the
binding but does NOT enforce it downstream — it deliberately does not declare
the ``governance-aware-seller`` specialism. Registering the binding is what the
``sales-non-guaranteed`` / ``sales-guaranteed`` specialism storyboards grade
(``accounts[0].status == "synced"`` + schema + context echo); enforcement
(check_governance) is a separate, un-declared capability.

Spec grounding (pinned AdCP 3.1.1 / adcp 6.6.0):
- Request: ``idempotency_key`` (required, ^[A-Za-z0-9_.:-]{16,255}$), ``accounts[]``
  (1..100), each ``{account: AccountReference, governance_agents[maxItems:1]}``,
  agent ``{url ^https://, authentication{schemes, credentials minLength:32}}``.
- Response: success variant carries ``accounts[]`` with per-item
  ``status in {synced, failed}``; synced echoes ``governance_agents[].url`` but
  NEVER credentials; envelope ``status: completed`` for the synchronous path.
- Normative MUST (sync_governance.mdx): "the seller MUST verify that the
  authenticated agent has authority over each referenced account before
  persisting." An account that is unknown OR exists but the agent lacks authority
  over both return per-account ``status: failed`` with ``ACCOUNT_NOT_FOUND`` — the
  two are indistinguishable per the ``*_NOT_FOUND`` uniform-response MUST (no
  cross-principal enumeration oracle). This is the graded BR-UC-030
  ``sync-no-authority`` code; ``SCOPE_INSUFFICIENT`` is NOT used (it is a
  task-scope / ``allowed_tasks`` code this seller does not model). See
  ``_sync_one_account`` for the full per-account error-code rationale.

Credentials are never persisted: the ``accounts.governance_agents`` column model
(core/account.json GovernanceAgent) is url-only by construction, so the binding
stores the durable agent identity (url) and nothing sensitive.

Idempotency replay + IDEMPOTENCY_CONFLICT (same key / different payload) are NOT
implemented here — a genuine deferred gap, tracked by the xfailed UC-030 replay/
conflict scenarios (#1329 follow-up). "Side-effect-free replace ⇒ resource-idempotent"
answers only the side-effect half; security.mdx L1 rule 4 requires returning the
STORED inner response WITHOUT re-executing, and replay is observably different from
re-execution here because per-account authority can change between calls (a replay
could flip an account synced -> failed). create_media_buy is the sibling that DOES
dedup (uow.idempotency_attempts); sync_accounts shares this same gap. The agent-wide
Idempotency(supported=True) capability is honored by create_media_buy (the mutating
tool with real spend), which is why it stays declared.
"""

from typing import Annotated, Any

from adcp.types import AccountReference as LibraryAccountReference
from adcp.types import ContextObject, Error
from adcp.types.aliases import SyncGovernanceAccount as SyncGovernanceAccountInput
from fastmcp.server.context import Context
from fastmcp.tools.tool import ToolResult
from pydantic import Field

from src.core.audit_logger import get_audit_logger
from src.core.auth import require_identity, require_principal_id, require_tenant
from src.core.database.repositories.account import AccountRepository
from src.core.database.repositories.uow import AccountUoW
from src.core.exceptions import (
    AdCPAccountAmbiguousError,
    AdCPAccountNotFoundError,
    AdCPAuthorizationError,
    AdCPError,
    AdCPValidationError,
    RecoveryHint,
)
from src.core.helpers.account_helpers import resolve_account
from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas.account import (
    SyncedGovernanceAgent,
    SyncGovernanceRequest,
    SyncGovernanceResponse,
    SyncGovernanceResponseAccount,
)
from src.core.tool_context import ToolContext
from src.core.transport_helpers import resolve_identity_from_context
from src.core.validation_helpers import adcp_validation_boundary

# Uniform per-account RESPONSE for an unresolved account. The *_NOT_FOUND
# uniform-response MUST (pinned error-code.json: CREATIVE_NOT_FOUND /
# SIGNAL_NOT_FOUND / PLAN_NOT_FOUND, generalized by REFERENCE_NOT_FOUND) requires
# that "the account does not exist" and "the account exists but the caller lacks
# authority over it" be indistinguishable on the RESPONSE channel — same code,
# message, and suggestion — otherwise the response is a cross-principal enumeration
# oracle. Both are surfaced with this one code + message + suggestion (mirrors the
# sync-governance-response.json partial-failure example). NOTE: this closes the
# response channel only. A second channel — response TIMING — is not closed here:
# _resolve_by_id raises not-found BEFORE running the access check, so a not-found is
# a shorter path than an exists-but-unauthorized. That timing side channel is shared
# with create_media_buy/sync_creatives (_resolve_by_id) and tracked separately
# (#1329 follow-up); it is not introduced by this tool.
_UNRESOLVED_ACCOUNT_MESSAGE = "Account does not exist or is not accessible to the authenticated agent."
_UNRESOLVED_ACCOUNT_SUGGESTION = "Use list_accounts to find accounts accessible to this agent."

# Code + recovery for the uniform unresolved-account result are DERIVED from the
# canonical AdCPAccountNotFoundError class metadata, not copied as literals, so the
# per-account wire code/recovery cannot drift from the exception the rest of the
# codebase raises for ACCOUNT_NOT_FOUND (#1329). The class docstring pins
# recovery=terminal against the pinned enumMetadata.
_UNRESOLVED_ACCOUNT_CODE = AdCPAccountNotFoundError._default_error_code
_UNRESOLVED_ACCOUNT_RECOVERY = AdCPAccountNotFoundError._default_recovery


def _failed_account_result(
    account_ref: LibraryAccountReference,
    code: str,
    *,
    recovery: RecoveryHint,
    message: str,
    suggestion: str | None = None,
) -> SyncGovernanceResponseAccount:
    """Build a per-account ``failed`` result carrying a single per-account error.

    ``code`` and ``recovery`` are set together and MUST agree with the pinned
    ``enums/error-code.json`` ``enumMetadata``. ``recovery`` is mandatory
    (keyword-only, no default): a receiver that cannot classify an unknown code
    falls back to ``transient`` when ``recovery`` is absent, which would auto-retry
    a non-retryable authz/not-found failure (#1329). The code is the
    spec-facing per-account code set explicitly, not read from the exception's wire
    code — the authority failure is relabeled to the uniform ``ACCOUNT_NOT_FOUND``
    (see ``_sync_one_account``).
    """
    return SyncGovernanceResponseAccount(
        account=account_ref,
        status="failed",
        errors=[
            Error(  # structural-guard: advisory per-account result in SyncGovernanceResponse.accounts[].errors[]
                code=code, message=message, suggestion=suggestion, recovery=recovery
            )
        ],
    )


def _sync_one_account(
    entry: SyncGovernanceAccountInput, identity: ResolvedIdentity, repo: AccountRepository
) -> SyncGovernanceResponseAccount:
    """Sync a single account's governance binding (authority check → persist → echo).

    Per-account failures (unknown/unowned/ambiguous/blocked account) are returned
    as ``failed`` results, NOT raised — the overall response stays the success
    variant with a mix of synced/failed entries (partial-failure model).

    Per-account error codes are the standard AdCP vocabulary
    (``enums/error-code.json``, pinned 3.1.1) that the graded BR-UC-030
    ``sync-no-authority`` scenario (feature line 179) checks on the wire:
    - ``ACCOUNT_NOT_FOUND`` (terminal) for an account that is unknown OR exists but
      the agent has no authority over — the two are collapsed for uniform response
      (see ``_UNRESOLVED_ACCOUNT_MESSAGE``). ``SCOPE_INSUFFICIENT`` is deliberately
      NOT used: it is a *task-scope* code (``allowed_tasks``) per its enum
      definition, a concept this seller does not model — ``has_access`` is a binary
      ownership check, so a scope-shaped code would misdescribe the failure and (by
      distinguishing exists-but-unauthorized from not-found) reintroduce the
      enumeration oracle the uniform-response MUST forbids.
    - ``ACCOUNT_AMBIGUOUS`` (correctable) for a natural key matching several of the
      caller's own accounts (scoped to accessible accounts, so not an oracle).
    - the resolver's own code + recovery for account-status blocks
      (setup/suspended/payment), which agree by construction.
    """
    try:
        account_id = resolve_account(entry.account, identity, repo)
    except (AdCPAccountNotFoundError, AdCPAuthorizationError):
        # Both collapse to the uniform ACCOUNT_NOT_FOUND (no enumeration oracle). The
        # code + recovery are the uniform ACCOUNT_NOT_FOUND's — NOT the caught
        # exception's (AdCPAuthorizationError's recovery differs, and leaking it would
        # re-open the oracle via recovery) — derived from the canonical class metadata.
        return _failed_account_result(
            entry.account,
            _UNRESOLVED_ACCOUNT_CODE,
            recovery=_UNRESOLVED_ACCOUNT_RECOVERY,
            message=_UNRESOLVED_ACCOUNT_MESSAGE,
            suggestion=_UNRESOLVED_ACCOUNT_SUGGESTION,
        )
    except (AdCPAccountAmbiguousError, AdCPError) as e:
        # Any other resolver failure — an ambiguous natural key (ACCOUNT_AMBIGUOUS,
        # scoped to the caller's own accounts so not an enumeration oracle) or an
        # account-status block (setup/suspended/payment) — surfaces as an honest
        # per-account failure carrying the exception's OWN code + recovery (they agree
        # by construction), never a silent success. AdCPAccountAmbiguousError is an
        # AdCPError subclass, so one handler covers both; it is named explicitly for
        # readers. The earlier NotFound/Authorization except stays separate because it
        # deliberately does NOT echo the caught code (that would reopen the oracle).
        return _failed_account_result(
            entry.account,
            e.error_code,
            recovery=e.recovery,
            message=str(e),
            suggestion=getattr(e, "suggestion", None),
        )

    # Persist through the repository, which OWNS the url-only projection (credentials
    # never persisted — #1329) and returns the stored list as typed GovernanceAgentColumn
    # records ({url: str}). Echo from that same list so persisted and echoed can never
    # disagree; the typed record means ``agent["url"]`` is statically key-checked — a column
    # rename fails mypy here rather than becoming a runtime KeyError (#1329).
    # set_governance_binding replaces the prior binding (per-account replace semantics).
    agent_urls = repo.set_governance_binding(account_id, entry.governance_agents)

    return SyncGovernanceResponseAccount(
        account=entry.account,
        status="synced",
        governance_agents=[SyncedGovernanceAgent(url=agent["url"]) for agent in agent_urls],
    )


async def _sync_governance_impl(
    req: SyncGovernanceRequest | None = None,
    identity: ResolvedIdentity | None = None,
) -> SyncGovernanceResponse:
    """Shared implementation for sync_governance.

    Args:
        req: Sync request with idempotency_key and per-account governance agents.
        identity: Resolved identity (must be authenticated).

    Returns:
        SyncGovernanceResponse with per-account synced/failed results.
    """
    if req is None:
        raise AdCPValidationError("sync_governance requires a request body with accounts and idempotency_key.")

    # Authentication is REQUIRED (write tool). require_principal_id first so the
    # canonical AUTH_REQUIRED message surfaces for a missing/anonymous token;
    # require_identity then narrows the type for the tenant lookup below.
    principal_id = require_principal_id(identity, context=req.context)
    identity = require_identity(identity, context=req.context)
    tenant = require_tenant(identity, context=req.context)
    tenant_id = tenant["tenant_id"]

    # A non-empty accounts array is guaranteed by the request schema (minItems: 1);
    # invalid requests are rejected at construction / the validation boundary.
    results: list[SyncGovernanceResponseAccount] = []
    with AccountUoW(tenant_id) as uow:
        assert uow.accounts is not None
        repo = uow.accounts
        for entry in req.accounts:
            results.append(_sync_one_account(entry, identity, repo))

    synced = sum(1 for r in results if r.status == "synced")
    audit_logger = get_audit_logger("sync_governance", tenant_id)
    audit_logger.log_info(f"sync_governance completed: {synced}/{len(results)} synced (principal={principal_id})")

    return SyncGovernanceResponse(accounts=results, context=req.context)


# ---------------------------------------------------------------------------
# Shared request assembly (non-REST transports)
# ---------------------------------------------------------------------------


def build_sync_governance_request(
    *,
    accounts: list[SyncGovernanceAccountInput] | list[dict[str, Any]] | None,
    context: ContextObject | dict[str, Any] | None,
    ext: dict[str, Any] | None,
    idempotency_key: str | None,
) -> SyncGovernanceRequest:
    """Assemble a ``SyncGovernanceRequest`` from loose params (the MCP + A2A wrappers).

    Owns the CONSTRUCTION SEMANTICS the two hand-assembling transports must share — the
    validation boundary, the omit-``None``-idempotency_key behaviour, and the boundary
    context string — so those cannot drift between MCP and A2A. It does NOT own the field
    LIST: the field names are still enumerated at four sites (these params, the MCP
    signature below, ``SyncGovernanceBody`` in ``src/routes/api_v1.py``, and the A2A
    skill's ``parameters.get(...)`` kwargs), and adding a spec field touches each.
    ``tests/unit/test_boundary_field_forwarding.py::TestSyncGovernanceFieldForwarding``
    is what actually prevents a field drift — it derives the spec-field set from the
    request model and asserts both wrappers forward it here (#1329 R9-D1). REST builds
    generically via ``model_dump(exclude_none=True)`` and needs no counterpart.
    Construction runs inside the AdCP validation boundary so a schema violation
    (missing/short idempotency_key, non-https url, short credentials, agent
    cardinality) surfaces as the VALIDATION_ERROR envelope — the same wire shape REST
    produces — on both transports (#1329).

    ``idempotency_key`` is OMITTED when None (not passed as None): REST drops it via
    ``model_dump(exclude_none=True)``, so a missing key renders as "Required field is
    missing" on all three transports rather than "Expected string, got NoneType"
    (#1329 H1). ``ext`` (the AdCP extension carrier) is forwarded on both transports —
    the MCP wrapper previously omitted it, forking a spec-valid field off one
    transport under a comment claiming parity (#1329 I4).
    """
    kwargs: dict[str, Any] = {"accounts": accounts or [], "context": context, "ext": ext}
    if idempotency_key is not None:
        kwargs["idempotency_key"] = idempotency_key
    with adcp_validation_boundary(context="sync_governance request"):
        return SyncGovernanceRequest(**kwargs)


# ---------------------------------------------------------------------------
# MCP wrapper
# ---------------------------------------------------------------------------


async def sync_governance(
    idempotency_key: Annotated[
        str | None,
        Field(description="Client-generated at-most-once key (spec-required; ^[A-Za-z0-9_.:-]{16,255}$)"),
    ] = None,
    accounts: list[SyncGovernanceAccountInput] | None = None,
    context: ContextObject | None = None,
    ext: dict[str, Any] | None = None,
    ctx: Context | ToolContext | None = None,
) -> ToolResult:
    """Bind a governance agent per account (MCP tool).

    MCP wrapper that accepts individual parameters per AdCP spec and constructs a
    SyncGovernanceRequest — via the shared ``build_sync_governance_request`` builder
    (the single non-REST field list, also used by the A2A skill) — for the shared
    implementation. ``idempotency_key`` is spec-required, but it is typed ``str | None``
    here (not ``str``) so a missing key surfaces as an AdCP validation error at model
    construction — the same wire shape REST/A2A produce — rather than being rejected
    earlier by FastMCP's own parameter-schema layer with a different, non-AdCP error
    shape. The schema's required ``idempotency_key`` still rejects ``None`` (UC-030
    grades that).

    Args:
        idempotency_key: Client-generated at-most-once key (spec-required; a
            missing key is rejected at request construction).
        accounts: Per-account governance agent bindings.
        context: Application-level context per AdCP spec (echoed back).
        ext: AdCP extension carrier — forwarded through the shared builder so the
            field is not dropped on this transport (#1329 I4).
        ctx: FastMCP context for authentication.

    Returns:
        ToolResult with human-readable text and structured data.
    """
    req = build_sync_governance_request(accounts=accounts, context=context, ext=ext, idempotency_key=idempotency_key)
    identity = (await ctx.get_state("identity")) if isinstance(ctx, Context) else None
    response = await _sync_governance_impl(req, identity)
    return ToolResult(content=str(response), structured_content=response)


# ---------------------------------------------------------------------------
# A2A / REST raw wrapper
# ---------------------------------------------------------------------------


async def sync_governance_raw(
    req: SyncGovernanceRequest | None = None,
    ctx: Context | ToolContext | None = None,
    identity: ResolvedIdentity | None = None,
) -> SyncGovernanceResponse:
    """Bind a governance agent per account (raw function for A2A/REST).

    Args:
        req: Sync request with per-account governance agents.
        ctx: FastMCP context.
        identity: Pre-resolved identity (if available).

    Returns:
        SyncGovernanceResponse with per-account results.
    """
    if identity is None:
        identity = resolve_identity_from_context(ctx, require_valid_token=True)
    return await _sync_governance_impl(req, identity)
