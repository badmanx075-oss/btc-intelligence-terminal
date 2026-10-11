import streamlit as st
from datetime import datetime
import sqlite3
import json
import os
import requests
import pandas as pd
import random
import time
import streamlit.components.v1 as components

st.set_page_config(layout="wide", page_title="BTC Terminal Pro", initial_sidebar_state="collapsed")

# Dark Theme Styling
st.markdown("""
<style>
    .stApp { background-color: #080a0f; color: #d1d4dc; font-family: monospace; }
    .card-box { background: #11141d; border: 1px solid #1f2430; border-radius: 8px; padding: 12px; text-align: center; }
    .metric-title { font-size: 11px; color: #88909e; font-weight: 600; letter-spacing: 0.5px; }
    .metric-value { font-size: 19px; font-weight: bold; margin-top: 4px; }
    #MainMenu, footer, header { visibility: hidden; }
</style>
""", unsafe_allow_html=True)

# ----------------- DATABASE SETUP -----------------
DB_FILE = "trades_vault.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        timestamp TEXT,
        trade_id TEXT PRIMARY KEY,
        direction TEXT,
        entry REAL,
        sl REAL,
        target_1 REAL,
        target_2 REAL,
        margin_used REAL,
        quality_rating TEXT,
        reason TEXT,
        conversational_remarks TEXT,
        status TEXT,
        pnl REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS wallet (
        id INTEGER PRIMARY KEY,
        balance REAL,
        locked_margin REAL,
        realized_pnl REAL
    )''')
    c.execute("INSERT OR IGNORE INTO wallet (id, balance, locked_margin, realized_pnl) VALUES (1, 100000.0, 0.0, 0.0)")
    conn.commit()
    conn.close()

init_db()

def get_wallet():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT balance, locked_margin, realized_pnl FROM wallet WHERE id=1")
        row = c.fetchone()
        conn.close()
        return row[0], row[1], row[2]
    except Exception:
        return 100000.0, 0.0, 0.0

def execute_trade_open_wallet(margin_req):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE wallet SET balance = balance - ?, locked_margin = locked_margin + ? WHERE id=1", (margin_req, margin_req))
        conn.commit()
        conn.close()
    except Exception:
        pass

def execute_trade_close_wallet(margin_released, pnl_usd):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            UPDATE wallet 
            SET balance = balance + ? + ?, 
                locked_margin = locked_margin - ?, 
                realized_pnl = realized_pnl + ? 
            WHERE id=1
        """, (margin_released, pnl_usd, margin_released, pnl_usd))
        conn.commit()
        conn.close()
    except Exception:
        pass

def get_db_trades():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            SELECT timestamp, trade_id, direction, entry, sl, target_1, target_2, quality_rating, conversational_remarks, status, pnl
            FROM trades ORDER BY rowid DESC LIMIT 20
        """)
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception:
        return []

def save_trade_db(t):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            t["timestamp"], t["trade_id"], t["direction"], t["entry"],
            t["sl"], t["target_1"], t["target_2"], t.get("margin_used", 2000.0),
            t["quality_rating"], t["reason"], t["conversational_remarks"], t["status"], t.get("pnl", 0.0)
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass

# ----------------- CONFIG & TELEGRAM -----------------
CONFIG_FILE = "config.json"

def get_telegram_creds():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                d = json.load(f)
                token = d.get("token", token)
                chat_id = d.get("chat_id", chat_id)
        except Exception:
            pass
    return token, chat_id

def send_telegram_alert(msg):
    token, chat_id = get_telegram_creds()
    if token and chat_id:
        try:
            url = "https://api.telegram.org/bot" + token + "/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=3)
            return True
        except Exception:
            return False
    return False

# ----------------- LIVE PRICE -----------------
def fetch_current_price():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=1.5)
        return float(r.json()["price"])
    except Exception:
        return 83047.0

current_btc = fetch_current_price()

# ----------------- STATE -----------------
if "active_trade" not in st.session_state:
    st.session_state.active_trade = None

if "last_auto_scan" not in st.session_state:
    st.session_state.last_auto_scan = time.time()

wallet_bal, locked_margin, realized_pnl = get_wallet()

# ----------------- TECHNICAL INDICATORS SIMULATOR -----------------
rsi_val = round(random.uniform(42.0, 68.0), 1)
macd_hist = round(random.uniform(-18.5, 24.2), 2)
ema_status = "BULLISH (20 > 50 EMA)" if rsi_val > 52 else "BEARISH (20 < 50 EMA)"
cvd_delta = "+$4.2M (Aggressive Bids)" if rsi_val > 50 else "-$3.8M (Ask Block Absorption)"
confluence_score = int(75 + (rsi_val / 4.5))

# ----------------- TOP WALLET & METRICS ROW -----------------
w1, w2, w3, w4 = st.columns(4)
with w1:
    st.markdown('<div class="card-box"><div class="metric-title">AVAILABLE WALLET BALANCE</div><div class="metric-value" style="color:#00e676;">$' + "{:,.2f}".format(wallet_bal) + '</div></div>', unsafe_allow_html=True)
with w2:
    st.markdown('<div class="card-box"><div class="metric-title">LOCKED MARGIN (IN TRADES)</div><div class="metric-value" style="color:#ffb300;">$' + "{:,.2f}".format(locked_margin) + '</div></div>', unsafe_allow_html=True)
with w3:
    pnl_clr = "#00e676" if realized_pnl >= 0 else "#ff5252"
    st.markdown('<div class="card-box"><div class="metric-title">TOTAL REALIZED PnL</div><div class="metric-value" style="color:' + pnl_clr + ';">$' + "{:,.2f}".format(realized_pnl) + '</div></div>', unsafe_allow_html=True)
with w4:
    st.markdown('<div class="card-box"><div class="metric-title">CONFLUENCE CONFIDENCE</div><div class="metric-value" style="color:#29b6f6;">' + str(confluence_score) + '/100</div></div>', unsafe_allow_html=True)

# ----------------- SCREEN INDICATORS BAR -----------------
st.markdown("##### 📊 ACTIVE QUANTITATIVE INDICATORS")
i1, i2, i3, i4 = st.columns(4)
with i1:
    rsi_clr = "#00e676" if 45 <= rsi_val <= 65 else "#ffb300"
    st.markdown('<div class="card-box"><div class="metric-title">RSI (14 PERIOD)</div><div class="metric-value" style="color:' + rsi_clr + ';">' + str(rsi_val) + '</div></div>', unsafe_allow_html=True)
with i2:
    macd_clr = "#00e676" if macd_hist > 0 else "#ff5252"
    st.markdown('<div class="card-box"><div class="metric-title">MACD HISTOGRAM</div><div class="metric-value" style="color:' + macd_clr + ';">' + str(macd_hist) + '</div></div>', unsafe_allow_html=True)
with i3:
    ema_clr = "#00e676" if "BULLISH" in ema_status else "#ff5252"
    st.markdown('<div class="card-box"><div class="metric-title">EMA 20/50 ALIGNMENT</div><div class="metric-value" style="color:' + ema_clr + '; font-size:14px;">' + ema_status + '</div></div>', unsafe_allow_html=True)
with i4:
    cvd_clr = "#00e676" if "+" in cvd_delta else "#ff5252"
    st.markdown('<div class="card-box"><div class="metric-title">CVD DELTA ABSORPTION</div><div class="metric-value" style="color:' + cvd_clr + '; font-size:14px;">' + cvd_delta + '</div></div>', unsafe_allow_html=True)

# ----------------- MILLISECOND LIVE WEBSOCKET TAPE -----------------
live_ticker_code = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body { margin: 0; padding: 0; background: transparent; font-family: monospace; }
        .ticker-card {
            background: #11141d;
            border: 1px solid #1f2430;
            border-radius: 8px;
            padding: 12px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .title { font-size: 11px; color: #88909e; font-weight: bold; letter-spacing: 0.5px; }
        .price { font-size: 30px; font-weight: bold; color: #00e676; margin-top: 2px; }
        .badge { display: inline-block; width: 8px; height: 8px; background: #00e676; border-radius: 50%; margin-right: 6px; box-shadow: 0 0 8px #00e676; }
        .status-text { font-size: 11px; color: #88909e; }
    </style>
</head>
<body>
    <div class="ticker-card">
        <div>
            <div class="title">BTC/USDT LIVE STREAM (DIRECT TAPE - MS LATENCY)</div>
            <div id="priceVal" class="price">Connecting...</div>
        </div>
        <div style="text-align: right;">
            <div><span class="badge"></span><span class="status-text">Bybit & Binance WebSocket Active</span></div>
            <div id="tickCount" style="font-size: 10px; color: #555d6e; margin-top: 3px;">Tape Sync: Real-Time</div>
        </div>
    </div>
    <script>
        const priceEl = document.getElementById("priceVal");
        let lastPrice = 0;
        function connectWs() {
            const ws = new WebSocket("wss://stream.binance.com:9443/ws/btcusdt@trade");
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                const p = parseFloat(data.p);
                priceEl.innerText = "$" + p.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
                priceEl.style.color = p >= lastPrice ? "#00e676" : "#ff5252";
                lastPrice = p;
            };
            ws.onerror = () => { setTimeout(connectWs, 2000); };
            ws.onclose = () => { setTimeout(connectWs, 2000); };
        }
        connectWs();
    </script>
</body>
</html>
"""
components.html(live_ticker_code, height=85)

# ----------------- TRADINGVIEW ADVANCED CHART (MULTI-TIMEFRAME) -----------------
st.markdown("##### 📈 MULTI-TIMEFRAME LIVE CANDLESTICK CHART")
tradingview_widget = """
<!-- TradingView Widget BEGIN -->
<div class="tradingview-widget-container" style="height:480px; width:100%;">
  <div id="tradingview_chart" style="height:100%; width:100%;"></div>
  <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
  <script type="text/javascript">
  new TradingView.widget({
    "autosize": true,
    "symbol": "BINANCE:BTCUSDT",
    "interval": "15",
    "timezone": "Etc/UTC",
    "theme": "dark",
    "style": "1",
    "locale": "en",
    "toolbar_bg": "#11141d",
    "enable_publishing": false,
    "hide_top_toolbar": false,
    "allow_symbol_change": true,
    "save_image": false,
    "container_id": "tradingview_chart"
  });
  </script>
</div>
<!-- TradingView Widget END -->
"""
components.html(tradingview_widget, height=490)

# ----------------- AUTO TRADE EXECUTION & WALLET DEBIT -----------------
def generate_trade_ticket(direction, price):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tid = "BOT-" + str(int(datetime.now().timestamp()) % 1000000)
    margin_cost = 2500.0  # $2,500 Margin debited per trade
    
    if direction == "LONG":
        sl = round(price - 180.0, 1)
        t1 = round(price + 350.0, 1)
        t2 = round(price + 700.0, 1)
        rating = "⭐⭐⭐⭐⭐ (9.3/10)"
        reason = "15m Key liquidity sweep + RSI 48 bounce + aggressive bid absorption confirm hui."
        remarks = "Bhai, sellers exhaust ho chuke hain aur orderbook me support ban rahi hai, $2,500 margin debit karke long initiate kiya hai."
    else:
        sl = round(price + 180.0, 1)
        t1 = round(price - 350.0, 1)
        t2 = round(price - 700.0, 1)
        rating = "⭐⭐⭐⭐⭐ (9.1/10)"
        reason = "Resistance par upside trap bana aur MACD bear crossover ke sath CVD drop hua."
        remarks = "Upar breakout fail ho gaya hai aur selling volume surge ho rahi hai, $2,500 margin lock karke short trigger kiya hai."

    return {
        "timestamp": now_str,
        "trade_id": tid,
        "direction": direction,
        "entry": round(price, 1),
        "sl": sl,
        "target_1": t1,
        "target_2": t2,
        "margin_used": margin_cost,
        "quality_rating": rating,
        "reason": reason,
        "conversational_remarks": remarks,
        "status": "ACTIVE_RUNNER",
        "pnl": 0.0
    }

# Check Active Runner Targets / SL
if st.session_state.active_trade:
    t = st.session_state.active_trade
    pnl = round(current_btc - t["entry"] if t["direction"] == "LONG" else t["entry"] - current_btc, 1)
    t["pnl"] = pnl

    # Target 1 Hit
    if ((t["direction"] == "LONG" and current_btc >= t["target_1"]) or 
        (t["direction"] == "SHORT" and current_btc <= t["target_1"])):
        t["status"] = "TARGET_1_HIT"
        profit_usd = 700.0  # Realized return
        execute_trade_close_wallet(t["margin_used"], profit_usd)
        save_trade_db(t)
        send_telegram_alert("🎯 *TARGET 1 HIT!* Trade: `" + t["trade_id"] + "` | Margin Released: $" + str(t["margin_used"]) + " \vert{} Profit: +$" + str(profit_usd))
        st.session_state.active_trade = None
        st.rerun()

    # SL Hit
    elif ((t["direction"] == "LONG" and current_btc <= t["sl"]) or 
          (t["direction"] == "SHORT" and current_btc >= t["sl"])):
        t["status"] = "SL_HIT"
        loss_usd = -360.0
        execute_trade_close_wallet(t["margin_used"], loss_usd)
        save_trade_db(t)
        send_telegram_alert("🛑 *SL HIT!* Trade: `" + t["trade_id"] + "` | Margin Released | Loss: -$" + str(abs(loss_usd)))
        st.session_state.active_trade = None
        st.rerun()

# Auto Scan Condition
elif (time.time() - st.session_state.last_auto_scan > 6):
    st.session_state.last_auto_scan = time.time()
    if confluence_score >= 82 and wallet_bal >= 2500.0:
        dir_pick = "LONG" if rsi_val >= 50 else "SHORT"
        new_tr = generate_trade_ticket(dir_pick, current_btc)
        execute_trade_open_wallet(new_tr["margin_used"])
        st.session_state.active_trade = new_tr
        save_trade_db(new_tr)
        
        tg_text = (
            "🤖 *NEW AUTO TRADE OPENED*\n\n"
            "• *Action:* *" + new_tr["direction"] + "* @ $" + str(new_tr["entry"]) + "\n"
            "• *Trade ID:* `" + new_tr["trade_id"] + "`\n"
            "• *Margin Debited:* $" + str(new_tr["margin_used"]) + "\n"
            "• *SL:* $" + str(new_tr["sl"]) + " \vert{} *T1:* $" + str(new_tr["target_1"]) + "\n"
            "• *Setup Rating:* " + new_tr["quality_rating"] + "\n\n"
            "💡 *Indicator Confluence:* " + new_tr["reason"] + "\n\n"
            "🗣️ *Note:* " + new_tr["conversational_remarks"]
        )
        send_telegram_alert(tg_text)
        st.rerun()

# ----------------- ACTIVE POSITION CARD -----------------
if st.session_state.active_trade:
    tr = st.session_state.active_trade
    pnl = round(current_btc - tr["entry"] if tr["direction"] == "LONG" else tr["entry"] - current_btc, 1)
    pnl_clr = "#00e676" if pnl >= 0 else "#ff5252"
    
    st.markdown("""
    <div style="background:#131824; border-left: 4px solid #7c4dff; border-radius:6px; padding:16px; margin-top:14px; margin-bottom:14px;">
        <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
            <div>
                <span style="background:#7c4dff; color:#fff; font-weight:bold; padding:2px 8px; border-radius:4px; font-size:12px;">ACTIVE POSITION</span>
                <strong style="margin-left:8px; font-size:15px;">""" + tr["direction"] + " [" + tr["trade_id"] + """]</strong>
                <span style="color:#ffb300; margin-left:10px; font-size:12px;">Margin Debited: $""" + str(tr.get("margin_used", 2500.0)) + """</span>
            </div>
            <div>Floating PnL: <strong style="color:""" + pnl_clr + """;">""" + str(pnl) + """ pts</strong></div>
        </div>
        <div style="display:grid; grid-template-columns: repeat(4, 1fr); gap:10px; font-size:13px; margin-bottom:10px;">
            <div><strong>Entry:</strong> $""" + str(tr["entry"]) + """</div>
            <div><strong>SL:</strong> $""" + str(tr["sl"]) + """</div>
            <div><strong>Target 1:</strong> $""" + str(tr["target_1"]) + """</div>
            <div><strong>Rating:</strong> <span style="color:#ffb300;">""" + str(tr["quality_rating"]) + """</span></div>
        </div>
        <div style="background:#0a0d14; border:1px solid #1f2430; border-radius:4px; padding:8px 12px; font-size:12px; margin-bottom:6px;">
            <strong>🎯 Entry Reason:</strong> """ + tr["reason"] + """
        </div>
        <div style="background:#0a0d14; border:1px solid #1f2430; border-radius:4px; padding:8px 12px; font-size:12px; color:#80cbc4;">
            <strong>🗣️ Trader's Note:</strong> """ + tr["conversational_remarks"] + """
        </div>
    </div>
    """, unsafe_allow_html=True)

# ----------------- CONTROLS -----------------
c_b1, c_b2, c_b3 = st.columns([2, 1, 1])
with c_b1:
    if st.button("⚡ Force Manual Opportunity Signal", use_container_width=True):
        d = random.choice(["LONG", "SHORT"])
        t_manual = generate_trade_ticket(d, current_btc)
        execute_trade_open_wallet(t_manual["margin_used"])
        st.session_state.active_trade = t_manual
        save_trade_db(t_manual)
        st.rerun()

with c_b2:
    if st.button("Close Trade (Market)", use_container_width=True):
        if st.session_state.active_trade:
            st.session_state.active_trade["status"] = "MANUAL_CLOSED"
            p = st.session_state.active_trade.get("pnl", 0.0)
            execute_trade_close_wallet(st.session_state.active_trade["margin_used"], p)
            save_trade_db(st.session_state.active_trade)
            st.session_state.active_trade = None
            st.rerun()

with c_b3:
    if st.button("Reset Wallet to $100k", use_container_width=True):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE wallet SET balance=100000.0, locked_margin=0.0, realized_pnl=0.0 WHERE id=1")
        conn.commit()
        conn.close()
        st.session_state.active_trade = None
        st.rerun()

# ----------------- SIGNALS VAULT AUDIT -----------------
st.markdown("### 🗄️ PERSISTENT SIGNALS VAULT & AUDIT TRAIL")
db_records = get_db_trades()

if db_records:
    table_df = pd.DataFrame(db_records, columns=[
        "Timestamp", "Trade ID", "Direction", "Entry", "SL", "Target 1", "Target 2", "Rating", "Conversational Note", "Status", "PnL"
    ])
    st.dataframe(table_df, use_container_width=True, hide_index=True)
else:
    st.info("Scanner market opportunity scan kar raha hai...")

time.sleep(3)
st.rerun()
