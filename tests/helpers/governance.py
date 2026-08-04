"""Governance test helpers for sync_governance (UC-030 / #1329).

One builder / reader per production boundary the unit, integration, and BDD suites touch, so
the governance test contract is expressed once rather than N times (#1682 review item 2):

- ``account_entry`` — the pinned 3.1.1 request element ``{"account": <ref>, "governance_agents": [...]}``.
- ``governance_agent_dict`` — one request-side agent (url + write-only authentication).
- ``persisted_governance_urls`` — the below-wire persisted-binding read-back (session-safe).
- ``governance_binding_stub`` — a ``set_governance_binding`` side_effect mirroring the repo's
  PUBLIC url-only write contract (no coupling to the private projector).
- ``GOV_URL`` / ``DEFAULT_URL`` / ``BEARER_CREDS`` / ``url_eq`` — the shared request constants
  and the trailing-slash-tolerant url comparison.

Account SEEDING stays in ``tests.helpers.accounts.seed_account_with_access`` (the canonical
seeder shared with every suite), never a governance-local twin.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import AnyUrl

# Shared request-shape constants for the sync_governance test suites (#1682 review item 4):
# one governance-agent url + Bearer credentials (>= the schema's minLength 32). Kept here so
# the unit / integration / BDD suites assert against one source of truth for the pinned
# 3.1.1 request shape rather than re-declaring these per file.
GOV_URL = "https://governance.pinnacle-media.com"
# A generic well-formed url for scenarios whose defect-under-test is elsewhere (missing auth,
# malformed idempotency_key, unresolvable account) — one shared default across all suites.
DEFAULT_URL = "https://governance.example.com"
BEARER_CREDS = "x" * 64


def url_eq(actual: str | None, expected: str) -> bool:
    """Compare governance-agent urls tolerant of AnyUrl trailing-slash normalization.

    ``actual`` is null-guarded (``AnyUrl`` may serialize with a trailing ``/``; a missing
    echo surfaces as ``None``), so a dropped url fails rather than raising.
    """
    return (actual or "").rstrip("/") == expected.rstrip("/")


def governance_agent_dict(
    url: str,
    *,
    cred_len: int = 64,
    credentials: str | None = None,
    scheme: str = "Bearer",
) -> dict[str, Any]:
    """Build a request-side governance agent (``url`` + write-only ``authentication``).

    Credentials default to ``cred_len`` ``x``s (>= the schema's minLength 32) so the
    only thing under test is the account/authority path, not request validation.
    """
    creds = credentials if credentials is not None else "x" * cred_len
    return {"url": url, "authentication": {"schemes": [scheme], "credentials": creds}}


def account_entry(account_ref: dict[str, Any], *, agents: list[dict[str, Any]]) -> dict[str, Any]:
    """Build one sync_governance request element: the pinned 3.1.1 request wrapper.

    ``account_ref`` passes straight through — an id form (``{"account_id": ...}``) or a
    natural-key form (``{"brand": {...}, "operator": ..., "sandbox": ...}``). Single home for
    the request wrapper so the unit / integration / BDD suites stop re-encoding it per call
    site (#1682 review item 2).
    """
    return {"account": account_ref, "governance_agents": agents}


def persisted_governance_urls(tenant_id: str, account_id: str) -> list[str]:
    """Read the persisted governance-agent urls for an account, session-lifetime-safe.

    Opens a fresh ``AccountUoW`` on the same DB the dispatch committed to and extracts the url
    strings INSIDE the block — ORM attributes expire on commit, so reading them after the
    session closes raises ``DetachedInstanceError`` (the drift the BDD copy guarded and the
    integration / sync_accounts copies did not). Returns ``[]`` when the account row is absent
    or unbound, so callers grade against a plain list of persisted urls (#1682 review item 2).
    """
    from src.core.database.repositories.uow import AccountUoW

    with AccountUoW(tenant_id) as uow:
        account = uow.accounts.get_by_id(account_id)
        if account is None:
            return []
        return [str(a.url) for a in (account.governance_agents or [])]


def governance_binding_stub() -> Callable[[str, list[Any]], list[dict[str, str]]]:
    """A ``set_governance_binding`` side_effect mirroring the repo's PUBLIC write contract.

    Projects each request agent to the persisted url-only record — ``{"url": <url>}`` with
    credentials stripped and the url ``AnyUrl``-normalized (trailing slash), which is the
    documented return of ``AccountRepository.set_governance_binding``. Normalization goes through
    ``AnyUrl`` (the public coercion the url-only column applies), NOT the module-private
    ``_serialize_governance_agents`` projector, so the unit test grades the tool's echo against
    the repository's public contract rather than coupling to the internal the repo-owned design
    exists to hide (#1682 review item 2).
    """

    def _side_effect(account_id: str, agents: list[Any]) -> list[dict[str, str]]:
        return [
            {"url": str(AnyUrl(agent["url"] if isinstance(agent, dict) else agent.url))} for agent in (agents or [])
        ]

    return _side_effect
