import json
from pathlib import Path

output_data = {
    "ticker": "BTC-USD",
    "regime": "Bull",
    "signal": "Long",
    "transition_matrix": [[0.84, 0.15, 0.00], [0.11, 0.77, 0.11], [0.00, 0.13, 0.86]],
    "stationary_distribution": [0.27, 0.39, 0.32],
    "backtest_sharpe": -0.315,
    "backtest_max_drawdown": -0.76,
    "last_updated": "2026-05-24T17:30:00"
}

output_file = Path(__file__).parent / "output.json"
with open(output_file, "w") as f:
    json.dump(output_data, f, indent=2)
print(f"Sample data written to {output_file}")