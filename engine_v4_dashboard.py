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
    "stage": "STAGE 0: SYNCING", "score": 0, "range_low": 0.0,
    "range_high": 0.0, "cvd": 0.0, "move_potential": "N/A"
}

connected_clients = set()

# ==================== DATABASE INITIALIZATION ====================
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

# ==================== ANALYTICS & FILTERS ====================
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
    
    if slope > 2.5:
        return "BULLISH TREND"
    elif slope < -2.5:
        return "BEARISH TREND"
    elif range_span < 0.012:
        return "SQUEEZE / COMPRESSION"
    else:
        return "RANGE CONSOLIDATION"

def point_move_classifier(atr, regime):
    if "SQUEEZE" in regime:
        return "EXTENDED MOVE (5,000 - 10,000+ pts)", 90
    elif "TREND" in regime:
        return "LARGE MOVE (2,000 - 5,000 pts)", 76
    elif atr > 110:
        return "MEDIUM MOVE (1,000 - 2,000 pts)", 60
    else:
        return "SMALL MOVE (500 - 1,000 pts)", 45

def evaluate_market_stages(candles_1m, candles_15m):
    global latest_metrics
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
    
    range_high = np.max(highs[:-4])
    range_low = np.min(lows[:-4])
    avg_vol = np.mean(volumes[:-1])
    is_vol_surge = volumes[-1] > (1.8 * avg_vol)
    
    bullish_cvd_div = (curr_low <= range_low) and (cvds[-1] > cvds[-4])
    bearish_cvd_div = (curr_high >= range_high) and (cvds[-1] < cvds[-4])
    bullish_reclaim = (lows[-2] < range_low) and (curr_close > range_low)
    bearish_reclaim = (highs[-2] > range_high) and (curr_close < range_high)

    meta = {
        "price": curr_close, "atr": round(atr, 2), "regime": regime,
        "range_low": round(range_low, 2), "range_high": round(range_high, 2),
        "potential": move_desc, "move_score": move_score, "cvd": round(cvds[-1], 2)
    }

    # Macro False-Signal Filter (Rule #26)
    # Don't short a Bullish Trend unless extreme exhaustion / structure failure occurs
    allow_short = (regime != "BULLISH TREND") or (bearish_cvd_div and bearish_reclaim)
    allow_long = (regime != "BEARISH TREND") or (bullish_cvd_div and bullish_reclaim)

    if (bullish_cvd_div or bullish_reclaim) and (curr_close >= range_low) and allow_long:
        score = 82 if bullish_cvd_div and bullish_reclaim else 74
        meta["direction"] = "LONG"
        meta["entry_zone"] = f"${round(range_low, 1)} -${round(range_low + (0.35 * atr), 1)}"
        meta["sl"] = round(curr_low - (0.4 * atr), 1)
        meta["t1"] = round(curr_close + (1.2 * atr), 1)
        meta["t2"] = round(curr_close + (2.5 * atr), 1)
        meta["t3"] = round(curr_close + (4.5 * atr), 1)
        meta["extended"] = round(curr_close + (8.0 * atr), 1)
        stage = "STAGE 2: EARLY LONG SETUP (ABSORPTION)"
        latest_metrics = {**meta, "stage": stage, "score": score}
        return stage, score, meta

    elif (bearish_cvd_div or bearish_reclaim) and (curr_close <= range_high) and allow_short:
        score = 82 if bearish_cvd_div and bearish_reclaim else 74
        meta["direction"] = "SHORT"
        meta["entry_zone"] = f"${round(range_high - (0.35 * atr), 1)} -${round(range_high, 1)}"
        meta["sl"] = round(curr_high + (0.4 * atr), 1)
        meta["t1"] = round(curr_close - (1.2 * atr), 1)
        meta["t2"] = round(curr_close - (2.5 * atr), 1)
        meta["t3"] = round(curr_close - (4.5 * atr), 1)
        meta["extended"] = round(curr_close - (8.0 * atr), 1)
        stage = "STAGE 2: EARLY SHORT SETUP (DISTRIBUTION)"
        latest_metrics = {**meta, "stage": stage, "score": score}
        return stage, score, meta

    elif curr_close > range_high and is_vol_surge and (regime != "BEARISH TREND"):
        stage = "STAGE 3: MOMENTUM BREAKOUT CONFIRMED"
        meta["direction"] = "LONG"
        meta["entry_zone"] = f"${round(range_high, 1)} -${round(curr_close, 1)}"
        meta["sl"] = round(range_high - (0.5 * atr), 1)
        meta["t1"] = round(curr_close + (1.5 * atr), 1)
        meta["t2"] = round(curr_close + (3.0 * atr), 1)
        meta["t3"] = round(curr_close + (6.0 * atr), 1)
        meta["extended"] = round(curr_close + (10.0 * atr), 1)
        latest_metrics = {**meta, "stage": stage, "score": 85}
        return stage, 85, meta

    stage = f"STAGE 0: {regime}"
    latest_metrics = {**meta, "stage": stage, "score": 35}
    return stage, 35, meta

