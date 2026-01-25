import io
import tempfile
import time
import traceback
import csv
import json
import requests
from datetime import date, datetime
from pathlib import Path
from collections.abc import Mapping

import streamlit as st
import pandas as pd
import yaml

# Local modules
import github_utils
import unavailability_store as ustore
import xlsx_utils

# Import generator
import turni_generator as tg

APP_BUILD = "2026-01-25-ui-v6"


# ---- Indisponibilità: fasce ammesse e normalizzazione (per compatibilità con valori "storici") ----
FASCIA_OPTIONS = ["Mattina", "Pomeriggio", "Notte", "Diurno", "Tutto il giorno"]

def normalize_fascia(val: object) -> tuple[str, bool, bool]:
    """Return (canonical_value, changed, unknown).

    - changed: value was recognized but normalized (e.g., 'matt' -> 'Mattina')
    - unknown: value wasn't recognized; we default to 'Tutto il giorno' but we warn the user.
    """
    if val is None:
        return "", False, False
    s = str(val).strip()
    if not s:
        return "", False, False
    key = s.casefold().strip()
    key = " ".join(key.split())  # collapse whitespace

    # direct matches (case-insensitive)
    direct = {
        "mattina": "Mattina",
        "pomeriggio": "Pomeriggio",
        "notte": "Notte",
        "diurno": "Diurno",
        "tutto il giorno": "Tutto il giorno",
        "tutto giorno": "Tutto il giorno",
        "all day": "Tutto il giorno",
        "giornata intera": "Tutto il giorno",
    }
    if key in direct:
        canon = direct[key]
        return canon, canon != s, False

    # fuzzy matches
    if "tutto" in key or "all" in key or "intera" in key:
        return "Tutto il giorno", True, False
    if "diurn" in key or "daytime" in key or key == "d":
        return "Diurno", True, False
    if "matt" in key or "morning" in key or key in {"am", "a.m."}:
        return "Mattina", True, False
    if "pome" in key or "pom" in key or "afternoon" in key or key in {"pm", "p.m."}:
        return "Pomeriggio", True, False
    if "nott" in key or "night" in key or key == "n":
        return "Notte", True, False

    # unknown
    return "Tutto il giorno", True, True
# ---------------- Page config & style ----------------
st.set_page_config(
    page_title="Turni UTIC – Autogeneratore",
    page_icon="🗓️",
    layout="wide",
)

st.markdown(
    """
<style>
/* Tidy up spacing */
.block-container { padding-top: 1.2rem; padding-bottom: 2.5rem; }
h1 { margin-bottom: 0.2rem; }
 .small-muted { opacity: 0.75; font-size: 0.92rem; }
.kpi { padding: 0.75rem 0.9rem; border-radius: 0.75rem; border: 1px solid rgba(128, 128, 128, 0.25); }
.kpi b { font-size: 1.05rem; }
hr { margin: 0.9rem 0; }
</style>
""",
    unsafe_allow_html=True,
)

# Build / version banner
with st.sidebar:
    st.caption(f"Build: {APP_BUILD} | tg={getattr(tg, '__version__', '?')}")
    try:
        st.caption(f"tg file: {Path(tg.__file__).name}")
    except Exception:
        pass

DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "Regole_Turni.yml"
DEFAULT_STYLE_TEMPLATE = Path(__file__).resolve().parent / "Style_Template.xlsx"
DEFAULT_UNAV_TEMPLATE = Path(__file__).resolve().parent / "unavailability_template.xlsx"

# ---------------- Secrets helpers ----------------
def _get_secret(path, default=None):
    """Safely read Streamlit secrets with nested keys.

    path: tuple[str, ...] e.g. ("auth","admin_pin") or ("ADMIN_PIN",)
    """
    cur = st.secrets
    for p in path:
        try:
            if isinstance(cur, Mapping) and p in cur:
                cur = cur[p]
            else:
                return default
        except Exception:
            return default
    return cur

def _get_admin_pin() -> str:
    # primary: [auth] admin_pin ; fallback: ADMIN_PIN
    return str(_get_secret(("auth", "admin_pin"), _get_secret(("ADMIN_PIN",), "")) or "")

def _get_doctor_pins() -> dict[str, str]:
    pins = _get_secret(("doctor_pins",), None)
    if isinstance(pins, Mapping):
        return {str(k): str(v) for k, v in pins.items()}
    pins_json = _get_secret(("DOCTOR_PINS_JSON",), "")
    if pins_json:
        try:
            d = yaml.safe_load(pins_json)
            if isinstance(d, Mapping):
                return {str(k): str(v) for k, v in d.items()}
        except Exception:
            pass
    return {}

def _github_cfg() -> dict:
    cfg = _get_secret(("github_unavailability",), None)
    if isinstance(cfg, Mapping):
        return dict(cfg)
    # fallback flat keys
    return {
        "token": _get_secret(("GITHUB_UNAV_TOKEN",), ""),
        "owner": _get_secret(("GITHUB_UNAV_OWNER",), ""),
        "repo": _get_secret(("GITHUB_UNAV_REPO",), ""),
        "branch": _get_secret(("GITHUB_UNAV_BRANCH",), "main"),
        "path": _get_secret(("GITHUB_UNAV_PATH",), "data/unavailability_store.csv"),
        "settings_path": _get_secret(("GITHUB_UNAV_SETTINGS_PATH",), "data/unavailability_settings.yml"),
        "audit_dir": _get_secret(("GITHUB_UNAV_AUDIT_DIR",), "data/unavailability_audit"),
    }

