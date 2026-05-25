"""Nico Server - FastAPI Webhook Relay (v0.1.1)

Acts as the "eyes" of Nico: receives signals from TradingView
and relays them to the local `nico-core` engine.

Endpoints:
  POST /webhook/tradingview - Receives TradingView alerts.
  POST /webhook/alert       - Generic alert receiver.
  GET  /health              - Health check.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx
import yaml
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

# --- Configuration ---
LOCAL_NICO_CORE_URL = os.getenv("NICO_CORE_URL", "http://localhost:8001")
ALLOWED_IPS = os.getenv("ALLOWED_IPS", "").split(",")  # Optional IP allowlist

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --- Models ---
class TradingViewAlert(BaseModel):
    symbol: str
    action: str  # "buy", "sell", "dca"
    price: float
    timeframe: str
    strategy: str
    comment: Optional[str] = None

# --- App ---
app = FastAPI(title="Nico Server", version="0.1.0")

@app.get("/health")
def health():
    return {"status": "ok", "uptime": time.time()}

@app.post("/webhook/tradingview")
async def tradingview_webhook(request: Request):
    """Handle incoming TradingView webhook alerts."""
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400, content="Invalid JSON")

    logger.info(f"Received TradingView alert: {json.dumps(body, default=str)}")

    # Extract fields (TradingView sends various formats)
    symbol = body.get("symbol", body.get("ticker", "UNKNOWN"))
    action = body.get("action", "hold")
    price = body.get("price", 0.0)
    timeframe = body.get("timeframe", "D")
    comment = body.get("comment", body.get("alert_message", ""))

    # Validate
    if not symbol or action == "hold":
        logger.warning(f"Ignoring non-actionable alert: {body}")
        return Response(status_code=200, content="Ignored")

    # Forward to local nico-core
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{LOCAL_NICO_CORE_URL}/webhook",
                json={
                    "symbol": symbol,
                    "action": action,
                    "price": float(price),
                    "timeframe": timeframe,
                    "source": "tradingview",
                    "comment": comment,
                    "received_at": time.time(),
                }
            )
            if resp.status_code == 200:
                logger.info(f"Forwarded alert for {symbol} -> nico-core OK")
                return {"status": "forwarded"}
            else:
                logger.error(f"nico-core rejected alert: {resp.status_code}")
                return Response(status_code=502, content="nico-core unavailable")
    except Exception as e:
        logger.error(f"Failed to forward to nico-core: {e}")
        return Response(status_code=502, content="nico-core unreachable")

@app.post("/webhook/alert")
async def generic_webhook(request: Request):
    """Generic webhook endpoint for any source."""
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400, content="Invalid JSON")

    logger.info(f"Generic alert received: {json.dumps(body, default=str)}")

    # Forward to nico-core
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{LOCAL_NICO_CORE_URL}/webhook",
                json={**body, "source": "webhook", "received_at": time.time()}
            )
            return {"status": "forwarded"} if resp.status_code == 200 else {"status": "failed"}
    except Exception as e:
        logger.error(f"Failed to forward: {e}")
        return Response(status_code=502, content="nico-core unreachable")

@app.post("/status")
async def send_status_to_discord(request: Request):
    """Endpoint to push status updates to Discord via a local helper."""
    try:
        body = await request.json()
        logger.info(f"Status update request: {json.dumps(body, default=str)}")
        # Could forward to the Discord bot here if it exposes an API
        # For now, just log and acknowledge
        return {"status": "logged"}
    except Exception as e:
        logger.error(f"Status error: {e}")
        return Response(status_code=500, content="Internal error")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    logger.info(f"Nico Server starting on port {port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port)
