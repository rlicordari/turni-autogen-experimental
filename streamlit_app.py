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

APP_BUILD = "2026-01-23-ui-v2"


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

    st.divider()

    tabs = st.tabs([label_map[x] for x in selected])
    edited_by_month = {}

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
            edited_by_month[(yy, mm)] = edited

    c1, c2 = st.columns([1, 2])
    with c1:
        save = st.button("Salva indisponibilità", type="primary")
    with c2:
        st.caption("Privacy: salviamo solo le righe del tuo nominativo nei mesi selezionati.")

    if save:
        new_rows = list(store_rows)
        for (yy, mm), edited in edited_by_month.items():
            entries = []
            for r in edited:
                d = r.get("Data")
                if isinstance(d, datetime):
                    d = d.date()
                if not isinstance(d, date):
                    continue
                sh_raw = r.get("Fascia", "")
                sh, _changed, _unknown = normalize_fascia(sh_raw).strip()
                note = str(r.get("Note", "") or "")
                if not sh:
                    continue
                entries.append((d, sh, note))
            new_rows = ustore.replace_doctor_month(new_rows, doctor, yy, mm, entries)

        try:
            save_store_to_github(new_rows, store_sha, message=f"Update unavailability: {doctor}")
            st.success("Salvato ✅")
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

                # Download outputs
                data = out_path.read_bytes()
                st.success(f"Creato ✅ in {round(time.time() - t0, 2)}s | status={stats.get('status')}")
                st.download_button(
                    "⬇️ Scarica Excel turni",
                    data=data,
                    file_name=f"turni_{mk}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                if log_path and Path(log_path).exists():
                    st.download_button(
                        "⬇️ Scarica solver log",
                        data=Path(log_path).read_bytes(),
                        file_name=f"solverlog_{mk}.txt",
                        mime="text/plain",
                    )

                # Quick, user-friendly quality panel
                st.markdown("### Controlli rapidi")
                k1, k2, k3 = st.columns(3)
                with k1:
                    st.markdown(f'<div class="kpi"><b>Solver</b><br>{stats.get("status","?")}</div>', unsafe_allow_html=True)
                with k2:
                    cdiag = stats.get("C_reperibilita_diag") if isinstance(stats, dict) else None
                    msg = "OK" if (isinstance(cdiag, dict) and cdiag.get("status","").startswith("OK")) else "Controllare"
                    st.markdown(f'<div class="kpi"><b>Reperibilità (C)</b><br>{msg}</div>', unsafe_allow_html=True)
                with k3:
                    blocked = (carryover_by_month.get(mk, {}) or {}).get("blocked_day1_doctors", [])
                    st.markdown(f'<div class="kpi"><b>Carryover</b><br>{len(blocked)} bloccati Giorno 1</div>', unsafe_allow_html=True)

                if isinstance(stats, dict) and stats.get("C_reperibilita_diag"):
                    with st.expander("Dettagli Reperibilità (C)"):
                        st.json(stats["C_reperibilita_diag"])

        except Exception:
            status.update(label="Errore ❌", state="error")
            st.error("Errore durante la generazione.")
            st.code(traceback.format_exc())
