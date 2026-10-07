"""Supervisory mappings: resolve unassigned hours, associate-matter splits, overrides, exclusions."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from compensation.models import AttributionSource
from compensation.validation import validate_share_groups, validate_supervision
from ui.common import (attempt, get_db, partner_names, require_user, results, show, user,
                       writable, year)

db = get_db()
Y = year()
st.title(f"Supervisory Mappings - {Y}")
st.caption("Attribution priority for non-partner hours: 1) time-entry override → 2) "
           "associate-matter mapping → 3) matter compensation supervising partner → 4) matter "
           "Responsible Professional (if a partner) → 5) timekeeper default supervisor → 6) "
           "UNASSIGNED (blocks finalization until resolved or expressly excluded).")
can_edit = writable()
inputs, res = results()
partners = partner_names()
att = res.attribution_detail

tabs = st.tabs(["Unassigned hours", "Associate-matter mappings", "Time-entry overrides",
                "Exclusions", "Attribution explorer"])

with tabs[0]:
    un = att[(att["Attribution source"] == AttributionSource.UNASSIGNED)
             & (att["Qualifying measure"] > 0) & (att["Excluded"] == "")]
    if un.empty:
        st.success("No unassigned hours. 🎉")
    else:
        groups = (un.groupby(["Timekeeper", "Matter ID"], as_index=False)
                  .agg(Entries=("Entry ID", "count"), Hours=("Recorded hours", "sum"),
                       Reason=("Attribution note", "first")))
        st.dataframe(groups, hide_index=True, width="stretch")
        labels = [f"{r['Timekeeper']} | {r['Matter ID']}" for _, r in groups.iterrows()]
        pick = st.selectbox("Resolve group", range(len(labels)), format_func=lambda i: labels[i])
        g = groups.iloc[pick]
        mode = st.radio("Resolution", ["Assign to supervising partner(s)", "Expressly exclude these "
                        "hours"], horizontal=True)
        if mode.startswith("Assign"):
            st.caption("Creates an associate-matter mapping (level 2) for this timekeeper and "
                       "matter. Add a second partner to split supervision.")
            c1, c2, c3, c4 = st.columns(4)
            p1 = c1.selectbox("Partner", partners, key="res_p1")
            pct1 = c2.number_input("Allocation %", 0.0, 100.0, 100.0, 5.0, key="res_pct1")
            p2 = c3.selectbox("Second partner (optional)", [""] + partners, key="res_p2")
            pct2 = c4.number_input("Allocation %", 0.0, 100.0, 0.0, 5.0, key="res_pct2")
            note = st.text_input("Notes / basis for assignment", key="res_note")
            if st.button("Create mapping", type="primary", disabled=not can_edit) and require_user():
                total = pct1 + (pct2 if p2 else 0)
                if abs(total - 100) > 1e-9:
                    st.error(f"Allocations total {total}%; they must total 100%.")
                elif not note.strip():
                    st.error("Enter the basis for the assignment.")
                else:
                    sup = db.load_table("supervision", Y)
                    new = [{"timekeeper": g["Timekeeper"], "matter_id": g["Matter ID"],
                            "partner": p1, "start_date": None, "end_date": None,
                            "allocation_pct": pct1, "notes": note,
                            "source": f"Exception resolution by {user()}"}]
                    if p2:
                        new.append({**new[0], "partner": p2, "allocation_pct": pct2})
                    attempt(lambda: db.save_table("supervision", Y, pd.concat(
                        [sup, pd.DataFrame(new)], ignore_index=True), user(),
                        "Unassigned hours resolved by mapping"), "Mapping created.")
                    st.rerun()
        else:
            reason = st.text_input("Reason for exclusion (required)", key="excl_reason")
            if st.button("Exclude entries", disabled=not can_edit) and require_user():
                if not reason.strip():
                    st.error("A reason is required.")
                else:
                    ex = db.load_table("exclusions", Y)
                    ids = un[(un["Timekeeper"] == g["Timekeeper"])
                             & (un["Matter ID"] == g["Matter ID"])]["Entry ID"].unique()
                    now = datetime.now().isoformat(timespec="seconds")
                    rows = pd.DataFrame([{"entry_id": i, "reason": reason, "entered_by": user(),
                                          "entered_at": now} for i in ids])
                    attempt(lambda: db.save_table("exclusions", Y, pd.concat([ex, rows]), user(),
                                                  "Hours expressly excluded"),
                            f"{len(ids)} entries excluded.")
                    st.rerun()

with tabs[1]:
    st.caption("One row per (timekeeper, matter, partner, effective period). Split supervision: "
               "several rows with the same timekeeper/matter/dates whose percentages total 100%. "
               "Leave dates blank for open-ended assignments. Dates use YYYY-MM-DD.")
    sup = db.load_table("supervision", Y)
    tk_names = sorted(set(db.load_table("timekeepers", Y)["name"]) - set(partners))
    matter_ids = sorted(set(db.load_table("matters", Y)["matter_id"]))
    sup_edit = st.data_editor(
        sup, num_rows="dynamic", width="stretch", disabled=not can_edit, key="sup_editor",
        column_config={
            "timekeeper": st.column_config.SelectboxColumn("Timekeeper", options=tk_names,
                                                           required=True),
            "matter_id": st.column_config.SelectboxColumn("Matter ID", options=matter_ids,
                                                          required=True),
            "partner": st.column_config.SelectboxColumn("Credited partner", options=partners,
                                                        required=True),
            "start_date": st.column_config.TextColumn("Effective start"),
            "end_date": st.column_config.TextColumn("Effective end"),
            "allocation_pct": st.column_config.NumberColumn("Allocation %", min_value=0.0,
                                                            max_value=100.0, format="%.2f"),
            "notes": "Notes", "source": "Source of assignment",
        })
    probs = validate_supervision(sup_edit, partners)
    for p in probs:
        st.warning(p)
    if st.button("Save mappings", type="primary", disabled=not can_edit) and require_user():
        if any("Row" in p and "date" in p for p in probs):
            st.error("Fix the date problems before saving.")
        else:
            attempt(lambda: db.save_table("supervision", Y, sup_edit, user(),
                                          "Associate-matter mappings saved"), "Mappings saved.")

with tabs[2]:
    st.caption("Highest priority. Use the Entry ID from the Attribution explorer. Several rows for "
               "the same entry split it; percentages must total 100%. A reason is required.")
    ov = db.load_table("entry_overrides", Y)
    ov_edit = st.data_editor(
        ov.drop(columns=["entered_by", "entered_at"]), num_rows="dynamic", width="stretch",
        disabled=not can_edit, key="ov_editor",
        column_config={
            "entry_id": st.column_config.TextColumn("Entry ID", required=True),
            "partner": st.column_config.SelectboxColumn("Credited partner", options=partners,
                                                        required=True),
            "allocation_pct": st.column_config.NumberColumn("Allocation %", format="%.2f"),
            "reason": st.column_config.TextColumn("Reason", required=True),
        })
    for p in validate_share_groups(ov_edit, ["entry_id"], "allocation_pct", "Entry"):
        st.warning(p)
    if st.button("Save overrides", disabled=not can_edit) and require_user():
        if (ov_edit["reason"].fillna("").astype(str).str.strip() == "").any():
            st.error("Every override needs a reason.")
        else:
            prev = ov.set_index(["entry_id", "partner"])[["entered_by", "entered_at"]] \
                if not ov.empty else None
            now = datetime.now().isoformat(timespec="seconds")
            out = ov_edit.copy()
            out["entered_by"] = [prev.loc[(e, p), "entered_by"] if prev is not None and (e, p) in
                                 prev.index else user() for e, p in zip(out["entry_id"], out["partner"])]
            out["entered_at"] = [prev.loc[(e, p), "entered_at"] if prev is not None and (e, p) in
                                 prev.index else now for e, p in zip(out["entry_id"], out["partner"])]
            attempt(lambda: db.save_table("entry_overrides", Y, out, user(),
                                          "Time-entry overrides saved"), "Overrides saved.")

with tabs[3]:
    st.caption("Expressly excluded entries earn no working credit for anyone and no longer block "
               "finalization.")
    ex = db.load_table("exclusions", Y)
    ex_edit = st.data_editor(ex, num_rows="dynamic", width="stretch",
                             disabled=not can_edit, key="ex_editor")
    if st.button("Save exclusions", disabled=not can_edit) and require_user():
        if (ex_edit["reason"].fillna("").astype(str).str.strip() == "").any():
            st.error("Every exclusion needs a reason.")
        else:
            ex_edit["entered_by"] = ex_edit["entered_by"].replace("", user()).fillna(user())
            attempt(lambda: db.save_table("exclusions", Y, ex_edit, user(), "Exclusions saved"),
                    "Exclusions saved.")

with tabs[4]:
    c1, c2, c3 = st.columns(3)
    f_tk = c1.multiselect("Timekeeper", sorted(att["Timekeeper"].unique()))
    f_m = c2.multiselect("Matter", sorted(att["Matter ID"].unique()))
    f_s = c3.multiselect("Attribution source", sorted(att["Attribution source"].unique()))
    view = att
    if f_tk:
        view = view[view["Timekeeper"].isin(f_tk)]
    if f_m:
        view = view[view["Matter ID"].isin(f_m)]
    if f_s:
        view = view[view["Attribution source"].isin(f_s)]
    st.dataframe(show(view), hide_index=True, width="stretch")
    st.caption(f"{len(view):,} rows")
