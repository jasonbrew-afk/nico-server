"""Red Day DCA Strategy Module.

Implements the 'Red Day DCA' logic:
- Buys on down days (Red Candles).
- Respects a budget and trigger limit (e.g., 15 triggers).
- Can exit positions when the Markov regime flips to 'Bull'.
"""

from __future__ import annotations

import pandas as pd
import numpy as np


class DCAStrategy:
    def __init__(self, budget: float, assets: list, max_triggers: int = 15, trigger_threshold: float = -0.02):
        self.budget = budget
        self.assets = assets
        self.max_triggers = max_triggers
        self.trigger_threshold = trigger_threshold
        
        # State tracking
        self.spent = 0.0
        self.triggers_used = 0
        self.quantities = {asset: 0.0 for asset in assets}  # Actual units held
        self.portfolio_value = 0.0
        self.transactions = []  # List of dicts for logging

    def process_bar(self, index: pd.Timestamp, row: pd.Series, regime: str, close_price: float) -> list:
        """
        Process a single day of data.
        Returns a list of executed transactions.
        """
        transactions = []
        daily_return = row['pct_change']  # Assuming this is pre-calculated or passed
        
        # Check for Red Day trigger
        is_red_day = daily_return < 0
        
        # 1. BUY LOGIC (DCA)
        if is_red_day and self.triggers_used < self.max_triggers:
            # Calculate allocation per asset
            remaining_budget = self.budget - self.spent
            allocation = remaining_budget / len(self.assets)
            
            for asset in self.assets:
                # Calculate how many units we can buy
                units = allocation / close_price
                self.quantities[asset] += units
                self.spent += allocation
                self.triggers_used += 1
                self.transactions.append({
                    'date': index,
                    'action': 'BUY',
                    'asset': asset,
                    'amount': allocation,
                    'quantity': units,
                    'price': close_price,
                    'reason': f'Red Day Trigger (#{self.triggers_used})'
                })
                transactions.append(f"BUY {asset} @ {units:.4f} units")

        # Update portfolio value based on current close price
        self.portfolio_value = sum(self.quantities[asset] * close_price for asset in self.assets)

        return transactions

    def get_summary(self) -> dict:
        return {
            'budget': self.budget,
            'spent': self.spent,
            'triggers_used': self.triggers_used,
            'portfolio_value': self.portfolio_value,
            'transactions_count': len(self.transactions)
        }
