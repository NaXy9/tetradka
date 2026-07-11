# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""In-memory payment provider for dev and tests: no network, no real money.

`create_hold` mints a provider id; capture/release/refund succeed deterministically.
Webhooks are signed with HMAC-SHA256 over the raw body using a shared secret, so
the signature-verification path is exercised for real rather than stubbed out —
`build_webhook` produces the exact `(body, signature)` a PSP would POST, which is
what drives the async hold/capture flow in dev and tests.
"""

import hashlib
import hmac
import json
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings

from .base import (
    CaptureResult,
    HoldResult,
    PaymentProvider,
    PaymentProviderError,
    RefundResult,
    WebhookEvent,
)


class MockProvider(PaymentProvider):
    """Deterministic stand-in for a real PSP."""

    def __init__(self, *, secret: str | None = None) -> None:
        self._secret = (secret or settings.PAYMENT_WEBHOOK_SECRET).encode()

    def create_hold(
        self, *, amount: Decimal, booking_id: int, idempotency_key: str, currency: str = "RUB"
    ) -> HoldResult:
        # A real PSP returns a confirmation URL for the payer; the mock has no UI,
        # so the hold is confirmed by delivering a build_webhook() event instead.
        return HoldResult(provider_id=f"mock-{uuid.uuid4()}", confirmation_url=None)

    def capture(self, *, provider_id: str, amount: Decimal, idempotency_key: str) -> CaptureResult:
        return CaptureResult(provider_id=provider_id, captured_amount=Decimal(amount))

    def release(self, *, provider_id: str, idempotency_key: str) -> None:
        return None

    def refund(self, *, provider_id: str, amount: Decimal, idempotency_key: str) -> RefundResult:
        return RefundResult(provider_id=provider_id, refunded_amount=Decimal(amount))

    def verify_signature(self, *, body: bytes, signature: str) -> bool:
        return hmac.compare_digest(self._sign(body), signature)

    def parse_webhook(self, body: bytes) -> WebhookEvent:
        try:
            data = json.loads(body)
            amount = data.get("amount")
            return WebhookEvent(
                event_id=data["event_id"],
                type=data["type"],
                provider_id=data["provider_id"],
                # Amounts cross the wire as strings to keep Decimal precision intact.
                amount=Decimal(str(amount)) if amount is not None else None,
            )
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError, InvalidOperation) as exc:
            raise PaymentProviderError(f"malformed webhook body: {exc}") from exc

    def build_webhook(
        self, *, event_id: str, type: str, provider_id: str, amount: Decimal | None = None
    ) -> tuple[bytes, str]:
        """Build a signed webhook the way a PSP would POST it (dev/test helper)."""
        payload: dict[str, str] = {
            "event_id": event_id,
            "type": str(type),
            "provider_id": provider_id,
        }
        if amount is not None:
            payload["amount"] = str(amount)
        body = json.dumps(payload).encode()
        return body, self._sign(body)

    def _sign(self, body: bytes) -> str:
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()
