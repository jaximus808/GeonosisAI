"""Stripe client setup for the factory's payments service (TEST MODE ONLY).

Keys come from the environment (or the repo-root .env). This module refuses to
run with anything other than an ``sk_test_`` / ``rk_test_`` key, so it can never
move real money. Key values are never printed or logged.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import stripe

# services/payments/src/rdf_payments/stripe_client.py -> repo root is parents[4]
REPO_ROOT = Path(__file__).resolve().parents[4]

# Pin the API version explicitly so behaviour doesn't drift with the account default.
STRIPE_API_VERSION = "2026-09-30.endive"

PROVIDER = "stripe_test"  # value written to payment.provider


class LiveModeRefused(RuntimeError):
    """Raised when a non-test Stripe key is configured."""


def _load_env() -> None:
    if os.environ.get("STRIPE_SECRET_KEY"):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv is optional; env vars may already be set
        return
    load_dotenv(REPO_ROOT / ".env", override=False)


def _secret_key() -> str:
    _load_env()
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        raise LiveModeRefused("STRIPE_SECRET_KEY is not set (expected an sk_test_ key in .env)")
    if not (key.startswith("sk_test_") or key.startswith("rk_test_")):
        # Never echo the key itself.
        raise LiveModeRefused("Refusing to run: STRIPE_SECRET_KEY is not a test-mode key")
    return key


@lru_cache(maxsize=1)
def get_client() -> stripe.StripeClient:
    """Return a shared StripeClient bound to the test-mode secret key."""
    return stripe.StripeClient(
        _secret_key(),
        stripe_version=STRIPE_API_VERSION,
        max_network_retries=2,
    )


def publishable_key() -> str:
    """Publishable key for the web dashboard (safe to expose to browsers)."""
    _load_env()
    key = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    if key and not key.startswith("pk_test_"):
        raise LiveModeRefused("Refusing to run: STRIPE_PUBLISHABLE_KEY is not a test-mode key")
    return key