# ---------------- Rules / doctors ----------------
def load_rules_from_source(uploaded) -> tuple[dict, Path]:
    """Return (cfg, rules_path)."""
    if uploaded is None:
        return tg.load_rules(DEFAULT_RULES_PATH), DEFAULT_RULES_PATH
    tmp = Path(tempfile.gettempdir()) / f"rules_{int(time.time())}.yml"
    tmp.write_bytes(uploaded.getvalue())
    return tg.load_rules(tmp), tmp

def doctors_from_cfg(cfg: dict) -> list[str]:
    try:
        return tg.collect_doctors(cfg)
    except Exception:
        return sorted(set((cfg.get("doctors") or [])))

# ---------------- GitHub datastore ops ----------------
def load_store_from_github() -> tuple[list[dict], str | None]:
    g = _github_cfg()
    if not (g.get("token") and g.get("owner") and g.get("repo") and g.get("path")):
        raise RuntimeError("Archivio indisponibilità: secrets GitHub non configurati.")
    gf = github_utils.get_file(
        owner=g["owner"],
        repo=g["repo"],
        path=g["path"],
        token=g["token"],
        branch=g.get("branch", "main"),
    )
    if gf is None:
        return [], None
    return ustore.load_store(gf.text), gf.sha

def save_store_to_github(rows: list[dict], sha: str | None, message: str):
    g = _github_cfg()
    text = ustore.to_csv(rows)
    github_utils.put_file(
        owner=g["owner"],
        repo=g["repo"],
        path=g["path"],
        token=g["token"],
        branch=g.get("branch", "main"),
        sha=sha,
        message=message,
        text=text,
    )


# ---------------- GitHub settings & audit log ----------------
DEFAULT_SETTINGS = {
    "unavailability_open": True,
    "max_unavailability_per_shift": 6,
}

AUDIT_FIELDS = [
    "ts_utc",
    "doctor",
    "month",
    "action",
    "before_count",
    "after_count",
    "added_count",
    "removed_count",
    "note_changed_count",
    "details_json",
    "app_build",
]

def load_app_settings_from_github() -> tuple[dict, str | None]:
    """Load app settings (toggle unavailability entry + max per shift) from GitHub.

    If the settings file doesn't exist yet, returns defaults.
    """
    g = _github_cfg()
    path = g.get("settings_path") or "data/unavailability_settings.yml"
    gf = github_utils.get_file(
        owner=g["owner"],
        repo=g["repo"],
        path=path,
        token=g["token"],
        branch=g.get("branch", "main"),
    )
    if gf is None:
        # Defaults (no file yet)
        return dict(DEFAULT_SETTINGS), None

    try:
        data = yaml.safe_load(gf.text) or {}
    except Exception:
        data = {}

    if not isinstance(data, Mapping):
        data = {}

    out = dict(DEFAULT_SETTINGS)

    # allow some legacy key names
    if "unavailability_open" in data:
        out["unavailability_open"] = bool(data.get("unavailability_open"))
    elif "unavailability_enabled" in data:
        out["unavailability_open"] = bool(data.get("unavailability_enabled"))
    elif "open" in data:
        out["unavailability_open"] = bool(data.get("open"))

    try:
        out["max_unavailability_per_shift"] = int(
            data.get("max_unavailability_per_shift", data.get("max_per_shift", DEFAULT_SETTINGS["max_unavailability_per_shift"]))
        )
    except Exception:
        out["max_unavailability_per_shift"] = DEFAULT_SETTINGS["max_unavailability_per_shift"]

    # optional metadata
    out["updated_at"] = str(data.get("updated_at") or "")
    out["updated_by"] = str(data.get("updated_by") or "")

    # defensive bounds
    if out["max_unavailability_per_shift"] < 0:
        out["max_unavailability_per_shift"] = 0

    return out, gf.sha

def save_app_settings_to_github(settings: dict, sha: str | None, message: str):
    g = _github_cfg()
    path = g.get("settings_path") or "data/unavailability_settings.yml"
    # Write as YAML for readability
    text = yaml.safe_dump(settings, sort_keys=False, allow_unicode=True)
    github_utils.put_file(
        owner=g["owner"],
        repo=g["repo"],
        path=path,
        token=g["token"],
        branch=g.get("branch", "main"),
        sha=sha,
        message=message,
        text=text,
    )

def _audit_path_for_month(mk: str) -> str:
    g = _github_cfg()
    audit_dir = g.get("audit_dir") or "data/unavailability_audit"
    return f"{audit_dir}/unavailability_audit_{mk}.csv"


@st.cache_data(ttl=60)
def load_audit_log_text_from_github(mk: str) -> str | None:
    """Return monthly audit log CSV text (or None if missing)."""
    g = _github_cfg()
    path = _audit_path_for_month(mk)
    gf = github_utils.get_file(
        owner=g["owner"],
        repo=g["repo"],
        path=path,
        token=g["token"],
        branch=g.get("branch", "main"),
    )
    return gf.text if gf else None


def audit_df_to_excel_bytes(df: pd.DataFrame, sheet_name: str = "audit") -> bytes:
    """Convert an audit dataframe to an .xlsx in-memory."""
    buf = io.BytesIO()
    # Use openpyxl engine (already in requirements)
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name[:31] or "audit")
    return buf.getvalue()

def _entries_map_from_store_rows(rows: list[dict]) -> dict[tuple[str, str], str]:
    """Map: (date_iso, shift) -> note"""
    m: dict[tuple[str, str], str] = {}
    for r in rows:
        try:
            d = ustore.parse_iso_date(r.get("date", ""))
        except Exception:
            continue
        sh = ustore.norm_shift(r.get("shift", ""))
        if not sh:
            continue
        m[(d.isoformat(), sh)] = str(r.get("note") or "")
    return m

