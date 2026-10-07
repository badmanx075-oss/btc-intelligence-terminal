import streamlit as st
import subprocess, sys, time, requests
import streamlit.components.v1 as components

st.set_page_config(layout="wide", page_title="BTC Terminal", initial_sidebar_state="collapsed")

# Custom hide default streamlit headers
st.markdown("""
<style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    .block-container {padding: 0 !important; margin: 0 !important;}
</style>
""", unsafe_allow_html=True)

@st.cache_resource
def start_terminal_backend():
    p = subprocess.Popen([sys.executable, "terminal_final.py"])
    for _ in range(40):
        try:
            r = requests.get("http://127.0.0.1:8000/", timeout=1)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.5)
    return p

start_terminal_backend()

try:
    resp = requests.get("http://127.0.0.1:8000/", timeout=2)
    if resp.status_code == 200:
        components.html(resp.text, height=1100, scrolling=True)
    else:
        st.warning("⚡ Terminal engine booting up... Please wait 5 seconds and refresh.")
except Exception:
    st.warning("⚡ Terminal engine booting up... Please wait 5 seconds and refresh.")
