\
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Turni Autogenerator – UTIC/Cardiologia
- Legge un template Excel (openpyxl)
- Legge regole YAML
- Legge indisponibilità mensili (Excel/CSV) con colonne: Medico, Data, Fascia (Mattina/Pomeriggio/Notte/Diurno/Tutto il giorno)
- Compila le colonne operative e salva un nuovo file Excel
Solver principale: OR-Tools CP-SAT (pip install ortools)
Fallback: greedy + report conflitti (meno robusto)
Autore: prototype by ChatGPT (GPT-5.2 Thinking)
"""
from __future__ import annotations
import argparse
import dataclasses
import calendar
import datetime as dt
import re
import sys
from collections import defaultdict, Counter
from copy import deepcopy, copy as _copy
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Iterable
import pandas as pd
import yaml
import openpyxl
from openpyxl.utils import get_column_letter
# -------------------------
# Utilities
# -------------------------
DOW_MAP = {
    0: "Mon",
    1: "Tue",
    2: "Wed",
    3: "Thu",
    4: "Fri",
    5: "Sat",
    6: "Sun",
}

# Optional style template used to make auto-generated monthly templates look
# like the official hospital model (header, column widths, weekend/holiday
# shading, etc.).
#
# Place a file named `Style_Template.xlsx` in the repo root (same folder as
# this script) to enable styling.
STYLE_TEMPLATE_FILENAME = "Style_Template.xlsx"
STYLE_TEMPLATE_FALLBACK = "Modello_Febbraio_2026.xlsx"  # backward-compat if you keep the old name


def _find_style_template() -> Optional[Path]:
    base = Path(__file__).resolve().parent
    p1 = base / STYLE_TEMPLATE_FILENAME
    if p1.exists():
        return p1
    p2 = base / STYLE_TEMPLATE_FALLBACK
    if p2.exists():
        return p2
    return None


def _load_style_ws() -> Optional[openpyxl.worksheet.worksheet.Worksheet]:
    """Load the first worksheet from the style template, if present."""
    p = _find_style_template()
    if not p:
        return None
    try:
        wb = openpyxl.load_workbook(p)
        return wb[wb.sheetnames[0]]
    except Exception:
        return None


def _easter_date_gregorian(year: int) -> dt.date:
    """Compute Easter Sunday (Gregorian calendar) using the Meeus/Jones/Butcher algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def italy_public_holidays(year: int) -> Set[dt.date]:
    """Italian national public holidays (incl. Easter Monday)."""
    easter = _easter_date_gregorian(year)
    easter_monday = easter + dt.timedelta(days=1)
    fixed = {
        dt.date(year, 1, 1),   # Capodanno
        dt.date(year, 1, 6),   # Epifania
        dt.date(year, 4, 25),  # Liberazione
        dt.date(year, 5, 1),   # Lavoro
        dt.date(year, 6, 2),   # Repubblica
        dt.date(year, 8, 15),  # Ferragosto
        dt.date(year, 11, 1),  # Ognissanti
        dt.date(year, 12, 8),  # Immacolata
        dt.date(year, 12, 25), # Natale
        dt.date(year, 12, 26), # Santo Stefano
    }
    return fixed | {easter_monday}


def _is_grey_solid(cell) -> bool:
    """Return True if cell has the same grey solid fill used by the model."""
    try:
        fill = cell.fill
        if not fill or fill.patternType != "solid":
            return False
        fg = getattr(fill.fgColor, "rgb", None)
        return fg == "FFC0C0C0"
    except Exception:
        return False


def _copy_cell_style(src, dst) -> None:
    """Copy style elements from src cell to dst cell (without copying value)."""
    try:
        # IMPORTANT: openpyxl style objects do not play well with deepcopy
        # (it can recurse). Use shallow copies or direct assignment.
        dst.font = _copy(src.font)
        dst.fill = _copy(src.fill)
        dst.border = _copy(src.border)
        dst.alignment = _copy(src.alignment)
        dst.number_format = src.number_format
        dst.protection = _copy(src.protection)
    except Exception:
        # best-effort: ignore style copy failures
        pass


def _apply_model_style_to_template(ws, cfg: dict, year: int, month: int, last_day: int) -> None:
    """Apply header/column widths and weekend/holiday shading using Style_Template.xlsx if available."""
    style_ws = _load_style_ws()
    if style_ws is None:
        return

    # Identify representative rows in the style worksheet
    # - sunday_row: first row where column B contains 'domenica' (Italian)
    # - weekday_row: first data row that is not sunday_row
    sunday_row = None
    weekday_row = None
    for r in range(2, min(style_ws.max_row, 20) + 1):
        b = style_ws.cell(r, 2).value
        if isinstance(b, str) and b.strip().lower().startswith("domen"):
            sunday_row = r
            break
    # fallback: assume row 2 is sunday in the provided model
    if sunday_row is None:
        sunday_row = 2
    # weekday row
    for r in range(2, min(style_ws.max_row, 20) + 1):
        if r == sunday_row:
            continue
        a = style_ws.cell(r, 1).value
        if isinstance(a, (dt.date, dt.datetime)):
            weekday_row = r
            break
    if weekday_row is None:
        weekday_row = min(3, style_ws.max_row)

    # Column widths + header styles/labels
    max_col = style_ws.max_column
    # Ensure we have at least up to max_col in row 1
    _ = ws.cell(row=1, column=max_col)

    # Row heights
    if style_ws.row_dimensions[1].height:
        ws.row_dimensions[1].height = style_ws.row_dimensions[1].height
    if style_ws.row_dimensions[weekday_row].height:
        data_h = style_ws.row_dimensions[weekday_row].height
        for r in range(2, last_day + 2):
            ws.row_dimensions[r].height = data_h

    # Copy column widths and header style/value
    for c in range(1, max_col + 1):
        letter = get_column_letter(c)
        w = style_ws.column_dimensions[letter].width
        if w:
            ws.column_dimensions[letter].width = w

        src_h = style_ws.cell(1, c)
        dst_h = ws.cell(1, c)
        _copy_cell_style(src_h, dst_h)
        # Fill missing header labels from model (important for empty spacer columns)
        if (dst_h.value is None or str(dst_h.value).strip() == "") and (src_h.value is not None):
            dst_h.value = src_h.value

    # Determine which columns are shaded in the model on Sundays/holidays
    grey_cols = {c for c in range(1, max_col + 1) if _is_grey_solid(style_ws.cell(sunday_row, c))}

    # Holiday set for styling
    extra = set()
    for x in cfg.get("festivi_extra", []) or []:
        try:
            extra.add(parse_date(x))
        except Exception:
            pass
    holidays = italy_public_holidays(int(year)) | extra

    # Apply per-cell styles for the month (lightweight: <= 31 rows * ~31 cols)
    for r in range(2, last_day + 2):
        d = ws.cell(r, 1).value
        if isinstance(d, dt.datetime):
            d = d.date()
        if not isinstance(d, dt.date):
            continue
        is_holiday = (d.weekday() == 6) or (d in holidays)
        for c in range(1, max_col + 1):
            dst = ws.cell(r, c)
            # Choose style row based on holiday shading columns
            if is_holiday and (c in grey_cols):
                src = style_ws.cell(sunday_row, c)
            else:
                src = style_ws.cell(weekday_row, c)
            _copy_cell_style(src, dst)