def _entries_map_from_tuples(entries: list[tuple[date, str, str]]) -> dict[tuple[str, str], str]:
    """Map: (date_iso, shift) -> note"""
    m: dict[tuple[str, str], str] = {}
    for d, sh, note in entries:
        if not isinstance(d, date):
            continue
        sh2 = ustore.norm_shift(sh)
        if not sh2:
            continue
        m[(d.isoformat(), sh2)] = str(note or "")
    return m

def compute_unavailability_diff(existing_rows: list[dict], new_entries: list[tuple[date, str, str]]) -> dict:
    """Return a diff summary between current store rows and the edited entries."""
    before = _entries_map_from_store_rows(existing_rows)
    after = _entries_map_from_tuples(new_entries)

    before_keys = set(before.keys())
    after_keys = set(after.keys())

    added = sorted(after_keys - before_keys)
    removed = sorted(before_keys - after_keys)

    note_changed = []
    for k in sorted(before_keys & after_keys):
        if str(before.get(k, "")) != str(after.get(k, "")):
            note_changed.append(k)

    # Privacy-friendly details: we log only (date, shift), not free-text notes.
    details = {
        "added": [{"date": k[0], "shift": k[1]} for k in added],
        "removed": [{"date": k[0], "shift": k[1]} for k in removed],
        "note_changed": [{"date": k[0], "shift": k[1]} for k in note_changed],
    }

    return {
        "before_count": len(before_keys),
        "after_count": len(after_keys),
        "added_count": len(added),
        "removed_count": len(removed),
        "note_changed_count": len(note_changed),
        "details": details,
    }

def append_unavailability_audit_log(mk: str, row: dict, max_retries: int = 3):
    """Append a row to the monthly audit log on GitHub.

    Uses optimistic concurrency (sha) and retries on conflicts.
    """
    g = _github_cfg()
    path = _audit_path_for_month(mk)

    last_err = None
    for _attempt in range(max_retries):
        gf = github_utils.get_file(
            owner=g["owner"],
            repo=g["repo"],
            path=path,
            token=g["token"],
            branch=g.get("branch", "main"),
        )
        existing_text = gf.text if gf else ""
        sha = gf.sha if gf else None

        # Build row line with proper CSV quoting
        row_buf = io.StringIO()
        writer = csv.DictWriter(row_buf, fieldnames=AUDIT_FIELDS)
        writer.writerow({k: row.get(k, "") for k in AUDIT_FIELDS})
        row_line = row_buf.getvalue().strip("\r\n")

        if existing_text.strip():
            # If header is missing/unknown, rebuild from scratch
            first = existing_text.splitlines()[0].strip()
            expected_header = ",".join(AUDIT_FIELDS)
            if first != expected_header:
                buf = io.StringIO()
                w = csv.DictWriter(buf, fieldnames=AUDIT_FIELDS)
                w.writeheader()
                buf.write(existing_text.strip("\n") + "\n")
                buf.write(row_line + "\n")
                new_text = buf.getvalue()
            else:
                new_text = existing_text.rstrip("\n") + "\n" + row_line + "\n"
        else:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=AUDIT_FIELDS)
            w.writeheader()
            buf.write(row_line + "\n")
            new_text = buf.getvalue()

        try:
            github_utils.put_file(
                owner=g["owner"],
                repo=g["repo"],
                path=path,
                token=g["token"],
                branch=g.get("branch", "main"),
                sha=sha,
                message=f"Audit unavailability {mk}: {row.get('doctor','')}",
                text=new_text,
            )
            return  # OK
        except requests.HTTPError as e:
            last_err = e
            # retry on sha mismatch / conflict
            resp = getattr(e, "response", None)
            if resp is not None and getattr(resp, "status_code", None) in (409, 412):
                continue
            raise
        except Exception as e:
            last_err = e
            raise

    if last_err:
        raise last_err

def extract_entries_from_editor(edited_rows: list[dict], yy: int, mm: int) -> tuple[list[tuple[date, str, str]], dict]:
    """Normalize and validate editor rows for a specific (yy,mm).

    Returns (entries, info) where entries is a list of (date, shift, note),
    de-duplicated by (date, shift).
    """
    entries: list[tuple[date, str, str]] = []
    invalid_date = 0
    out_of_month = 0

    for r in edited_rows or []:
        d = r.get("Data")
        if isinstance(d, datetime):
            d = d.date()
        if not isinstance(d, date):
            invalid_date += 1
            continue
        if d.year != int(yy) or d.month != int(mm):
            out_of_month += 1
            continue

        sh_raw = r.get("Fascia", "")
        sh, _changed, _unknown = normalize_fascia(sh_raw)
        sh = (sh or "").strip()
        if not sh:
            continue

        note = str(r.get("Note", "") or "")
        entries.append((d, sh, note))

    # de-duplicate by (date, shift): keep last note
    dedup: dict[tuple[date, str], str] = {}
    for d, sh, note in entries:
        dedup[(d, sh)] = note
    entries2 = [(d, sh, note) for (d, sh), note in dedup.items()]

    counts = {}
    for _d, sh, _n in entries2:
        counts[sh] = counts.get(sh, 0) + 1

    return entries2, {"invalid_date": invalid_date, "out_of_month": out_of_month, "counts": counts}


# ---------------- UI: Header ----------------
st.title("Turni UTIC – Autogeneratore")
st.markdown(
    '<div class="small-muted">Genera il file turni del mese rispettando regole e indisponibilità. '
    'I medici possono inserire solo le <b>proprie</b> indisponibilità (privacy).</div>',
    unsafe_allow_html=True,
)

mode = st.sidebar.radio(
    "Sezione",
    ["Genera turni (Admin)", "Indisponibilità (Medico)"],
    index=0,
)

