import asyncio
import json
import sqlite3
import sys
import numpy as np
import websockets
import aiohttp
from datetime import datetime

# ==================== CONFIGURATION & DATABASE ====================
SYMBOL = "BTCUSDT"
DB_NAME = "trades_vault.db"

candles_1m = []
candles_15m = []
cvd_current_candle = 0.0

active_trade = None

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS signals (
        trade_id TEXT PRIMARY KEY,
        timestamp TEXT,
        direction TEXT,
        stage TEXT,
        setup_score INTEGER,
        entry_price REAL,
        invalidation REAL,
        t1 REAL,
        t2 REAL,
        t3 REAL,
        extended REAL,
        potential_move TEXT,
        status TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS trade_events (
        event_id TEXT PRIMARY KEY,
        trade_id TEXT,
        event_type TEXT,
        price REAL,
        timestamp TEXT
    )''')
    conn.commit()
    conn.close()

def log_trade_db(trade_data):
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
            trade_data["id"], trade_data["time"], trade_data["direction"], trade_data["stage"],
            trade_data["score"], trade_data["entry"], trade_data["sl"], trade_data["t1"],
            trade_data["t2"], trade_data["t3"], trade_data["extended"], trade_data["potential"],
            trade_data["status"]
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB Error] {e}")

# ==================== ADVANCED TECHNICAL ENGINES ====================
def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 100.0
    highs = np.array([c["high"] for c in candles[-period-1:]])
    lows = np.array([c["low"] for c in candles[-period-1:]])
    closes = np.array([c["close"] for c in candles[-period-1:]])
    
    tr1 = highs[1:] - lows[1:]
    tr2 = np.abs(highs[1:] - closes[:-1])
    tr3 = np.abs(lows[1:] - closes[:-1])
    tr = np.maximum(tr1, np.maximum(tr2, tr3))
    return float(np.mean(tr[-period:]))

def detect_regime(candles_15m):
    if len(candles_15m) < 20:
        return "UNCLEAR REGIME"
    closes = np.array([c["close"] for c in candles_15m[-20:]])
    x = np.arange(len(closes))
    slope, _ = np.polyfit(x, closes, 1)
    
    highs = np.array([c["high"] for c in candles_15m[-20:]])
    lows = np.array([c["low"] for c in candles_15m[-20:]])
    range_span = (np.max(highs) - np.min(lows)) / closes[-1]
    
    if slope > 3.0:
        return "BULLISH TREND"
    elif slope < -3.0:
        return "BEARISH TREND"
    elif range_span < 0.012:
        return "SQUEEZE / COMPRESSION"
    else:
        return "RANGE CONSOLIDATION"

def point_move_classifier(atr, regime):
    if "SQUEEZE" in regime:
        return "EXTENDED RUN (5,000 - 10,000+ pts)", 90
    elif "TREND" in regime:
        return "LARGE MOVE (2,000 - 5,000 pts)", 75
    elif atr > 120:
        return "MEDIUM MOVE (1,000 - 2,000 pts)", 60
    else:
        return "SMALL MOVE (500 - 1,000 pts)", 45

# ==================== STAGES 1 TO 6 DETECTION ====================
def evaluate_market_stages(candles_1m, candles_15m):
    if len(candles_1m) < 30:
        return "STAGE 0: SYNCING DATA", 0, {}
    
    recent_1m = candles_1m[-30:]
    closes = np.array([c["close"] for c in recent_1m])
    cvds = np.array([c["cvd"] for c in recent_1m])
    highs = np.array([c["high"] for c in recent_1m])
    lows = np.array([c["low"] for c in recent_1m])
    volumes = np.array([c["vol"] for c in recent_1m])
    
    curr_close = closes[-1]
    curr_high = highs[-1]
    curr_low = lows[-1]
    
    atr = calculate_atr(candles_1m, 14)
    regime = detect_regime(candles_15m)
    move_desc, move_score = point_move_classifier(atr, regime)
    
    range_high = np.max(highs[:-5])
    range_low = np.min(lows[:-5])
    avg_vol = np.mean(volumes[:-1])
    is_volume_spike = volumes[-1] > (1.8 * avg_vol)
    
    # 1. Delta Divergence Check (Leading Early Signals)
    bullish_cvd_divergence = (curr_low <= range_low) and (cvds[-1] > cvds[-4])
    bearish_cvd_divergence = (curr_high >= range_high) and (cvds[-1] < cvds[-4])
    
    # 2. Breakout Reclaim Logic
    bullish_reclaim = (lows[-2] < range_low) and (curr_close > range_low)
    bearish_reclaim = (highs[-2] > range_high) and (curr_close < range_high)

    meta = {
        "price": curr_close,
        "atr": round(atr, 2),
        "regime": regime,
        "range_low": round(range_low, 2),
        "range_high": round(range_high, 2),
        "potential": move_desc,
        "move_score": move_score,
        "cvd": round(cvds[-1], 2)
    }

    # --- STAGE 2: EARLY LONG SETUP ---
    if (bullish_cvd_divergence or bullish_reclaim) and (curr_close >= range_low):
        score = 80 if bullish_cvd_divergence and bullish_reclaim else 72
        meta["direction"] = "LONG"
        meta["entry_zone"] = f"${round(range_low, 1)} - ${round(range_low + (0.3 * atr), 1)}"
        meta["sl"] = round(curr_low - (0.35 * atr), 1)
        meta["t1"] = round(curr_close + (1.2 * atr), 1)
        meta["t2"] = round(curr_close + (2.5 * atr), 1)
        meta["t3"] = round(curr_close + (4.5 * atr), 1)
        meta["extended"] = round(curr_close + (8.0 * atr), 1)
        return "STAGE 2: EARLY LONG SETUP (ABSORPTION)", score, meta

    # --- STAGE 2: EARLY SHORT SETUP ---
    elif (bearish_cvd_divergence or bearish_reclaim) and (curr_close <= range_high):
        score = 80 if bearish_cvd_divergence and bearish_reclaim else 72
        meta["direction"] = "SHORT"
        meta["entry_zone"] = f"${round(range_high - (0.3 * atr), 1)} - ${round(range_high, 1)}"
        meta["sl"] = round(curr_high + (0.35 * atr), 1)
        meta["t1"] = round(curr_close - (1.2 * atr), 1)
        meta["t2"] = round(curr_close - (2.5 * atr), 1)
        meta["t3"] = round(curr_close - (4.5 * atr), 1)
        meta["extended"] = round(curr_close - (8.0 * atr), 1)
        return "STAGE 2: EARLY SHORT SETUP (DISTRIBUTION)", score, meta

    # --- STAGE 3: MOMENTUM CONFIRMATION / BREAKOUT ---
    elif curr_close > range_high and is_volume_spike:
        meta["direction"] = "LONG"
        meta["entry_zone"] = f"${round(range_high, 1)} - ${round(curr_close, 1)}"
        meta["sl"] = round(range_high - (0.5 * atr), 1)
        meta["t1"] = round(curr_close + (1.5 * atr), 1)
        meta["t2"] = round(curr_close + (3.0 * atr), 1)
        meta["t3"] = round(curr_close + (6.0 * atr), 1)
        meta["extended"] = round(curr_close + (10.0 * atr), 1)
        return "STAGE 3: MOMENTUM BREAKOUT CONFIRMED", 85, meta

    # --- STAGE 1: ACCUMULATION / DISTRIBUTION WATCH ---
    elif (curr_close <= range_low + (0.25 * atr)) or (curr_close >= range_high - (0.25 * atr)):
        return "STAGE 1: BOUNDARY PRESSURE WATCH", 55, meta

    return f"STAGE 0: {regime}", 35, meta

# ==================== POSITION LIFECYCLE MONITOR ====================
def monitor_trade_lifecycle(curr_price):
    global active_trade
    if not active_trade:
        return

    d = active_trade["direction"]
    entry = active_trade["entry"]
    sl = active_trade["sl"]
    t1 = active_trade["t1"]
    t2 = active_trade["t2"]
    t3 = active_trade["t3"]
    state = active_trade["stage_state"]
    
    pnl = (curr_price - entry) if d == "LONG" else (entry - curr_price)
    pnl_pct = (pnl / entry) * 100

    print(f"\n[LIVE POSITION MONITOR] ID: {active_trade['id']} | {d} @ ${entry:,.1f}")
    print(f"Current: ${curr_price:,.1f} | PnL: {pnl:+,.1f} pts ({pnl_pct:+.2f}%) | Active SL: ${sl:,.1f}")

    # 1. Stop Loss Hit
    if (d == "LONG" and curr_price <= sl) or (d == "SHORT" and curr_price >= sl):
        print("🛑 POSITION CLOSED: Hard Invalidation / Stop Loss Hit.")
        active_trade["status"] = "STOPPED_OUT"
        log_trade_db(active_trade)
        active_trade = None
        return

    # 2. Target 3 Hit
    if ((d == "LONG" and curr_price >= t3) or (d == "SHORT" and curr_price <= t3)) and state < 3:
        print("🎯 TARGET 3 HIT! Massive move captured. Secure 80% profit. Trail stop aggressively.")
        active_trade["stage_state"] = 3
        active_trade["sl"] = t2
        active_trade["status"] = "T3_HIT"
        log_trade_db(active_trade)

    # 3. Target 2 Hit
    elif ((d == "LONG" and curr_price >= t2) or (d == "SHORT" and curr_price <= t2)) and state < 2:
        print("🎯 TARGET 2 HIT! Book 50% profit. Trailing stop shifted to T1.")
        active_trade["stage_state"] = 2
        active_trade["sl"] = t1
        active_trade["status"] = "T2_HIT"
        log_trade_db(active_trade)

    # 4. Target 1 Hit
    elif ((d == "LONG" and curr_price >= t1) or (d == "SHORT" and curr_price <= t1)) and state < 1:
        print("🎯 TARGET 1 HIT! Move Stop Loss to BREAKEVEN. Trade is now zero risk.")
        active_trade["stage_state"] = 1
        active_trade["sl"] = entry
        active_trade["status"] = "T1_HIT_BE_ACTIVE"
        log_trade_db(active_trade)
    else:
        print("🟢 POSITION STATUS: HOLD — Momentum Supportive")

# ==================== DATA SYNC & STREAMING ====================
async def preload_history():
    global candles_1m, candles_15m
    print("⏳ Synchronizing 1m and 15m historical candles from Bybit...")
    async with aiohttp.ClientSession() as session:
        # Load 1m
        url_1m = "https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=1&limit=60"
        async with session.get(url_1m) as resp:
            data = await resp.json()
            raw = data.get("result", {}).get("list", [])
            raw.reverse()
            for k in raw[:-1]:
                candles_1m.append({
                    "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "vol": float(k[5]), "cvd": 0.0,
                    "time": datetime.fromtimestamp(int(k[0]) / 1000).strftime('%H:%M:%S')
                })
        
        # Load 15m for Macro Regime
        url_15m = "https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=15&limit=30"
        async with session.get(url_15m) as resp:
            data = await resp.json()
            raw = data.get("result", {}).get("list", [])
            raw.reverse()
            for k in raw[:-1]:
                candles_15m.append({
                    "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "vol": float(k[5])
                })
    print(f"✅ Loaded {len(candles_1m)} 1m bars and {len(candles_15m)} 15m bars. Database linked.\n")

async def trade_stream():
    global cvd_current_candle
    url = "wss://stream.bybit.com/v5/public/linear"
    subscribe_msg = json.dumps({"op": "subscribe", "args": ["publicTrade.BTCUSDT"]})
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(subscribe_msg)
                while True:
                    msg = json.loads(await ws.recv())
                    if "data" in msg:
                        for trade in msg["data"]:
                            size = float(trade["v"])
                            if trade["S"] == "Buy":
                                cvd_current_candle += size
                            else:
                                cvd_current_candle -= size
        except Exception:
            await asyncio.sleep(2)

async def kline_stream():
    global cvd_current_candle, candles_1m, active_trade
    url = "wss://stream.bybit.com/v5/public/linear"
    subscribe_msg = json.dumps({"op": "subscribe", "args": ["kline.1.BTCUSDT"]})
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(subscribe_msg)
                print("⚡ Real-time Multi-TF Analysis active.\n")
                
                while True:
                    msg = json.loads(await ws.recv())
                    if "data" not in msg:
                        continue
                    kline = msg["data"][0]
                    is_closed = kline["confirm"]
                    curr_price = float(kline["close"])
                    
                    if active_trade:
                        monitor_trade_lifecycle(curr_price)

                    if is_closed:
                        candle_data = {
                            "open": float(kline["open"]), "high": float(kline["high"]),
                            "low": float(kline["low"]), "close": curr_price,
                            "vol": float(kline["volume"]), "cvd": cvd_current_candle,
                            "time": datetime.fromtimestamp(int(kline["end"]) / 1000).strftime('%H:%M:%S')
                        }
                        candles_1m.append(candle_data)
                        if len(candles_1m) > 120:
                            candles_1m.pop(0)
                        cvd_current_candle = 0.0

                        stage, score, meta = evaluate_market_stages(candles_1m, candles_15m)
                        
                        print(f"[{candle_data['time']}] BTC: ${curr_price:,.1f} | Regime (15m): {meta.get('regime')} | ATR: ${meta.get('atr')}")
                        print(f"Market Stage : {stage} | Score: {score}/100")
                        print(f"Expected Run : {meta.get('potential')} (Potential Score: {meta.get('move_score')}/100)")
                        print(f"Range Scope  : ${meta.get('range_low'):,.1f} <---> ${meta.get('range_high'):,.1f} | 1m-Delta: {candle_data['cvd']:+,.2f}")
                        
                        # High-Confidence Trigger (Stage 2 or Stage 3)
                        if score >= 72 and not active_trade:
                            trade_id = f"BTC-{meta['direction']}-{datetime.now().strftime('%Y%m%d-%H%M')}"
                            print("\n" + "🔥"*30)
                            print(f"🚨 OPPORTUNITY FIRED: {meta['direction']} ({stage})")
                            print(f"   Trade ID      : {trade_id}")
                            print(f"   Entry Zone    : {meta['entry_zone']}")
                            print(f"   Invalidation  : ${meta['sl']:,.1f}")
                            print(f"   Target 1 (1R) : ${meta['t1']:,.1f}")
                            print(f"   Target 2 (2.5R): ${meta['t2']:,.1f}")
                            print(f"   Target 3 (Macro): ${meta['t3']:,.1f}")
                            print(f"   Extended Run  : ${meta['extended']:,.1f}")
                            print("🔥"*30 + "\n")

                            active_trade = {
                                "id": trade_id, "time": candle_data["time"], "direction": meta["direction"],
                                "stage": stage, "score": score, "entry": curr_price, "sl": meta["sl"],
                                "t1": meta["t1"], "t2": meta["t2"], "t3": meta["t3"], "extended": meta["extended"],
                                "potential": meta["potential"], "status": "ACTIVE", "stage_state": 0
                            }
                            log_trade_db(active_trade)

                        print("-" * 68)
                        sys.stdout.flush()
        except Exception as e:
            print(f"Reconnecting stream in 3s... ({e})")
            await asyncio.sleep(3)

# ==================== APPLICATION BOOT ====================
async def main():
    init_db()
    await preload_history()
    await asyncio.gather(
        trade_stream(),
        kline_stream()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nEngine safely terminated.")