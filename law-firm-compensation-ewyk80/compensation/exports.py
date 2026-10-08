"""Professionally formatted Excel report (xlsxwriter).

The workbook contains live formulas where practical (pools, shares,
compensation, reconciliation) *and* the exact values produced
by the application, so reviewers can audit the arithmetic in Excel while the
application's cent-exact results remain authoritative.

Styling conventions (explained on the Executive Summary sheet):

* Inputs: blue font on yellow fill.
* Formulas: black font on light-green fill.
* Application-calculated values: black font, no fill.
"""

from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
import xlsxwriter
from xlsxwriter.utility import xl_col_to_name, xl_rowcol_to_cell

from .calculations import CompensationInputs, CompensationResult
from .models import HUNDRED, Severity, to_decimal

HEADER_ROW = 3  # zero-based row of table headers on table sheets
MAX_WIDTH = 60


class _Formats:
    def __init__(self, wb: xlsxwriter.Workbook) -> None:
        base = {"font_name": "Arial", "font_size": 10, "valign": "top"}
        self.title = wb.add_format({**base, "bold": True, "font_size": 16, "font_color": "#1F3864"})
        self.subtitle = wb.add_format({**base, "italic": True, "font_color": "#595959"})
        self.header = wb.add_format({**base, "bold": True, "font_color": "#FFFFFF",
                                     "bg_color": "#1F3864", "text_wrap": True, "border": 1,
                                     "valign": "vcenter", "align": "center"})
        self.text = wb.add_format({**base, "text_wrap": False})
        self.wrap = wb.add_format({**base, "text_wrap": True})
        self.bold = wb.add_format({**base, "bold": True})
        kinds = {"money": "$#,##0.00;[Red]-$#,##0.00", "pct": "0.000%", "num": "#,##0.00",
                 "int": "#,##0", "date": "yyyy-mm-dd", "text": "@", "bool": "General",
                 "pct2": "0.00%"}
        self.value = {k: wb.add_format({**base, "num_format": v}) for k, v in kinds.items()}
        self.input = {k: wb.add_format({**base, "num_format": v, "font_color": "#0000FF",
                                        "bg_color": "#FFFF99", "border": 1, "border_color": "#BFBFBF"})
                      for k, v in kinds.items()}
        self.formula = {k: wb.add_format({**base, "num_format": v, "bg_color": "#E2EFDA"})
                        for k, v in kinds.items()}
        self.total = {k: wb.add_format({**base, "num_format": v, "bold": True, "top": 1,
                                        "bottom": 6, "bg_color": "#E2EFDA"})
                      for k, v in kinds.items()}
        self.bad = wb.add_format({"bg_color": "#FFC7CE", "font_color": "#9C0006"})
        self.warn = wb.add_format({"bg_color": "#FFEB9C", "font_color": "#9C5700"})
        self.good = wb.add_format({"bg_color": "#C6EFCE", "font_color": "#006100"})


