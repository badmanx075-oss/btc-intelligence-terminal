import asyncio
import json
import sqlite3
import sys
import numpy as np
import websockets
import aiohttp
from datetime import datetime
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
import uvicorn

# ==================== CONFIGURATION & STATE ====================
SYMBOL = "BTCUSDT"
DB_NAME = "trades_vault.db"

candles_1m = []
candles_15m = []
cvd_current_candle = 0.0

active_trade = None
latest_metrics = {
    "price": 0.0, "atr": 0.0, "regime": "INITIALIZING",
    "stage": "STAGE 0: SYNCING", "score": 35,
    "score_structure": 50, "score_volume": 45, "score_momentum": 50, "score_liquidity": 40,
    "range_low": 0.0, "range_high": 0.0, "cvd": 0.0, "move_potential": "N/A"
}

connected_clients = set()

# ==================== DATABASE ====================
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
    conn.commit()
    conn.close()

def log_trade_db(t):
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
            t["id"], t["time"], t["direction"], t["stage"],
            t["score"], t["entry"], t["sl"], t["t1"],
            t["t2"], t["t3"], t["extended"], t["potential"],
            t["status"]
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB Error] {e}")

# ==================== ADVANCED METRICS ENGINE ====================
def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 80.0
    highs = np.array([c["high"] for c in candles[-period-1:]])
    lows = np.array([c["low"] for c in candles[-period-1:]])
    closes = np.array([c["close"] for c in candles[-period-1:]])
    tr = np.maximum(highs[1:] - lows[1:], np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])))
    return float(np.mean(tr[-period:]))

def detect_regime(candles_15m):
    if len(candles_15m) < 20:
        return "UNCLEAR REGIME"
    closes = np.array([c["close"] for c in candles_15m[-20:]])
    slope, _ = np.polyfit(np.arange(len(closes)), closes, 1)
    highs = np.array([c["high"] for c in candles_15m[-20:]])
    lows = np.array([c["low"] for c in candles_15m[-20:]])
    range_span = (np.max(highs) - np.min(lows)) / closes[-1]
    
    if slope > 2.0:
        return "BULLISH TREND"
    elif slope < -2.0:
        return "BEARISH TREND"
    elif range_span < 0.012:
        return "SQUEEZE / COMPRESSION"
    else:
        return "RANGE CONSOLIDATION"

def point_move_classifier(atr, regime):
    if "SQUEEZE" in regime:
        return "EXTENDED MOVE (5,000 - 10,000+ pts)", 92
    elif "TREND" in regime:
        return "LARGE MOVE (2,000 - 5,000 pts)", 78
    elif atr > 100:
        return "MEDIUM MOVE (1,000 - 2,000 pts)", 62
    else:
        return "SMALL MOVE (500 - 1,000 pts)", 45

