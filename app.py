import streamlit as st
from datetime import datetime
import sqlite3
import os
import requests
import pandas as pd

st.set_page_config(layout="wide", page_title="BTC Terminal", initial_sidebar_state="collapsed")

# Custom Dark Theme
st.markdown("""
<style>
    .stApp { background-color: #0b0e14; color: #d1d4dc; }
    .stat-card { background: #131722; border: 1px solid #2a2e39; border-radius: 6px; padding: 10px; text-align: center; }
    .stat-title { font-size: 11px; color: #787b86; font-weight: bold; }
    .stat-val { font-size: 20px; font-weight: bold; margin-top: 4px; }
    #MainMenu, footer, header { visibility: hidden; }
</style>
""", unsafe_allow_html=True)

# Database
DB_FILE = "trades_vault.db"
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        time TEXT, id TEXT PRIMARY KEY, direction TEXT, entry REAL,
        sl REAL, t1 REAL, t2 REAL, status TEXT, pnl REAL
    )''')
    conn.commit()
    conn.close()

init_db()

def get_trades():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT time, id, direction, entry, sl, t1, t2, status FROM trades ORDER BY rowid DESC LIMIT 10")
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception:
        return []

def save_trade(t):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (t["time"], t["id"], t["direction"], t["entry"], t["sl"], t["t1"], t["t2"], t["status"], t["pnl"]))
        conn.commit()
        conn.close()
    except Exception:
        pass

# Telegram Alert
def send_telegram(msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=3)
        except Exception:
            pass

# Price Fetcher (Cloud-safe)
def get_btc_price():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=2)
        return float(r.json()["price"])
    except Exception:
        try:
            r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=2)
            return float(r.json()["data"]["amount"])
        except Exception:
            return 84500.0

if "active_trade" not in st.session_state:
    st.session_state.active_trade = None

# Top Metrics Row
c1, c2, c3, c4 = st.columns(4)
with c1:
    st.markdown('<div class="stat-card"><div class="stat-title">STRUCTURE SCORE</div><div class="stat-val" style="color:#00e676;">85/100</div></div>', unsafe_allow_html=True)
with c2:
    st.markdown('<div class="stat-card"><div class="stat-title">VOLUME / DELTA</div><div class="stat-val" style="color:#00e676;">85/100</div></div>', unsafe_allow_html=True)
with c3:
    st.markdown('<div class="stat-card"><div class="stat-title">MOMENTUM SQUEEZE</div><div class="stat-val" style="color:#ffb300;">45/100</div></div>', unsafe_allow_html=True)
with c4:
    st.markdown('<div class="stat-card"><div class="stat-title">LIQUIDITY PROXIMITY</div><div class="stat-val" style="color:#ab47bc;">40/100</div></div>', unsafe_allow_html=True)

st.write("")

price = get_btc_price()

# Price Header & Trigger Controls
col_p, col_b1, col_b2 = st.columns([3, 1, 1])
with col_p:
    st.markdown(f"### BTC/USDT LIVE: <span style='color:#ffb300;'>${price:,.2f}</span>", unsafe_allow_html=True)

with col_b1:
    if st.button("⚡ Force Signal Trigger", use_container_width=True):
        tid = f"MANUAL-{int(datetime.now().timestamp()) % 1000000}"
        st.session_state.active_trade = {
            "id": tid,
            "time": datetime.now().strftime("%H:%M:%S"),
            "direction": "LONG",
            "entry": round(price, 1),
            "sl": round(price - 180.0, 1),
            "t1": round(price + 350.0, 1),
            "t2": round(price + 700.0, 1),
            "status": "ACTIVE",
            "pnl": 0.0
        }
        save_trade(st.session_state.active_trade)
        send_telegram(f"🚨 *STAGE 2/3 EXECUTION TICKET*\n\nID: `{tid}`\nDirection: *LONG*\nEntry: *${price:,.1f}*\nSL: *${price - 180.0:,.1f}*\nT1: *${price + 350.0:,.1f}*")
        st.rerun()

with col_b2:
    if st.button("Reset Trade", use_container_width=True):
        st.session_state.active_trade = None
        st.rerun()

# Runner Status Box
if st.session_state.active_trade:
    tr = st.session_state.active_trade
    pnl = round(price - tr["entry"] if tr["direction"] == "LONG" else tr["entry"] - price, 1)
    st.success(f"ACTIVE RUNNER: {tr['direction']} [{tr['id']}] | Entry: ${tr['entry']} | SL: ${tr['sl']} \vert{} T1:${tr['t1']} | PnL: {pnl} pts | Status: {tr['status']}")
else:
    st.caption("NO ACTIVE TRADE IN RUNNER")

# Signals History
st.markdown("#### PERSISTENT SIGNALS VAULT (DATABASE AUDIT)")
trade_rows = get_trades()
if trade_rows:
    df = pd.DataFrame(trade_rows, columns=["Time", "Trade ID", "Direction", "Entry", "Invalidation", "Target 1", "Target 2", "Outcome Status"])
    st.dataframe(df, use_container_width=True, hide_index=True)
else:
    st.write("No trade history available yet.")
