"""Red Day DCA Strategy Module.

Implements the 'Red Day DCA' logic:
- Buys on down days (Red Candles).
- Respects a budget and trigger limit (e.g., 15 triggers).
- Splits allocation across assets evenly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Optional

import pandas as pd
import numpy as np


@dataclass
class DCAState:
    """Track DCA strategy state."""
    budget: float
    assets: List[str]
    max_triggers: int
    trigger_threshold: float
    allocation_per_asset: float = 0.3333  # Split evenly
    min_trade_usd: float = 5.0

    # Internal state
    spent: float = 0.0
    triggers_used: int = 0
    holdings: List[Dict] = field(default_factory=list)  # Each buy: {asset, price, qty, date}
    transactions: List[Dict] = field(default_factory=list)  # All trades

    @property
    def remaining_budget(self) -> float:
        return self.budget - self.spent

    @property
    def portfolio_value(self) -> float:
        """Current value of all holdings at last price."""
        return sum(h["qty"] * h["current_price"] for h in self.holdings) if self.holdings else 0.0


class DCAStrategy:
    def __init__(
        self,
        budget: float,
        assets: list,
        max_triggers: int = 15,
        trigger_threshold: float = -0.02,
        allocation_per_asset: float = 0.3333,
        min_trade_usd: float = 5.0,
    ):
        self.state = DCAState(
            budget=budget,
            assets=assets,
            max_triggers=max_triggers,
            trigger_threshold=trigger_threshold,
            allocation_per_asset=allocation_per_asset,
            min_trade_usd=min_trade_usd,
        )

    def process_bar(
        self,
        index: pd.Timestamp,
        row: pd.Series,
        regime: str,
        close_price: float,
    ) -> Optional[Dict]:
        """
        Process a single day of data.
        Returns executed transactions, or None if no trade.
        """
        daily_return = row['pct_change']

        # Update current price on all holdings
        for h in self.state.holdings:
            h["current_price"] = close_price

        # Check for Red Day trigger
        is_red_day = daily_return < self.state.trigger_threshold

        # BUY LOGIC (DCA)
        if is_red_day and self.state.triggers_used < self.state.max_triggers:
            # Calculate allocation per asset
            allocation = self.state.remaining_budget * self.state.allocation_per_asset

            if allocation < self.state.min_trade_usd:
                return None  # Too small to trade

            transactions = []
            for asset in self.state.assets:
                qty = allocation / close_price
                self.state.holdings.append({
                    "asset": asset,
                    "price": close_price,
                    "qty": qty,
                    "date": index,
                    "current_price": close_price,
                })
                self.state.spent += allocation
                self.state.triggers_used += 1
                self.state.transactions.append({
                    "date": index,
                    "action": "BUY",
                    "asset": asset,
                    "amount": allocation,
                    "quantity": qty,
                    "price": close_price,
                    "reason": f"Red Day Trigger (#{self.state.triggers_used})",
                })
                transactions.append(f"BUY {asset} @ {qty:.6f} units @ ${close_price:.2f}")

            return {
                "date": index,
                "action": "BUY",
                "allocations": transactions,
                "total_spent": self.state.spent,
                "budget_remaining": self.state.remaining_budget,
            }

        return None

    def get_summary(self) -> Dict:
        return {
            "budget": self.state.budget,
            "spent": self.state.spent,
            "triggers_used": self.state.triggers_used,
            "portfolio_value": self.state.portfolio_value,
            "transactions_count": len(self.state.transactions),
            "remaining_budget": self.state.remaining_budget,
        }
