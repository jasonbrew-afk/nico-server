"""CLI entry point: fetch -> label -> matrix -> stationary -> walk-forward.

Usage:
    python run.py [--ticker SPY] [--years 10] [--config config.yaml]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from nico_core.data import fetch_ohlcv
from nico_core.regime import (
    STATES,
    build_transition_matrix,
    label_regimes,
    signal_from_matrix,
    stationary_distribution,
    walk_forward_backtest,
)
from nico_core.dca import DCAStrategy


def _hmm_available() -> bool:
    """Check if hmmlearn is available without importing it."""
    try:
        import hmmlearn  # noqa: F401
        return True
    except ImportError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(prog="nico-core")
    parser.add_argument("--ticker", default="BTC-USD", help="Asset ticker")
    parser.add_argument("--years", type=int, default=10, help="Years of history to fetch")
    parser.add_argument("--window", type=int, default=20, help="Rolling-return window in trading days")
    parser.add_argument("--bull-thresh", type=float, default=5.0, help="Bull threshold %%")
    parser.add_argument("--bear-thresh", type=float, default=5.0, help="Bear threshold %%")
    parser.add_argument("--config", default=Path(__file__).parent / "config.yaml", help="Path to config.yaml")
    parser.add_argument("--no-hmm", action="store_true", help="Skip HMM fit even if available")
    args = parser.parse_args()

    # Load config
    if Path(args.config).exists():
        with open(args.config) as f:
            config = yaml.safe_load(f)
        # Override with CLI args if provided
        args.bull_thresh = args.bull_thresh if args.bull_thresh != 5.0 else config.get("strategy", {}).get("bull_threshold", 5.0)
        args.bear_thresh = args.bear_thresh if args.bear_thresh != 5.0 else config.get("strategy", {}).get("bear_threshold", 5.0)
        args.window = args.window if args.window != 20 else config.get("strategy", {}).get("lookback_window", 20)
    else:
        config = {}

    print(f"\nnico-core — ticker={args.ticker} years={args.years} window={args.window}")
    print(f"  fetching {args.ticker} from Yahoo Finance...")

    close, index = fetch_ohlcv(args.ticker, args.years)
    print(f"  fetched {len(close)} rows | {index.min().date()} -> {index.max().date()}")

    bull_thresh = args.bull_thresh / 100.0
    bear_thresh = -args.bear_thresh / 100.0  # Negative for log-return
    labels = label_regimes(close, window=args.window, bull_threshold=bull_thresh, bear_threshold=bear_thresh)
    P = build_transition_matrix(labels)
    pi = stationary_distribution(P)

    print("\nTransition matrix (rows = from, cols = to):")
    print(f"            {STATES[0]:>9s} {STATES[1]:>9s} {STATES[2]:>9s}")
    for i, from_state in enumerate(STATES):
        row = "  ".join(f"{P[i, j]*100:7.2f}%" for j in range(3))
        print(f"  {from_state:>9s}  {row}")

    print("\nPersistence diagonal:")
    print(f"  {STATES[0]} -> {STATES[0]}: {P[0,0]*100:.2f}%")
    print(f"  {STATES[1]} -> {STATES[1]}: {P[1,1]*100:.2f}%")
    print(f"  {STATES[2]} -> {STATES[2]}: {P[2,2]*100:.2f}%")

    print("\nStationary distribution (long-run regime mix):")
    for s, p in zip(STATES, pi):
        print(f"  {s:>9s}: {p*100:.2f}%")

    print("\nWalk-forward backtest (re-estimating matrix at every step, no lookahead)...")
    result = walk_forward_backtest(close, labels)
    sharpe = result["sharpe"]
    mdd = result["max_drawdown"]
    if np.isfinite(sharpe):
        print(f"  Sharpe (annualised, walk-forward): {sharpe:.3f}")
    else:
        print("  Sharpe: NaN (insufficient data)")
    if np.isfinite(mdd):
        print(f"  Max drawdown:                       {mdd*100:.2f}%")
    else:
        print("  Max drawdown: NaN")
    print(f"  Trades evaluated: {result['n_trades']}")

    if not args.no_hmm and _hmm_available():
        print("\nHMM extension available. (Run with --no-hmm to skip)")

    print("\n----------------------------------------------------------------")
    print(" Framework: Roan (@RohOnChain). Installed as a Nico skill.")
    print(" Backtests are historical, not forward-looking.")
    print("----------------------------------------------------------------\n")

    # Save results to a JSON file for the Discord bot to read later
    output_file = Path(__file__).parent / "output.json"
    output_data = {
        "ticker": args.ticker,
        "transition_matrix": P.tolist(),
        "stationary_distribution": pi.tolist(),
        "backtest_sharpe": sharpe,
        "backtest_max_drawdown": mdd,
        "last_updated": pd.Timestamp.utcnow().isoformat(),
    }
    import json
    with open(output_file, "w") as f:
        json.dump(output_data, f, indent=2)
    print(f"  Results saved to {output_file}")

    # --- DCA Simulation ---
    if config.get('dca'):
        dca_cfg = config['dca']
        currency = dca_cfg.get('currency', 'USD')
        symbol = '$' if currency == 'USD' else '£'
        
        dca_strat = DCAStrategy(
            budget=dca_cfg['budget'],
            assets=dca_cfg['assets'],
            max_triggers=dca_cfg['max_triggers'],
            trigger_threshold=dca_cfg.get('trigger_threshold', -0.02)
        )

        print(f"\nRunning Red Day DCA Simulation...")
        print(f"  Budget: {symbol}{dca_cfg['budget']:,.2f} | Assets: {dca_cfg['assets']}")
        
        # Prepare data for DCA
        close_prices = close.copy()
        pct_changes = close_prices.pct_change()
        
        # Combine data into a DataFrame for easier processing
        df = pd.DataFrame({'close': close_prices, 'pct_change': pct_changes, 'regime': labels})
        df = df.dropna()

        # Run simulation
        for index, row in df.iterrows():
            dca_strat.process_bar(index, row, row['regime'], row['close'])

        summary = dca_strat.get_summary()
        print(f"  DCA Triggers Used: {summary['triggers_used']}/{dca_cfg['max_triggers']}")
        print(f"  Total Spent: {symbol}{summary['spent']:,.2f}")
        print(f"  Current Portfolio Value: {symbol}{summary['portfolio_value']:,.2f}")
        print(f"  Transactions Logged: {summary['transactions_count']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
