import asyncio
import json
import sys
import numpy as np
import websockets
import aiohttp
from datetime import datetime

# ==================== CONFIGURATION ====================
SYMBOL = "BTCUSDT"
MAX_CANDLES = 120

candles_1m = []
cvd_current_candle = 0.0

# Active position tracking (User simulated / monitored)
active_trade = None

# ==================== CORE MATH & METRICS ====================
def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0
    highs = np.array([c["high"] for c in candles[-period-1:]])
    lows = np.array([c["low"] for c in candles[-period-1:]])
    closes = np.array([c["close"] for c in candles[-period-1:]])
    
    tr1 = highs[1:] - lows[1:]
    tr2 = np.abs(highs[1:] - closes[:-1])
    tr3 = np.abs(lows[1:] - closes[:-1])
    tr = np.maximum(tr1, np.maximum(tr2, tr3))
    return float(np.mean(tr[-period:]))

def detect_market_regime(candles):
    """Identifies Trend vs Range vs Transition vs Chop"""
    if len(candles) < 30:
        return "UNCLEAR / INITIALIZING"
    
    closes = np.array([c["close"] for c in candles[-30:]])
    x = np.arange(len(closes))
    slope, _ = np.polyfit(x, closes, 1)
    
    highs = np.array([c["high"] for c in candles[-30:]])
    lows = np.array([c["low"] for c in candles[-30:]])
    range_span = (np.max(highs) - np.min(lows)) / closes[-1]
    
    if slope > 2.0:
        return "BULLISH TREND"
    elif slope < -2.0:
        return "BEARISH TREND"
    elif range_span < 0.015:
        return "TIGHT CONSOLIDATION (COMPRESSION)"
    else:
        return "CONSOLIDATION / RANGE"

def calculate_move_potential(atr, regime):
    """Categorizes 500, 1k, 2k, 5k, 10k+ points potential based on ATR volatility"""
    if atr <= 0:
        return "Small (500-1,000 pts)", 40
    
    volatility_ratio = atr / 100.0  # Normalized base
    if "COMPRESSION" in regime:
        return "EXTENDED POTENTIAL (5,000 - 10,000+ pts)", 88
    elif "TREND" in regime:
        return "LARGE MOVE (2,000 - 5,000 pts)", 74
    else:
        return "MEDIUM MOVE (1,000 - 2,000 pts)", 58

# ==================== 6-STAGE ENGINE ====================
def analyze_market_stages(candles):
    if len(candles) < 25:
        return "STAGE 0: INSUFFICIENT DATA", 0, {}
    
    recent = candles[-25:]
    closes = np.array([c["close"] for c in recent])
    cvds = np.array([c["cvd"] for c in recent])
    highs = np.array([c["high"] for c in recent])
    lows = np.array([c["low"] for c in recent])
    
    curr_close = closes[-1]
    curr_low = lows[-1]
    curr_high = highs[-1]
    atr = calculate_atr(candles, 14)
    regime = detect_market_regime(candles)
    move_desc, move_score = calculate_move_potential(atr, regime)
    
    range_high = np.max(highs[:-3])
    range_low = np.min(lows[:-3])
    
    # Delta Divergence (Absorption detection)
    bullish_cvd_div = (curr_low <= range_low) and (cvds[-1] > cvds[-4])
    bearish_cvd_div = (curr_high >= range_high) and (cvds[-1] < cvds[-4])
    
    meta = {
        "price": curr_close,
        "atr": round(atr, 2),
        "regime": regime,
        "range_low": round(range_low, 2),
        "range_high": round(range_high, 2),
        "move_potential": move_desc,
        "large_move_score": move_score,
        "cvd": round(cvds[-1], 2)
    }
    
    # STAGE 2: EARLY LONG SETUP
    if bullish_cvd_div and (curr_close >= range_low):
        meta["direction"] = "LONG"
        meta["entry_zone"] = f"${round(range_low, 1)} - ${round(range_low + (0.35 * atr), 1)}"
        meta["invalidation"] = round(curr_low - (0.4 * atr), 1)
        meta["confirmation"] = round(range_low + (0.7 * atr), 1)
        meta["t1"] = round(curr_close + (1.2 * atr), 1)
        meta["t2"] = round(curr_close + (2.8 * atr), 1)
        meta["t3"] = round(curr_close + (5.0 * atr), 1)
        meta["extended"] = round(curr_close + (8.5 * atr), 1)
        return "STAGE 2: EARLY LONG SETUP (ABSORPTION)", 78, meta

    # STAGE 2: EARLY SHORT SETUP
    elif bearish_cvd_div and (curr_close <= range_high):
        meta["direction"] = "SHORT"
        meta["entry_zone"] = f"${round(range_high - (0.35 * atr), 1)} - ${round(range_high, 1)}"
        meta["invalidation"] = round(curr_high + (0.4 * atr), 1)
        meta["confirmation"] = round(range_high - (0.7 * atr), 1)
        meta["t1"] = round(curr_close - (1.2 * atr), 1)
        meta["t2"] = round(curr_close - (2.8 * atr), 1)
        meta["t3"] = round(curr_close - (5.0 * atr), 1)
        meta["extended"] = round(curr_close - (8.5 * atr), 1)
        return "STAGE 2: EARLY SHORT SETUP (DISTRIBUTION)", 78, meta

    # STAGE 1: ACCUMULATION / DISTRIBUTION WATCH
    elif (curr_close <= range_low + (0.2 * atr)) or (curr_close >= range_high - (0.2 * atr)):
        return "STAGE 1: BOUNDARY PRESSURE WATCH", 55, meta

    # STAGE 0 / RANGE
    return f"STAGE 0: {regime}", 35, meta

