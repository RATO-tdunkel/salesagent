"""Account-related Pydantic schemas.

Extends adcp library account types per pattern #1 (schema inheritance).
All classes are re-exported from ``src.core.schemas`` for backward compatibility.

beads: salesagent-x79

SDK 5.7 type:ignore tracking (adcontextprotocol/adcp-client-python#913):
- [misc] on line ~127: SyncAccountsResponse class def. Pydantic metaclass
  interaction in SDK hierarchy; permanent.
- [assignment] on line ~79: idempotency_key override (required -> optional).
  Architectural; permanent.
"""

from typing import Any, Literal, NoReturn

from adcp.types import Account as LibraryAccountDomain
from adcp.types import AccountReference as LibraryAccountReference
from adcp.types import ContextObject as LibraryContextObject
from adcp.types import Error as LibraryError
from adcp.types import ListAccountsRequest as LibraryListAccountsRequest
from adcp.types import ListAccountsResponse as LibraryListAccountsResponse
from adcp.types import Setup as LibrarySetup
from adcp.types import SyncAccountsRequest as LibrarySyncAccountsRequest
from adcp.types import SyncGovernanceRequest as LibrarySyncGovernanceRequest
from adcp.types import SyncGovernanceResponse as LibrarySyncGovernanceResponse
from adcp.types.aliases import SyncAccountsSuccessResponse as LibrarySyncAccountsSuccess
from adcp.types.generated_poc.core.brand_ref import BrandReference as LibraryBrandReference
from pydantic import ConfigDict, model_validator
from pydantic_core import InitErrorDetails, PydanticCustomError
from pydantic_core import ValidationError as CoreValidationError

from src.core.config import get_pydantic_extra_mode
from src.core.schemas._base import NestedModelSerializerMixin, SalesAgentBaseModel
from src.core.security.url_validator import check_url_ssrf, strip_url_userinfo

# ---------------------------------------------------------------------------
# Core domain Account (used in ListAccountsResponse.accounts)
# ---------------------------------------------------------------------------


class Account(LibraryAccountDomain):
    """Extends library Account with salesagent model_config.

    Library provides: account_id, name, advertiser, billing_proxy, status,
    brand, operator, billing, rate_card, payment_terms, credit_limit, setup,
    account_scope, governance_agents, sandbox, ext.
    """

    model_config = ConfigDict(extra=get_pydantic_extra_mode())

    # POST-S3: Buyer knows advertiser, rate_card, and payment_terms.
    # Library model_dump defaults exclude_none=True which strips these when
    # None.  Override to always include them so callers can distinguish
    # "field absent" from "field=null".
    _ALWAYS_INCLUDE = {"advertiser", "rate_card", "payment_terms"}

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        result = super().model_dump(**kwargs)
        for field in self._ALWAYS_INCLUDE:
            if field not in result:
                result[field] = getattr(self, field, None)
        return result


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class ListAccountsRequest(LibraryListAccountsRequest):
    """Extends library ListAccountsRequest.

    Library provides: status, pagination, sandbox, context, ext.
    """

    model_config = ConfigDict(extra=get_pydantic_extra_mode())


class SyncAccountsRequest(LibrarySyncAccountsRequest):
    """Extends library SyncAccountsRequest.

    Library provides: idempotency_key, accounts, delete_missing, dry_run,
    push_notification_config, context, ext.
    """

    model_config = ConfigDict(extra=get_pydantic_extra_mode())

    # adcp 4.3 makes idempotency_key required.  Override as optional —
    # generated at the transport boundary when not supplied by the caller.
    idempotency_key: str | None = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ListAccountsResponse(NestedModelSerializerMixin, LibraryListAccountsResponse):
    """Extends library ListAccountsResponse.

    Library provides: accounts, errors, pagination, context, ext.
    NestedModelSerializerMixin ensures nested Account objects serialize correctly.
    Accounts field redeclared for Pattern #4 (nested serialization with local subclass).
    """

    model_config = ConfigDict(extra=get_pydantic_extra_mode())

    # Required (no default): pinned 3.1 list-accounts-response marks 'accounts'
    # required. Redeclared for Pattern #4 (nested serialization with local subclass)
    # and to enforce the spec-required field (#1399 Plan-B).
    accounts: list[Account]  # type: ignore[assignment]

    def __str__(self) -> str:
        """Return human-readable summary message for protocol envelope."""
        count = len(self.accounts) if self.accounts else 0
        return f"Found {count} account{'s' if count != 1 else ''}."