# ==================== POST-ENTRY MONITORING ENGINE ====================
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
    
    # Stop-Loss Hit
    if (d == "LONG" and curr_price <= sl) or (d == "SHORT" and curr_price >= sl):
        rec = "🔴 EXIT: Hard Invalidation / Stop Reached"
        active_trade["status"] = "STOPPED_OUT"
        log_trade_db(active_trade)
        res = {**active_trade, "pnl": pnl, "pnl_pct": pnl_pct, "rec": rec, "active": False}
        active_trade = None
        return res

    # Target 3 Hit
    if ((d == "LONG" and curr_price >= t3) or (d == "SHORT" and curr_price <= t3)) and state < 3:
        active_trade["stage_state"] = 3
        active_trade["sl"] = t2
        active_trade["status"] = "T3_HIT"
        rec = "🟠 T3 HIT: Secure 80% profit! Run remainder with trailing SL."
        log_trade_db(active_trade)

    # Target 2 Hit
    elif ((d == "LONG" and curr_price >= t2) or (d == "SHORT" and curr_price <= t2)) and state < 2:
        active_trade["stage_state"] = 2
        active_trade["sl"] = t1
        active_trade["status"] = "T2_HIT"
        rec = "🟡 T2 HIT: Secure 50% profit. Stop shifted to T1."
        log_trade_db(active_trade)

    # Target 1 Hit
    elif ((d == "LONG" and curr_price >= t1) or (d == "SHORT" and curr_price <= t1)) and state < 1:
        active_trade["stage_state"] = 1
        active_trade["sl"] = entry
        active_trade["status"] = "T1_HIT_BE"
        rec = "🟡 T1 HIT: Stop moved to BREAKEVEN. Trade is zero risk."
        log_trade_db(active_trade)

    return {**active_trade, "pnl": round(pnl, 1), "pnl_pct": round(pnl_pct, 2), "rec": rec, "active": True}

# ==================== REAL-TIME WEBSOCKET BROADCASTER ====================
async def broadcast_ui_state():
    while True:
        if connected_clients:
            curr_p = latest_metrics.get("price", 0.0)
            trade_status = evaluate_active_trade(curr_p)
            payload = json.dumps({
                "metrics": latest_metrics,
                "trade": trade_status,
                "timestamp": datetime.now().strftime('%H:%M:%S')
            })
            dead_clients = set()
            for ws in connected_clients:
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead_clients.add(ws)
            connected_clients.difference_update(dead_clients)
        await asyncio.sleep(0.5)

