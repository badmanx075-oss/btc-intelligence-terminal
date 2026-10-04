import asyncio
import json
import numpy as np
import websockets
import aiohttp
from datetime import datetime

MAX_CANDLES = 100
candles_1m = []
cvd_current_candle = 0.0

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

def analyze_market_stage(candles):
    if len(candles) < 20:
        return "STAGE 0: INSUFFICIENT DATA", 0, {}
    
    recent = candles[-20:]
    closes = np.array([c["close"] for c in recent])
    cvds = np.array([c["cvd"] for c in recent])
    highs = np.array([c["high"] for c in recent])
    lows = np.array([c["low"] for c in recent])
    
    curr_close = closes[-1]
    curr_low = lows[-1]
    curr_high = highs[-1]
    atr = calculate_atr(candles, 14)
    
    range_high = np.max(highs[:-2])
    range_low = np.min(lows[:-2])
    
    bullish_cvd_divergence = (curr_low <= range_low) and (cvds[-1] > cvds[-3])
    bearish_cvd_divergence = (curr_high >= range_high) and (cvds[-1] < cvds[-3])
    
    metrics = {
        "price": curr_close,
        "atr": round(atr, 2),
        "range_low": round(range_low, 2),
        "range_high": round(range_high, 2),
        "last_cvd": round(cvds[-1], 2)
    }
    
    if bullish_cvd_divergence and (curr_close >= range_low):
        score = 75
        metrics["invalidation"] = round(curr_low - (0.3 * atr), 2)
        metrics["entry_zone"] = f"{round(range_low, 2)} - {round(range_low + (0.4 * atr), 2)}"
        metrics["t1"] = round(curr_close + atr, 2)
        metrics["t2"] = round(curr_close + (2.5 * atr), 2)
        return "STAGE 2: EARLY LONG SETUP (ABSORPTION)", score, metrics

    elif bearish_cvd_divergence and (curr_close <= range_high):
        score = 75
        metrics["invalidation"] = round(curr_high + (0.3 * atr), 2)
        metrics["entry_zone"] = f"{round(range_high - (0.4 * atr), 2)} - {round(range_high, 2)}"
        metrics["t1"] = round(curr_close - atr, 2)
        metrics["t2"] = round(curr_close - (2.5 * atr), 2)
        return "STAGE 2: EARLY SHORT SETUP (DISTRIBUTION)", score, metrics

    elif range_low < curr_close < range_high:
        return "STAGE 1: RANGE CONSOLIDATION", 40, metrics

    return "STAGE 0: NO EDGE / WAIT", 20, metrics

async def preload_historical_candles():
    """Start hote hi pichli 50 candles fetch karega taaki turant calculations start ho sakein."""
    global candles_1m
    url = "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1m&limit=50"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
                for k in data[:-1]:  # Closed candles
                    candles_1m.append({
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "vol": float(k[5]),
                        "cvd": 0.0,
                        "time": datetime.fromtimestamp(k[0] / 1000).strftime('%H:%M:%S')
                    })
        print(f"✅ Loaded {len(candles_1m)} historical 1m candles. Instant calculations ready!")
    except Exception as e:
        print(f"⚠️ Preload warning: {e}. Will populate from live stream.")

async def binance_aggtrade_stream():
    global cvd_current_candle
    url = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                while True:
                    msg = json.loads(await ws.recv())
                    qty = float(msg["q"])
                    is_buyer_maker = msg["m"]
                    if is_buyer_maker:
                        cvd_current_candle -= qty
                    else:
                        cvd_current_candle += qty
        except Exception as e:
            await asyncio.sleep(2)

async def binance_kline_stream():
    global cvd_current_candle, candles_1m
    url = "wss://fstream.binance.com/ws/btcusdt@kline_1m"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                print("⚡ Real-time stream active. Processing live ticks...\n")
                last_printed_sec = ""
                
                while True:
                    msg = json.loads(await ws.recv())
                    k = msg["k"]
                    is_closed = k["x"]
                    curr_price = float(k["c"])
                    
                    # Live tick monitor (har 5 second me terminal update karega taaki screen freeze na lage)
                    now_sec = datetime.now().strftime('%S')
                    if int(now_sec) % 5 == 0 and now_sec != last_printed_sec:
                        last_printed_sec = now_sec
                        print(f"⏳ Live Feed: BTC @ ${curr_price:,.1f} | Active Delta: {cvd_current_candle:+.2f} BTC", end="\r")

                    # Candle closed (1 minute complete)
                    if is_closed:
                        candle_data = {
                            "open": float(k["o"]),
                            "high": float(k["h"]),
                            "low": float(k["l"]),
                            "close": curr_price,
                            "vol": float(k["v"]),
                            "cvd": cvd_current_candle,
                            "time": datetime.fromtimestamp(k["t"] / 1000).strftime('%H:%M:%S')
                        }
                        candles_1m.append(candle_data)
                        if len(candles_1m) > MAX_CANDLES:
                            candles_1m.pop(0)
                        
                        cvd_current_candle = 0.0
                        
                        stage, score, meta = analyze_market_stage(candles_1m)
                        print(f"\n──────────────────────────────────────────────────")
                        print(f"[{candle_data['time']}] BTC: ${candle_data['close']} | ATR: {meta.get('atr')} | 1m-CVD: {candle_data['cvd']:+.2f}")
                        print(f"Status : {stage} (Score: {score}/100)")
                        print(f"Range  : ${meta.get('range_low')}  <--->  ${meta.get('range_high')}")
                        if score >= 70:
                            print(f"🚨 OPPORTUNITY DETECTED:")
                            print(f"   Entry Zone   : {meta.get('entry_zone')}")
                            print(f"   Invalidation : {meta.get('invalidation')}")
                            print(f"   Target 1     : {meta.get('t1')}")
                            print(f"   Target 2     : {meta.get('t2')}")
                        print(f"──────────────────────────────────────────────────\n")
        except Exception as e:
            print(f"\n[Connection Notice] Reconnecting in 3s... ({e})")
            await asyncio.sleep(3)

async def main():
    await preload_historical_candles()
    await asyncio.gather(
        binance_aggtrade_stream(),
        binance_kline_stream()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nEngine stopped.")