class SyncResponseAccount(SalesAgentBaseModel):
    """Per-account result in a sync_accounts response.

    SDK 4.3 provided this as adcp.types.generated_poc.account.sync_accounts_response.Account.
    SDK 5.7 restructured the response; we now own this model.

    Fields are typed with adcp library models (Error, Setup) so Pydantic
    reconstructs them properly on transport roundtrip (A2A/MCP/REST).

    brand/operator/action/status are REQUIRED per the pinned AdCP schema
    (adcontextprotocol/adcp@04f59d2d5, sync-accounts-response success variant,
    accounts.items.required) — the model enforces them rather than relying on every
    call site. billing stays optional (not in the schema's required set).
    """

    brand: LibraryBrandReference
    operator: str
    action: str
    status: str
    account_id: str | None = None
    name: str | None = None
    billing: str | None = None
    sandbox: bool | None = None
    errors: list[LibraryError] | None = None
    setup: LibrarySetup | None = None


class SyncAccountsResponse(NestedModelSerializerMixin, LibrarySyncAccountsSuccess):  # type: ignore[misc]
    """Extends library SyncAccountsResponse success variant.

    adcp 3.10: SyncAccountsResponse is a union TypeAlias (not RootModel).
    Since the error variant is never constructed (ToolError handles failures),
    we subclass the success variant directly.

    SDK 5.7 collapsed the success envelope to just `status`. Fields previously
    inherited (accounts, dry_run, context, ext) are now declared locally.
    """

    model_config = ConfigDict(extra=get_pydantic_extra_mode())

    # SDK 5.7 removed these from the parent — declare locally.
    # Typed as SyncResponseAccount for proper deserialization on transport roundtrip.
    # `accounts` is REQUIRED (no default): AdCP 3.1 sync-accounts-response is
    # oneOf(SyncAccountsSuccess requires `accounts` | SyncAccountsError requires
    # `errors`). This model is the success variant, so omitting `accounts`
    # entirely is invalid (it would be neither a valid success nor error). May
    # be an empty list for a zero-account sync, but the field must be present.
    accounts: list[SyncResponseAccount]
    dry_run: bool | None = None
    context: LibraryContextObject | dict[str, Any] | None = None
    ext: dict[str, Any] | None = None

    def __str__(self) -> str:
        """Return human-readable summary message for protocol envelope."""
        count = len(self.accounts) if self.accounts else 0
        dry_run_note = " (dry run)" if self.dry_run else ""
        return f"Synced {count} account{'s' if count != 1 else ''}{dry_run_note}."


# ---------------------------------------------------------------------------
# sync_governance — bind a governance agent per account (UC-030, #1329)
# ---------------------------------------------------------------------------


def _raise_governance_url_error(loc: tuple[str | int, ...], message: str, input_value: str) -> NoReturn:
    """Raise a field-located ``ValidationError`` for a rejected governance agent url.

    A ``model_validator(mode="after")`` that raises a bare ``ValueError`` produces
    ``loc=()`` — the buyer wire then carries ``field=""`` on both envelope layers and an
    empty bullet. Emitting an explicit ``loc`` via ``from_exception_data`` restores the
    ``accounts[i].governance_agents[j].url`` field pointer so error consumers (and the
    BR-UC-030 wire steps) can pin ``field=`` instead of a free-text message match
    (#1682 review H2). The rendered message must be a literal (no ``{}`` placeholders) —
    ``PydanticCustomError`` treats the second arg as a template.
    """
    raise CoreValidationError.from_exception_data(
        "SyncGovernanceRequest",
        [InitErrorDetails(type=PydanticCustomError("value_error", message), loc=loc, input=input_value)],
    )


