import streamlit as st
import subprocess, sys, time, requests
import streamlit.components.v1 as components

st.set_page_config(layout="wide", page_title="BTC Terminal", initial_sidebar_state="collapsed")

@st.cache_resource
def start_server():
    p = subprocess.Popen([sys.executable, "terminal_final.py"])
    for _ in range(40):
        try:
            r = requests.get("http://127.0.0.1:8000/", timeout=1)
            if r.status_code == 200:
                break
        except:
            time.sleep(0.5)
    return p

start_server()
try:
    html = requests.get("http://127.0.0.1:8000/").text
    components.html(html, height=1050, scrolling=True)
except Exception:
    st.warning("Initializing Terminal Engine... Please refresh in 5 seconds.")
