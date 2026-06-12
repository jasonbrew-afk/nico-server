"""Human-in-the-loop trade approval for the Robinhood MCP backend  (SCAFFOLD).

Robinhood's Agentic account is REAL money — there is no paper mode. Jason's rule:
every order must be sent to his agent **Imago** with a rationale, and only placed
after he approves. Architecture (park & release):

    run_live (one-shot cron)
        detects DCA trigger -> builds PendingOrder + rationale
        -> request_approval()  ── POST ──>  Imago        (this module)
        -> exits WITHOUT trading
    Jason reviews in Imago, approves
    Imago  ── triggers ──>  nico-bot (always-on)
    nico-bot  -> RobinhoodMCPExecution.buy(...)          (the actual placement)

This module is ONLY the request side (run_live -> Imago). The release side
(Imago -> bot -> place) is not built yet — it needs Imago's callback transport
and a bot inbound hook (see TODO at bottom).

FAIL-CLOSED: if Imago isn't configured, request_approval() raises. That guarantees
the MCP backend can never silently place an unapproved real-money order.

Transport: default is an HTTP webhook to Imago. If Imago speaks MCP or a CLI
instead, only `_send_to_imago` needs to change — the contract dict is stable.

Env:
    IMAGO_APPROVAL_URL    HTTPS endpoint on Imago that receives approval requests
    IMAGO_API_TOKEN       Bearer token for that endpoint
    ROBINHOOD_REQUIRE_APPROVAL   "1" (default for MCP backend) gates every order
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

import requests


class ApprovalNotConfigured(RuntimeError):
    """Raised when no Imago target is set — callers must treat as 'do not trade'."""


@dataclass
class PendingOrder:
    symbol: str
    side: str               # "buy" | "sell"
    qty: float              # share/unit quantity (Nico computes from notional)
    notional_usd: float     # dollar size of the order
    price: float            # reference price at request time
    rationale: str          # human-readable "why"
    trigger_number: int = 0
    request_id: str = ""
    created_at: str = ""

    def __post_init__(self):
        if not self.request_id:
            # uuid4 avoids the banned Date.now()/random() concerns; uuid is fine.
            self.request_id = uuid.uuid4().hex[:16]
        if not self.created_at:
            self.created_at = datetime.utcnow().isoformat() + "Z"


def build_rationale(
    *,
    asset: str,
    daily_return: float,
    threshold: float,
    trigger_number: int,
    max_triggers: int,
    notional: float,
    remaining_budget: float,
) -> str:
    """Compose the 'why are we doing this' message Jason sees in Imago."""
    return (
        f"DCA red-day buy of ${notional:,.2f} {asset}. "
        f"Today's return {daily_return*100:.2f}% breached the {threshold*100:.1f}% "
        f"buy-the-dip threshold. This is trigger #{trigger_number}/{max_triggers}; "
        f"${remaining_budget:,.2f} of DCA budget remains after this."
    )


def approval_required(backend_name: str) -> bool:
    """MCP backend gates orders by default; flip ROBINHOOD_REQUIRE_APPROVAL=0 to bypass."""
    if backend_name not in ("robinhood_mcp", "robinhood", "mcp"):
        return False
    return os.environ.get("ROBINHOOD_REQUIRE_APPROVAL", "1") != "0"


def request_approval(order: PendingOrder) -> str:
    """Send the order + rationale to Imago for Jason's approval.

    Returns the request_id. Does NOT place the order — placement happens on the
    bot side after Jason approves. Raises ApprovalNotConfigured if Imago isn't
    set up (fail-closed: caller must not trade).
    """
    url = os.environ.get("IMAGO_APPROVAL_URL", "")
    token = os.environ.get("IMAGO_API_TOKEN", "")
    if not url or not token:
        raise ApprovalNotConfigured(
            "IMAGO_APPROVAL_URL / IMAGO_API_TOKEN not set — refusing to place an "
            "unapproved real-money order. Configure Imago or keep "
            "NICO_EXECUTION_BACKEND=alpaca."
        )
    _send_to_imago(url, token, _contract(order))
    return order.request_id


def _contract(order: PendingOrder) -> dict:
    """Stable request payload Imago consumes (transport-independent)."""
    return {
        "type": "trade_approval_request",
        "source": "nico",
        "request_id": order.request_id,
        "order": {
            "side": order.side,
            "symbol": order.symbol,
            "qty": order.qty,
            "notional_usd": order.notional_usd,
            "price": order.price,
        },
        "rationale": order.rationale,
        "created_at": order.created_at,
        # Imago should, on approval, call the bot executor with this request_id.
        # The release contract (Imago -> bot) is TODO — see module footer.
    }


def _send_to_imago(url: str, token: str, payload: dict) -> None:
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# TODO (release side — needs Jason's input, see chat):
#   1. Imago transport: is IMAGO_APPROVAL_URL an HTTP webhook, or does Imago
#      expose an MCP tool / CLI? Only _send_to_imago changes.
#   2. Imago -> bot release: how does an approval reach the always-on nico-bot?
#      Likely a small authenticated endpoint on the bot:
#          POST /execute {request_id, approval_token}  -> bot places via MCP
#      The bot then owns: place order, advance DCA state, post fill to #lab-brew.
#   3. Cross-service state: run_live and the bot are separate Railway services
#      with no shared disk. Decide the source of truth for pending/placed
#      (Imago holds it, or a shared Railway Postgres/Redis).
# ---------------------------------------------------------------------------