def evaluate_market_stages(candles_1m, candles_15m):
    global latest_metrics
    if len(candles_1m) < 30:
        return "STAGE 0: SYNCING", 35, {}
    
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
    
    range_high = np.max(highs[:-4])
    range_low = np.min(lows[:-4])
    avg_vol = np.mean(volumes[:-1])
    is_vol_surge = volumes[-1] > (1.8 * avg_vol)
    
    bullish_cvd_div = (curr_low <= range_low) and (cvds[-1] > cvds[-4])
    bearish_cvd_div = (curr_high >= range_high) and (cvds[-1] < cvds[-4])
    bullish_reclaim = (lows[-2] < range_low) and (curr_close > range_low)
    bearish_reclaim = (highs[-2] > range_high) and (curr_close < range_high)

    score_structure = 75 if "TREND" in regime else 50
    score_volume = 80 if is_vol_surge else 45
    score_momentum = 70 if (bullish_cvd_div or bearish_cvd_div) else 40
    score_liquidity = 85 if (curr_close <= range_low or curr_close >= range_high) else 35

    meta = {
        "price": curr_close, "atr": round(atr, 2), "regime": regime,
        "range_low": round(range_low, 2), "range_high": round(range_high, 2),
        "potential": move_desc, "move_score": move_score, "cvd": round(cvds[-1], 2),
        "score_structure": score_structure, "score_volume": score_volume,
        "score_momentum": score_momentum, "score_liquidity": score_liquidity
    }

    allow_short = (regime != "BULLISH TREND") or (bearish_cvd_div and bearish_reclaim)
    allow_long = (regime != "BEARISH TREND") or (bullish_cvd_div and bullish_reclaim)

    if (bullish_cvd_div or bullish_reclaim) and (curr_close >= range_low) and allow_long:
        score = 84 if bullish_cvd_div and bullish_reclaim else 76
        meta.update({
            "direction": "LONG",
            "entry_zone": f"${round(range_low, 1)} - ${round(range_low + (0.35 * atr), 1)}",
            "sl": round(curr_low - (0.4 * atr), 1),
            "t1": round(curr_close + (1.2 * atr), 1),
            "t2": round(curr_close + (2.5 * atr), 1),
            "t3": round(curr_close + (4.5 * atr), 1),
            "extended": round(curr_close + (8.0 * atr), 1)
        })
        stage = "STAGE 2: EARLY LONG SETUP"
        latest_metrics = {**meta, "stage": stage, "score": score}
        return stage, score, meta

    elif (bearish_cvd_div or bearish_reclaim) and (curr_close <= range_high) and allow_short:
        score = 84 if bearish_cvd_div and bearish_reclaim else 76
        meta.update({
            "direction": "SHORT",
            "entry_zone": f"${round(range_high - (0.35 * atr), 1)} - ${round(range_high, 1)}",
            "sl": round(curr_high + (0.4 * atr), 1),
            "t1": round(curr_close - (1.2 * atr), 1),
            "t2": round(curr_close - (2.5 * atr), 1),
            "t3": round(curr_close - (4.5 * atr), 1),
            "extended": round(curr_close - (8.0 * atr), 1)
        })
        stage = "STAGE 2: EARLY SHORT SETUP"
        latest_metrics = {**meta, "stage": stage, "score": score}
        return stage, score, meta

    stage = f"STAGE 0: {regime}"
    latest_metrics = {**meta, "stage": stage, "score": 35}
    return stage, 35, meta

# ==================== POSITION MONITORING ====================
def evaluate_active_trade(curr_price):
    global active_trade
    if not active_trade:
        return None
    
    d = active_trade["direction"]
    entry = active_trade["entry"]
    sl = active_trade["sl"]
    t1 = active_trade["t1"]
    t2 = active_trade["t2"]
    t3 = active_trade["t3"]
    state = active_trade["stage_state"]
    
    pnl = (curr_price - entry) if d == "LONG" else (entry - curr_price)
    pnl_pct = (pnl / entry) * 100
    
    rec = "🟢 HOLD — Momentum Supportive"
    
    if (d == "LONG" and curr_price <= sl) or (d == "SHORT" and curr_price >= sl):
        rec = "🔴 EXIT: Hard Invalidation Hit"
        active_trade["status"] = "STOPPED_OUT"
        log_trade_db(active_trade)
        res = {**active_trade, "pnl": round(pnl, 1), "pnl_pct": round(pnl_pct, 2), "rec": rec, "active": False}
        active_trade = None
        return res

    if ((d == "LONG" and curr_price >= t3) or (d == "SHORT" and curr_price <= t3)) and state < 3:
        active_trade["stage_state"] = 3
        active_trade["sl"] = t2
        active_trade["status"] = "T3_HIT"
        rec = "🟠 T3 REACHED: Secure 80% Profit! Trailing Runner Active."
        log_trade_db(active_trade)
    elif ((d == "LONG" and curr_price >= t2) or (d == "SHORT" and curr_price <= t2)) and state < 2:
        active_trade["stage_state"] = 2
        active_trade["sl"] = t1
        active_trade["status"] = "T2_HIT"
        rec = "🟡 T2 REACHED: Secure 50% Profit. Stop at T1."
        log_trade_db(active_trade)
    elif ((d == "LONG" and curr_price >= t1) or (d == "SHORT" and curr_price <= t1)) and state < 1:
        active_trade["stage_state"] = 1
        active_trade["sl"] = entry
        active_trade["status"] = "T1_HIT_BE"
        rec = "🟡 T1 REACHED: SL at BREAKEVEN. Zero risk."
        log_trade_db(active_trade)

    return {**active_trade, "pnl": round(pnl, 1), "pnl_pct": round(pnl_pct, 2), "rec": rec, "active": True}