def _py(value: Any) -> Any:
    """Convert values to types xlsxwriter can write."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.date() if not pd.isna(value) else None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        return value.item()
    return value


def _width(header: str, values: list[Any], kind: str) -> float:
    longest_word = max((len(w) for w in str(header).split()), default=8)
    content = 0
    for v in values[:2000]:
        if v is None:
            continue
        if kind == "money":
            content = max(content, len(f"${float(v):,.2f}") + 1)
        elif kind in ("pct", "pct2"):
            content = max(content, 9)
        elif kind == "date":
            content = max(content, 11)
        else:
            content = max(content, len(str(v)))
    width = max(min(len(str(header)), 24), longest_word + 2, content + 2, 9)
    return min(width, MAX_WIDTH)


def _table(ws: Any, fmt: _Formats, title: str, subtitle: str, df: pd.DataFrame,
           kinds: dict[str, str] | None = None, inputs: set[str] | None = None,
           wrap_cols: set[str] | None = None) -> tuple[int, int, dict[str, int]]:
    """Write a titled, filtered, frozen table. Returns (first_row, last_row, col index)."""
    kinds = kinds or {}
    inputs = inputs or set()
    wrap_cols = wrap_cols or set()
    ws.write(0, 0, title, fmt.title)
    ws.write(1, 0, subtitle, fmt.subtitle)
    cols = list(df.columns)
    header_lines = 1
    for c, name in enumerate(cols):
        kind = kinds.get(name, "text")
        values = [_py(v) for v in df[name].tolist()] if not df.empty else []
        width = _width(name, values, kind)
        if name in wrap_cols:
            width = min(max(width, 30), MAX_WIDTH)
        ws.set_column(c, c, width)
        header_lines = max(header_lines, -(-len(name) // max(int(width) - 1, 1)))
        ws.write(HEADER_ROW, c, name, fmt.header)
    ws.set_row(HEADER_ROW, 14 * header_lines + 4)
    first = HEADER_ROW + 1
    for r, rec in enumerate(df.itertuples(index=False), start=first):
        for c, name in enumerate(cols):
            kind = kinds.get(name, "text")
            v = _py(rec[c])
            style = (fmt.input if name in inputs else fmt.value)[kind if kind != "text" else "text"]
            if name in wrap_cols:
                style = fmt.wrap
            if v is None or v == "":
                ws.write_blank(r, c, None, style)
            elif kind == "date" and isinstance(v, (date, datetime)):
                ws.write_datetime(r, c, datetime.combine(v, datetime.min.time())
                                  if isinstance(v, date) and not isinstance(v, datetime) else v, style)
            elif isinstance(v, bool):
                ws.write_boolean(r, c, v, style)
            elif isinstance(v, (int, float)):
                ws.write_number(r, c, v, style)
            else:
                ws.write_string(r, c, str(v), style)
    last = first + max(len(df), 1) - 1
    ws.freeze_panes(first, 1 if len(cols) > 3 else 0)
    if cols:
        ws.autofilter(HEADER_ROW, 0, max(last, first), len(cols) - 1)
    return first, last, {name: i for i, name in enumerate(cols)}


def _frac(series: pd.Series) -> pd.Series:
    """Percent numbers -> exact fractions as floats (18.2 -> 0.182, without float noise)."""
    return pd.to_numeric(series, errors="coerce").map(
        lambda v: None if pd.isna(v) else float(Decimal(repr(float(v))) / HUNDRED))


def _decimals_to_float(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object and out[c].map(lambda v: isinstance(v, Decimal)).any():
            out[c] = out[c].map(lambda v: float(v) if isinstance(v, Decimal) else v)
    return out


def build_workbook(inputs: CompensationInputs, result: CompensationResult,
                   audit_log: pd.DataFrame | None = None, status: str = "Draft",
                   prepared_by: str = "") -> bytes:
    """Build the 12-sheet XLSX report and return it as bytes."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True, "default_date_format": "yyyy-mm-dd"})
    fmt = _Formats(wb)
    policy = result.policy
    summary = _decimals_to_float(result.partner_summary)

    sheets = ["Executive Summary", "Policy Inputs", "Partner Compensation", "EWYK Detail",
              "Origination Detail", "Working Credit Detail", "Associate-Matter Mappings",
              "TimeSolv Collections", "TimeSolv Time Entries", "Partner Expenses", "Expense Allocation", "Exceptions", "Reconciliation",
              "Audit Log"]
    ws = {name: wb.add_worksheet(name) for name in sheets}
    for w in ws.values():
        w.hide_gridlines(2)
        w.set_zoom(90)

    # ------------------------------------------------------------ Policy Inputs
    pw = ws["Policy Inputs"]
    pw.write(0, 0, f"Policy Inputs - {policy.year}", fmt.title)
    pw.write(1, 0, "Blue-on-yellow cells are inputs. Editing them recalculates the formula "
                   "columns in this workbook (the application's values are shown alongside).",
             fmt.subtitle)
    for c, h in enumerate(["Setting", "Value", "Notes"]):
        pw.write(HEADER_ROW, c, h, fmt.header)
    pw.set_column(0, 0, 52)
    pw.set_column(1, 1, 20)
    pw.set_column(2, 2, 70)
    p_rows = [
        ("Compensation year", policy.year, "int", None, ""),
        ("Period start", policy.period_start, "date", None, ""),
        ("Period end", policy.period_end, "date", None, ""),
        ("Distributable partner-compensation pool", float(policy.distributable_pool), "money",
         "Pool", ""),
        ("Equal-share percentage", float(policy.equal_pct / HUNDRED), "pct", "EqualPct", ""),
        ("EWYK percentage", float(policy.ewyk_pct / HUNDRED), "pct", "EWYKPct", ""),
        ("Originating-partner portion of EWYK credit", float(policy.origination_pct / HUNDRED),
         "pct", "OrigPct", ""),
        ("Working-partner portion of EWYK credit", float(policy.working_pct / HUNDRED), "pct",
         "WorkPct", ""),
        ("Originator eligible for working credit", "Yes" if policy.originator_working_eligible
         else "No", "text", None, "If No, originators are excluded from the working-share denominator."),
        ("Include nonbillable hours", "Yes" if policy.include_nonbillable else "No", "text", None, ""),
        ("Include written-off hours", "Yes" if policy.include_written_off else "No", "text", None, ""),
        ("Include paralegal / other staff hours", "Yes" if policy.include_staff_hours else "No",
         "text", None, "Associate hours are always attributed to partners."),
        ("Working-share methodology", policy.working_method, "text", None, ""),
    ]
    for i, (label, value, kind, name, note) in enumerate(p_rows):
        r = HEADER_ROW + 1 + i
        pw.write(r, 0, label, fmt.text)
        if kind == "date":
            pw.write_datetime(r, 1, datetime.combine(value, datetime.min.time()), fmt.input["date"])
        elif isinstance(value, str):
            pw.write_string(r, 1, value, fmt.input["text"])
        else:
            pw.write_number(r, 1, value, fmt.input[kind])
        pw.write(r, 2, note, fmt.wrap)
        if name:
            wb.define_name(name, f"='Policy Inputs'!{xl_rowcol_to_cell(r, 1, True, True)}")
    chk = HEADER_ROW + 2 + len(p_rows)
    pw.write(chk, 0, "Check: Equal + EWYK = 100%", fmt.bold)
    pw.write_formula(chk, 1, '=IF(ROUND(EqualPct+EWYKPct,6)=1,"OK","ERROR")',
                     fmt.formula["text"], "OK" if policy.equal_pct + policy.ewyk_pct
                     == HUNDRED else "ERROR")
    pw.write(chk + 1, 0, "Check: Origination + Working = 100%", fmt.bold)
    pw.write_formula(chk + 1, 1, '=IF(ROUND(OrigPct+WorkPct,6)=1,"OK","ERROR")',
                     fmt.formula["text"], "OK" if policy.origination_pct + policy.working_pct
                     == HUNDRED else "ERROR")
    for r in (chk, chk + 1):
        pw.conditional_format(r, 1, r, 1, {"type": "cell", "criteria": "==", "value": '"ERROR"',
                                           "format": fmt.bad})
    pw.freeze_panes(HEADER_ROW + 1, 0)

    # ------------------------------------------------------------ Partner Compensation
    cw = ws["Partner Compensation"]
    pc = pd.DataFrame({
        "Partner": summary["Partner"], "Managing Partner": summary["Managing Partner"],
        "Active": summary["Active"],
        "Total EWYK credit": summary["Total EWYK credit"],
        "EWYK performance share": _frac(summary["EWYK performance share %"]),
        "Equal compensation (formula)": 0.0, "EWYK compensation (formula)": 0.0,
        "Total compensation (formula)": 0.0,
        "Equal compensation (application)": summary["Equal compensation"],
        "EWYK compensation (application)": summary["EWYK compensation"],
        "Total compensation (application)": summary["Total compensation"],
        "Rounding difference (formula - application)": 0.0,
    }) if not summary.empty else pd.DataFrame()
    kinds = {c: "money" for c in pc.columns}
    kinds.update({"Partner": "text", "Managing Partner": "bool", "Active": "bool",
                  "EWYK performance share": "pct"})
    first, last, cols = _table(cw, fmt, f"Partner Compensation - {policy.year}",
                               "Formula columns (green) recompute from Policy Inputs; application "
                               "columns are the cent-exact amounts to be paid.", pc, kinds)
    if not pc.empty:
        n_active = int(summary["Active"].sum())
        tot_credit = float(summary.loc[summary["Active"], "Total EWYK credit"].sum())
        pool = float(policy.distributable_pool)

        def col(name: str) -> str:
            return xl_col_to_name(cols[name])

        rng_active = f"${col('Active')}${first + 1}:${col('Active')}${last + 1}"
        rng_credit = f"${col('Total EWYK credit')}${first + 1}:${col('Total EWYK credit')}${last + 1}"
        for i, rec in enumerate(summary.to_dict("records")):
            r = first + i
            n = r + 1
            act = rec["Active"]
            share = rec["Total EWYK credit"] / tot_credit if act and tot_credit else 0.0
            eq = pool * float(policy.equal_pct) / 100 / n_active if act and n_active else 0.0
            ew = pool * float(policy.ewyk_pct) / 100 * share if act else 0.0
            a = f"${col('Active')}{n}"
            cw.write_formula(r, cols["EWYK performance share"],
                             f"=IF({a},{col('Total EWYK credit')}{n}/SUMIFS({rng_credit},{rng_active},TRUE),0)",
                             fmt.formula["pct"], share)
            cw.write_formula(r, cols["Equal compensation (formula)"],
                             f"=IF({a},Pool*EqualPct/COUNTIF({rng_active},TRUE),0)",
                             fmt.formula["money"], eq)
            cw.write_formula(r, cols["EWYK compensation (formula)"],
                             f"=IF({a},Pool*EWYKPct*{col('EWYK performance share')}{n},0)",
                             fmt.formula["money"], ew)
            cw.write_formula(r, cols["Total compensation (formula)"],
                             f"={col('Equal compensation (formula)')}{n}+{col('EWYK compensation (formula)')}{n}",
                             fmt.formula["money"], eq + ew)
            cw.write_formula(r, cols["Rounding difference (formula - application)"],
                             f"={col('Total compensation (formula)')}{n}-"
                             f"{col('Total compensation (application)')}{n}",
                             fmt.formula["money"], eq + ew - rec["Total compensation"])
        tr = last + 1
        cw.write(tr, 0, "TOTAL", fmt.total["text"])
        for name in pc.columns[3:]:
            k = "pct" if name == "EWYK performance share" else "money"
            formula = f"=SUM({col(name)}{first + 1}:{col(name)}{last + 1})"
            cw.write_formula(tr, cols[name], formula, fmt.total[k])
        cw.write(tr + 2, 0, "Check: application total = pool", fmt.bold)
        app_tot = f"{col('Total compensation (application)')}{tr + 1}"
        cw.write_formula(tr + 2, 1, f'=IF(ROUND({app_tot}-Pool,2)=0,"OK","DIFFERENCE")',
                         fmt.formula["text"], "OK" if result.reconciled else "DIFFERENCE")
        for r in (tr + 2,):
            cw.conditional_format(r, 1, r, 1, {"type": "cell", "criteria": "!=", "value": '"OK"',
                                               "format": fmt.bad})
        cw.conditional_format(first, cols["Active"], last, cols["Active"],
                              {"type": "cell", "criteria": "==", "value": "FALSE",
                               "format": fmt.warn})

    # ------------------------------------------------------------ EWYK Detail
    ew = ws["EWYK Detail"]
    ed_cols = ["Partner", "Active", "Originating credit", "Own partner-hour working credit",
               "Supervised associate-hour working credit", "Other delegated-hour working credit",
               "Manual-allocation working credit", "Total working credit", "Total EWYK credit",
               "EWYK performance share %", "EWYK compensation"]
    ed = summary[ed_cols].copy() if not summary.empty else pd.DataFrame(columns=ed_cols)
    ed = ed.rename(columns={"EWYK performance share %": "EWYK performance share"})
    ed["EWYK performance share"] = _frac(ed["EWYK performance share"])
    kinds = {c: "money" for c in ed.columns}
    kinds.update({"Partner": "text", "Active": "bool", "EWYK performance share": "pct"})
    first, last, cols = _table(ew, fmt, "EWYK Detail",
                               "Total EWYK credit = origination credit + working credit. Share = "
                               "partner credit / total credit of ACTIVE partners.", ed, kinds)
    if not ed.empty:
        def c2(name: str) -> str:
            return xl_col_to_name(cols[name])
        ra = f"${c2('Active')}${first + 1}:${c2('Active')}${last + 1}"
        rc = f"${c2('Total EWYK credit')}${first + 1}:${c2('Total EWYK credit')}${last + 1}"
        for i, rec in enumerate(ed.to_dict("records")):
            n = first + i + 1
            ew.write_formula(n - 1, cols["Total working credit"],
                             f"=SUM({c2('Own partner-hour working credit')}{n}:"
                             f"{c2('Manual-allocation working credit')}{n})",
                             fmt.formula["money"], rec["Total working credit"])
            ew.write_formula(n - 1, cols["Total EWYK credit"],
                             f"={c2('Originating credit')}{n}+{c2('Total working credit')}{n}",
                             fmt.formula["money"], rec["Total EWYK credit"])
            ew.write_formula(n - 1, cols["EWYK performance share"],
                             f"=IF({c2('Active')}{n},{c2('Total EWYK credit')}{n}/SUMIFS({rc},{ra},TRUE),0)",
                             fmt.formula["pct"], rec["EWYK performance share"])
        tr = last + 1
        ew.write(tr, 0, "TOTAL", fmt.total["text"])
        for name in ed.columns[2:]:
            k = "pct" if name == "EWYK performance share" else "money"
            ew.write_formula(tr, cols[name], f"=SUM({c2(name)}{first + 1}:{c2(name)}{last + 1})",
                             fmt.total[k])

    # ------------------------------------------------------------ Origination Detail
    od = _decimals_to_float(result.origination_detail)
    od["Origination %"] = _frac(od["Origination %"]) if not od.empty else []
    od["Originator share %"] = _frac(od["Originator share %"]) if not od.empty else []
    od.insert(len(od.columns) - 1, "Formula credit (unrounded)", 0.0)
    kinds = {"Fees collected": "money", "Origination portion": "money", "Origination credit": "money",
             "Formula credit (unrounded)": "money", "Origination %": "pct",
             "Originator share %": "pct", "Collection date": "date"}
    first, last, cols = _table(ws["Origination Detail"], fmt, "Origination Detail",
                               "Origination credit = collected professional fees x origination % x "
                               "originator share. Application amounts are allocated to the cent.",
                               od, kinds)
    if not od.empty:
        f_ = {k: xl_col_to_name(v) for k, v in cols.items()}
        for i, rec in enumerate(od.to_dict("records")):
            n = first + i + 1
            ws["Origination Detail"].write_formula(
                n - 1, cols["Formula credit (unrounded)"],
                f"={f_['Fees collected']}{n}*{f_['Origination %']}{n}*{f_['Originator share %']}{n}",
                fmt.formula["money"],
                rec["Fees collected"] * rec["Origination %"] * rec["Originator share %"])
        _total_row(ws["Origination Detail"], fmt, first, last, cols,
                   ["Origination credit", "Formula credit (unrounded)"])

    # ------------------------------------------------------------ Working Credit Detail
    wd = _decimals_to_float(result.working_detail)
    wd.insert(len(wd.columns) - 1, "Formula credit (unrounded)", 0.0)
    kinds = {"Fees collected": "money", "Working portion": "money", "Working credit": "money",
             "Formula credit (unrounded)": "money", "Working share": "pct",
             "Credited measure": "num", "Total credited measure": "num", "Collection date": "date"}
    wsw = ws["Working Credit Detail"]
    first, last, cols = _table(wsw, fmt, "Working Credit Detail",
                               f"Working credit = fees x {policy.working_pct}% x (partner credited "
                               f"measure / total credited measure). Measure basis: "
                               f"{policy.working_method}.", wd, kinds, wrap_cols={"Note"})
    if not wd.empty:
        f_ = {k: xl_col_to_name(v) for k, v in cols.items()}
        for i, rec in enumerate(wd.to_dict("records")):
            n = first + i + 1
            share = rec["Working share"] or 0.0
            wsw.write_formula(n - 1, cols["Working share"],
                              f"=IF({f_['Total credited measure']}{n}=0,0,"
                              f"{f_['Credited measure']}{n}/{f_['Total credited measure']}{n})",
                              fmt.formula["pct"], share)
            wsw.write_formula(n - 1, cols["Formula credit (unrounded)"],
                              f"={f_['Working portion']}{n}*{f_['Working share']}{n}",
                              fmt.formula["money"], rec["Working portion"] * share)
        _total_row(wsw, fmt, first, last, cols, ["Working credit", "Formula credit (unrounded)"])
        wsw.conditional_format(first, cols["Method"], last, cols["Method"],
                               {"type": "text", "criteria": "containing", "value": "fallback",
                                "format": fmt.warn})

    # ------------------------------------------------------------ Mappings
    mp = inputs.supervision.copy() if not inputs.supervision.empty else pd.DataFrame(
        columns=["timekeeper", "matter_id", "partner", "start_date", "end_date",
                 "allocation_pct", "notes", "source"])
    mp = mp.rename(columns={"timekeeper": "Timekeeper", "matter_id": "Matter ID",
                            "partner": "Credited partner", "start_date": "Effective start",
                            "end_date": "Effective end", "allocation_pct": "Allocation %",
                            "notes": "Notes", "source": "Source of assignment"})
    if not mp.empty:
        mp["Allocation %"] = _frac(mp["Allocation %"])
    first, last, cols = _table(ws["Associate-Matter Mappings"], fmt, "Associate-Matter Mappings",
                               "Level-2 attribution rules. Splits for the same timekeeper, matter "
                               "and dates must total 100%.", mp,
                               {"Allocation %": "pct"}, inputs=set(mp.columns), wrap_cols={"Notes"})

    # ------------------------------------------------------------ Collections
    cs = _decimals_to_float(result.collection_status)
    kinds = {c: "money" for c in ["Amount collected", "Fees", "Expenses", "Taxes",
                                  "Origination credited", "Working credited", "Unallocated"]}
    kinds.update({"Collection date": "date", "Row ID": "int", "Included": "bool"})
    first, last, cols = _table(ws["TimeSolv Collections"], fmt, "TimeSolv Collections",
                               "Every imported collection row with its inclusion decision. Only "
                               "the professional-fee allocation of included rows earns credit.",
                               cs, kinds, wrap_cols={"Exclusion reason"})
    if not cs.empty:
        ws["TimeSolv Collections"].conditional_format(
            first, cols["Included"], last, cols["Included"],
            {"type": "cell", "criteria": "==", "value": "FALSE", "format": fmt.warn})
        _total_row(ws["TimeSolv Collections"], fmt, first, last, cols,
                   ["Amount collected", "Fees", "Expenses", "Taxes", "Origination credited",
                    "Working credited", "Unallocated"])

    # ------------------------------------------------------------ Time entries
    te = result.attribution_detail.copy()
    raw_te = inputs.time_entries
    if not te.empty and not raw_te.empty:
        extra = raw_te.assign(_id=raw_te["entry_id"].where(raw_te["entry_id"] != "",
                                                           raw_te["dedup_key"]))
        extra = extra[["_id", "billable", "billed_status", "hours_billed", "rate", "billed_value",
                       "task_code", "description"]].rename(columns={
                           "billable": "Billable", "billed_status": "Billed status",
                           "hours_billed": "Hours billed", "rate": "Rate",
                           "billed_value": "Billed value", "task_code": "Task code",
                           "description": "Description"})
        te = te.merge(extra.drop_duplicates("_id"), left_on="Entry ID", right_on="_id",
                      how="left").drop(columns=["_id"])
    if "Allocation %" in te:
        te["Allocation %"] = _frac(te["Allocation %"])
    kinds = {"Work date": "date", "Recorded hours": "num", "Hours credited to partner": "num",
             "Qualifying measure": "num", "Allocation %": "pct", "Credited measure": "num",
             "Hours billed": "num", "Rate": "money", "Billed value": "money", "Billable": "bool"}
    first, last, cols = _table(ws["TimeSolv Time Entries"], fmt, "TimeSolv Time Entries",
                               "Each entry with its qualifying measure and credited partner "
                               "(split entries appear once per partner).", te, kinds)
    if not te.empty:
        ws["TimeSolv Time Entries"].conditional_format(
            first, cols["Credited partner"], last, cols["Credited partner"],
            {"type": "cell", "criteria": "==", "value": '"UNASSIGNED"', "format": fmt.bad})

    # ------------------------------------------------------------ Partner Expenses
    _expense_sheets(ws["Partner Expenses"], ws["Expense Allocation"], fmt, result, summary)

    # ------------------------------------------------------------ Exceptions
    ex = result.exceptions.copy()
    exw = ws["Exceptions"]
    first, last, cols = _table(exw, fmt, "Exceptions",
                               "Blocking items must be resolved (or expressly excluded) before the "
                               "year can be finalized.", ex,
                               {"Amount": "money", "Hours / Measure": "num"},
                               wrap_cols={"Description", "How to resolve"})
    if not ex.empty:
        rng = (first, 0, last, len(ex.columns) - 1)
        exw.conditional_format(*rng, {"type": "formula", "criteria": f'=$A{first + 1}="Blocking"',
                                      "format": fmt.bad})
        exw.conditional_format(*rng, {"type": "formula", "criteria": f'=$A{first + 1}="Warning"',
                                      "format": fmt.warn})
    else:
        exw.write(first, 0, "No exceptions.", fmt.text)

    # ------------------------------------------------------------ Reconciliation
    rc = _decimals_to_float(result.reconciliation)
    rw = ws["Reconciliation"]
    first, last, cols = _table(rw, fmt, "Reconciliation",
                               "Application checks (values) followed by live workbook checks "
                               "(formulas).", rc,
                               {"Expected": "num", "Actual": "num", "Difference": "num"})
    if not rc.empty:
        rw.conditional_format(first, cols["Status"], last, cols["Status"],
                              {"type": "cell", "criteria": "==", "value": '"OK"', "format": fmt.good})
        rw.conditional_format(first, cols["Status"], last, cols["Status"],
                              {"type": "cell", "criteria": "!=", "value": '"OK"', "format": fmt.bad})
        r0 = last + 3
        rw.write(r0 - 1, 0, "Live workbook checks", fmt.bold)
        n_pc = len(summary)
        tot_row = HEADER_ROW + 1 + n_pc + 1  # 1-based row of TOTAL on Partner Compensation
        pcn = {name: xl_col_to_name(i) for i, name in enumerate(pc.columns)} if not pc.empty else {}
        live = [
            ("Pools (Equal + EWYK) x Pool = Pool", "=Pool*(EqualPct+EWYKPct)",
             "=Pool"),
            ("Formula total compensation = Pool",
             f"='Partner Compensation'!{pcn.get('Total compensation (formula)', 'A')}{tot_row}", "=Pool"),
            ("Application total compensation = Pool",
             f"='Partner Compensation'!{pcn.get('Total compensation (application)', 'A')}{tot_row}",
             "=Pool"),
        ]
        for j, (label, actual, expected) in enumerate(live):
            r = r0 + j
            rw.write(r, 0, label, fmt.text)
            rw.write_formula(r, 1, expected, fmt.formula["money"])
            rw.write_formula(r, 2, actual, fmt.formula["money"])
            rw.write_formula(r, 3, f"=C{r + 1}-B{r + 1}", fmt.formula["money"])
            rw.write_formula(r, 4, f'=IF(ABS(D{r + 1})<0.005,"OK","DIFFERENCE")', fmt.formula["text"])
        rw.conditional_format(r0, 4, r0 + len(live) - 1, 4,
                              {"type": "cell", "criteria": "!=", "value": '"OK"', "format": fmt.bad})

    # ------------------------------------------------------------ Audit Log
    al = audit_log if audit_log is not None else pd.DataFrame(
        columns=["log_id", "ts", "year", "user", "action", "details"])
    al = al.rename(columns={"log_id": "Log ID", "ts": "Timestamp", "year": "Year", "user": "User",
                            "action": "Action", "details": "Details"})
    if "Details" in al:
        al["Details"] = al["Details"].astype(str).str.slice(0, 2000)
    _table(ws["Audit Log"], fmt, "Audit Log", "All recorded changes for this year (newest first).",
           al, {"Log ID": "int", "Year": "int"}, wrap_cols={"Details"})

    # ------------------------------------------------------------ Executive Summary
    _executive_summary(ws["Executive Summary"], fmt, result, status, prepared_by)
    wb.close()
    return buf.getvalue()


