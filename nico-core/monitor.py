"""Alpaca Red-Day Monitor for Nico.
Checks Alpaca data for red candles and triggers alerts via the Railway server.
"""

import json
import logging
import os
import requests
from pathlib import Path
from datetime import datetime, date
from alpaca.data.timeframe import TimeFrame
from alpaca.data.requests import StockBarsRequest
from alpaca.data.historical import StockHistoricalDataClient


def _load_dotenv():
    """Local-dev: load the nearest .env without overriding real env vars. No-op if absent."""
    here = Path(__file__).resolve()
    for d in (here.parent, *here.parents):
        f = d / ".env"
        if f.is_file():
            for line in f.read_text().splitlines():
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, v = s.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            break


_load_dotenv()

# Configuration
SYMBOL = os.getenv("NICO_SYMBOL", "SPY")  # e.g. SPY, BTC-USD, TSLA
SERVER_URL = os.getenv("NICO_SERVER_URL", "https://nico-server-production.up.railway.app/webhook/alpaca")
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "True").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def monitor_red_day():
    """Check if the market closed red today and trigger an alert."""
    logger.info(f"Checking {SYMBOL} for Red Day via Alpaca...")
    
    try:
        # Request the last 2 bars to compare today's close vs open
        data_client = StockHistoricalDataClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY)
        
        request_params = StockBarsRequest(
            symbol_or_symbols=SYMBOL,
            timeframe=TimeFrame.Day,
            start=datetime.now() - __import__("datetime").timedelta(days=3),
            end=datetime.now()
        )
        bars = data_client.get_stock_bars(request_params)
        
        if not bars or not bars.data:
            logger.warning("No bars returned from Alpaca.")
            return

        # Get the latest bar (most recent trading day)
        # bars.data is a dict of symbol -> list of bars
        if SYMBOL not in bars.data:
            logger.warning(f"No bar data for {SYMBOL}")
            return
            
        bar_list = bars.data[SYMBOL]
        if len(bar_list) < 1:
            logger.warning("Insufficient bar data.")
            return
            
        latest_bar = bar_list[-1]
        
        open_price = float(latest_bar.open)
        close_price = float(latest_bar.close)
        is_red = close_price < open_price
        change_pct = ((close_price - open_price) / open_price) * 100
        
        if is_red:
            logger.info(f"RED DAY DETECTED: {SYMBOL} closed at ${close_price:.2f} ({change_pct:.2f}%)")
            
            # Send alert to your Railway server
            alert_payload = {
                "symbol": SYMBOL,
                "action": "dca_buy",
                "price": close_price,
                "timeframe": "D",
                "strategy": "red_day_dca",
                "comment": f"Auto-detected Red Day (-{change_pct:.2f}%) via Alpaca Monitor",
                "source": "alpaca_monitor"
            }
            
            try:
                resp = requests.post(SERVER_URL, json=alert_payload, timeout=10)
                if resp.status_code == 200:
                    logger.info("Alert forwarded to Nico Server successfully.")
                else:
                    logger.error(f"Server rejected alert: {resp.status_code}")
            except Exception as e:
                logger.error(f"Failed to send alert: {e}")
        else:
            logger.info(f"No Red Day for {SYMBOL}. Close ${close_price:.2f} vs Open ${open_price:.2f}")

    except Exception as e:
        logger.error(f"Error checking Alpaca data: {e}")

if __name__ == "__main__":
    monitor_red_day()
