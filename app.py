import streamlit as st 
import subprocess, sys, time 
import streamlit.components.v1 as components 
st.set_page_config(layout="wide", page_title="BTC Perpetual Terminal", initial_sidebar_state="collapsed") 
@st.cache_resource 
def run_backend(): 
    p = subprocess.Popen([sys.executable, "terminal_final.py"]) 
    time.sleep(3) 
    return p 
run_backend() 
components.iframe("http://localhost:8000", height=950, scrolling=True) 
