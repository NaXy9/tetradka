# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Payment provider abstraction: the hold → capture / release / refund lifecycle.

External payment services (YooKassa) live behind `PaymentProvider`; endpoints and
Celery tasks depend on this interface, never on a concrete PSP, and dev/tests run
against the mock.

Authority model — which signal flips the domain Payment status:
  * `create_hold` only *initiates* an authorization; it depends on the payer, so
    the hold becomes usable only when a `hold_succeeded` webhook confirms it, not
    when the call returns.
  * `capture`, `release` and `refund` are server-initiated — their return value is
    authoritative for the domain state change. The matching `*_succeeded` /
    `*_failed` webhooks are the PSP's asynchronous echo, used for reconciliation
    and as an idempotent safety net (e.g. a capture that settles then later fails).

Idempotency: every mutating call takes an `idempotency_key`, forwarded to the PSP
so a retried request is de-duplicated server-side. It does not make the stateless
mock dedupe — our own records are protected at the domain layer instead, by the
unique (provider, provider_id) constraint and the webhook handler's event_id dedup.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class PaymentProviderError(Exception):
    """Base class for provider-side failures (network, declined, illegal state)."""


class InvalidSignature(PaymentProviderError):
    """Raised by the webhook handler when a payload fails signature verification."""


class WebhookType(StrEnum):
    """Normalized webhook event types the domain reacts to, across all PSPs."""

    HOLD_SUCCEEDED = "hold_succeeded"  # authorization confirmed → Payment held
    HOLD_FAILED = "hold_failed"  # authorization declined → Payment failed
    CAPTURE_SUCCEEDED = "capture_succeeded"
    CAPTURE_FAILED = "capture_failed"  # capture accepted then failed to settle
    REFUND_SUCCEEDED = "refund_succeeded"
    REFUND_FAILED = "refund_failed"


@dataclass(frozen=True)
class HoldResult:
    """Outcome of initiating an authorization hold."""

    provider_id: str  # PSP payment id; persisted as Payment.provider_id
    confirmation_url: str | None = None  # where to redirect the payer (None for mock)


@dataclass(frozen=True)
class CaptureResult:
    provider_id: str
    captured_amount: Decimal


@dataclass(frozen=True)
class RefundResult:
    provider_id: str
    refunded_amount: Decimal


@dataclass(frozen=True)
class WebhookEvent:
    """A PSP webhook normalized to what the domain needs, provider-agnostic.

    `event_id` is the PSP's own event identifier and is the idempotency key for
    the webhook handler; `type` holds a `WebhookType` value (kept as a plain str
    so an unrecognized event parses cleanly and is simply ignored downstream).
    """

    event_id: str
    type: str
    provider_id: str
    amount: Decimal | None = None


class PaymentProvider(ABC):
    """Contract every payment backend implements."""

    @abstractmethod
    def create_hold(
        self, *, amount: Decimal, booking_id: int, idempotency_key: str, currency: str = "RUB"
    ) -> HoldResult:
        """Initiate an authorization hold for `amount`; not captured yet.

        The returned hold is pending until a `hold_succeeded` webhook confirms it.
        """

    @abstractmethod
    def capture(self, *, provider_id: str, amount: Decimal, idempotency_key: str) -> CaptureResult:
        """Capture `amount` of a confirmed hold; a partial capture releases the rest."""

    @abstractmethod
    def release(self, *, provider_id: str, idempotency_key: str) -> None:
        """Void the hold entirely — nothing is charged, the full amount is freed."""

    @abstractmethod
    def refund(self, *, provider_id: str, amount: Decimal, idempotency_key: str) -> RefundResult:
        """Refund `amount` of an already captured payment (full or partial)."""

    @abstractmethod
    def verify_signature(self, *, body: bytes, signature: str) -> bool:
        """Return True iff `signature` authenticates the raw webhook `body`."""

    @abstractmethod
    def parse_webhook(self, body: bytes) -> WebhookEvent:
        """Parse a (already signature-verified) webhook body into a normalized event.

        Raises:
            PaymentProviderError: If the body is not a well-formed event payload.
        """
