import io
import tempfile
import time
import traceback
from datetime import date, datetime
from pathlib import Path
from collections.abc import Mapping

import streamlit as st
import yaml

# Local modules
import github_utils
import unavailability_store as ustore
import xlsx_utils

# Import generator
import turni_generator as tg


st.set_page_config(page_title="Turni UTIC – Autogeneratore", layout="wide")

DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "Regole_Turni.yml"
DEFAULT_STYLE_TEMPLATE = Path(__file__).resolve().parent / "Style_Template.xlsx"
DEFAULT_UNAV_TEMPLATE = Path(__file__).resolve().parent / "unavailability_template.xlsx"

# -------- Secrets helpers --------
def _get_secret(path, default=None):
    """Safely read Streamlit secrets with nested keys.

    Parameters
    ----------
    path: tuple[str, ...]
        e.g. ("auth", "admin_pin") or ("ADMIN_PIN",)
    default: any
        returned if the key path does not exist
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

def _get_admin_pin():
    # primary: [auth] admin_pin ; fallback: ADMIN_PIN
    return str(_get_secret(("auth","admin_pin"), _get_secret(("ADMIN_PIN",), "")) or "")

def _get_doctor_pins():
    pins = _get_secret(("doctor_pins",), None)
    if isinstance(pins, Mapping):
        return {str(k): str(v) for k,v in pins.items()}
    # allow JSON string in secrets
    pins_json = _get_secret(("DOCTOR_PINS_JSON",), "")
    if pins_json:
        try:
            d = yaml.safe_load(pins_json)
            if isinstance(d, Mapping):
                return {str(k): str(v) for k,v in d.items()}
        except Exception:
            pass
    return {}

def _github_cfg():
    cfg = _get_secret(("github_unavailability",), None)
    if isinstance(cfg, Mapping):
        return cfg
    # fallback flat keys
    return {
        "token": _get_secret(("GITHUB_UNAV_TOKEN",), ""),
        "owner": _get_secret(("GITHUB_UNAV_OWNER",), ""),
        "repo": _get_secret(("GITHUB_UNAV_REPO",), ""),
        "branch": _get_secret(("GITHUB_UNAV_BRANCH",), "main"),
        "path": _get_secret(("GITHUB_UNAV_PATH",), "data/unavailability_store.csv"),
    }

# -------- Load rules / doctor list --------
def load_rules_from_source(uploaded) -> tuple[dict, Path]:
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

# -------- GitHub datastore ops --------
def load_store_from_github() -> tuple[list[dict], str | None]:
    g = _github_cfg()
    if not (g.get("token") and g.get("owner") and g.get("repo") and g.get("path")):
        raise RuntimeError("GitHub unavailability secrets not configured.")
    gf = github_utils.get_file(g["owner"], g["repo"], g["path"], g["token"], branch=g.get("branch","main"))
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
        branch=g.get("branch","main"),
        sha=sha,
        message=message,
        text=text,
    )

# -------- UI --------
st.title("Turni UTIC – Autogeneratore")

mode = st.sidebar.radio("Sezione", ["Genera turni (Admin)", "Indisponibilità (Medico)"], index=0)

# Regole: caricate di default dal repository (NON modificabili dai medici)
rules_upload = None
cfg, rules_path = load_rules_from_source(None)
doctors = doctors_from_cfg(cfg)

if mode == "Indisponibilità (Medico)":
    st.subheader("Inserisci le tue indisponibilità (privacy)")
    pins = _get_doctor_pins()
    if not pins:
        st.error("PIN medici non configurati in secrets (doctor_pins).")
        st.stop()

    col1, col2 = st.columns([2,1])
    with col1:
        doctor = st.selectbox("Seleziona il tuo nome", doctors, index=0)
    with col2:
        pin = st.text_input("PIN", type="password")

    expected = str(pins.get(doctor,""))
    if not pin or pin != expected:
        st.info("Inserisci il PIN corretto per accedere al modulo.")
        st.stop()

    # month selection
    today = date.today()
    default_month = today.month
    default_year = today.year
    months = [(y,m) for y in range(default_year, default_year+2) for m in range(1,13)]
    # show next 6 months
    upcoming=[]
    for i in range(0,6):
        mm = (default_month-1+i)%12 + 1
        yy = default_year + ((default_month-1+i)//12)
        upcoming.append((yy,mm))
    label_map = { (yy,mm): f"{yy}-{mm:02d}" for yy,mm in upcoming }
    selected = st.multiselect("Mesi da compilare", options=upcoming, default=[upcoming[0]], format_func=lambda x: label_map[x])

    try:
        store_rows, store_sha = load_store_from_github()
    except Exception as e:
        st.error(f"Errore accesso archivio indisponibilità: {e}")
        st.stop()

    tabs = st.tabs([label_map[x] for x in selected])
    edited_by_month = {}

    for (yy,mm), tab in zip(selected, tabs):
        with tab:
            existing = ustore.filter_doctor_month(store_rows, doctor, yy, mm)
            # Build initial df-like list
            init = []
            for r in existing:
                try:
                    d = datetime.fromisoformat(r["date"]).date()
                except Exception:
                    d = r["date"]
                init.append({"Data": d, "Fascia": r["shift"], "Note": r.get("note","")})
            if not init:
                init=[{"Data": date(yy,mm,1), "Fascia": "Mattina", "Note": ""}]
            edited = st.data_editor(
                init,
                num_rows="dynamic",
                use_container_width=True,
                column_config={
                    "Data": st.column_config.DateColumn("Data"),
                    "Fascia": st.column_config.SelectboxColumn("Fascia", options=list(ustore.VALID_SHIFTS)),
                    "Note": st.column_config.TextColumn("Note"),
                },
                key=f"ed_{doctor}_{yy}_{mm}",
            )
            edited_by_month[(yy,mm)] = edited

    if st.button("Salva indisponibilità selezionate", type="primary"):
        new_rows = store_rows
        for (yy,mm), edited in edited_by_month.items():
            entries=[]
            for r in edited:
                d = r.get("Data")
                if isinstance(d, datetime):
                    d = d.date()
                if not isinstance(d, date):
                    continue
                sh = str(r.get("Fascia","")).strip()
                note = str(r.get("Note","") or "")
                entries.append((d, sh, note))
            new_rows = ustore.replace_doctor_month(new_rows, doctor, yy, mm, entries)
        try:
            save_store_to_github(new_rows, store_sha, message=f"Update unavailability: {doctor}")
            st.success("Salvato ✅")
        except Exception as e:
            st.error(f"Errore salvataggio su GitHub: {e}")
            st.info("Cause più comuni del 404: (1) token senza accesso alla repo privata (fine-grained: Repository access deve includere la repo; Contents: Read and write), (2) owner/repo/branch/path errati, (3) token non autorizzato SSO (se repo in Organization).")
    st.caption("Nota: puoi modificare solo le tue indisponibilità; le indisponibilità degli altri non sono visibili.")

else:
    # Admin
    st.subheader("Generazione turni (Admin)")
    admin_pin = _get_admin_pin()
    if not admin_pin:
        st.error("Admin PIN non configurato in secrets (auth.admin_pin).")
        st.stop()
    pin = st.text_input("PIN Admin", type="password")
    if pin != admin_pin:
        st.info("Inserisci il PIN Admin per accedere.")
        st.stop()

    # Regole: solo Admin può caricare una YAML alternativa
    st.sidebar.markdown("### Regole (solo Admin)")
    rules_upload = st.sidebar.file_uploader("Carica Regole YAML (opzionale)", type=["yml","yaml"])
    if rules_upload is not None:
        cfg, rules_path = load_rules_from_source(rules_upload)
        doctors = doctors_from_cfg(cfg)
    

    # Month/year
    today = date.today()
    colA, colB, colC = st.columns([1,1,2])
    with colA:
        year = st.number_input("Anno", min_value=2025, max_value=2035, value=today.year, step=1)
    with colB:
        month = st.number_input("Mese", min_value=1, max_value=12, value=today.month, step=1)
    mk = f"{int(year)}-{int(month):02d}"

    # Template source
    st.markdown("### Template Excel")
    template_upload = st.file_uploader("Carica template turni (opzionale)", type=["xlsx"])
    style_upload = st.file_uploader("Carica Style_Template.xlsx (opzionale)", type=["xlsx"])
    sheet_name = st.text_input("Nome foglio (opzionale)", value="")

    # Carryover
    st.markdown("### Carryover mese precedente (NOTTE ultimo giorno → blocco giorno 1)")
    prev_out = st.file_uploader("Carica output mese precedente (opzionale)", type=["xlsx"], key="prev")
    manual_block = st.multiselect("Oppure seleziona manualmente medico/i da bloccare il giorno 1", doctors, default=[])

    carryover_by_month = {}
    carry_info = None
    if prev_out is not None:
        tmp_prev = Path(tempfile.gettempdir()) / f"prev_{int(time.time())}.xlsx"
        tmp_prev.write_bytes(prev_out.getvalue())
        try:
            carry_info = tg.extract_carryover_from_output_xlsx(tmp_prev, sheet_name=sheet_name or None, night_col_letter="J", min_gap=int(cfg.get("global_constraints",{}).get("night_spacing_days_min",5)))
            carryover_by_month[mk] = carry_info
            st.success(f"Letto carryover: ultima data {carry_info.get('source_last_date')} | NOTTE ultimo giorno: {carry_info.get('night_last_day_doctor')}")
        except Exception as e:
            st.error(f"Errore lettura carryover: {e}")

    if manual_block:
        carryover_by_month.setdefault(mk, {})
        carryover_by_month[mk].setdefault("blocked_day1_doctors", [])
        for d in manual_block:
            if d not in carryover_by_month[mk]["blocked_day1_doctors"]:
                carryover_by_month[mk]["blocked_day1_doctors"].append(d)

    # Unavailability source
    st.markdown("### Indisponibilità")
    unav_mode = st.radio("Fonte indisponibilità", ["Nessuna", "Carica file manuale", "Usa archivio (privacy)"], horizontal=True)
    unav_upload = None
    if unav_mode == "Carica file manuale":
        unav_upload = st.file_uploader("Carica indisponibilità (xlsx/csv/tsv)", type=["xlsx","csv","tsv"])
    use_archive = (unav_mode == "Usa archivio (privacy)")

    if st.button("Genera turni", type="primary"):
        t0 = time.time()
        with st.spinner("Generazione in corso…"):
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                # rules path (resolved from upload or default)
                rules_path_use = rules_path
                # template path
                if template_upload is not None:
                    template_path = td / "template.xlsx"
                    template_path.write_bytes(template_upload.getvalue())
                else:
                    # create auto template
                    style_path = td / "Style_Template.xlsx"
                    if style_upload is not None:
                        style_path.write_bytes(style_upload.getvalue())
                    else:
                        style_path = DEFAULT_STYLE_TEMPLATE if DEFAULT_STYLE_TEMPLATE.exists() else None
                    template_path = td / f"turni_{mk}.xlsx"
                    tg.create_month_template_xlsx(rules_path_use, int(year), int(month), out_path=template_path, sheet_name=sheet_name or None)

                # rules path
                # unav path
                unav_path = None
                if unav_mode == "Carica file manuale" and unav_upload is not None:
                    unav_path = td / "unavailability.xlsx"
                    unav_path.write_bytes(unav_upload.getvalue())
                elif use_archive:
                    store_rows, _sha = load_store_from_github()
                    rows_month = ustore.filter_month(store_rows, int(year), int(month))
                    unav_path = td / "unavailability_from_store.xlsx"
                    xlsx_utils.build_unavailability_xlsx(rows_month, DEFAULT_UNAV_TEMPLATE, unav_path)

                out_path = td / f"output_{mk}.xlsx"
                try:
                    stats, log_path = tg.generate_schedule(
                        template_xlsx=template_path,
                        rules_yml=rules_path_use,
                        out_xlsx=out_path,
                        unavailability_path=unav_path,
                        sheet_name=sheet_name or None,
                        carryover_by_month=carryover_by_month if carryover_by_month else None,
                    )
                except Exception as e:
                    st.error("Errore durante la generazione.")
                    st.code(traceback.format_exc())
                    st.stop()

                # download
                data = out_path.read_bytes()
                st.success(f"Creato ✅ in {round(time.time()-t0,2)}s | status={stats.get('status')}")
                st.download_button("Scarica Excel", data=data, file_name=f"turni_{mk}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                if log_path and Path(log_path).exists():
                    st.download_button("Scarica solver log", data=Path(log_path).read_bytes(), file_name=f"solverlog_{mk}.txt", mime="text/plain")

                # quick report
                st.markdown("### Report rapido")
                try:
                    wb = tg.openpyxl.load_workbook(out_path, data_only=True)
                    ws = wb.active if not sheet_name else wb[sheet_name] if sheet_name in wb.sheetnames else wb.active
                    # assume header row 1, data rows start at 2
                    headers = [ws.cell(1,c).value for c in range(1, ws.max_column+1)]
                    # choose some key columns to count
                    col_map = {h: i+1 for i,h in enumerate(headers) if isinstance(h,str) and h.strip()}
                    # Always include column letters known in your template
                    # Count occurrences per column for doctors
                    counts={}
                    for c in range(3, ws.max_column+1):
                        h = ws.cell(1,c).value
                        if h is None:
                            continue
                        colname = str(h)
                        cnt={}
                        for r in range(2, ws.max_row+1):
                            v=ws.cell(r,c).value
                            if not v:
                                continue
                            v=str(v).strip()
                            cnt[v]=cnt.get(v,0)+1
                        if cnt:
                            counts[colname]=cnt
                    # show C counts if exists
                    if "Reperibilità" in counts:
                        st.write("**Reperibilità (C)**", counts["Reperibilità"])
                    else:
                        # show first column that looks like 'C'
                        pass
                except Exception:
                    st.caption("Report rapido non disponibile (template non standard).")
