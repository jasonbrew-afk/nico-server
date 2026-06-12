"""Data fetching and cleaning module for Nico Core."""

from __future__ import annotations

import time
from typing import Tuple

import numpy as np
import pandas as pd


def fetch_ohlcv(ticker: str, years: int) -> Tuple[pd.Series, pd.DatetimeIndex]:
    """
    Fetch daily OHLCV data using yfinance.

    Returns a cleaned 'Close' series and the DatetimeIndex.
    """
    import yfinance as yf

    end = pd.Timestamp.utcnow().normalize()
    start = end - pd.DateOffset(years=years)

    for attempt in (1, 2):
        try:
            df = yf.download(
                ticker,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                progress=False,
                auto_adjust=True,
            )
        except Exception as exc:
            print(f"  ! yfinance error on attempt {attempt}: {exc}")
            df = pd.DataFrame()

        if not df.empty:
            # Handle MultiIndex columns from some yfinance versions
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            # Ensure we get a Series, not a DataFrame (single-column edge case)
            close = df["Close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            return close.dropna(), df.index

        if attempt == 1:
            print("  ! yfinance returned empty data — retrying in 30s.")
            time.sleep(30)

    raise RuntimeError(
        f"yfinance returned empty data for {ticker} after retry. "
        "Yahoo may be rate-limiting. Try again in a few minutes."
    )