def _expense_sheets(ws: Any, wd: Any, fmt: _Formats, result: CompensationResult,
                    summary: pd.DataFrame) -> None:
    """Partner Expenses (net compensation with formulas) and Expense Allocation (detail)."""
    exp = result.expenses
    cats = [c for c in exp.by_partner.columns if c not in ("Partner", "Total expenses")] \
        if not exp.by_partner.empty else []
    by_p = exp.by_partner.set_index("Partner") if not exp.by_partner.empty else pd.DataFrame()
    rows = []
    for rec in summary.to_dict("records"):
        row = {"Partner": rec["Partner"], "Active": rec["Active"],
               "Gross compensation": rec["Total compensation"]}
        for c in cats:
            row[c] = float(by_p.loc[rec["Partner"], c]) if rec["Partner"] in by_p.index else 0.0
        row.update({"Total expenses": rec["Allocated expenses"],
                    "Prior-year carry-forward": rec["Prior-year carry-forward"],
                    "Net compensation": rec["Net compensation"],
                    "Net payable": rec["Net payable"],
                    "Shortfall (carried forward or owed)": rec["Carry forward to next year"]
                    + rec["Owed to the firm"]})
        rows.append(row)
    df = pd.DataFrame(rows)
    kinds = {c: "money" for c in df.columns}
    kinds.update({"Partner": "text", "Active": "bool"})
    first, last, cols = _table(ws, fmt, f"Partner Expenses and Net Compensation - {result.policy.year}",
                               f"Net = gross - expenses - prior-year carry-forward. Negative net: "
                               f"{result.policy.negative_net_treatment}.", df, kinds,
                               inputs={"Prior-year carry-forward"})
    if df.empty:
        return
    f_ = {k: xl_col_to_name(v) for k, v in cols.items()}
    for i, rec in enumerate(rows):
        r = first + i
        n = r + 1
        if cats:
            ws.write_formula(r, cols["Total expenses"],
                             f"=SUM({f_[cats[0]]}{n}:{f_[cats[-1]]}{n})", fmt.formula["money"],
                             rec["Total expenses"])
        ws.write_formula(r, cols["Net compensation"],
                         f"={f_['Gross compensation']}{n}-{f_['Total expenses']}{n}"
                         f"-{f_['Prior-year carry-forward']}{n}", fmt.formula["money"],
                         rec["Net compensation"])
        ws.write_formula(r, cols["Net payable"], f"=MAX({f_['Net compensation']}{n},0)",
                         fmt.formula["money"], rec["Net payable"])
        ws.write_formula(r, cols["Shortfall (carried forward or owed)"],
                         f"=MAX(-{f_['Net compensation']}{n},0)", fmt.formula["money"],
                         rec["Shortfall (carried forward or owed)"])
    tr = last + 1
    ws.write(tr, 0, "TOTAL", fmt.total["text"])
    for name in df.columns[2:]:
        ws.write_formula(tr, cols[name], f"=SUM({f_[name]}{first + 1}:{f_[name]}{last + 1})",
                         fmt.total["money"])
    ws.write(tr + 2, 0, "Check: expenses allocated = expenses entered", fmt.bold)
    ws.write_formula(tr + 2, 1, f'=IF(ROUND({f_["Total expenses"]}{tr + 1}-'
                                f'{float(exp.total_expenses)},2)=0,"OK","DIFFERENCE")',
                     fmt.formula["text"], "OK" if exp.total_expenses == exp.total_allocated
                     else "DIFFERENCE")
    ws.conditional_format(tr + 2, 1, tr + 2, 1, {"type": "cell", "criteria": "!=",
                                                   "value": '"OK"', "format": fmt.bad})
    ws.conditional_format(first, cols["Net compensation"], last, cols["Net compensation"],
                          {"type": "cell", "criteria": "<", "value": 0, "format": fmt.bad})
    det = _decimals_to_float(exp.detail)
    det["Share %"] = _frac(det["Share %"]) if not det.empty else []
    d_first, d_last, d_cols = _table(wd, fmt, "Expense Allocation",
                                     "Every expense and the partner(s) it was charged to.", det,
                                     {"Date": "date", "Expense amount": "money", "Basis": "num",
                                      "Share %": "pct", "Allocated amount": "money"})
    if not det.empty:
        _total_row(wd, fmt, d_first, d_last, d_cols, ["Allocated amount"])
        wd.conditional_format(d_first, d_cols["Partner"], d_last, d_cols["Partner"],
                              {"type": "cell", "criteria": "==", "value": '"(unallocated)"',
                               "format": fmt.bad})


