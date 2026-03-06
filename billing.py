from __future__ import annotations

from typing import Any, Dict, Optional

try:
    import stripe
except Exception:  # pragma: no cover
    stripe = None  # type: ignore

from .config import settings


def stripe_enabled() -> bool:
    return bool(settings.stripe_enabled and settings.stripe_secret_key and stripe)


def configure_stripe() -> None:
    if stripe_enabled():
        stripe.api_key = settings.stripe_secret_key


def create_checkout_session(
    customer_email: str,
    success_url: str,
    cancel_url: str,
    metadata: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    if not stripe_enabled():
        return {
            "ok": False,
            "error": "Stripe is not enabled",
        }

    configure_stripe()
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": settings.stripe_price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        customer_email=customer_email,
        metadata=metadata or {},
    )
    return {
        "ok": True,
        "id": session.id,
        "url": session.url,
    }


def construct_webhook_event(payload: bytes, sig_header: str) -> Any:
    if not stripe_enabled():
        raise RuntimeError("Stripe is not enabled")

    configure_stripe()
    return stripe.Webhook.construct_event(
        payload=payload,
        sig_header=sig_header,
        secret=settings.stripe_webhook_secret,
    )