# ==================== WEBSOCKET BROADCAST ====================
async def broadcast_ui_state():
    while True:
        if connected_clients:
            curr_p = latest_metrics.get("price", 0.0)
            trade_status = evaluate_active_trade(curr_p)
            
            payload = json.dumps({
                "metrics": latest_metrics,
                "trade": trade_status,
                "chart": candles_1m[-40:],
                "timestamp": datetime.now().strftime('%H:%M:%S')
            })
            dead = set()
            for ws in connected_clients:
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.add(ws)
            connected_clients.difference_update(dead)
        await asyncio.sleep(0.5)

# ==================== DATA STREAMS ====================
async def preload_history():
    global candles_1m, candles_15m
    print("⏳ Synchronizing market candles from Bybit...")
    async with aiohttp.ClientSession() as session:
        url_1m = "https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=1&limit=60"
        async with session.get(url_1m) as resp:
            data = await resp.json()
            raw = data.get("result", {}).get("list", [])
            raw.reverse()
            for k in raw[:-1]:
                candles_1m.append({
                    "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "vol": float(k[5]), "cvd": 0.0,
                    "time": datetime.fromtimestamp(int(k[0]) / 1000).strftime('%H:%M')
                })
        
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
    print("✅ Ready.\n")

async def trade_stream():
    global cvd_current_candle
    url = "wss://stream.bybit.com/v5/public/linear"
    msg = json.dumps({"op": "subscribe", "args": ["publicTrade.BTCUSDT"]})
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(msg)
                while True:
                    m = json.loads(await ws.recv())
                    if "data" in m:
                        for t in m["data"]:
                            sz = float(t["v"])
                            if t["S"] == "Buy":
                                cvd_current_candle += sz
                            else:
                                cvd_current_candle -= sz
        except Exception:
            await asyncio.sleep(2)

async def kline_stream():
    global cvd_current_candle, candles_1m, active_trade, latest_metrics
    url = "wss://stream.bybit.com/v5/public/linear"
    msg = json.dumps({"op": "subscribe", "args": ["kline.1.BTCUSDT"]})
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(msg)
                while True:
                    m = json.loads(await ws.recv())
                    if "data" not in m:
                        continue
                    k = m["data"][0]
                    curr_price = float(k["close"])
                    is_closed = k["confirm"]
                    
                    latest_metrics["price"] = curr_price
                    
                    if is_closed:
                        candle_data = {
                            "open": float(k["open"]), "high": float(k["high"]),
                            "low": float(k["low"]), "close": curr_price,
                            "vol": float(k["volume"]), "cvd": cvd_current_candle,
                            "time": datetime.fromtimestamp(int(k["end"]) / 1000).strftime('%H:%M')
                        }
                        candles_1m.append(candle_data)
                        if len(candles_1m) > 120:
                            candles_1m.pop(0)
                        cvd_current_candle = 0.0

                        stage, score, meta = evaluate_market_stages(candles_1m, candles_15m)
                        
                        if score >= 76 and not active_trade:
                            trade_id = f"BTC-{meta['direction']}-{datetime.now().strftime('%Y%m%d-%H%M')}"
                            active_trade = {
                                "id": trade_id, "time": candle_data["time"], "direction": meta["direction"],
                                "stage": stage, "score": score, "entry": curr_price, "sl": meta["sl"],
                                "t1": meta["t1"], "t2": meta["t2"], "t3": meta["t3"], "extended": meta["extended"],
                                "potential": meta["potential"], "status": "ACTIVE", "stage_state": 0
                            }
                            log_trade_db(active_trade)
        except Exception:
            await asyncio.sleep(2)

