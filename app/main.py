"""Streamlit UI. Run: streamlit run app/main.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from app import config
from app.agent import run_agent
from app.data import list_sample_files

st.set_page_config(page_title="HackAlem project", layout="wide")
st.title("HackAlem project")  # TODO(case): project name

with st.sidebar:
    st.caption(f"Model: {config.OPENAI_MODEL}")
    st.caption("API key: set" if config.OPENAI_API_KEY else "API key: MISSING (see .env.example)")
    st.caption(f"Sample files: {len(list_sample_files())}")

query = st.text_area("Input", placeholder="TODO(case): what does the user provide?")
if st.button("Run", type="primary") and query.strip():
    with st.spinner("Agent is working…"):
        answer, trace = run_agent(query)
    st.subheader("Result")
    st.markdown(answer)
    with st.expander(f"Agent steps ({len(trace)} tool calls)"):
        for step in trace:
            st.json(step)
