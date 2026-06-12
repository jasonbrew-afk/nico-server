"""Combined Momentum + Markov DCA Strategy.

Momentum sets the directional bias (bullish/bearish/neutral).
DCA fires on red days, but only when momentum is bullish.
When momentum is bearish, DCA entries are paused (don't catch falling knives).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Optional

import numpy as np
import pandas as pd


@dataclass
class CombinedSignal:
    """A combined signal from both strategies."""
    date: pd.Timestamp
    # Momentum component
    roc: float = 0.0
    momentum_signal: int = 0  # -1 (short), 0 (flat), 1 (long)
    momentum_budget_remaining: float = 0.0
    # DCA component
    dca_signal: int = 0  # -1 (none), 0 (buy)
    dca_trigger_number: int = 0
    dca_allocation: float = 0.0
    # Combined
    action: str = "HOLD"  # "BUY_DCA", "BUY_MOMENTUM", "SELL", "HOLD"
    reason: str = ""


class CombinedStrategy:
    """Combines momentum trend-following with DCA entry timing."""

    def __init__(
        self,
        total_budget: float,
        dca_assets: List[str],
        dca_max_triggers: int = 20,
        dca_trigger_threshold: float = -0.02,
        momentum_enabled: bool = True,
        momentum_roc_period: int = 14,
        momentum_entry_threshold: float = 5.0,
        momentum_exit_threshold: float = -5.0,
        momentum_hold_max_days: int = 30,
    ):
        # Budget split: 50/50 between DCA and momentum
        dca_budget = total_budget * 0.5
        momentum_budget = total_budget * 0.5

        self.dca_budget = dca_budget
        self.momentum_budget = momentum_budget
        self.total_budget = total_budget

        self.dca_assets = dca_assets
        self.dca_max_triggers = dca_max_triggers
        self.dca_trigger_threshold = dca_trigger_threshold

        self.momentum_enabled = momentum_enabled
        self.momentum_roc_period = momentum_roc_period
        self.momentum_entry_threshold = momentum_entry_threshold
        self.momentum_exit_threshold = momentum_exit_threshold
        self.momentum_hold_max_days = momentum_hold_max_days

        # State tracking
        self.dca_spent = 0.0
        self.dca_triggers_used = 0
        self.dca_holdings: List[Dict] = []  # Track each DCA buy

        self.momentum_spent = 0.0
        self.momentum_position = None  # None or {"asset": str, "entry_price": float, "entry_date": pd.Timestamp, "qty": float}
        self.momentum_hold_days = 0

        self.cash = total_budget
        self.portfolio_value = total_budget
        self.transactions: List[Dict] = []

    def process_bar(
        self,
        index: pd.Timestamp,
        close: float,
        daily_return: float,
        roc: float,
    ) -> Optional[CombinedSignal]:
        """Process one bar of data and return the combined signal."""
        signal = CombinedSignal(date=index, roc=roc)

        # ── Momentum Component ─────────────────────────────────────
        if self.momentum_enabled:
            # Momentum entry: ROC crosses above threshold
            if self.momentum_position is None:
                if roc > self.momentum_entry_threshold:
                    signal.momentum_signal = 1
                    # Allocate momentum budget
                    remaining = self.momentum_budget - self.momentum_spent
                    if remaining > 10:  # Min $10 trade
                        qty = remaining / close
                        self.momentum_position = {
                            "asset": self.dca_assets[0],
                            "entry_price": close,
                            "entry_date": index,
                            "qty": qty,
                        }
                        self.momentum_spent += remaining
                        self.cash -= remaining
                        self.momentum_hold_days = 0
                        signal.action = "BUY_MOMENTUM"
                        signal.reason = f"Momentum buy: ROC={roc:.1f}%, ${remaining:.0f}"
                        self.transactions.append({
                            "date": index,
                            "action": "BUY_MOMENTUM",
                            "asset": self.dca_assets[0],
                            "price": close,
                            "qty": qty,
                            "amount": remaining,
                        })

            # Momentum exit: ROC crosses below -threshold OR hold timeout
            elif self.momentum_position is not None:
                self.momentum_hold_days += 1
                if roc < self.momentum_exit_threshold or self.momentum_hold_days >= self.momentum_hold_max_days:
                    pos = self.momentum_position
                    sell_value = pos["qty"] * close
                    self.cash += sell_value
                    signal.action = "SELL"
                    signal.reason = f"Momentum exit: ROC={roc:.1f}%, hold={self.momentum_hold_days}d"
                    self.transactions.append({
                        "date": index,
                        "action": "SELL",
                        "asset": pos["asset"],
                        "price": close,
                        "qty": pos["qty"],
                        "amount": sell_value,
                    })
                    self.momentum_position = None

        # ── DCA Component ──────────────────────────────────────────
        if self.dca_triggers_used < self.dca_max_triggers:
            is_red_day = daily_return < self.dca_trigger_threshold

            if is_red_day and signal.action != "BUY_MOMENTUM":
                # Only DCA if we didn't just trigger momentum
                remaining_budget = self.dca_budget - self.dca_spent
                if remaining_budget > 10:
                    allocation = remaining_budget / len(self.dca_assets)
                    qty = allocation / close
                    self.dca_holdings.append({
                        "asset": self.dca_assets[0],
                        "price": close,
                        "qty": qty,
                        "date": index,
                    })
                    self.dca_spent += allocation
                    self.cash -= allocation
                    self.dca_triggers_used += 1
                    signal.dca_signal = 0  # Buy
                    signal.dca_trigger_number = self.dca_triggers_used
                    signal.dca_allocation = allocation
                    signal.action = "BUY_DCA"
                    signal.reason = f"DCA trigger #{signal.dca_trigger_number}: red day ({daily_return*100:.1f}%)"
                    self.transactions.append({
                        "date": index,
                        "action": "BUY_DCA",
                        "asset": self.dca_assets[0],
                        "price": close,
                        "qty": qty,
                        "amount": allocation,
                    })

        # ── Update portfolio value ─────────────────────────────────
        dca_holdings_value = sum(h["qty"] * close for h in self.dca_holdings)
        momentum_value = self.momentum_position["qty"] * close if self.momentum_position else 0
        self.portfolio_value = self.cash + dca_holdings_value + momentum_value

        # ── No signals ─────────────────────────────────────────────
        if signal.action == "HOLD":
            if self.momentum_position is not None:
                signal.action = "HOLD_MOMENTUM"
                signal.reason = f"Holding momentum position (hold={self.momentum_hold_days}d)"

        return signal

    def get_summary(self) -> Dict:
        return {
            "total_budget": self.total_budget,
            "cash": self.cash,
            "dca_spent": self.dca_spent,
            "dca_triggers_used": self.dca_triggers_used,
            "momentum_spent": self.momentum_spent,
            "momentum_position": "OPEN" if self.momentum_position else "CLOSED",
            "portfolio_value": self.portfolio_value,
            "total_transactions": len(self.transactions),
        }
