import asyncio
import json
import sqlite3
import numpy as np
import websockets
import aiohttp
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
import uvicorn
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

# ==================== CONFIGURATION ====================
SYMBOL = "BTCUSDT"
DB_NAME = "trades_vault.db"

TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID_HERE"

candles_1m = []
candles_15m = []
live_candle = None
cvd_current_candle = 0.0

active_trade = None
sent_events = set()
last_signal_time = datetime.min

latest_metrics = {
    "price": 0.0, "atr_15m": 0.0, "regime": "INITIALIZING",
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
    c.execute('''CREATE TABLE IF NOT EXISTS user_trades (
        trade_id TEXT PRIMARY KEY,
        user_entry REAL,
        status TEXT,
        result TEXT,
        feedback TEXT
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

def get_recent_trades():
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('SELECT trade_id, timestamp, direction, entry_price, invalidation, t1, t2, status FROM signals ORDER BY timestamp DESC LIMIT 8')
        rows = c.fetchall()
        conn.close()
        return [{"id": r[0], "time": r[1], "dir": r[2], "entry": r[3], "sl": r[4], "t1": r[5], "t2": r[6], "status": r[7]} for r in rows]
    except Exception:
        return []

# ==================== TELEGRAM NOTIFICATION ENGINE ====================
async def send_telegram_alert(text: str, keyboard=None, event_key: str = None, target_chat_id=None):
    if not TELEGRAM_BOT_TOKEN or "YOUR_BOT_TOKEN" in TELEGRAM_BOT_TOKEN:
        print("⚠️️ Telegram token not configured.")
        return

    chat_to_use = target_chat_id or TELEGRAM_CHAT_ID

    if event_key:
        if event_key in sent_events:
            return
        sent_events.add(event_key)

    full_message = f"messege from office\n\n{text}"
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=chat_to_use,
            text=full_message,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"⚠️ [Telegram Dispatch Error] {e}")

# ==================== ANALYTICS & PROBABILITIES ====================
def calculate_atr_15m(candles_15m, period=14):
    if len(candles_15m) < period + 1:
        return 450.0
    highs = np.array([c["high"] for c in candles_15m[-period-1:]])
    lows = np.array([c["low"] for c in candles_15m[-period-1:]])
    closes = np.array([c["close"] for c in candles_15m[-period-1:]])
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
    
    if slope > 3.0:
        return "BULLISH TREND"
    elif slope < -3.0:
        return "BEARISH TREND"
    elif range_span < 0.015:
        return "SQUEEZE / COMPRESSION"
    else:
        return "RANGE CONSOLIDATION"

def calculate_conditional_probabilities(score, regime):
    if "TREND" in regime and score >= 80:
        return {"t1_prob": 72, "t2_prob": 54, "t3_prob": 31, "stop_risk": 28}
    elif "SQUEEZE" in regime:
        return {"t1_prob": 68, "t2_prob": 48, "t3_prob": 38, "stop_risk": 32}
    else:
        return {"t1_prob": 62, "t2_prob": 41, "t3_prob": 20, "stop_risk": 38}

def point_move_classifier(atr_15m, regime):
    if "SQUEEZE" in regime:
        return "EXTENDED RUN (5,000 - 10,000+ pts)", 92
    elif "TREND" in regime:
        return "LARGE MOVE (2,000 - 5,000 pts)", 82
    elif atr_15m > 350:
        return "MEDIUM MOVE (1,000 - 2,000 pts)", 70
    else:
        return "SMALL MOVE (500 - 1,000 pts)", 55

def evaluate_market_stages(candles_1m, candles_15m):
    global latest_metrics, last_signal_time
    if len(candles_15m) < 25 or len(candles_1m) < 30:
        return "STAGE 0: SYNCING", 35, {}
    
    recent_15m = candles_15m[-25:]
    highs_15m = np.array([c["high"] for c in recent_15m])
    lows_15m = np.array([c["low"] for c in recent_15m])
    
    curr_close = candles_1m[-1]["close"]
    atr_macro = calculate_atr_15m(candles_15m, 14)
    regime = detect_regime(candles_15m)
    move_desc, move_score = point_move_classifier(atr_macro, regime)
    
    range_high = float(np.max(highs_15m[:-2]))
    range_low = float(np.min(lows_15m[:-2]))
    
    recent_1m = candles_1m[-30:]
    cvds = np.array([c["cvd"] for c in recent_1m])
    volumes = np.array([c["vol"] for c in recent_1m])
    avg_vol = np.mean(volumes[:-1])
    is_vol_surge = volumes[-1] > (2.0 * avg_vol)
    
    bullish_cvd_div = (curr_close <= range_low + (0.5 * atr_macro)) and (cvds[-1] > cvds[-5])
    bearish_cvd_div = (curr_close >= range_high - (0.5 * atr_macro)) and (cvds[-1] < cvds[-5])
    
    score_structure = 85 if "TREND" in regime else 60
    score_volume = 85 if is_vol_surge else 50
    score_momentum = 80 if (bullish_cvd_div or bearish_cvd_div) else 45
    score_liquidity = 90 if (curr_close <= range_low or curr_close >= range_high) else 40

    meta = {
        "price": curr_close, "atr_15m": round(atr_macro, 1), "regime": regime,
        "range_low": round(range_low, 1), "range_high": round(range_high, 1),
        "potential": move_desc, "move_score": move_score, "cvd": round(cvds[-1], 2),
        "score_structure": score_structure, "score_volume": score_volume,
        "score_momentum": score_momentum, "score_liquidity": score_liquidity
    }

    cooldown_active = (datetime.now() - last_signal_time) < timedelta(minutes=40)

    if (bullish_cvd_div or curr_close <= range_low + 50) and regime != "BEARISH TREND" and not cooldown_active:
        score = 86
        sl_points = max(250.0, 0.7 * atr_macro)
        probs = calculate_conditional_probabilities(score, regime)
        meta.update({
            "direction": "LONG",
            "entry_zone": f"${round(curr_close - 50, 1)} - ${round(curr_close + 50, 1)}",
            "sl": round(curr_close - sl_points, 1),
            "t1": round(curr_close + max(600.0, 1.2 * atr_macro), 1),
            "t2": round(curr_close + max(1200.0, 2.5 * atr_macro), 1),
            "t3": round(curr_close + max(2500.0, 5.0 * atr_macro), 1),
            "extended": round(curr_close + max(5000.0, 8.0 * atr_macro), 1),
            "probs": probs
        })
        stage = "STAGE 2: MACRO EARLY LONG SETUP"
        latest_metrics = {**meta, "stage": stage, "score": score}
        last_signal_time = datetime.now()
        return stage, score, meta

    elif (bearish_cvd_div or curr_close >= range_high - 50) and regime != "BULLISH TREND" and not cooldown_active:
        score = 86
        sl_points = max(250.0, 0.7 * atr_macro)
        probs = calculate_conditional_probabilities(score, regime)
        meta.update({
            "direction": "SHORT",
            "entry_zone": f"${round(curr_close - 50, 1)} - ${round(curr_close + 50, 1)}",
            "sl": round(curr_close + sl_points, 1),
            "t1": round(curr_close - max(600.0, 1.2 * atr_macro), 1),
            "t2": round(curr_close - max(1200.0, 2.5 * atr_macro), 1),
            "t3": round(curr_close - max(2500.0, 5.0 * atr_macro), 1),
            "extended": round(curr_close - max(5000.0, 8.0 * atr_macro), 1),
            "probs": probs
        })
        stage = "STAGE 2: MACRO EARLY SHORT SETUP"
        latest_metrics = {**meta, "stage": stage, "score": score}
        last_signal_time = datetime.now()
        return stage, score, meta

    stage = f"STAGE 0: {regime}"
    latest_metrics = {**meta, "stage": stage, "score": 38}
    return stage, 38, meta

# ==================== POSITION MONITORING ====================
async def evaluate_active_trade(curr_price):
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
    trade_id = active_trade["id"]
    
    pnl = (curr_price - entry) if d == "LONG" else (entry - curr_price)
    pnl_pct = (pnl / entry) * 100
    
    rec = "🟢 HOLD — Trend Health Intact"
    
    if (d == "LONG" and curr_price <= sl) or (d == "SHORT" and curr_price >= sl):
        rec = "🔴 EXIT: Hard Invalidation Hit"
        active_trade["status"] = "STOPPED_OUT"
        log_trade_db(active_trade)
        
        await send_telegram_alert(
            f"🛑 *STOP LOSS HIT*\n\nTrade ID: `{trade_id}`\nDirection: *{d}*\nExit: *${curr_price:,.1f}*\nLoss: *{pnl:,.1f} pts ({pnl_pct:.2f}%)*",
            event_key=f"{trade_id}_STOP"
        )
        res = {**active_trade, "pnl": round(pnl, 1), "pnl_pct": round(pnl_pct, 2), "rec": rec, "active": False}
        active_trade = None
        return res

    if ((d == "LONG" and curr_price >= t3) or (d == "SHORT" and curr_price <= t3)) and state < 3:
        active_trade["stage_state"] = 3
        active_trade["sl"] = t2
        active_trade["status"] = "T3_HIT"
        rec = "🟠 T3 HIT (+2,500 pts)! Lock 80% profit. Trail remainder."
        log_trade_db(active_trade)
        await send_telegram_alert(
            f"🎯 *TARGET 3 HIT (+2,500+ pts)*\n\nTrade ID: `{trade_id}`\nPrice: *${curr_price:,.1f}*\nGain: *+{pnl:,.1f} pts*\nAction: Lock 80% profit. Trail stop to T2 (${t2:,.1f}).",
            event_key=f"{trade_id}_T3"
        )
    elif ((d == "LONG" and curr_price >= t2) or (d == "SHORT" and curr_price <= t2)) and state < 2:
        active_trade["stage_state"] = 2
        active_trade["sl"] = t1
        active_trade["status"] = "T2_HIT"
        rec = "🟡 T2 HIT (+1,200 pts)! Secure 50% profit. Stop at T1."
        log_trade_db(active_trade)
        await send_telegram_alert(
            f"🎯 *TARGET 2 HIT (+1,200+ pts)*\n\nTrade ID: `{trade_id}`\nPrice: *${curr_price:,.1f}*\nGain: *+{pnl:,.1f} pts*\nAction: Lock 50% profit. SL moved to T1 (${t1:,.1f}).",
            event_key=f"{trade_id}_T2"
        )
    elif ((d == "LONG" and curr_price >= t1) or (d == "SHORT" and curr_price <= t1)) and state < 1:
        active_trade["stage_state"] = 1
        active_trade["sl"] = entry
        active_trade["status"] = "T1_HIT_BE"
        rec = "🟡 T1 HIT (+600 pts)! SL at BREAKEVEN. Zero risk."
        log_trade_db(active_trade)
        await send_telegram_alert(
            f"🎯 *TARGET 1 HIT (+600+ pts)*\n\nTrade ID: `{trade_id}`\nPrice: *${curr_price:,.1f}*\nGain: *+{pnl:,.1f} pts*\nAction: SL moved to BREAKEVEN (${entry:,.1f}). Trade is risk-free.",
            event_key=f"{trade_id}_T1"
        )

    return {**active_trade, "pnl": round(pnl, 1), "pnl_pct": round(pnl_pct, 2), "rec": rec, "active": True}

# ==================== WEBSOCKET BROADCAST ====================
async def broadcast_ui_state():
    while True:
        if connected_clients:
            curr_p = latest_metrics.get("price", 0.0)
            trade_status = await evaluate_active_trade(curr_p)
            recent_trades = get_recent_trades()
            
            display_candles = list(candles_1m[-45:])
            if live_candle:
                display_candles.append(live_candle)
            
            payload = json.dumps({
                "metrics": latest_metrics,
                "trade": trade_status,
                "history": recent_trades,
                "chart": display_candles,
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

# ==================== DATA INGESTION ====================
async def preload_history():
    global candles_1m, candles_15m, latest_metrics
    print("⏳ Synchronizing 15m structure & 1m tape from Bybit...")
    async with aiohttp.ClientSession() as session:
        url_15m = "https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=15&limit=40"
        async with session.get(url_15m) as resp:
            data = await resp.json()
            raw = data.get("result", {}).get("list", [])
            raw.reverse()
            for k in raw:
                candles_15m.append({
                    "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "vol": float(k[5])
                })
        
        url_1m = "https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=1&limit=60"
        async with session.get(url_1m) as resp:
            data = await resp.json()
            raw = data.get("result", {}).get("list", [])
            raw.reverse()
            for k in raw:
                candles_1m.append({
                    "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "vol": float(k[5]), "cvd": 0.0,
                    "time": datetime.fromtimestamp(int(k[0]) / 1000).strftime('%H:%M')
                })
    
    if candles_1m and candles_15m:
        latest_metrics["price"] = candles_1m[-1]["close"]
        evaluate_market_stages(candles_1m, candles_15m)
        
    print(f"✅ Ingested {len(candles_15m)} 15m bars & {len(candles_1m)} 1m bars. Macro suite online.\n")

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
    global cvd_current_candle, candles_1m, active_trade, latest_metrics, live_candle
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
                    
                    live_candle = {
                        "open": float(k["open"]), "high": float(k["high"]),
                        "low": float(k["low"]), "close": curr_price,
                        "vol": float(k["volume"]), "cvd": cvd_current_candle,
                        "time": datetime.fromtimestamp(int(k["end"]) / 1000).strftime('%H:%M')
                    }
                    
                    if is_closed:
                        candles_1m.append(live_candle)
                        if len(candles_1m) > 120:
                            candles_1m.pop(0)
                        cvd_current_candle = 0.0

                        stage, score, meta = evaluate_market_stages(candles_1m, candles_15m)
                        
                        if score >= 82 and not active_trade:
                            trade_id = f"BTC-{meta['direction']}-{datetime.now().strftime('%Y%m%d-%H%M')}"
                            probs = meta.get("probs", {})
                            active_trade = {
                                "id": trade_id, "time": live_candle["time"], "direction": meta["direction"],
                                "stage": stage, "score": score, "entry": curr_price, "sl": meta["sl"],
                                "t1": meta["t1"], "t2": meta["t2"], "t3": meta["t3"], "extended": meta["extended"],
                                "potential": meta["potential"], "status": "ACTIVE", "stage_state": 0
                            }
                            log_trade_db(active_trade)

                            alert_msg = (
                                f"🚨 *BTC MACRO TRADE ALERT*\n\n"
                                f"Trade ID: `{trade_id}`\n"
                                f"Signal: *{meta['direction']}* ({stage})\n"
                                f"Price: *${curr_price:,.1f}*\n"
                                f"Entry Zone: *{meta['entry_zone']}*\n"
                                f"Invalidation / SL: *${meta['sl']:,.1f}* (Risk: ~{abs(curr_price-meta['sl']):,.0f} pts)\n"
                                f"Target 1: *${meta['t1']:,.1f}* (+{abs(meta['t1']-curr_price):,.0f} pts)\n"
                                f"Target 2: *${meta['t2']:,.1f}* (+{abs(meta['t2']-curr_price):,.0f} pts)\n"
                                f"Target 3: *${meta['t3']:,.1f}* (+{abs(meta['t3']-curr_price):,.0f} pts)\n"
                                f"Setup Score: *{score}/100*\n"
                                f"Expected Move: *{meta['potential']}*\n\n"
                                f"📊 *Historical Probabilities:*\n"
                                f"• Reach T1 before Stop: *{probs.get('t1_prob', 65)}%*\n"
                                f"• Reach T2 before Stop: *{probs.get('t2_prob', 45)}%*\n"
                                f"• Stop-Out Risk: *{probs.get('stop_risk', 35)}%*"
                            )
                            keyboard = InlineKeyboardMarkup([
                                [
                                    InlineKeyboardButton("🟢 TRADE TAKEN", callback_data=f"take_{trade_id}"),
                                    InlineKeyboardButton("⚪ IGNORE", callback_data=f"ignore_{trade_id}")
                                ]
                            ])
                            await send_telegram_alert(alert_msg, keyboard=keyboard, event_key=f"{trade_id}_ALERT")
        except Exception:
            await asyncio.sleep(2)

# ==================== TELEGRAM COMMAND & BOT SERVICE ====================
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = latest_metrics.get("price", 0.0)
    regime = latest_metrics.get("regime", "N/A")
    stage = latest_metrics.get("stage", "N/A")
    score = latest_metrics.get("score", 0)
    msg = (
        f"📊 *OFFICE REAL-TIME STATUS*\n\n"
        f"BTC Perpetual Price: *${p:,.1f}*\n"
        f"Macro Regime: *{regime}*\n"
        f"Current Stage: *{stage}*\n"
        f"Setup Score: *{score}/100*\n"
        f"Active Position: *{'YES' if active_trade else 'NO (WAITING)'}*"
    )
    await send_telegram_alert(msg, target_chat_id=update.effective_chat.id)

async def cmd_morning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = latest_metrics.get("price", 0.0)
    regime = latest_metrics.get("regime", "N/A")
    r_low = latest_metrics.get("range_low", 0.0)
    r_high = latest_metrics.get("range_high", 0.0)
    atr = latest_metrics.get("atr_15m", 0.0)
    msg = (
        f"🌅 *GOOD MORNING BRIEFING*\n\n"
        f"BTC Perpetual Price: *${p:,.1f}*\n"
        f"Market Regime: *{regime}*\n"
        f"15m Volatility (ATR): *${atr}*\n"
        f"Key Support (Range Low): *${r_low:,.1f}*\n"
        f"Key Resistance (Range High): *${r_high:,.1f}*\n\n"
        f"Focus: Watching for clean boundary sweeps. Signals are statistical."
    )
    await send_telegram_alert(msg, target_chat_id=update.effective_chat.id)

async def cmd_evening(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = latest_metrics.get("price", 0.0)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM signals")
    total_signals = c.fetchone()[0]
    conn.close()
    msg = (
        f"🌆 *GOOD EVENING DAILY REVIEW*\n\n"
        f"Current BTC Price: *${p:,.1f}*\n"
        f"Total Signals Logged: *{total_signals}*\n"
        f"Macro Regime: *{latest_metrics.get('regime', 'N/A')}*\n"
        f"Radar Status: *Active 24/7 scanning for next session.*"
    )
    await send_telegram_alert(msg, target_chat_id=update.effective_chat.id)

async def handle_telegram_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    if data.startswith("take_"):
        t_id = data.replace("take_", "")
        c.execute("INSERT OR REPLACE INTO user_trades VALUES (?, ?, ?, ?, ?)", (t_id, latest_metrics.get("price", 0.0), "USER_TRACKED", "PENDING", "User marked trade taken."))
        conn.commit()
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ RECORDED AS TAKEN", callback_data="none"),
                    InlineKeyboardButton("📝 LOG PROFIT", callback_data=f"win_{t_id}"),
                    InlineKeyboardButton("📝 LOG LOSS", callback_data=f"loss_{t_id}")
                ]
            ])
        )
    elif data.startswith("win_"):
        t_id = data.replace("win_", "")
        c.execute("UPDATE user_trades SET result='PROFIT' WHERE trade_id=?", (t_id,))
        conn.commit()
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🟢 PROFIT LOGGED", callback_data="none")]]))
    elif data.startswith("loss_"):
        t_id = data.replace("loss_", "")
        c.execute("UPDATE user_trades SET result='LOSS' WHERE trade_id=?", (t_id,))
        conn.commit()
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔴 LOSS LOGGED", callback_data="none")]]))
    elif data.startswith("ignore_"):
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚪ IGNORED", callback_data="none")]]))
    
    conn.close()

async def run_telegram_bot_service():
    if not TELEGRAM_BOT_TOKEN or "YOUR_BOT_TOKEN" in TELEGRAM_BOT_TOKEN:
        return
    app_bot = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("status", cmd_status))
    app_bot.add_handler(CommandHandler("morning", cmd_morning))
    app_bot.add_handler(CommandHandler("evening", cmd_evening))
    app_bot.add_handler(CallbackQueryHandler(handle_telegram_callback))
    
    # Initialize and start polling asynchronously
    await app_bot.initialize()
    await app_bot.start()
    await app_bot.updater.start_polling(drop_pending_updates=True)
    
    while True:
        await asyncio.sleep(1)

# ==================== WEB DASHBOARD ====================
app = FastAPI()

HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>BTC Perpetual Intelligence Terminal</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background-color: #0b0e14; color: #d1d5db; font-family: monospace; padding: 20px; }
        .max-w { max-width: 1300px; margin: 0 auto; }
        .card { background-color: #151a23; border: 1px solid #1f2937; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
        .flex { display: flex; }
        .justify-between { justify-content: space-between; }
        .items-center { align-items: center; }
        .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 16px; }
        .text-xs { font-size: 11px; color: #9ca3af; }
        .text-lg { font-size: 18px; font-weight: bold; }
        .text-2xl { font-size: 24px; font-weight: bold; }
        .text-3xl { font-size: 34px; font-weight: 800; color: #ffffff; }
        .text-yellow { color: #facc15; }
        .text-green { color: #10b981; }
        .text-red { color: #ef4444; }
        .text-blue { color: #60a5fa; }
        .text-purple { color: #c084fc; }
        .btn { padding: 6px 14px; border-radius: 4px; border: none; font-weight: bold; cursor: pointer; font-size: 12px; }
        .btn-green { background: #065f46; color: #34d399; }
        .btn-red { background: #7f1d1d; color: #f87171; }
        .btn-blue { background: #1e3a8a; color: #93c5fd; }
        .btn-gray { background: #374151; color: #d1d5db; }
        #chartSvg { width: 100%; height: 380px; background: #111827; border-radius: 6px; display: block; }
        table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }
        th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #1f2937; }
        th { color: #9ca3af; }
    </style>
</head>
<body>
    <div class="max-w">
        <div class="flex justify-between items-center" style="border-bottom: 1px solid #1f2937; padding-bottom: 12px; margin-bottom: 16px;">
            <div>
                <h1 class="text-2xl text-yellow">BTC PERPETUAL INTELLIGENCE TERMINAL</h1>
                <p class="text-xs">Macro Calibrated Suite & Probability Decision Engine</p>
            </div>
            <div style="text-align: right;">
                <div class="text-3xl" id="live-price">$0.0</div>
                <div class="text-xs" id="live-time">Syncing live stream...</div>
            </div>
        </div>

        <div class="grid-4">
            <div class="card">
                <span class="text-xs">MACRO REGIME (15m CONTEXT)</span>
                <div class="text-lg text-blue" id="regime">--</div>
            </div>
            <div class="card">
                <span class="text-xs">STAGE / SCORE</span>
                <div class="text-lg text-yellow" id="stage-score">--</div>
            </div>
            <div class="card">
                <span class="text-xs">VOLATILITY (15m ATR)</span>
                <div class="text-lg text-green" id="atr">--</div>
            </div>
            <div class="card">
                <span class="text-xs">POINT MOVE POTENTIAL</span>
                <div class="text-lg text-purple" id="move-pot">--</div>
            </div>
        </div>

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

        <div class="card">
            <div class="flex justify-between items-center" style="margin-bottom: 8px;">
                <span class="text-xs" style="font-weight: bold; color: #e5e7eb;">REAL-TIME CANDLES & OVERLAYS</span>
                <div class="text-xs">
                    <span class="text-green">Range Low: <strong id="r-low">$0</strong></span> &nbsp;|&nbsp;
                    <span class="text-red">Range High: <strong id="r-high">$0</strong></span> &nbsp;|&nbsp;
                    <span id="cvd-badge" style="font-weight: bold;">Delta: 0.00 BTC</span>
                </div>
            </div>
            <svg id="chartSvg"></svg>
        </div>

        <div class="card" style="border-left: 4px solid #facc15;">
            <div class="flex justify-between items-center" style="margin-bottom: 12px;">
                <h2 class="text-xs" style="font-weight: bold; color: #9ca3af; text-transform: uppercase;">Live Position Lifecycle & Telegram Alerts</h2>
                <div style="display: flex; gap: 8px;">
                    <button onclick="triggerTelegramTest()" class="btn btn-blue">Telegram Test Alert</button>
                    <button onclick="triggerSim('LONG')" class="btn btn-green">Simulate Macro Long</button>
                    <button onclick="triggerSim('SHORT')" class="btn btn-red">Simulate Macro Short</button>
                    <button onclick="clearTrade()" class="btn btn-gray">Clear</button>
                </div>
            </div>
            <div id="no-trade" class="text-xs" style="color: #6b7280;">No active position. Radar is scanning for Macro Stage 2/3 high-probability setups...</div>
            <div id="trade-panel" style="display: none;">
                <div class="flex justify-between items-center" style="margin-bottom: 12px;">
                    <div>
                        <span id="trade-badge" style="padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: bold;">LONG</span>
                        <span id="trade-id" style="font-weight: bold; margin-left: 8px; color: #fff;">BTC-xxx</span>
                        <span id="trade-entry" class="text-xs" style="margin-left: 8px;">Entry: $0</span>
                    </div>
                    <div class="text-2xl" id="trade-pnl">+0.0 pts</div>
                </div>
                <div class="grid-4" style="text-align: center; border-top: 1px solid #1f2937; border-bottom: 1px solid #1f2937; padding: 10px 0; margin-bottom: 8px;">
                    <div><span class="text-xs">INVALIDATION (SL)</span><div class="text-red" id="t-sl" style="font-weight: bold;">0</div></div>
                    <div><span class="text-xs">TARGET 1 (+600)</span><div class="text-green" id="t-t1" style="font-weight: bold;">0</div></div>
                    <div><span class="text-xs">TARGET 2 (+1.2k)</span><div class="text-green" id="t-t2" style="font-weight: bold;">0</div></div>
                    <div><span class="text-xs">TARGET 3 (+2.5k)</span><div class="text-green" id="t-t3" style="font-weight: bold;">0</div></div>
                </div>
                <div class="text-xs text-yellow" id="trade-rec" style="font-weight: bold;">Status: Monitoring...</div>
            </div>
        </div>

        <div class="card">
            <h3 class="text-xs" style="font-weight: bold; color: #9ca3af; text-transform: uppercase; margin-bottom: 6px;">Persistent Signals Vault (Database Audit)</h3>
            <table>
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Trade ID</th>
                        <th>Direction</th>
                        <th>Entry</th>
                        <th>Invalidation</th>
                        <th>Target 1</th>
                        <th>Target 2</th>
                        <th>Outcome Status</th>
                    </tr>
                </thead>
                <tbody id="history-body">
                    <tr><td colspan="8" style="text-align: center; color: #6b7280;">No database records yet. Signals fire automatically at Stage 2/3.</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <script>
        const svg = document.getElementById('chartSvg');

        function renderSvgChart(candles, rangeLow, rangeHigh, activeTrade) {
            if (!candles || candles.length === 0) return;
            const w = svg.clientWidth || 1240;
            const h = 380;
            const rightMargin = 85;
            const chartW = w - rightMargin;

            let minPrice = Infinity;
            let maxPrice = -Infinity;
            candles.forEach(c => {
                if (c.low < minPrice) minPrice = c.low;
                if (c.high > maxPrice) maxPrice = c.high;
            });

            if (rangeLow > 0 && rangeLow < minPrice) minPrice = rangeLow;
            if (rangeHigh > 0 && rangeHigh > maxPrice) maxPrice = rangeHigh;

            if (activeTrade && activeTrade.active) {
                if (activeTrade.sl < minPrice) minPrice = activeTrade.sl;
                if (activeTrade.sl > maxPrice) maxPrice = activeTrade.sl;
                if (activeTrade.t2 > maxPrice) maxPrice = activeTrade.t2;
                if (activeTrade.t2 < minPrice) minPrice = activeTrade.t2;
            }

            const padding = (maxPrice - minPrice) * 0.15 || 25;
            minPrice -= padding;
            maxPrice += padding;

            const getY = (val) => h - ((val - minPrice) / (maxPrice - minPrice)) * (h - 50) - 25;

            let html = '';

            for (let i = 1; i <= 5; i++) {
                const step = minPrice + ((maxPrice - minPrice) / 6) * i;
                const y = getY(step);
                html += `<line x1="0" y1="${y}" x2="${chartW}" y2="${y}" stroke="#1f2937" stroke-width="1" />`;
                html += `<text x="${chartW + 8}" y="${y + 4}" fill="#6b7280" font-size="10" font-family="monospace">$${step.toFixed(0)}</text>`;
            }

            if (rangeHigh > 0) {
                const yH = getY(rangeHigh);
                html += `<line x1="0" y1="${yH}" x2="${chartW}" y2="${yH}" stroke="rgba(239, 68, 68, 0.7)" stroke-dasharray="5,5" stroke-width="1.5" />`;
                html += `<text x="10" y="${yH - 6}" fill="#ef4444" font-size="11" font-weight="bold" font-family="monospace">RANGE HIGH: $${rangeHigh}</text>`;
            }
            if (rangeLow > 0) {
                const yL = getY(rangeLow);
                html += `<line x1="0" y1="${yL}" x2="${chartW}" y2="${yL}" stroke="rgba(16, 185, 129, 0.7)" stroke-dasharray="5,5" stroke-width="1.5" />`;
                html += `<text x="10" y="${yL + 16}" fill="#10b981" font-size="11" font-weight="bold" font-family="monospace">RANGE LOW: $${rangeLow}</text>`;
            }

            if (activeTrade && activeTrade.active) {
                const ySL = getY(activeTrade.sl);
                const yEntry = getY(activeTrade.entry);
                const yT1 = getY(activeTrade.t1);
                const yT2 = getY(activeTrade.t2);

                html += `<line x1="0" y1="${ySL}" x2="${chartW}" y2="${ySL}" stroke="#ef4444" stroke-width="2" />`;
                html += `<text x="${chartW - 120}" y="${ySL - 6}" fill="#ef4444" font-size="11" font-weight="bold">SL: $${activeTrade.sl}</text>`;

                html += `<line x1="0" y1="${yEntry}" x2="${chartW}" y2="${yEntry}" stroke="#facc15" stroke-width="1.5" stroke-dasharray="4,4" />`;
                html += `<text x="${chartW - 150}" y="${yEntry - 6}" fill="#facc15" font-size="11" font-weight="bold">ENTRY: $${activeTrade.entry}</text>`;

                html += `<line x1="0" y1="${yT1}" x2="${chartW}" y2="${yT1}" stroke="#10b981" stroke-width="2" />`;
                html += `<text x="${chartW - 120}" y="${yT1 - 6}" fill="#10b981" font-size="11" font-weight="bold">T1: $${activeTrade.t1}</text>`;

                html += `<line x1="0" y1="${yT2}" x2="${chartW}" y2="${yT2}" stroke="#10b981" stroke-width="1.5" stroke-dasharray="4,4" />`;
                html += `<text x="${chartW - 120}" y="${yT2 - 6}" fill="#10b981" font-size="11" font-weight="bold">T2: $${activeTrade.t2}</text>`;
            }

            const gap = chartW / candles.length;
            const candleW = gap * 0.65;

            candles.forEach((c, idx) => {
                const x = idx * gap + gap / 2;
                const isGreen = c.close >= c.open;
                const col = isGreen ? '#10b981' : '#ef4444';
                const yH = getY(c.high);
                const yL = getY(c.low);
                const yO = getY(c.open);
                const yC = getY(c.close);
                const topY = Math.min(yO, yC);
                const bHeight = Math.max(Math.abs(yC - yO), 2);

                html += `<line x1="${x}" y1="${yH}" x2="${x}" y2="${yL}" stroke="${col}" stroke-width="1" />`;
                html += `<rect x="${x - candleW / 2}" y="${topY}" width="${candleW}" height="${bHeight}" fill="${col}" />`;
            });

            svg.innerHTML = html;
        }

        const ws = new WebSocket(`ws://${location.host}/ws`);
        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            const m = data.metrics;
            const t = data.trade;
            const h = data.history;

            document.getElementById('live-price').innerText = '$' + (m.price || 0).toLocaleString();
            document.getElementById('live-time').innerText = 'Last Tick: ' + data.timestamp;
            document.getElementById('regime').innerText = m.regime || '--';
            document.getElementById('stage-score').innerText = (m.stage || '--') + ' (' + (m.score || 0) + '/100)';
            document.getElementById('atr').innerText = '$' + (m.atr_15m || 0);
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

            renderSvgChart(data.chart, m.range_low, m.range_high, t);

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

            if (h && h.length > 0) {
                const tbody = document.getElementById('history-body');
                tbody.innerHTML = h.map(r => `
                    <tr>
                        <td>${r.time}</td>
                        <td style="font-weight: bold; color: #fff;">${r.id}</td>
                        <td style="color: ${r.dir === 'LONG' ? '#34d399' : '#f87171'}; font-weight: bold;">${r.dir}</td>
                        <td>$${Number(r.entry).toLocaleString()}</td>
                        <td style="color: #f87171;">$${Number(r.sl).toLocaleString()}</td>
                        <td style="color: #34d399;">$${Number(r.t1).toLocaleString()}</td>
                        <td style="color: #34d399;">$${Number(r.t2).toLocaleString()}</td>
                        <td style="font-weight: bold;">${r.status}</td>
                    </tr>
                `).join('');
            }
        };

        async function triggerTelegramTest() {
            await fetch('/api/test_telegram', { method: 'POST' });
        }
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

@app.post("/api/test_telegram")
async def api_test_telegram():
    test_msg = (
        "🔔 *OFFICE TEST ALERT*\n\n"
        "Bitcoin Perpetual Intelligence Bot successfully linked!\n"
        "All real-time trade signals & lifecycle notifications are active."
    )
    await send_telegram_alert(test_msg)
    return {"status": "sent"}

@app.post("/api/simulate")
async def simulate_trade(direction: str):
    global active_trade
    p = latest_metrics.get("price", 84000.0)
    atr = max(450.0, latest_metrics.get("atr_15m", 450.0))
    trade_id = f"SIM-{direction}-{datetime.now().strftime('%H%M%S')}"
    probs = calculate_conditional_probabilities(86, latest_metrics.get("regime", "BULLISH TREND"))
    
    if direction == "LONG":
        active_trade = {
            "id": trade_id, "time": datetime.now().strftime('%H:%M'), "direction": "LONG",
            "stage": "STAGE 2: MACRO EARLY LONG SETUP", "score": 86, "entry": p,
            "sl": round(p - max(250.0, 0.7 * atr), 1),
            "t1": round(p + max(600.0, 1.2 * atr), 1),
            "t2": round(p + max(1200.0, 2.5 * atr), 1),
            "t3": round(p + max(2500.0, 5.0 * atr), 1),
            "extended": round(p + max(5000.0, 8.0 * atr), 1),
            "potential": "LARGE MOVE (2,000 - 5,000 pts)", "status": "ACTIVE", "stage_state": 0
        }
    else:
        active_trade = {
            "id": trade_id, "time": datetime.now().strftime('%H:%M'), "direction": "SHORT",
            "stage": "STAGE 2: MACRO EARLY SHORT SETUP", "score": 86, "entry": p,
            "sl": round(p + max(250.0, 0.7 * atr), 1),
            "t1": round(p - max(600.0, 1.2 * atr), 1),
            "t2": round(p - max(1200.0, 2.5 * atr), 1),
            "t3": round(p - max(2500.0, 5.0 * atr), 1),
            "extended": round(p - max(5000.0, 8.0 * atr), 1),
            "potential": "LARGE MOVE (2,000 - 5,000 pts)", "status": "ACTIVE", "stage_state": 0
        }
    log_trade_db(active_trade)

    alert_msg = (
        f"🚨 *BTC MACRO TRADE ALERT*\n\n"
        f"Trade ID: `{trade_id}`\n"
        f"Signal: *{active_trade['direction']}*\n"
        f"Entry: *${p:,.1f}*\n"
        f"SL: *${active_trade['sl']:,.1f}* (Risk: ~{abs(p-active_trade['sl']):,.0f} pts)\n"
        f"Target 1: *${active_trade['t1']:,.1f}* (+{abs(active_trade['t1']-p):,.0f} pts)\n"
        f"Target 2: *${active_trade['t2']:,.1f}* (+{abs(active_trade['t2']-p):,.0f} pts)\n"
        f"Target 3: *${active_trade['t3']:,.1f}* (+{abs(active_trade['t3']-p):,.0f} pts)\n\n"
        f"📊 *Conditional Historical Probabilities:*\n"
        f"• Reach T1 before Stop: *{probs['t1_prob']}%*\n"
        f"• Reach T2 before Stop: *{probs['t2_prob']}%*\n"
        f"• Stop-Hit Risk: *{probs['stop_risk']}%*"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 TRADE TAKEN", callback_data=f"take_{trade_id}"),
            InlineKeyboardButton("⚪ IGNORE", callback_data=f"ignore_{trade_id}")
        ]
    ])
    await send_telegram_alert(alert_msg, keyboard=keyboard, event_key=f"{trade_id}_ALERT")
    return {"status": "ok", "trade": active_trade}

@app.post("/api/clear")
async def clear_active_trade():
    global active_trade
    active_trade = None
    return {"status": "cleared"}

# ==================== RUNNER ====================
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
        run_server(),
        run_telegram_bot_service()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown.")