class SyncGovernanceRequest(LibrarySyncGovernanceRequest):
    """Extends library SyncGovernanceRequest.

    Library provides: idempotency_key (required), accounts, context, ext.
    Per the pinned 3.1.1 schema (account/sync-governance-request.json),
    ``idempotency_key`` is REQUIRED (``x-mutates-state: true``) and each
    ``accounts[]`` entry pairs an ``AccountReference`` with a ``governance_agents``
    array of ``maxItems: 1``. Unlike SyncAccountsRequest, we do NOT relax
    ``idempotency_key`` to optional: UC-030 grades rejection when it is absent,
    so a missing key must surface as a validation error, not be auto-generated.
    """

    model_config = ConfigDict(extra=get_pydantic_extra_mode())

    @model_validator(mode="after")
    def _validate_governance_agent_urls(self) -> "SyncGovernanceRequest":
        """Reject embedded credentials, enforce https, and SSRF-validate agent urls.

        Three construction-time gates, uniform across every transport (MCP/A2A/REST
        all build this type), each raising a field-located error at
        ``accounts[i].governance_agents[j].url`` (#1682 review H2):

        1. **no userinfo** (checked FIRST) — ``https://user:pass@host/`` embeds a
           credential in the url, which SSRF hostname checks skip (they read only the
           host) and which would otherwise be persisted verbatim and echoed on the
           wire. Ordering this gate before the https/SSRF gates guarantees a
           credential-bearing url never reaches a gate whose message renders the url;
           the https message additionally strips userinfo as defense in depth (#1682
           review B).
        2. **https** — the pinned 3.1.1 request schema marks the agent ``url``
           ``pattern: ^https://``, but the generated ``AnyUrl`` field does not carry
           that constraint (SDK codegen gap), so an ``http://`` url would slip
           through. The spec is authoritative; enforce it here.
        3. **SSRF** — the persisted url is a future ``check_governance`` target;
           reject private/internal/loopback/metadata hosts at bind time so a
           poisoned binding is never stored (#1329). ``resolve_dns=False``
           mirrors the webhook-registration convention (#1697): literal-IP + blocked
           -hostname checks apply, but fixture hostnames are not NXDOMAIN-rejected;
           a use-time DNS pin belongs with ``check_governance``.
        """
        for a_idx, account in enumerate(self.accounts):
            for g_idx, agent in enumerate(account.governance_agents):
                url = agent.url
                url_str = str(url)
                loc = ("accounts", a_idx, "governance_agents", g_idx, "url")
                # 1. userinfo FIRST — a credential-bearing url must be rejected before any
                #    gate below renders it (operands checked separately so username-only and
                #    password-only credentials are both rejected — #1682 review B/D2).
                if getattr(url, "username", None) or getattr(url, "password", None):
                    _raise_governance_url_error(
                        loc, "governance agent url must not embed userinfo credentials", url_str
                    )
                # 2. https — render a userinfo-stripped url (belt-and-suspenders; gate 1
                #    already rejected any userinfo-bearing url).
                if not url_str.startswith("https://"):
                    _raise_governance_url_error(
                        loc, f"governance agent url must use https:// (got '{strip_url_userinfo(url_str)}')", url_str
                    )
                # 3. SSRF — reason names the host class, never the url userinfo.
                ok, reason = check_url_ssrf(url_str, require_https=True, resolve_dns=False)
                if not ok:
                    _raise_governance_url_error(
                        loc, f"governance agent url targets a disallowed host: {reason}", url_str
                    )
        return self


class SyncedGovernanceAgent(SalesAgentBaseModel):
    """A governance agent as echoed on the sync_governance response.

    URL-only by construction. The request-side agent carries ``authentication``
    (schemes + credentials); the response MUST NOT echo credentials
    (sync-governance-response.json success ``governance_agents.items`` requires
    only ``url``). Modelling the echo with a url-only type makes that a
    structural guarantee, not a call-site discipline.

    Deliberately a parallel, minimal SDK-decoupled type rather than reusing the
    library ``GovernanceAgent`` (which also models the response as url-only): the
    echo contract is a bare ``{url}`` and owning it keeps the response shape
    independent of SDK codegen churn on the request-side agent type.
    """

    url: str


class SyncGovernanceResponseAccount(SalesAgentBaseModel):
    """Per-account result in a sync_governance response.

    The SDK collapsed the response ``oneOf`` into a flat envelope with a bare
    ``payload`` dict (no typed ``accounts``), so — mirroring SyncResponseAccount
    — we own this model. Shape from the pinned 3.1.1 success variant
    (sync-governance-response.json ``accounts.items``): ``account`` echoed,
    ``status`` in {synced, failed}, ``governance_agents`` present on synced
    entries (url only), per-account ``errors`` present on failed entries.
    """

    account: LibraryAccountReference
    # Two-member enum per the pinned sync-governance-response.json (status.enum
    # ["synced","failed"]); a Literal makes the constraint structural rather than
    # call-site discipline (mirrors the SyncedGovernanceAgent url-only rationale).
    status: Literal["synced", "failed"]
    governance_agents: list[SyncedGovernanceAgent] | None = None
    errors: list[LibraryError] | None = None


class SyncGovernanceResponse(NestedModelSerializerMixin, LibrarySyncGovernanceResponse):
    """Extends library SyncGovernanceResponse (success variant).

    The library type is the flattened protocol envelope; ``accounts`` is
    re-declared locally (Pattern #4 nested serialization) and is REQUIRED on
    the success variant (sync-governance-response.json ``oneOf`` requires
    ``accounts`` on success | ``errors`` on error). ``status`` defaults to
    ``completed`` on the library base — the synchronous success path — so it is
    not set here. ``context`` (inherited from the protocol envelope) is echoed
    unchanged, which the specialism storyboards grade.
    """

    model_config = ConfigDict(extra=get_pydantic_extra_mode())

    accounts: list[SyncGovernanceResponseAccount]

    def __str__(self) -> str:
        """Return human-readable summary message for protocol envelope."""
        synced = sum(1 for a in self.accounts if a.status == "synced")
        total = len(self.accounts)
        return f"Synced governance for {synced}/{total} account{'s' if total != 1 else ''}."


__all__ = [
    "Account",
    "ListAccountsRequest",
    "ListAccountsResponse",
    "SyncAccountsRequest",
    "SyncAccountsResponse",
    "SyncedGovernanceAgent",
    "SyncGovernanceRequest",
    "SyncGovernanceResponse",
    "SyncGovernanceResponseAccount",
    "SyncResponseAccount",
]
