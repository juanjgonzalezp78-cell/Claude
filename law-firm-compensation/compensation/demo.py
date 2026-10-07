"""Load the bundled demo data set through the real import pipeline."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .database import Database
from .imports import import_file
from .models import Policy

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"
DEMO_YEAR = 2025


def load_demo(db: Database, year: int = DEMO_YEAR, user: str = "demo") -> list[str]:
    """Reset ``year`` and load the sample firm. Returns a list of log messages.

    Files are imported with automatically suggested column mappings, exactly as
    a user would through the Imports page.
    """
    if db.is_finalized(year):
        raise PermissionError(f"{year} is finalized; reopen it before loading demo data.")
    db.clear_year_data(year, user)
    db.save_policy(Policy(year=year), user)
    messages: list[str] = []
    steps = [
        ("partners", "partners.csv"),
        ("professionals", "timesolv_professionals.csv"),
        ("matters", "timesolv_projects.csv"),
        ("time_entries", "timesolv_time_entries.csv"),
        ("collections", "timesolv_payment_allocations.csv"),
    ]
    for dataset, filename in steps:
        res = import_file(db, year, dataset, str(SAMPLE_DIR / filename), user, replace_roster=True)
        messages.append(f"{filename}: {res.imported} imported, {res.updated} updated, "
                        f"{res.duplicates} duplicates skipped, {res.flagged_duplicates} flagged.")
    # Committee-approved split origination replaces the single imported originator.
    overrides = pd.read_csv(SAMPLE_DIR / "matter_originator_overrides.csv", dtype=str).fillna("")
    origs = db.load_table("originators", year)
    origs = origs[~origs["matter_id"].isin(set(overrides["matter_id"]))]
    db.save_table("originators", year, pd.concat([origs, overrides], ignore_index=True), user,
                  "Demo: split origination loaded")
    mappings = pd.read_csv(SAMPLE_DIR / "associate_matter_mappings.csv", dtype=str).fillna("")
    db.save_table("supervision", year, mappings, user, "Demo: associate-matter mappings loaded")
    messages.append("Split origination for M-1003 and associate-matter mappings loaded.")
    db.log(year, user, "Demo data loaded", messages)
    return messages
