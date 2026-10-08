"""Audit & exceptions: every unresolved or unusual item, reconciliation and the audit log."""

from __future__ import annotations

import streamlit as st

from compensation.models import Severity
from ui.common import get_db, results, show, year

db = get_db()
Y = year()
st.title(f"Audit & Exceptions - {Y}")
inputs, res = results()
ex = res.exceptions

c = st.columns(3)
for col, sev in zip(c, (Severity.BLOCKING, Severity.WARNING, Severity.INFO)):
    col.metric(sev, int((ex["Severity"] == sev).sum()) if not ex.empty else 0)

tabs = st.tabs(["Exceptions", "Excluded collections", "Reconciliation", "Audit log"])
with tabs[0]:
    if ex.empty:
        st.success("No exceptions.")
    else:
        c1, c2 = st.columns(2)
        sev = c1.multiselect("Severity", [Severity.BLOCKING, Severity.WARNING, Severity.INFO],
                             default=[Severity.BLOCKING, Severity.WARNING])
        cats = c2.multiselect("Category", sorted(ex["Category"].unique()))
        view = ex[ex["Severity"].isin(sev)] if sev else ex
        if cats:
            view = view[view["Category"].isin(cats)]

        def colour(row):  # noqa: ANN001, ANN202
            shade = {"Blocking": "background-color:#FFC7CE", "Warning": "background-color:#FFEB9C"}
            return [shade.get(row["Severity"], "")] * len(row)

        st.dataframe(view.style.apply(colour, axis=1), hide_index=True, width="stretch",
                     column_config={"Amount": st.column_config.NumberColumn(format="$%,.2f")})
        st.caption("Blocking items prevent finalization. Use the 'How to resolve' column; most are "
                   "fixed on Firm Setup or Supervisory Mappings.")
with tabs[1]:
    cs = show(res.collection_status)
    if cs.empty:
        st.info("No collections.")
    else:
        st.dataframe(cs[~cs["Included"]], hide_index=True, width="stretch")
with tabs[2]:
    rc = show(res.reconciliation)
    st.dataframe(rc, hide_index=True, width="stretch")
    if res.reconciled:
        st.success("All reconciliation checks pass.")
    else:
        st.error("Reconciliation differences exist.")
with tabs[3]:
    log = db.audit_log(Y)
    st.dataframe(log, hide_index=True, width="stretch")
