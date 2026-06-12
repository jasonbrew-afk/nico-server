"""Robinhood official MCP execution backend for Nico  (SCAFFOLD — not yet wired in).

Talks to Robinhood's first-party agent endpoint:

    https://agent.robinhood.com/mcp/trading

Transport (confirmed by recon 2026-06-12):
  * MCP **Streamable HTTP** — NOT SSE.
    - `GET`  -> 405, `Allow: POST`
    - `POST` -> JSON-RPC; server may answer with `application/json` OR an
      `text/event-stream` body (single SSE frame).  Both are handled below.
    - Session id is returned in the `Mcp-Session-Id` response header on
      `initialize` and must be echoed on every subsequent request.

Auth (confirmed by recon):
  * OAuth 2.0, Bearer token in the `Authorization` header.
  * Unauthenticated POST -> 401 with
      `WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource/mcp/trading"`
  * Authorization-server metadata advertises:
      authorization_endpoint : https://robinhood.com/oauth
      token_endpoint         : https://api.robinhood.com/oauth2/token/
      registration_endpoint  : https://agent.robinhood.com/oauth/trading/register   (RFC 7591 DCR)
      grant_types            : authorization_code, refresh_token
      code_challenge_methods : S256            (PKCE required)
      token_endpoint_auth    : none            (public client — no client secret)
      scopes                 : internal
  => A one-time browser authorization_code+PKCE dance is required to mint a
     token. Nico cannot do that headlessly. Jason completes it once, then
     provides the resulting access (and optionally refresh) token via env vars.

This adapter intentionally mirrors the surface of the existing
`nico_core.alpaca_execution.AlpacaExecution` so it is a drop-in, feature-flagged
alternative. NOTE: Nico's *current* live broker is Alpaca, not robin_stocks —
there is no robin_stocks code in this repo. See the recon notes the assistant
left with this change.

!! The MCP tool NAMES below (`TOOL_*`) are PLACEHOLDERS. We could not call
   `tools/list` without a token. After Jason supplies a token, run
   `RobinhoodMCPExecution(...).list_tools()` and correct the `TOOL_MAP`. !!
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

# Reuse the canonical Order/Position shapes so callers don't care which backend
# produced them.
try:  # when imported as part of the installed nico_core package
    from nico_core.alpaca_execution import Order, Position
except Exception:  # pragma: no cover - standalone import fallback
    @dataclass
    class Order:  # type: ignore[no-redef]
        order_id: str
        symbol: str
        qty: float
        side: str
        type: str
        status: str
        filled_avg_price: float = 0.0
        placed_at: str = ""
        reason: str = ""

    @dataclass
    class Position:  # type: ignore[no-redef]
        symbol: str
        qty: float
        avg_entry_price: float
        current_price: float
        unrealized_pnl: float = 0.0
        unrealized_pnl_pct: float = 0.0
        entry_date: str = ""
        stop_loss: float = 0.0
        take_profit: float = 0.0


MCP_ENDPOINT = "https://agent.robinhood.com/mcp/trading"
TOKEN_ENDPOINT = "https://api.robinhood.com/oauth2/token/"
PROTOCOL_VERSION = "2025-06-18"

# --- TOOL NAME MAP (PLACEHOLDERS — verify with .list_tools() once authed) ------
# Map Nico's intent -> the server's actual MCP tool name. Edit the right-hand
# side after discovery; nothing else in this file needs to change.
TOOL_MAP: Dict[str, str] = {
    "get_account": "get_account",
    "get_positions": "get_positions",
    "place_order": "place_order",
    "cancel_order": "cancel_order",
}


class RobinhoodMCPError(RuntimeError):
    pass


class RobinhoodMCPExecution:
    """Execute trades via Robinhood's official MCP server (Streamable HTTP).

    Surface-compatible with ``AlpacaExecution``:
        get_portfolio() -> dict
        buy(symbol, qty, ...) -> Optional[Order]
        sell(symbol, qty, ...) -> Optional[Order]

    Plus the alias names the migration brief asked for:
        get_account_balance(), get_positions(), market_buy(), market_sell()
    """

    def __init__(
        self,
        access_token: Optional[str] = None,
        endpoint: str = MCP_ENDPOINT,
        paper: bool = True,
        hard_stop_pct: float = 0.15,
        trailing_stop_pct: float = 0.10,
        timeout: int = 15,
        # Optional self-refresh (public client, PKCE). Leave unset to manage
        # the token externally.
        refresh_token: Optional[str] = None,
        client_id: Optional[str] = None,
    ):
        self.access_token = access_token or os.environ.get("ROBINHOOD_MCP_TOKEN", "")
        self.refresh_token = refresh_token or os.environ.get("ROBINHOOD_MCP_REFRESH_TOKEN", "")
        self.client_id = client_id or os.environ.get("ROBINHOOD_MCP_CLIENT_ID", "")
        self.endpoint = endpoint
        self.paper = paper
        self.hard_stop_pct = hard_stop_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.timeout = timeout

        if not self.access_token:
            raise RobinhoodMCPError(
                "No Robinhood MCP token. Complete the one-time OAuth dance and set "
                "ROBINHOOD_MCP_TOKEN (see module docstring / migration notes)."
            )

        self._session_id: Optional[str] = None
        self._rpc_id = 0
        self.positions: Dict[str, Position] = {}
        self.order_history: List[Order] = []

        self._initialize()

    # --- transport -----------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    @staticmethod
    def _parse_body(resp: requests.Response) -> Dict[str, Any]:
        """Accept either a plain JSON body or a single SSE `data:` frame."""
        ctype = resp.headers.get("Content-Type", "")
        text = resp.text
        if "text/event-stream" in ctype:
            for line in text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[len("data:"):].strip())
            raise RobinhoodMCPError(f"No SSE data frame in response: {text[:200]}")
        return json.loads(text) if text.strip() else {}

    def _rpc(self, method: str, params: Optional[dict] = None, *, notify: bool = False) -> Any:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            payload["id"] = self._next_id()
        if params is not None:
            payload["params"] = params

        resp = requests.post(
            self.endpoint, headers=self._headers(), json=payload, timeout=self.timeout
        )

        if resp.status_code == 401 and self.refresh_token:
            # Best-effort one-shot refresh, then retry.
            self._refresh_access_token()
            resp = requests.post(
                self.endpoint, headers=self._headers(), json=payload, timeout=self.timeout
            )

        # Capture/refresh the session id whenever the server hands one back.
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid

        if resp.status_code >= 400:
            raise RobinhoodMCPError(
                f"{method} -> HTTP {resp.status_code}: {resp.text[:300]}"
            )
        if notify:
            return None

        body = self._parse_body(resp)
        if "error" in body:
            raise RobinhoodMCPError(f"{method} -> JSON-RPC error: {body['error']}")
        return body.get("result")

    def _initialize(self) -> None:
        self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "nico", "version": "0.1"},
            },
        )
        # Spec requires the initialized notification before normal calls.
        self._rpc("notifications/initialized", notify=True)

    def _refresh_access_token(self) -> None:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }
        if self.client_id:  # public client; include if the server expects it
            data["client_id"] = self.client_id
        r = requests.post(TOKEN_ENDPOINT, data=data, timeout=self.timeout)
        r.raise_for_status()
        tok = r.json()
        self.access_token = tok["access_token"]
        if tok.get("refresh_token"):
            self.refresh_token = tok["refresh_token"]
        self._session_id = None  # force a fresh MCP session on next call

    # --- MCP helpers ---------------------------------------------------------

    def list_tools(self) -> List[Dict[str, Any]]:
        """Discovery helper — run this once authed and reconcile TOOL_MAP."""
        result = self._rpc("tools/list", {})
        return (result or {}).get("tools", [])

    def _call_tool(self, intent: str, arguments: dict) -> Dict[str, Any]:
        name = TOOL_MAP.get(intent, intent)
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        # MCP tool results come back as a content list; pull the first JSON/text
        # block. The exact shape of Robinhood's payloads is unverified — adjust
        # once we can see a real response.
        content = (result or {}).get("content", [])
        for block in content:
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except (json.JSONDecodeError, KeyError):
                    return {"text": block.get("text", "")}
            if block.get("type") == "json":
                return block.get("json", {})
        return result or {}

    # --- AlpacaExecution-compatible surface ----------------------------------

    def get_portfolio(self) -> dict:
        """Mirror of AlpacaExecution.get_portfolio()."""
        account = self._call_tool("get_account", {})
        positions = self._call_tool("get_positions", {})
        pos_list = positions if isinstance(positions, list) else positions.get("positions", [])
        return {
            "cash": _f(account.get("cash") or account.get("buying_power")),
            "portfolio_value": _f(account.get("portfolio_value") or account.get("equity")),
            "buying_power": _f(account.get("buying_power")),
            "positions": [
                {
                    "symbol": p.get("symbol"),
                    "qty": _f(p.get("qty") or p.get("quantity")),
                    "market_value": _f(p.get("market_value")),
                    "current_price": _f(p.get("current_price") or p.get("price")),
                    "unrealized_pl": _f(p.get("unrealized_pl") or p.get("unrealized_pnl")),
                    "unrealized_plpc": _f(p.get("unrealized_plpc")),
                }
                for p in pos_list
            ],
        }

    def buy(
        self,
        symbol: str,
        qty: float,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        time_in_force: str = "day",
        reason: str = "",
    ) -> Optional[Order]:
        return self._place(symbol, qty, "buy", limit_price, stop_price, time_in_force, reason)

    def sell(
        self,
        symbol: str,
        qty: float,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        time_in_force: str = "day",
        reason: str = "",
    ) -> Optional[Order]:
        return self._place(symbol, qty, "sell", limit_price, stop_price, time_in_force, reason)

    def _place(
        self,
        symbol: str,
        qty: float,
        side: str,
        limit_price: Optional[float],
        stop_price: Optional[float],
        time_in_force: str,
        reason: str,
    ) -> Optional[Order]:
        otype = "market"
        args: Dict[str, Any] = {
            "symbol": symbol,
            "quantity": str(qty),
            "side": side,
            "time_in_force": time_in_force,
            "client_order_id": f"nico-{side}-{symbol}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        }
        if limit_price:
            otype = "limit"
            args["limit_price"] = str(limit_price)
        if stop_price:
            otype = "stop"
            args["stop_price"] = str(stop_price)
        args["type"] = otype

        result = self._call_tool("place_order", args)
        if not result:
            return None
        order = Order(
            order_id=str(result.get("id") or result.get("order_id") or ""),
            symbol=symbol,
            qty=qty,
            side=side,
            type=otype,
            status=str(result.get("status", "pending")),
            filled_avg_price=_f(result.get("filled_avg_price")),
            placed_at=str(result.get("submitted_at") or result.get("created_at") or ""),
            reason=reason,
        )
        self.order_history.append(order)
        print(f"  {side.upper()} {qty} {symbol} @ {otype} — {order.order_id}  [robinhood-mcp]")
        return order

    # --- brief-requested aliases --------------------------------------------

    def get_account_balance(self) -> dict:
        return self.get_portfolio()

    def get_positions(self) -> List[dict]:
        return self.get_portfolio()["positions"]

    def market_buy(self, symbol: str, qty: float, reason: str = "") -> Optional[Order]:
        return self.buy(symbol, qty, reason=reason)

    def market_sell(self, symbol: str, qty: float, reason: str = "") -> Optional[Order]:
        return self.sell(symbol, qty, reason=reason)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --- feature-flag factory ----------------------------------------------------
# Lets callers swap backends without code changes. NOT wired into run_live.py
# yet — see the migration notes for the 3-line edit to opt in.
def get_execution_backend(**alpaca_kwargs):
    """Return an execution backend chosen by NICO_EXECUTION_BACKEND.

      NICO_EXECUTION_BACKEND=alpaca         (default — unchanged behaviour)
      NICO_EXECUTION_BACKEND=robinhood_mcp  (this adapter; needs ROBINHOOD_MCP_TOKEN)
    """
    backend = os.environ.get("NICO_EXECUTION_BACKEND", "alpaca").lower()
    if backend in ("robinhood_mcp", "robinhood", "mcp"):
        return RobinhoodMCPExecution(
            paper=alpaca_kwargs.get("paper", True),
            hard_stop_pct=alpaca_kwargs.get("hard_stop_pct", 0.15),
            trailing_stop_pct=alpaca_kwargs.get("trailing_stop_pct", 0.10),
        )
    from nico_core.alpaca_execution import AlpacaExecution
    return AlpacaExecution(**alpaca_kwargs)
