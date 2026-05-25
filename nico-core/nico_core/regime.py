"""Observable Markov Regime Model.

Labels each day Bull (1), Bear (-1), or Sideways (0) using a rolling
return threshold, then builds a 3x3 transition matrix via MLE counting,
solves for the stationary distribution, and runs a walk-forward backtest.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

STATES = ["Bear", "Sideways", "Bull"]  # index 0, 1, 2


def label_regimes(close: pd.Series, window: int = 20, bull_threshold: float = 0.05, bear_threshold: float = -0.05) -> pd.Series:
    """Label each day as Bull / Bear / Sideways from rolling log-return.

    Bull   : log-return > bull_threshold
    Bear   : log-return < bear_threshold
    Sideways: otherwise
    """
    log_ret = np.log(close / close.shift(window))
    labels = pd.Series(1, index=close.index, dtype=int)  # default Sideways
    labels[log_ret > bull_threshold] = 2  # Bull
    labels[log_ret < bear_threshold] = 0  # Bear
    return labels.dropna()


def build_transition_matrix(labels: pd.Series) -> np.ndarray:
    """MLE estimate of the 3x3 transition matrix from a sequence of labels."""
    n = 3
    counts = np.zeros((n, n), dtype=float)
    arr = labels.to_numpy()
    for i in range(len(arr) - 1):
        counts[arr[i], arr[i + 1]] += 1
    row_sums = counts.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1.0  # avoid divide-by-zero on empty rows
    return counts / row_sums


def stationary_distribution(P: np.ndarray) -> np.ndarray:
    """Left eigenvector of P with eigenvalue 1, normalised to sum to 1."""
    eigvals, eigvecs = np.linalg.eig(P.T)
    # Find the eigenvector closest to eigenvalue 1
    idx = np.argmin(np.abs(eigvals - 1.0))
    vec = np.real(eigvecs[:, idx])
    vec = np.abs(vec)
    return vec / vec.sum()


def n_step_forecast(P: np.ndarray, n: int) -> np.ndarray:
    """Chapman-Kolmogorov: P^n is the n-step transition matrix."""
    return np.linalg.matrix_power(P, n)


def signal_from_matrix(P: np.ndarray, current_state: int) -> float:
    """Signed signal: P(next=Bull|current) - P(next=Bear|current).

    Positive -> long, negative -> short, magnitude -> conviction.
    """
    return float(P[current_state, 2] - P[current_state, 0])


def walk_forward_backtest(
    close: pd.Series,
    labels: pd.Series,
    min_train: int = 252,
) -> dict:
    """Walk-forward: at each day t, fit the matrix on labels up to t-1,
    derive the signal from the current state, hold for one day, score.

    No lookahead. No tuning.
    """
    daily_returns = close.pct_change().dropna()
    common_index = labels.index.intersection(daily_returns.index)
    labels = labels.loc[common_index]
    daily_returns = daily_returns.loc[common_index]

    if len(labels) < min_train + 30:
        return {"sharpe": float("nan"), "max_drawdown": float("nan"), "n_trades": 0}

    strategy_returns = []
    for t in range(min_train, len(labels) - 1):
        P_t = build_transition_matrix(labels.iloc[:t])
        current_state = int(labels.iloc[t])
        signal = signal_from_matrix(P_t, current_state)
        position = float(np.sign(signal))  # +1 / 0 / -1 — simple sign
        next_day_return = float(daily_returns.iloc[t + 1])
        strategy_returns.append(position * next_day_return)

    sr = np.array(strategy_returns, dtype=float)
    if sr.std(ddof=1) == 0 or not np.isfinite(sr.std(ddof=1)):
        sharpe = float("nan")
    else:
        sharpe = float(sr.mean() / sr.std(ddof=1) * np.sqrt(252))

    equity = (1.0 + sr).cumprod()
    running_max = np.maximum.accumulate(equity)
    drawdown = (equity - running_max) / running_max
    max_dd = float(drawdown.min()) if len(drawdown) else float("nan")

    return {"sharpe": sharpe, "max_drawdown": max_dd, "n_trades": int(len(sr))}
