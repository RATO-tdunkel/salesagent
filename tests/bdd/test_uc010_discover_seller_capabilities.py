"""BDD scenario binding for UC-010: the account/sandbox honesty capability.

Binds the compiled BR-UC-010 feature via pytest-bdd's ``scenarios()`` (the whole-feature
binding the CI shard manifest requires — ``scripts/ci/shard_split.py`` counts scenarios off
this call). Only the ``@T-UC-010-v31-account-sandbox`` account/sandbox-honesty grader is
actually WIRED: the conftest UC-010 branch routes that one outline to CapabilitiesEnv and
xfails every other BR-UC-010 scenario (full capability discovery, signing posture,
idempotency-ttl, version-unsupported, ...), which have no step definitions. They are
therefore collected-but-xfailed — never run — so this PR does not un-dormant the feature or
its pre-existing account-on-no-tenant gap. Mirrors the UC-030 bind-all + xfail-out-of-scope
pattern (``test_uc030_manage_governance.py``).

The wired outline executes against CapabilitiesEnv across a2a/mcp/rest. Step definitions come
from ``tests.bdd.steps.domain.uc010_capabilities`` (+ the shared generic Givens).

#1329 (UC-010) / #1682 review item 1.
"""

from __future__ import annotations

from pytest_bdd import scenarios

scenarios("features/BR-UC-010-discover-seller-capabilities.feature")
