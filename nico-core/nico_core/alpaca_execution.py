"""Alpaca execution module for Nico.

Handles order placement, position management, and risk controls.
Supports both paper and live trading.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests


@dataclass
class Order:
    """Represents a placed order."""
    order_id: str
    symbol: str
    qty: float
    side: str  # "buy" or "sell"
    type: str  # "market", "limit", "stop"
    status: str  # "filled", "partial", "cancelled", "rejected"
    filled_avg_price: float = 0.0
    placed_at: str = ""
    reason: str = ""


@dataclass
class Position:
    """Represents an open position."""
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    entry_date: str = ""
    stop_loss: float = 0.0
    take_profit: float = 0.0


class AlpacaExecution:
    """Execute trades via Alpaca API."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        base_url: str = "https://paper-api.alpaca.markets",
        paper: bool = True,
        hard_stop_pct: float = 0.15,
        trailing_stop_pct: float = 0.10,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url
        self.paper = paper
        self.hard_stop_pct = hard_stop_pct
        self.trailing_stop_pct = trailing_stop_pct

        self.positions: Dict[str, Position] = {}
        self.order_history: List[Order] = []

        # Headers for API requests
        self.headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, endpoint: str, data: Optional[dict] = None) -> dict:
        """Make a request to the Alpaca API."""
        url = f"{self.base_url}/v2{endpoint}"
        try:
            if method == "GET":
                resp = requests.get(url, headers=self.headers, timeout=10)
            elif method == "POST":
                resp = requests.post(url, headers=self.headers, json=data, timeout=10)
            elif method == "DELETE":
                # Cancel order
                resp = requests.delete(url, headers=self.headers, timeout=10)
            else:
                raise ValueError(f"Unsupported method: {method}")

            resp.raise_for_status()
            return resp.json() if resp.status_code != 204 else {}
        except requests.exceptions.RequestException as e:
            print(f"  Alpaca API error: {e}")
            return {}

    def get_portfolio(self) -> dict:
        """Get current portfolio summary."""
        portfolio = self._request("GET", "/account")
        positions = self._request("GET", "/positions")

        return {
            "cash": float(portfolio.get("cash", 0)),
            "portfolio_value": float(portfolio.get("portfolio_value", 0)),
            "buying_power": float(portfolio.get("buying_power", 0)),
            "positions": [
                {
                    "symbol": p.get("symbol"),
                    "qty": float(p.get("qty", 0)),
                    "market_value": float(p.get("market_value", 0)),
                    "current_price": float(p.get("current_price", 0)),
                    "unrealized_pl": float(p.get("unrealized_pl", 0)),
                    "unrealized_plpc": float(p.get("unrealized_plpc", 0)),
                }
                for p in positions
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
        """Place a buy order."""
        data = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "market",  # Always market for simplicity
            "time_in_force": time_in_force,
            "client_order_id": f"nico-{symbol}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        }

        if limit_price:
            data["type"] = "limit"
            data["limit_price"] = str(limit_price)
        if stop_price:
            data["type"] = "stop"
            data["stop_price"] = str(stop_price)

        result = self._request("POST", "/orders", data)
        if not result:
            return None

        order = Order(
            order_id=result.get("id", ""),
            symbol=symbol,
            qty=qty,
            side="buy",
            type=data["type"],
            status=result.get("status", "pending"),
            placed_at=result.get("submitted_at", ""),
            reason=reason,
        )
        self.order_history.append(order)
        print(f"  BUY {qty} {symbol} @ {data['type']} — {order.order_id}")
        return order

    def sell(
        self,
        symbol: str,
        qty: float,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        time_in_force: str = "day",
        reason: str = "",
    ) -> Optional[Order]:
        """Place a sell order."""
        data = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "market",
            "time_in_force": time_in_force,
            "client_order_id": f"nico-sell-{symbol}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        }

        if limit_price:
            data["type"] = "limit"
            data["limit_price"] = str(limit_price)
        if stop_price:
            data["type"] = "stop"
            data["stop_price"] = str(stop_price)

        result = self._request("POST", "/orders", data)
        if not result:
            return None

        order = Order(
            order_id=result.get("id", ""),
            symbol=symbol,
            qty=qty,
            side="sell",
            type=data["type"],
            status=result.get("status", "pending"),
            placed_at=result.get("submitted_at", ""),
            reason=reason,
        )
        self.order_history.append(order)
        print(f"  SELL {qty} {symbol} @ {data['type']} — {order.order_id}")
        return order

    def check_risk_controls(self, symbol: str, current_price: float) -> bool:
        """Check if a trade should be blocked by risk controls. Returns True if blocked."""
        if symbol not in self.positions:
            return False  # No position to stop-loss

        pos = self.positions[symbol]
        pnl_pct = (current_price - pos.avg_entry_price) / pos.avg_entry_price

        # Hard stop: sell entire position if dropped 15% from entry
        if pnl_pct <= -self.hard_stop_pct:
            print(f"  ⚠ HARD STOP triggered for {symbol}: {pnl_pct*100:.1f}% loss")
            return True

        # Trailing stop: track peak and stop if 10% below
        if pos.unrealized_pnl_pct > 0:
            new_peak = pos.avg_entry_price * (1 + pos.unrealized_pnl_pct)
            trailing_stop = new_peak * (1 - self.trailing_stop_pct)
            if current_price <= trailing_stop:
                print(f"  ⚠ TRAILING STOP triggered for {symbol}")
                return True

        return False

    def update_position(self, symbol: str, current_price: float):
        """Update position PnL tracking."""
        if symbol in self.positions:
            pos = self.positions[symbol]
            pos.current_price = current_price
            pos.unrealized_pnl = (current_price - pos.avg_entry_price) * pos.qty
            pos.unrealized_pnl_pct = (current_price - pos.avg_entry_price) / pos.avg_entry_price
            # Update trailing stop
            pos.stop_loss = current_price * (1 - self.trailing_stop_pct)
