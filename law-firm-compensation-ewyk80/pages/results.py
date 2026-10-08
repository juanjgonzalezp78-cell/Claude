"""Compensation results with partner drill-down and Excel export."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from compensation.exports import build_workbook
from compensation.models import WorkingBucket
from ui.common import fmt_money, get_db, money_config, results, show, user, year

db = get_db()
Y = year()
st.title(f"Compensation Results - {Y}")
inputs, res = results()
summary = show(res.partner_summary)
if summary.empty:
    st.info("No partners set up for this year.")
    st.stop()

c = st.columns(3)
c[0].metric("Total compensation", fmt_money(summary["Total compensation"].sum()))
c[1].metric("Equal", fmt_money(summary["Equal compensation"].sum()))
c[2].metric("EWYK", fmt_money(summary["EWYK compensation"].sum()))
if res.blocking_count:
    st.warning(f"{res.blocking_count} blocking exception(s) - results are PROVISIONAL until "
               "resolved (see Audit & Exceptions).")

st.caption("Click a column heading to sort. Equal = pool × equal % ÷ active partners; EWYK = pool "
           "× EWYK % × EWYK share.")
cols = [c for c in summary.columns if c != "Manual-allocation working credit"
        or summary[c].astype(float).abs().sum() > 0]
st.dataframe(summary[cols], hide_index=True, width="stretch",
             column_config=money_config(summary))

status = db.year_status(Y).get("status", "open")
xlsx = build_workbook(inputs, res, db.audit_log(Y),
                      status="FINALIZED" if status == "finalized" else "Draft - not finalized",
                      prepared_by=user())
st.download_button("⬇️ Download Excel report (12 sheets)", xlsx,
                   file_name=f"partner_compensation_{Y}.xlsx", type="primary",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

st.divider()
st.subheader("Partner drill-down")
partner = st.selectbox("Partner", summary["Partner"].tolist())
row = summary[summary["Partner"] == partner].iloc[0]
c = st.columns(4)
c[0].metric("Originating credit", fmt_money(row["Originating credit"]))
c[1].metric("Working credit", fmt_money(row["Total working credit"]))
c[2].metric("EWYK share", f"{row['EWYK performance share %']:.3f}%")
c[3].metric("Total compensation", fmt_money(row["Total compensation"]))

od = show(res.origination_detail)
wd = show(res.working_detail)
att = show(res.attribution_detail)
p_od = od[od["Partner"] == partner]
p_wd = wd[wd["Partner"] == partner].copy()
p_wd["Working share"] = p_wd["Working share"] * 100
p_wd = p_wd.rename(columns={"Working share": "Working share %"})
credit_rows = pd.concat([
    p_od[["Client", "Matter ID", "Collection", "Origination credit"]].rename(
        columns={"Origination credit": "Credit"}).assign(Type="Origination"),
    p_wd[["Client", "Matter ID", "Collection", "Working credit"]].rename(
        columns={"Working credit": "Credit"}).assign(Type="Working"),
])

tabs = st.tabs(["Clients", "Matters", "Collections", "Originating credits", "Own hours",
                "Associate hours", "Supervisory assignments", "Working-credit calculation"])
with tabs[0]:
    by_client = credit_rows.pivot_table(index="Client", columns="Type", values="Credit",
                                        aggfunc="sum", fill_value=0).reset_index()
    by_client["Total EWYK credit"] = by_client.drop(columns=["Client"]).sum(axis=1)
    by_client = by_client.sort_values("Total EWYK credit", ascending=False)
    st.dataframe(by_client, hide_index=True, width="stretch",
                 column_config={c: st.column_config.NumberColumn(format="$%,.2f")
                                for c in by_client.columns if c != "Client"})
with tabs[1]:
    by_matter = credit_rows.pivot_table(index=["Client", "Matter ID"], columns="Type",
                                        values="Credit", aggfunc="sum", fill_value=0).reset_index()
    by_matter["Total EWYK credit"] = by_matter.drop(columns=["Client", "Matter ID"]).sum(axis=1)
    st.dataframe(by_matter.sort_values("Total EWYK credit", ascending=False), hide_index=True,
                 width="stretch",
                 column_config={c: st.column_config.NumberColumn(format="$%,.2f")
                                for c in by_matter.columns if c not in ("Client", "Matter ID")})
with tabs[2]:
    by_coll = credit_rows.pivot_table(index=["Collection", "Matter ID"], columns="Type",
                                      values="Credit", aggfunc="sum", fill_value=0).reset_index()
    st.dataframe(by_coll, hide_index=True, width="stretch",
                 column_config={c: st.column_config.NumberColumn(format="$%,.2f")
                                for c in by_coll.columns if c not in ("Collection", "Matter ID")})
with tabs[3]:
    st.dataframe(p_od, hide_index=True, width="stretch", column_config=money_config(p_od))
with tabs[4]:
    own = att[(att["Credited partner"] == partner) & (att["Component"] == WorkingBucket.OWN)]
    st.metric("Own recorded hours / qualifying measure",
              f"{own['Recorded hours'].sum():,.2f} / {own['Credited measure'].sum():,.2f}")
    st.dataframe(own, hide_index=True, width="stretch")
with tabs[5]:
    assoc = att[(att["Credited partner"] == partner) & (att["Component"] != WorkingBucket.OWN)]
    st.dataframe(assoc.groupby(["Timekeeper", "Category", "Matter ID", "Attribution source"],
                               as_index=False)[["Hours credited to partner", "Credited measure"]].sum(),
                 hide_index=True, width="stretch")
    with st.expander("Entry detail"):
        st.dataframe(assoc, hide_index=True, width="stretch")
with tabs[6]:
    sup = inputs.supervision
    st.markdown("**Associate-matter mappings crediting this partner**")
    st.dataframe(sup[sup["partner"] == partner], hide_index=True, width="stretch")
    m = inputs.matters
    st.markdown("**Matters where this partner is compensation supervising partner**")
    st.dataframe(m[m["comp_supervising_partner"] == partner], hide_index=True,
                 width="stretch")
    st.markdown("**Timekeepers with this partner as default supervisor**")
    tk = inputs.timekeepers
    st.dataframe(tk[tk["default_supervisor"] == partner], hide_index=True, width="stretch")
with tabs[7]:
    st.caption("Partner working credit = collected fees × working % × (partner credited measure ÷ "
               "total credited measure for the invoice or matter).")
    st.dataframe(p_wd, hide_index=True, width="stretch", column_config=money_config(p_wd))
    fb = p_wd[p_wd["Method"] != "Invoice-level"]
    if not fb.empty:
        st.info(f"{fb['Collection'].nunique()} collection(s) used the matter-level fallback.")
