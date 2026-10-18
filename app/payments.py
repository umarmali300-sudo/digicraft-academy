from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request

from app.config import Settings


class PaymentError(Exception):
    pass


def create_checkout(settings: Settings, order_id: int, amount_pkr: int, plan_name: str, email: str) -> tuple[str, str]:
    if not settings.payments_ready:
        raise PaymentError("Stripe is not fully configured.")
    payload = {
        "mode": "payment",
        "success_url": (
            f"{settings.base_url}/enroll/return?order_id={order_id}"
            "&session_id={CHECKOUT_SESSION_ID}"
        ),
        "cancel_url": f"{settings.base_url}/enroll?plan_order={order_id}",
        "customer_email": email,
        "client_reference_id": str(order_id),
        "metadata[order_id]": str(order_id),
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "pkr",
        "line_items[0][price_data][unit_amount]": str(amount_pkr * 100),
        "line_items[0][price_data][product_data][name]": f"Digicraft {plan_name}",
    }
    request = urllib.request.Request(
        "https://api.stripe.com/v1/checkout/sessions",
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.stripe_secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise PaymentError("Stripe did not start a checkout session.") from exc
    url = body.get("url")
    session_id = body.get("id")
    if not url or not session_id:
        raise PaymentError("Stripe did not return a checkout URL.")
    return session_id, url


def verify_webhook(payload: bytes, header: str, secret: str, tolerance: int = 300) -> dict:
    if not secret:
        raise PaymentError("Stripe webhook secret is not configured.")
    parts = {}
    for item in (header or "").split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            parts.setdefault(key.strip(), []).append(value.strip())
    try:
        timestamp = int(parts.get("t", ["0"])[0])
    except ValueError as exc:
        raise PaymentError("Invalid Stripe signature.") from exc
    if abs(int(time.time()) - timestamp) > tolerance:
        raise PaymentError("Stripe signature expired.")
    signed = f"{timestamp}.".encode("utf-8") + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    signatures = parts.get("v1", [])
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise PaymentError("Invalid Stripe signature.")
    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise PaymentError("Invalid Stripe payload.") from exc
