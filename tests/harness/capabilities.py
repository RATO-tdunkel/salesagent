"""CapabilitiesEnv — cross-transport wire env for get_adcp_capabilities (UC-010).

Runs the real ``_get_adcp_capabilities_impl`` and its MCP/A2A wrappers so a test
can assert the ``account`` capability section (sandbox flag + billing models) on
the ACTUAL serialized wire — not just the typed ``model_dump`` — across transports.

get_adcp_capabilities is an auth-optional discovery endpoint; the account section
is built from ``identity.tenant`` (``_build_account_capability``), so the env only
needs a resolvable tenant/identity (the base ``setup_default_data`` provides one).

REST is a GET discovery endpoint (``/api/v1/capabilities``) rather than the POST
convention the base harness dispatch assumes, so ``_run_rest_request`` below overrides
the base to GET instead — this makes ``call_via(Transport.REST)`` (and thus the BDD
``dispatch_request`` REST leg) work directly, same as MCP/A2A through the standard
harness hooks (which stash the real success-path wire).

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

    def _run_rest_request(self, endpoint: str, **kwargs: Any) -> Any:
        """GET the capabilities discovery endpoint (the base dispatch assumes POST).

        ``get_adcp_capabilities`` is a GET at ``/api/v1/capabilities`` (api_v1.py), not the
        POST the base ``_run_rest_request`` issues, so ``call_via(Transport.REST)`` — and thus
        the BDD ``dispatch_request`` REST leg — must GET here. The shared preamble
        (``_prepare_rest_request``) still pops identity, commits factory rows, and installs the
        auth override, so the RestDispatcher captures the real success-path wire from
        ``response.json()`` exactly as it does for POST endpoints (#1682 review item 1).
        """
        client, _identity = self._prepare_rest_request(kwargs)
        return client.get(endpoint)
