"""Dashboard: key figures, data-quality indicators and the demo loader."""

from __future__ import annotations

import streamlit as st

from compensation.demo import DEMO_YEAR, load_demo
from compensation.models import Severity
from ui.common import (attempt, fmt_money, get_db, money_config, require_user, results, show,
                       user, year)

st.title("Partner Compensation Dashboard")
inputs, res = results()
m = res.metrics
policy = res.policy

with st.expander("Load Demo Data", expanded=inputs.partners.empty):
    st.write(f"Loads an 11-partner sample firm for **{DEMO_YEAR}** through the real import "
             "pipeline (TimeSolv-style CSVs in `sample_data/`). **All existing data for "
             f"{DEMO_YEAR} is replaced.** The demo intentionally contains unresolved exceptions.")
    confirm = st.checkbox(f"I understand that {DEMO_YEAR} data will be replaced")
    if st.button("Load Demo Data", type="primary", disabled=not confirm):
        if require_user():
            msgs = attempt(lambda: load_demo(get_db(), DEMO_YEAR, user()), "Demo data loaded.")
            if msgs:
                st.session_state["year"] = DEMO_YEAR
                for msg in msgs:
                    st.caption(msg)
                st.rerun()

if inputs.partners.empty:
    st.info(f"No partner roster exists for {year()}. Load the demo data above or start in "
            "Firm Setup.")
    st.stop()

st.subheader("Pools")
c = st.columns(4)
c[0].metric("Distributable pool", fmt_money(m["distributable_pool"]))
c[1].metric(f"Equal pool ({policy.equal_pct}%)", fmt_money(m["equal_pool"]))
c[2].metric(f"EWYK pool ({policy.ewyk_pct}%)", fmt_money(m["ewyk_pool"]))
c[3].metric(f"Lockstep pool ({policy.lockstep_pct}%)", fmt_money(m["lockstep_pool"]))

st.subheader("Collections")
c = st.columns(4)
c[0].metric("Total collections", fmt_money(m["total_collections"]))
c[1].metric("Collected professional fees (credited)", fmt_money(m["total_fees_collected"]))
c[2].metric("Collections included / excluded",
            f"{m['collections_included']} / {m['collections_excluded']}")
c[3].metric("Matter-level fallback collections", m["fallback_collections"])

st.subheader("Data quality")
c = st.columns(5)
c[0].metric("Active partners", m["active_partners"])
c[1].metric("Unassigned hours", f"{float(m['unassigned_hours']):,.2f}")
c[2].metric("Collections missing originator", m["collections_missing_originator"])
c[3].metric("Matters with incomplete supervision", m["matters_incomplete_supervision"])
c[4].metric("Blocking exceptions", m["blocking_exceptions"])

if m["reconciled"]:
    st.success("✅ Compensation reconciles: equal + EWYK + lockstep payments equal the "
               f"distributable pool of {fmt_money(m['distributable_pool'])}.")
else:
    st.error("❌ Compensation does not reconcile. See Audit & Exceptions > Reconciliation.")
if float(m["unallocated_credit"]):
    st.warning(f"{fmt_money(m['unallocated_credit'])} of collected fees could not be credited "
               "to any partner (see exceptions).")

blocking = res.exceptions[res.exceptions["Severity"] == Severity.BLOCKING] \
    if not res.exceptions.empty else res.exceptions
if not blocking.empty:
    st.subheader("Blocking items (must be resolved before finalization)")
    st.dataframe(blocking[["Category", "Description", "How to resolve"]], hide_index=True,
                 width="stretch")

st.subheader("Compensation by partner")
cols = ["Partner", "Active", "Total EWYK credit", "EWYK performance share %",
        "Equal compensation", "EWYK compensation", "Lockstep compensation", "Total compensation"]
df = show(res.partner_summary[cols])
st.dataframe(df, hide_index=True, width="stretch", column_config=money_config(df))
