# -*- coding: utf-8 -*-
"""Unavailability datastore utilities.

Storage format (CSV, UTF-8):
  doctor,date,shift,note,updated_at
where:
  - date is ISO YYYY-MM-DD
  - shift is one of: Mattina, Pomeriggio, Notte, Diurno, Tutto il giorno
"""

from __future__ import annotations

import csv
import io
import datetime as dt
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

VALID_SHIFTS = {"Mattina", "Pomeriggio", "Notte", "Diurno", "Tutto il giorno"}

def norm_shift(s: str) -> str:
    s0 = (s or "").strip()
    # allow some aliases
    low = s0.lower()
    if low in {"matt", "mattina"}:
        return "Mattina"
    if low in {"pom", "pomeriggio"}:
        return "Pomeriggio"
    if low in {"notte", "night"}:
        return "Notte"
    if low.startswith("diurn"):
        return "Diurno"
    if low.startswith("tutto"):
        return "Tutto il giorno"
    return s0

def parse_iso_date(s: str) -> dt.date:
    return dt.date.fromisoformat(str(s).strip()[:10])

def load_store(csv_text: str) -> List[Dict[str, str]]:
    if not csv_text.strip():
        return []
    f = io.StringIO(csv_text)
    rdr = csv.DictReader(f)
    out: List[Dict[str, str]] = []
    for row in rdr:
        if not row:
            continue
        doctor = (row.get("doctor") or "").strip()
        date = (row.get("date") or "").strip()
        shift = (row.get("shift") or "").strip()
        if not doctor or not date or not shift:
            continue
        out.append({
            "doctor": doctor,
            "date": date[:10],
            "shift": norm_shift(shift),
            "note": (row.get("note") or "").strip(),
            "updated_at": (row.get("updated_at") or "").strip(),
        })
    return out

def to_csv(rows: List[Dict[str, str]]) -> str:
    buf = io.StringIO()
    fieldnames = ["doctor", "date", "shift", "note", "updated_at"]
    wr = csv.DictWriter(buf, fieldnames=fieldnames)
    wr.writeheader()
    for r in rows:
        wr.writerow({
            "doctor": r.get("doctor",""),
            "date": r.get("date","")[:10],
            "shift": norm_shift(r.get("shift","")),
            "note": r.get("note",""),
            "updated_at": r.get("updated_at",""),
        })
    return buf.getvalue()

def filter_doctor_month(rows: List[Dict[str, str]], doctor: str, year: int, month: int) -> List[Dict[str, str]]:
    out=[]
    for r in rows:
        if (r.get("doctor") or "") != doctor:
            continue
        try:
            d = parse_iso_date(r.get("date",""))
        except Exception:
            continue
        if d.year==year and d.month==month:
            out.append(r)
    return out

def filter_month(rows: List[Dict[str, str]], year: int, month: int) -> List[Dict[str, str]]:
    out=[]
    for r in rows:
        try:
            d = parse_iso_date(r.get("date",""))
        except Exception:
            continue
        if d.year==year and d.month==month:
            out.append(r)
    return out

def replace_doctor_month(
    rows: List[Dict[str, str]],
    doctor: str,
    year: int,
    month: int,
    new_entries: Iterable[Tuple[dt.date, str, str]],
    updated_at: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Replace all entries for doctor+month with new_entries."""
    updated_at = updated_at or dt.datetime.now(dt.timezone.utc).isoformat()
    doctor = doctor.strip()
    kept=[]
    for r in rows:
        if (r.get("doctor") or "") != doctor:
            kept.append(r); continue
        try:
            d = parse_iso_date(r.get("date",""))
        except Exception:
            continue
        if d.year==year and d.month==month:
            continue  # drop
        kept.append(r)

    for d, shift, note in new_entries:
        if not isinstance(d, dt.date):
            continue
        sh = norm_shift(shift)
        if sh not in VALID_SHIFTS:
            continue
        kept.append({
            "doctor": doctor,
            "date": d.isoformat(),
            "shift": sh,
            "note": (note or "").strip(),
            "updated_at": updated_at,
        })

    # de-duplicate by (doctor,date,shift) keep latest
    dedup: Dict[Tuple[str,str,str], Dict[str,str]] = {}
    for r in kept:
        k=(r.get("doctor",""), r.get("date","")[:10], norm_shift(r.get("shift","")))
        prev=dedup.get(k)
        if not prev:
            dedup[k]=r
        else:
            # choose lexicographically larger updated_at as "newer" if iso format
            if (r.get("updated_at","") or "") >= (prev.get("updated_at","") or ""):
                dedup[k]=r
    return list(dedup.values())