# Load default rules (for doctor list)
cfg_default = tg.load_rules(DEFAULT_RULES_PATH)
doctors_default = doctors_from_cfg(cfg_default)

# =====================================================================
#                        MEDICO – Indisponibilità
# =====================================================================
if mode == "Indisponibilità (Medico)":
    st.subheader("Indisponibilità (Medico)")
    st.write(
        "Compila le tue indisponibilità per uno o più mesi. "
        "Le indisponibilità degli altri non sono visibili."
    )

    pins = _get_doctor_pins()
    if not pins:
        st.error("PIN medici non configurati in secrets (doctor_pins).")
        st.stop()

    # ---- Session state (evita che l'app 'torni alla home' ad ogni modifica) ----
    if "doctor_auth_ok" not in st.session_state:
        st.session_state.doctor_auth_ok = False
        st.session_state.doctor_name = None

    if st.session_state.doctor_auth_ok:
        st.success(f"Accesso attivo: **{st.session_state.doctor_name}**")
        if st.button("Esci / cambia medico"):
            st.session_state.doctor_auth_ok = False
            st.session_state.doctor_name = None
            st.session_state.pop("doctor_selected_months", None)
            # cancella anche eventuali editor keys (non obbligatorio)
            st.rerun()

    if not st.session_state.doctor_auth_ok:
        with st.form("medico_login", clear_on_submit=False):
            col1, col2 = st.columns([2, 1])
            with col1:
                doctor = st.selectbox("1) Seleziona il tuo nome", doctors_default, index=0, key="login_doctor")
            with col2:
                pin = st.text_input("2) PIN", type="password", key="login_pin", help="PIN personale a 4 cifre")
            go = st.form_submit_button("Accedi", type="primary")

        if go:
            expected = str(pins.get(doctor, ""))
            if pin and pin == expected:
                st.session_state.doctor_auth_ok = True
                st.session_state.doctor_name = doctor
                st.rerun()
            else:
                st.error("PIN non valido. Controlla il PIN e riprova.")

        st.stop()

    doctor = st.session_state.doctor_name

    # ---- Selezione mesi da compilare (Anno + Mese separati) ----
    today = date.today()
    horizon_years = 20  # ampia finestra per evitare modifiche future
    year_options = list(range(today.year, today.year + horizon_years + 1))
    month_names = {
        1: "Gennaio", 2: "Febbraio", 3: "Marzo", 4: "Aprile", 5: "Maggio", 6: "Giugno",
        7: "Luglio", 8: "Agosto", 9: "Settembre", 10: "Ottobre", 11: "Novembre", 12: "Dicembre",
    }

    sel_default = st.session_state.get("doctor_selected_months") or [(today.year, today.month)]
    sel_set = set(sel_default)

    st.subheader("3) Seleziona mese/i da compilare")
    c1, c2, c3, c4 = st.columns([1, 1.4, 1, 1])
    with c1:
        yy_sel = st.selectbox("Anno", year_options, index=0, key="doctor_year_sel")
    with c2:
        mm_sel = st.selectbox(
            "Mese",
            list(range(1, 13)),
            format_func=lambda m: f"{m:02d} - {month_names.get(m, str(m))}",
            key="doctor_month_sel",
        )
    with c3:
        add_month = st.button("Aggiungi", use_container_width=True, help="Aggiunge l’anno/mese selezionato all’elenco.")
    with c4:
        remove_month = st.button("Rimuovi", use_container_width=True, help="Rimuove l’anno/mese selezionato dall’elenco.")

    cur = (int(yy_sel), int(mm_sel))
    if add_month:
        sel_set.add(cur)
    if remove_month:
        sel_set.discard(cur)

    selected = sorted(sel_set)
    st.session_state.doctor_selected_months = selected

    st.caption("Mesi selezionati: " + ", ".join([f"{yy}-{mm:02d}" for (yy, mm) in selected]))
    if not selected:
        st.info("Aggiungi almeno un mese per iniziare.")
        st.stop()

    label_map = {(yy, mm): f"{yy}-{mm:02d}" for (yy, mm) in selected}

    # Load store after auth (so we don't hit GitHub before login)
    try:
        store_rows, store_sha = load_store_from_github()
    except Exception as e:
        st.error(f"Errore accesso archivio indisponibilità: {e}")
        st.stop()

    # Load app settings (open/closed + limits)
    try:
        app_settings, _settings_sha = load_app_settings_from_github()
    except Exception as e:
        app_settings, _settings_sha = dict(DEFAULT_SETTINGS), None
        st.warning(f"Impostazioni indisponibilità non leggibili (uso default): {e}")

    unav_open = bool(app_settings.get("unavailability_open", True))
    try:
        max_per_shift = int(app_settings.get("max_unavailability_per_shift", DEFAULT_SETTINGS["max_unavailability_per_shift"]))
    except Exception:
        max_per_shift = DEFAULT_SETTINGS["max_unavailability_per_shift"]
    if max_per_shift < 0:
        max_per_shift = 0

    if not unav_open:
        st.warning("🔒 Inserimento indisponibilità temporaneamente **chiuso** dall'amministratore. Puoi solo visualizzare (non puoi salvare).")
    st.caption(
        f"Limite per medico: **max {max_per_shift}** inserimenti per ogni fascia "
        "(Mattina/Pomeriggio/Notte/Diurno/Tutto il giorno) per ogni mese."
    )

    st.divider()

    tabs = st.tabs([label_map[x] for x in selected])
    edited_by_month = {}
    normalized_entries_by_month = {}
    violations_by_month = {}
    info_by_month = {}

    for (yy, mm), tab in zip(selected, tabs):
        with tab:
            st.caption("Inserisci righe con Data + Fascia. Le righe vuote verranno ignorate.")
            existing = ustore.filter_doctor_month(store_rows, doctor, yy, mm)
            init = []
            conversions = []
            for r in existing:
                try:
                    d = datetime.fromisoformat(r["date"]).date()
                except Exception:
                    d = r["date"]
                raw_shift = r.get("shift", "")
                canon_shift, changed, unknown = normalize_fascia(raw_shift)
                if changed:
                    conversions.append({
                        "Data": d,
                        "Fascia_originale": raw_shift,
                        "Fascia_impostata": canon_shift,
                        "Nota": "Non riconosciuta (default applicato)" if unknown else "Normalizzata",
                    })
                init.append({"Data": d, "Fascia": canon_shift or "Tutto il giorno", "Note": r.get("note", "")})

            if conversions:
                st.warning("Abbiamo trovato alcune fasce non standard salvate in passato. Le abbiamo normalizzate automaticamente: controlla e, se necessario, modifica dal menu a tendina prima di salvare.")
                st.dataframe(conversions, use_container_width=True, hide_index=True)


            if not init:
                init = [{"Data": date(yy, mm, 1), "Fascia": "Mattina", "Note": ""}]

            if unav_open:
                edited = st.data_editor(
                    init,
                    num_rows="dynamic",
                    use_container_width=True,
                    column_config={
                        "Data": st.column_config.DateColumn("Data", required=True),
                        "Fascia": st.column_config.SelectboxColumn("Fascia", options=FASCIA_OPTIONS, required=True),
                        "Note": st.column_config.TextColumn("Note"),
                    },
                    key=f"unav_editor_{doctor}_{yy}_{mm}",
                )
            else:
                # Read-only view when the admin closes submissions
                st.dataframe(init, use_container_width=True, hide_index=True)
                edited = init

            edited_by_month[(yy, mm)] = edited

            # Normalize & validate + enforce max per shift (per month)
            entries_norm, info = extract_entries_from_editor(edited, yy, mm)
            normalized_entries_by_month[(yy, mm)] = entries_norm
            info_by_month[(yy, mm)] = info

            counts = info.get("counts", {}) or {}
            over = {sh: n for sh, n in counts.items() if n > max_per_shift}
            violations_by_month[(yy, mm)] = over

            if info.get("out_of_month"):
                st.warning(
                    f"⚠️ {info['out_of_month']} righe con data fuori mese sono state ignorate "
                    f"(devono essere in {yy}-{mm:02d})."
                )
            if info.get("invalid_date"):
                st.warning(f"⚠️ {info['invalid_date']} righe hanno una data non valida e sono state ignorate.")

            st.caption(
                "Conteggi mese (per fascia): "
                + ", ".join([f"{sh} {counts.get(sh, 0)}/{max_per_shift}" for sh in FASCIA_OPTIONS])
            )

            if over:
                pretty = ", ".join([f"{sh}: {n}/{max_per_shift}" for sh, n in over.items()])
                st.error(f"Limite superato in questo mese → {pretty}. Rimuovi alcune righe prima di salvare.")

    any_over = any(bool(v) for v in (violations_by_month or {}).values())
    can_save = bool(unav_open) and (not any_over)

    c1, c2 = st.columns([1, 2])
    with c1:
        save = st.button("Salva indisponibilità", type="primary", disabled=not can_save)
    with c2:
        st.caption("Privacy: salviamo solo le righe del tuo nominativo nei mesi selezionati.")

    if save:
        if not unav_open:
            st.error("Inserimento indisponibilità chiuso dall'amministratore: non è possibile salvare.")
            st.stop()

        # Server-side re-check (in caso di race / rerun)
        hard_viol = []
        for (yy, mm), entries_norm in (normalized_entries_by_month or {}).items():
            counts = {}
            for _d, sh, _n in entries_norm:
                counts[sh] = counts.get(sh, 0) + 1
            over = {sh: n for sh, n in counts.items() if n > max_per_shift}
            if over:
                hard_viol.append(
                    f"{yy}-{mm:02d}: " + ", ".join([f"{sh} {n}/{max_per_shift}" for sh, n in over.items()])
                )

        if hard_viol:
            st.error(
                "Impossibile salvare: limite indisponibilità superato.\n\n"
                + "\n".join([f"- {x}" for x in hard_viol])
            )
            st.stop()

        updated_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        new_rows = list(store_rows)

        audit_todo: list[tuple[str, dict]] = []
        for (yy, mm), entries_norm in (normalized_entries_by_month or {}).items():
            existing_rows = ustore.filter_doctor_month(store_rows, doctor, int(yy), int(mm))
            diff = compute_unavailability_diff(existing_rows, entries_norm)
            if diff.get("added_count") or diff.get("removed_count") or diff.get("note_changed_count"):
                audit_todo.append((f"{int(yy)}-{int(mm):02d}", diff))

            new_rows = ustore.replace_doctor_month(
                new_rows, doctor, int(yy), int(mm), entries_norm, updated_at=updated_at
            )

        try:
            save_store_to_github(new_rows, store_sha, message=f"Update unavailability: {doctor} ({updated_at})")

            # Monthly audit log (best-effort)
            for mk_audit, diff in audit_todo:
                audit_row = {
                    "ts_utc": updated_at,
                    "doctor": doctor,
                    "month": mk_audit,
                    "action": "save",
                    "before_count": diff.get("before_count", 0),
                    "after_count": diff.get("after_count", 0),
                    "added_count": diff.get("added_count", 0),
                    "removed_count": diff.get("removed_count", 0),
                    "note_changed_count": diff.get("note_changed_count", 0),
                    "details_json": json.dumps(diff.get("details", {}), ensure_ascii=False),
                    "app_build": APP_BUILD,
                }
                try:
                    append_unavailability_audit_log(mk_audit, audit_row)
                except Exception as e:
                    st.warning(f"Audit log non aggiornato per {mk_audit}: {e}")

            st.success("Salvato ✅")
            st.rerun()
        except Exception as e:
            st.error(f"Errore salvataggio su GitHub: {e}")
            st.info(
                "Se vedi 404: (1) token senza accesso alla repo privata, "
                "(2) owner/repo/branch/path errati, (3) token non autorizzato SSO (se repo in Organization)."
            )


