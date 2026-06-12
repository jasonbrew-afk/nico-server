"""Live DCA runner — connects strategy to Alpaca execution.

This is the main module for live trading. It:
1. Fetches real-time data from Yahoo Finance
2. Runs DCA logic on the latest bar
3. Executes trades via Alpaca API
4. Logs everything and updates state

Usage:
    python run_live.py [--ticker BTC-USD] [--config config.yaml] [--dry-run]

    --dry-run: Simulate trades without sending to Alpaca (recommended for first run)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
import pandas as pd


def _load_dotenv():
    """Local-dev: load the nearest .env (walk up to repo root) without overriding
    real env vars. No-op if absent (e.g. on Railway, where vars come from the dashboard)."""
    here = Path(__file__).resolve()
    for d in (here.parent, *here.parents):
        f = d / ".env"
        if f.is_file():
            for line in f.read_text().splitlines():
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, v = s.split("=", 1)
                    v = v.strip()
                    q = v[:1]
                    if q in ("'", '"'):
                        v = v[1:].split(q, 1)[0]
                    elif " #" in v:
                        v = v.split(" #", 1)[0].rstrip()
                    os.environ.setdefault(k.strip(), v)
            break


_load_dotenv()

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from nico_core.data import fetch_ohlcv
from nico_core.dca import DCAStrategy
from nico_core.alpaca_execution import AlpacaExecution
from nico_core import state_client
from platforms.robinhood.mcp_adapter import get_execution_backend
from platforms.robinhood import approval as rh_approval

EXECUTION_BACKEND = os.environ.get("NICO_EXECUTION_BACKEND", "alpaca").lower()


# ── State Management ──────────────────────────────────────────────

STATE_FILE = Path(__file__).parent / "live_state.json"


def load_state() -> dict:
    """Load previous trading state."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "triggers_used": 0,
        "last_run": None,
        "transactions": [],
    }


