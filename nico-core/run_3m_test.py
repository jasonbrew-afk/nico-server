"""3-Month Backtest with Improved Strategy (Laplace Smoothing + Volatility Filter)."""
import sys
import numpy as np
import pandas as pd
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from nico_core.data import fetch_ohlcv
from nico_core.regime import STATES, label_regimes, stationary_distribution, n_step_forecast

def build_transition_matrix_smoothing(labels: pd.Series, alpha: float = 1.0) -> np.ndarray:
    """MLE estimate with Laplace smoothing to avoid absolute 0/1 probabilities."""
    n = 3
    counts = np.zeros((n, n), dtype=float)
    arr = labels.to_numpy()
    for i in range(len(arr) - 1):
        counts[arr[i], arr[i + 1]] += 1
    
    # Laplace smoothing
    counts += alpha
    row_sums = counts.sum(axis=1, keepdims=True)
    return counts / row_sums

def signal_from_matrix(P: np.ndarray, current_state: int, alpha: float = 1.0) -> float:
    """Signal with Laplace smoothing: P(next=Bull|current) - P(next=Bear|current)."""
    return float(P[current_state, 2] - P[current_state, 0])

def walk_forward_improved(
    close: pd.Series,
    labels: pd.Series,
    train_window: int = 45,
    min_train: int = 30,
) -> dict:
    """Walk-forward with Laplace smoothing + volatility filter."""
    daily_returns = close.pct_change().dropna()
    common_index = labels.index.intersection(daily_returns.index)
    labels = labels.loc[common_index]
    daily_returns = daily_returns.loc[common_index]
    
    # Calculate volatility (rolling std)
    volatility = daily_returns.rolling(window=20).std()

    strategy_returns = []
    positions = []
    
    for t in range(min_train, len(labels) - 1):
        # Use recent lookback
        P_t = build_transition_matrix_smoothing(labels.iloc[max(0, t - train_window):t], alpha=1.5)
        current_state = int(labels.iloc[t])
        signal = signal_from_matrix(P_t, current_state, alpha=1.5)
        
        # Volatility filter: only trade if volatility is above 20-day avg
        vol_threshold = volatility.iloc[t] > volatility.iloc[max(0, t-60):t].mean() if t >= 60 else True
        
        # Simple sign position, but filtered by volatility
        if vol_threshold and abs(signal) > 0.1:  # Minimum conviction threshold
            position = float(np.sign(signal))
        else:
            position = 0.0
            
        next_day_return = float(daily_returns.iloc[t + 1])
        strategy_returns.append(position * next_day_return)
        positions.append(position)

    sr = np.array(strategy_returns, dtype=float)
    if sr.std(ddof=1) == 0 or not np.isfinite(sr.std(ddof=1)):
        sharpe = float("nan")
    else:
        sharpe = float(sr.mean() / sr.std(ddof=1) * np.sqrt(252))

    equity = (1.0 + sr).cumprod()
    running_max = np.maximum.accumulate(equity)
    drawdown = (equity - running_max) / running_max
    max_dd = float(drawdown.min()) if len(drawdown) else float("nan")
    
    # Count actual trades (positions != 0)
    n_trades = int(np.sum(np.abs(np.array(positions)) > 0))

    return {"sharpe": sharpe, "max_drawdown": max_dd, "n_trades": n_trades, "equity_curve": equity.tolist()}

def main():
    print("Fetching 3 months of data...")
    close, index = fetch_ohlcv("BTC-USD", years=1)
    close = close.tail(90)  # Last 3 months (~90 trading days)
    
    print(f"Data range: {index.min().date()} to {index.max().date()} ({len(close)} bars)")
    
    # Use shorter lookback for regime labeling
    labels = label_regimes(close, window=20, bull_threshold=0.03, bear_threshold=-0.03)
    
    # Run improved walk-forward
    result = walk_forward_improved(close, labels, train_window=30)
    
    sharpe = result["sharpe"]
    mdd = result["max_drawdown"]
    n_trades = result["n_trades"]
    equity = result["equity_curve"]
    
    initial_capital = 1000.0
    final_capital = initial_capital * equity[-1] if equity else initial_capital
    pnl = final_capital - initial_capital
    pnl_pct = (pnl / initial_capital) * 100
    
    print(f"\n--- 3-Month Backtest Results (BTC-USD) ---")
    print(f"  Initial Capital:     ${initial_capital:,.2f}")
    print(f"  Final Capital:       ${final_capital:,.2f}")
    print(f"  Net PnL:             ${pnl:,.2f} ({pnl_pct:+.2f}%)")
    print(f"  Sharpe Ratio:        {sharpe:.3f}" if np.isfinite(sharpe) else "  Sharpe Ratio:        NaN")
    print(f"  Max Drawdown:        {mdd*100:.2f}%" if np.isfinite(mdd) else "  Max Drawdown:        NaN")
    print(f"  Active Trade Days:   {n_trades}")
    print(f"  Strategy: Laplace Smoothing (α=1.5) + Volatility Filter")
    print(f"------------------------------------------------\n")
    
    # Save to JSON for bot
    output = {
        "ticker": "BTC-USD",
        "period": "3_months",
        "initial_capital": initial_capital,
        "final_capital": final_capital,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "sharpe": sharpe,
        "max_drawdown": mdd,
        "n_trades": n_trades,
        "equity_curve": equity,
        "timestamp": pd.Timestamp.utcnow().isoformat()
    }
    with open(Path(__file__).parent / "output_3m.json", "w") as f:
        import json
        json.dump(output, f, indent=2)
    print("Results saved to output_3m.json")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