# ==================== FASTAPI APP & NATIVE HTML5 CANVAS UI ====================
app = FastAPI()

HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>BTC Perpetual Intelligence Terminal</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background-color: #0b0e14; color: #d1d5db; font-family: monospace; padding: 24px; }
        .max-w { max-width: 1280px; margin: 0 auto; }
        .card { background-color: #151a23; border: 1px solid #1f2937; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
        .flex { display: flex; }
        .justify-between { justify-content: space-between; }
        .items-center { align-items: center; }
        .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 16px; }
        .text-xs { font-size: 11px; color: #9ca3af; }
        .text-lg { font-size: 18px; font-weight: bold; }
        .text-2xl { font-size: 24px; font-weight: bold; }
        .text-3xl { font-size: 32px; font-weight: 800; color: #ffffff; }
        .text-yellow { color: #facc15; }
        .text-green { color: #10b981; }
        .text-red { color: #ef4444; }
        .text-blue { color: #60a5fa; }
        .text-purple { color: #c084fc; }
        .btn { padding: 6px 12px; border-radius: 4px; border: none; font-weight: bold; cursor: pointer; font-size: 12px; }
        .btn-green { background: #065f46; color: #34d399; }
        .btn-red { background: #7f1d1d; color: #f87171; }
        .btn-gray { background: #374151; color: #d1d5db; }
        #chartCanvas { width: 100%; height: 350px; background: #111827; border-radius: 6px; display: block; }
    </style>
</head>
<body>
    <div class="max-w">
        <!-- HEADER -->
        <div class="flex justify-between items-center" style="border-bottom: 1px solid #1f2937; padding-bottom: 12px; margin-bottom: 16px;">
            <div>
                <h1 class="text-2xl text-yellow">BTC PERPETUAL INTELLIGENCE TERMINAL</h1>
                <p class="text-xs">Stages 1-6 Dynamic State Machine & Autonomous Opportunity Radar</p>
            </div>
            <div style="text-align: right;">
                <div class="text-3xl" id="live-price">$0.0</div>
                <div class="text-xs" id="live-time">Syncing live stream...</div>
            </div>
        </div>

        <!-- TOP METRICS -->
        <div class="grid-4">
            <div class="card">
                <span class="text-xs">REGIME (15m CONTEXT)</span>
                <div class="text-lg text-blue" id="regime">--</div>
            </div>
            <div class="card">
                <span class="text-xs">STAGE / SCORE</span>
                <div class="text-lg text-yellow" id="stage-score">--</div>
            </div>
            <div class="card">
                <span class="text-xs">VOLATILITY / ATR</span>
                <div class="text-lg text-green" id="atr">--</div>
            </div>
            <div class="card">
                <span class="text-xs">EXPECTED MOVE</span>
                <div class="text-lg text-purple" id="move-pot">--</div>
            </div>
        </div>

        <!-- SUB-SCORES RADAR -->
        <div class="grid-4">
            <div class="card" style="text-align: center;">
                <span class="text-xs">STRUCTURE SCORE</span>
                <div class="text-lg text-blue" id="score-struct">0/100</div>
            </div>
            <div class="card" style="text-align: center;">
                <span class="text-xs">VOLUME / DELTA</span>
                <div class="text-lg text-green" id="score-vol">0/100</div>
            </div>
            <div class="card" style="text-align: center;">
                <span class="text-xs">MOMENTUM SQUEEZE</span>
                <div class="text-lg text-yellow" id="score-mom">0/100</div>
            </div>
            <div class="card" style="text-align: center;">
                <span class="text-xs">LIQUIDITY PROXIMITY</span>
                <div class="text-lg text-purple" id="score-liq">0/100</div>
            </div>
        </div>

        <!-- NATIVE CANVAS CHART -->
        <div class="card">
            <div class="flex justify-between items-center" style="margin-bottom: 8px;">
                <span class="text-xs" style="font-weight: bold; color: #e5e7eb;">REAL-TIME CANDLES & BOUNDS</span>
                <div class="text-xs">
                    <span class="text-green">Range Low: <strong id="r-low">$0</strong></span> &nbsp;|&nbsp;
                    <span class="text-red">Range High: <strong id="r-high">$0</strong></span> &nbsp;|&nbsp;
                    <span id="cvd-badge" style="font-weight: bold;">Delta: 0.00 BTC</span>
                </div>
            </div>
            <canvas id="chartCanvas"></canvas>
        </div>

        <!-- POSITION LIFECYCLE PANEL -->
        <div class="card" style="border-left: 4px solid #facc15;">
            <div class="flex justify-between items-center" style="margin-bottom: 12px;">
                <h2 class="text-xs" style="font-weight: bold; color: #9ca3af; text-transform: uppercase;">Live Position Lifecycle</h2>
                <div>
                    <button onclick="triggerSim('LONG')" class="btn btn-green">Simulate Long</button>
                    <button onclick="triggerSim('SHORT')" class="btn btn-red">Simulate Short</button>
                    <button onclick="clearTrade()" class="btn btn-gray">Clear</button>
                </div>
            </div>
            <div id="no-trade" class="text-xs" style="color: #6b7280;">No active position. Radar is actively scanning for Stage 2/3 confirmation...</div>
            <div id="trade-panel" style="display: none;">
                <div class="flex justify-between items-center" style="margin-bottom: 12px;">
                    <div>
                        <span id="trade-badge" style="padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">LONG</span>
                        <span id="trade-id" style="font-weight: bold; margin-left: 8px; color: #fff;">BTC-xxx</span>
                        <span id="trade-entry" class="text-xs" style="margin-left: 8px;">Entry: $0</span>
                    </div>
                    <div class="text-2xl" id="trade-pnl">+0.0 pts</div>
                </div>
                <div class="grid-4" style="text-align: center; border-top: 1px solid #1f2937; border-bottom: 1px solid #1f2937; padding: 8px 0; margin-bottom: 8px;">
                    <div><span class="text-xs">INVALIDATION (SL)</span><div class="text-red" id="t-sl" style="font-weight: bold;">0</div></div>
                    <div><span class="text-xs">TARGET 1</span><div class="text-green" id="t-t1" style="font-weight: bold;">0</div></div>
                    <div><span class="text-xs">TARGET 2</span><div class="text-green" id="t-t2" style="font-weight: bold;">0</div></div>
                    <div><span class="text-xs">TARGET 3</span><div class="text-green" id="t-t3" style="font-weight: bold;">0</div></div>
                </div>
                <div class="text-xs text-yellow" id="trade-rec" style="font-weight: bold;">Status: Monitoring...</div>
            </div>
        </div>
    </div>

    <script>
        const canvas = document.getElementById('chartCanvas');
        const ctx = canvas.getContext('2d');

        function resizeCanvas() {
            canvas.width = canvas.parentElement.clientWidth - 32;
            canvas.height = 350;
        }
        window.addEventListener('resize', resizeCanvas);
        resizeCanvas();

        function renderNativeChart(candles, rangeLow, rangeHigh) {
            if (!candles || candles.length === 0) return;
            const w = canvas.width;
            const h = canvas.height;
            ctx.clearRect(0, 0, w, h);

            let minPrice = Infinity;
            let maxPrice = -Infinity;
            candles.forEach(c => {
                if (c.low < minPrice) minPrice = c.low;
                if (c.high > maxPrice) maxPrice = c.high;
            });
            if (rangeLow > 0 && rangeLow < minPrice) minPrice = rangeLow;
            if (rangeHigh > 0 && rangeHigh > maxPrice) maxPrice = rangeHigh;
            
            const padding = (maxPrice - minPrice) * 0.1 || 10;
            minPrice -= padding;
            maxPrice += padding;

            const getY = (val) => h - ((val - minPrice) / (maxPrice - minPrice)) * (h - 20) - 10;

            // Draw Range High / Low Horizontal Lines
            if (rangeHigh > 0) {
                ctx.strokeStyle = 'rgba(239, 68, 68, 0.4)';
                ctx.setLineDash([4, 4]);
                ctx.beginPath();
                ctx.moveTo(0, getY(rangeHigh));
                ctx.lineTo(w, getY(rangeHigh));
                ctx.stroke();
            }
            if (rangeLow > 0) {
                ctx.strokeStyle = 'rgba(16, 185, 129, 0.4)';
                ctx.setLineDash([4, 4]);
                ctx.beginPath();
                ctx.moveTo(0, getY(rangeLow));
                ctx.lineTo(w, getY(rangeLow));
                ctx.stroke();
                ctx.setLineDash([]);
            }

            // Draw Candlesticks
            const candleWidth = (w / candles.length) * 0.7;
            const gap = w / candles.length;

            candles.forEach((c, idx) => {
                const x = idx * gap + gap / 2;
                const isGreen = c.close >= c.open;
                ctx.fillStyle = isGreen ? '#10b981' : '#ef4444';
                ctx.strokeStyle = isGreen ? '#10b981' : '#ef4444';

                // Wick
                ctx.beginPath();
                ctx.moveTo(x, getY(c.high));
                ctx.lineTo(x, getY(c.low));
                ctx.stroke();

                // Body
                const yOpen = getY(c.open);
                const yClose = getY(c.close);
                const bodyHeight = Math.max(Math.abs(yClose - yOpen), 2);
                ctx.fillRect(x - candleWidth / 2, Math.min(yOpen, yClose), candleWidth, bodyHeight);
            });
        }

        const ws = new WebSocket(`ws://${location.host}/ws`);
        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            const m = data.metrics;
            const t = data.trade;

            document.getElementById('live-price').innerText = '$' + (m.price || 0).toLocaleString();
            document.getElementById('live-time').innerText = 'Last Tick: ' + data.timestamp;
            document.getElementById('regime').innerText = m.regime || '--';
            document.getElementById('stage-score').innerText = (m.stage || '--') + ' (' + (m.score || 0) + '/100)';
            document.getElementById('atr').innerText = '$' + (m.atr || 0);
            document.getElementById('move-pot').innerText = m.potential || 'N/A';

            document.getElementById('score-struct').innerText = (m.score_structure || 0) + '/100';
            document.getElementById('score-vol').innerText = (m.score_volume || 0) + '/100';
            document.getElementById('score-mom').innerText = (m.score_momentum || 0) + '/100';
            document.getElementById('score-liq').innerText = (m.score_liquidity || 0) + '/100';

            document.getElementById('r-low').innerText = '$' + (m.range_low || 0).toLocaleString();
            document.getElementById('r-high').innerText = '$' + (m.range_high || 0).toLocaleString();

            const cvdEl = document.getElementById('cvd-badge');
            cvdEl.innerText = 'Delta: ' + (m.cvd > 0 ? '+' : '') + (m.cvd || 0) + ' BTC';
            cvdEl.style.color = m.cvd >= 0 ? '#10b981' : '#ef4444';

            // Draw Chart
            renderNativeChart(data.chart, m.range_low, m.range_high);

            // Active Trade Updates
            if (t && t.active) {
                document.getElementById('no-trade').style.display = 'none';
                document.getElementById('trade-panel').style.display = 'block';
                document.getElementById('trade-id').innerText = t.id;
                document.getElementById('trade-entry').innerText = 'Entry: $' + t.entry.toLocaleString();
                
                const pnlEl = document.getElementById('trade-pnl');
                pnlEl.innerText = (t.pnl > 0 ? '+' : '') + t.pnl + ' pts (' + (t.pnl_pct > 0 ? '+' : '') + t.pnl_pct + '%)';
                pnlEl.style.color = t.pnl >= 0 ? '#10b981' : '#ef4444';

                const badge = document.getElementById('trade-badge');
                badge.innerText = t.direction;
                badge.style.background = t.direction === 'LONG' ? '#065f46' : '#7f1d1d';
                badge.style.color = t.direction === 'LONG' ? '#34d399' : '#f87171';

                document.getElementById('t-sl').innerText = '$' + t.sl.toLocaleString();
                document.getElementById('t-t1').innerText = '$' + t.t1.toLocaleString();
                document.getElementById('t-t2').innerText = '$' + t.t2.toLocaleString();
                document.getElementById('t-t3').innerText = '$' + t.t3.toLocaleString();
                document.getElementById('trade-rec').innerText = t.rec;
            } else {
                document.getElementById('no-trade').style.display = 'block';
                document.getElementById('trade-panel').style.display = 'none';
            }
        };

        async function triggerSim(dir) {
            await fetch(`/api/simulate?direction=${dir}`, { method: 'POST' });
        }
        async function clearTrade() {
            await fetch('/api/clear', { method: 'POST' });
        }
    </script>
</body>
</html>
"""

@app.get("/")
async def get_dashboard():
    return HTMLResponse(HTML_PAGE)

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except Exception:
        connected_clients.discard(ws)

@app.post("/api/simulate")
async def simulate_trade(direction: str):
    global active_trade
    p = latest_metrics.get("price", 84000.0)
    atr = latest_metrics.get("atr", 85.0)
    trade_id = f"SIM-{direction}-{datetime.now().strftime('%H%M%S')}"
    
    if direction == "LONG":
        active_trade = {
            "id": trade_id, "time": datetime.now().strftime('%H:%M'), "direction": "LONG",
            "stage": "STAGE 2: EARLY LONG SETUP", "score": 85, "entry": p,
            "sl": round(p - (0.4 * atr), 1), "t1": round(p + (1.2 * atr), 1),
            "t2": round(p + (2.5 * atr), 1), "t3": round(p + (4.5 * atr), 1),
            "extended": round(p + (8.0 * atr), 1), "potential": "LARGE MOVE", "status": "ACTIVE", "stage_state": 0
        }
    else:
        active_trade = {
            "id": trade_id, "time": datetime.now().strftime('%H:%M'), "direction": "SHORT",
            "stage": "STAGE 2: EARLY SHORT SETUP", "score": 85, "entry": p,
            "sl": round(p + (0.4 * atr), 1), "t1": round(p - (1.2 * atr), 1),
            "t2": round(p - (2.5 * atr), 1), "t3": round(p - (4.5 * atr), 1),
            "extended": round(p - (8.0 * atr), 1), "potential": "LARGE MOVE", "status": "ACTIVE", "stage_state": 0
        }
    return {"status": "ok", "trade": active_trade}

@app.post("/api/clear")
async def clear_active_trade():
    global active_trade
    active_trade = None
    return {"status": "cleared"}

# ==================== BOOTSTRAP ====================
async def run_server():
    config = uvicorn.Config(app=app, host="127.0.0.1", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    init_db()
    await preload_history()
    await asyncio.gather(
        trade_stream(),
        kline_stream(),
        broadcast_ui_state(),
        run_server()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown.")