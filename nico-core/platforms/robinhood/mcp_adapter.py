"""Robinhood official MCP execution backend for Nico  (SCAFFOLD — not yet wired in).

Speaks to Robinhood's first-party agent endpoint using the official Anthropic
`mcp` Python package over **Streamable HTTP**:

    https://agent.robinhood.com/mcp/trading

Why this shape
--------------
Nico is a plain Python script, not an LLM client, so it has to *be* the MCP
client itself (architecture option A — standalone, no Hermes in the loop). We
use `mcp.client.streamable_http.streamablehttp_client` + `mcp.ClientSession`,
and `mcp.client.auth.OAuthClientProvider` for the OAuth handshake. The MCP lib
is async-only; this adapter wraps each call in `asyncio.run()` so the rest of
Nico (sync, Alpaca-shaped) is unchanged. Per-call connect is fine for Nico's
once-a-day DCA cadence.

Auth model (confirmed by recon + Robinhood docs)
------------------------------------------------
  * OAuth 2.0, PKCE (S256), public client (token_endpoint_auth=none), Dynamic
    Client Registration. Scope `internal`.
  * Connecting triggers a **one-time desktop-browser onboarding** that creates a
    separate **Agentic account** (distinct from Jason's main account).
  * After onboarding the session has READ access to ALL his Robinhood accounts
    (portfolio value, buying power, positions, balances, history) but can only
    PLACE ORDERS in the dedicated Agentic account.

Because that dance needs a real browser, it can't run headless. Do it ONCE via:

    python -m platforms.robinhood.mcp_adapter authorize

which opens the browser, captures the redirect on localhost, and persists the
tokens (+ DCR client info) to:

    ~/.config/nico/robinhood-mcp-tokens.json      (gitignored; refreshed in place)

Thereafter `RobinhoodMCPExecution()` loads those tokens and runs unattended,
auto-refreshing via the stored refresh_token.

Tool names
----------
The `TOOL_MAP` right-hand sides are PLACEHOLDERS. After `authorize`, the CLI
prints the server's real `tools/list`; reconcile TOOL_MAP then. Nothing else in
this file needs to change.

NOTE: Nico's current live broker is Alpaca, not robin_stocks — there is no
robin_stocks code in this repo. This adapter is a feature-flagged alternative to
`nico_core.alpaca_execution.AlpacaExecution`, selected by
NICO_EXECUTION_BACKEND=robinhood_mcp.

Install: this backend needs the `mcp` package — `pip install 'mcp>=1.9'`
(declared as the `robinhood` optional-dependency in pyproject.toml).
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Reuse the canonical Order/Position shapes so callers don't care which backend
# produced them.
try:
    from nico_core.alpaca_execution import Order, Position
except Exception:  # pragma: no cover - standalone import fallback
    from dataclasses import dataclass

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


DEFAULT_ENDPOINT = "https://agent.robinhood.com/mcp/trading"
DEFAULT_TOKENS_FILE = Path.home() / ".config" / "nico" / "robinhood-mcp-tokens.json"
SCOPE = "internal"

# --- TOOL NAME MAP (PLACEHOLDERS — verify with `authorize` / list_tools) -------
# Map Nico's intent -> the server's actual MCP tool name. Edit the right side
# after discovery; nothing else in this file changes.
TOOL_MAP: Dict[str, str] = {
    "get_account": "get_account",
    "get_positions": "get_positions",
    "place_order": "place_order",
    "cancel_order": "cancel_order",
}


class RobinhoodMCPError(RuntimeError):
    pass


def _endpoint() -> str:
    return os.environ.get("ROBINHOOD_MCP_ENDPOINT", DEFAULT_ENDPOINT)


def _tokens_file() -> Path:
    return Path(os.environ.get("ROBINHOOD_MCP_TOKENS_FILE", str(DEFAULT_TOKENS_FILE)))


# --- file-backed token storage ------------------------------------------------
# Implements mcp.client.auth.TokenStorage so the OAuthClientProvider persists and
# reloads tokens + DCR client registration across runs.
def _make_storage():
    from mcp.client.auth import TokenStorage
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    class FileTokenStorage(TokenStorage):
        def __init__(self, path: Path):
            self.path = path

        def _read(self) -> dict:
            if not self.path.exists():
                return {}
            try:
                return json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}

        def _write(self, data: dict) -> None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2))
            try:
                os.chmod(self.path, 0o600)  # tokens are secrets
            except OSError:
                pass

        async def get_tokens(self) -> Optional[OAuthToken]:
            raw = self._read().get("tokens")
            return OAuthToken.model_validate(raw) if raw else None

        async def set_tokens(self, tokens: OAuthToken) -> None:
            data = self._read()
            data["tokens"] = tokens.model_dump(exclude_none=True)
            self._write(data)

        async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
            raw = self._read().get("client_info")
            return OAuthClientInformationFull.model_validate(raw) if raw else None

        async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
            data = self._read()
            data["client_info"] = client_info.model_dump(exclude_none=True)
            self._write(data)

    return FileTokenStorage(_tokens_file())


def _build_oauth_provider(redirect_handler=None, callback_handler=None):
    """Construct the OAuthClientProvider (an httpx.Auth) for the endpoint."""
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    client_metadata = OAuthClientMetadata(
        client_name="nico-trading",
        redirect_uris=["http://localhost:8765/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",  # public client (PKCE)
        scope=SCOPE,
    )
    return OAuthClientProvider(
        server_url=_endpoint(),
        client_metadata=client_metadata,
        storage=_make_storage(),
        redirect_handler=redirect_handler or _noninteractive_redirect,
        callback_handler=callback_handler or _noninteractive_callback,
    )


async def _noninteractive_redirect(authorization_url: str) -> None:
    raise RobinhoodMCPError(
        "Robinhood MCP needs interactive OAuth. Run `python -m "
        "platforms.robinhood.mcp_adapter authorize` once in a desktop session."
    )


async def _noninteractive_callback() -> tuple[str, Optional[str]]:
    raise RobinhoodMCPError("Interactive OAuth not available in this context.")


# --- async core ---------------------------------------------------------------
async def _with_session(fn, *, oauth=None):
    """Open a Streamable HTTP MCP session, initialize, run fn(session)."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    provider = oauth or _build_oauth_provider()
    async with streamablehttp_client(_endpoint(), auth=provider, timeout=30) as (
        read_stream,
        write_stream,
        _get_session_id,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await fn(session)


def _run(coro):
    return asyncio.run(coro)


def _unwrap_tool_result(result) -> Dict[str, Any]:
    """Pull a dict out of an MCP CallToolResult."""
    if getattr(result, "isError", False):
        raise RobinhoodMCPError(f"tool error: {getattr(result, 'content', result)}")
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return {}


# --- sync, Alpaca-compatible adapter ------------------------------------------
class RobinhoodMCPExecution:
    """Surface-compatible with AlpacaExecution, backed by Robinhood MCP.

        get_portfolio() -> dict
        buy(symbol, qty, ...) -> Optional[Order]
        sell(symbol, qty, ...) -> Optional[Order]

    Plus brief-requested aliases: get_account_balance, get_positions,
    market_buy, market_sell.
    """

    def __init__(
        self,
        paper: bool = True,
        hard_stop_pct: float = 0.15,
        trailing_stop_pct: float = 0.10,
    ):
        self.paper = paper
        self.hard_stop_pct = hard_stop_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.positions: Dict[str, Position] = {}
        self.order_history: List[Order] = []

        tf = _tokens_file()
        if not tf.exists():
            raise RobinhoodMCPError(
                f"No Robinhood MCP tokens at {tf}. Complete the one-time desktop "
                "OAuth dance: `python -m platforms.robinhood.mcp_adapter authorize`."
            )

    # -- MCP calls --
    def _call(self, intent: str, arguments: dict) -> Dict[str, Any]:
        name = TOOL_MAP.get(intent, intent)

        async def go(session):
            return await session.call_tool(name, arguments)

        return _unwrap_tool_result(_run(_with_session(go)))

    def list_tools(self) -> List[Dict[str, Any]]:
        async def go(session):
            return await session.list_tools()

        result = _run(_with_session(go))
        return [t.model_dump() for t in result.tools]

    # -- AlpacaExecution surface --
    def get_portfolio(self) -> dict:
        account = self._call("get_account", {})
        positions = self._call("get_positions", {})
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

    def buy(self, symbol, qty, limit_price=None, stop_price=None,
            time_in_force="day", reason="") -> Optional[Order]:
        return self._place(symbol, qty, "buy", limit_price, stop_price, time_in_force, reason)

    def sell(self, symbol, qty, limit_price=None, stop_price=None,
             time_in_force="day", reason="") -> Optional[Order]:
        return self._place(symbol, qty, "sell", limit_price, stop_price, time_in_force, reason)

    def _place(self, symbol, qty, side, limit_price, stop_price, time_in_force, reason):
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

        result = self._call("place_order", args)
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

    # -- brief-requested aliases --
    def get_account_balance(self) -> dict:
        return self.get_portfolio()

    def get_positions(self) -> List[dict]:
        return self.get_portfolio()["positions"]

    def market_buy(self, symbol, qty, reason="") -> Optional[Order]:
        return self.buy(symbol, qty, reason=reason)

    def market_sell(self, symbol, qty, reason="") -> Optional[Order]:
        return self.sell(symbol, qty, reason=reason)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --- feature-flag factory -----------------------------------------------------
def get_execution_backend(**alpaca_kwargs):
    """Return an execution backend chosen by NICO_EXECUTION_BACKEND.

      NICO_EXECUTION_BACKEND=alpaca         (default — unchanged behaviour)
      NICO_EXECUTION_BACKEND=robinhood_mcp  (this adapter)
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


# --- one-time interactive OAuth ('authorize' CLI) -----------------------------
async def _authorize_async() -> None:
    import threading
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs

    captured: Dict[str, Optional[str]] = {"code": None, "state": None}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            captured["code"] = (q.get("code") or [None])[0]
            captured["state"] = (q.get("state") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Nico: Robinhood authorized. You can close this tab.</h2>")
            done.set()

        def log_message(self, *_):  # silence
            pass

    server = HTTPServer(("localhost", 8765), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def redirect_handler(authorization_url: str) -> None:
        print("\nOpening Robinhood onboarding in your DESKTOP browser:")
        print(f"  {authorization_url}\n")
        print("If it doesn't open, paste that URL into a desktop browser.")
        webbrowser.open(authorization_url)

    async def callback_handler() -> tuple[str, Optional[str]]:
        print("Waiting for the browser redirect on http://localhost:8765/callback ...")
        await asyncio.get_event_loop().run_in_executor(None, done.wait)
        server.shutdown()
        if not captured["code"]:
            raise RobinhoodMCPError("No authorization code received.")
        return captured["code"], captured["state"]

    provider = _build_oauth_provider(redirect_handler, callback_handler)

    async def list_after(session):
        return await session.list_tools()

    result = await _with_session(list_after, oauth=provider)
    print(f"\nAuthorized. Tokens saved to {_tokens_file()}")
    print("\nServer tools/list (reconcile these into TOOL_MAP):")
    for t in result.tools:
        print(f"  - {t.name}: {(t.description or '').splitlines()[0][:80]}")


def authorize() -> None:
    """Run the one-time desktop-browser OAuth dance and persist tokens."""
    _run(_authorize_async())


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "authorize":
        authorize()
    elif len(sys.argv) > 1 and sys.argv[1] == "list-tools":
        for t in RobinhoodMCPExecution().list_tools():
            print(t.get("name"), "-", (t.get("description") or "")[:80])
    else:
        print("usage: python -m platforms.robinhood.mcp_adapter [authorize|list-tools]")
