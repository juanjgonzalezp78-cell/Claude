"""Lockstep update: proposed next-year weights, policy checks and committee overrides."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pandas as pd
import streamlit as st

from compensation.lockstep import check_final_weights
from compensation.models import to_decimal
from ui.common import PCT, attempt, get_db, require_user, results, user, writable, year

db = get_db()
Y = year()
st.title(f"Lockstep Update - proposed weights for {Y + 1}")
inputs, res = results()
policy = res.policy
prop = res.lockstep

st.caption(f"Gap = EWYK share − current weight. Desired change = gap capped at ±"
           f"{policy.max_lockstep_change_pct} points (Managing Partner never below "
           f"{policy.managing_partner_floor_pct}%). The larger of total increases / total decreases "
           "is scaled down so changes net to zero; weights are rounded to 0.001% and total exactly "
           "100%.")
for msg in prop.messages:
    st.info(msg)
for p in prop.problems:
    st.error(p)
if prop.table.empty:
    st.stop()

cfg = {c: PCT for c in ["Current weight", "EWYK performance share", "Performance gap",
                        "Desired change", "Balancing adjustment", "Final proposed change",
                        "Proposed next-year weight"]}
st.subheader("Proposal")
st.dataframe(prop.table, hide_index=True, width="stretch", column_config=cfg)

st.subheader("Final weights (after committee overrides)")
final = res.final_lockstep
st.dataframe(final, hide_index=True, width="stretch",
             column_config={c: PCT for c in ["Current weight", "Proposed weight", "Final weight",
                                             "Final change"]})
problems = check_final_weights(
    [{"name": r["Partner"], "is_managing_partner": r["Managing Partner"],
      "current": Decimal(str(r["Current weight"])), "final": Decimal(str(r["Final weight"]))}
     for r in final.to_dict("records")], policy.max_lockstep_change_pct,
    policy.managing_partner_floor_pct)
total = sum(Decimal(str(v)) for v in final["Final weight"])
c = st.columns(3)
c[0].metric("Final weights total", f"{total:.3f}%", "OK" if total == 100 else "must be 100%",
            delta_color="normal" if total == 100 else "inverse")
c[1].metric("Max |change|", f"{final['Final change'].abs().max():.3f} pts",
            f"cap {policy.max_lockstep_change_pct}")
mp = final[final["Managing Partner"]]
c[2].metric("Managing Partner", f"{mp['Final weight'].iloc[0]:.3f}%" if not mp.empty else "-",
            f"floor {policy.managing_partner_floor_pct}%")
if problems:
    for p in problems:
        st.error(p)
else:
    st.success("All policy checks pass: total = 100%, no change exceeds the cap, Managing Partner "
               "at or above the floor.")

st.subheader("Committee override")
if writable():
    st.caption("Edit 'Final weight' for any partner. Overrides require a written reason; the user, "
               "date and time are recorded, and the set is saved only if all checks pass.")
    editable = final[["Partner", "Managing Partner", "Current weight", "Proposed weight",
                      "Final weight"]].copy()
    edited = st.data_editor(
        editable, hide_index=True, width="stretch", key="ls_override",
        disabled=["Partner", "Managing Partner", "Current weight", "Proposed weight"],
        column_config={"Final weight": st.column_config.NumberColumn(
            "Final weight %", format="%.3f", step=0.001, min_value=0.0, max_value=100.0),
            "Current weight": PCT, "Proposed weight": PCT})
    rows = [{"name": r["Partner"], "is_managing_partner": r["Managing Partner"],
             "current": to_decimal(r["Current weight"]), "final": to_decimal(r["Final weight"])}
            for r in edited.to_dict("records")]
    new_problems = check_final_weights(rows, policy.max_lockstep_change_pct,
                                       policy.managing_partner_floor_pct)
    new_total = sum((r["final"] for r in rows), Decimal(0))
    st.markdown(
        f"- {'✅' if new_total == 100 else '❌'} Final weights total 100% (now {new_total:.3f}%)\n"
        f"- {'✅' if not any('cap' in p for p in new_problems) else '❌'} No change exceeds "
        f"{policy.max_lockstep_change_pct} points\n"
        f"- {'✅' if not any('floor' in p for p in new_problems) else '❌'} Managing Partner at or "
        f"above {policy.managing_partner_floor_pct}%")
    for p in new_problems:
        st.error(p)
    reason = st.text_area("Written reason for the override (required)")
    confirm = st.checkbox("I confirm the committee approved these final weights")
    c1, c2 = st.columns(2)
    if c1.button("Save overrides", type="primary") and require_user():
        if new_problems:
            st.error("Not saved - resolve the policy check failures above.")
        elif not reason.strip():
            st.error("A written reason is required.")
        elif not confirm:
            st.error("Tick the confirmation box.")
        else:
            now = datetime.now().isoformat(timespec="seconds")
            out = [{"partner": r["Partner"], "override_weight": r["Final weight"], "reason": reason,
                    "entered_by": user(), "entered_at": now}
                   for r in edited.to_dict("records")
                   if to_decimal(r["Final weight"]) != to_decimal(r["Proposed weight"])]
            attempt(lambda: db.save_table("lockstep_overrides", Y, pd.DataFrame(
                out, columns=["partner", "override_weight", "reason", "entered_by", "entered_at"]),
                user(), "Lockstep committee overrides saved"),
                f"{len(out)} override(s) saved.")
            st.rerun()
    if c2.button("Clear all overrides (revert to proposal)") and require_user():
        attempt(lambda: db.save_table("lockstep_overrides", Y, pd.DataFrame(), user(),
                                      "Lockstep overrides cleared"), "Overrides cleared.")
        st.rerun()
