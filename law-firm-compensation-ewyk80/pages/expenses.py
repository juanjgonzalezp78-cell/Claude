"""Partner expenses: categories and allocation rules, expense entries, overrides, carry-forwards."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from compensation.expenses import ExpenseRule
from compensation.service import load_carryforwards
from compensation.validation import validate_share_groups
from ui.common import (MONEY, PCT, attempt, fmt_money, get_db, partner_names, require_user,
                       results, show, user, writable, year)

db = get_db()
Y = year()
st.title(f"Partner Expenses - {Y}")
st.caption("Expenses are deducted from each partner's gross compensation. Each category has an "
           "allocation rule; individual expenses can be overridden. Net = gross compensation − "
           "allocated expenses − prior-year carry-forward.")
can_edit = writable()
inputs, res = results()
policy = res.policy
partners = partner_names()
exp = res.expenses
cats = db.load_table("expense_categories", Y)
cat_names = sorted(c for c in cats["category"].tolist() if c)

tabs = st.tabs(["Summary", "Categories & rules", "Expenses", "Entry overrides",
                "Carry-forwards", "Allocation detail"])

# ------------------------------------------------------------------ Summary
with tabs[0]:
    c = st.columns(4)
    c[0].metric("Total expenses", fmt_money(exp.total_expenses))
    c[1].metric("Allocated to partners", fmt_money(exp.total_allocated))
    c[2].metric("Unallocated", fmt_money(exp.total_expenses - exp.total_allocated))
    c[3].metric("Carried forward to next year", fmt_money(res.metrics["total_carry_forward_out"]))
    st.caption(f"Negative net compensation: **{policy.negative_net_treatment}**. Equal splits "
               f"{'are' if policy.prorate_equal_expenses else 'are not'} prorated by time as "
               "partner. Change these on Firm Setup → Policy.")
    net_cols = ["Partner", "Active", "Total compensation", "Allocated expenses",
                "Prior-year carry-forward", "Net compensation", "Net payable",
                "Carry forward to next year", "Owed to the firm"]
    net = show(res.partner_summary[net_cols]).rename(
        columns={"Total compensation": "Gross compensation"})
    st.markdown("**Net compensation by partner**")
    st.dataframe(net, hide_index=True, width="stretch",
                 column_config={c: MONEY for c in net.columns if c not in ("Partner", "Active")})
    if not exp.by_partner.empty:
        st.markdown("**Expenses by partner and category**")
        st.dataframe(exp.by_partner, hide_index=True, width="stretch",
                     column_config={c: MONEY for c in exp.by_partner.columns if c != "Partner"})
    bad = res.exceptions[res.exceptions["Category"].isin(
        ["Expenses not allocated", "Expense charged to inactive partner",
         "Negative net compensation"])] if not res.exceptions.empty else res.exceptions
    if not bad.empty:
        st.markdown("**Expense exceptions**")
        st.dataframe(bad[["Severity", "Category", "Description", "How to resolve"]],
                     hide_index=True, width="stretch")

# ------------------------------------------------------------------ Categories
with tabs[1]:
    st.markdown("**Categories and their default rule**")
    st.caption("Direct / Fixed split use the partner splits below. 'By associate hours' needs the "
               "associate's name and splits the cost by the hours of that associate credited to "
               "each partner. Equal, EWYK and gross-compensation rules apply to active partners.")
    tk_names = sorted(n for n in db.load_table("timekeepers", Y)["name"].tolist()
                      if n and n not in partners)
    cat_edit = st.data_editor(
        cats, num_rows="dynamic", width="stretch", key="cat_editor", disabled=not can_edit,
        column_config={
            "category": st.column_config.TextColumn("Category", required=True),
            "rule": st.column_config.SelectboxColumn("Allocation rule", options=ExpenseRule.ALL,
                                                     required=True),
            "associate": st.column_config.SelectboxColumn("Associate (for 'By associate hours')",
                                                          options=[""] + tk_names),
            "notes": "Notes",
        })
    used = sorted({c for c in db.load_table("expenses", Y)["category"].tolist() if c}
                  - set(cat_edit["category"].dropna()))
    if used:
        st.warning("Categories used by expenses but not defined: " + ", ".join(used))
    c1, c2 = st.columns(2)
    if c1.button("Save categories", type="primary", disabled=not can_edit) and require_user():
        attempt(lambda: db.save_table("expense_categories", Y, cat_edit, user(),
                                      "Expense categories saved"), "Categories saved.")
        st.rerun()
    if used and c2.button("Add the undefined categories (rule still to be chosen)",
                          disabled=not can_edit) and require_user():
        new = pd.concat([cat_edit, pd.DataFrame([{"category": u, "rule": "", "associate": "",
                                                  "notes": "Added from expenses"} for u in used])])
        attempt(lambda: db.save_table("expense_categories", Y, new, user(),
                                      "Expense categories added from expenses"), "Added.")
        st.rerun()

    st.markdown("**Partner splits for Direct / Fixed-split categories** (must total 100% per "
                "category)")
    splits = db.load_table("category_splits", Y)
    split_edit = st.data_editor(
        splits, num_rows="dynamic", width="stretch", key="split_editor", disabled=not can_edit,
        column_config={
            "category": st.column_config.SelectboxColumn("Category", options=cat_names,
                                                         required=True),
            "partner": st.column_config.SelectboxColumn("Partner", options=partners, required=True),
            "share_pct": st.column_config.NumberColumn("Share %", min_value=0.0, max_value=100.0,
                                                       format="%.2f"),
            "notes": "Notes",
        })
    for p in validate_share_groups(split_edit, ["category"], "share_pct", "Category"):
        st.warning(p)
    if st.button("Save splits", disabled=not can_edit) and require_user():
        attempt(lambda: db.save_table("category_splits", Y, split_edit, user(),
                                      "Expense category splits saved"), "Splits saved.")

# ------------------------------------------------------------------ Expenses
with tabs[2]:
    st.caption("Enter expenses here, or import an accounting export on the Imports page "
               "(dataset 'Partner expenses'). Amounts are positive numbers.")
    ex = db.load_table("expenses", Y)
    st.metric("Expenses entered", f"{len(ex):,}  ·  {fmt_money(pd.to_numeric(ex['amount']).sum())}")
    ex_edit = st.data_editor(
        ex, num_rows="dynamic", width="stretch", key="exp_editor", disabled=not can_edit,
        column_config={
            "expense_id": st.column_config.TextColumn("Expense ID (blank = automatic)"),
            "expense_date": st.column_config.TextColumn("Date (YYYY-MM-DD)"),
            "category": st.column_config.SelectboxColumn("Category", options=cat_names,
                                                         required=True),
            "description": "Description",
            "amount": st.column_config.NumberColumn("Amount", format="$%,.2f"),
            "vendor": "Vendor", "reference": "Reference",
        })
    if st.button("Save expenses", type="primary", disabled=not can_edit) and require_user():
        out = ex_edit.copy()
        blank = out["expense_id"].fillna("").astype(str).str.strip() == ""
        out.loc[blank, "expense_id"] = [f"EXP-{Y}-M{i:04d}" for i in range(1, int(blank.sum()) + 1)]
        if out["expense_id"].duplicated().any():
            st.error("Expense IDs must be unique.")
        else:
            attempt(lambda: db.save_table("expenses", Y, out, user(), "Expenses saved"),
                    "Expenses saved.")

# ------------------------------------------------------------------ Overrides
with tabs[3]:
    st.caption("Overrides one expense's category rule with its own partner split. Rows for the "
               "same expense must total 100%. A reason is required.")
    ov = db.load_table("expense_overrides", Y)
    ids = db.load_table("expenses", Y)["expense_id"].tolist()
    ov_edit = st.data_editor(
        ov.drop(columns=["entered_by", "entered_at"]), num_rows="dynamic", width="stretch",
        key="exp_ov_editor", disabled=not can_edit,
        column_config={
            "expense_id": st.column_config.SelectboxColumn("Expense ID", options=ids,
                                                           required=True),
            "partner": st.column_config.SelectboxColumn("Partner", options=partners, required=True),
            "share_pct": st.column_config.NumberColumn("Share %", format="%.2f"),
            "reason": st.column_config.TextColumn("Reason", required=True),
        })
    for p in validate_share_groups(ov_edit, ["expense_id"], "share_pct", "Expense"):
        st.warning(p)
    if st.button("Save overrides", disabled=not can_edit) and require_user():
        if (ov_edit["reason"].fillna("").astype(str).str.strip() == "").any():
            st.error("Every override needs a reason.")
        else:
            now = datetime.now().isoformat(timespec="seconds")
            out = ov_edit.assign(entered_by=user(), entered_at=now)
            attempt(lambda: db.save_table("expense_overrides", Y, out, user(),
                                          "Expense overrides saved"), "Overrides saved.")

# ------------------------------------------------------------------ Carry-forwards
with tabs[4]:
    st.caption("Shortfalls carried in from the previous year are deducted from this year's "
               "compensation. Loading from the prior year uses its finalized snapshot when "
               "available. Manual edits are audited.")
    cf = db.load_table("carryforwards", Y)
    cf_edit = st.data_editor(
        cf, num_rows="dynamic", width="stretch", key="cf_editor", disabled=not can_edit,
        column_config={
            "partner": st.column_config.SelectboxColumn("Partner", options=partners, required=True),
            "amount": st.column_config.NumberColumn("Amount carried in", format="$%,.2f"),
            "source_year": "From year", "notes": "Notes",
        })
    c1, c2 = st.columns(2)
    if c1.button("Save carry-forwards", disabled=not can_edit) and require_user():
        attempt(lambda: db.save_table("carryforwards", Y, cf_edit, user(),
                                      "Carry-forwards edited"), "Saved.")
    if c2.button(f"Load carry-forwards from {Y - 1}", disabled=not can_edit) and require_user():
        notes = attempt(lambda: load_carryforwards(db, Y - 1, Y, user()))
        for n in notes or []:
            st.info(n)

# ------------------------------------------------------------------ Detail
with tabs[5]:
    det = show(exp.detail)
    if det.empty:
        st.info("No expenses entered.")
    else:
        f_p = st.multiselect("Partner", sorted(det["Partner"].unique()))
        f_c = st.multiselect("Category", sorted(det["Category"].unique()))
        view = det
        if f_p:
            view = view[view["Partner"].isin(f_p)]
        if f_c:
            view = view[view["Category"].isin(f_c)]
        st.dataframe(view, hide_index=True, width="stretch",
                     column_config={"Expense amount": MONEY, "Allocated amount": MONEY,
                                    "Share %": PCT})
        st.caption(f"{len(view):,} rows · {fmt_money(view['Allocated amount'].sum())} allocated")