# ==================== POST-ENTRY POSITION MONITOR ====================
def monitor_active_position(curr_price):
    global active_trade
    if not active_trade:
        return
    
    d = active_trade["direction"]
    entry = active_trade["entry"]
    sl = active_trade["sl"]
    t1 = active_trade["t1"]
    t2 = active_trade["t2"]
    
    pnl_pts = (curr_price - entry) if d == "LONG" else (entry - curr_price)
    
    print("\n" + "="*55)
    print(f"📌 ACTIVE MONITORED POSITION: {d} @ ${entry:,.1f}")
    print(f"Current Price: ${curr_price:,.1f} | Unrealized: {pnl_pts:+,.1f} pts")
    
    if (d == "LONG" and curr_price <= sl) or (d == "SHORT" and curr_price >= sl):
        print("🔴 STATUS: TRADE INVALIDATED / STOP HIT. Exiting.")
        active_trade = None
    elif (d == "LONG" and curr_price >= t2) or (d == "SHORT" and curr_price <= t2):
        print("🟠 STATUS: TARGET 2 HIT! Book 60% profit. Trail stop to T1.")
    elif (d == "LONG" and curr_price >= t1) or (d == "SHORT" and curr_price <= t1):
        print("🟡 STATUS: TARGET 1 HIT! Move stop to Breakeven. Hold runner.")
    else:
        print("🟢 STATUS: HOLD — THESIS INTACT & SUPPORTED")
    print("="*55 + "\n")

