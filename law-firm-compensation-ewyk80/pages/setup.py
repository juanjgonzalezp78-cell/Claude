"""Firm Setup: policy, partner roster, timekeepers, matters/originators, manual shares."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from compensation.expenses import NegativeNet
from compensation.models import FeeSplit
from compensation.models import Policy, TimekeeperCategory, WorkingMethod, to_decimal
from compensation.service import (assign_responsible_as_originator, rename_partner,
                                  start_year_from_prior)
from compensation.validation import validate_policy, validate_roster, validate_share_groups
from ui.common import attempt, get_db, partner_names, require_user, user, writable, year

db = get_db()
Y = year()
st.title(f"Firm Setup - {Y}")
can_edit = writable()

tabs = st.tabs(["Policy", "Partner roster", "Timekeepers", "Matters & originators",
                "Manual working shares", "Start from prior year"])

# ------------------------------------------------------------------ Policy
with tabs[0]:
    p = db.load_policy(Y)
    st.caption("Every assumption used by the calculation is here and is saved with the annual "
               "snapshot. Changes are recorded in the audit log.")
    with st.form("policy"):
        c1, c2, c3 = st.columns(3)
        pool = c1.number_input("Total distributable partner-compensation pool ($)", min_value=0.0,
                               value=float(p.distributable_pool), step=10000.0, format="%.2f")
        start = c2.date_input("Compensation period start", value=p.period_start)
        end = c3.date_input("Compensation period end", value=p.period_end)
        st.markdown("**Compensation pools (must total 100%)**")
        c1, c2 = st.columns(2)
        eq = c1.number_input("Equal-share %", 0.0, 100.0, float(p.equal_pct), 0.5)
        ew = c2.number_input("EWYK %", 0.0, 100.0, float(p.ewyk_pct), 0.5)
        st.markdown("**EWYK credit split (must total 100%)**")
        c1, c2 = st.columns(2)
        og = c1.number_input("Originating-partner portion %", 0.0, 100.0, float(p.origination_pct), 1.0)
        wk = c2.number_input("Working-partner portion %", 0.0, 100.0, float(p.working_pct), 1.0)
        st.markdown("**Working credit**")
        c1, c2 = st.columns(2)
        method = c1.selectbox("Working-share methodology", WorkingMethod.ALL,
                              index=WorkingMethod.ALL.index(p.working_method),
                              help="Billed hours is the recommended default.")
        orig_ok = c2.radio("Originator may also receive working credit", ["Yes", "No"],
                           index=0 if p.originator_working_eligible else 1, horizontal=True)
        c1, c2, c3 = st.columns(3)
        nonbill = c1.radio("Include nonbillable hours", ["No", "Yes"],
                           index=1 if p.include_nonbillable else 0, horizontal=True)
        wo = c2.radio("Include written-off hours", ["No", "Yes"],
                      index=1 if p.include_written_off else 0, horizontal=True)
        staff = c3.radio("Include paralegal / other staff hours", ["No", "Yes"],
                         index=1 if p.include_staff_hours else 0, horizontal=True)
        fee_split = st.selectbox(
            "Fee portion of payment allocations (when the export has no fee/expense split)",
            FeeSplit.ALL, index=FeeSplit.ALL.index(p.fee_split_method)
            if p.fee_split_method in FeeSplit.ALL else 0,
            help="TimeSolv applies payments to tax, expenses and interest before fees unless the "
                 "firm changed its line-item allocation order. Confirm the firm's TimeSolv setting.")
        st.markdown("**Partner expenses**")
        c1, c2 = st.columns(2)
        prorate = c1.radio("Prorate equal expense splits by time as partner", ["Yes", "No"],
                           index=0 if p.prorate_equal_expenses else 1, horizontal=True,
                           help="Uses the start/end dates on the partner roster.")
        negative = c2.selectbox("If expenses exceed a partner's compensation",
                                NegativeNet.ALL, index=NegativeNet.ALL.index(p.negative_net_treatment)
                                if p.negative_net_treatment in NegativeNet.ALL else 0)
        submitted = st.form_submit_button("Save policy", type="primary", disabled=not can_edit)
    new = Policy(year=Y, distributable_pool=to_decimal(f"{pool:.2f}"), equal_pct=to_decimal(eq),
                 ewyk_pct=to_decimal(ew), origination_pct=to_decimal(og),
                 working_pct=to_decimal(wk),
                 originator_working_eligible=orig_ok == "Yes", include_nonbillable=nonbill == "Yes",
                 include_written_off=wo == "Yes", include_staff_hours=staff == "Yes",
                 working_method=method, period_start=start, period_end=end,
                 prorate_equal_expenses=prorate == "Yes", negative_net_treatment=negative,
                 fee_split_method=fee_split)
    problems = validate_policy(new)
    for prob in problems:
        st.error(prob)
    if submitted and require_user():
        if problems:
            st.error("Policy not saved - fix the problems above.")
        else:
            attempt(lambda: db.save_policy(new, user()), "Policy saved.")

# ------------------------------------------------------------------ Partner roster
with tabs[1]:
    st.caption("Active partners share the equal pool and the EWYK pool. Inactive partners receive "
               "no compensation. The Managing Partner flag is informational only in this version.")
    roster = db.load_table("partners", Y)
    if roster.empty:
        roster = pd.DataFrame(
            [{"name": "Managing Partner", "is_managing_partner": True, "active": True, "notes": ""}]
            + [{"name": f"Partner {i}", "is_managing_partner": False, "active": True, "notes": ""}
               for i in range(1, 11)])
        st.info("No roster saved yet - a default 11-partner template is shown. Edit and save, or "
                "import a roster on the Imports page.")
    edited = st.data_editor(
        roster, num_rows="dynamic", width="stretch", key="roster_editor",
        disabled=not can_edit,
        column_config={
            "name": st.column_config.TextColumn("Partner name", required=True),
            "is_managing_partner": st.column_config.CheckboxColumn("Managing Partner"),
            "active": st.column_config.CheckboxColumn("Active"),
            "notes": st.column_config.TextColumn("Notes"),
            "start_date": st.column_config.TextColumn(
                "Partner from (YYYY-MM-DD)", help="Blank = whole period. Used to prorate equal "
                "expense splits."),
            "end_date": st.column_config.TextColumn("Partner until (YYYY-MM-DD)",
                                                    help="Blank = whole period."),
        })
    st.metric("Active partners", int(edited["active"].fillna(False).astype(bool).sum()))
    problems = validate_roster(edited, db.load_policy(Y))
    for prob in problems:
        st.warning(prob)
    if st.button("Save roster", type="primary", disabled=not can_edit) and require_user():
        attempt(lambda: db.save_table("partners", Y, edited, user(), "Partner roster saved"),
                "Roster saved." + (" Note: roster still has validation problems." if problems else ""))
        st.rerun()
    with st.expander("Rename a partner everywhere"):
        st.caption("Updates the roster, originators, mappings, overrides and timekeeper links. "
                   "Imported TimeSolv data is never rewritten.")
        names = partner_names()
        c1, c2 = st.columns(2)
        old = c1.selectbox("Partner", names, key="rename_old") if names else None
        new_name = c2.text_input("New name", key="rename_new")
        if st.button("Rename", disabled=not (can_edit and old and new_name)) and require_user():
            n = attempt(lambda: rename_partner(db, Y, old, new_name, user()))
            if n is not None:
                st.success(f"Renamed ({n} references updated).")
                st.rerun()

# ------------------------------------------------------------------ Timekeepers
with tabs[2]:
    st.caption("Category drives attribution: Partner hours credit the linked partner; associate "
               "(and, if enabled, paralegal/staff) hours are attributed to a supervising partner. "
               "The default supervisor is hierarchy level 5.")
    tks = db.load_table("timekeepers", Y)
    names = [""] + partner_names()
    tk_edit = st.data_editor(
        tks, num_rows="dynamic", width="stretch", key="tk_editor", disabled=not can_edit,
        column_config={
            "timekeeper_id": "Timekeeper ID", "name": st.column_config.TextColumn("Name", required=True),
            "role": "Role / title",
            "category": st.column_config.SelectboxColumn("Category", options=TimekeeperCategory.ALL,
                                                         required=True),
            "linked_partner": st.column_config.SelectboxColumn(
                "Linked partner (partners only)", options=names),
            "default_supervisor": st.column_config.SelectboxColumn(
                "Default supervising partner", options=names),
            "active": st.column_config.CheckboxColumn("Active"),
        })
    if st.button("Save timekeepers", type="primary", disabled=not can_edit) and require_user():
        attempt(lambda: db.save_table("timekeepers", Y, tk_edit, user(), "Timekeepers saved"),
                "Timekeepers saved.")

# ------------------------------------------------------------------ Matters & originators
with tabs[3]:
    st.markdown("**Matters**")
    st.caption("The compensation supervising partner (level 3) is separate from TimeSolv's "
               "Responsible Professional (level 4).")
    matters = db.load_table("matters", Y)
    m_edit = st.data_editor(
        matters, num_rows="dynamic", width="stretch", key="matter_editor",
        disabled=not can_edit,
        column_config={
            "matter_id": st.column_config.TextColumn("Matter ID", required=True),
            "client": "Client", "matter_name": "Matter name",
            "responsible_professional": "Responsible Professional (TimeSolv)",
            "comp_supervising_partner": st.column_config.SelectboxColumn(
                "Compensation supervising partner", options=names),
            "status": "Status", "open_date": "Open date", "close_date": "Close date",
            "custom_fields": "Custom fields (JSON)",
        })
    if st.button("Save matters", disabled=not can_edit) and require_user():
        attempt(lambda: db.save_table("matters", Y, m_edit, user(), "Matters saved"), "Matters saved.")

    st.markdown("**Matter originators** - one or more partners per matter; shares must total 100%.")
    st.caption("The Responsible Professional is NOT assumed to be the originator.")
    origs = db.load_table("originators", Y)
    o_edit = st.data_editor(
        origs, num_rows="dynamic", width="stretch", key="orig_editor",
        disabled=not can_edit,
        column_config={
            "matter_id": st.column_config.SelectboxColumn(
                "Matter ID", options=sorted(set(matters["matter_id"])), required=True),
            "partner": st.column_config.SelectboxColumn("Originating partner",
                                                        options=partner_names(), required=True),
            "share_pct": st.column_config.NumberColumn("Originator share %", min_value=0.0,
                                                       max_value=100.0, format="%.2f"),
            "notes": "Notes",
        })
    o_problems = validate_share_groups(o_edit, ["matter_id"], "share_pct", "Matter")
    missing = sorted(set(matters["matter_id"]) - set(o_edit["matter_id"]))
    for prob in o_problems:
        st.warning(prob)
    if missing:
        st.warning("Matters without an originator: " + ", ".join(missing))
    c1, c2 = st.columns(2)
    if c1.button("Save originators", type="primary", disabled=not can_edit) and require_user():
        attempt(lambda: db.save_table("originators", Y, o_edit, user(), "Originators saved"),
                "Originators saved.")
    if c2.button("Use Responsible Professional as originator for matters with none",
                 disabled=not can_edit, help="Explicit action, recorded in the audit log. Only "
                 "applies where the Responsible Professional is a partner.") and require_user():
        added = attempt(lambda: assign_responsible_as_originator(db, Y, user()))
        if added is not None:
            st.success(f"{len(added)} originator(s) assigned: " + ", ".join(added) if added
                       else "No eligible matters.")

# ------------------------------------------------------------------ Manual shares
with tabs[4]:
    policy = db.load_policy(Y)
    if policy.working_method != WorkingMethod.MANUAL:
        st.info("Manual working percentages are only used when the working-share methodology is "
                "'Manual percentages' (currently: " + policy.working_method + ").")
    ms = db.load_table("manual_shares", Y)
    ms_edit = st.data_editor(
        ms, num_rows="dynamic", width="stretch", key="manual_editor",
        disabled=not can_edit,
        column_config={
            "matter_id": st.column_config.SelectboxColumn(
                "Matter ID", options=sorted(set(matters["matter_id"])), required=True),
            "partner": st.column_config.SelectboxColumn("Partner", options=partner_names()),
            "share_pct": st.column_config.NumberColumn("Working share %", format="%.2f"),
            "notes": "Notes",
        })
    for prob in validate_share_groups(ms_edit, ["matter_id"], "share_pct", "Matter"):
        st.warning(prob)
    if st.button("Save manual shares", disabled=not can_edit) and require_user():
        attempt(lambda: db.save_table("manual_shares", Y, ms_edit, user(), "Manual shares saved"),
                "Manual working shares saved.")

# ------------------------------------------------------------------ Prior year
with tabs[5]:
    st.caption("Copies policy, roster, timekeepers, matters, originators, supervisory mappings and "
               "manual shares from a prior year.")
    years = [y for y in db.known_years() if y != Y]
    if not years:
        st.info("No other years exist yet.")
    else:
        src = st.selectbox("Copy from year", years, index=len(years) - 1)
        sure = st.checkbox(f"Replace the setup tables of {Y}")
        if st.button("Copy setup", disabled=not (can_edit and sure)) and require_user():
            notes = attempt(lambda: start_year_from_prior(db, src, Y, user()),
                            f"Setup copied from {src}.")
            for n in notes or []:
                st.caption(n)
st.caption(f"Today: {date.today():%Y-%m-%d}")