def save_state(state: dict) -> None:
    """Save trading state."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Main Runner ───────────────────────────────────────────────────

def run_live(
    ticker: str = "BTC-USD",
    config_path: str = "config.yaml",
    dry_run: bool = False,
) -> None:
    """Run the DCA strategy and execute trades."""

    # Load config
    config = {}
    if Path(config_path).exists():
        with open(config_path) as f:
            config = yaml.safe_load(f)

    dca_cfg = config.get("dca", {})
    alpaca_cfg = config.get("alpaca", {})

    # Initialize execution backend (Alpaca by default; Robinhood MCP when
    # NICO_EXECUTION_BACKEND=robinhood_mcp). The factory only imports/needs the
    # creds for whichever backend is selected.
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")

    if EXECUTION_BACKEND == "alpaca" and (not api_key or not secret_key):
        print("ERROR: ALPACA_API_KEY and ALPACA_SECRET_KEY env vars required")
        print("  Set them from your Alpaca dashboard:")
        print("  export ALPACA_API_KEY=your_key")
        print("  export ALPACA_SECRET_KEY=your_secret")
        sys.exit(1)

    base_url = alpaca_cfg.get("base_url", "https://paper-api.alpaca.markets")
    is_paper = alpaca_cfg.get("paper", True)

    execution = get_execution_backend(
        api_key=api_key,
        secret_key=secret_key,
        base_url=base_url,
        paper=is_paper,
    )
    print(f"  Execution backend: {EXECUTION_BACKEND}")

    print(f"\n{'='*70}")
    print(f"  Nico DCA Runner — {'PAPER' if is_paper else 'LIVE'} mode")
    print(f"  Ticker: {ticker}")
    print(f"  Dry Run: {dry_run}")
    print(f"{'='*70}\n")

    # Fetch latest data
    print("Fetching market data...")
    close, index = fetch_ohlcv(ticker, 1)  # Just last 30 days for trigger check
    if len(close) < 2:
        print("ERROR: Insufficient data")
        return

    # Get last bar
    current_price = float(close.iloc[-1])
    daily_return = float(close.pct_change().iloc[-1])
    timestamp = index[-1]

    print(f"Current price: ${current_price:.2f}")
    print(f"Daily return: {daily_return*100:.2f}%")
    print(f"Last bar: {timestamp.date()}\n")

    # Load state. Shared Postgres store (via nico-server) is the source of truth
    # when configured; otherwise fall back to the legacy local live_state.json.
    state = load_state()
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    # Real-money (gated) backends fail closed if the shared state is unreadable;
    # the legacy Alpaca path falls back to local state so a state-API hiccup never
    # blocks normal trading.
    gated_backend = rh_approval.approval_required(EXECUTION_BACKEND)
    remote = None
    if state_client.enabled():
        try:
            remote = state_client.get_state()
        except Exception as e:
            if gated_backend:
                print(f"ERROR: could not read shared state API (refusing real-money "
                      f"trade rather than risk double-spend): {e}\n")
                return
            print(f"WARN: shared state API unreadable; falling back to local state: {e}\n")

    if remote is not None:
        # last_run = last placement; last_emitted = last intent sent for approval.
        # Skip if either happened today, so reruns don't double-place or re-emit.
        if remote.get("last_run") == today_str or remote.get("last_emitted") == today_str:
            print(f"Already acted today (run={remote.get('last_run')}, "
                  f"emitted={remote.get('last_emitted')}). Skipping.\n")
            return
    elif state.get("last_run") == today_str:
        print(f"Already traded today ({state.get('last_run')}). Skipping.\n")
        return

    # Initialize DCA strategy
    assets = dca_cfg.get("assets", ["BTC/USD", "ETH/USD", "SPY"])

    dca = DCAStrategy(
        budget=dca_cfg.get("budget", 1500.0),
        assets=assets,
        max_triggers=dca_cfg.get("max_triggers", 30),
        trigger_threshold=dca_cfg.get("trigger_threshold", -0.015),
        allocation_per_asset=dca_cfg.get("allocation_per_asset", 0.3333),
        min_trade_usd=dca_cfg.get("min_trade_usd", 5.0),
    )

    # Rehydrate progress from the shared source of truth so budget math is correct.
    if remote is not None:
        dca.state.triggers_used = int(remote.get("triggers_used", 0))
        dca.state.spent = float(remote.get("spent", 0.0))

    # Check if red day trigger is active
    is_red_day = daily_return < dca.state.trigger_threshold

    print(f"Red day trigger: {'YES' if is_red_day else 'NO'} (threshold: {dca.state.trigger_threshold*100:.1f}%)")
    print(f"Triggers used: {dca.state.triggers_used}/{dca.state.max_triggers}\n")

    if is_red_day and dca.state.triggers_used < dca.state.max_triggers:
        allocation = dca.state.remaining_budget * dca.state.allocation_per_asset

        if allocation < dca.state.min_trade_usd:
            print(f"Allocation too small (${allocation:.2f} < ${dca.state.min_trade_usd}). Skipping.\n")
            return

        print(f"Allocation per asset: ${allocation:.2f}\n")

        # Get Alpaca position info
        portfolio = execution.get_portfolio()
        print(f"Current portfolio ({EXECUTION_BACKEND}):")
        print(f"  Cash: ${portfolio['cash']:,.2f}")
        print(f"  Portfolio value: ${portfolio['portfolio_value']:,.2f}")
        print(f"  Buying power: ${portfolio['buying_power']:,.2f}\n")

        if portfolio['cash'] < allocation * len(assets):
            print(f"ERROR: Insufficient buying power.")
            print(f"  Need: ${allocation * len(assets):,.2f}")
            print(f"  Have: ${portfolio['cash']:,.2f}\n")
            return

        # On the Robinhood MCP backend (real money), orders are gated: instead of
        # placing, we send each one to Imago for Jason's approval and park here.
        # The always-on bot places approved orders later. See platforms/robinhood/approval.py.
        gated = rh_approval.approval_required(EXECUTION_BACKEND)
        parked = False

        # Execute (or park) trades
        for asset in assets:
            # Map Yahoo ticker to Alpaca ticker
            alpaca_symbol = ticker.replace("-", "/") if "-" in ticker else asset
            if asset == "SPY":
                alpaca_symbol = "SPY"
            else:
                alpaca_symbol = asset  # Already in Alpaca format

            qty = allocation / current_price
            print(f"  BUY {asset}: {qty:.6f} units @ ${current_price:.2f} = ${allocation:.2f}")

            if dry_run:
                print(f"    [DRY RUN — not sending]")
                continue

            if gated:
                # Park & release: request approval, do NOT place here.
                order_req = rh_approval.PendingOrder(
                    symbol=alpaca_symbol,
                    side="buy",
                    qty=qty,
                    notional_usd=allocation,
                    price=current_price,
                    trigger_number=dca.state.triggers_used + 1,
                    rationale=rh_approval.build_rationale(
                        asset=asset,
                        daily_return=daily_return,
                        threshold=dca.state.trigger_threshold,
                        trigger_number=dca.state.triggers_used + 1,
                        max_triggers=dca.state.max_triggers,
                        notional=allocation,
                        remaining_budget=dca.state.remaining_budget - allocation,
                    ),
                )
                # Persist the intent to the shared source of truth FIRST, so the
                # row exists when the bot later reports the fill. If the state API
                # is configured but unreachable, refuse to trade (fail-closed).
                if state_client.enabled():
                    try:
                        state_client.post_pending({
                            "request_id": order_req.request_id,
                            "symbol": order_req.symbol,
                            "side": order_req.side,
                            "qty": order_req.qty,
                            "notional_usd": order_req.notional_usd,
                            "price": order_req.price,
                            "rationale": order_req.rationale,
                            "trigger_number": order_req.trigger_number,
                        })
                    except Exception as e:
                        print(f"    ❌ Could not persist pending order to state API — NOT trading: {e}")
                        return
                try:
                    req_id = rh_approval.request_approval(order_req)
                    parked = True
                    print(f"    📨 Sent to Imago for approval (req {req_id}) — placement deferred")
                except rh_approval.ApprovalNotConfigured as e:
                    print(f"    ❌ Approval not configured — NOT trading: {e}")
                    return
                continue

            try:
                order = execution.buy(
                    symbol=alpaca_symbol,
                    qty=qty,
                    reason=f"DCA trigger #{dca.state.triggers_used + 1}",
                )
                if order and order.status == "filled":
                    print(f"    ✅ Order filled: {order.order_id}")
                else:
                    print(f"    ⚠️ Order {order.status}: {order.order_id}")
            except Exception as e:
                print(f"    ❌ Error: {e}")

        if gated and parked:
            # Park & release: DCA budget/trigger state advances on actual
            # PLACEMENT (bot side, post-approval), not on emission — so an
            # unapproved request never decrements the budget. We only stamp
            # last_run to avoid re-emitting duplicate requests on same-day reruns.
            state["last_run"] = today_str
            state.setdefault("pending_approvals", []).append({
                "date": today_str, "ticker": ticker, "assets": assets,
            })
            save_state(state)
            print(f"\n📨 {len(assets)} order(s) sent to Imago — awaiting approval. "
                  f"DCA state will advance when the bot places them.\n")
            return

        # Update DCA state (direct-placement backends only)
        for _ in assets:
            dca.state.spent += allocation
            dca.state.triggers_used += 1

        # Save state
        state["last_run"] = today_str
        state["triggers_used"] = dca.state.triggers_used
        state["transactions"].append({
            "date": today_str,
            "ticker": ticker,
            "price": current_price,
            "trigger_number": dca.state.triggers_used,
            "allocation": allocation,
            "assets": assets,
        })
        save_state(state)

        print(f"\n✅ DCA trade executed! (Trigger #{dca.state.triggers_used})")
        print(f"   Remaining budget: ${dca.state.remaining_budget:,.2f}\n")
    else:
        print("No trade today. Waiting for red day...\n")


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_live")
    parser.add_argument("--ticker", default="BTC-USD", help="Asset ticker (Yahoo format)")
    parser.add_argument("--config", default=Path(__file__).parent / "config.yaml", help="Config path")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without sending orders")
    args = parser.parse_args()

    try:
        run_live(
            ticker=args.ticker,
            config_path=args.config,
            dry_run=args.dry_run,
        )
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
