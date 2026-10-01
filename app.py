"""Streamlit entry point for BahiKhata AI.

TASK-001 scope: a minimal home page only. Capture & Review, Dashboard and
Ask Your Ledger pages are added in later tasks (files in pages/).
UI code stays thin: all logic lives in the bahikhata/ package.
"""

import streamlit as st

from bahikhata import config

st.set_page_config(page_title="BahiKhata AI", page_icon="🧾")

st.title("BahiKhata AI")
st.write(
    "Receipt-to-ledger assistant for Nepali small businesses: "
    "capture → validate → confirm → store → query."
)

st.subheader("Setup status")
if config.has_gemini_api_key():
    st.success("Gemini API key found in environment.")
else:
    st.warning("Gemini API key not set. Copy `.env.example` to `.env` and add your key.")

st.caption(f"Model: {config.MODEL_NAME or 'not chosen yet (TASK-002)'}")
st.caption(f"Extraction prompt: {config.EXTRACTION_PROMPT} · SQL prompt: {config.SQL_PROMPT}")

st.info("Pages for capture, dashboard and questions will appear here in later tasks.")
