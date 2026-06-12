"""Head-to-head backtest: Momentum vs DCA vs Combined.

Usage:
    python compare_strategies.py [--tickers SPY BTC-USD ETH-USD] [--years 10]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from nico_core.data import fetch_ohlcv
from nico_core.regime import (
    STATES,
    build_transition_matrix,
    label_regimes,
    signal_from_matrix,
    walk_forward_backtest,
)
from nico_core.dca import DCAStrategy
from nico_core.combined_strategy import CombinedStrategy


# ── Momentum Strategy ──────────────────────────────────────────────

def momentum_backtest(
    close: pd.Series,
    roc_period: int = 14,
    threshold: float = 5.0,
    initial_capital: float = 100_000.0,
) -> dict:
    """Run the go-trader momentum strategy as a backtest."""
    capital = initial_capital
    equity = [capital]
    n_trades = 0
    win_trades = 0
    in_position = False
    entry_price = 0.0

    roc = ((close - close.shift(roc_period)) / close.shift(roc_period)) * 100

    for i in range(roc_period, len(close)):
        current_roc = roc.iloc[i]
        prev_roc = roc.iloc[i - 1]
        price = close.iloc[i]

        if not in_position:
            # Entry: ROC crosses above threshold
            if prev_roc <= threshold and current_roc > threshold:
                in_position = True
                entry_price = price
                n_trades += 1
        else:
            # Exit: ROC crosses below -threshold or after 30 days (hard timeout)
            if prev_roc >= -threshold and current_roc < -threshold:
                pnl = (price - entry_price) / entry_price
                capital *= (1 + pnl)
                equity.append(capital)
                in_position = False
                if pnl > 0:
                    win_trades += 1

    # Close any open position at the end
    if in_position:
        pnl = (close.iloc[-1] - entry_price) / entry_price
        capital *= (1 + pnl)
        equity.append(capital)

    equity = pd.Series(equity)
    daily_returns = equity.pct_change().dropna()

    if len(daily_returns) > 1 and daily_returns.std(ddof=1) > 0:
        sharpe = daily_returns.mean() / daily_returns.std(ddof=1) * np.sqrt(252)
    else:
        sharpe = float("nan")

    running_max = np.maximum.accumulate(equity)
    drawdown = (equity - running_max) / running_max
    max_dd = float(drawdown.min()) if len(drawdown) else float("nan")

    total_return = (capital / initial_capital - 1) * 100

    return {
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "total_return_pct": total_return,
        "n_trades": n_trades,
        "win_rate": win_trades / n_trades if n_trades > 0 else 0,
        "final_capital": capital,
    }


# ── Markov + DCA Strategy ──────────────────────────────────────────

def markov_dca_backtest(
    close: pd.Series,
    initial_capital: float = 100_000.0,
    window: int = 20,
    bull_threshold: float = 5.0,
    bear_threshold: float = 5.0,
) -> dict:
    """Run the Nico Markov regime + DCA strategy as a backtest."""
    labels = label_regimes(close, window=window, bull_threshold=bull_threshold/100, bear_threshold=-bear_threshold/100)
    labels = labels.loc[labels.index.intersection(close.index)]

    # DCA configuration
    dca_strat = DCAStrategy(
        budget=initial_capital,
        assets=["BTC-USD"],  # single asset for comparison
        max_triggers=25,
        trigger_threshold=-0.02,
    )

    close_prices = close.copy()
    pct_changes = close_prices.pct_change()
    df = pd.DataFrame({"close": close_prices, "pct_change": pct_changes, "regime": labels})
    df = df.dropna()

    for idx, row in df.iterrows():
        dca_strat.process_bar(idx, row, row["regime"], row["close"])

    # Calculate portfolio value at end
    final_value = dca_strat.portfolio_value
    total_return = (final_value / initial_capital - 1) * 100

    # Estimate Sharpe from regime signals
    equity = np.ones(len(df))
    for pos, (idx, row) in enumerate(df.iterrows()):
        if row["regime"] == 0:  # Bear
            equity[pos] = 1 - abs(row["pct_change"]) * 0.5  # partial loss
        elif row["regime"] == 2:  # Bull
            equity[pos] = 1 + row["pct_change"] * 0.5
        # Sideways: no change

    equity = pd.Series(equity)
    daily_returns = equity.pct_change().dropna()

    if len(daily_returns) > 1 and daily_returns.std(ddof=1) > 0:
        sharpe = daily_returns.mean() / daily_returns.std(ddof=1) * np.sqrt(252)
    else:
        sharpe = float("nan")

    running_max = np.maximum.accumulate(equity)
    drawdown = (equity - running_max) / running_max
    max_dd = float(drawdown.min()) if len(drawdown) else float("nan")

    return {
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "total_return_pct": total_return,
        "n_trades": dca_strat.triggers_used,
        "win_rate": 0,  # DCA doesn't have discrete win/loss
        "final_capital": final_value,
    }


# ── Combined Strategy ──────────────────────────────────────────────

def combined_backtest(
    close: pd.Series,
    initial_capital: float = 1500.0,
    window: int = 20,
    bull_threshold: float = 5.0,
    bear_threshold: float = 5.0,
) -> dict:
    """Run the combined momentum + DCA strategy as a backtest."""
    labels = label_regimes(close, window=window, bull_threshold=bull_threshold/100, bear_threshold=-bear_threshold/100)
    labels = labels.loc[labels.index.intersection(close.index)]

    combined = CombinedStrategy(
        total_budget=initial_capital,
        dca_assets=["BTC-USD"],
        dca_max_triggers=20,
        dca_trigger_threshold=-0.02,
        momentum_enabled=True,
        momentum_roc_period=14,
        momentum_entry_threshold=5.0,
        momentum_exit_threshold=-5.0,
    )

    roc = ((close - close.shift(14)) / close.shift(14)) * 100
    df = pd.DataFrame({
        "close": close,
        "pct_change": close.pct_change(),
        "roc": roc,
        "regime": labels,
    }).dropna()

    equity = [initial_capital]
    for idx, row in df.iterrows():
        signal = combined.process_bar(
            index=idx,
            close=row["close"],
            daily_return=row["pct_change"],
            roc=row["roc"],
        )
        equity.append(combined.portfolio_value)

    # Calculate metrics from equity curve
    equity = pd.Series(equity)
    daily_returns = equity.pct_change().dropna()

    if len(daily_returns) > 1 and daily_returns.std(ddof=1) > 0:
        sharpe = daily_returns.mean() / daily_returns.std(ddof=1) * np.sqrt(252)
    else:
        sharpe = float("nan")

    running_max = np.maximum.accumulate(equity)
    drawdown = (equity - running_max) / running_max
    max_dd = float(drawdown.min()) if len(drawdown) else float("nan")

    final_value = combined.portfolio_value
    total_return = (final_value / initial_capital - 1) * 100

    return {
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "total_return_pct": total_return,
        "n_trades": combined.dca_triggers_used + (1 if combined.momentum_position else 0),
        "win_rate": 0,
        "final_capital": final_value,
    }


# ── Comparison Runner ──────────────────────────────────────────────

def compare(ticker: str, years: int = 10) -> None:
    print(f"\n{'='*70}")
    print(f"  {ticker} | {years}-year backtest")
    print(f"{'='*70}")

    close, _ = fetch_ohlcv(ticker, years)
    print(f"  Data: {len(close)} bars | {close.index.min().date()} -> {close.index.max().date()}")

    momentum = momentum_backtest(close)
    dca = markov_dca_backtest(close)
    combined = combined_backtest(close)

    # Display comparison
    print(f"\n  {'Metric':<30s}  {'Momentum':>15s}  {'DCA':>15s}  {'Combined':>15s}  {'Winner':>12s}")
    print(f"  {'─'*30}  {'─'*15}  {'─'*15}  {'─'*15}  {'─'*12}")

    for key, label in [
        ("sharpe", "Sharpe Ratio"),
        ("max_drawdown", "Max Drawdown"),
        ("total_return_pct", "Total Return"),
        ("n_trades", "Trades"),
    ]:
        m_val = momentum[key]
        d_val = dca[key]
        c_val = combined[key]
        m_str = f"{m_val:.3f}" if isinstance(m_val, float) and np.isfinite(m_val) else "N/A"
        d_str = f"{d_val:.3f}" if isinstance(d_val, float) and np.isfinite(d_val) else "N/A"
        c_str = f"{c_val:.3f}" if isinstance(c_val, float) and np.isfinite(c_val) else "N/A"

        if key in ("max_drawdown",):
            values = [(abs(m_val), "Momentum"), (abs(d_val), "DCA"), (abs(c_val), "Combined")]
            values = [(v, n) for v, n in values if np.isfinite(v)]
            winner = min(values, key=lambda x: x[0])[1] if values else "—"
        elif key == "sharpe":
            values = [(m_val, "Momentum"), (d_val, "DCA"), (c_val, "Combined")]
            values = [(v, n) for v, n in values if np.isfinite(v)]
            winner = max(values, key=lambda x: x[0])[1] if values else "—"
        elif key == "total_return_pct":
            values = [(m_val, "Momentum"), (d_val, "DCA"), (c_val, "Combined")]
            values = [(v, n) for v, n in values if np.isfinite(v)]
            winner = max(values, key=lambda x: x[0])[1] if values else "—"
        elif key == "n_trades":
            winner = "—"  # neutral metric
        else:
            winner = "—"

        print(f"  {label:<30s}  {m_str:>15s}  {d_str:>15s}  {c_str:>15s}  {winner:>12s}")

    # Win rate (momentum only)
    m_wr = momentum.get("win_rate", 0) * 100
    print(f"\n  Momentum win rate: {m_wr:.1f}% ({momentum['n_trades']} trades)")
    print(f"  DCA triggers used: {dca['n_trades']}")
    print(f"  Combined trades: {combined['n_trades']}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="compare_strategies")
    parser.add_argument("--tickers", nargs="+", default=["BTC-USD", "SPY"], help="Tickers to compare")
    parser.add_argument("--years", type=int, default=10, help="Years of history")
    args = parser.parse_args()

    for ticker in args.tickers:
        try:
            compare(ticker, args.years)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  ERROR: {e}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
