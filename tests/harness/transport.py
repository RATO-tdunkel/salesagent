"""Transport enum and TransportResult for multi-transport behavioral tests.

Defines the seven dispatch transports (IMPL, A2A, REST, MCP + E2E variants)
and a frozen result container that separates transport-specific envelope from
shared payload.

Usage::

    result = env.call_via(Transport.REST, creatives=[...])
    assert result.is_success
    assert result.payload.creatives[0].action == CreativeAction.created
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


def _pinned_error_metadata() -> dict[str, dict[str, str]]:
    """code -> {recovery, suggestion} — delegates to the PUBLIC ``pinned_error_metadata``.

    The private module-local name is retained for the in-module methods and the existing
    ``from tests.harness.transport import _pinned_error_metadata`` call sites; the loader and
    its per-field source contract now live once in ``tests.helpers.error_metadata`` (#1329).
    """
    from tests.helpers.error_metadata import pinned_error_metadata

    return pinned_error_metadata()


def extract_wire_suggestion(envelope: dict | None) -> str | None:
    """The buyer-facing ``suggestion`` from a two-layer AdCP wire error envelope.

    STRICT error.json conformance: ``suggestion`` is a top-level sibling of
    code/message/field/retry_after/recovery on the error object (in either the
    ``errors[0]`` or the envelope-level ``adcp_error`` layer). A suggestion
    buried in the free-form ``details`` dict is NOT at the protocol position
    and deliberately does not satisfy this lookup — emitters that bury it are
    conformance bugs the harness must surface, not mask (#1417).
    Single source of truth for both ``TransportResult.assert_wire_error`` and
    the BDD ``_wire_suggestion`` step (#1417). Returns ``None`` when
    there is no envelope (IMPL / no-wire).
    """
    if not envelope:
        return None
    errors = envelope.get("errors") or [{}]
    adcp_error = envelope.get("adcp_error") or {}
    return errors[0].get("suggestion") or adcp_error.get("suggestion")


class Transport(StrEnum):
    """Dispatch transports for behavioral tests."""

    IMPL = "impl"  # Direct _impl() call
    A2A = "a2a"  # _raw() A2A wrapper
    REST = "rest"  # FastAPI TestClient → route → _raw() → _impl()
    MCP = "mcp"  # Mock Context → MCP wrapper → _impl()
    E2E_REST = "e2e_rest"  # Real HTTP via httpx → nginx → server
    E2E_MCP = "e2e_mcp"  # Real MCP via httpx → nginx → server (placeholder)
    E2E_A2A = "e2e_a2a"  # Real A2A via httpx → nginx → server (placeholder)


# Maps Transport → ResolvedIdentity.protocol value
TRANSPORT_PROTOCOL: dict[Transport, str] = {
    Transport.IMPL: "mcp",  # _impl doesn't inspect protocol; keep default
    Transport.A2A: "a2a",
    Transport.REST: "rest",
    Transport.MCP: "mcp",
    Transport.E2E_REST: "rest",
    Transport.E2E_MCP: "mcp",
    Transport.E2E_A2A: "a2a",
}


@dataclass(frozen=True)
class E2EConfig:
    """Configuration for E2E transport dispatch.

    Attributes:
        base_url: Docker stack URL (e.g., ``http://localhost:8092``).
        postgres_url: Docker PostgreSQL URL for factory data writes.
    """

    base_url: str
    postgres_url: str


@dataclass(frozen=True)
class TransportResult:
    """Normalized result from any transport dispatch.

    Attributes:
        payload: Pydantic response model (shared assertions target this).
        envelope: Transport-specific metadata (HTTP status, ToolResult, etc.).
        error: Exception raised during dispatch, if any.
        raw_response: Unprocessed transport response (httpx.Response, ToolResult, etc.).
        wire_response: Serialized success-path response body as a dict, captured
            from the real wire (REST HTTP JSON body, MCP structured_content, A2A
            artifact DataPart). ``None`` on error and on IMPL (no wire — serialize
            the typed ``payload`` instead). Lets success-path tests assert the
            actual serialized shape (e.g. the v3.1 format_id federation contract).
        wire_error_envelope: Raw two-layer error envelope dict captured from
            the actual wire bytes (REST HTTP body, MCP ToolError content text,
            A2A failed-Task artifact DataPart). ``None`` on success or on the
            IMPL transport, which has no wire. This is the canonical field
            for error verification — see ``tests/CLAUDE.md`` § Error
            Verification Policy.
        synthesized_error_envelope: Two-layer envelope produced by
            ``build_two_layer_error_envelope`` against the IMPL-caught
            ``AdCPError`` — what production WOULD emit at the boundary.
            ``None`` on success and on REST/MCP/A2A (those expose the real
            wire envelope above instead). Tests asserting on this field
            verify the envelope-builder contract, NOT the wire shape — a
            regression in the production boundary translator would not be
            caught here. Use REST/MCP/A2A for wire-shape regressions.
    """

    payload: BaseModel | None = None
    envelope: dict[str, Any] = field(default_factory=dict)
    error: Exception | None = None
    raw_response: Any = None
    wire_response: dict[str, Any] | None = None
    wire_error_envelope: dict[str, Any] | None = None
    synthesized_error_envelope: dict[str, Any] | None = None

    @property
    def is_success(self) -> bool:
        return self.error is None and self.payload is not None

    @property
    def is_error(self) -> bool:
        return self.error is not None

    def assert_wire_error(
        self,
        code: str,
        *,
        recovery: str | None = None,
        require_suggestion: bool = False,
        message_substr: str | None = None,
        field: str | None = None,
        field_substr: str | None = None,
        suggestion_substr: str | None = None,
    ) -> None:
        """Assert this result carries the AdCP two-layer wire error ``code``.

        Transport-independent: reads the normalized ``wire_error_envelope`` the
        dispatcher captured for whatever transport produced this result, so the
        same call holds on a2a/mcp/rest. Recovery defaults to the PINNED AdCP
        enum's classification for ``code`` (pin-wins), making the assertion
        non-vacuous without per-scenario duplication. This is the single
        harness-provided way to verify an error on the wire — step definitions
        must not hand-roll envelope parsing.

        ``message_substr`` / ``suggestion_substr`` pin the buyer-facing message and
        suggestion CONTENT (not merely their presence), so a transport-blind scenario
        can assert the SAME strings on every transport — a per-transport message/
        suggestion fork (e.g. MCP surfacing a different text than A2A/REST) then reddens
        instead of passing because ``field`` alone matched (#1329).
        """
        from tests.helpers import assert_envelope_shape

        meta = _pinned_error_metadata()
        spec = meta.get(code)
        assert spec is not None, (
            f"{code!r} is not a canonical AdCP error code (pinned error-code.json). "
            "Reconcile the feature to a canonical code."
        )
        expected_recovery = recovery if recovery is not None else spec["recovery"]

        envelope = self.wire_error_envelope
        assert envelope is not None, (
            f"Expected a wire rejection with {code}, but no wire_error_envelope was captured "
            f"(is_error={self.is_error}, payload={self.payload!r}). The operation either "
            "succeeded or errored before reaching a transport."
        )
        assert_envelope_shape(
            envelope,
            code,
            recovery=expected_recovery,
            message_substr=message_substr,
            field=field,
            field_substr=field_substr,
        )
        if require_suggestion or suggestion_substr is not None:
            suggestion = extract_wire_suggestion(envelope)
            assert suggestion, f"Expected a non-empty suggestion in the {code} wire envelope: {envelope}"
            if suggestion_substr is not None:
                assert suggestion_substr in suggestion, (
                    f"Expected suggestion to contain {suggestion_substr!r} in the {code} wire envelope, "
                    f"got {suggestion!r}"
                )

    def assert_wire_error_shape(self) -> None:
        """Assert a well-formed two-layer AdCP error envelope WITHOUT pinning the code.

        Code-agnostic structural grade: both layers present, their codes non-empty AND
        agreeing, and a recovery hint set. The SPECIFIC code is pinned separately (by
        ``assert_wire_error`` or a following ``the error code is "X"`` step), so this is the
        single home for "the envelope is a real two-layer error" that step definitions must
        not re-hand-roll by digging ``adcp_error.code``/``errors[0].code``/``recovery`` out
        of the dict themselves. A single-layer or code-less envelope ("flip the code to
        garbage and this stays green" no longer holds) fails here (#1329).
        """
        envelope = self.wire_error_envelope
        assert envelope is not None, (
            f"expected a two-layer wire error envelope, but none was captured "
            f"(is_error={self.is_error}, payload={self.payload!r})"
        )
        top = (envelope.get("adcp_error") or {}).get("code")
        leaf = (envelope.get("errors") or [{}])[0].get("code")
        assert top and leaf and top == leaf, f"malformed/disagreeing two-layer error codes: {envelope}"
        assert (envelope.get("errors") or [{}])[0].get("recovery"), f"error missing recovery hint: {envelope}"

    def assert_secret_absent(self, secret: str) -> None:
        """Assert ``secret`` reaches NEITHER the success wire body NOR the error envelope.

        Scans BOTH ``wire_response`` (success-path body) and ``wire_error_envelope`` (error
        envelope) — a credential must never be echoed on either the accept OR the reject
        path. Raises LOUDLY if NEITHER is populated: nothing was captured to scan, so a
        green here would be vacuous (the dispatch neither succeeded with a body nor errored
        with an envelope). Single home for the "credential absent on the wire" invariant so
        the BDD leak steps + integration redaction tests stop each re-implementing a
        ``secret not in str(envelope)`` scan (#1329).
        """
        haystacks: list[tuple[str, dict[str, Any]]] = []
        if self.wire_response is not None:
            haystacks.append(("wire_response", self.wire_response))
        if self.wire_error_envelope is not None:
            haystacks.append(("wire_error_envelope", self.wire_error_envelope))
        assert haystacks, (
            "assert_secret_absent captured no wire (neither wire_response nor wire_error_envelope "
            f"populated) — nothing to scan (is_error={self.is_error}, payload={self.payload!r})"
        )
        for name, body in haystacks:
            assert secret not in str(body), f"leaked secret reached the {name}: {body!r}"

    def assert_account_error(self, account_id: str, code: str, *, recovery: str | None = None) -> None:
        """Assert the per-account entry for ``account_id`` (SUCCESS envelope) carries ``code``.

        A per-account failure lives under ``accounts[]`` (``status=failed`` + a per-account
        ``errors[]``) of the partial-failure SUCCESS variant, NOT the top-level error
        envelope (spec oneOf: accounts XOR adcp_error). Finds the entry by its echoed ref
        (the ref-echo grade — raises if the requested id was not echoed), asserts it failed,
        and pins ``code`` + recovery on its ``errors[]``. Recovery defaults to the PINNED
        AdCP enum's classification for ``code`` (pin-wins) exactly as ``assert_wire_error``
        does (reuses ``_pinned_error_metadata``), so a per-account recovery drift reddens
        without a per-scenario literal (#1329). Single home for per-account wire
        reads so the ``then_per_account_*`` steps stop hand-rolling the accounts[] scan.
        """
        meta = _pinned_error_metadata()
        spec = meta.get(code)
        assert spec is not None, (
            f"{code!r} is not a canonical AdCP error code (pinned error-code.json). "
            "Reconcile the feature to a canonical code."
        )
        expected_recovery = recovery if recovery is not None else spec["recovery"]

        body = self.wire_response
        assert body is not None, (
            "assert_account_error needs the success-path wire (wire_response); none captured "
            f"(is_error={self.is_error}). A per-account failure is the SUCCESS variant, not a "
            "top-level error."
        )
        accounts = body.get("accounts") or []
        matched = [a for a in accounts if (a.get("account") or {}).get("account_id") == account_id]
        available = [(a.get("account") or {}).get("account_id") for a in accounts]
        assert matched, f"no wire account {account_id!r}; available: {available}"
        acct = matched[0]
        assert acct.get("status") == "failed", (
            f"account {account_id} expected per-account status 'failed', got {acct.get('status')!r}: {acct}"
        )
        errs = acct.get("errors") or []
        codes = {e.get("code") for e in errs}
        assert code in codes, f"account {account_id} per-account errors {codes} do not include {code!r}"
        recoveries = {e.get("recovery") for e in errs if e.get("code") == code}
        assert recoveries == {expected_recovery}, (
            f"account {account_id} {code} recovery {recoveries} must equal the pinned enum {expected_recovery!r}"
        )
