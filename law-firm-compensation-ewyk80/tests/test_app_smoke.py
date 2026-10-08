"""Smoke tests: every Streamlit page renders without exceptions on the demo data."""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from compensation.database import Database
from compensation.demo import load_demo
from conftest import ROOT

PAGES = ["dashboard", "setup", "imports", "mappings", "expenses", "results", "audit",
         "history"]


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory: pytest.TempPathFactory) -> str:
    path = tmp_path_factory.mktemp("app") / "app.db"
    load_demo(Database(path), 2025, "tester")
    return str(path)


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(page: str, demo_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMP_DB_PATH", demo_db)
    script = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
from ui.common import get_db, sidebar
get_db.clear()
st.session_state.setdefault("year", 2025)
st.session_state.setdefault("user_name", "tester")
sidebar()
exec(open({str(ROOT / 'pages' / (page + '.py'))!r}).read())
"""
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title, "page did not render a title"
