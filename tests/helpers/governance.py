"""Governance-specific request-shape helpers for sync_governance (UC-030 / #1329) tests.

Home for the governance REQUEST shape the integration tests
(``tests/integration/test_sync_governance.py``) and the BDD steps
(``tests/bdd/steps/domain/uc030_governance.py``) both build: the url + Bearer-credentials
constants, the url-comparison helper, and the request-side governance-agent dict.

Account SEEDING is deliberately NOT here — it goes through the canonical
``tests.helpers.accounts.seed_account_with_access`` (the single seeder shared with every
other suite), never a governance-local twin (#1682 review item 3). The helpers below are
pure builders with no session dependency.
"""

from __future__ import annotations

from typing import Any

# Shared request-shape constants for the sync_governance test suites (#1682 review item 4):
# one governance-agent url + Bearer credentials (>= the schema's minLength 32). Kept here so
# the unit / integration / BDD suites assert against one source of truth for the pinned
# 3.1.1 request shape rather than re-declaring these per file.
GOV_URL = "https://governance.pinnacle-media.com"
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
