# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Payment provider abstraction: factory selection and the mock's behaviour."""

from decimal import Decimal

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.payments.providers import (
    MockProvider,
    PaymentProvider,
    PaymentProviderError,
    WebhookType,
    get_payment_provider,
)


def test_factory_returns_mock_by_default(settings):
    settings.PAYMENT_PROVIDER = "mock"
    assert isinstance(get_payment_provider(), MockProvider)


@pytest.mark.parametrize("name", ["yookassa", "stripe", ""])
def test_factory_rejects_unimplemented_provider(settings, name):
    settings.PAYMENT_PROVIDER = name
    with pytest.raises(ImproperlyConfigured):
        get_payment_provider()


def test_create_hold_mints_provider_id_without_confirmation_url():
    provider = MockProvider(secret="s")

    first = provider.create_hold(amount=Decimal("1500"), booking_id=1, idempotency_key="k1")
    second = provider.create_hold(amount=Decimal("1500"), booking_id=1, idempotency_key="k2")

    assert first.provider_id and first.provider_id != second.provider_id
    assert first.confirmation_url is None


def test_capture_and_refund_echo_the_requested_amount():
    provider = MockProvider(secret="s")

    capture = provider.capture(provider_id="mock-1", amount=Decimal("900"), idempotency_key="k")
    refund = provider.refund(provider_id="mock-1", amount=Decimal("600"), idempotency_key="k")

    assert capture.captured_amount == Decimal("900")
    assert refund.refunded_amount == Decimal("600")
    assert provider.release(provider_id="mock-1", idempotency_key="k") is None


def test_signed_webhook_round_trips_through_verify_and_parse():
    provider = MockProvider(secret="top-secret")

    body, signature = provider.build_webhook(
        event_id="evt-1",
        type=WebhookType.HOLD_SUCCEEDED,
        provider_id="mock-1",
        amount=Decimal("1500.50"),
    )

    assert provider.verify_signature(body=body, signature=signature)
    event = provider.parse_webhook(body)
    assert event.event_id == "evt-1"
    assert event.type == WebhookType.HOLD_SUCCEEDED
    assert event.provider_id == "mock-1"
    # Amount survives the JSON round-trip as an exact Decimal, not a float.
    assert event.amount == Decimal("1500.50")


def test_verify_signature_rejects_tampered_body():
    provider = MockProvider(secret="top-secret")
    body, signature = provider.build_webhook(
        event_id="evt-1", type=WebhookType.HOLD_SUCCEEDED, provider_id="mock-1"
    )

    assert not provider.verify_signature(body=body + b"x", signature=signature)
    assert not provider.verify_signature(body=body, signature="deadbeef")


def test_verify_signature_rejects_other_secret():
    signer = MockProvider(secret="secret-a")
    verifier = MockProvider(secret="secret-b")
    body, signature = signer.build_webhook(
        event_id="evt-1", type=WebhookType.HOLD_SUCCEEDED, provider_id="mock-1"
    )

    assert not verifier.verify_signature(body=body, signature=signature)


def test_webhook_without_amount_parses_to_none():
    provider = MockProvider(secret="s")
    body, _ = provider.build_webhook(
        event_id="evt-2", type=WebhookType.HOLD_FAILED, provider_id="mock-2"
    )

    assert provider.parse_webhook(body).amount is None


def test_verify_signature_rejects_empty_signature():
    provider = MockProvider(secret="s")
    body, _ = provider.build_webhook(
        event_id="evt-1", type=WebhookType.HOLD_SUCCEEDED, provider_id="mock-1"
    )

    assert not provider.verify_signature(body=body, signature="")


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"event_id": "x"}',
        b"[]",
        b"42",
        # Structurally valid, but a non-numeric amount must not escape as a raw
        # decimal.InvalidOperation — the money field is the one most worth guarding.
        b'{"event_id": "e", "type": "hold_succeeded", "provider_id": "p", "amount": "abc"}',
    ],
)
def test_parse_webhook_wraps_malformed_body(body):
    provider = MockProvider(secret="s")
    with pytest.raises(PaymentProviderError):
        provider.parse_webhook(body)


def test_parse_webhook_keeps_unrecognized_type():
    # A PSP sends event types we do not model; parsing must not choke — the type
    # is carried through verbatim and simply ignored by the dispatcher downstream.
    provider = MockProvider(secret="s")
    body, _ = provider.build_webhook(
        event_id="evt-9", type="some_future_event", provider_id="mock-9"
    )

    assert provider.parse_webhook(body).type == "some_future_event"


def test_mock_uses_settings_secret_when_none_given():
    # No explicit secret → both instances fall back to settings.PAYMENT_WEBHOOK_SECRET.
    signer = MockProvider()
    verifier = MockProvider()
    body, signature = signer.build_webhook(
        event_id="evt-1", type=WebhookType.HOLD_SUCCEEDED, provider_id="mock-1"
    )

    assert verifier.verify_signature(body=body, signature=signature)


def test_payment_provider_is_abstract():
    with pytest.raises(TypeError):
        PaymentProvider()


def test_mock_hold_is_stateless_across_calls():
    # The mock does not dedupe by idempotency_key (it forwards to a real PSP that
    # would); protection against double-processing lives in the domain layer, so
    # the same key here still mints distinct holds. This pins that contract.
    provider = MockProvider(secret="s")
    first = provider.create_hold(amount=Decimal("100"), booking_id=1, idempotency_key="same")
    second = provider.create_hold(amount=Decimal("100"), booking_id=1, idempotency_key="same")

    assert first.provider_id != second.provider_id
