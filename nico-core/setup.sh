#!/bin/bash
# Nico DCA Setup Script
# 
# This script helps you set up the Nico DCA trading system.

set -e

echo ""
echo "===================================================================="
echo "  Nico DCA Setup"
echo "===================================================================="
echo ""

# Check if Alpaca env vars are set
if [ -z "$ALPACA_API_KEY" ] || [ -z "$ALPACA_SECRET_KEY" ]; then
    echo "ALPACA_API_KEY and ALPACA_SECRET_KEY not found."
    echo ""
    echo "To get your keys:"
    echo "1. Go to https://app.alpaca.markets/paper/trade"
    echo "2. Click Profile → API Keys"
    echo "3. Copy Paper Trading API Key and Secret Key"
    echo ""
    echo "Then run:"
    echo "  export ALPACA_API_KEY=your_paper_key"
    echo "  export ALPACA_SECRET_KEY=your_paper_secret"
    echo ""
    echo "Or add to ~/.bashrc ~/.zshrc for persistence:"
    echo "  echo 'export ALPACA_API_KEY=your_paper_key' >> ~/.bashrc"
    echo "  echo 'export ALPACA_SECRET_KEY=your_paper_secret' >> ~/.bashrc"
    echo ""
    exit 1
fi

echo "Alpaca credentials found."
echo ""

# Test connection
echo "Testing Alpaca connection..."
cd "$(dirname "$0")"
python3 test_alpaca.py

echo ""
echo "Setup complete!"
echo ""
echo "Next steps:"
echo "1. Fund your Alpaca account (minimum \$25 for paper trading)"
echo "2. Run a dry-run test: python3 run_live.py --dry-run"
echo "3. Execute live trades: python3 run_live.py"
echo ""
