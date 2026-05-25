"""Nico Core HTTP Server

Lightweight FastAPI server that receives webhooks from the Railway relay
and updates the local regime model + Discord status.

Endpoints:
  POST /webhook  - Receives alerts from nico-server
  GET  /status   - Returns current regime/status JSON
  GET  /health   - Health check
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

OUTPUT_PATH = Path(__file__).parent / "output.json"

# --- Models ---
class WebhookAlert(BaseModel):
    symbol: str
    action: str  # "buy", "sell", "dca"
    price: float
    timeframe: str
    source: str = "webhook"
    comment: Optional[str] = None
    received_at: Optional[float] = None

# --- App ---
app = FastAPI(title="Nico Core", version="0.1.0")

@app.get("/health")
def health():
    return {"status": "ok", "uptime": time.time()}

@app.get("/status")
def status():
    """Return current regime data for the Discord bot."""
    if OUTPUT_PATH.exists():
        with open(OUTPUT_PATH) as f:
            data = json.load(f)
        return data
    return {"ticker": "BTC-USD", "message": "No data yet. Run `python run.py` first."}

@app.post("/webhook")
def receive_webhook(alert: WebhookAlert):
    """Receive an alert from the Railway relay and log it."""
    logger.info(f"Webhook from {alert.source}: {alert.action} {alert.symbol} @ {alert.price}")
    
    # Here we would:
    # 1. Add the alert to a local data buffer
    # 2. Optionally re-run the regime model if significant
    # 3. Push a status update to the Discord bot
    
    # For now, just log and acknowledge
    return {
        "status": "received",
        "symbol": alert.symbol,
        "action": alert.action,
        "price": alert.price,
        "source": alert.source,
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8001)
