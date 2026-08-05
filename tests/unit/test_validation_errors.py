"""Unit tests for validation error handling in create_media_buy."""

import pytest
from pydantic import BaseModel, ValidationError

from src.core.exceptions import AdCPValidationError
from src.core.schemas import CreateMediaBuyRequest
from src.core.validation_helpers import first_validation_error_field, format_validation_error


def test_first_validation_error_field_uses_bracket_notation():
    """first_validation_error_field renders list indices as [i] (bracket form).

    The boundary-derived field path must match the hand-rolled field= strings
    raised inside the _impl layer (e.g. packages[].budget), so the wire
    envelope's `field` attribute has one consistent shape regardless of where
    the validation error originated.
    """

    class _Pkg(BaseModel):
        budget: float

    class _Req(BaseModel):
        packages: list[_Pkg]

    with pytest.raises(ValidationError) as exc_info:
        _Req(packages=[{"budget": "not-a-number"}])

    assert first_validation_error_field(exc_info.value) == "packages[0].budget"


def test_first_validation_error_field_is_owned_by_exception_leaf_module():
    """The field-path helper must not recreate an exceptions/helpers import cycle."""
    assert first_validation_error_field.__module__ == "src.core.exceptions"


def test_create_media_buy_boundary_validation_preserves_field_suggestion():
    """Boundary request construction keeps the current field-specific hint."""
    from src.core.tools.media_buy_create import _build_create_media_buy_request

    with pytest.raises(AdCPValidationError) as exc_info:
        _build_create_media_buy_request(
            brand={"domain": "wiretest.example"},
            packages=None,
            start_time=None,
            end_time=None,
            po_number=None,
            reporting_webhook=None,
            context=None,
            ext=None,
            account=None,
            idempotency_key=None,
            paused=None,
        )

    error = exc_info.value
    assert error.field == "idempotency_key"
    assert error.suggestion == ("Provide the required 'idempotency_key' field and resend the request.")


def test_brand_target_audience_must_be_string():
    """Test Brand target_audience field accepts strings (adcp 3.12: Brand replaced BrandManifest)."""
    from adcp.types.generated_poc.brand import Brand, LocalizedName  # TODO: no stable alias in adcp.types

    brand = Brand(
        id="test_brand",
        names=[LocalizedName(name="Test Brand", language="en")],
        target_audience="spiritual seekers interested in unexplained phenomena",
    )
    assert brand.target_audience == "spiritual seekers interested in unexplained phenomena"


def test_brand_accepts_extra_fields():
    """Test that Brand accepts arbitrary extra fields (extra=allow)."""
    from adcp.types.generated_poc.brand import Brand, LocalizedName  # TODO: no stable alias in adcp.types

    brand = Brand(
        id="test_brand",
        names=[LocalizedName(name="Test Brand", language="en")],
        custom_field="custom_value",
    )
    # Brand accepts extra fields with extra="allow"
    assert brand is not None


def test_create_media_buy_request_invalid_brand_manifest():
    """Test that CreateMediaBuyRequest accepts brand field (adcp 3.6.0: brand replaced brand_manifest)."""
    # In adcp 3.6.0, brand is a BrandReference with optional domain field
    # Missing domain does not raise an error since domain is optional
    req = CreateMediaBuyRequest(
        brand={"domain": "testbrand.com"},
        end_time="2026-02-01T00:00:00Z",
        start_time="2026-01-01T00:00:00Z",
        idempotency_key="unit-test-key-invalid-brand-mfst",
    )
    assert req.brand is not None


def test_validation_error_formatting():
    """Test that our validation error formatting provides helpful messages."""
    # Test the format_validation_error helper function
    try:
        raise ValidationError.from_exception_data(
            "CreateMediaBuyRequest",
            [
                {
                    "type": "string_type",
                    "loc": ("brand_manifest", "BrandManifest", "target_audience"),
                    "msg": "Input should be a valid string",
                    "input": {"demographics": ["test"], "interests": ["test"]},
                }
            ],
        )
    except ValidationError as e:
        # Use the shared helper function
        error_msg = format_validation_error(e, context="test request")

        # Check that we got a helpful error message
        assert "Invalid test request:" in error_msg
        assert "brand_manifest.BrandManifest.target_audience" in error_msg
        assert "Expected string, got object" in error_msg
        assert "AdCP spec requires this field to be a simple string" in error_msg
        assert "https://adcontextprotocol.org/schemas/v1/" in error_msg


def test_validation_error_formatting_missing_field():
    """Test formatting for missing required fields."""
    try:
        raise ValidationError.from_exception_data(
            "CreateMediaBuyRequest",
            [{"type": "missing", "loc": ("brand",), "msg": "Field required", "input": {}}],
        )
    except ValidationError as e:
        error_msg = format_validation_error(e)

        assert "brand: Required field is missing" in error_msg
        assert "Invalid request:" in error_msg


def test_validation_error_formatting_extra_field_redacts_innocuous_scalar():
    """Even an innocuous scalar extra field is redacted — the echo is redact-ALL.

    The value is withheld for EVERY extra_forbidden rejection, not only
    credential-shaped ones: a deny-list cannot enumerate buyer-invented names, so the
    only safe policy is to never echo (#1682 review C). The actionable field PATH
    always survives.
    """
    try:
        raise ValidationError.from_exception_data(
            "CreateMediaBuyRequest",
            [
                {
                    "type": "extra_forbidden",
                    "loc": ("unknown_field",),
                    "msg": "Extra inputs are not permitted",
                    "input": "some_value",
                }
            ],
        )
    except ValidationError as e:
        error_msg = format_validation_error(e)

        assert "unknown_field: Extra field not allowed by AdCP spec" in error_msg
        # The value is NEVER echoed — even an innocuous one.
        assert "some_value" not in error_msg
        assert "Received value: [redacted]" in error_msg