SHIFT_NORMALIZE = {
    "mattina": "Mattina",
    "pom": "Pomeriggio",
    "pomeriggio": "Pomeriggio",
    "notte": "Notte",
}
def parse_date(x) -> dt.date:
    """Parse a date from Excel/str/datetime."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        raise ValueError("Empty date")
    if isinstance(x, dt.date) and not isinstance(x, dt.datetime):
        return x
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, (int, float)):
        # Excel serial date support.
        # We assume the default Excel 1900 date system.
        # NOTE: Excel's epoch has the 1900-leap-year bug; using 1899-12-30
        # as origin reproduces Excel serials correctly for modern dates.
        try:
            n = int(x)
        except Exception:
            raise ValueError(f"Numeric date not supported: {x}")
        origin = dt.date(1899, 12, 30)
        return origin + dt.timedelta(days=n)
    s = str(x).strip()
    # Accept dd/mm/yyyy or yyyy-mm-dd
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognized date format: {x}")


# -------------------------
# Carryover (cross-month) helpers
# -------------------------

def _detect_date_column(ws, max_scan_rows: int = 40) -> int:
    """Best-effort: detect the column containing date values (usually column A)."""
    # Scan first few columns for date-like values in rows 2..max_scan_rows
    for c in range(1, min(ws.max_column, 6) + 1):
        hits = 0
        for r in range(2, min(ws.max_row, max_scan_rows) + 1):
            v = ws.cell(r, c).value
            if isinstance(v, dt.datetime):
                hits += 1
            elif isinstance(v, dt.date):
                hits += 1
            else:
                # also accept iso-like strings
                if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v.strip()):
                    hits += 1
        if hits >= 5:
            return c
    # Fallback to col 1
    return 1


def _find_column_by_header(ws, header_name: str) -> int | None:
    """Find 1-based column index whose header (row 1) matches header_name (case-insensitive)."""
    if not header_name:
        return None
    target = str(header_name).strip().lower()
    for c in range(1, ws.max_column + 1):
        h = ws.cell(1, c).value
        if h is None:
            continue
        if str(h).strip().lower() == target:
            return c
    return None


def extract_carryover_from_output_xlsx(
    prev_xlsx: "Path | str",
    rules_yml: "Path | str",
    sheet_name: str | None = None,
) -> dict:
    """Extract cross-month constraints from a previously generated output Excel.

    Returns a dict with keys:
      - recent_nights: list of (date, doctor) tuples for the last (min_gap-1) days
      - note: human-readable summary

    This is used to enforce night-spacing and night-off-next-day across month boundaries.
    """
    prev_xlsx = Path(prev_xlsx)
    cfg = load_rules(Path(rules_yml))
    gc = cfg.get('global_constraints', {}) or {}
    min_gap = int(gc.get('night_spacing_days_min', 5) or 5)
    # Use label from cfg.columns.J when available; fallback to 'J'/'Notte'
    cols_map = cfg.get('columns', {}) or {}
    night_label = cols_map.get('J') or 'J'

    wb = openpyxl.load_workbook(prev_xlsx, data_only=True)
    ws = wb[sheet_name] if (sheet_name and sheet_name in wb.sheetnames) else wb[wb.sheetnames[0]]

    date_col = _detect_date_column(ws)
    colJ = _find_column_by_header(ws, night_label)
    if colJ is None:
        # try common fallbacks
        for cand in ['J', 'Notte', 'NOTTE', 'Night']:
            colJ = _find_column_by_header(ws, cand)
            if colJ is not None:
                break

    if colJ is None:
        return {
            'recent_nights': [],
            'note': f"Impossibile trovare la colonna Notte (J) nel file precedente: header atteso '{night_label}'.",
        }

    # Collect rows with valid dates
    rows = []
    for r in range(2, ws.max_row + 1):
        v = ws.cell(r, date_col).value
        if v is None:
            continue
        try:
            d = parse_date(v)
        except Exception:
            continue
        rows.append((d, r))

    if not rows:
        return {'recent_nights': [], 'note': 'Nessuna data leggibile nel file precedente.'}

    rows.sort(key=lambda t: t[0])
    last_date, last_row = rows[-1]
    start_window = last_date - dt.timedelta(days=(min_gap - 1))

    recent_nights = []
    for d, r in rows:
        if d < start_window:
            continue
        doc = ws.cell(r, colJ).value
        doc = norm_name(doc) if doc else None
        if doc and doc.lower() != 'recupero':
            recent_nights.append((d, doc))

    night_off_next = bool((gc.get('night_off') or {}).get('next_day', True))
    last_night_doc = ws.cell(last_row, colJ).value
    last_night_doc = norm_name(last_night_doc) if last_night_doc else None
    block_all_on_first_day = []
    if night_off_next and last_night_doc and last_night_doc.lower() != 'recupero':
        block_all_on_first_day = [last_night_doc]

    blocked_txt = ", ".join(block_all_on_first_day) if block_all_on_first_day else "-"
    note = (
        f"Carryover dal mese precedente: ultima data {last_date.isoformat()}, "
        f"notti negli ultimi {min_gap-1} giorni: {len(recent_nights)}, "
        f"blocco consigliato 1° giorno (notte ultimo giorno): {blocked_txt}"
    )
    return {
        'recent_nights': recent_nights,
        'block_all_on_first_day': block_all_on_first_day,
        'last_date': last_date.isoformat(),
        'last_night_doctor': last_night_doc,
        'note': note,
    }


def build_carryover_from_assignment(
    cfg: dict,
    prev_days: List['DayRow'],
    prev_slots: List['Slot'],
    prev_assignment: Dict[str, Optional[str]],
    next_first_day: dt.date,
) -> dict:
    """Build carryover context from a solved previous month (when template includes consecutive months)."""
    gc = cfg.get('global_constraints', {}) or {}
    min_gap = int(gc.get('night_spacing_days_min', 5) or 5)
    night_off_next = bool((gc.get('night_off') or {}).get('next_day', True))

    prev_last_day = max(d.date for d in prev_days)
    start_window = next_first_day - dt.timedelta(days=(min_gap - 1))

    recent_nights = []
    last_night_doc = None

    for s in prev_slots:
        if s.columns != ['J']:
            continue
        d = s.day.date
        if d < start_window:
            continue
        doc = prev_assignment.get(s.slot_id)
        if doc:
            doc = norm_name(doc)
            if doc.lower() != 'recupero':
                recent_nights.append((d, doc))
        if d == prev_last_day:
            last_night_doc = norm_name(doc) if doc else None

    # If the last day had a night, enforce next-day off across boundary by seeding that night.
    # The OR-Tools integration will interpret this list accordingly.
    carry = {'recent_nights': recent_nights}
    if night_off_next and last_night_doc:
        carry['block_all_on_first_day'] = [last_night_doc]
    return carry
def norm_name(x: str) -> str:
    return re.sub(r"\s+", " ", str(x).strip())
def norm_shift(x: str) -> str:
    s = str(x).strip().lower()
    if s in SHIFT_NORMALIZE:
        return SHIFT_NORMALIZE[s]
    # allow first letter
    if s.startswith("m"):
        return "Mattina"
    if s.startswith("p"):
        return "Pomeriggio"
    if s.startswith("n"):
        return "Notte"
    raise ValueError(f"Unrecognized shift: {x}")
def shifts_from_fascia(x: str) -> Set[str]:
    """Map unavailability 'Fascia' value to one or more internal shifts.
    Accepted:
      - Mattina / Pomeriggio / Notte  -> that single shift
      - Diurno (or Giorno)            -> {'Mattina','Pomeriggio'}
      - Tutto il giorno / All day     -> {'Any'} (treated as full-day)
    """
    s = str(x).strip().lower()
    # Full-day first (because it contains 'giorno')
    if any(k in s for k in ["tutto", "intera", "completa", "allday", "all day", "full day", "24h", "24 h"]):
        return {"Any"}
    # Daytime (morning + afternoon, but still allows night)
    if s in ["diurno", "giorno", "daytime", "day"] or s.startswith("diurn"):
        return {"Mattina", "Pomeriggio"}
    # Default single-shift
    return {norm_shift(x)}
def dayspec_contains(dow: str, spec) -> bool:
    """
    spec can be:
    - string: 'Mon-Sat', 'Mon-Fri', 'Wed'
    - list of strings: ['Mon','Tue']
    - None: means all days
    """
    if spec is None:
        return True
    if isinstance(spec, str):
        spec = spec.strip()
        if "-" in spec:
            a, b = spec.split("-", 1)
            a, b = a.strip(), b.strip()
            order = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
            ia, ib = order.index(a), order.index(b)
            if ia <= ib:
                return order.index(dow) >= ia and order.index(dow) <= ib
            # wrap (rare)
            return order.index(dow) >= ia or order.index(dow) <= ib
        return dow == spec
    if isinstance(spec, list):
        return dow in spec
    raise TypeError(f"Invalid days spec: {spec}")
# -------------------------
# Data structures
# -------------------------
@dataclasses.dataclass(frozen=True)
class DayRow:
    date: dt.date
    dow: str  # 'Mon'...'Sun'
    row_idx: int
@dataclasses.dataclass
class Slot:
    """One assignable decision for a day."""
    day: DayRow
    slot_id: str                 # unique id
    columns: List[str]           # Excel column letters to fill with same doctor
    allowed: List[str]           # allowed doctor names (domain)
    required: bool = True        # if False, can be blank
    blank_penalty: int = 0      # penalty if left blank (only if required=False)
    shift: str = "Mattina"       # Mattina / Pomeriggio / Notte / Any
    rule_tag: str = ""           # for reporting
# -------------------------
# Load config / template
# -------------------------
def load_rules(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError("Rules YAML must be a mapping.")
    return cfg


def create_month_template_xlsx(
    rules_yml: "Path | str",
    year: int,
    month: int,
    out_path: "Path | str",
    sheet_name: Optional[str] = None,
) -> Path:
    """Create a minimal Excel template for the given month.

    The generated template is compatible with this generator:
    - Column A (from row 2): dates
    - Optional headers in row 1
    - Creates the worksheet specified by `sheet_name` (or a default)
    - Writes headers for columns declared in the YAML `columns:` mapping
    - Ensures `keep_empty_columns:` exist (as blank headers) for layout

    Parameters
    ----------
    rules_yml: YAML rules file path
    year, month: target year/month
    out_path: output .xlsx path
    sheet_name: worksheet name to create

    Returns
    -------
    Path to the created template.
    """
    rules_path = Path(rules_yml)
    outp = Path(out_path)
    cfg = load_rules(rules_path)

    # Determine columns from YAML
    cols_map = cfg.get("columns") or {}
    if not isinstance(cols_map, dict):
        cols_map = {}
    keep_empty = cfg.get("keep_empty_columns") or []
    if not isinstance(keep_empty, list):
        keep_empty = []

    # Create workbook
    wb = openpyxl.Workbook()
    ws = wb.active

    if sheet_name:
        ws.title = str(sheet_name)
    else:
        ws.title = f"GUARDIE_{year}_{month:02d}"

    # Header cells (kept blank like the official model)
    _ = ws["A1"]
    _ = ws["B1"]

    # Column headers from YAML mapping (letters -> labels)
    for col_letter, label in cols_map.items():
        col_letter = str(col_letter).strip().upper()
        if not col_letter:
            continue
        ws[f"{col_letter}1"] = str(label) if label is not None else ""

    # Keep empty spacer columns
    for col_letter in keep_empty:
        col_letter = str(col_letter).strip().upper()
        if not col_letter:
            continue
        # Ensure the cell exists (leave blank)
        _ = ws[f"{col_letter}1"]

    # Fill dates
    last_day = calendar.monthrange(int(year), int(month))[1]
    r = 2
    for day in range(1, last_day + 1):
        d = dt.date(int(year), int(month), int(day))
        ws.cell(row=r, column=1).value = d
        ws.cell(row=r, column=1).number_format = "dd/mm/yyyy"
        # Italian day labels in the Excel view (logic uses DOW_MAP internally)
        ws.cell(row=r, column=2).value = ["lunedi","martedi","mercoledi","giovedi","venerdi","sabato","domenica"][d.weekday()]
        r += 1

    # Nice-to-have formatting
    try:
        ws.freeze_panes = "A2"
        ws.column_dimensions["A"].width = 12
        ws.column_dimensions["B"].width = 6
    except Exception:
        pass

    # Apply model styling (if Style_Template.xlsx is present)
    _apply_model_style_to_template(ws, cfg, int(year), int(month), int(last_day))

    outp.parent.mkdir(parents=True, exist_ok=True)
    wb.save(outp)
    return outp

def load_template_days(xlsx_path: Path, sheet_name: Optional[str]=None) -> Tuple[openpyxl.Workbook, openpyxl.worksheet.worksheet.Worksheet, List[DayRow]]:
    wb = openpyxl.load_workbook(xlsx_path)
    if sheet_name:
        if sheet_name not in wb.sheetnames:
            available = ", ".join(wb.sheetnames)
            raise KeyError(f"Worksheet '{sheet_name}' does not exist. Available: {available}")
        ws = wb[sheet_name]
    else:
        ws = wb.active
    # Find day rows: column A contains dates
    days: List[DayRow] = []
    for r in range(2, ws.max_row + 1):
        v = ws.cell(row=r, column=1).value
        if isinstance(v, (dt.datetime, dt.date)):
            d = v.date() if isinstance(v, dt.datetime) else v
            dow = DOW_MAP[d.weekday()]
            days.append(DayRow(date=d, dow=dow, row_idx=r))
    if not days:
        raise ValueError("No date rows found in column A (starting from row 2).")
    return wb, ws, days
def load_unavailability(unav_path: Optional[Path]) -> Dict[str, Dict[dt.date, Set[str]]]:
    """
    Returns: unav[doctor][date] = {'Mattina','Pomeriggio','Notte'} (oppure 'Any' per full-day)
    """
    unav: Dict[str, Dict[dt.date, Set[str]]] = defaultdict(lambda: defaultdict(set))
    if unav_path is None:
        return unav
    if not unav_path.exists():
        raise FileNotFoundError(unav_path)
    if unav_path.suffix.lower() in [".xlsx", ".xls"]:
        df = pd.read_excel(unav_path)
    elif unav_path.suffix.lower() in [".csv", ".tsv"]:
        sep = "\t" if unav_path.suffix.lower() == ".tsv" else ","
        df = pd.read_csv(unav_path, sep=sep)
    else:
        raise ValueError("Unavailability file must be .xlsx/.xls/.csv/.tsv")
    # Flexible column names
    cols = {c.lower().strip(): c for c in df.columns}
    def pick(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None
    c_med = pick("medico", "doctor", "name")
    c_dat = pick("data", "date", "giorno")
    c_fas = pick("fascia", "shift", "turno")
    if not (c_med and c_dat and c_fas):
        raise ValueError("Unavailability file must contain columns: Medico, Data, Fascia")
    for _, row in df.iterrows():
        med = row.get(c_med)
        dat = row.get(c_dat)
        fas = row.get(c_fas)
        if pd.isna(med) or pd.isna(dat) or pd.isna(fas):
            continue
        doctor = norm_name(med)
        date = parse_date(dat)
        for shift in shifts_from_fascia(fas):
            unav[doctor][date].add(shift)
    return unav
def collect_doctors(cfg: dict) -> List[str]:
    """
    Union of all pools/allowed in YAML, minus absolute_exclusions.
    Keeps special placeholder 'Recupero' if present.
    """
    doctors: Set[str] = set()
    # From unavailability section too
    for d in (cfg.get("unavailability") or {}).keys():
        doctors.add(norm_name(d))
    rules = cfg.get("rules", {})
    if isinstance(rules, dict):
        for _, rule in rules.items():
            if not isinstance(rule, dict):
                continue
            for k in ["allowed", "pool", "pool_other", "pool_mon_fri", "other_pool", "fallback_pool", "distribution_pool"]:
                if k in rule and isinstance(rule[k], list):
                    doctors |= {norm_name(x) for x in rule[k]}
            for k in ["fixed", "tuesday_fixed", "prefer"]:
                if k in rule and rule[k]:
                    doctors.add(norm_name(rule[k]))
    # Remove absolute exclusions
    abs_excl = {norm_name(x) for x in (cfg.get("absolute_exclusions") or [])}
    doctors = {d for d in doctors if d not in abs_excl}
    # Stable sorting: keep 'Recupero' last-ish
    doctors_list = sorted(doctors, key=lambda s: (s == "Recupero", s.lower()))
    return doctors_list
# -------------------------
# Build slots from rules
# -------------------------
def is_festivo(day: DayRow, cfg: dict) -> bool:
    extra = set()
    for x in cfg.get("festivi_extra", []) or []:
        try:
            extra.add(parse_date(x))
        except Exception:
            pass
    # Treat Sunday and Italian national holidays as "festivo".
    # Extra holidays can be provided via cfg.festivi_extra.
    hol = italy_public_holidays(int(day.date.year))
    return day.dow == "Sun" or day.date in hol or day.date in extra
def apply_unavailability(allowed: List[str], day: DayRow, shift: str, unav: Dict[str, Dict[dt.date, Set[str]]]) -> List[str]:
    out = []
    for doc in allowed:
        d_unav = unav.get(doc, {}).get(day.date, set())
        # If any shift marked 'Any' treat as full-day
        if "Any" in d_unav:
            continue
        if shift == "Any":
            if d_unav:
                continue
        elif shift in d_unav:
            continue
        out.append(doc)
    return out
def slots_for_month(cfg: dict, days: List[DayRow], unav: Dict[str, Dict[dt.date, Set[str]]]) -> List[Slot]:
    """
    Converts YAML column rules into per-day slots.
    Handles exception days (festivi) by merging D+E and H+I, and merging E+G always.
    """
    rules = cfg.get("rules", {})
    if not isinstance(rules, dict):
        raise ValueError("cfg.rules must be a mapping.")
    doctors_all = collect_doctors(cfg)
    doctors_set = set(doctors_all)
    # Relief valves (optional): allow specific columns to be left blank with penalties (used only if needed).
    gc = cfg.get("global_constraints") or {}
    relief = gc.get("relief_valves") or {}
    blank_penalties: Dict[str, int] = {}
    if isinstance(relief.get("allow_blank_columns"), dict):
        for _k, _v in (relief.get("allow_blank_columns") or {}).items():
            try:
                blank_penalties[str(_k).strip().upper()] = int(_v)
            except Exception:
                pass
    def req_and_blank(col_letter: str) -> Tuple[bool, int]:
        col_letter = str(col_letter).strip().upper()
        if col_letter in blank_penalties:
            return False, blank_penalties[col_letter]
        return True, 0
    # YAML also has inline date-only unavailability (full-day)
    for doc, dates in (cfg.get("unavailability") or {}).items():
        for ds in dates or []:
            try:
                unav[norm_name(doc)][parse_date(ds)].add("Any")
            except Exception:
                pass
    slots: List[Slot] = []
    def mk_allowed(pool: List[str]) -> List[str]:
        # keep only known doctors + keep 'Recupero' if used
        out = [norm_name(x) for x in pool if norm_name(x) in doctors_set]
        return out
    for day in days:
        festivo = is_festivo(day, cfg)
        # Optional: on Saturdays, assign the SAME doctor to K and T (single combined slot K+T)
        gc = cfg.get("global_constraints", {}) or {}
        merge_KT_sat = bool(gc.get("saturday_K_equals_T", False)) and (day.dow == "Sat") and (not festivo)             and ("K" in rules) and ("T" in rules) and dayspec_contains(day.dow, (rules.get("T") or {}).get("days"))
        # ---- C: Reperibilità (daily)
        if "C_reperibilita" in rules:
            r = rules["C_reperibilita"]
            excluded = {norm_name(x) for x in (r.get("excluded") or [])}
            pool = [d for d in doctors_all if d not in excluded and d != "Recupero"]  # usually a real doctor
            pool = apply_unavailability(pool, day, "Any", unav)
            slots.append(Slot(day, f"{day.date}-C", ["C"], pool, required=True, shift="Any", rule_tag="C_reperibilita"))
        # ---- Morning D/F and E/G and Afternoon H/I depend on festivo
        if festivo:
            # DE unified (D+E) – required
            # IMPORTANT: per le domeniche/festivi NON usare le regole D/F (Grimaldi/Calabrò)
            # ma un pool dedicato (se disponibile) coerente con la sezione "Domeniche e festivi".
            rFest = rules.get("Festivi", {}) if isinstance(rules.get("Festivi", {}), dict) else {}
            fest_excl = {norm_name(x) for x in (rFest.get("excluded") or [])}
            fest_pool_m = rFest.get("pool_mattina") or rFest.get("pool") or []
            allowed_de = mk_allowed(fest_pool_m)
            if not allowed_de:
                # fallback conservativo: tutti tranne Recupero
                allowed_de = [d for d in doctors_all if d != "Recupero" and d not in fest_excl]
            else:
                allowed_de = [d for d in allowed_de if d not in fest_excl]
            allowed_de = apply_unavailability(allowed_de, day, "Mattina", unav)
            slots.append(Slot(day, f"{day.date}-DE", ["D","E"], allowed_de, required=True, shift="Mattina", rule_tag="Festivo_DE"))
            # HI unified (H+I) – required
            fest_pool_p = rFest.get("pool_pomeriggio") or rFest.get("pool") or []
            allowed_hi = mk_allowed(fest_pool_p)
            if not allowed_hi:
                allowed_hi = [d for d in doctors_all if d != "Recupero" and d not in fest_excl]
            else:
                allowed_hi = [d for d in allowed_hi if d not in fest_excl]
            allowed_hi = apply_unavailability(allowed_hi, day, "Pomeriggio", unav)
            slots.append(Slot(day, f"{day.date}-HI", ["H","I"], allowed_hi, required=True, shift="Pomeriggio", rule_tag="Festivo_HI"))
        else:
            # D / F (Mon-Sat)
            if "D_F" in rules and dayspec_contains(day.dow, rules["D_F"].get("days")):
                r = rules["D_F"]
                allowed_d = mk_allowed(r.get("allowed") or [])
                allowed_d = apply_unavailability(allowed_d, day, "Mattina", unav)
                # F: prefer the other of allowed, fallback to any free doctor if requested
                fallback = bool(r.get("fallback_F_any_free_doctor", False))
                allowed_f = mk_allowed(r.get("allowed") or [])
                if fallback:
                    allowed_f = sorted({*allowed_f, *[d for d in doctors_all if d != "Recupero"]})
                allowed_f = apply_unavailability(allowed_f, day, "Mattina", unav)
                slots.append(Slot(day, f"{day.date}-D", ["D"], allowed_d, required=True, shift="Mattina", rule_tag="D_F.D"))
                slots.append(Slot(day, f"{day.date}-F", ["F"], allowed_f, required=True, shift="Mattina", rule_tag="D_F.F"))
            # EG paired (Mon-Sat)
            if "E_G" in rules and dayspec_contains(day.dow, rules["E_G"].get("days")):
                r = rules["E_G"]
                allowed = mk_allowed(r.get("allowed") or [])
                allowed = apply_unavailability(allowed, day, "Mattina", unav)
                slots.append(Slot(day, f"{day.date}-EG", ["E","G"], allowed, required=True, shift="Mattina", rule_tag="E_G"))
            # H (Mon-Sat) + I (Mon-Sat)
            if "H" in rules:
                rH = rules["H"]
                # Saturdays must be covered; for Mon-Fri, use pool_mon_fri (+ special)
                if day.dow == "Sat" or dayspec_contains(day.dow, "Mon-Fri"):
                    allowed = mk_allowed(rH.get("pool_mon_fri") or [])
                    for special in ["Grimaldi","Calabrò"]:
                        if special in doctors_set and special not in allowed:
                            allowed.append(special)
                    allowed = apply_unavailability(allowed, day, "Pomeriggio", unav)
                    slots.append(Slot(day, f"{day.date}-H", ["H"], allowed, required=True, shift="Pomeriggio", rule_tag="H"))
            if "I" in rules and dayspec_contains(day.dow, "Mon-Sat"):
                rI = rules["I"]
                allowed = mk_allowed(rI.get("distribution_pool") or [])
                allowed = apply_unavailability(allowed, day, "Pomeriggio", unav)
                # In practice I is an afternoon activity; required Mon-Sat
                slots.append(Slot(day, f"{day.date}-I", ["I"], allowed, required=True, shift="Pomeriggio", rule_tag="I"))
        # ---- Night J (daily except Thu if configured)
        if "J" in rules:
            rJ = rules["J"]
            if not (rJ.get("thursday_blank", False) and day.dow == "Thu"):
                allowed = mk_allowed(rJ.get("pool_other") or [])
                # add quota doctors even if not in pool_other
                for special in (rJ.get("monthly_quotas") or {}).keys():
                    if special in doctors_set and special not in allowed:
                        allowed.append(special)
                allowed = [d for d in allowed if d != "Recupero"]
                allowed = apply_unavailability(allowed, day, "Notte", unav)
                slots.append(Slot(day, f"{day.date}-J", ["J"], allowed, required=True, shift="Notte", rule_tag="J"))
        # ---- K Letto (daily, but blank on Sundays/festivi)
        # If merge_KT_sat is enabled, we create a single combined slot K+T on Saturday.
        if merge_KT_sat:
            rK = rules.get("K", {}) if isinstance(rules.get("K", {}), dict) else {}
            rT = rules.get("T", {}) if isinstance(rules.get("T", {}), dict) else {}
            allowed_k = mk_allowed(rK.get("pool") or [])
            allowed_t = mk_allowed(rT.get("pool") or [])
            allowed_k = apply_unavailability(allowed_k, day, "Mattina", unav)
            allowed_t = apply_unavailability(allowed_t, day, "Mattina", unav)
            inter = [d for d in allowed_k if d in set(allowed_t)]
            allowed = inter if inter else sorted({*allowed_k, *allowed_t})
            slots.append(Slot(day, f"{day.date}-KT", ["K","T"], allowed, required=True, shift="Mattina", rule_tag="K_T_SAT"))
        elif "K" in rules and not festivo:
            rK = rules["K"]
            allowed = mk_allowed(rK.get("pool") or [])
            allowed = apply_unavailability(allowed, day, "Mattina", unav)
            slots.append(Slot(day, f"{day.date}-K", ["K"], allowed, required=True, shift="Mattina", rule_tag="K"))
        # ---- L Padiglioni (Mon-Wed)
        if "L" in rules:
            rL = rules["L"]
            if dayspec_contains(day.dow, rL.get("days")):
                pool = mk_allowed(rL.get("pool_other") or [])
                # allow Recupero as placeholder
                if "Recupero" in doctors_set and "Recupero" not in pool:
                    pool.append("Recupero")
                pool = apply_unavailability(pool, day, "Mattina", unav)
                req, bp = req_and_blank("L")
                slots.append(Slot(day, f"{day.date}-L", ["L"], pool, required=req, blank_penalty=bp, shift="Mattina", rule_tag="L"))
        # ---- Q Eco base (Mon-Sat)
        if "Q" in rules:
            r = rules["Q"]
            if dayspec_contains(day.dow, r.get("days")):
                pool = mk_allowed(r.get("pool") or [])
                pool = apply_unavailability(pool, day, "Mattina", unav)
                slots.append(Slot(day, f"{day.date}-Q", ["Q"], pool, required=True, shift="Mattina", rule_tag="Q"))
        # ---- R (Mon-Fri)
        if "R" in rules:
            r = rules["R"]
            if dayspec_contains(day.dow, r.get("days")):
                pool = mk_allowed(r.get("pool") or [])
                pool = apply_unavailability(pool, day, "Mattina", unav)
                req, bp = req_and_blank("R")
                slots.append(Slot(day, f"{day.date}-R", ["R"], pool, required=req, blank_penalty=bp, shift="Mattina", rule_tag="R"))
        # ---- S (Wed, optional if can be absorbed in R)
        if "S" in rules:
            r = rules["S"]
            if dayspec_contains(day.dow, r.get("day")):
                pool = mk_allowed(r.get("pool") or [])
                pool = apply_unavailability(pool, day, "Mattina", unav)
                required = not bool(r.get("if_not_dedicated_put_in_R", False))
                slots.append(Slot(day, f"{day.date}-S", ["S"], pool, required=required, shift="Mattina", rule_tag="S"))
        # ---- T Interni (Mon-Sat)
        if "T" in rules and not merge_KT_sat:
            r = rules["T"]
            if dayspec_contains(day.dow, r.get("days")):
                pool = mk_allowed(r.get("pool") or [])
                pool = apply_unavailability(pool, day, "Mattina", unav)
                slots.append(Slot(day, f"{day.date}-T", ["T"], pool, required=True, shift="Mattina", rule_tag="T"))
        # ---- U Contr.PM (Mon-Tue)
        if "U" in rules:
            r = rules["U"]
            if dayspec_contains(day.dow, r.get("days")):
                pool = mk_allowed(r.get("pool") or [])
                pool = apply_unavailability(pool, day, "Mattina", unav)
                slots.append(Slot(day, f"{day.date}-U", ["U"], pool, required=True, shift="Mattina", rule_tag="U"))
        # ---- V Sala PM (Wed,Fri)
        if "V" in rules:
            r = rules["V"]
            if dayspec_contains(day.dow, r.get("days")):
                pool = mk_allowed(r.get("pool") or [])
                pool = apply_unavailability(pool, day, "Mattina", unav)
                slots.append(Slot(day, f"{day.date}-V", ["V"], pool, required=True, shift="Mattina", rule_tag="V"))
        # ---- W Ergometria/CPET (Mon-Fri; Tue fixed)
        if "W" in rules:
            r = rules["W"]
            if day.dow in ["Mon","Tue","Wed","Thu","Fri"]:
                if day.dow == "Tue" and r.get("tuesday_fixed"):
                    fixed = norm_name(r["tuesday_fixed"])
                    pool = [fixed] if fixed in doctors_set else []
                else:
                    pool = mk_allowed(r.get("other_days_pool") or [])
                pool = apply_unavailability(pool, day, "Mattina", unav)
                required = True if (day.dow == "Wed" and r.get("wednesday_mandatory", False)) else True
                slots.append(Slot(day, f"{day.date}-W", ["W"], pool, required=required, shift="Mattina", rule_tag="W"))
        # ---- Y Amb specialistici (Mon only)
        # Requirement:
        #  - Every Monday: 1 doctor among other_pool
        #  - PLUS: on exactly 2 Mondays: also 'Recupero' (appended in the same cell)
        if "Y" in rules:
            r = rules["Y"]
            if dayspec_contains(day.dow, r.get("day")):
                # Main doctor (always required)
                pool_main = [d for d in mk_allowed(r.get("other_pool") or []) if d != "Recupero"]
                pool_main = apply_unavailability(pool_main, day, "Mattina", unav)
                slots.append(Slot(day, f"{day.date}-Y", ["Y"], pool_main, required=True, shift="Mattina", rule_tag="Y_MAIN"))
                # Optional second line (Recupero) on 2 Mondays/month
                if r.get("recupero_two_mondays_per_month", False) and "Recupero" in doctors_set:
                    pool_rec = apply_unavailability(["Recupero"], day, "Mattina", unav)
                    slots.append(Slot(day, f"{day.date}-Y_REC", ["Y"], pool_rec, required=False, shift="Mattina", rule_tag="Y_REC"))
        # ---- AB Holter/Brugada/FA (Thu)
        if "AB" in rules:
            r = rules["AB"]
            if r.get("weekly", False) and dayspec_contains(day.dow, r.get("fixed_day")):
                prefer = norm_name(r.get("prefer") or "")
                pool = []
                if prefer and prefer in doctors_set:
                    pool.append(prefer)
                pool += mk_allowed(r.get("fallback_pool") or [])
                # unique list preserving order
                seen=set(); pool=[x for x in pool if not (x in seen or seen.add(x))]
                pool = apply_unavailability(pool, day, "Mattina", unav)
                slots.append(Slot(day, f"{day.date}-AB", ["AB"], pool, required=True, shift="Mattina", rule_tag="AB"))
        # ---- AC Scintigrafia (Tue/Wed fixed)
        if "AC" in rules:
            r = rules["AC"]
            if dayspec_contains(day.dow, r.get("days")):
                fixed = norm_name(r.get("fixed") or "")
                pool = [fixed] if fixed in doctors_set else mk_allowed(r.get("pool") or [])
                pool = apply_unavailability(pool, day, "Mattina", unav)
                slots.append(Slot(day, f"{day.date}-AC", ["AC"], pool, required=True, shift="Mattina", rule_tag="AC"))
    # Validate domains
    for s in slots:
        if s.required and not s.allowed:
            raise ValueError(f"Slot {s.slot_id} has empty allowed domain (required). Check pools/unavailability.")
        if not s.allowed:
            # optional slot can be left blank
            pass
    return slots
# -------------------------
# Solver (OR-Tools CP-SAT)
# -------------------------
def _max_bipartite_matching(slots_day: List[Slot]) -> Tuple[int, Dict[str, str]]:
    """
    Simple DFS-based bipartite matching: Slot -> Doctor.
    Returns:
      matched_count, slot_to_doc (by slot_id)
    """
    # Ensure deterministic iteration
    slots_day = list(slots_day)
    doc_to_slot: Dict[str, Slot] = {}
    def try_assign(slot: Slot, seen: Set[str]) -> bool:
        for d in dict.fromkeys(slot.allowed):
            if d in seen:
                continue
            seen.add(d)
            if d not in doc_to_slot or try_assign(doc_to_slot[d], seen):
                doc_to_slot[d] = slot
                return True
        return False
    for slot in sorted(slots_day, key=lambda s: (len(s.allowed), s.slot_id)):
        try_assign(slot, set())
    matched_slot_ids = set(s.slot_id for s in doc_to_slot.values())
    slot_to_doc: Dict[str, str] = {}
    for d, s in doc_to_slot.items():
        slot_to_doc[s.slot_id] = d
    return len(matched_slot_ids), slot_to_doc
def diagnose_day_level(days: List[DayRow], slots: List[Slot]) -> List[Dict]:
    """
    Day-level feasibility diagnostics (ignores cross-day constraints such as night spacing).
    Useful to pinpoint single-day bottlenecks created by unavailability.
    """
    slots_by_day: Dict[dt.date, List[Slot]] = defaultdict(list)
    for s in slots:
        slots_by_day[s.day.date].append(s)
    report: List[Dict] = []
    for day in days:
        day_slots = slots_by_day.get(day.date, [])
        # consider only required slots (and also "penalized optional" slots) as "should be filled"
        must_fill = [s for s in day_slots if s.required or (getattr(s, "blank_penalty", 0) and int(getattr(s, "blank_penalty", 0)) > 0)]
        if not must_fill:
            continue
        matched, slot_to_doc = _max_bipartite_matching(must_fill)
        ok = (matched == len(must_fill))
        union_docs = sorted({d for s in must_fill for d in s.allowed})
        tight = sorted([(s.slot_id, s.columns, len(s.allowed)) for s in must_fill], key=lambda x: x[2])[:6]
        if not ok:
            # identify some unmatched slots
            matched_ids = set(slot_to_doc.keys())
            unmatched = []
            for s in sorted(must_fill, key=lambda s: (len(s.allowed), s.slot_id)):
                if s.slot_id not in matched_ids:
                    unmatched.append({"slot_id": s.slot_id, "columns": s.columns, "allowed_n": len(s.allowed), "allowed": s.allowed[:15]})
            report.append({
                "date": day.date.isoformat(),
                "dow": day.dow,
                "required_slots": len(must_fill),
                "union_doctors": len(union_docs),
                "tightest_slots": tight,
                "unmatched_slots": unmatched[:6],
            })
    return report
def build_relief_log(days: List[DayRow], slots: List[Slot], assignment: Dict[str, Optional[str]]) -> Dict:
    """
    Summarize where relief valves were used (K=T same doctor, blanks on penalized optional columns).
    """
    slots_by_day: Dict[dt.date, List[Slot]] = defaultdict(list)
    for s in slots:
        slots_by_day[s.day.date].append(s)
    kt_share_days: List[str] = []
    blank_cols: Dict[str, List[str]] = defaultdict(list)
    for day in days:
        day_slots = slots_by_day.get(day.date, [])
        # blanks
        for s in day_slots:
            if (not s.required) and int(getattr(s, "blank_penalty", 0)) > 0:
                if assignment.get(s.slot_id) is None:
                    for c in s.columns:
                        blank_cols[str(c)].append(day.date.isoformat())
        # K/T share (only when K and T are separate slots)
        sk = next((s for s in day_slots if s.columns == ["K"]), None)
        st = next((s for s in day_slots if s.columns == ["T"]), None)
        if sk and st:
            dk = assignment.get(sk.slot_id)
            dt_ = assignment.get(st.slot_id)
            if dk is not None and dk == dt_:
                kt_share_days.append(day.date.isoformat())
    return {
        "kt_share_days": kt_share_days,
        "blank_columns": dict(blank_cols),
    }
def solve_with_ortools(cfg: dict, days: List[DayRow], slots: List[Slot], carryover: Optional[dict] = None) -> Tuple[Dict[str, Optional[str]], Dict]:
    """
    Returns:
      assignment: slot_id -> doctor (or None for optional left blank)
      stats: dict with diagnostic info
    """
    try:
        from ortools.sat.python import cp_model
    except Exception as e:
        raise RuntimeError("OR-Tools not installed. Install with: pip install ortools") from e
    model = cp_model.CpModel()
    # Collect extra objective terms built during constraint setup
    extra_obj = []
    doctors = collect_doctors(cfg)
    doctors = [d for d in doctors if d != "Recupero"] + (["Recupero"] if "Recupero" in doctors else [])
    doc_to_idx = {d:i for i,d in enumerate(doctors)}
    # Decision vars: x[(slot_id, doc)] in {0,1}
    x = {}
    for s in slots:
        for d in s.allowed:
            if d not in doc_to_idx:
                continue
            x[(s.slot_id, d)] = model.NewBoolVar(f"x_{hash(s.slot_id)%10**8}_{hash(d)%10**8}")
    # Slot assignment constraints
    for s in slots:
        vars_ = [x[(s.slot_id, d)] for d in s.allowed if (s.slot_id, d) in x]
        if not vars_:
            # optional slot can be blank
            continue
        if s.required:
            model.Add(sum(vars_) == 1)
        else:
            # Optional slot: can be left blank. If blank_penalty>0, we penalize blanks so it is used only as a last resort.
            if getattr(s, "blank_penalty", 0) and int(getattr(s, "blank_penalty", 0)) > 0:
                # Optional slot may be left blank, but only as a last resort: we add a large penalty
                # term to the objective. This must go into `extra_obj` because `objective_terms`
                # is defined later, when all high-level soft constraints are assembled.
                b = model.NewBoolVar(f"blank_{hash(s.slot_id)%10**8}")
                model.Add(sum(vars_) + b == 1)
                extra_obj.append(b * int(getattr(s, "blank_penalty", 0)))
            else:
                model.Add(sum(vars_) <= 1)
    # Helper: slots per day
    slots_by_day: Dict[dt.date, List[Slot]] = defaultdict(list)
    for s in slots:
        slots_by_day[s.day.date].append(s)
    # Uniqueness per day: one doctor max 1 slot/day (exceptions already handled by merged columns)
    gc = cfg.get("global_constraints") or {}
    relief = gc.get("relief_valves") or {}
    enable_kt_share = bool(relief.get("enable_kt_share", False))
    kt_share_penalty = int(relief.get("kt_share_penalty", 5000))
    for day in days:
        day_slots = slots_by_day.get(day.date, [])
        # Emergency valve: allow the SAME doctor to cover BOTH K and T on the same day (non-Sat),
        # only if needed (penalized in objective).
        y_by_doc = None
        slotK = None
        slotT = None
        if enable_kt_share:
            for s in day_slots:
                if s.columns == ["K"]:
                    slotK = s
                elif s.columns == ["T"]:
                    slotT = s
        if enable_kt_share and slotK is not None and slotT is not None:
            y_by_doc = {}
            for d in doctors:
                if (slotK.slot_id, d) in x and (slotT.slot_id, d) in x:
                    y = model.NewBoolVar(f"kt_same_{day.date.isoformat()}_{hash(d)%10**6}")
                    model.Add(y <= x[(slotK.slot_id, d)])
                    model.Add(y <= x[(slotT.slot_id, d)])
                    model.Add(y >= x[(slotK.slot_id, d)] + x[(slotT.slot_id, d)] - 1)
                    y_by_doc[d] = y
            if y_by_doc:
                model.Add(sum(y_by_doc.values()) <= 1)
                extra_obj.append(sum(y_by_doc.values()) * kt_share_penalty)
        for d in doctors:
            vars_ = []
            for s in day_slots:
                if (s.slot_id, d) in x:
                    vars_.append(x[(s.slot_id, d)])
            if vars_:
                if y_by_doc is not None and d in y_by_doc:
                    model.Add(sum(vars_) <= 1 + y_by_doc[d])
                else:
                    model.Add(sum(vars_) <= 1)
    # ---- D/F pattern 3+3 for Grimaldi/Calabrò (conditional hard)
    # If configured in rules.D_F (pattern_3_3 + pattern_conditional_hard),
    # enforce:
    #   Mon–Wed: D=doc1, F=doc2
    #   Thu–Sat: D=doc2, F=doc1
    # Pattern can be violated ONLY on days where (H is doc1/doc2) OR (J is doc2).
    rules_map = cfg.get("rules", {}) or {}
    r_df = rules_map.get("D_F", {}) if isinstance(rules_map.get("D_F", {}), dict) else {}
    if r_df.get("pattern_3_3") and r_df.get("pattern_conditional_hard"):
        doc1 = norm_name(r_df.get("pattern_doc1") or "")
        doc2 = norm_name(r_df.get("pattern_doc2") or "")
        if doc1 in doctors and doc2 in doctors:
            for day in days:
                if day.dow not in ["Mon","Tue","Wed","Thu","Fri","Sat"]:
                    continue
                dslot = f"{day.date}-D"
                fslot = f"{day.date}-F"
                # if D/F slots don't exist for this day, skip
                v_d1 = x.get((dslot, doc1))
                v_d2 = x.get((dslot, doc2))
                v_f1 = x.get((fslot, doc1))
                v_f2 = x.get((fslot, doc2))
                if (v_d1 is None and v_d2 is None) or (v_f1 is None and v_f2 is None):
                    continue
                # Exception = (H is doc1/doc2) OR (J is doc2)
                conds = []
                hslot = f"{day.date}-H"
                jslot = f"{day.date}-J"
                for v in [x.get((hslot, doc1)), x.get((hslot, doc2)), x.get((jslot, doc2))]:
                    if v is not None:
                        conds.append(v)
                if conds:
                    exc = model.NewBoolVar(f"exc_DF_{day.date}")
                    model.AddMaxEquality(exc, conds)  # exc = OR(conds)
                else:
                    exc = None
                if day.dow in ["Mon","Tue","Wed"]:
                    # Only enforce the 3-day pattern if BOTH required assignments are actually possible
                    # (i.e., corresponding variables exist after unavailability / domain filtering).
                    # Otherwise, enforcing only one side (e.g. F=doc2 while D=doc1 cannot happen)
                    # can make the whole month INFEASIBLE due to daily uniqueness constraints.
                    if v_d1 is None or v_f2 is None:
                        continue
                    # enforce D=doc1, F=doc2 when not exception
                    if v_d1 is not None:
                        if exc is None:
                            model.Add(v_d1 == 1)
                        else:
                            model.Add(v_d1 == 1).OnlyEnforceIf(exc.Not())
                    if v_f2 is not None:
                        if exc is None:
                            model.Add(v_f2 == 1)
                        else:
                            model.Add(v_f2 == 1).OnlyEnforceIf(exc.Not())
                else:
                    if v_d2 is None or v_f1 is None:
                        continue
                    # Thu/Fri/Sat: D=doc2, F=doc1 when not exception
                    if v_d2 is not None:
                        if exc is None:
                            model.Add(v_d2 == 1)
                        else:
                            model.Add(v_d2 == 1).OnlyEnforceIf(exc.Not())
                    if v_f1 is not None:
                        if exc is None:
                            model.Add(v_f1 == 1)
                        else:
                            model.Add(v_f1 == 1).OnlyEnforceIf(exc.Not())
    # Night off next day
    gc = cfg.get("global_constraints", {}) or {}
    night_off = (gc.get("night_off") or {})
    night_next = bool(night_off.get("next_day", True))
    # Identify night slots
    night_slot_ids = [s.slot_id for s in slots if "J" in s.columns]
    if night_next:
        # for each day (except last), if doctor works night(d) then no slot(d+1)
        day_index = {d.date:i for i,d in enumerate(days)}
        for drow in days:
            i = day_index[drow.date]
            if i+1 >= len(days):
                continue
            next_day = days[i+1].date
            for doc in doctors:
                night_vars = []
                for sid in night_slot_ids:
                    if sid.startswith(str(drow.date)) and (sid, doc) in x:
                        night_vars.append(x[(sid, doc)])
                if not night_vars:
                    continue
                night_var = night_vars[0]  # only one night slot per day in our model
                # For every slot on next day, doc cannot be assigned if night_var=1
                for s2 in slots_by_day.get(next_day, []):
                    if (s2.slot_id, doc) in x:
                        model.Add(x[(s2.slot_id, doc)] == 0).OnlyEnforceIf(night_var)
    # Night spacing min
    min_gap = int(gc.get("night_spacing_days_min", 5))
    # Build night vars by (day,doc)
    night_var_by_day_doc = {}
    for s in slots:
        if "J" in s.columns:
            for doc in doctors:
                if (s.slot_id, doc) in x:
                    night_var_by_day_doc[(s.day.date, doc)] = x[(s.slot_id, doc)]
    # For each doc, prevent nights too close
    for doc in doctors:
        for i, drow in enumerate(days):
            for k in range(1, min_gap):
                j = i + k
                if j >= len(days):
                    break
                d1, d2 = drow.date, days[j].date
                v1 = night_var_by_day_doc.get((d1, doc))
                v2 = night_var_by_day_doc.get((d2, doc))
                if v1 is not None and v2 is not None:
                    model.Add(v1 + v2 <= 1)
    # Carryover constraints (previous month / previous block)
    # - block doctors on the first day if they did night on the last day of the previous month
    # - enforce night spacing across the boundary (min_gap)
    carry = carryover or {}
    try:
        first_day = days[0].date
    except Exception:
        first_day = None

    # 1) Next-day off across boundary (if provided)
    blocked = set(norm_name(d) for d in (carry.get('block_all_on_first_day') or []) if d)
    if first_day and blocked:
        for doc in blocked:
            if doc not in doctors:
                continue
            for s2 in slots_by_day.get(first_day, []):
                v = x.get((s2.slot_id, doc))
                if v is not None:
                    model.Add(v == 0)

    # 2) Night spacing across boundary: for any recent night in previous month, forbid nights too close in current month
    recent = carry.get('recent_nights') or []
    parsed_recent: List[Tuple[dt.date, str]] = []
    for item in recent:
        try:
            if isinstance(item, dict):
                d0 = parse_date(item.get('date'))
                doc0 = norm_name(item.get('doctor'))
            else:
                d0 = parse_date(item[0])
                doc0 = norm_name(item[1])
            if doc0 and doc0 in doctors and doc0.lower() != 'recupero':
                parsed_recent.append((d0, doc0))
        except Exception:
            continue

    if parsed_recent:
        for d_prev, doc in parsed_recent:
            for drow in days:
                dd = (drow.date - d_prev).days
                if 0 <= dd < min_gap:
                    v = night_var_by_day_doc.get((drow.date, doc))
                    if v is not None:
                        model.Add(v == 0)

    # Reperibilità constraints: not night same day + next 2 days
    if "rules" in cfg and "C_reperibilita" in cfg["rules"]:
        rC = cfg["rules"]["C_reperibilita"]
        constraints = set(rC.get("constraints") or [])
        if "not_night_same_day" in constraints or "not_night_next_2_days" in constraints:
            for day in days:
                # find C slot
                c_slot = next((s for s in slots_by_day[day.date] if s.columns == ["C"]), None)
                if not c_slot:
                    continue
                for doc in doctors:
                    if (c_slot.slot_id, doc) not in x:
                        continue
                    cvar = x[(c_slot.slot_id, doc)]
                    # same day
                    if "not_night_same_day" in constraints:
                        nvar = night_var_by_day_doc.get((day.date, doc))
                        if nvar is not None:
                            model.Add(nvar == 0).OnlyEnforceIf(cvar)
                    # next 2 days
                    if "not_night_next_2_days" in constraints:
                        for off in [1,2]:
                            idx = days.index(day) + off if day in days else None  # not used
                        # safer with index mapping
                        # map date->pos once
            # Implement with date->pos mapping
            pos = {d.date:i for i,d in enumerate(days)}
            for day in days:
                c_slot = next((s for s in slots_by_day[day.date] if s.columns == ["C"]), None)
                if not c_slot:
                    continue
                for doc in doctors:
                    if (c_slot.slot_id, doc) not in x:
                        continue
                    cvar = x[(c_slot.slot_id, doc)]
                    i = pos[day.date]
                    for off in [0,1,2]:
                        j = i + off
                        if j >= len(days):
                            continue
                        nvar = night_var_by_day_doc.get((days[j].date, doc))
                        if nvar is not None:
                            model.Add(nvar == 0).OnlyEnforceIf(cvar)
    # K no consecutive days same doctor
    if "rules" in cfg and "K" in cfg["rules"] and cfg["rules"]["K"].get("no_consecutive_days_same_doctor", False):
        for i in range(len(days)-1):
            d1, d2 = days[i].date, days[i+1].date
            k1 = next((s for s in slots_by_day[d1] if "K" in s.columns), None)
            k2 = next((s for s in slots_by_day[d2] if "K" in s.columns), None)
            if not k1 or not k2:
                continue
            for doc in doctors:
                v1 = x.get((k1.slot_id, doc))
                v2 = x.get((k2.slot_id, doc))
                if v1 is not None and v2 is not None:
                    model.Add(v1 + v2 <= 1)
    # D and F must be different on same day (pair rule)
    for day in days:
        sD = next((s for s in slots_by_day[day.date] if s.columns == ["D"]), None)
        sF = next((s for s in slots_by_day[day.date] if s.columns == ["F"]), None)
        if not sD or not sF:
            continue
        for doc in doctors:
            vD = x.get((sD.slot_id, doc))
            vF = x.get((sF.slot_id, doc))
            if vD is not None and vF is not None:
                model.Add(vD + vF <= 1)
    # D/F 3+3 weekly pattern:
    # Preferred layout (example):
    #   Mon-Wed: D=Grimaldi, F=Calabrò
    #   Thu-Sat: D=Calabrò, F=Grimaldi
    #
    # Two modes:
    #   - pattern_conditional_hard: enforce the preferred pattern on "non-exception" days
    #     (exception if Grimaldi or Calabrò is scheduled in H, or if Calabrò is scheduled in J on the same date)
    #   - otherwise: only SOFT penalties (legacy behavior)
    if "rules" in cfg and "D_F" in cfg["rules"] and isinstance(cfg["rules"]["D_F"], dict):
        rDF = cfg["rules"]["D_F"]
        if rDF.get("pattern_3_3", False):
            doc1 = norm_name(rDF.get("pattern_doc1") or "Grimaldi")
            doc2 = norm_name(rDF.get("pattern_doc2") or "Calabrò")
            pair = {doc1, doc2}
            conditional_hard = bool(rDF.get("pattern_conditional_hard", False))
            pen_wrong = int(rDF.get("pattern_penalty", 80) or 80)
            pen_fallback = int(rDF.get("fallback_penalty", 200) or 200)
            for day in days:
                if day.dow not in ["Mon","Tue","Wed","Thu","Fri","Sat"]:
                    continue
                sD = next((s for s in slots_by_day[day.date] if s.columns == ["D"]), None)
                sF = next((s for s in slots_by_day[day.date] if s.columns == ["F"]), None)
                if not sD or not sF:
                    continue
                prefD, prefF = (doc1, doc2) if day.dow in ["Mon","Tue","Wed"] else (doc2, doc1)
                # Build "exception" flag: doc1/doc2 used in H OR doc2 used in J on the same day.
                exc_terms = []
                sH = next((s for s in slots_by_day[day.date] if s.columns == ["H"]), None)
                if sH:
                    vH1 = x.get((sH.slot_id, doc1))
                    vH2 = x.get((sH.slot_id, doc2))
                    if vH1 is not None:
                        exc_terms.append(vH1)
                    if vH2 is not None:
                        exc_terms.append(vH2)
                sJ = next((s for s in slots_by_day[day.date] if s.columns == ["J"]), None)
                if sJ:
                    vJ2 = x.get((sJ.slot_id, doc2))
                    if vJ2 is not None:
                        exc_terms.append(vJ2)
                if exc_terms:
                    exc = model.NewBoolVar(f"df_exc_{day.date}")
                    model.AddMaxEquality(exc, exc_terms)  # OR
                else:
                    # no exception drivers present -> treat as no exception
                    exc = model.NewConstant(0)
                # If enabled, enforce the preferred pattern on non-exception days (HARD).
                vD_pref = x.get((sD.slot_id, prefD))
                vF_pref = x.get((sF.slot_id, prefF))
                if conditional_hard and vD_pref is not None and vF_pref is not None:
                    model.Add(vD_pref == 1).OnlyEnforceIf(exc.Not())
                    model.Add(vF_pref == 1).OnlyEnforceIf(exc.Not())
                # Always keep SOFT guidance: avoid non-pair on F, and prefer the weekly pattern when possible.
                # (On days where H/J consumes one of the pair, the solver can deviate and pay a smaller cost.)
                for doc in sD.allowed:
                    v = x.get((sD.slot_id, doc))
                    if v is not None and doc != prefD:
                        extra_obj.append(pen_wrong * v)
                for doc in sF.allowed:
                    v = x.get((sF.slot_id, doc))
                    if v is None:
                        continue
                    if doc != prefF:
                        extra_obj.append(pen_wrong * v)
                    if doc not in pair:
                        extra_obj.append(pen_fallback * v)
# E/G weekly blocks (Mon-Sat) if block_days=6
    if "rules" in cfg and "E_G" in cfg["rules"]:
        block_days = int(cfg["rules"]["E_G"].get("block_days", 0) or 0)
        # Find all EG slots by date
        eg_by_date = {}
        for s in slots:
            if s.columns == ["E","G"]:
                eg_by_date[s.day.date] = s
        if block_days == 6:
            # For each Monday, enforce same doctor Mon..Sat (if all present)
            for day in days:
                if day.dow != "Mon":
                    continue
                seq = [day.date + dt.timedelta(days=k) for k in range(0,6)]
                if not all(d in eg_by_date for d in seq):
                    continue
                for doc in doctors:
                    v0 = x.get((eg_by_date[seq[0]].slot_id, doc))
                    if v0 is None:
                        continue
                    for d in seq[1:]:
                        v = x.get((eg_by_date[d].slot_id, doc))
                        if v is not None:
                            model.Add(v == v0)
        elif block_days == 3:
            # Split week into 2 blocks: Mon-Wed and Thu-Sat (if all present).
            for day in days:
                if day.dow != "Mon":
                    continue
                seq1 = [day.date + dt.timedelta(days=k) for k in range(0,3)]
                seq2 = [day.date + dt.timedelta(days=k) for k in range(3,6)]
                if not all(d in eg_by_date for d in (seq1 + seq2)):
                    continue
                for doc in doctors:
                    v0 = x.get((eg_by_date[seq1[0]].slot_id, doc))
                    if v0 is not None:
                        for d in seq1[1:]:
                            v = x.get((eg_by_date[d].slot_id, doc))
                            if v is not None:
                                model.Add(v == v0)
                for doc in doctors:
                    v3 = x.get((eg_by_date[seq2[0]].slot_id, doc))
                    if v3 is not None:
                        for d in seq2[1:]:
                            v = x.get((eg_by_date[d].slot_id, doc))
                            if v is not None:
                                model.Add(v == v3)
                # Soft preference: use two different doctors between the two 3-day blocks.
                # Penalize if the SAME doctor is chosen on Mon (block1 start) and Thu (block2 start).
                pen_split = int((cfg["rules"]["E_G"].get("block_split_penalty", 10) or 10))
                mon_slot = eg_by_date[seq1[0]]
                thu_slot = eg_by_date[seq2[0]]
                same_terms = []
                for doc in doctors:
                    vmon = x.get((mon_slot.slot_id, doc))
                    vthu = x.get((thu_slot.slot_id, doc))
                    if vmon is None or vthu is None:
                        continue
                    b = model.NewBoolVar(f"eg_same_{hash(doc)%10**6}_{day.date}")
                    model.AddBoolAnd([vmon, vthu]).OnlyEnforceIf(b)
                    model.AddBoolOr([vmon.Not(), vthu.Not()]).OnlyEnforceIf(b.Not())
                    same_terms.append(b)
                if same_terms:
                    extra_obj.append(pen_split * sum(same_terms))
    # Monthly quotas (hard)
    # J monthly_quotas
    if "rules" in cfg and "J" in cfg["rules"]:
        mq = cfg["rules"]["J"].get("monthly_quotas") or {}
        for doc, q in mq.items():
            doc = norm_name(doc)
            if doc not in doctors:
                continue
            vars_ = [night_var_by_day_doc.get((d.date, doc)) for d in days]
            vars_ = [v for v in vars_ if v is not None]
            if vars_:
                model.Add(sum(vars_) == int(q))
    # Night distribution (soft): if the night pool size implies an equal target per doctor (e.g., Feb: 24 nights / 12 docs = 2),
    # add penalties to keep each doctor close to that target. This avoids extreme imbalances (e.g., 5 nights vs 1).
    if "rules" in cfg and "J" in cfg["rules"]:
        rJ = cfg["rules"]["J"]
        # Night pool = pool_other + monthly_quotas keys
        night_pool = set(norm_name(d) for d in (rJ.get("pool_other") or []))
        night_pool |= set(norm_name(d) for d in (rJ.get("monthly_quotas") or {}).keys())
        night_pool = {d for d in night_pool if d in doctors and d != "Recupero"}
        total_nights = sum(1 for s in slots if s.columns == ["J"])
        if night_pool and total_nights > 0 and total_nights % len(night_pool) == 0:
            target = total_nights // len(night_pool)
            for doc in sorted(night_pool):
                vars_ = []
                for drow in days:
                    v = night_var_by_day_doc.get((drow.date, doc))
                    if v is not None:
                        vars_.append(v)
                if vars_:
                    cnt = model.NewIntVar(0, total_nights, f"nightcnt_{hash(doc)%10**6}")
                    model.Add(cnt == sum(vars_))
                    # abs(cnt - target)
                    diff = model.NewIntVar(0, total_nights, f"nightdiff_{hash(doc)%10**6}")
                    model.AddAbsEquality(diff, cnt - target)
                    extra_obj.append(20 * diff)
    # H monthly quotas Mon-Fri
    if "rules" in cfg and "H" in cfg["rules"]:
        mqH = cfg["rules"]["H"].get("monthly_quotas") or {}
        for key, q in mqH.items():
            # keys could be 'Grimaldi_mon_fri'
            m = re.match(r"(.+)_mon_fri", str(key).strip(), flags=re.I)
            if not m:
                continue
            doc = norm_name(m.group(1))
            if doc not in doctors:
                continue
            vars_ = []
            for day in days:
                if day.dow in ["Mon","Tue","Wed","Thu","Fri"]:
                    sH = next((s for s in slots_by_day[day.date] if s.columns == ["H"]), None)
                    if sH and (sH.slot_id, doc) in x:
                        vars_.append(x[(sH.slot_id, doc)])
            if vars_:
                model.Add(sum(vars_) == int(q))
        # cap per doctor for pool_mon_fri
        cap = cfg["rules"]["H"].get("cap_mon_fri_per_doctor")
        if cap is not None:
            cap = int(cap)
            pool_cap = [norm_name(d) for d in (cfg["rules"]["H"].get("pool_mon_fri") or [])]
            for doc in pool_cap:
                if doc not in doctors:
                    continue
                vars_ = []
                for day in days:
                    if day.dow in ["Mon","Tue","Wed","Thu","Fri"]:
                        sH = next((s for s in slots_by_day[day.date] if s.columns == ["H"]), None)
                        if sH and (sH.slot_id, doc) in x:
                            vars_.append(x[(sH.slot_id, doc)])
                if vars_:
                    model.Add(sum(vars_) <= cap)
    # L quota Recupero
    if "rules" in cfg and "L" in cfg["rules"]:
        qrec = cfg["rules"]["L"].get("quota_recupero_per_month")
        if qrec is not None and "Recupero" in doctors:
            vars_=[]
            for s in slots:
                if s.columns == ["L"] and (s.slot_id, "Recupero") in x:
                    vars_.append(x[(s.slot_id, "Recupero")])
            if vars_:
                # Hard cap (<=) and soft preference to reach the target.
                qrec_int = int(qrec)
                model.Add(sum(vars_) <= qrec_int)
                # soft: minimize shortfall (qrec_int - sum(vars_))
                short = model.NewIntVar(0, qrec_int, f"L_rec_short")
                model.Add(sum(vars_) + short == qrec_int)
                extra_obj.append(5 * short)
    # Y two Mondays as Recupero (if enabled) – counts ONLY the optional Y_REC slots
    if "rules" in cfg and "Y" in cfg["rules"]:
        rY = cfg["rules"]["Y"]
        if rY.get("recupero_two_mondays_per_month", False) and "Recupero" in doctors:
            vars_=[]
            for s in slots:
                if (s.rule_tag == "Y_REC") and (s.slot_id, "Recupero") in x:
                    vars_.append(x[(s.slot_id, "Recupero")])
            if vars_:
                model.Add(sum(vars_) == 2)
    # Weekend full off: at least N Sat+Sun "full weekends off" per doctor.
    # By default this is a HARD constraint. If it makes the month infeasible,
    # you can set global_constraints.weekend_off_soft: true to make it a SOFT constraint
    # (the solver will minimize the number of missing weekends-off).
    min_weekends = int(gc.get("min_full_weekends_off_per_month", 0) or 0)
    weekend_exempt = set(norm_name(x) for x in (gc.get("weekend_off_exempt") or []))
    # Always exempt the placeholder 'Recupero' from weekend-off accounting
    weekend_exempt.add("Recupero")
    weekend_soft = bool(gc.get("weekend_off_soft", False))
    weekend_penalty = int(gc.get("weekend_off_penalty", 50) or 50)  # penalty per missing full weekend off
    weekend_shortfalls: List = []
    if min_weekends > 0:
        # find weekend pairs within available days
        date_set = {d.date for d in days}
        weekend_pairs = []
        for drow in days:
            if drow.dow == "Sat":
                sun = drow.date + dt.timedelta(days=1)
                # count only complete Sat+Sun pairs present in the template for this month
                if sun in date_set and DOW_MAP[sun.weekday()] == "Sun":
                    weekend_pairs.append((drow.date, sun))
        # For each doctor, create weekend_off[w,doc] bool
        weekend_off: Dict[Tuple[int, str], "cp_model.IntVar"] = {}
        for wi, (sat, sun) in enumerate(weekend_pairs):
            for doc in doctors:
                if doc in weekend_exempt:
                    continue
                b = model.NewBoolVar(f"wkoff_{wi}_{hash(doc)%10**6}")
                weekend_off[(wi, doc)] = b
                # If weekend_off=1 then doc has no assignment on sat and sun
                for s in slots_by_day.get(sat, []):
                    if (s.slot_id, doc) in x:
                        model.Add(x[(s.slot_id, doc)] == 0).OnlyEnforceIf(b)
                for s in slots_by_day.get(sun, []):
                    if (s.slot_id, doc) in x:
                        model.Add(x[(s.slot_id, doc)] == 0).OnlyEnforceIf(b)
                # Reverse implication: if any assignment on sat or sun then b=0
                any_vars = []
                for s in slots_by_day.get(sat, []):
                    v = x.get((s.slot_id, doc))
                    if v is not None:
                        any_vars.append(v)
                for s in slots_by_day.get(sun, []):
                    v = x.get((s.slot_id, doc))
                    if v is not None:
                        any_vars.append(v)
                if any_vars:
                    # sum(any_vars)==0 -> b=1
                    model.Add(sum(any_vars) == 0).OnlyEnforceIf(b)
                    # if any assignment then b=0
                    for av in any_vars:
                        model.Add(b + av <= 1)
        for doc in doctors:
            if doc in weekend_exempt:
                continue
            vars_ = [weekend_off[(wi, doc)] for wi, _ in enumerate(weekend_pairs) if (wi, doc) in weekend_off]
            if vars_:
                if weekend_soft:
                    short = model.NewIntVar(0, len(weekend_pairs), f"wkshort_{hash(doc)%10**6}")
                    model.Add(sum(vars_) + short >= min_weekends)
                    weekend_shortfalls.append(short)
                else:
                    model.Add(sum(vars_) >= min_weekends)
    # Objectives (soft): fairness + maximize S dedicated + minimize weekend night concentration + prefer night spacing >=7
    objective_terms = []
    # Weekend-off soft penalties
    if weekend_shortfalls:
        objective_terms.append(weekend_penalty * sum(weekend_shortfalls))
    # fairness: minimize max assignments per doctor (excluding 'Recupero')
    real_doctors = [d for d in doctors if d != "Recupero"]
    max_load = model.NewIntVar(0, 999, "max_load")
    for doc in real_doctors:
        vars_ = []
        for s in slots:
            v = x.get((s.slot_id, doc))
            if v is not None:
                vars_.append(v)
        if vars_:
            load = model.NewIntVar(0, 999, f"load_{hash(doc)%10**6}")
            model.Add(load == sum(vars_))
            model.Add(load <= max_load)
    objective_terms.append(max_load * 10)
    # Penalize using placeholder 'Recupero' (otherwise the solver overuses it to improve fairness)
    gc_obj = cfg.get("global_constraints", {}) or {}
    rec_pen_default = int(gc_obj.get("recupero_penalty_default", 50))
    rec_pen_echo = int(gc_obj.get("recupero_penalty_echo", 200))
    for s in slots:
        v_rec = x.get((s.slot_id, "Recupero"))
        if v_rec is None:
            continue
        cols = set(s.columns)
        pen = rec_pen_echo if cols.intersection({"Q","R","T"}) else rec_pen_default
        objective_terms.append(pen * v_rec)
    # Maximize number of Wednesdays with dedicated S assignment (if optional)
    s_slots = [s for s in slots if s.columns == ["S"]]
    s_dedicated = []
    for s in s_slots:
        vars_ = [x.get((s.slot_id, d)) for d in doctors if (s.slot_id, d) in x]
        vars_ = [v for v in vars_ if v is not None]
        if vars_:
            b = model.NewBoolVar(f"sfilled_{hash(s.slot_id)%10**6}")
            model.Add(sum(vars_) == 1).OnlyEnforceIf(b)
            model.Add(sum(vars_) == 0).OnlyEnforceIf(b.Not())
            s_dedicated.append(b)
    if s_dedicated:
        # subtract to maximize (minimize negative)
        objective_terms.append(-5 * sum(s_dedicated))
    # Prefer night spacing >=7: penalize gaps of 5 or 6
    preferred = int(gc.get("night_spacing_days_preferred", 7))
    if preferred > min_gap:
        penalties=[]
        for doc in real_doctors:
            for i, drow in enumerate(days):
                for k in range(min_gap, preferred):
                    j=i+k
                    if j>=len(days): break
                    v1=night_var_by_day_doc.get((drow.date, doc))
                    v2=night_var_by_day_doc.get((days[j].date, doc))
                    if v1 is not None and v2 is not None:
                        p = model.NewBoolVar(f"ngap_{hash(doc)%10**6}_{i}_{k}")
                        # p=1 if both nights, else 0
                        model.Add(v1 + v2 == 2).OnlyEnforceIf(p)
                        model.Add(v1 + v2 <= 1).OnlyEnforceIf(p.Not())
                        penalties.append(p)
        if penalties:
            objective_terms.append(3 * sum(penalties))
    # Weekend night concentration: minimize max weekend nights per doctor
    weekend_night_max = model.NewIntVar(0, 31, "wk_night_max")
    for doc in real_doctors:
        vars_=[]
        for day in days:
            if day.dow in ["Fri","Sat","Sun"]:
                v=night_var_by_day_doc.get((day.date, doc))
                if v is not None:
                    vars_.append(v)
        if vars_:
            wkload = model.NewIntVar(0, 31, f"wk_night_{hash(doc)%10**6}")
            model.Add(wkload == sum(vars_))
            model.Add(wkload <= weekend_night_max)
    objective_terms.append(weekend_night_max * 2)
    model.Minimize(sum(objective_terms + extra_obj))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30.0
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    if status not in [cp_model.OPTIMAL, cp_model.FEASIBLE]:
        raise RuntimeError("No feasible schedule found with given rules/unavailability.")
    assignment: Dict[str, Optional[str]] = {s.slot_id: None for s in slots}
    for s in slots:
        chosen = None
        for d in s.allowed:
            v = x.get((s.slot_id, d))
            if v is not None and solver.Value(v) == 1:
                chosen = d
                break
        assignment[s.slot_id] = chosen
    stats = {
        "status": "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE",
        "objective": solver.ObjectiveValue(),
    }
    return assignment, stats
# -------------------------
# Fallback greedy (simple)
# -------------------------
def solve_greedy(cfg: dict, days: List[DayRow], slots: List[Slot], carryover: Optional[dict] = None) -> Tuple[Dict[str, Optional[str]], Dict]:
    """
    Greedy with rule-aware priorities (fallback when OR-Tools is missing or after autorelax still infeasible).
    Goals:
    - always cover required slots when possible
    - enforce key HARD quotas (e.g., Recupero on L <= 3, Recupero on Y = 2 Mondays) and fixed-day rules
    - avoid obviously bad imbalances (especially nights)
    """
    # Rank shifts: Night first helps satisfy spacing/off constraints early
    shift_rank = {"Notte": 0, "Pomeriggio": 1, "Mattina": 2, "Any": 3}
    # Sort: required first, then smallest candidate pool, then shift priority
    slots_sorted = sorted(
        slots,
        key=lambda s: (not s.required, len(s.allowed), shift_rank.get(s.shift, 9), s.rule_tag or "", s.slot_id),
    )
    assignment: Dict[str, Optional[str]] = {s.slot_id: None for s in slots}
    used_per_day: Dict[dt.date, Set[str]] = defaultdict(set)
    nights_by_doc: Dict[str, List[dt.date]] = defaultdict(list)
    # Seed nights with carryover (previous month) so spacing and next-day-off also apply on day 1
    carry = carryover or {}
    recent = carry.get('recent_nights') or []
    for item in recent:
        try:
            if isinstance(item, dict):
                d0 = parse_date(item.get('date'))
                doc0 = norm_name(item.get('doctor'))
            else:
                d0 = parse_date(item[0])
                doc0 = norm_name(item[1])
            if doc0 and doc0.lower() != 'recupero':
                nights_by_doc[doc0].append(d0)
        except Exception:
            continue
    # Optional: explicit block-all-on-first-day doctors (if provided)
    first_day = days[0].date if days else None
    block_first = set(norm_name(d) for d in (carry.get('block_all_on_first_day') or []) if d)
    # represent as a 'night on previous day' only when we know the previous-day date;
    # otherwise we hard-block via a date->set map used in can_assign
    blocked_by_day = defaultdict(set)
    if first_day and block_first:
        blocked_by_day[first_day] |= block_first
    load_total = Counter()
    load_by_tag: Dict[str, Counter] = defaultdict(Counter)
    gc = cfg.get("global_constraints", {}) or {}
    min_gap = int(gc.get("night_spacing_days_min", 5) or 5)
    night_off_next = bool((gc.get("night_off") or {}).get("next_day", True))
    # --- Hard quotas / caps from rules
    rules = cfg.get("rules", {}) or {}
    # L: Recupero cap (interpret as MAX, not exact)
    L_cap_rec = None
    if isinstance(rules.get("L"), dict):
        L_cap_rec = rules["L"].get("quota_recupero_per_month")
        L_cap_rec = int(L_cap_rec) if L_cap_rec is not None else None
    L_rec_used = 0
    # Y: Monday specialist clinics
    # - Y_MAIN: always 1 doctor among the main pool (rotation)
    # - Y_REC: optional second line (Recupero) on exactly 2 Mondays/month
    Y_need_rec = 0
    if isinstance(rules.get("Y"), dict) and rules["Y"].get("recupero_two_mondays_per_month", False):
        Y_need_rec = 2
    Y_rec_used = 0
    Y_rec_slots_total = sum(1 for s in slots if getattr(s, "rule_tag", "") == "Y_REC")
    Y_rec_slots_done = 0
    Y_pool_counts = Counter()
    # Night equalization target (if divisible)
    night_pool = set()
    if isinstance(rules.get("J"), dict):
        rJ = rules["J"]
        night_pool |= {norm_name(d) for d in (rJ.get("pool_other") or [])}
        night_pool |= {norm_name(d) for d in (rJ.get("monthly_quotas") or {}).keys()}
        night_pool.discard("Recupero")
    night_slots_total = sum(1 for s in slots if s.columns == ["J"])
    night_target = None
    if night_pool and night_slots_total > 0 and night_slots_total % len(night_pool) == 0:
        night_target = night_slots_total // len(night_pool)
    def can_assign(s: Slot, doc: str) -> bool:
        # per-day uniqueness (ignore placeholder 'Recupero')
        if doc != "Recupero" and doc in used_per_day[s.day.date]:
            return False
        # carryover explicit blocks (e.g., night on last day of previous month -> no work on day 1)
        if doc in blocked_by_day.get(s.day.date, set()):
            return False
        # L cap for Recupero
        if s.columns == ["L"] and doc == "Recupero" and L_cap_rec is not None and L_rec_used >= L_cap_rec:
            return False
        # Y_REC quota for Recupero (avoid exceeding)
        if (getattr(s, "rule_tag", "") == "Y_REC") and doc == "Recupero" and Y_need_rec and Y_rec_used >= Y_need_rec:
            return False
        # Night spacing
        if s.columns == ["J"]:
            for prev in nights_by_doc[doc]:
                if abs((s.day.date - prev).days) < min_gap:
                    return False
        # Night off next day (if doc did night previous day)
        if night_off_next:
            prev_day = s.day.date - dt.timedelta(days=1)
            if prev_day in nights_by_doc[doc]:
                return False
        # K no consecutive days (if enabled in rules)
        if "K" in s.columns and isinstance(rules.get("K"), dict) and rules["K"].get("no_consecutive_days_same_doctor", False):
            prev_day = s.day.date - dt.timedelta(days=1)
            if assignment.get(f"{prev_day}-K") == doc:
                return False
        # D/F different is handled by per-day uniqueness + separate slots; ok.
        return True
    def score_candidate(s: Slot, doc: str) -> Tuple:
        """
        Lower is better.
        Prioritize:
        - Night balance (nights first, then total load)
        - Column-specific balance (e.g., Y rotation)
        - Total load balance
        """
        tag = s.rule_tag or ""
        if s.columns == ["J"]:
            night_cnt = len(nights_by_doc[doc])
            # keep closer to target if known
            tgt_pen = abs(night_cnt - (night_target or 0)) if night_target is not None else night_cnt
            return (tgt_pen, night_cnt, load_total[doc], doc.lower())
        if (s.rule_tag or "") == "Y_REC":
            return (0, load_total[doc], doc.lower())
        if (s.rule_tag or "") == "Y_MAIN":
            return (Y_pool_counts[doc], load_total[doc], doc.lower())
        # default: balance by tag then total
        return (load_by_tag[tag][doc], load_total[doc], doc.lower())
    def pick(s: Slot) -> Optional[str]:
        candidates = [d for d in s.allowed if can_assign(s, d)]
        if not candidates:
            return None
        # Force Recupero on Y_REC when needed and we're running out of Monday slots
        nonlocal Y_rec_slots_done, Y_rec_used
        if (getattr(s, "rule_tag", "") == "Y_REC") and Y_need_rec:
            remaining_after_this = (Y_rec_slots_total - (Y_rec_slots_done + 1))
            remaining_need = (Y_need_rec - Y_rec_used)
            if remaining_need <= 0:
                return None  # leave blank
            if remaining_need > remaining_after_this:
                # must use Recupero now (if possible)
                if "Recupero" in candidates:
                    return "Recupero"
                return None
            # otherwise, leave blank to keep flexibility
            return None
        candidates.sort(key=lambda d: score_candidate(s, d))
        return candidates[0]
    conflicts = []
    for s in slots_sorted:
        if (s.rule_tag or "") == "Y_REC":
            Y_rec_slots_done += 1
        chosen = pick(s)
        if chosen is None:
            if s.required:
                conflicts.append(f"UNFILLED required slot {s.slot_id} ({s.columns})")
            continue
        assignment[s.slot_id] = chosen
        used_per_day[s.day.date].add(chosen)
        load_total[chosen] += 1
        load_by_tag[s.rule_tag or ""][chosen] += 1
        if s.columns == ["J"]:
            nights_by_doc[chosen].append(s.day.date)
        if s.columns == ["L"] and chosen == "Recupero":
            L_rec_used += 1
        if (s.rule_tag or "") == "Y_REC":
            if chosen == "Recupero":
                Y_rec_used += 1
        if (s.rule_tag or "") == "Y_MAIN":
            Y_pool_counts[chosen] += 1
    stats = {
        "status": "GREEDY",
        "conflicts": conflicts,
        "loads": dict(load_total),
        "nights_per_doc": {k: len(v) for k, v in nights_by_doc.items()},
        "L_recupero_used": L_rec_used,
        "Y_recupero_used": Y_rec_used,
    }
    return assignment, stats
# -------------------------
# Write output Excel
# -------------------------
def write_output(
    wb: openpyxl.Workbook,
    ws: openpyxl.worksheet.worksheet.Worksheet,
    days: List[DayRow],
    slots: List[Slot],
    assignment: Dict[str, Optional[str]],
    out_path: Path,
    unav_map: Optional[Dict[str, Dict[dt.date, Set[str]]]] = None,
):
    # Clear target columns (only those managed)
    managed_cols=set()
    for s in slots:
        managed_cols |= set(s.columns)
    # do not wipe A,B headers; wipe from row 2
    for drow in days:
        for col in managed_cols:
            ws[f"{col}{drow.row_idx}"].value = None
    # Fill (support multiple assignments on the same cell, e.g. Y_MAIN + Y_REC)
    slot_by_id = {s.slot_id: s for s in slots}
    assigned_by_day: Dict[dt.date, Set[str]] = defaultdict(set)
    cell_values: Dict[Tuple[int, str], List[str]] = defaultdict(list)
    # Iterate slots in creation order for stable writing
    for s in slots:
        doc = assignment.get(s.slot_id)
        if not doc:
            continue
        for col in s.columns:
            cell_values[(s.day.row_idx, col)].append(doc)
        assigned_by_day[s.day.date].add(doc)
    for (row_idx, col), docs in cell_values.items():
        # de-duplicate while preserving order
        seen = set()
        uniq = [d for d in docs if not (d in seen or seen.add(d))]
        ws[f"{col}{row_idx}"].value = "\n".join(uniq)
    # Fill medici liberi 1/2 (AD/AE)
    # Nota: le colonne AD/AE possono rimanere vuote. Se però inseriamo un nome,
    # deve essere un medico *disponibile* quel giorno (nessuna indisponibilità registrata).
    all_docs=set()
    for s in slots:
        all_docs |= set(s.allowed)
    # Remove placeholder if present
    all_docs = {d for d in all_docs if d != "Recupero"}
    unav_map = unav_map or {}
    for drow in days:
        assigned_today = assigned_by_day.get(drow.date, set())
        unavailable_today = {d for d in all_docs if unav_map.get(d, {}).get(drow.date)}
        free = sorted(list(all_docs - assigned_today - unavailable_today), key=lambda s: s.lower())
        ws[f"AD{drow.row_idx}"].value = free[0] if len(free) > 0 else None
        ws[f"AE{drow.row_idx}"].value = free[1] if len(free) > 1 else None
    wb.save(out_path)
def write_solver_log(out_path: Path, stats: Dict) -> Optional[Path]:
    """
    Writes a human-readable solver log next to the output Excel.
    Includes: per-month status, objective, relief valves used, and day-level bottlenecks if any.
    """
    try:
        log_path = out_path.with_name(out_path.stem + "_solverlog.txt")
        lines: List[str] = []
        lines.append(f"Output: {out_path}")
        lines.append(f"Generated: {dt.datetime.now().isoformat(timespec='seconds')}")
        lines.append(f"Overall status: {stats.get('status')}")
        months = stats.get("months") or {}
        for mk in sorted(months.keys()):
            sm = months.get(mk) or {}
            lines.append("")
            lines.append(f"== {mk} ==")
            lines.append(f"status: {sm.get('status')}")
            if "objective" in sm:
                lines.append(f"objective: {sm.get('objective')}")
            if sm.get("autorelax"):
                lines.append(f"autorelax: {sm.get('autorelax')}")
            if sm.get("solver_error"):
                lines.append(f"solver_error: {sm.get('solver_error')}")
            # Relief used
            ru = sm.get("relief_used") or {}
            if ru.get("kt_share_days") or ru.get("blank_columns"):
                if ru.get("kt_share_days"):
                    lines.append("relief: K=T same doctor days: " + ", ".join(ru.get("kt_share_days")))
                bc = ru.get("blank_columns") or {}
                for c in sorted(bc.keys()):
                    lines.append(f"relief: blank {c} on: " + ", ".join(bc[c]))
            # Day-level bottlenecks (if OR-Tools failed at least once)
            bl = sm.get("day_level_bottlenecks") or []
            if bl:
                lines.append("")
                lines.append("Day-level bottlenecks (ignores cross-day constraints):")
                for item in bl[:10]:
                    lines.append(f"- {item.get('date')} ({item.get('dow')}): required_slots={item.get('required_slots')}, union_doctors={item.get('union_doctors')}")
                    for us in (item.get("unmatched_slots") or [])[:3]:
                        lines.append(f"    * {us.get('slot_id')} cols={us.get('columns')} allowed_n={us.get('allowed_n')}")
        log_path.write_text("\n".join(lines), encoding="utf-8")
        return log_path
    except Exception:
        return None
def solve_across_months(cfg: dict, days: List[DayRow], unav_map: Dict[str, Dict[dt.date, Set[str]]], carryover_by_month: Optional[dict] = None) -> Tuple[List[Slot], Dict[str, Optional[str]], Dict]:
    """
    Many constraints in the YAML are intended "per month" (e.g., caps, quotas).
    If the template contains multiple months (e.g., Feb+Mar in a single sheet),
    solving the whole horizon at once can become INFEASIBLE.
    Strategy: solve each (year, month) independently and merge the results back
    into a single assignment, then write once to the same workbook.
    """
    month_keys = sorted({(d.date.year, d.date.month) for d in days})
    carryover_by_month = carryover_by_month or {}
    prev_key = None
    prev_days_m = None
    prev_slots_m = None
    prev_assignment_m = None
    slots_all: List[Slot] = []
    assignment_all: Dict[str, Optional[str]] = {}
    stats_all: Dict = {"status": "OK", "months": {}}
    for (yy, mm) in month_keys:
        days_m = [d for d in days if (d.date.year, d.date.month) == (yy, mm)]
        carry_m = carryover_by_month.get((yy, mm)) or carryover_by_month.get(f"{yy}-{mm:02d}")
        # If not provided, propagate from previous solved month when consecutive in the template
        if carry_m is None and prev_key is not None and prev_days_m is not None and prev_slots_m is not None and prev_assignment_m is not None:
            try:
                prev_last = max(d.date for d in prev_days_m)
                cur_first = min(d.date for d in days_m)
                if cur_first == (prev_last + dt.timedelta(days=1)):
                    carry_m = build_carryover_from_assignment(cfg, prev_days_m, prev_slots_m, prev_assignment_m, cur_first)
            except Exception:
                carry_m = None
        # Apply carryover "block on first day" as a synthetic full-day unavailability.
        # This has two benefits:
        # - it reinforces the constraint at the domain level (clearer diagnostics)
        # - it prevents blocked doctors from appearing as "Medici liberi" on day 1
        try:
            if carry_m and (carry_m.get("block_all_on_first_day") or []):
                first_day = min(d.date for d in days_m)
                for doc in (carry_m.get("block_all_on_first_day") or []):
                    doc = norm_name(doc)
                    if not doc:
                        continue
                    unav_map.setdefault(doc, {}).setdefault(first_day, set()).add("Any")
        except Exception:
            pass

        slots_m = slots_for_month(cfg, days_m, unav_map)
        # Try OR-Tools first. If infeasible, auto-relax in a controlled order (so you still get a near-optimal schedule)
        # before falling back to greedy.
        try:
            assignment_m, stats_m = solve_with_ortools(cfg, days_m, slots_m, carryover=carry_m)
        except Exception as e1:
            # Day-level diagnostics to pinpoint bottlenecks (ignores cross-day constraints)
            diag1 = diagnose_day_level(days_m, slots_m)
            # Auto-relax (default ON)
            try:
                gc = (cfg.get("global_constraints") or {})
                autorelax = bool(gc.get("autorelax", True))
            except Exception:
                autorelax = True
            if autorelax:
                # 1) Relax weekend-off as SOFT (common source of infeasibility, especially months with 3 full weekend pairs)
                cfg2 = deepcopy(cfg)
                cfg2.setdefault("global_constraints", {})
                cfg2["global_constraints"]["weekend_off_soft"] = True
                cfg2["global_constraints"].setdefault("weekend_off_penalty", 50)
                slots_m2 = slots_for_month(cfg2, days_m, unav_map)
                try:
                    assignment_m, stats_m = solve_with_ortools(cfg2, days_m, slots_m2, carryover=carry_m)
                    stats_m = dict(stats_m) if isinstance(stats_m, dict) else {"status": "FEASIBLE"}
                    stats_m["autorelax"] = ["weekend_off_soft"]
                    # Keep cfg2 changes local: we only use them for this month solve
                    slots_m = slots_m2
                except Exception as e2:
                    # Final fallback: greedy
                    assignment_m, stats_m = solve_greedy(cfg, days_m, slots_m, carryover=carry_m)
                    stats_m = dict(stats_m) if isinstance(stats_m, dict) else {"status": "GREEDY"}
                    stats_m["solver_error"] = f"{e1} | autorelax failed: {e2}"
            else:
                assignment_m, stats_m = solve_greedy(cfg, days_m, slots_m, carryover=carry_m)
                stats_m = dict(stats_m) if isinstance(stats_m, dict) else {"status": "GREEDY"}
                stats_m["solver_error"] = str(e1)
            if diag1:
                stats_m["day_level_bottlenecks"] = diag1
        # Relief usage summary (only recorded if something was actually relaxed)
        try:
            relief_used = build_relief_log(days_m, slots_m, assignment_m)
            if relief_used.get("kt_share_days") or relief_used.get("blank_columns"):
                stats_m["relief_used"] = relief_used
        except Exception:
            pass
        # Save for carryover propagation
        prev_key = (yy, mm)
        prev_days_m = days_m
        prev_slots_m = slots_m
        prev_assignment_m = assignment_m
        # Merge
        slots_all.extend(slots_m)
        assignment_all.update(assignment_m)
        stats_all["months"][f"{yy}-{mm:02d}"] = stats_m
        st = str(stats_m.get("status", "")).upper()
        if "INFEAS" in st:
            stats_all["status"] = "INFEASIBLE"
    return slots_all, assignment_all, stats_all
# -------------------------


# -------------------------
# Public API (headless)
# -------------------------
def generate_schedule(
    template_xlsx: "Path | str",
    rules_yml: "Path | str",
    out_xlsx: "Path | str",
    unavailability_path: "Path | str | None" = None,
    sheet_name: "str | None" = None,
    carryover_by_month: Optional[dict] = None,
):
    """Generate schedules without Tkinter.

    This is the function used by Streamlit (and can be used programmatically).

    Parameters
    ----------
    template_xlsx: path to the Excel template (.xlsx)
    rules_yml: path to the YAML rules file (.yml/.yaml)
    out_xlsx: output Excel path (.xlsx)
    unavailability_path: optional path to unavailability file (.xlsx/.csv/.tsv)
    sheet_name: optional worksheet name in the template

    Returns
    -------
    (stats, log_path)
        stats: dict returned by the solver
        log_path: path to the written log file (or None)
    """
    template = Path(template_xlsx)
    rules = Path(rules_yml)
    outp = Path(out_xlsx)
    unav = Path(unavailability_path) if unavailability_path else None

    cfg = load_rules(rules)
    wb, ws, days = load_template_days(template, sheet_name=sheet_name)
    unav_map = load_unavailability(unav)
    slots, assignment, stats = solve_across_months(cfg, days, unav_map, carryover_by_month=carryover_by_month)
    write_output(wb, ws, days, slots, assignment, outp, unav_map=unav_map)
    logp = write_solver_log(outp, stats)
    return stats, str(logp) if logp else None

# GUI (tkinter)
# -------------------------
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox
    root = tk.Tk()
    root.title("Turni Autogenerator (prototype)")
    template_var = tk.StringVar()
    rules_var = tk.StringVar()
    unav_var = tk.StringVar()
    out_var = tk.StringVar()
    def pick_file(var: tk.StringVar, types):
        p = filedialog.askopenfilename(filetypes=types)
        if p:
            var.set(p)
    def pick_save(var: tk.StringVar):
        p = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")])
        if p:
            var.set(p)
    def go():
        try:
            template = Path(template_var.get())
            rules = Path(rules_var.get())
            unav = Path(unav_var.get()) if unav_var.get().strip() else None
            outp = Path(out_var.get())
            if not template.exists() or not rules.exists() or not outp:
                raise ValueError("Seleziona template, regole e output.")
            cfg = load_rules(rules)
            wb, ws, days = load_template_days(template)
            unav_map = load_unavailability(unav)
            carryover_by_month = None  # Tkinter GUI currently has no carryover input
            slots, assignment, stats = solve_across_months(cfg, days, unav_map, carryover_by_month=carryover_by_month)
            # Avviso se qualche mese è andato in fallback greedy
            greedy_months = [k for k,v in (stats.get('months') or {}).items() if isinstance(v, dict) and v.get('solver_error')]
            if greedy_months:
                messagebox.showwarning(
                    "Solver",
                    "OR-Tools non disponibile o schedule infeasible per: " + ", ".join(greedy_months) + "\nUso greedy per quei mesi."
                )
            write_output(wb, ws, days, slots, assignment, outp, unav_map=unav_map)
            logp = write_solver_log(outp, stats)
            msg = f"Creato: {outp}\nSolver: {stats.get('status')}"
            if logp:
                msg += f"\nLog: {logp}"
            messagebox.showinfo("OK", msg)
        except Exception as e:
            messagebox.showerror("Errore", str(e))
    frm = tk.Frame(root, padx=10, pady=10)
    frm.pack(fill="both", expand=True)
    def row(label, var, btn_text, cmd, r):
        tk.Label(frm, text=label, anchor="w").grid(row=r, column=0, sticky="w")
        tk.Entry(frm, textvariable=var, width=60).grid(row=r, column=1, padx=5)
        tk.Button(frm, text=btn_text, command=cmd).grid(row=r, column=2)
    row("Template turni (.xlsx)", template_var, "Scegli...", lambda: pick_file(template_var,[("Excel","*.xlsx")]), 0)
    row("Regole (.yml)", rules_var, "Scegli...", lambda: pick_file(rules_var,[("YAML","*.yml;*.yaml")]), 1)
    row("Indisponibilità (opz.)", unav_var, "Scegli...", lambda: pick_file(unav_var,[("Excel/CSV","*.xlsx;*.xls;*.csv;*.tsv")]), 2)
    row("Output (.xlsx)", out_var, "Salva come...", lambda: pick_save(out_var), 3)
    tk.Button(frm, text="Genera turni", command=go, height=2).grid(row=4, column=0, columnspan=3, pady=10, sticky="ew")
    root.mainloop()
# -------------------------
# Main
# -------------------------
def main():
    ap = argparse.ArgumentParser(description="Turni Autogenerator (UTIC/Cardiologia)")
    ap.add_argument("--template", type=str, help="Template Excel .xlsx")
    ap.add_argument("--rules", type=str, help="Regole YAML .yml/.yaml")
    ap.add_argument("--unavailability", type=str, default="", help="Indisponibilità mensili .xlsx/.csv (opzionale)")
    ap.add_argument("--out", type=str, help="Output Excel .xlsx")
    ap.add_argument("--sheet", type=str, default="", help="Nome foglio (opzionale)")
    ap.add_argument("--gui", action="store_true", help="Avvia interfaccia grafica (Tkinter)")
    args = ap.parse_args()
    if args.gui or (not args.template and not args.rules):
        run_gui()
        return
    if not args.template or not args.rules or not args.out:
        ap.error("In modalità CLI devi specificare: --template, --rules, --out")
    template = Path(args.template)
    rules = Path(args.rules)
    outp = Path(args.out)
    unav = Path(args.unavailability) if args.unavailability.strip() else None
    cfg = load_rules(rules)
    wb, ws, days = load_template_days(template, sheet_name=args.sheet if args.sheet else None)
    unav_map = load_unavailability(unav)
    carryover_by_month = None  # CLI currently has no carryover input
    slots, assignment, stats = solve_across_months(cfg, days, unav_map, carryover_by_month=carryover_by_month)
    # If any month fell back to greedy, print it
    greedy_months = [k for k,v in (stats.get('months') or {}).items() if isinstance(v, dict) and v.get('solver_error')]
    if greedy_months:
        print("[WARN] OR-Tools non disponibile o infeasible per:", ", ".join(greedy_months), file=sys.stderr)
    write_output(wb, ws, days, slots, assignment, outp, unav_map=unav_map)
    logp = write_solver_log(outp, stats)
    if logp:
        print(f"OK: creato {outp} | solver={stats.get('status')} | log={logp}")
    else:
        print(f"OK: creato {outp} | solver={stats.get('status')}")
if __name__ == "__main__":
    main()