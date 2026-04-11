from __future__ import annotations

from typing import Any, Dict, Optional

try:
    import stripe
except Exception:  # pragma: no cover
    stripe = None  # type: ignore

from config import settings

# ---------------------------------------------------------------------------
# Plan definitions
# ---------------------------------------------------------------------------

PLANS: Dict[str, Dict[str, Any]] = {
    "starter": {
        "price_cents": 4900,
        "name": "Starter",
        "minutes": 200,
        "overage_cents_per_min": 6,
        "stripe_price_id_env": "STRIPE_PRICE_STARTER",
    },
    "growth": {
        "price_cents": 9900,
        "name": "Growth",
        "minutes": 1000,
        "overage_cents_per_min": 5,
        "stripe_price_id_env": "STRIPE_PRICE_GROWTH",
    },
    "pro": {
        "price_cents": 24900,
        "name": "Pro",
        "minutes": 1500,
        "overage_cents_per_min": 5,
        "stripe_price_id_env": "STRIPE_PRICE_PRO",
    },
    "enterprise": {
        "price_cents": 79900,
        "name": "Enterprise",
        "minutes": 6000,
        "overage_cents_per_min": 4,
        "stripe_price_id_env": "STRIPE_PRICE_ENTERPRISE",
    },
}

ADD_ONS: Dict[str, Dict[str, Any]] = {
    "voice_clone": {"price_cents": 1900, "name": "Voice Clone"},
    "territory_standard": {"price_cents": 1500, "name": "Territory (Standard)"},
    "territory_premium": {"price_cents": 3000, "name": "Territory (Premium)"},
    "territory_elite": {"price_cents": 5000, "name": "Territory (Elite)"},
}


def stripe_enabled() -> bool:
    return bool(settings.stripe_enabled and settings.stripe_secret_key and stripe)


def configure_stripe() -> None:
    if stripe_enabled():
        stripe.api_key = settings.stripe_secret_key


def _get_plan_price_id(plan_name: str) -> str:
    """Return the Stripe price ID for a given plan."""
    import os
    plan = PLANS.get(plan_name)
    if plan:
        env_key = plan["stripe_price_id_env"]
        price_id = os.getenv(env_key, "")
        if price_id:
            return price_id
    # Fall back to legacy single price ID
    return settings.stripe_price_id or ""


def create_checkout_session(
    customer_email: str,
    success_url: str,
    cancel_url: str,
    metadata: Optional[Dict[str, str]] = None,
    plan_name: Optional[str] = None,
    price_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not stripe_enabled():
        return {"ok": False, "error": "Stripe is not enabled"}

    configure_stripe()

    resolved_price_id = price_id or _get_plan_price_id(plan_name or "starter")
    if not resolved_price_id:
        return {"ok": False, "error": f"No Stripe price ID configured for plan '{plan_name}'"}

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": resolved_price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        customer_email=customer_email,
        metadata=metadata or {},
    )
    return {"ok": True, "id": session.id, "url": session.url}


def create_portal_session(customer_id: str, return_url: str) -> Dict[str, Any]:
    """Create a Stripe customer portal session for subscription management."""
    if not stripe_enabled():
        return {"ok": False, "error": "Stripe is not enabled"}
    configure_stripe()
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )
    return {"ok": True, "url": session.url}


def create_payment_intent(amount_cents: int, currency: str = "usd", metadata: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Create a one-time Stripe payment intent (for marketplace purchases)."""
    if not stripe_enabled():
        return {"ok": False, "error": "Stripe is not enabled"}
    configure_stripe()
    intent = stripe.PaymentIntent.create(
        amount=amount_cents,
        currency=currency,
        metadata=metadata or {},
        automatic_payment_methods={"enabled": True},
    )
    return {"ok": True, "client_secret": intent.client_secret, "id": intent.id}


def construct_webhook_event(payload: bytes, sig_header: str) -> Any:
    if not stripe_enabled():
        raise RuntimeError("Stripe is not enabled")
    configure_stripe()
    return stripe.Webhook.construct_event(
        payload=payload,
        sig_header=sig_header,
        secret=settings.stripe_webhook_secret,
    )


def get_plan_for_subscription(subscription_id: str) -> Optional[str]:
    """Look up which plan a Stripe subscription corresponds to."""
    if not stripe_enabled():
        return None
    configure_stripe()
    try:
        sub = stripe.Subscription.retrieve(subscription_id)
        price_id = sub["items"]["data"][0]["price"]["id"]
        import os
        for plan_name, plan_data in PLANS.items():
            env_key = plan_data["stripe_price_id_env"]
            if os.getenv(env_key) == price_id:
                return plan_name
    except Exception:
        pass
    return None