def test_validation_error_formatting_extra_field_with_dict_redacted():
    """An extra field with a dict value is redacted too — the structure could nest a secret.

    A value scan cannot prove a dict is credential-free (a list-of-pairs or a
    buyer-invented key escapes it), so the whole value is withheld (#1682 review C).
    """
    try:
        raise ValidationError.from_exception_data(
            "Package",
            [
                {
                    "type": "extra_forbidden",
                    "loc": ("format_ids", "agent_url"),
                    "msg": "Extra inputs are not permitted",
                    "input": {"agent_url": "https://creative.adcontextprotocol.org/", "id": "display_300x250"},
                }
            ],
        )
    except ValidationError as e:
        error_msg = format_validation_error(e)

        assert "format_ids.agent_url: Extra field not allowed by AdCP spec" in error_msg
        assert "Received value: [redacted]" in error_msg
        # The value (even innocuous) is withheld — not echoed.
        assert "https://creative.adcontextprotocol.org/" not in error_msg
        assert "display_300x250" not in error_msg


def test_validation_error_redacts_declared_field_name_misplaced():
    """A DECLARED field name misplaced as an extra (e.g. keywords) is redacted uniformly.

    Redact-all makes the former deny-list's over-match moot: declared field names that
    happened to contain a fragment (``keywords`` -> ``key``, ``idempotency_key``) no
    longer get special treatment — every extra_forbidden value is withheld the same way
    (#1682 review C).
    """
    try:
        raise ValidationError.from_exception_data(
            "SyncAccountsRequest",
            [
                {
                    "type": "extra_forbidden",
                    "loc": ("keywords",),
                    "msg": "Extra inputs are not permitted",
                    "input": ["news", "sports"],
                }
            ],
        )
    except ValidationError as e:
        error_msg = format_validation_error(e)

        assert "keywords: Extra field not allowed by AdCP spec" in error_msg
        assert "Received value: [redacted]" in error_msg
        assert "news" not in error_msg and "sports" not in error_msg


def test_validation_error_redacts_credential_under_authentication():
    """A typo'd extra field under authentication must NOT echo the secret value.

    format_validation_error feeds errors[0].message, which reaches the buyer wire
    (REST/A2A) and the error-log + audit sinks. A buyer typo (`credential` for
    `credentials`) still carries the bearer token; redact-all withholds the value while
    the actionable field PATH is preserved (#1682 review C, was BLOCKER A).
    """
    secret = "SUPERSECRETcredential00000000000000"
    try:
        raise ValidationError.from_exception_data(
            "SyncGovernanceRequest",
            [
                {
                    "type": "extra_forbidden",
                    "loc": ("accounts", 0, "governance_agents", 0, "authentication", "credential"),
                    "msg": "Extra inputs are not permitted",
                    "input": secret,
                }
            ],
        )
    except ValidationError as e:
        error_msg = format_validation_error(e)

        assert secret not in error_msg, f"credential leaked into validation message: {error_msg!r}"
        assert "[redacted]" in error_msg
        # The field path is still actionable.
        assert "authentication.credential: Extra field not allowed by AdCP spec" in error_msg


def test_validation_error_redacts_nested_secret_under_unknown_field():
    """An unknown top-level field carrying a nested credential must be redacted.

    The offending loc segment is innocuous and the value nests a sensitive key several
    levels deep — redact-all withholds it without needing to detect the nesting
    (#1682 review C).
    """
    secret = "NESTEDbearerSECRET00000000000000000"
    try:
        raise ValidationError.from_exception_data(
            "SyncGovernanceRequest",
            [
                {
                    "type": "extra_forbidden",
                    "loc": ("extra_config",),
                    "msg": "Extra inputs are not permitted",
                    "input": {"authentication": {"credentials": secret}},
                }
            ],
        )
    except ValidationError as e:
        error_msg = format_validation_error(e)

        assert secret not in error_msg, f"nested credential leaked: {error_msg!r}"
        assert "[redacted]" in error_msg


@pytest.mark.parametrize(
    "field_name",
    [
        "api_key",
        "access_token",
        "client_secret",
        "auth_token",
        "private_key",
        "bearer_token",
        "secret_key",
        "refresh_token",
        "session_token",
        "password",
    ],
)
def test_validation_error_redacts_credential_shaped_sibling_of_url(field_name):
    """Realistic credential field names as a SCALAR SIBLING of ``url`` are redacted.

    Names like api_key/access_token/client_secret/private_key placed NOT under
    ``authentication`` but as a plain scalar sibling would defeat any nested-key scan;
    redact-all withholds them regardless. A deny-list could never enumerate this open
    set, which is why the policy redacts every value (#1682 review C).
    """
    secret = "sk-live-" + "z" * 40
    try:
        raise ValidationError.from_exception_data(
            "SyncGovernanceRequest",
            [
                {
                    "type": "extra_forbidden",
                    "loc": ("accounts", 0, "governance_agents", 0, field_name),
                    "msg": "Extra inputs are not permitted",
                    "input": secret,
                }
            ],
        )
    except ValidationError as e:
        error_msg = format_validation_error(e)

        assert secret not in error_msg, f"{field_name} value must be withheld from the echo"
        assert "[redacted]" in error_msg
        # The field path stays actionable.
        assert f"{field_name}: Extra field not allowed by AdCP spec" in error_msg
