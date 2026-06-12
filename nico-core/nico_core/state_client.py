"""HTTP client for Nico's shared DCA state (the Postgres store behind nico-server).

run_live (cron) and nico-bot use this instead of touching a database directly, so
only nico-server holds DB credentials. Configure with:

    NICO_STATE_API_URL    base URL of nico-server (e.g. https://nico-server-...up.railway.app)
    NICO_STATE_TOKEN      bearer token matching nico-server's NICO_STATE_TOKEN

When NICO_STATE_API_URL is unset, enabled() is False and callers fall back to the
legacy local live_state.json (today's behaviour) — so nothing breaks until the API
is provisioned.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests


def enabled() -> bool:
    return bool(os.environ.get("NICO_STATE_API_URL"))


def _base() -> str:
    return os.environ["NICO_STATE_API_URL"].rstrip("/")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ.get('NICO_STATE_TOKEN', '')}",
        "Content-Type": "application/json",
    }


def get_state(timeout: int = 10) -> dict[str, Any]:
    r = requests.get(f"{_base()}/state", headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()


def post_pending(order: dict[str, Any], timeout: int = 10) -> dict[str, Any]:
    """Persist a parked intent to the source of truth. order must include
    request_id, symbol, side, qty (and ideally notional_usd, price, rationale,
    trigger_number)."""
    r = requests.post(f"{_base()}/pending", headers=_headers(), json=order, timeout=timeout)
    r.raise_for_status()
    return r.json()


def post_placed(
    request_id: str,
    order_id: str,
    order_status: str,
    timeout: int = 10,
) -> dict[str, Any]:
    """Report a confirmed fill so nico-server advances spend/triggers (idempotent)."""
    r = requests.post(
        f"{_base()}/placed",
        headers=_headers(),
        json={"request_id": request_id, "order_id": order_id, "order_status": order_status},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()