# ==================== DATA INGESTION LOOPS ====================
async def preload_history():
    global candles_1m, candles_15m
    print("⏳ Synchronizing historical candles from Bybit...")
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
                    "time": datetime.fromtimestamp(int(k[0]) / 1000).strftime('%H:%M:%S')
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
    print("✅ Ingestion complete. Analytics and Dashboard online.\n")

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
                            "time": datetime.fromtimestamp(int(k["end"]) / 1000).strftime('%H:%M:%S')
                        }
                        candles_1m.append(candle_data)
                        if len(candles_1m) > 120:
                            candles_1m.pop(0)
                        cvd_current_candle = 0.0

                        stage, score, meta = evaluate_market_stages(candles_1m, candles_15m)
                        
                        if score >= 74 and not active_trade:
                            trade_id = f"BTC-{meta['direction']}-{datetime.now().strftime('%Y%m%d-%H%M')}"
                            active_trade = {
                                "id": trade_id, "time": candle_data["time"], "direction": meta["direction"],
                                "stage": stage, "score": score, "entry": curr_price, "sl": meta["sl"],
                                "t1": meta["t1"], "t2": meta["t2"], "t3": meta["t3"], "extended": meta["extended"],
                                "potential": meta["potential"], "status": "ACTIVE", "stage_state": 0
                            }
                            log_trade_db(active_trade)
                            print(f"\n🚨 [AUTO-DISPATCH SETUP] {trade_id} -> {meta['direction']} @ ${curr_price:,.1f}")
        except Exception:
            await asyncio.sleep(2)

# ==================== FASTAPI WEB INTERFACE ====================
app = FastAPI()

