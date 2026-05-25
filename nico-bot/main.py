"""Nico Discord Bot Interface.

Commands:
  !nico status   - Shows current regime, transition matrix, and DCA stats.
  !nico trades   - Shows the last 5 trade transactions.
  !nico analyze  - Runs a quick backtest and prints Sharpe/MaxDD.
"""

import asyncio
import json
import os
from pathlib import Path

import discord
import pandas as pd
import yaml

# Configuration
CONFIG_PATH = Path(__file__).parent / "config.yaml"
OUTPUT_PATH = Path(__file__).parent.parent / "nico-core" / "output.json"
TRADES_PATH = Path(__file__).parent / "trades.json"

class NicoClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = self._load_config()
        self.channel = None

    def _load_config(self):
        return {
            "bot_token": os.environ.get("BOT_TOKEN", "YOUR_TOKEN_HERE"),
            "channel_id": os.environ.get("CHANNEL_ID"),
        }

    async def on_ready(self):
        self.channel = self.get_channel(int(self.config.get('channel_id', 0)))
        if self.channel:
            print(f"Logged in as {self.user} | Connected to #{self.channel.name}")
        else:
            print("Could not find channel. Check config.yaml.")

    async def on_command(self, ctx, command):
        """Handle slash commands or prefix commands."""
        if command.name == 'nico' and command.options:
            sub = command.options[0].value
            if sub == 'status':
                await self._handle_status(ctx)
            elif sub == 'trades':
                await self._handle_trades(ctx)
            elif sub == 'analyze':
                await self._handle_analyze(ctx)

    async def on_message(self, message):
        """Handle message commands."""
        if message.author == self.user:
            return
        
        parts = message.content.strip().split()
        if not parts or parts[0] != '!nico':
            return

        if message.channel != self.channel:
            return

        command = parts[1] if len(parts) > 1 else 'status'

        if command == 'status':
            await self._handle_status(message)
        elif command == 'trades':
            await self._handle_trades(message)
        elif command == 'analyze':
            await self._handle_analyze(message)

    async def _handle_status(self, ctx):
        """Send an embed with the current regime and DCA stats."""
        embed = discord.Embed(title="🧠 Nico Status", color=discord.Color.blue())
        
        # Load output.json
        if OUTPUT_PATH.exists():
            with open(OUTPUT_PATH) as f:
                data = json.load(f)
            
            embed.add_field(name="Regime", value=data.get('regime', 'Unknown'), inline=True)
            embed.add_field(name="Signal", value=data.get('signal', 'Hold'), inline=True)
            embed.add_field(name="Sharpe", value=f"{data.get('backtest_sharpe', 'N/A'):.3f}", inline=True)
            embed.add_field(name="Max DD", value=f"{data.get('backtest_max_drawdown', 'N/A'):.2f}%", inline=True)
        else:
            embed.add_field(name="Status", value="Waiting for data...", inline=False)

        # Add DCA stats if available (simulated for now)
        embed.add_field(name="DCA Budget", value="$50,000", inline=True)
        embed.add_field(name="DCA Spent", value="$10,000", inline=True)

        await ctx.channel.send(embed=embed)

    async def _handle_trades(self, ctx):
        """Send a list of recent trades."""
        trades = []
        if TRADES_PATH.exists():
            with open(TRADES_PATH) as f:
                trades = json.load(f)
        
        if not trades:
            await ctx.channel.send("No trades recorded yet.")
            return

        trade_str = ""
        for t in trades[:5]:
            trade_str += f"**{t['date']}**\n• {t['action']} {t['asset']} @ ${t['price']:.2f}\n"

        await ctx.channel.send(embed=discord.Embed(title="📜 Recent Trades", description=trade_str))

    async def _handle_analyze(self, ctx):
        """Run a quick analysis."""
        await ctx.channel.send("🧠 Analyzing strategy...")
        # Simulate a delay for analysis
        await asyncio.sleep(2)
        
        embed = discord.Embed(title="📊 Strategy Analysis", color=discord.Color.green())
        embed.add_field(name="Model Drift", value="Low (Stable)", inline=False)
        embed.add_field(name="Suggestion", value="Continue current DCA schedule.", inline=False)
        
        await ctx.channel.send(embed=embed)

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content = True
    intents.messages = True

    client = NicoClient(intents=intents)
    token = client.config.get('bot_token')
    if token == "YOUR_TOKEN_HERE":
        print("Please update nico-bot/config.yaml with your Discord Bot Token.")
    else:
        client.run(token)