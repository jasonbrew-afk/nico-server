"""Approved-order executor for nico-bot  (release side of the Imago approval flow).

Park & release recap:
    run_live -> sends order + rationale to Imago -> exits (no trade)
    Jason approves in Imago
    Imago -> POST /execute on THIS bot  -> we place the order via the Robinhood
             MCP adapter, post the fill to #lab-brew, and record it.

This module provides the aiohttp handler. main.py mounts it next to the Discord
client (same event loop) when IMAGO_EXECUTE_TOKEN is set.

Contract (Imago -> bot):
    POST /execute
    Authorization: Bearer <IMAGO_EXECUTE_TOKEN>
    {
      "request_id": "abc123",                 # idempotency key from run_live
      "order": {"side":"buy","symbol":"BTC/USD","qty":0.01,
                "notional_usd":100.0,"price":10000.0},
      "rationale": "DCA red-day buy ...",      # optional, for the Discord post
      "approved_by": "jason"                   # optional
    }
  ->  200 {"status":"placed","request_id":"abc123","order_id":"...","order_status":"filled"}
      200 {"status":"duplicate","request_id":"abc123"}            # already placed
      401 {"error":"unauthorized"}
      503 {"error":"mcp backend unavailable"}                     # adapter/token missing

The Robinhood MCP adapter is sync and uses asyncio.run() internally, so we run it
via asyncio.to_thread() to avoid 'asyncio.run() cannot be called from a running
event loop'.

NOTE — cross-service DCA state (TODO): run_live owns nico-core's DCA budget/trigger
state on a different Railway service. This executor records the placement to the
bot's trades.json and posts to Discord, but does NOT yet advance nico-core's DCA
budget. Decide the shared source of truth (Imago, or shared Postgres/Redis) before
relying on budget accounting across the approval boundary.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from aiohttp import web

# The MCP adapter lives in the sibling nico-core package. Import is best-effort
# so the bot keeps running (executor just reports 503) if it's unavailable.
_NICO_CORE = Path(__file__).resolve().parent.parent / "nico-core"
if str(_NICO_CORE) not in sys.path:
    sys.path.insert(0, str(_NICO_CORE))

try:
    from platforms.robinhood.mcp_adapter import RobinhoodMCPExecution, RobinhoodMCPError
    _ADAPTER_IMPORT_ERR = None
except Exception as e:  # mcp not installed / package missing
    RobinhoodMCPExecution = None  # type: ignore
    RobinhoodMCPError = Exception  # type: ignore
    _ADAPTER_IMPORT_ERR = e

TRADES_PATH = Path(__file__).parent / "trades.json"

# In-memory idempotency guard. Survives for the process lifetime; for durable
# dedup across restarts, back this with the shared store (see TODO above).
_placed_request_ids: set[str] = set()


def _expected_token() -> str:
    return os.environ.get("IMAGO_EXECUTE_TOKEN", "")


def _authorized(request: web.Request) -> bool:
    expected = _expected_token()
    if not expected:
        return False
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {expected}"


def _record_trade(order: dict, result) -> None:
    """Append the fill to trades.json in the shape _handle_trades expects."""
    trades = []
    if TRADES_PATH.exists():
        try:
            trades = json.loads(TRADES_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            trades = []
    import datetime
    trades.insert(0, {
        "date": datetime.date.today().isoformat(),
        "action": order.get("side", "buy").upper(),
        "asset": order.get("symbol"),
        "price": float(order.get("price") or 0.0),
        "qty": float(order.get("qty") or 0.0),
        "notional_usd": float(order.get("notional_usd") or 0.0),
        "order_id": getattr(result, "order_id", "") if result else "",
        "via": "robinhood-mcp",
    })
    TRADES_PATH.write_text(json.dumps(trades, indent=2))


async def _report_placed(request_id: str, order_id: str, order_status: str) -> None:
    """POST the confirmed fill to nico-server's /placed (advances DCA state).

    No-op if the state API isn't configured. Runs the blocking requests call off
    the event loop. Never raises — the order is already placed."""
    import asyncio
    import requests

    api = os.environ.get("NICO_STATE_API_URL", "")
    if not api:
        return
    token = os.environ.get("NICO_STATE_TOKEN", "")

    def _post():
        return requests.post(
            f"{api.rstrip('/')}/placed",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"request_id": request_id, "order_id": order_id, "order_status": order_status},
            timeout=10,
        )

    try:
        r = await asyncio.to_thread(_post)
        if r.status_code >= 400:
            print(f"[executor] /placed report failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        print(f"[executor] /placed report error: {e}")


def make_app(discord_client=None) -> web.Application:
    """Build the aiohttp app. discord_client (optional) lets us post fills to #lab-brew."""
    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "nico-bot"})

    async def execute(request: web.Request) -> web.Response:
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if RobinhoodMCPExecution is None:
            return web.json_response(
                {"error": "mcp backend unavailable", "detail": str(_ADAPTER_IMPORT_ERR)},
                status=503,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        request_id = str(body.get("request_id") or "")
        order = body.get("order") or {}
        side = (order.get("side") or "buy").lower()
        symbol = order.get("symbol")
        qty = order.get("qty")
        if not request_id or not symbol or qty is None:
            return web.json_response({"error": "missing request_id/order fields"}, status=400)

        if request_id in _placed_request_ids:
            return web.json_response({"status": "duplicate", "request_id": request_id})

        # Place via the sync MCP adapter, off the event loop.
        import asyncio
        try:
            def _place():
                execution = RobinhoodMCPExecution()
                fn = execution.sell if side == "sell" else execution.buy
                return fn(symbol, float(qty), reason=f"approved:{request_id}")

            result = await asyncio.to_thread(_place)
        except RobinhoodMCPError as e:
            return web.json_response({"error": "placement failed", "detail": str(e)}, status=502)
        except Exception as e:
            return web.json_response({"error": "unexpected", "detail": str(e)}, status=500)

        if result is None:
            return web.json_response({"error": "no order returned"}, status=502)

        _placed_request_ids.add(request_id)
        _record_trade({**order, "side": side}, result)

        # Report the fill to nico-server so spend/triggers advance at the source of
        # truth (idempotent server-side). Best-effort: the order is already placed,
        # so a state-API hiccup must not fail the HTTP call — but log it loudly.
        await _report_placed(request_id, result.order_id, result.status)

        # Post the fill to Discord (#lab-brew), best-effort.
        if discord_client is not None and getattr(discord_client, "channel", None):
            rationale = body.get("rationale", "")
            try:
                await discord_client.channel.send(
                    f"✅ **Approved order placed** — {side.upper()} {qty} {symbol}\n"
                    f"`{result.order_id}` · status: {result.status}"
                    + (f"\n_why:_ {rationale}" if rationale else "")
                )
            except Exception as e:  # don't fail the HTTP call on a Discord hiccup
                print(f"[executor] Discord post failed: {e}")

        return web.json_response({
            "status": "placed",
            "request_id": request_id,
            "order_id": result.order_id,
            "order_status": result.status,
        })

    app.router.add_get("/health", health)
    app.router.add_post("/execute", execute)
    return app


async def start_http_server(discord_client=None) -> web.AppRunner:
    """Start the aiohttp server on $PORT (Railway) / 8080. Returns the runner."""
    app = make_app(discord_client)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[executor] /execute listening on :{port}")
    return runner