HTML_DASHBOARD = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Bitcoin Perpetual Intelligence Terminal</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background-color: #0b0e14; color: #d1d5db; font-family: monospace; }
        .card { background-color: #151a23; border: 1px solid #1f2937; }
    </style>
</head>
<body class="p-6">
    <div class="max-w-6xl mx-auto space-y-6">
        <!-- HEADER -->
        <div class="flex justify-between items-center border-b border-gray-800 pb-4">
            <div>
                <h1 class="text-2xl font-bold text-yellow-400">BTCUSDT PERPETUAL ENGINE</h1>
                <p class="text-xs text-gray-500">Stages 1-6 Dynamic State Machine & Opportunity Radar</p>
            </div>
            <div class="text-right">
                <div class="text-3xl font-extrabold text-white" id="live-price">$0.0</div>
                <div class="text-xs text-gray-400" id="live-time">Syncing...</div>
            </div>
        </div>

        <!-- REGIME & METRICS -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div class="card p-4 rounded-lg">
                <span class="text-xs text-gray-500 uppercase">Regime</span>
                <div class="text-lg font-bold text-blue-400" id="regime">--</div>
            </div>
            <div class="card p-4 rounded-lg">
                <span class="text-xs text-gray-500 uppercase">Stage / Score</span>
                <div class="text-lg font-bold text-yellow-400" id="stage-score">--</div>
            </div>
            <div class="card p-4 rounded-lg">
                <span class="text-xs text-gray-500 uppercase">Volatility / ATR</span>
                <div class="text-lg font-bold text-emerald-400" id="atr">--</div>
            </div>
            <div class="card p-4 rounded-lg">
                <span class="text-xs text-gray-500 uppercase">Expected Move</span>
                <div class="text-sm font-bold text-purple-400" id="move-pot">--</div>
            </div>
        </div>

        <!-- RANGE & CONSOLIDATION -->
        <div class="card p-4 rounded-lg flex justify-between items-center">
            <div>
                <span class="text-xs text-gray-500">DETECTED RANGE LOW</span>
                <div class="text-xl font-bold text-green-500" id="range-low">$0.0</div>
            </div>
            <div class="text-center px-4">
                <span class="text-xs text-gray-500">1m DELTA (CVD)</span>
                <div class="text-base font-bold" id="cvd-delta">0.00 BTC</div>
            </div>
            <div class="text-right">
                <span class="text-xs text-gray-500">DETECTED RANGE HIGH</span>
                <div class="text-xl font-bold text-red-500" id="range-high">$0.0</div>
            </div>
        </div>

        <!-- ACTIVE POSITION LIFECYCLE MONITOR -->
        <div class="card p-6 rounded-lg border-l-4 border-yellow-500">
            <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-4">Live Position Lifecycle</h2>
            <div id="no-trade" class="text-gray-600 text-sm">No active position under monitoring. Watching for Stage 2/3 signals...</div>
            <div id="trade-panel" class="hidden space-y-4">
                <div class="flex justify-between items-center">
                    <div>
                        <span class="px-2 py-1 text-xs font-bold rounded" id="trade-badge">LONG</span>
                        <span class="ml-2 font-bold text-white" id="trade-id">BTC-xxx</span>
                        <span class="text-xs text-gray-400 ml-2" id="trade-entry">Entry: $0</span>
                    </div>
                    <div class="text-right">
                        <div class="text-xl font-bold" id="trade-pnl">+0.0 pts</div>
                    </div>
                </div>
                <div class="grid grid-cols-4 gap-2 text-xs text-center border-t border-b border-gray-800 py-2">
                    <div><span class="text-gray-500">PROTECTED SL</span><div class="font-bold text-red-400" id="t-sl">0</div></div>
                    <div><span class="text-gray-500">TARGET 1</span><div class="font-bold text-emerald-400" id="t-t1">0</div></div>
                    <div><span class="text-gray-500">TARGET 2</span><div class="font-bold text-emerald-400" id="t-t2">0</div></div>
                    <div><span class="text-gray-500">TARGET 3</span><div class="font-bold text-emerald-400" id="t-t3">0</div></div>
                </div>
                <div class="text-sm font-bold text-yellow-300" id="trade-rec">Status: Monitoring...</div>
            </div>
        </div>
    </div>

    <script>
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
            document.getElementById('range-low').innerText = '$' + (m.range_low || 0).toLocaleString();
            document.getElementById('range-high').innerText = '$' + (m.range_high || 0).toLocaleString();

            const cvdEl = document.getElementById('cvd-delta');
            cvdEl.innerText = (m.cvd > 0 ? '+' : '') + (m.cvd || 0) + ' BTC';
            cvdEl.className = 'text-base font-bold ' + (m.cvd >= 0 ? 'text-green-400' : 'text-red-400');

            if (t && t.active) {
                document.getElementById('no-trade').classList.add('hidden');
                document.getElementById('trade-panel').classList.remove('hidden');
                document.getElementById('trade-id').innerText = t.id;
                document.getElementById('trade-entry').innerText = 'Entry: $' + t.entry.toLocaleString();
                
                const pnlEl = document.getElementById('trade-pnl');
                pnlEl.innerText = (t.pnl > 0 ? '+' : '') + t.pnl + ' pts (' + (t.pnl_pct > 0 ? '+' : '') + t.pnl_pct + '%)';
                pnlEl.className = 'text-xl font-bold ' + (t.pnl >= 0 ? 'text-green-400' : 'text-red-400');

                const badge = document.getElementById('trade-badge');
                badge.innerText = t.direction;
                badge.className = 'px-2 py-1 text-xs font-bold rounded ' + (t.direction === 'LONG' ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300');

                document.getElementById('t-sl').innerText = '$' + t.sl.toLocaleString();
                document.getElementById('t-t1').innerText = '$' + t.t1.toLocaleString();
                document.getElementById('t-t2').innerText = '$' + t.t2.toLocaleString();
                document.getElementById('t-t3').innerText = '$' + t.t3.toLocaleString();
                document.getElementById('trade-rec').innerText = t.rec;
            } else {
                document.getElementById('no-trade').classList.remove('hidden');
                document.getElementById('trade-panel').classList.add('hidden');
            }
        };
    </script>
</body>
</html>
"""

@app.get("/")
async def get_dashboard():
    return HTMLResponse(HTML_DASHBOARD)

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except Exception:
        connected_clients.discard(ws)

# ==================== INTEGRATED RUNNER ====================
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
        print("\nEngine safely terminated.")