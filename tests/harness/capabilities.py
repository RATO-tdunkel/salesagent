"""CapabilitiesEnv — cross-transport wire env for get_adcp_capabilities (UC-010).

Runs the real ``_get_adcp_capabilities_impl`` and its MCP/A2A wrappers so a test
can assert the ``account`` capability section (sandbox flag + billing models) on
the ACTUAL serialized wire — not just the typed ``model_dump`` — across transports.

get_adcp_capabilities is an auth-optional discovery endpoint; the account section
is built from ``identity.tenant`` (``_build_account_capability``), so the env only
needs a resolvable tenant/identity (the base ``setup_default_data`` provides one).

REST is a GET discovery endpoint (``/api/v1/capabilities``) rather than the POST
convention the base harness assumes, so this env declares ``REST_METHOD = "get"``.
Both REST legs read that single attribute — the in-process ``_run_rest_request`` and
the in-network ``RestE2EDispatcher`` — so ``call_via(Transport.REST)`` (and thus the
BDD ``dispatch_request`` REST leg) GETs the discovery endpoint on every transport,
same as MCP/A2A through the standard harness hooks (which stash the real success-path
wire). An earlier version overrode only the in-process ``_run_rest_request``, leaving
the in-network leg POSTing to a GET-only route → live-route 404 (#1682 review A).

#1329 (UC-010 account/sandbox honesty)
"""

from __future__ import annotations

from typing import Any

from adcp.types import GetAdcpCapabilitiesResponse

from tests.harness._base import IntegrationEnv

CAPABILITIES_REST_ENDPOINT = "/api/v1/capabilities"


class CapabilitiesEnv(IntegrationEnv):
    """Integration env for get_adcp_capabilities across impl/mcp/a2a (+ REST GET in-test)."""

    EXTERNAL_PATCHES: dict[str, str] = {}
    REST_ENDPOINT = CAPABILITIES_REST_ENDPOINT
    REST_METHOD = "get"  # GET discovery endpoint; both REST legs read this (#1682 review A)

    def call_impl(self, **kwargs: Any) -> GetAdcpCapabilitiesResponse:
        from src.core.tools.capabilities import _get_adcp_capabilities_impl

        self._commit_factory_data()
        kwargs.setdefault("identity", self.identity)
        return _get_adcp_capabilities_impl(**kwargs)

    def call_mcp(self, **kwargs: Any) -> GetAdcpCapabilitiesResponse:
        return self._run_mcp_client("get_adcp_capabilities", GetAdcpCapabilitiesResponse, **kwargs)

    def call_a2a(self, **kwargs: Any) -> GetAdcpCapabilitiesResponse:
        return self._run_a2a_handler("get_adcp_capabilities", GetAdcpCapabilitiesResponse, **kwargs)

    def parse_rest_response(self, data: dict[str, Any]) -> GetAdcpCapabilitiesResponse:
        return GetAdcpCapabilitiesResponse(**data)