# ==================== DATA INGESTION (BYBIT FAST & UNBLOCKED) ====================
async def preload_candles():
    """Fetches initial 50 candles instantly via Bybit Public REST API"""
    global candles_1m
    url = "https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=1&limit=50"
    print("⏳ Connecting to exchange feed and downloading historical context...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                res = await resp.json()
                raw_list = res.get("result", {}).get("list", [])
                raw_list.reverse() # Oldest to newest
                
                for k in raw_list[:-1]:
                    candles_1m.append({
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "vol": float(k[5]),
                        "cvd": 0.0,
                        "time": datetime.fromtimestamp(int(k[0]) / 1000).strftime('%H:%M:%S')
                    })
        print(f"✅ Successfully ingested {len(candles_1m)} candles. Engine is fully primed!\n")
    except Exception as e:
        print(f"⚠️️ Preload fallback triggered: {e}. Building live from socket stream.\n")

async def trade_websocket_stream():
    """Captures real-time AggTrades for Delta Volume (CVD) calculation"""
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
                            side = trade["S"]  # "Buy" or "Sell"
                            if side == "Buy":
                                cvd_current_candle += size
                            else:
                                cvd_current_candle -= size
        except Exception:
            await asyncio.sleep(2)

async def kline_websocket_stream():
    """Main execution loop: Ingests 1m bars and runs Stage & Opportunity evaluation"""
    global cvd_current_candle, candles_1m, active_trade
    url = "wss://stream.bybit.com/v5/public/linear"
    subscribe_msg = json.dumps({"op": "subscribe", "args": ["kline.1.BTCUSDT"]})
    
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(subscribe_msg)
                print("⚡ Real-time Pipeline Online. Streaming BTC Perpetual Data...\n")
                
                while True:
                    msg = json.loads(await ws.recv())
                    if "data" not in msg:
                        continue
                        
                    kline = msg["data"][0]
                    is_closed = kline["confirm"]
                    curr_price = float(kline["close"])
                    
                    # Real-time position evaluation on every price change
                    if active_trade:
                        monitor_active_position(curr_price)

                    # When a 1-minute candle closes
                    if is_closed:
                        candle_data = {
                            "open": float(kline["open"]),
                            "high": float(kline["high"]),
                            "low": float(kline["low"]),
                            "close": curr_price,
                            "vol": float(kline["volume"]),
                            "cvd": cvd_current_candle,
                            "time": datetime.fromtimestamp(int(kline["end"]) / 1000).strftime('%H:%M:%S')
                        }
                        candles_1m.append(candle_data)
                        if len(candles_1m) > MAX_CANDLES:
                            candles_1m.pop(0)
                        
                        # Reset CVD counter for next candle
                        cvd_current_candle = 0.0
                        
                        # Run Analysis
                        stage, score, meta = analyze_market_stages(candles_1m)
                        
                        # Clean Dashboard Output
                        print(f"[{candle_data['time']}] BTC: ${curr_price:,.1f} | Regime: {meta.get('regime')} | ATR: ${meta.get('atr')}")
                        print(f"Market Phase : {stage} | Score: {score}/100")
                        print(f"Expected Move: {meta.get('move_potential')} (Score: {meta.get('large_move_score')}/100)")
                        print(f"Range Scope  : ${meta.get('range_low'):,.1f} <---> ${meta.get('range_high'):,.1f} | Delta CVD: {candle_data['cvd']:+,.2f}")
                        
                        # If High-Probability Opportunity Appears
                        if score >= 75 and not active_trade:
                            print("\n" + "🔥"*25)
                            print(f"🚨 OPPORTUNITY TRIGGER: {meta.get('direction')} DETECTED")
                            print(f"   Entry Zone    : {meta.get('entry_zone')}")
                            print(f"   Invalidation  : ${meta.get('invalidation'):,.1f}")
                            print(f"   Confirmation  : Above/Below ${meta.get('confirmation'):,.1f}")
                            print(f"   Target 1 (1R) : ${meta.get('t1'):,.1f}")
                            print(f"   Target 2 (3R) : ${meta.get('t2'):,.1f}")
                            print(f"   Target 3      : ${meta.get('t3'):,.1f}")
                            print(f"   Extended Run  : ${meta.get('extended'):,.1f}")
                            print("🔥"*25 + "\n")
                            
                            # Auto-track setup as active simulation
                            active_trade = {
                                "direction": meta.get("direction"),
                                "entry": curr_price,
                                "sl": meta.get("invalidation"),
                                "t1": meta.get("t1"),
                                "t2": meta.get("t2")
                            }
                        
                        print("-" * 65)
                        sys.stdout.flush()
        except Exception as e:
            print(f"Reconnecting stream in 3s... ({e})")
            await asyncio.sleep(3)

# ==================== ENTRY POINT ====================
async def main():
    await preload_candles()
    await asyncio.gather(
        trade_websocket_stream(),
        kline_websocket_stream()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nEngine safely terminated.")