# =====================================================================
#                           ADMIN – Generazione
# =====================================================================
else:
    st.subheader("Generazione turni (Admin)")
    admin_pin = _get_admin_pin()
    if not admin_pin:
        st.error("Admin PIN non configurato in secrets (auth.admin_pin).")
        st.stop()

    # Persist admin auth across reruns
    if "admin_auth_ok" not in st.session_state:
        st.session_state.admin_auth_ok = False

    if not st.session_state.admin_auth_ok:
        with st.form("admin_login"):
            pin = st.text_input("PIN Admin", type="password")
            ok = st.form_submit_button("Sblocca area Admin", type="primary")

        if not ok:
            st.stop()
        if pin != admin_pin:
            st.error("PIN Admin errato.")
            st.stop()

        st.session_state.admin_auth_ok = True
        # Rerun to avoid re-submitting the form on next widget interaction
        st.rerun()

    col_logout, col_status = st.columns([1, 3])
    with col_logout:
        if st.button("Esci (Admin)", help="Chiude la sessione Admin su questo browser."):
            st.session_state.admin_auth_ok = False
            st.rerun()
    with col_status:
        st.success("Area Admin sbloccata ✅")

    # ---- Admin settings: open/close unavailability + limits ----
    with st.expander("⚙️ Impostazioni indisponibilità (Admin)", expanded=True):
        try:
            app_settings, app_settings_sha = load_app_settings_from_github()
        except Exception as e:
            app_settings, app_settings_sha = dict(DEFAULT_SETTINGS), None
            st.warning(f"Impossibile leggere impostazioni da GitHub (uso default): {e}")

        cur_open = bool(app_settings.get("unavailability_open", True))
        try:
            cur_max = int(app_settings.get("max_unavailability_per_shift", DEFAULT_SETTINGS["max_unavailability_per_shift"]))
        except Exception:
            cur_max = DEFAULT_SETTINGS["max_unavailability_per_shift"]
        if cur_max < 0:
            cur_max = 0

        cS1, cS2, cS3 = st.columns([1.4, 1, 2])
        with cS1:
            new_open = st.toggle(
                "Consenti ai medici di inserire/modificare indisponibilità",
                value=cur_open,
                help="Se disattivato, i medici possono solo visualizzare le proprie indisponibilità ma non salvarle.",
            )
        with cS2:
            new_max = st.number_input(
                "Max per fascia (per mese)",
                min_value=0,
                max_value=31,
                value=int(cur_max),
                step=1,
                help="Esempio: 6 significa max 6 Mattine, 6 Pomeriggi, 6 Notti, ecc. per ogni mese.",
            )
        with cS3:
            meta = ""
            if app_settings.get("updated_at"):
                meta += f"Ultimo aggiornamento: {app_settings.get('updated_at')}"
            if app_settings.get("updated_by"):
                meta += f" | da: {app_settings.get('updated_by')}"
            if meta:
                st.caption(meta)

        if st.button("Salva impostazioni indisponibilità", type="primary"):
            settings_to_save = {
                "unavailability_open": bool(new_open),
                "max_unavailability_per_shift": int(new_max),
                "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "updated_by": "admin",
            }
            try:
                save_app_settings_to_github(
                    settings_to_save,
                    app_settings_sha,
                    message=f"Update unavailability settings: open={bool(new_open)} max={int(new_max)}",
                )
                st.success("Impostazioni salvate ✅")
                st.rerun()
            except Exception as e:
                st.error(f"Errore salvataggio impostazioni su GitHub: {e}")

    st.divider()

    # Step 1: Periodo
    st.markdown("### 1) Periodo")
    today = date.today()
    colA, colB, colC = st.columns([1, 1, 2])
    with colA:
        year = st.number_input("Anno", min_value=2025, max_value=2035, value=today.year, step=1)
    with colB:
        month = st.number_input("Mese", min_value=1, max_value=12, value=today.month, step=1)
    mk = f"{int(year)}-{int(month):02d}"
    st.caption(f"Stai generando: **{mk}**")

    # ---- Admin: download / inspect monthly audit log (unavailability edits) ----
    with st.expander("📜 Log inserimenti/modifiche indisponibilità (Audit)", expanded=False):
        st.write(
            "Questo log registra chi ha inserito/modificato indisponibilità, con timestamp e conteggi. "
            "È utile per tracciare le modifiche mese per mese."
        )

        cL1, cL2, cL3 = st.columns([1, 1, 2])
        with cL1:
            audit_year = st.number_input(
                "Anno log",
                min_value=2025,
                max_value=2035,
                value=int(year),
                step=1,
                key="audit_year",
            )
        with cL2:
            audit_month = st.number_input(
                "Mese log",
                min_value=1,
                max_value=12,
                value=int(month),
                step=1,
                key="audit_month",
            )
        with cL3:
            mk_log = f"{int(audit_year)}-{int(audit_month):02d}"
            st.caption(f"Log selezionato: **{mk_log}**")

        try:
            audit_text = load_audit_log_text_from_github(mk_log)
        except Exception as e:
            audit_text = None
            st.error(f"Errore lettura audit log da GitHub: {e}")

        if not audit_text or not str(audit_text).strip():
            st.info(
                "Nessun audit log trovato per questo mese (nessuna modifica registrata oppure file non ancora creato)."
            )
        else:
            # Download buttons (full file)
            st.download_button(
                "⬇️ Scarica audit log (CSV)",
                data=str(audit_text).encode("utf-8"),
                file_name=f"unavailability_audit_{mk_log}.csv",
                mime="text/csv",
                key=f"dl_audit_csv_{mk_log}",
            )

            # Parse for preview + optional Excel export
            try:
                df_audit = pd.read_csv(io.StringIO(audit_text))
            except Exception as e:
                df_audit = None
                st.error("Il file audit log esiste ma non riesco a leggerlo come CSV.")
                st.code(str(e))

            if df_audit is not None and not df_audit.empty:
                try:
                    # Sort newest first if possible
                    if "ts_utc" in df_audit.columns:
                        df_audit = df_audit.sort_values("ts_utc", ascending=False)
                except Exception:
                    pass

                # Optional doctor filter for on-screen preview
                doctor_filter = "Tutti"
                if "doctor" in df_audit.columns:
                    doctors_in_log = sorted(
                        [str(x) for x in df_audit["doctor"].dropna().unique().tolist() if str(x).strip()]
                    )
                    doctor_filter = st.selectbox(
                        "Filtro medico (solo anteprima)",
                        ["Tutti"] + doctors_in_log,
                        index=0,
                        key=f"audit_filter_{mk_log}",
                    )

                df_preview = df_audit
                if doctor_filter != "Tutti" and "doctor" in df_audit.columns:
                    df_preview = df_audit[df_audit["doctor"] == doctor_filter]

                # Excel export
                try:
                    xlsx_bytes = audit_df_to_excel_bytes(df_audit, sheet_name=f"audit_{mk_log}")
                    st.download_button(
                        "⬇️ Scarica audit log (Excel)",
                        data=xlsx_bytes,
                        file_name=f"unavailability_audit_{mk_log}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=f"dl_audit_xlsx_{mk_log}",
                    )
                except Exception as e:
                    st.warning(f"Esportazione Excel non disponibile: {e}")

                st.markdown("**Anteprima**")
                st.dataframe(df_preview.head(200), use_container_width=True, hide_index=True)
                st.caption("Mostro al massimo 200 righe. Per analisi completa usa il download.")

    # Step 2: Indisponibilità
    st.markdown("### 2) Indisponibilità")
    unav_mode = st.radio(
        "Fonte indisponibilità",
        ["Nessuna", "Carica file manuale", "Usa archivio (privacy)"],
        horizontal=True,
        help="Puoi caricare un file manuale, oppure usare l’archivio compilato dai medici.",
    )
    unav_upload = None
    if unav_mode == "Carica file manuale":
        unav_upload = st.file_uploader("Carica indisponibilità (xlsx/csv/tsv)", type=["xlsx", "csv", "tsv"])
    use_archive = (unav_mode == "Usa archivio (privacy)")

    # Step 3: Vincolo post-notte (carryover)
    st.markdown("### 3) Vincolo post-notte a cavallo mese")
    st.info(
        "Serve solo se qualcuno ha fatto **NOTTE l’ultimo giorno del mese precedente**: "
        "quella persona **non può lavorare il Giorno 1** del mese corrente.\n\n"
        "✅ Consigliato: carica l’**output del mese precedente**.\n"
        "🔁 Alternativa: seleziona manualmente chi ha fatto la NOTTE.",
        icon="💡",
    )

    # Admin advanced (rules/template/carryover file)
    with st.expander("⚙️ Avanzate (Regole, Template, Carryover file)", expanded=False):
        st.markdown("**Regole (solo Admin)**")
        rules_upload = st.file_uploader("Carica Regole YAML (opzionale)", type=["yml", "yaml"])
        cfg_admin, rules_path = load_rules_from_source(rules_upload)
        doctors = doctors_from_cfg(cfg_admin)

        st.markdown("**Template Excel**")
        template_upload = st.file_uploader("Carica template turni (opzionale)", type=["xlsx"])
        style_upload = st.file_uploader("Carica Style_Template.xlsx (opzionale)", type=["xlsx"])
        sheet_name = st.text_input("Nome foglio (opzionale)", value="")

        st.markdown("**Carryover – file mese precedente (opzionale)**")
        prev_out = st.file_uploader("Carica output mese precedente", type=["xlsx"], key="prev")

    # If advanced not expanded, still need cfg_admin/doctors variables
    if "cfg_admin" not in locals():
        cfg_admin, rules_path = tg.load_rules(DEFAULT_RULES_PATH), DEFAULT_RULES_PATH
        doctors = doctors_from_cfg(cfg_admin)
        template_upload = None
        style_upload = None
        sheet_name = ""

        prev_out = None

    manual_block = st.multiselect(
        "Seleziona medico/i da bloccare il Giorno 1 (se non carichi l’output precedente)",
        doctors,
        default=[],
        help="Inserisci qui chi ha fatto NOTTE l’ultimo giorno del mese precedente.",
    )

    carryover_by_month = {}
    carry_info = None

    # From file
    if prev_out is not None:
        tmp_prev = Path(tempfile.gettempdir()) / f"prev_{int(time.time())}.xlsx"
        tmp_prev.write_bytes(prev_out.getvalue())
        try:
            carry_info = tg.extract_carryover_from_output_xlsx(
                tmp_prev,
                sheet_name=sheet_name or None,
                night_col_letter="J",
                min_gap=int((cfg_admin.get("global_constraints") or {}).get("night_spacing_days_min", 5)),
            )
            carryover_by_month[mk] = carry_info
            st.success(
                f"Carryover letto: ultima data {carry_info.get('source_last_date')} | "
                f"NOTTE ultimo giorno: {carry_info.get('night_last_day_doctor')}"
            )
        except Exception as e:
            st.error(f"Errore lettura carryover: {e}")

    # Manual fallback
    if manual_block:
        carryover_by_month.setdefault(mk, {})
        carryover_by_month[mk].setdefault("blocked_day1_doctors", [])
        for d in manual_block:
            if d not in carryover_by_month[mk]["blocked_day1_doctors"]:
                carryover_by_month[mk]["blocked_day1_doctors"].append(d)

    st.divider()

    # Generate button
    generate = st.button("🚀 Genera turni", type="primary")

    if generate:
        t0 = time.time()
        status = st.status("Preparazione…", expanded=True)
        try:
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                rules_path_use = rules_path

                status.update(label="Preparazione template…", state="running")
                if template_upload is not None:
                    template_path = td / "template.xlsx"
                    template_path.write_bytes(template_upload.getvalue())
                else:
                    # Auto template
                    if style_upload is not None:
                        style_path = td / "Style_Template.xlsx"
                        style_path.write_bytes(style_upload.getvalue())
                    else:
                        style_path = DEFAULT_STYLE_TEMPLATE if DEFAULT_STYLE_TEMPLATE.exists() else None
                    template_path = td / f"turni_{mk}.xlsx"
                    tg.create_month_template_xlsx(
                        rules_path_use,
                        int(year),
                        int(month),
                        out_path=template_path,
                        sheet_name=sheet_name or None,
                    )

                status.update(label="Carico indisponibilità…", state="running")
                unav_path = None
                if unav_mode == "Carica file manuale" and unav_upload is not None:
                    unav_path = td / "unavailability.xlsx"
                    unav_path.write_bytes(unav_upload.getvalue())
                elif use_archive:
                    store_rows, _sha = load_store_from_github()
                    rows_month = ustore.filter_month(store_rows, int(year), int(month))
                    unav_path = td / "unavailability_from_store.xlsx"
                    xlsx_utils.build_unavailability_xlsx(rows_month, DEFAULT_UNAV_TEMPLATE, unav_path)
                    st.caption(f"Archivio indisponibilità: {len(rows_month)} righe per {mk}")

                status.update(label="Generazione turni…", state="running")
                out_path = td / f"output_{mk}.xlsx"
                stats, log_path = tg.generate_schedule(
                    template_xlsx=template_path,
                    rules_yml=rules_path_use,
                    out_xlsx=out_path,
                    unavailability_path=unav_path,
                    sheet_name=sheet_name or None,
                    carryover_by_month=carryover_by_month if carryover_by_month else None,
                )

                status.update(label="Completato ✅", state="complete")

                # Persist outputs in session_state so that download clicks do not
                # "lose" the generated files (Streamlit re-runs the script on
                # every widget interaction).
                excel_bytes = out_path.read_bytes()
                log_bytes = None
                if log_path and Path(log_path).exists():
                    log_bytes = Path(log_path).read_bytes()

                st.session_state["last_generated"] = {
                    "mk": mk,
                    "excel_bytes": excel_bytes,
                    "log_bytes": log_bytes,
                    "stats": stats,
                    "elapsed_s": round(time.time() - t0, 2),
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                }

        except Exception:
            status.update(label="Errore ❌", state="error")
            st.error("Errore durante la generazione.")
            st.code(traceback.format_exc())

    # Downloads + summary (sticky): if a file was generated for this month, keep
    # the buttons visible even after clicking one of them.
    last = st.session_state.get("last_generated")
    if isinstance(last, dict) and last.get("mk") == mk and last.get("excel_bytes"):
        _stats = last.get("stats") if isinstance(last.get("stats"), dict) else {}
        st.success(
            f"Creato ✅ in {last.get('elapsed_s')}s | status={_stats.get('status')} | {last.get('generated_at','')}"
        )

        st.download_button(
            "⬇️ Scarica Excel turni",
            data=last["excel_bytes"],
            file_name=f"turni_{mk}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl_xlsx_{mk}",
        )
        if last.get("log_bytes"):
            st.download_button(
                "⬇️ Scarica solver log",
                data=last["log_bytes"],
                file_name=f"solverlog_{mk}.txt",
                mime="text/plain",
                key=f"dl_log_{mk}",
            )

        # Quick, user-friendly quality panel
        st.markdown("### Controlli rapidi")
        k1, k2, k3 = st.columns(3)
        with k1:
            st.markdown(
                f'<div class="kpi"><b>Solver</b><br>{_stats.get("status","?")}</div>',
                unsafe_allow_html=True,
            )
        with k2:
            cdiag = _stats.get("C_reperibilita_diag") if isinstance(_stats, dict) else None
            msg = "OK" if (isinstance(cdiag, dict) and cdiag.get("status", "").startswith("OK")) else "Controllare"
            st.markdown(
                f'<div class="kpi"><b>Reperibilità (C)</b><br>{msg}</div>',
                unsafe_allow_html=True,
            )
        with k3:
            blocked = (carryover_by_month.get(mk, {}) or {}).get("blocked_day1_doctors", [])
            st.markdown(
                f'<div class="kpi"><b>Carryover</b><br>{len(blocked)} bloccati Giorno 1</div>',
                unsafe_allow_html=True,
            )

        if isinstance(_stats, dict) and _stats.get("C_reperibilita_diag"):
            with st.expander("Dettagli Reperibilità (C)"):
                st.json(_stats["C_reperibilita_diag"])
