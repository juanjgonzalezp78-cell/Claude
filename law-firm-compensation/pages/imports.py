"""Imports: upload, preview, map, validate and save TimeSolv files."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from compensation.database import sha256
from compensation.imports import (DATASETS, excel_sheets, header_signature, import_dataframe,
                                  mapping_problems, normalize, read_upload, recall_or_suggest)
from ui.common import attempt, get_db, require_user, results, show, user, writable, year

db = get_db()
Y = year()
st.title(f"Imports - {Y}")
can_edit = writable()

tab_wizard, tab_history, tab_review = st.tabs(["Import wizard", "Import history",
                                               "Review collections"])

with tab_wizard:
    st.markdown("**Recommended order:** partner roster → professionals → matters → "
                "originating professionals → time → invoices → payments → expenses. Use TimeSolv's "
                "Import/Export → Export Excel entities (Matter, Matter Originating Professional, "
                "Time, Invoice, Payment & Allocation).")
    labels = {k: v.label for k, v in DATASETS.items()}
    dataset = st.selectbox("1. What are you importing?", list(labels), format_func=labels.get,
                           index=list(labels).index("collections"))
    spec = DATASETS[dataset]
    st.caption(spec.description)
    upload = st.file_uploader("2. Upload a CSV or XLSX export", type=["csv", "xlsx", "xls", "txt"],
                              key=f"upload_{dataset}")
    if upload is not None:
        data = upload.getvalue()
        c1, c2 = st.columns(2)
        sheet = None
        if upload.name.lower().endswith((".xlsx", ".xls")):
            try:
                sheet = c1.selectbox("Worksheet", excel_sheets(data))
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not open workbook: {exc}")
                st.stop()
        header_row = c2.number_input("Header row (1 = first row)", 1, 50, 1,
                                     help="TimeSolv reports sometimes have title rows above the "
                                          "column headings.") - 1
        try:
            raw = read_upload(upload.name, data, int(header_row), sheet)
        except ValueError as exc:
            st.error(str(exc))
            st.stop()
        st.markdown(f"**3. Preview** - {len(raw):,} rows, {len(raw.columns)} columns "
                    f"(SHA-256 `{sha256(data)[:16]}…`)")
        st.dataframe(raw.head(20), width="stretch")

        mapping, origin = recall_or_suggest(db, dataset, list(raw.columns))
        st.markdown("**4. Column mapping** - " + (
            "a mapping saved for these exact headings was recalled." if origin == "saved"
            else "suggested automatically; please confirm or correct each field."))
        options = [""] + list(raw.columns)
        sig = header_signature(list(raw.columns))
        new_map: dict[str, str] = {}
        cols = st.columns(3)
        for i, f in enumerate(spec.fields):
            default = mapping.get(f.key, "")
            new_map[f.key] = cols[i % 3].selectbox(
                f"{f.label}{' *' if f.required else ''}", options,
                index=options.index(default) if default in options else 0,
                key=f"map_{dataset}_{sig}_{f.key}", help=f.help or None)
        unmapped = [c for c in raw.columns if c not in new_map.values()]
        if unmapped:
            st.caption("Unmapped source columns (kept in the audit copy"
                       + (" and stored as matter custom fields" if dataset == "matters" else "")
                       + "): " + ", ".join(unmapped))
        problems = mapping_problems(dataset, new_map)
        for prob in problems:
            st.error(prob)

        if not problems:
            st.markdown("**5. Validation**")
            norm = normalize(dataset, raw, new_map)
            for w in norm.warnings:
                st.warning(w)
            if norm.errors:
                st.error(f"{len(norm.invalid_rows)} row(s) have problems:")
                st.dataframe(pd.DataFrame(norm.errors).drop(columns=["_index"]),
                             width="stretch", hide_index=True)
            else:
                st.success(f"All {len(raw):,} rows passed validation.")
            with st.expander("Normalized preview"):
                st.dataframe(show(norm.frame.drop(columns=["raw_json"], errors="ignore").head(50)),
                             width="stretch")
            c1, c2, c3 = st.columns(3)
            skip = c1.checkbox("Skip invalid rows", value=False, disabled=not norm.errors)
            save_map = c2.checkbox("Save this mapping for future imports", value=True)
            replace = c3.checkbox("Replace the existing roster", value=False) \
                if dataset == "partners" else False
            if st.button("6. Import", type="primary", disabled=not can_edit) and require_user():
                res = attempt(lambda: import_dataframe(
                    db, Y, dataset, raw, new_map, upload.name, data, user(), skip_invalid=skip,
                    save_mapping=save_map, replace_roster=replace))
                if res:
                    st.success(f"Imported {res.imported} new row(s), updated {res.updated}, "
                               f"skipped {res.duplicates} duplicate(s) and {res.invalid} invalid "
                               f"row(s). Batch #{res.batch_id}.")
                    for msg in res.messages:
                        st.info(msg)

with tab_history:
    batches = db.batches(Y)
    if batches.empty:
        st.info("No imports yet for this year.")
    else:
        st.dataframe(batches.drop(columns=["mapping_json"]), hide_index=True,
                     width="stretch")
        bid = st.selectbox("Batch", batches["batch_id"].tolist(),
                           format_func=lambda b: f"#{b} - " + batches.set_index("batch_id")
                           .loc[b, "filename"])
        row = batches.set_index("batch_id").loc[bid]
        st.caption(f"Mapping used: {row['mapping_json']}")
        f = db.batch_file(int(bid))
        if f:
            st.download_button("Download original file (audit copy)", f[1], file_name=f[0])
        if row["dataset"] in ("collections", "time_entries"):
            sure = st.checkbox("I want to remove this batch's rows")
            if st.button("Remove batch rows", disabled=not (can_edit and sure)) and require_user():
                attempt(lambda: db.delete_batch(Y, int(bid), user()), "Batch rows removed.")
                st.rerun()

with tab_review:
    st.caption("Exclude rows that must not earn credit (e.g. a confirmed duplicate) or re-include "
               "a flagged row that is a genuine separate payment. A reason is required and the "
               "change is audited. Automatic exclusions (trust deposits, void payments, outside "
               "the period, expense-only) are shown on the Audit page and need no action.")
    coll = db.load_data("collections", Y)
    if coll.empty:
        st.info("No collections imported.")
    else:
        only_flags = st.checkbox("Show only possible duplicates / excluded rows", value=True)
        view = coll[(coll["duplicate_of"] != "") | coll["excluded"]] if only_flags else coll
        cols = ["row_id", "excluded", "exclusion_reason", "duplicate_of", "collection_date",
                "client", "matter_id", "invoice_id", "payment_id", "amount_collected", "fee_amount",
                "expense_amount", "payment_type", "payment_status"]
        edited = st.data_editor(view[cols], hide_index=True, width="stretch",
                                disabled=[c for c in cols if c not in ("excluded", "exclusion_reason")]
                                if can_edit else True, key="coll_review")
        if st.button("Save inclusion changes", disabled=not can_edit) and require_user():
            changed = edited.merge(view[["row_id", "excluded", "exclusion_reason"]], on="row_id",
                                   suffixes=("", "_old"))
            changed = changed[(changed["excluded"] != changed["excluded_old"])
                              | (changed["exclusion_reason"] != changed["exclusion_reason_old"])]
            if (changed["exclusion_reason"].fillna("").str.strip() == "").any():
                st.error("Enter a reason for every changed row.")
            else:
                n = attempt(lambda: db.update_collection_flags(Y, changed, user()))
                if n is not None:
                    st.success(f"{n} row(s) updated.")
                    results(force=True)
