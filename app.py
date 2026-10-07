from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent

st.set_page_config(page_title="BahiKhata AI", page_icon=str(ROOT / "assets/icon.svg"), layout="wide")
st.logo(str(ROOT / "assets/logo.svg"), size="large")

st.html("""
<style>
/* Full-width canvas with comfortable gutters instead of the narrow blog column. */
[data-testid="stMainBlockContainer"] {
    max-width: 1440px;
    padding: 5.25rem 2.5rem 3rem;
}
@media (max-width: 640px) {
    [data-testid="stMainBlockContainer"] { padding: 4.5rem 1rem 2rem; }
}

/* Top bar: white, with a hairline under it, full-bleed; its contents line up with the 1440px canvas. */
[data-testid="stHeader"] {
    background: #FFFFFF;
    border-bottom: 1px solid #E3E7EC;
    padding-inline: max(0px, calc((100% - 1440px) / 2));
}

/* Page titles and their subtitle caption. */
h1 { letter-spacing: -0.02em; margin-bottom: 0 !important; }
[data-testid="stMainBlockContainer"] h3 { letter-spacing: -0.01em; }

/* Bordered containers keyed "card_*" read as white cards. */
[class*="st-key-card_"] {
    background: #FFFFFF;
    box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04), 0 1px 3px rgba(16, 24, 40, 0.06);
}

/* KPI tiles. */
[data-testid="stMetric"] {
    background: #FFFFFF;
    border: 1px solid #E3E7EC;
    border-radius: 0.75rem;
    padding: 1rem 1.25rem;
    box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
}
[data-testid="stMetricLabel"] p {
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-size: 0.75rem;
    font-weight: 600;
    color: #5B6672;
}

/* Chat input on Ask Your Ledger: same width and gutters as the canvas above it. */
[data-testid="stBottomBlockContainer"] {
    max-width: 1440px;
    padding-left: 2.5rem;
    padding-right: 2.5rem;
}
@media (max-width: 640px) {
    [data-testid="stBottomBlockContainer"] { padding-left: 1rem; padding-right: 1rem; }
}

/* Capture & Review: the receipt image column stays in view while the review form scrolls. */
@media (min-width: 641px) {
    [data-testid="stColumn"]:has(.st-key-card_image) {
        position: sticky;
        top: 4.5rem;
        align-self: flex-start;
        max-height: calc(100vh - 5.5rem);
        overflow-y: auto;
    }
}

/* Chat bubbles on Ask Your Ledger. */
[data-testid="stChatMessage"] {
    background: #FFFFFF;
    border: 1px solid #E3E7EC;
    border-radius: 0.75rem;
    padding: 0.9rem 1.1rem;
}

/* Dashboard hero banner. */
.bk-hero {
    background: linear-gradient(120deg, #0F766E 0%, #115E59 55%, #134E4A 100%);
    color: #FFFFFF;
    border-radius: 1rem;
    padding: 2.25rem 2.5rem;
    margin-bottom: 0.5rem;
}
.bk-hero h1 { color: #FFFFFF; font-size: 2.25rem; margin: 0 0 0.4rem; }
.bk-hero p { color: #CCFBF1; font-size: 1.05rem; margin: 0; max-width: 46rem; }
.bk-hero .bk-tag {
    display: inline-block;
    background: rgba(255, 255, 255, 0.14);
    color: #FFFFFF;
    border-radius: 999px;
    padding: 0.2rem 0.75rem;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    margin-bottom: 0.9rem;
}
</style>
""")

pages = [
    st.Page("pages/dashboard/page.py", title="Dashboard", icon=":material/insights:", url_path="dashboard",
            default=True),
    st.Page("pages/capture_and_review/page.py", title="Capture & Review", icon=":material/receipt_long:",
            url_path="capture"),
    st.Page("pages/ask_your_ledger/page.py", title="Ask Your Ledger", icon=":material/forum:", url_path="ask"),
]
st.navigation(pages, position="top").run()
