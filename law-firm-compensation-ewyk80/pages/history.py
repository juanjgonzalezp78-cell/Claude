"""Finalize a year, reopen it (administrator), and browse historical snapshots."""

from __future__ import annotations

import json
import os

import pandas as pd
import streamlit as st

from compensation.database import ADMIN_PASSWORD_ENV
from compensation.service import finalize_year
from compensation.validation import finalization_blockers
from ui.common import attempt, get_db, require_user, results, user, year

db = get_db()
Y = year()
st.title(f"Finalize & History - {Y}")
status = db.year_status(Y)

st.subheader("Finalize")
if status.get("status") == "finalized":
    st.success(f"{Y} was finalized on {status['finalized_at']} by {status['finalized_by']}.")
else:
    inputs, res = results()
    blockers = finalization_blockers(res)
    if blockers:
        st.error("The year cannot be finalized yet:")
        for b in blockers:
            st.markdown(f"- {b}")
    else:
        st.success("No blocking exceptions and the compensation reconciles.")
    st.caption("Finalizing stores a snapshot of: imported file hashes, column mappings, policy "
               "assumptions, roster, matter originators, supervisory mappings, overrides, "
               "exclusions, calculated results and the audit log. The year then becomes read-only.")
    confirm = st.checkbox(f"I confirm the {Y} results are approved for finalization")
    if st.button("Finalize year", type="primary", disabled=bool(blockers) or not confirm) \
            and require_user():
        sid = attempt(lambda: finalize_year(db, Y, user()))
        if sid:
            st.success(f"{Y} finalized (snapshot #{sid}).")
            st.rerun()

st.subheader("Reopen a finalized year")
if status.get("status") != "finalized":
    st.caption("Only finalized years can be reopened.")
elif not os.environ.get(ADMIN_PASSWORD_ENV):
    st.warning(f"Reopening is disabled because the {ADMIN_PASSWORD_ENV} environment variable is "
               "not set. Restart the application with it set (see README).")
else:
    with st.form("reopen"):
        pw = st.text_input("Administrator password", type="password")
        reason = st.text_area("Reason for reopening (required)")
        if st.form_submit_button("Reopen year") and require_user():
            attempt(lambda: db.reopen_year(Y, user(), pw, reason), f"{Y} reopened.")
            st.rerun()

st.subheader("Historical snapshots")
snaps = db.snapshots()
if snaps.empty:
    st.info("No snapshots yet.")
else:
    st.dataframe(snaps, hide_index=True, width="stretch")
    sid = st.selectbox("Snapshot", snaps["snapshot_id"].tolist(),
                       format_func=lambda s: f"#{s} - {snaps.set_index('snapshot_id').loc[s, 'year']}")
    payload = db.load_snapshot(int(sid))
    if payload:
        st.download_button("Download snapshot (JSON)", json.dumps(payload, indent=2, default=str),
                           file_name=f"compensation_snapshot_{payload['year']}_{sid}.json")
        st.markdown("**Partner results in this snapshot**")
        ps = pd.DataFrame(payload["results"]["partner_summary"])
        keep = [c for c in ["Partner", "Active", "Total EWYK credit", "EWYK performance share %",
                            "Equal compensation", "EWYK compensation",
                            "Total compensation"] if c in ps]
        st.dataframe(ps[keep], hide_index=True, width="stretch")

    finals = snaps[snaps["kind"] == "finalization"]
    if len(finals) > 0:
        st.markdown("**Total compensation by finalized year**")
        rows = []
        for s in finals["snapshot_id"]:
            p = db.load_snapshot(int(s))
            for r in p["results"]["partner_summary"]:
                rows.append({"Year": p["year"], "Partner": r["Partner"],
                             "Total compensation": float(r["Total compensation"])})
        hist = pd.DataFrame(rows).drop_duplicates(["Year", "Partner"], keep="first")
        st.dataframe(hist.pivot(index="Partner", columns="Year", values="Total compensation"),
                     width="stretch")
