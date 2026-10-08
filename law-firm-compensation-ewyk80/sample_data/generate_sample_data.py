"""Generate the realistic demo TimeSolv-style exports in this folder.

Run ``python sample_data/generate_sample_data.py`` to regenerate.  Output is
deterministic (fixed random seed).  Column headings intentionally mimic
TimeSolv exports rather than the application's logical field names so the
import wizard's automatic mapping is exercised.

Scenario highlights (year 2025):

* 11 partners (Margaret Chen is Managing Partner), 5 associates, 2 paralegals
  and a law clerk.
* 12 matters; M-1003 has split origination (Chen 60% / Alvarez 40%).
* Daniel Smith normally works for David Okafor but is assigned to Elena Petrova
  on M-1004.
* Rachel Jones is split 60/40 between Michael Brennan and Aisha Mohammed on
  M-1007.
* Kevin Park's M-1005 supervision changes mid-year (effective-dated mapping).
* M-1012 time entries carry no invoice numbers -> matter-level fallback.
* Quarterly invoices, partial payments, fee + expense (+ tax) allocations, an
  unapplied trust deposit, a void payment, a payment outside the period and a
  duplicated payment row.
* INTENTIONAL UNRESOLVED EXCEPTION: Owen Fletcher's hours on M-1011 have no
  mapping, the matter has no compensation supervising partner, its Responsible
  Professional is an associate and Fletcher has no default supervisor, so the
  hours are flagged as unassigned.  Kevin Park also logged time to M-1099,
  which is not in the matters list.
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

OUT = Path(__file__).resolve().parent
YEAR = 2025
rng = random.Random(20250101)

PARTNERS = [
    ("P01", "Margaret Chen", 650, True), ("P02", "David Okafor", 575, False),
    ("P03", "Sarah Lindqvist", 560, False), ("P04", "James Whitfield", 550, False),
    ("P05", "Priya Raman", 540, False), ("P06", "Robert Alvarez", 530, False),
    ("P07", "Elena Petrova", 525, False), ("P08", "Michael Brennan", 520, False),
    ("P09", "Aisha Mohammed", 515, False), ("P10", "Thomas Nakamura", 510, False),
    ("P11", "Catherine Doyle", 505, False),
]
STAFF = [
    ("A01", "Daniel Smith", "Senior Associate", "Associate", 395, "David Okafor"),
    ("A02", "Rachel Jones", "Associate", "Associate", 350, "Sarah Lindqvist"),
    ("A03", "Kevin Park", "Associate", "Associate", 340, "James Whitfield"),
    ("A04", "Laura Martinez", "Associate", "Associate", 330, "Priya Raman"),
    ("A05", "Owen Fletcher", "Junior Associate", "Associate", 295, ""),
    ("L01", "Grace Liu", "Senior Paralegal", "Paralegal", 195, "Margaret Chen"),
    ("L02", "Henry Adams", "Paralegal", "Paralegal", 185, "Robert Alvarez"),
    ("S01", "Nina Patel", "Law Clerk", "Staff", 150, ""),
]
RATES = {n: r for _, n, r, _ in PARTNERS} | {s[1]: s[4] for s in STAFF}
TYPES = {n: "Partner" for _, n, _, _ in PARTNERS} | {s[1]: s[3] for s in STAFF}

# matter id, client, name, originating attorney (export), responsible professional,
# practice area, staff list
MATTERS = [
    ("M-1001", "Harborview Holdings LLC", "Commercial lease dispute", "David Okafor",
     "David Okafor", "Litigation", ["David Okafor", "Daniel Smith", "Grace Liu"]),
    ("M-1002", "Pinecrest Medical Group", "Regulatory compliance review", "Sarah Lindqvist",
     "Sarah Lindqvist", "Healthcare", ["Sarah Lindqvist", "Rachel Jones", "Laura Martinez",
                                       "Margaret Chen"]),
    ("M-1003", "Atlas Manufacturing Inc.", "Acquisition of Delta Tooling", "Margaret Chen",
     "Margaret Chen", "Corporate", ["Margaret Chen", "Robert Alvarez", "Daniel Smith", "Grace Liu"]),
    ("M-1004", "Bluewater Logistics", "Employment class action defense", "James Whitfield",
     "Elena Petrova", "Employment", ["James Whitfield", "Elena Petrova", "Daniel Smith"]),
    ("M-1005", "Greenfield Energy Partners", "Solar project finance", "Priya Raman",
     "Priya Raman", "Finance", ["Priya Raman", "Kevin Park", "Henry Adams"]),
    ("M-1006", "Northgate School District", "Construction defect claim", "Robert Alvarez",
     "Michael Brennan", "Construction", ["Michael Brennan", "Robert Alvarez", "Kevin Park"]),
    ("M-1007", "Summit Biotech Corp.", "Patent licensing program", "Aisha Mohammed",
     "Aisha Mohammed", "Intellectual Property", ["Aisha Mohammed", "Michael Brennan",
                                                 "Rachel Jones"]),
    ("M-1008", "Riverside Credit Union", "Commercial loan workout", "Thomas Nakamura",
     "Thomas Nakamura", "Banking", ["Thomas Nakamura", "Laura Martinez"]),
    ("M-1009", "Estate of Harold Vance", "Estate administration", "Catherine Doyle",
     "Catherine Doyle", "Trusts & Estates", ["Catherine Doyle", "Nina Patel", "Henry Adams"]),
    ("M-1010", "Keystone Retail Partners", "Retail portfolio sale", "Michael Brennan",
     "Rachel Jones", "Real Estate", ["Michael Brennan", "Rachel Jones", "James Whitfield"]),
    ("M-1011", "Cedar Point Hospitality", "Franchise agreement litigation", "Elena Petrova",
     "Owen Fletcher", "Litigation", ["Elena Petrova", "Owen Fletcher"]),
    ("M-1012", "Meridian Software Ltd.", "SaaS contract portfolio", "Thomas Nakamura",
     "David Okafor", "Technology", ["Thomas Nakamura", "David Okafor", "Laura Martinez"]),
]
NO_INVOICE_LINK = {"M-1012"}
TASKS = [("L110", "Fact investigation / development"), ("L120", "Analysis / strategy"),
         ("L210", "Pleadings"), ("L250", "Other written motions and submissions"),
         ("L310", "Written discovery"), ("C100", "Fact gathering"), ("C300", "Analysis and advice"),
         ("A101", "Plan and prepare for"), ("A103", "Draft / revise"), ("A104", "Review / analyze"),
         ("A106", "Communicate (with client)"), ("A108", "Communicate (other external)")]
QUARTERS = [((1, 3), date(YEAR, 4, 4)), ((4, 6), date(YEAR, 7, 3)), ((7, 9), date(YEAR, 10, 3)),
            ((10, 12), date(YEAR, 12, 31))]


def money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def hours_for(name: str) -> int:
    t = TYPES[name]
    return {"Partner": 3, "Associate": 5, "Paralegal": 3, "Staff": 2}[t]


def main() -> None:
    entries = []
    invoices: dict[tuple[str, int], dict] = {}
    eid = 500000
    for mid, client, _, _, _, _, staff in MATTERS:
        for month in range(1, 13):
            if month == 12 and mid in ("M-1009",):
                continue
            q = (month - 1) // 3
            for person in staff:
                for _ in range(rng.randint(1, hours_for(person))):
                    eid += 1
                    day = date(YEAR, month, rng.randint(1, 28))
                    hrs = Decimal(str(round(rng.uniform(0.3, 5.5), 1)))
                    task, desc = rng.choice(TASKS)
                    billable = rng.random() > 0.06
                    rate = Decimal(RATES[person])
                    if not billable:
                        status, billed_hrs, inv = "Non-billable", Decimal("0"), ""
                        desc = "Internal file management / training"
                    elif q == 3 and month >= 11:
                        status, billed_hrs, inv = "Unbilled", Decimal("0"), ""
                    elif rng.random() < 0.03:
                        status, billed_hrs, inv = "Written Off", Decimal("0"), ""
                    else:
                        status = "Billed"
                        billed_hrs = hrs
                        if rng.random() < 0.10:
                            billed_hrs = max(Decimal("0.1"), hrs - Decimal("0.5"))
                        inv = f"INV-{mid[2:]}-{q + 1}"
                    billed_amt = money(billed_hrs * rate) if status == "Billed" else Decimal("0")
                    if status == "Billed":
                        key = (mid, q)
                        invoices.setdefault(key, {"invoice": inv, "fees": Decimal("0"),
                                                  "date": QUARTERS[q][1]})
                        invoices[key]["fees"] += billed_amt
                    entries.append({
                        "Entry ID": f"TE-{eid}", "Date": day.strftime("%m/%d/%Y"), "Client": client,
                        "Project ID": mid,
                        "Invoice Number": "" if mid in NO_INVOICE_LINK else inv,
                        "Timekeeper": person, "Professional Type": TYPES[person],
                        "Hours": f"{hrs}", "Billable": "Yes" if billable else "No",
                        "Billed Status": status, "Billed Hours": f"{billed_hrs}",
                        "Rate": f"{rate:.2f}", "Billed Amount": f"{billed_amt:.2f}",
                        "Task Code": task, "Description": desc,
                    })
    # time on a matter that is missing from the matters list
    for i in range(3):
        eid += 1
        entries.append({
            "Entry ID": f"TE-{eid}", "Date": f"03/{10 + i}/{YEAR}", "Client": "Unknown Client",
            "Project ID": "M-1099", "Invoice Number": "", "Timekeeper": "Kevin Park",
            "Professional Type": "Associate", "Hours": "1.5", "Billable": "Yes",
            "Billed Status": "Unbilled", "Billed Hours": "0", "Rate": "340.00",
            "Billed Amount": "0.00", "Task Code": "A104", "Description": "Conflict check research",
        })
    entries.sort(key=lambda r: (r["Date"][6:], r["Date"][:5], r["Entry ID"]))

    # ---------------------------------------------------------------- collections
    matter_info = {m[0]: m for m in MATTERS}
    pays = []
    pid = 9000

    def pay(mid: str, inv: str, when: date, fees: Decimal, expenses: Decimal, tax: Decimal = Decimal(0),
            ptype: str = "Check", status: str = "Cleared") -> None:
        nonlocal pid
        pid += 1
        m = matter_info.get(mid)
        pays.append({
            "Payment Date": when.strftime("%m/%d/%Y"), "Client Name": m[1] if m else "",
            "Project ID": mid, "Project Name": m[2] if m else "", "Invoice #": inv,
            "Payment ID": f"PMT-{pid}", "Payment Amount": f"{fees + expenses + tax:.2f}",
            "Applied to Fees": f"{fees:.2f}", "Applied to Expenses": f"{expenses:.2f}",
            "Applied to Tax": f"{tax:.2f}", "Payment Type": ptype, "Payment Status": status,
            "Responsible Professional": m[4] if m else "",
        })

    for (mid, q), inv in sorted(invoices.items()):
        fees = inv["fees"]
        expenses = money(fees * Decimal(rng.choice(["0.01", "0.02", "0.035", "0.05"])))
        tax = money(fees * Decimal("0.0")) if mid != "M-1009" else money(fees * Decimal("0.01"))
        when = inv["date"]
        ptype = rng.choice(["Check", "ACH", "Wire", "Credit Card"])
        if q == 0:
            pay(mid, inv["invoice"], when + timedelta(days=rng.randint(20, 45)), fees, expenses, tax,
                ptype)
        elif q == 1:
            first = money(fees * Decimal("0.6"))
            pay(mid, inv["invoice"], when + timedelta(days=25), first, expenses, tax, ptype)
            pay(mid, inv["invoice"], when + timedelta(days=60), fees - first, Decimal("0"),
                Decimal("0"), ptype)
        elif q == 2:
            part = money(fees * Decimal(rng.choice(["0.5", "0.75", "1.0"])))
            pay(mid, inv["invoice"], when + timedelta(days=rng.randint(25, 50)), part, expenses,
                tax, ptype)
        else:
            # Q4 invoice paid in January of the following year -> outside the period
            pay(mid, inv["invoice"], date(YEAR + 1, 1, 20), money(fees * Decimal("0.5")),
                Decimal("0"), Decimal("0"), ptype)
    # special rows
    pay("M-1001", "", date(YEAR, 2, 14), Decimal("0"), Decimal("0"), ptype="Trust Deposit")
    pays[-1]["Payment Amount"] = "25000.00"
    pays[-1]["Applied to Fees"] = "0.00"
    pay("M-1006", "INV-1006-1", date(YEAR, 6, 2), Decimal("4800.00"), Decimal("0"), ptype="Check",
        status="Void")
    pays.append(dict(pays[5]))  # duplicated export row
    pays.sort(key=lambda r: (r["Payment Date"][6:], r["Payment Date"][:5], r["Payment ID"]))

    # ---------------------------------------------------------------- write files
    def write(name: str, rows: list[dict]) -> None:
        with open(OUT / name, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    write("partners.csv", [{"Partner Name": n, "Managing Partner": "Yes" if mp else "No",
                            "Active": "Yes",
                            "Partner Since": f"{YEAR}-04-01" if n == "Catherine Doyle" else ""}
                           for _, n, _, mp in PARTNERS])
    write("timesolv_professionals.csv",
          [{"Professional ID": pid_, "Name": n, "Title": "Managing Partner" if mp else "Partner",
            "Type": "Partner", "Supervisor": "", "Status": "Active"} for pid_, n, _, mp in PARTNERS]
          + [{"Professional ID": s[0], "Name": s[1], "Title": s[2], "Type": s[3],
              "Supervisor": s[5], "Status": "Active"} for s in STAFF])
    write("timesolv_projects.csv", [{
        "Project ID": m[0], "Client Name": m[1], "Project Name": m[2],
        "Originating Attorney": m[3], "Responsible Professional": m[4],
        "Status": "Open", "Open Date": f"0{1 + i % 9}/15/{YEAR - 1 - i % 3}", "Close Date": "",
        "Practice Area": m[5], "Billing Arrangement": "Hourly",
    } for i, m in enumerate(MATTERS)])
    write("timesolv_time_entries.csv", entries)
    write("timesolv_payment_allocations.csv", pays)
    write("matter_originator_overrides.csv", [
        {"matter_id": "M-1003", "partner": "Margaret Chen", "share_pct": "60",
         "notes": "Split origination approved by committee"},
        {"matter_id": "M-1003", "partner": "Robert Alvarez", "share_pct": "40",
         "notes": "Split origination approved by committee"},
    ])
    write("associate_matter_mappings.csv", [
        {"timekeeper": "Daniel Smith", "matter_id": "M-1001", "partner": "David Okafor",
         "start_date": "", "end_date": "", "allocation_pct": "100",
         "notes": "Okafor's matter", "source": "Staffing memo"},
        {"timekeeper": "Daniel Smith", "matter_id": "M-1004", "partner": "Elena Petrova",
         "start_date": "", "end_date": "", "allocation_pct": "100",
         "notes": "Assigned to the matter by Petrova", "source": "Staffing memo"},
        {"timekeeper": "Rachel Jones", "matter_id": "M-1007", "partner": "Michael Brennan",
         "start_date": "", "end_date": "", "allocation_pct": "60",
         "notes": "Joint supervision", "source": "Committee decision"},
        {"timekeeper": "Rachel Jones", "matter_id": "M-1007", "partner": "Aisha Mohammed",
         "start_date": "", "end_date": "", "allocation_pct": "40",
         "notes": "Joint supervision", "source": "Committee decision"},
        {"timekeeper": "Kevin Park", "matter_id": "M-1005", "partner": "Priya Raman",
         "start_date": f"{YEAR}-01-01", "end_date": f"{YEAR}-06-30", "allocation_pct": "100",
         "notes": "First half", "source": "Staffing memo"},
        {"timekeeper": "Kevin Park", "matter_id": "M-1005", "partner": "Thomas Nakamura",
         "start_date": f"{YEAR}-07-01", "end_date": "", "allocation_pct": "100",
         "notes": "Supervision transferred mid-year", "source": "Staffing memo"},
        {"timekeeper": "Laura Martinez", "matter_id": "M-1008", "partner": "Thomas Nakamura",
         "start_date": "", "end_date": "", "allocation_pct": "100",
         "notes": "", "source": "Staffing memo"},
    ])
    write_expenses(write)
    print(f"Wrote {len(entries)} time entries and {len(pays)} payment rows to {OUT}")


def write_expenses(write) -> None:
    """Expense categories, partner splits and a year of expenses (accounting-style export)."""
    cats = [
        ("Associate salary - Daniel Smith", "Direct (one partner)", "", "Paid by Okafor"),
        ("Associate salary - Rachel Jones", "Fixed split", "", "Shared by Lindqvist and Brennan"),
        ("Associate salary - Kevin Park", "By associate hours", "Kevin Park",
         "Follows the partners Park actually worked for"),
        ("Associate salary - Laura Martinez", "Fixed split", "", "Raman 60 / Nakamura 40"),
        ("Associate salary - Owen Fletcher", "Direct (one partner)", "", "Paid by Petrova"),
        ("Support staff salaries", "Equal (active partners)", "", "Paralegals, reception, clerk"),
        ("Rent", "Equal (active partners)", "", ""),
        ("Internet & telephone", "Equal (active partners)", "", ""),
        ("Utilities", "Equal (active partners)", "", ""),
        ("Malpractice insurance", "Proportional to gross compensation", "", ""),
        ("Bar dues & CLE", "Fixed split", "", "Overridden per entry"),
    ]
    write("expense_categories.csv", [{"category": c, "rule": r, "associate": a, "notes": n}
                                     for c, r, a, n in cats])
    write("expense_category_splits.csv", [
        {"category": "Associate salary - Daniel Smith", "partner": "David Okafor",
         "share_pct": "100", "notes": ""},
        {"category": "Associate salary - Rachel Jones", "partner": "Sarah Lindqvist",
         "share_pct": "50", "notes": ""},
        {"category": "Associate salary - Rachel Jones", "partner": "Michael Brennan",
         "share_pct": "50", "notes": ""},
        {"category": "Associate salary - Laura Martinez", "partner": "Priya Raman",
         "share_pct": "60", "notes": ""},
        {"category": "Associate salary - Laura Martinez", "partner": "Thomas Nakamura",
         "share_pct": "40", "notes": ""},
        {"category": "Associate salary - Owen Fletcher", "partner": "Elena Petrova",
         "share_pct": "100", "notes": ""},
        {"category": "Bar dues & CLE", "partner": "Margaret Chen", "share_pct": "100",
         "notes": "Default; each entry is overridden to the partner concerned"},
    ])
    monthly = [
        ("Associate salary - Daniel Smith", "Payroll", 15416.67),
        ("Associate salary - Rachel Jones", "Payroll", 12500.00),
        ("Associate salary - Kevin Park", "Payroll", 11666.67),
        ("Associate salary - Laura Martinez", "Payroll", 11250.00),
        ("Associate salary - Owen Fletcher", "Payroll", 9166.67),
        ("Support staff salaries", "Payroll", 21250.00),
        ("Rent", "Harbor Plaza Realty", 38500.00),
        ("Internet & telephone", "MetroNet Business", 1480.00),
        ("Utilities", "City Power & Water", 2650.00),
    ]
    rows = []
    n = 0
    for month in range(1, 13):
        for cat, payee, amt in monthly:
            n += 1
            rows.append({"Transaction ID": f"GL-{YEAR}-{n:05d}", "Date": f"{month:02d}/28/{YEAR}",
                         "Account": cat, "Memo": f"{cat} - {month:02d}/{YEAR}",
                         "Amount": f"{amt:.2f}", "Payee": payee})
    for q, month in enumerate((1, 4, 7, 10), start=1):
        n += 1
        rows.append({"Transaction ID": f"GL-{YEAR}-{n:05d}", "Date": f"{month:02d}/15/{YEAR}",
                     "Account": "Malpractice insurance", "Memo": f"Professional liability Q{q}",
                     "Amount": "41250.00", "Payee": "Lawyers Mutual"})
    dues = [("Margaret Chen", 1850.00), ("David Okafor", 1450.00), ("Sarah Lindqvist", 1625.00)]
    for partner, amt in dues:
        n += 1
        rows.append({"Transaction ID": f"GL-{YEAR}-{n:05d}", "Date": f"02/10/{YEAR}",
                     "Account": "Bar dues & CLE", "Memo": f"Bar dues and CLE - {partner}",
                     "Amount": f"{amt:.2f}", "Payee": "State Bar"})
    write("partner_expenses.csv", rows)
    write("expense_overrides.csv", [
        {"expense_id": r["Transaction ID"], "partner": r["Memo"].split(" - ")[-1],
         "share_pct": "100", "reason": "Personal bar dues are borne by the partner concerned"}
        for r in rows if r["Account"] == "Bar dues & CLE"])


if __name__ == "__main__":
    main()
