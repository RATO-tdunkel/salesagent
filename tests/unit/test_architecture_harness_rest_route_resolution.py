"""Guard: every harness env's (REST_METHOD, REST_ENDPOINT) resolves to a live APIRoute.

An ``IntegrationEnv`` that declares ``REST_ENDPOINT`` is dispatched on two REST legs —
the in-process ``RestDispatcher`` (``_run_rest_request``) and the in-network
``RestE2EDispatcher``. Both read ``REST_METHOD`` (default ``post``). If the declared
``(verb, path)`` does not resolve to a FastAPI ``APIRoute``, the request falls through
to the catch-all Flask mount at ``/`` and returns a Werkzeug HTML 404 — invisible to
``make quality`` (unit + offline) and to the in-process leg when only ONE leg was
overridden.

That is exactly the ``CapabilitiesEnv`` regression (#1682 review A): it overrode only
the in-process ``_run_rest_request`` to GET, so the in-network leg POSTed to the
GET-only ``/api/v1/capabilities`` and 404'd on the live route — a red in-network CI job
that no unit/in-process check could see. This guard performs that check structurally:
it fails the moment a harness env points at a ``(verb, path)`` with no matching
``APIRoute``.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from fastapi.routing import APIRoute

from src.routes.api_v1 import router as api_v1_router
from tests.harness._base import BaseTestEnv

_HARNESS_DIR = Path(__file__).resolve().parents[1] / "harness"

# Pre-existing dead literals: a declared REST_ENDPOINT whose tool has no REST route.
# get_media_buys is not exposed over REST — no /api/v1/media-buys/query route exists —
# so MediaBuyListEnv's REST leg has never resolved. This predates #1329 and is separate
# from it. Allowlist only shrinks: resolve by adding the route or dropping the env's
# REST_ENDPOINT, then delete the entry.
_KNOWN_UNRESOLVED: frozenset[tuple[str, str]] = frozenset(
    {
        ("post", "/api/v1/media-buys/query"),  # FIXME(#1682): get_media_buys has no REST route
    }
)


def _import_all_harness_modules() -> None:
    """Import every harness module so all env subclasses are registered."""
    for py_file in sorted(_HARNESS_DIR.glob("*.py")):
        if py_file.name.startswith("__"):
            continue
        importlib.import_module(f"tests.harness.{py_file.stem}")


def _all_env_subclasses() -> set[type]:
    """Every (transitive) BaseTestEnv subclass currently imported."""
    seen: set[type] = set()
    stack = list(BaseTestEnv.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
    return seen


def _live_routes() -> set[tuple[str, str]]:
    """(method-lower, path) for every APIRoute reachable via the api_v1 router."""
    routes: set[tuple[str, str]] = set()
    for route in api_v1_router.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                routes.add((method.lower(), route.path))
    return routes


def _declared_rest_dispatches() -> set[tuple[str, str, str]]:
    """(env_name, method-lower, endpoint) for every env declaring a non-empty REST_ENDPOINT.

    A property-based ``REST_METHOD`` (evaluated per-request, e.g. the media-buy dual
    env) reads back as a non-str on the class, so its endpoint is checked at the base
    default verb (``post``) — the verb its non-update path uses.
    """
    _import_all_harness_modules()
    dispatches: set[tuple[str, str, str]] = set()
    for cls in _all_env_subclasses():
        endpoint = getattr(cls, "REST_ENDPOINT", "")
        if not isinstance(endpoint, str) or not endpoint:
            continue
        method_attr = getattr(cls, "REST_METHOD", "post")
        method = method_attr if isinstance(method_attr, str) else "post"
        dispatches.add((cls.__name__, method.lower(), endpoint))
    return dispatches


def test_every_harness_rest_endpoint_resolves_to_live_route():
    """No harness env may declare a REST dispatch that resolves to no live APIRoute."""
    live = _live_routes()
    unresolved = {
        (name, method, endpoint)
        for name, method, endpoint in _declared_rest_dispatches()
        if (method, endpoint) not in live and (method, endpoint) not in _KNOWN_UNRESOLVED
    }
    assert unresolved == set(), (
        f"Harness env(s) declare a REST dispatch that resolves to no live APIRoute: {sorted(unresolved)}. "
        f"A (verb, path) with no matching route falls through to the Flask mount and 404s on the in-network "
        f"leg (#1682 review A). Fix the env's REST_METHOD/REST_ENDPOINT or add the route. Do NOT add to the "
        f"allowlist."
    )


def test_known_unresolved_not_stale():
    """Allowlist only shrinks — an entry that now resolves (or is no longer declared) must be removed."""
    live = _live_routes()
    declared = {(method, endpoint) for _name, method, endpoint in _declared_rest_dispatches()}
    stale = {pair for pair in _KNOWN_UNRESOLVED if pair in live or pair not in declared}
    assert stale == set(), (
        f"Stale _KNOWN_UNRESOLVED entr(ies) — now resolvable or no longer declared, remove them: {sorted(stale)}."
    )


def test_guard_detects_verb_path_mismatch():
    """Meta: the exact blocker-A shape (POST to the GET-only capabilities route) does NOT resolve."""
    live = _live_routes()
    assert ("get", "/api/v1/capabilities") in live, "capabilities GET route missing — app route table changed"
    assert ("post", "/api/v1/capabilities") not in live, "capabilities must be GET-only; a POST match hides the gap"