def _total_row(ws: Any, fmt: _Formats, first: int, last: int, cols: dict[str, int],
               names: list[str]) -> None:
    tr = last + 1
    ws.write(tr, 0, "TOTAL", fmt.total["text"])
    for name in names:
        c = xl_col_to_name(cols[name])
        ws.write_formula(tr, cols[name], f"=SUBTOTAL(9,{c}{first + 1}:{c}{last + 1})",
                         fmt.total["money"])


def _executive_summary(ws: Any, fmt: _Formats, result: CompensationResult, status: str,
                       prepared_by: str) -> None:
    policy = result.policy
    m = result.metrics
    ws.set_column(0, 0, 50)
    ws.set_column(1, 1, 22)
    ws.set_column(2, 2, 70)
    ws.write(0, 0, f"Partner Compensation Report - {policy.year}", fmt.title)
    ws.write(1, 0, f"Status: {status}.  Generated {datetime.now():%Y-%m-%d %H:%M}"
                   + (f" by {prepared_by}" if prepared_by else "") + ".", fmt.subtitle)
    for c, h in enumerate(["Measure", "Value", "Notes"]):
        ws.write(HEADER_ROW, c, h, fmt.header)
    n_ex = len(result.exceptions)
    rows: list[tuple[str, Any, str, str, bool]] = [
        ("Distributable compensation pool", "=Pool", "money",
         "Policy Inputs (input).", True),
        ("Equal pool", "=Pool*EqualPct", "money", f"{policy.equal_pct}% of the pool.", True),
        ("EWYK pool", "=Pool*EWYKPct", "money", f"{policy.ewyk_pct}% of the pool.", True),
        ("Total compensation allocated (application)",
         f"='Partner Compensation'!{xl_col_to_name(10)}{HEADER_ROW + 2 + len(result.partner_summary)}",
         "money", "Sum of cent-exact partner totals.", True),
        ("Reconciliation difference", "=B8-B5", "money", "Must be zero.", True),
        ("Total collections (all included and excluded cash rows)", float(m["total_collections"]),
         "money", "Fees + expenses + taxes as exported (manually excluded rows omitted).", False),
        ("Total collected professional fees (credited)", float(m["total_fees_collected"]), "money",
         "Only fee allocations of qualifying collections.", False),
        ("Unallocated EWYK credit", float(m["unallocated_credit"]), "money",
         "Fees whose credit could not be assigned (see Exceptions).", False),
        ("Active partners", m["active_partners"], "int", "", False),
        ("Unassigned non-partner hours", float(m["unassigned_hours"]), "num",
         "Must be resolved or expressly excluded before finalization.", False),
        ("Collections with missing originators", m["collections_missing_originator"], "int", "", False),
        ("Matters with incomplete supervisory mappings", m["matters_incomplete_supervision"], "int",
         "", False),
        ("Collections using matter-level fallback", m["fallback_collections"], "int",
         "See Working Credit Detail (Method column).", False),
        ("Blocking exceptions", f'=COUNTIF(Exceptions!A{HEADER_ROW + 2}:A{HEADER_ROW + 1 + max(n_ex, 1)},'
                                f'"{Severity.BLOCKING}")', "int", "", True),
        ("Partner expenses allocated", float(m.get("total_expenses", 0)), "money",
         "See Partner Expenses sheet.", False),
        ("Net payable to partners (after expenses)", float(m.get("total_net_payable", 0)),
         "money", "Gross compensation less expenses and prior-year carry-forwards.", False),
        ("Shortfalls carried forward to next year", float(m.get("total_carry_forward_out", 0)),
         "money", f"Policy: {policy.negative_net_treatment}.", False),
        ("Reconciliation status", "OK" if result.reconciled else "DIFFERENCE", "text",
         "See Reconciliation sheet.", False),
    ]
    for i, (label, value, kind, note, is_formula) in enumerate(rows):
        r = HEADER_ROW + 1 + i
        ws.write(r, 0, label, fmt.text)
        if is_formula:
            cached = {
                "Distributable compensation pool": float(result.pools["distributable"]),
                "Equal pool": float(result.pools["equal"]), "EWYK pool": float(result.pools["ewyk"]),
                "Total compensation allocated (application)": float(
                    sum((to_decimal(v) for v in result.partner_summary["Total compensation"]),
                        Decimal(0))) if not result.partner_summary.empty else 0.0,
                "Reconciliation difference": 0.0,
                "Blocking exceptions": result.blocking_count,
            }.get(label, 0)
            ws.write_formula(r, 1, value, fmt.formula[kind], cached)
        elif isinstance(value, str):
            ws.write_string(r, 1, value, fmt.value["text"])
        else:
            ws.write_number(r, 1, value, fmt.value[kind])
        ws.write(r, 2, note, fmt.wrap)
    r_status = HEADER_ROW + len(rows)
    ws.conditional_format(r_status, 1, r_status, 1, {"type": "cell", "criteria": "!=",
                                                     "value": '"OK"', "format": fmt.bad})
    leg = HEADER_ROW + len(rows) + 3
    ws.write(leg, 0, "Legend", fmt.bold)
    ws.write(leg + 1, 0, "Input (editable assumption)", fmt.text)
    ws.write(leg + 1, 1, "Input", fmt.input["text"])
    ws.write(leg + 2, 0, "Formula (recalculates in Excel)", fmt.text)
    ws.write(leg + 2, 1, "Formula", fmt.formula["text"])
    ws.write(leg + 3, 0, "Application value (authoritative, cent-exact)", fmt.text)
    ws.write(leg + 3, 1, "Value", fmt.value["text"])
    ws.write(leg + 5, 0, "Policy assumptions", fmt.bold)
    for j, (label, value) in enumerate(policy.describe()):
        ws.write(leg + 6 + j, 0, label, fmt.text)
        ws.write(leg + 6 + j, 1, value, fmt.value["text"])
    ws.freeze_panes(HEADER_ROW + 1, 0)
