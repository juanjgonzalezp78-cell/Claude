"""Law-firm partner compensation application (Streamlit entry point).

Run with:  streamlit run app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui.common import sidebar  # noqa: E402

st.set_page_config(page_title="Partner Compensation", page_icon="⚖️", layout="wide")

PAGES = {
    "Overview": [
        st.Page("pages/dashboard.py", title="Dashboard", icon="📊", default=True),
    ],
    "Setup & data": [
        st.Page("pages/setup.py", title="Firm Setup", icon="🏛️"),
        st.Page("pages/imports.py", title="Imports", icon="📥"),
        st.Page("pages/mappings.py", title="Supervisory Mappings", icon="🧭"),
        st.Page("pages/expenses.py", title="Partner Expenses", icon="🧾"),
    ],
    "Results": [
        st.Page("pages/results.py", title="Compensation Results", icon="💼"),
        st.Page("pages/audit.py", title="Audit & Exceptions", icon="🚩"),
        st.Page("pages/history.py", title="Finalize & History", icon="🗄️"),
    ],
}

if "year" not in st.session_state:
    from ui.common import get_db

    years = get_db().known_years()
    st.session_state["year"] = years[-1] if years else 2025

sidebar()
st.navigation(PAGES).run()
