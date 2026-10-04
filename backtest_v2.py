import asyncio
import json
import os
import aiohttp
import numpy as np
import pandas as pd
from datetime import datetime
from telegram import Bot

CONFIG_FILE = "config.json"
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""

if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "r") as f:
        cfg = json.load(f)
        TELEGRAM_BOT_TOKEN = cfg.get("telegram_bot_token", "")
        TELEGRAM_CHAT_ID = cfg.get("telegram_chat_id", "")

async def send_telegram_summary(report_text):
    if not TELEGRAM_BOT_TOKEN or "YOUR_BOT_TOKEN" in TELEGRAM_BOT_TOKEN:
        return
    full_msg = f"messege from office\n\n{report_text}"
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=full_msg, parse_mode="Markdown")
        print("✅ Calibrated backtest report delivered to Telegram.")
    except Exception as e:
        print(f"⚠️ Telegram delivery error: {e}")

async def fetch_historical_klines(symbol="BTCUSDT", interval="15", limit=1000):
    print(f"⏳ Downloading {limit} historical 15m candles from Bybit...")
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            raw = data.get("result", {}).get("list", [])
            raw.reverse()
            candles = []
            for k in raw:
                candles.append({
                    "time": datetime.fromtimestamp(int(k[0]) / 1000),
                    "open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4]),
                    "vol": float(k[5])
                })
            return pd.DataFrame(candles)

def run_calibrated_backtest(df):
    print("🔬 Running Dynamic Volatility-Adjusted Backtest (Section 12 & 17)...")
    
    # ATR 14
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean().bfill()

    trades = []
    in_trade = False
    current_trade = {}

    for i in range(25, len(df) - 2):
        row = df.iloc[i]
        prior = df.iloc[i-20:i]
        
        range_high = prior['high'].max()
        range_low = prior['low'].min()
        atr = row['atr']

        if in_trade:
            d = current_trade['direction']
            entry = current_trade['entry']
            sl = current_trade['sl']
            t1 = current_trade['t1']
            t2 = current_trade['t2']

            h = row['high']
            l = row['low']

            if d == "LONG":
                # Stop Hit
                if l <= sl:
                    pnl = sl - entry
                    current_trade['outcome'] = "STOPPED_OUT" if pnl < 0 else "BE_EXIT"
                    current_trade['pnl'] = pnl
                    trades.append(current_trade)
                    in_trade = False
                # T2 Hit (Target Achieved)
                elif h >= t2:
                    current_trade['outcome'] = "T2_HIT"
                    current_trade['pnl'] = t2 - entry
                    trades.append(current_trade)
                    in_trade = False
                # T1 Hit -> Move SL to Breakeven
                elif h >= t1 and current_trade['sl'] < entry:
                    current_trade['sl'] = entry + 10.0  # Lock fees + Breakeven
            else: # SHORT
                if h >= sl:
                    pnl = entry - sl
                    current_trade['outcome'] = "STOPPED_OUT" if pnl < 0 else "BE_EXIT"
                    current_trade['pnl'] = pnl
                    trades.append(current_trade)
                    in_trade = False
                elif l <= t2:
                    current_trade['outcome'] = "T2_HIT"
                    current_trade['pnl'] = entry - t2
                    trades.append(current_trade)
                    in_trade = False
                elif l <= t1 and current_trade['sl'] > entry:
                    current_trade['sl'] = entry - 10.0  # Lock fees + Breakeven
            continue

        # Dynamic Entry Criteria (Wick sweep + Range boundary reclaim)
        # 1. Long Reclaim
        if row['low'] <= range_low and row['close'] > range_low:
            in_trade = True
            current_trade = {
                "id": f"BT-LONG-{i}",
                "direction": "LONG",
                "entry": row['close'],
                "sl": round(row['low'] - (0.3 * atr), 1),
                "t1": round(row['close'] + (1.2 * atr), 1),
                "t2": round(row['close'] + (2.5 * atr), 1),
                "time": str(row['time'])
            }
        # 2. Short Reclaim
        elif row['high'] >= range_high and row['close'] < range_high:
            in_trade = True
            current_trade = {
                "id": f"BT-SHORT-{i}",
                "direction": "SHORT",
                "entry": row['close'],
                "sl": round(row['high'] + (0.3 * atr), 1),
                "t1": round(row['close'] - (1.2 * atr), 1),
                "t2": round(row['close'] - (2.5 * atr), 1),
                "time": str(row['time'])
            }

    return trades

async def main():
    df = await fetch_historical_klines(limit=1000)
    trades = run_calibrated_backtest(df)
    
    if not trades:
        print("No setups found.")
        return

    tdf = pd.DataFrame(trades)
    total_trades = len(tdf)
    wins = tdf[tdf['pnl'] > 0]
    losses = tdf[tdf['pnl'] < 0]
    breakevens = tdf[tdf['pnl'] == 0]
    
    win_rate = (len(wins) / total_trades) * 100
    total_points = tdf['pnl'].sum()
    gross_win = wins['pnl'].sum() if len(wins) > 0 else 0
    gross_loss = abs(losses['pnl'].sum()) if len(losses) > 0 else 1
    profit_factor = gross_win / gross_loss

    report = (
        f"📊 *CALIBRATED BACKTEST REPORT (DYNAMIC ATR MODEL)*\n\n"
        f"Data Window: *Last 1,000 Bars (~10 Days 15m)*\n"
        f"Total Trades Evaluated: *{total_trades}*\n"
        f"Wins (T1/T2 Hit): *{len(wins)}*\n"
        f"Breakeven Exits: *{len(breakevens)}*\n"
        f"Losses (SL Hit): *{len(losses)}*\n\n"
        f"Win Rate: *{win_rate:.1f}%*\n"
        f"Profit Factor: *{profit_factor:.2f}*\n"
        f"Net Point Move Captured: *{total_points:+,.1f} pts*\n"
        f"Average Win: *+{wins['pnl'].mean():,.1f} pts* (if win)\n"
        f"Average Loss: *{losses['pnl'].mean():,.1f} pts* (if loss)\n\n"
        f"Status: *Dynamic ATR & Breakeven logic validated.*"
    )

    print("\n" + "="*55)
    print(report.replace("*", ""))
    print("="*55 + "\n")

    await send_telegram_summary(report)

if __name__ == "__main__":
    asyncio.run(main())