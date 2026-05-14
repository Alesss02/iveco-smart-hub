import ast
import base64
import datetime as dt
import json
import os
import random
import time
from io import BytesIO
from typing import Dict, List, Optional, Tuple

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import qrcode
import streamlit as st
from PIL import Image

# =====================================================================================
# 1. CONFIGURAZIONE GENERALE
# =====================================================================================
STORE_FILE = "iveco_unified_store.csv"

CATEGORIE = ["Motore", "Elettronica", "Freni", "Cambio"]
TECNICI = ["Marco", "Luca", "Antonio", "Davide", "Roberto"]
RICAMBI_CATALOGO = ["Olio 5W30", "Filtro Aria", "Pasticche Freni", "Cinghia", "Sensore NOx"]
URGENZE = ["ALTA", "MEDIA", "BASSA"]
STATI = ["In Attesa", "In Corso", "Sospeso", "Completato"]
REWORK_CAUSES = [
    "Nessuna",
    "Errore diagnosi",
    "Ricambio errato",
    "Montaggio non conforme",
    "Test finale fallito",
    "Guasto ricorrente",
    "Rientro cliente",
]

COSTO_ORARIO_OFFICINA = 70
LIMITE_ORE_GIORNO = 8.5
SOGLIA_OVERLOAD_WARNING = 1.00
SOGLIA_OVERLOAD_CRITICAL = 1.15
DECADIMENTO_VALUE = 0.05
GROWTH_STAGE_1 = 0.15
GROWTH_STAGE_2 = 0.05
MIN_SKILL_MENTOR = 4.5
MAX_SKILL_APPRENTICE = 3.0
PERC_AFFIANCAMENTO = 0.30
BONUS_TEACHING = 0.01
PROB_REWORK = 0.05
COEFF_REWORK_TIME = 1.4
MOLTIPLICATORE_REWORK_ECO = 1.5
SLA_DELAY_TOLERANCE_H = 2.0

IMPATTO_BASE = {
    "Motore": {"Olio_L": 5.0, "Scarti_Kg": 2.0, "Energia_kWh": 5.0},
    "Elettronica": {"Olio_L": 0.0, "Scarti_Kg": 0.5, "Energia_kWh": 2.5},
    "Freni": {"Olio_L": 0.5, "Scarti_Kg": 4.0, "Energia_kWh": 2.0},
    "Cambio": {"Olio_L": 4.0, "Scarti_Kg": 3.0, "Energia_kWh": 4.0},
}
CO2_FATTORI = {"Olio": 2.8, "Scarti": 1.5, "Energia": 0.4}

CHECKLIST_DATA = {
    "Motore": [
        {"task": "Serraggio Testata coppia specifica", "risk": "ALTO", "eco": False, "critical": True},
        {"task": "Verifica Efficienza Termica", "risk": "BASSO", "eco": True, "critical": False},
    ],
    "Cambio": [
        {"task": "Tolleranze Ingranaggi", "risk": "ALTO", "eco": False, "critical": True},
        {"task": "Recupero Olio Trasmissione", "risk": "MEDIO", "eco": True, "critical": False},
    ],
    "Freni": [
        {"task": "Misurazione Spessore Dischi", "risk": "ALTO", "eco": False, "critical": True},
        {"task": "Spurgo Impianto", "risk": "ALTO", "eco": False, "critical": True},
    ],
    "Elettronica": [
        {"task": "Scansione Errori DTC", "risk": "MEDIO", "eco": False, "critical": True},
        {"task": "Reset Parametri Adattativi", "risk": "BASSO", "eco": True, "critical": False},
    ],
}

st.set_page_config(page_title="IVECO Integrated Workshop Hub", layout="wide", page_icon="🧬")

# =====================================================================================
# 2. STILE
# =====================================================================================
st.markdown(
    """
    <style>
        .block-container {padding-top: 1.1rem; padding-bottom: 1.4rem;}
        .main-title {
            padding: 1rem 1.2rem;
            border-radius: 18px;
            background: linear-gradient(90deg, rgba(11,35,84,1) 0%, rgba(29,78,216,1) 55%, rgba(14,165,233,1) 100%);
            color: white;
            margin-bottom: 1rem;
            box-shadow: 0 10px 25px rgba(15, 23, 42, 0.15);
        }
        .section-card {
            border: 1px solid rgba(148, 163, 184, 0.25);
            background: rgba(248, 250, 252, 0.65);
            padding: 0.85rem 1rem;
            border-radius: 16px;
            margin-bottom: 0.75rem;
        }
        .small-note {font-size: 0.90rem; color: #475569;}
        .alert-card {
            border-left: 6px solid #f59e0b;
            background: rgba(255, 247, 237, 0.85);
            padding: 0.8rem 1rem;
            border-radius: 14px;
            margin-bottom: 0.6rem;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

for key, default in {
    "candidate_table": None,
    "candidate_context": None,
    "last_created_job_id": None,
    "public_base_url_input": "",
    "wo_veicolo": "",
    "wo_categoria": CATEGORIE[0],
    "wo_urgenza": URGENZE[0],
    "wo_planned_date": local_now().date() if "local_now" in globals() else dt.datetime.now().date(),
    "wo_ore_std": 3.0,
    "wo_fermo": True,
    "wo_safety": False,
    "wo_ricambi": True,
    "wo_sla": 24.0,
    "wo_prev_ricambi": 0.0,
    "wo_note": "",
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


# =====================================================================================
# 3. DATABASE CSV UNIFICATO
# =====================================================================================
def utc_now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat()


def local_now() -> dt.datetime:
    return dt.datetime.now()


def normalize_base_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url.rstrip("/")


def infer_runtime_base_url() -> str:
    host = "localhost"
    if hasattr(st, "context") and hasattr(st.context, "headers"):
        host = st.context.headers.get("Host", "localhost")
        forwarded_proto = st.context.headers.get("X-Forwarded-Proto") or st.context.headers.get("x-forwarded-proto")
        if forwarded_proto:
            protocol = forwarded_proto.split(",")[0].strip()
        else:
            protocol = "https" if "localhost" not in host and "127.0.0.1" not in host else "http"
    else:
        protocol = "http"
    return f"{protocol}://{host}".rstrip("/")


def get_public_base_url() -> str:
    secret_url = ""
    try:
        secret_url = st.secrets.get("PUBLIC_BASE_URL", "")
    except Exception:
        secret_url = ""
    env_url = os.getenv("PUBLIC_BASE_URL", "")
    session_url = st.session_state.get("public_base_url_input", "")
    configured = normalize_base_url(secret_url) or normalize_base_url(env_url) or normalize_base_url(session_url)
    return configured or infer_runtime_base_url()


def build_operator_url(job_id: str) -> str:
    return f"{get_public_base_url()}?bolla_id={job_id}"


def reset_new_job_form() -> None:
    st.session_state["wo_veicolo"] = ""
    st.session_state["wo_categoria"] = CATEGORIE[0]
    st.session_state["wo_urgenza"] = URGENZE[0]
    st.session_state["wo_planned_date"] = local_now().date()
    st.session_state["wo_ore_std"] = 3.0
    st.session_state["wo_fermo"] = True
    st.session_state["wo_safety"] = False
    st.session_state["wo_ricambi"] = True
    st.session_state["wo_sla"] = 24.0
    st.session_state["wo_prev_ricambi"] = 0.0
    st.session_state["wo_note"] = ""


def initial_skill_profiles() -> Dict[str, dict]:
    return {
        "Marco": {"Tecnico": "Marco", "Motore": 5.0, "Elettronica": 5.0, "Freni": 3.0, "Cambio": 4.0, "Teaching_Score": 0.0},
        "Luca": {"Tecnico": "Luca", "Motore": 4.0, "Elettronica": 5.0, "Freni": 2.0, "Cambio": 2.0, "Teaching_Score": 0.0},
        "Antonio": {"Tecnico": "Antonio", "Motore": 3.0, "Elettronica": 1.0, "Freni": 5.0, "Cambio": 3.0, "Teaching_Score": 0.0},
        "Davide": {"Tecnico": "Davide", "Motore": 1.0, "Elettronica": 2.0, "Freni": 2.0, "Cambio": 1.0, "Teaching_Score": 0.0},
        "Roberto": {"Tecnico": "Roberto", "Motore": 2.0, "Elettronica": 3.0, "Freni": 4.0, "Cambio": 5.0, "Teaching_Score": 0.0},
    }


def make_row(entity_type: str, entity_id: str, data: dict, created_at: Optional[str] = None) -> dict:
    now = utc_now_iso()
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "created_at": created_at or now,
        "updated_at": now,
        "data_json": json.dumps(data, ensure_ascii=False),
    }


def bootstrap_store() -> None:
    rows = [make_row("technician", tech, payload) for tech, payload in initial_skill_profiles().items()]
    rows.append(make_row("meta", "system", {"version": "Integrated 2.0", "description": "Store unificato CSV per officina + DSS + planning + audit"}))
    rows.append(make_row("audit", f"AUD-{int(time.time())}", {"event": "bootstrap", "details": "Inizializzazione database unificato", "ts": utc_now_iso()}))
    pd.DataFrame(rows).to_csv(STORE_FILE, index=False)


def load_store_df() -> pd.DataFrame:
    if not os.path.exists(STORE_FILE):
        bootstrap_store()
    try:
        df = pd.read_csv(STORE_FILE)
        required = ["entity_type", "entity_id", "created_at", "updated_at", "data_json"]
        for col in required:
            if col not in df.columns:
                raise ValueError("Formato store non valido")
        return df.fillna("")
    except Exception:
        backup = f"{STORE_FILE}.corrupted_{int(time.time())}"
        if os.path.exists(STORE_FILE):
            os.replace(STORE_FILE, backup)
        bootstrap_store()
        return pd.read_csv(STORE_FILE).fillna("")


def save_store_df(df: pd.DataFrame) -> None:
    df.to_csv(STORE_FILE, index=False)


def parse_records(df: pd.DataFrame, entity_type: str) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    filtered = df[df["entity_type"] == entity_type]
    for _, row in filtered.iterrows():
        try:
            out[row["entity_id"]] = json.loads(row["data_json"])
        except Exception:
            out[row["entity_id"]] = {}
    return out


def upsert_entity(entity_type: str, entity_id: str, data: dict) -> None:
    df = load_store_df()
    mask = (df["entity_type"] == entity_type) & (df["entity_id"] == entity_id)
    if mask.any():
        created = df.loc[mask, "created_at"].iloc[0]
        df = df.loc[~mask].copy()
        df = pd.concat([df, pd.DataFrame([make_row(entity_type, entity_id, data, created_at=created)])], ignore_index=True)
    else:
        df = pd.concat([df, pd.DataFrame([make_row(entity_type, entity_id, data)])], ignore_index=True)
    save_store_df(df)


def append_entity(entity_type: str, entity_id: str, data: dict) -> None:
    df = load_store_df()
    df = pd.concat([df, pd.DataFrame([make_row(entity_type, entity_id, data)])], ignore_index=True)
    save_store_df(df)


def add_audit(event: str, details: str, extra: Optional[dict] = None) -> None:
    append_entity("audit", f"AUD-{int(time.time()*1000)}-{random.randint(100,999)}", {"event": event, "details": details, "ts": utc_now_iso(), "extra": extra or {}})


def reload_data() -> Tuple[pd.DataFrame, Dict[str, dict], Dict[str, dict], Dict[str, dict], Dict[str, dict]]:
    df = load_store_df()
    return df, parse_records(df, "technician"), parse_records(df, "work_order"), parse_records(df, "skill_snapshot"), parse_records(df, "audit")


# =====================================================================================
# 4. UTILITY E MOTORE DSS
# =====================================================================================
def encode_img(upload) -> str:
    img = Image.open(upload)
    img.thumbnail((900, 900))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def get_qr(url: str) -> bytes:
    qr = qrcode.make(url)
    buf = BytesIO()
    qr.save(buf, format="PNG")
    return buf.getvalue()


def iso_week_str(value: str) -> str:
    try:
        d = dt.datetime.fromisoformat(value)
    except Exception:
        d = local_now()
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def get_days_elapsed_in_week(ref: Optional[dt.datetime] = None) -> int:
    ref = ref or local_now()
    return max(1, min(5, ref.isoweekday()))


def get_week_capacity(days_elapsed: Optional[int] = None) -> float:
    days_elapsed = days_elapsed or get_days_elapsed_in_week()
    return round(LIMITE_ORE_GIORNO * days_elapsed, 2)


def assess_overload(projected_week_hours: float, days_elapsed: Optional[int] = None) -> dict:
    days_elapsed = days_elapsed or get_days_elapsed_in_week()
    capacity = get_week_capacity(days_elapsed)
    ratio = projected_week_hours / capacity if capacity > 0 else 0.0
    if ratio >= SOGLIA_OVERLOAD_CRITICAL:
        status, color = "CRITICO", "red"
    elif ratio >= SOGLIA_OVERLOAD_WARNING:
        status, color = "ATTENZIONE", "orange"
    else:
        status, color = "OK", "green"
    return {"capacity_h": round(capacity, 2), "projected_h": round(projected_week_hours, 2), "ratio": round(ratio, 3), "status": status, "color": color}


def safe_date_str(value, fallback: Optional[dt.date] = None) -> str:
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value[:10]
    return (fallback or local_now().date()).isoformat()


def format_hours(v) -> float:
    try:
        return round(float(v), 2)
    except Exception:
        return 0.0


def technician_skill_df(tech_profiles: Dict[str, dict]) -> pd.DataFrame:
    rows = []
    for tech in TECNICI:
        rows.append(tech_profiles.get(tech, initial_skill_profiles()[tech]))
    df = pd.DataFrame(rows)
    for col in CATEGORIE + ["Teaching_Score"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return df


def work_orders_df(work_orders: Dict[str, dict]) -> pd.DataFrame:
    if not work_orders:
        return pd.DataFrame()
    df = pd.DataFrame.from_dict(work_orders, orient="index").reset_index(drop=True)
    numeric_cols = [
        "tempo_sec", "ore_std", "ore_eff_previste", "ore_eff_consuntive", "roi_eur", "co2_kg", "exergia_kwh",
        "olio_l", "scarti_kg", "energia_kwh", "skill_corrente", "preventivo_manodopera_eur", "consuntivo_manodopera_eur",
        "preventivo_totale_eur", "consuntivo_totale_eur", "preventivo_ricambi_eur", "consuntivo_ricambi_eur",
        "priority_score", "final_checklist_pct", "carico_proiettato_h", "carico_attuale_h", "capacity_reference_h",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    for col in ["checklist_state", "ricambi_usati"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: x if isinstance(x, (dict, list)) else safe_literal_eval(x, {} if col == "checklist_state" else []))
    return df


def get_weekly_hours_map(work_df: pd.DataFrame, week: Optional[str] = None) -> Dict[str, float]:
    week = week or iso_week_str(utc_now_iso())
    m = {t: 0.0 for t in TECNICI}
    if work_df.empty:
        return m
    week_df = work_df[work_df["week_id"] == week].copy()
    for _, row in week_df.iterrows():
        tech = row.get("tecnico")
        if tech in m:
            if row.get("stato_lavoro") == "Completato":
                m[tech] += float(row.get("ore_eff_consuntive", row.get("tempo_sec", 0) / 3600))
            else:
                m[tech] += float(row.get("ore_eff_previste", row.get("ore_std", 0)))
    return m


def get_daily_load_map(work_df: pd.DataFrame, target_date: str) -> Dict[str, float]:
    m = {t: 0.0 for t in TECNICI}
    if work_df.empty or "planned_date" not in work_df.columns:
        return m
    day_df = work_df[work_df["planned_date"].astype(str) == str(target_date)].copy()
    for _, row in day_df.iterrows():
        tech = row.get("tecnico")
        if tech in m and row.get("stato_lavoro") != "Completato":
            m[tech] += float(row.get("ore_eff_previste", row.get("ore_std", 0)))
    return m


def apply_decay_to_inactive_skills(tech_profiles: Dict[str, dict], work_df: pd.DataFrame) -> Dict[str, dict]:
    current_week = iso_week_str(utc_now_iso())
    if work_df.empty:
        return tech_profiles
    for tech in TECNICI:
        tech_jobs = work_df[(work_df["tecnico"] == tech) & (work_df["week_id"] == current_week)]
        active_categories = set(tech_jobs["categoria"].tolist()) if not tech_jobs.empty else set()
        payload = tech_profiles.get(tech, initial_skill_profiles()[tech]).copy()
        changed = False
        for cat in CATEGORIE:
            if cat not in active_categories:
                new_val = round(max(1.0, float(payload.get(cat, 1.0)) - DECADIMENTO_VALUE), 2)
                if new_val != payload.get(cat):
                    payload[cat] = new_val
                    changed = True
        if changed:
            tech_profiles[tech] = payload
            upsert_entity("technician", tech, payload)
            append_entity("skill_snapshot", f"SNAP-{tech}-{int(time.time()*1000)}", {"Tecnico": tech, "week_id": current_week, "source": "decadimento", **payload})
    return tech_profiles


def compute_priority_score(urgenza: str, fermo_veicolo: bool, safety_risk: bool, ricambi_disponibili: bool, sla_hours: float, ore_std: float) -> Tuple[float, str]:
    score = 0.0
    score += {"ALTA": 45, "MEDIA": 28, "BASSA": 15}.get(urgenza, 15)
    score += 20 if fermo_veicolo else 0
    score += 18 if safety_risk else 0
    score += 8 if ricambi_disponibili else -12
    score += 12 if sla_hours <= 8 else (6 if sla_hours <= 24 else 0)
    score += min(12, max(0, ore_std - 2) * 2)
    if score >= 75:
        band = "CRITICA"
    elif score >= 50:
        band = "ALTA"
    elif score >= 30:
        band = "MEDIA"
    else:
        band = "BASSA"
    return round(score, 1), band


def get_assignment_candidates(category: str, urgency: str, work_df: pd.DataFrame, tech_profiles: Dict[str, dict], ore_std: float, planned_date: str, priority_score: float) -> pd.DataFrame:
    current_week = iso_week_str(utc_now_iso())
    week_hours_map = get_weekly_hours_map(work_df, current_week)
    day_load_map = get_daily_load_map(work_df, planned_date)
    rows = []
    days_elapsed = get_days_elapsed_in_week()
    urgency_bonus_map = {"ALTA": 1.25, "MEDIA": 1.0, "BASSA": 0.85}

    for tech in TECNICI:
        prof = tech_profiles.get(tech, initial_skill_profiles()[tech])
        skill = float(prof.get(category, 1.0))
        weekly_hours = float(week_hours_map.get(tech, 0.0))
        daily_load = float(day_load_map.get(tech, 0.0))
        current_daily_avg = weekly_hours / days_elapsed if days_elapsed else weekly_hours

        coeff_app_time = 1.5 - (skill / 5 * 0.7)
        base_fatigued = current_daily_avg > LIMITE_ORE_GIORNO or daily_load > LIMITE_ORE_GIORNO * 0.85
        base_coeff_fat = 1.25 if base_fatigued else 1.0
        ore_eff_previste = round(ore_std * coeff_app_time * base_coeff_fat * (1 + PROB_REWORK * (COEFF_REWORK_TIME - 1)), 2)

        projected_week_hours = weekly_hours + ore_eff_previste
        projected_daily_load = daily_load + ore_eff_previste
        projected_daily_avg = projected_week_hours / days_elapsed if days_elapsed else projected_week_hours
        projected_fatigued = projected_daily_avg > LIMITE_ORE_GIORNO or projected_daily_load > LIMITE_ORE_GIORNO
        final_coeff_fat = 1.25 if projected_fatigued else 1.0
        if final_coeff_fat != base_coeff_fat:
            ore_eff_previste = round(ore_std * coeff_app_time * final_coeff_fat * (1 + PROB_REWORK * (COEFF_REWORK_TIME - 1)), 2)
            projected_week_hours = weekly_hours + ore_eff_previste
            projected_daily_load = daily_load + ore_eff_previste
            projected_daily_avg = projected_week_hours / days_elapsed if days_elapsed else projected_week_hours

        overload = assess_overload(projected_week_hours, days_elapsed)
        load_penalty = weekly_hours * 0.22 + daily_load * 0.20
        urgency_bonus = skill * urgency_bonus_map[urgency]
        overload_penalty = 0.0 if overload["status"] == "OK" else (1.5 if overload["status"] == "ATTENZIONE" else 4.0)

        explain_skill = round(skill * 2.5, 2)
        explain_urgency = round(urgency_bonus, 2)
        explain_priority = round(priority_score / 25, 2)
        explain_load = round(-load_penalty, 2)
        explain_fatigue = -2.0 if projected_fatigued else 0.0
        explain_overload = round(-overload_penalty, 2)

        suitability = round(explain_skill + explain_urgency + explain_priority + explain_load + explain_fatigue + explain_overload, 3)

        mentor = "Nessuno"
        affiancamento = False
        if skill <= MAX_SKILL_APPRENTICE:
            mentors = []
            for other in TECNICI:
                if other == tech:
                    continue
                other_prof = tech_profiles.get(other, initial_skill_profiles()[other])
                other_skill = float(other_prof.get(category, 1.0))
                other_load = float(week_hours_map.get(other, 0.0))
                if other_skill >= MIN_SKILL_MENTOR and (other_load / days_elapsed) <= LIMITE_ORE_GIORNO:
                    mentors.append((other, other_skill, other_load))
            if mentors:
                mentors.sort(key=lambda x: (-x[1], x[2]))
                mentor = mentors[0][0]
                affiancamento = True
                suitability += 0.6

        rows.append({
            "Tecnico": tech,
            "Skill": round(skill, 2),
            "Carico_Attuale_h": round(weekly_hours, 2),
            "Carico_Giorno_h": round(daily_load, 2),
            "Ore_Eff_Previste": ore_eff_previste,
            "Carico_Proiettato_h": round(projected_week_hours, 2),
            "Carico_Giorno_Proiettato_h": round(projected_daily_load, 2),
            "Media_Giornaliera_h": round(projected_daily_avg, 2),
            "Capacita_Sett_h": overload["capacity_h"],
            "Affaticato": "SÌ" if projected_fatigued else "No",
            "Overload_Status": overload["status"],
            "Mentor": mentor,
            "Affiancamento": "SÌ" if affiancamento else "No",
            "Score_Skill": explain_skill,
            "Score_Urgenza": explain_urgency,
            "Score_Priorita": explain_priority,
            "Penalty_Carico": explain_load,
            "Penalty_Fatica": explain_fatigue,
            "Penalty_Overload": explain_overload,
            "Suitability_Score": round(suitability, 3),
        })
    cand = pd.DataFrame(rows).sort_values(
        by=["Overload_Status", "Suitability_Score", "Skill"],
        ascending=[True, False, False],
        key=lambda s: s.map({"OK": 0, "ATTENZIONE": 1, "CRITICO": 2}) if s.name == "Overload_Status" else s,
    ).reset_index(drop=True)
    return cand


def find_best_alternative(cand_df: pd.DataFrame, selected_tech: str) -> Optional[pd.Series]:
    if cand_df.empty:
        return None
    alternatives = cand_df[cand_df["Tecnico"] != selected_tech].copy()
    if alternatives.empty:
        return None
    ok_alt = alternatives[alternatives["Overload_Status"] == "OK"]
    if not ok_alt.empty:
        return ok_alt.sort_values(by=["Suitability_Score", "Skill"], ascending=[False, False]).iloc[0]
    warn_alt = alternatives[alternatives["Overload_Status"] == "ATTENZIONE"]
    if not warn_alt.empty:
        return warn_alt.sort_values(by=["Suitability_Score", "Skill"], ascending=[False, False]).iloc[0]
    return alternatives.sort_values(by=["Suitability_Score", "Skill"], ascending=[False, False]).iloc[0]


def estimate_job_metrics(job: dict, tech_profiles: Dict[str, dict], work_df: pd.DataFrame, is_rework: Optional[bool] = None) -> dict:
    tech = job["tecnico"]
    category = job["categoria"]
    prof = tech_profiles.get(tech, initial_skill_profiles()[tech])
    skill = float(prof.get(category, 1.0))
    week_id = job.get("week_id") or iso_week_str(job.get("created_at", utc_now_iso()))
    planned_date = str(job.get("planned_date", local_now().date()))
    hours_map = get_weekly_hours_map(work_df, week_id)
    daily_load_map = get_daily_load_map(work_df[work_df.get("id", pd.Series(dtype=str)) != job.get("id", "")] if not work_df.empty and "id" in work_df.columns else work_df, planned_date)
    week_hours = float(hours_map.get(tech, 0.0))
    day_load = float(daily_load_map.get(tech, 0.0))
    days_elapsed = get_days_elapsed_in_week()

    coeff_app_time = 1.5 - (skill / 5 * 0.7)
    is_fatigued = (week_hours / days_elapsed) > LIMITE_ORE_GIORNO or day_load > LIMITE_ORE_GIORNO * 0.85
    coeff_fat = 1.25 if is_fatigued else 1.0
    rework_bool = is_rework if is_rework is not None else bool(job.get("rework_flag", False))
    coeff_rework = COEFF_REWORK_TIME if rework_bool else 1.0

    ore_std = float(job.get("ore_std", 0.0))
    ore_eff = round(ore_std * coeff_app_time * coeff_fat * coeff_rework, 2)
    risparmio_h = round(ore_std - ore_eff, 2)

    eco_coeff = max(0.95, 1.10 - (skill * 0.03))
    coeff_error_eco = MOLTIPLICATORE_REWORK_ECO if rework_bool else 1.0
    base_eco = IMPATTO_BASE[category]

    olio = round(base_eco["Olio_L"] * eco_coeff * coeff_error_eco, 2)
    scarti = round(base_eco["Scarti_Kg"] * eco_coeff * coeff_error_eco, 2)
    energia = round(base_eco["Energia_kWh"] * eco_coeff * coeff_error_eco, 2)
    co2 = round((olio * CO2_FATTORI["Olio"]) + (scarti * CO2_FATTORI["Scarti"]) + (energia * CO2_FATTORI["Energia"]), 2)
    exergia = round(energia * (0.5 - (skill * 0.08)) * coeff_error_eco, 3)

    ricambi_cons = float(job.get("consuntivo_ricambi_eur", job.get("preventivo_ricambi_eur", 0.0)))
    labor_cons = round(ore_eff * COSTO_ORARIO_OFFICINA, 2)

    return {
        "skill_corrente": round(skill, 2),
        "is_fatigued": is_fatigued,
        "ore_eff": ore_eff,
        "risparmio_h": risparmio_h,
        "roi_eur": round(risparmio_h * COSTO_ORARIO_OFFICINA, 2),
        "olio_l": olio,
        "scarti_kg": scarti,
        "energia_kwh": energia,
        "co2_kg": co2,
        "exergia_kwh": exergia,
        "consuntivo_manodopera_eur": labor_cons,
        "consuntivo_totale_eur": round(labor_cons + ricambi_cons, 2),
        "mentor": job.get("mentor", "Nessuno"),
        "affiancamento": bool(job.get("affiancamento", False)),
        "rework_flag": rework_bool,
    }


def update_technician_after_completion(job: dict, tech_profiles: Dict[str, dict]) -> Dict[str, dict]:
    tech = job["tecnico"]
    cat = job["categoria"]
    prof = tech_profiles.get(tech, initial_skill_profiles()[tech]).copy()
    skill = float(prof.get(cat, 1.0))
    growth = GROWTH_STAGE_1 if skill < 4.2 else GROWTH_STAGE_2
    prof[cat] = round(min(5.0, skill + growth), 2)
    upsert_entity("technician", tech, prof)
    append_entity("skill_snapshot", f"SNAP-{tech}-{int(time.time()*1000)}", {"Tecnico": tech, "week_id": job.get("week_id"), "source": f"completamento_{job['id']}", **prof})

    mentor = job.get("mentor", "Nessuno")
    if job.get("affiancamento") and mentor in TECNICI:
        m_prof = tech_profiles.get(mentor, initial_skill_profiles()[mentor]).copy()
        bonus = round(float(job.get("ore_eff_consuntive", job.get("ore_eff_previste", 0.0))) * PERC_AFFIANCAMENTO * BONUS_TEACHING, 3)
        m_prof["Teaching_Score"] = round(float(m_prof.get("Teaching_Score", 0.0)) + bonus, 3)
        upsert_entity("technician", mentor, m_prof)
        append_entity("skill_snapshot", f"SNAP-{mentor}-{int(time.time()*1000)}", {"Tecnico": mentor, "week_id": job.get("week_id"), "source": f"mentoring_{job['id']}", **m_prof})
        tech_profiles[mentor] = m_prof

    tech_profiles[tech] = prof
    return tech_profiles


def calculate_checklist_progress(job: dict) -> Tuple[int, int, float, bool]:
    tasks = CHECKLIST_DATA.get(job.get("categoria", ""), [])
    state = job.get("checklist_state", {}) if isinstance(job.get("checklist_state", {}), dict) else {}
    total = len(tasks)
    done = 0
    critical_ok = True
    for i, t in enumerate(tasks):
        checked = bool(state.get(f"chk_{i}", False))
        done += 1 if checked else 0
        if t.get("critical") and not checked:
            critical_ok = False
    pct = round((done / total) * 100, 1) if total else 100.0
    return done, total, pct, critical_ok


def evaluate_closure_readiness(job: dict) -> dict:
    done, total, pct, critical_ok = calculate_checklist_progress(job)
    has_photo = isinstance(job.get("foto_b64", ""), str) and len(job.get("foto_b64", "")) > 20
    no_spare_required = bool(job.get("no_spare_required", False))
    ricambi_ok = no_spare_required or len(job.get("ricambi_usati", [])) > 0
    blockers = []
    if not critical_ok:
        blockers.append("Checklist critica incompleta")
    if not has_photo:
        blockers.append("Manca evidenza fotografica")
    if not ricambi_ok:
        blockers.append("Ricambi non registrati o flag 'nessun ricambio necessario' non attivo")
    return {"pct": pct, "critical_ok": critical_ok, "has_photo": has_photo, "ricambi_ok": ricambi_ok, "blockers": blockers, "ready": len(blockers) == 0}


def build_notifications(work_df: pd.DataFrame) -> List[dict]:
    alerts: List[dict] = []
    if work_df.empty:
        return alerts
    now_date = local_now().date()
    for _, row in work_df.iterrows():
        job_id = row.get("id", "")
        stato = row.get("stato_lavoro", "")
        if stato == "Completato":
            continue
        planned_date = str(row.get("planned_date", ""))[:10]
        if planned_date:
            try:
                d = dt.date.fromisoformat(planned_date)
                if d < now_date:
                    alerts.append({"severity": "warning", "title": f"Bolla {job_id} in ritardo", "text": f"Il lavoro {row.get('veicolo','')} era pianificato per il {planned_date} ed è ancora aperto."})
            except Exception:
                pass
        if str(row.get("overload_status", "")) in ["ATTENZIONE", "CRITICO"]:
            alerts.append({"severity": "warning" if row.get("overload_status") == "ATTENZIONE" else "error", "title": f"Overload {row.get('tecnico','')}", "text": f"La bolla {job_id} porta il tecnico a stato {row.get('overload_status')} ({row.get('carico_proiettato_h',0)} h)."})
        closure = evaluate_closure_readiness(row.to_dict())
        if stato in ["In Corso", "Sospeso"] and not closure["ready"]:
            alerts.append({"severity": "info", "title": f"Chiusura non pronta {job_id}", "text": "; ".join(closure["blockers"])})
    return alerts[:12]


def get_vehicle_history(work_df: pd.DataFrame, vehicle: str) -> pd.DataFrame:
    if work_df.empty or not vehicle:
        return pd.DataFrame()
    hist = work_df[work_df["veicolo"].astype(str).str.upper() == str(vehicle).upper()].copy()
    if hist.empty:
        return hist
    return hist.sort_values(by="created_at", ascending=False)


def compute_master_kpis(work_df: pd.DataFrame) -> dict:
    if work_df.empty:
        return {
            "roi_tot": 0.0, "co2_tot": 0.0, "exergia_tot": 0.0, "open_jobs": 0, "completed_jobs": 0,
            "rework_tot": 0, "affiancamenti": 0, "indice_fuzzy": 0.0, "olio_tot": 0.0, "scarti_tot": 0.0,
            "energia_tot": 0.0, "lead_time_h": 0.0, "ftf_rate": 0.0, "checklist_compliance": 0.0,
            "delay_rate": 0.0, "utilization_avg": 0.0,
        }
    df = work_df.copy()
    for col in ["roi_eur", "co2_kg", "exergia_kwh", "olio_l", "scarti_kg", "energia_kwh", "ore_eff_consuntive", "ore_eff_previste", "skill_corrente", "final_checklist_pct"]:
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    completed = df[df["stato_lavoro"] == "Completato"].copy()
    active = df[df["stato_lavoro"].isin(["In Attesa", "In Corso", "Sospeso"])].copy()
    all_eval = pd.concat([completed, active], ignore_index=True)

    ore_ref = all_eval["ore_eff_consuntive"].replace(0, pd.NA).fillna(all_eval["ore_eff_previste"]).fillna(all_eval["ore_std"] if "ore_std" in all_eval.columns else 0)
    ore_tot = float(ore_ref.sum()) if not all_eval.empty and float(ore_ref.sum()) > 0 else 1.0
    ore_stanchezza = float(ore_ref[all_eval.get("is_fatigued", False) == True].sum()) if not all_eval.empty and "is_fatigued" in all_eval.columns else 0.0
    ore_bassa_skill = float(ore_ref[all_eval["skill_corrente"] < 3.5].sum()) if not all_eval.empty else 0.0
    indice_fuzzy = min(100.0, ((ore_stanchezza / ore_tot) * 0.6 + (ore_bassa_skill / ore_tot) * 0.4) * 100 * 1.5)

    lead_time_h = 0.0
    delay_rate = 0.0
    if not completed.empty:
        created_ts = pd.to_datetime(completed["created_at"], errors="coerce")
        completed_ts = pd.to_datetime(completed["completed_at"], errors="coerce")
        lead_time_h = round(((completed_ts - created_ts).dt.total_seconds() / 3600).fillna(0).mean(), 2)
        if "planned_date" in completed.columns:
            planned_dt = pd.to_datetime(completed["planned_date"], errors="coerce")
            is_delay = completed_ts > (planned_dt + pd.to_timedelta(1, unit="D"))
            delay_rate = round(is_delay.fillna(False).mean() * 100, 1)

    ftf_rate = round((~completed.get("rework_flag", pd.Series(dtype=bool)).fillna(False)).mean() * 100, 1) if not completed.empty else 0.0
    checklist_compliance = round(completed["final_checklist_pct"].mean(), 1) if not completed.empty else 0.0
    hours_map = get_weekly_hours_map(df)
    capacity = get_week_capacity()
    util = [h / capacity * 100 for h in hours_map.values()] if capacity > 0 else [0]

    return {
        "roi_tot": round(completed["roi_eur"].sum(), 2),
        "co2_tot": round(completed["co2_kg"].sum(), 2),
        "exergia_tot": round(completed["exergia_kwh"].sum(), 2),
        "open_jobs": int(len(active)),
        "completed_jobs": int(len(completed)),
        "rework_tot": int(completed.get("rework_flag", pd.Series(dtype=bool)).fillna(False).sum()) if not completed.empty else 0,
        "affiancamenti": int(completed.get("affiancamento", pd.Series(dtype=bool)).fillna(False).sum()) if not completed.empty else 0,
        "indice_fuzzy": round(indice_fuzzy, 1),
        "olio_tot": round(completed["olio_l"].sum(), 2),
        "scarti_tot": round(completed["scarti_kg"].sum(), 2),
        "energia_tot": round(completed["energia_kwh"].sum(), 2),
        "lead_time_h": lead_time_h,
        "ftf_rate": ftf_rate,
        "checklist_compliance": checklist_compliance,
        "delay_rate": delay_rate,
        "utilization_avg": round(sum(util) / len(util), 1) if util else 0.0,
    }


def build_weekly_kpi_df(work_df: pd.DataFrame) -> pd.DataFrame:
    if work_df.empty or "week_id" not in work_df.columns:
        return pd.DataFrame()
    rows = []
    for week_id, g in work_df.groupby("week_id"):
        rows.append({
            "week_id": week_id,
            "ROI_€": round(pd.to_numeric(g.get("roi_eur", 0), errors="coerce").fillna(0).sum(), 2),
            "CO2_Totale_Kg": round(pd.to_numeric(g.get("co2_kg", 0), errors="coerce").fillna(0).sum(), 2),
            "Exergia_Persa_kWh": round(pd.to_numeric(g.get("exergia_kwh", 0), errors="coerce").fillna(0).sum(), 2),
            "Errori_Rework": int(pd.Series(g.get("rework_flag", False)).fillna(False).sum()),
            "Affiancamenti": int(pd.Series(g.get("affiancamento", False)).fillna(False).sum()),
            "Ore_Std": round(pd.to_numeric(g.get("ore_std", 0), errors="coerce").fillna(0).sum(), 2),
            "Ore_Eff": round(pd.to_numeric(g.get("ore_eff_consuntive", 0), errors="coerce").replace(0, pd.NA).fillna(pd.to_numeric(g.get("ore_eff_previste", 0), errors="coerce")).fillna(0).sum(), 2),
        })
    return pd.DataFrame(rows).sort_values(by="week_id")


def build_planning_df(work_df: pd.DataFrame) -> pd.DataFrame:
    if work_df.empty:
        return pd.DataFrame()
    out = work_df.copy()
    if "planned_date" not in out.columns:
        return pd.DataFrame()
    out = out[[c for c in ["planned_date", "tecnico", "id", "veicolo", "categoria", "priority_band", "stato_lavoro", "ore_eff_previste", "overload_status"] if c in out.columns]].copy()
    return out.sort_values(by=["planned_date", "tecnico", "priority_band"], ascending=[True, True, True])


def build_benchmark_df(work_df: pd.DataFrame) -> pd.DataFrame:
    if work_df.empty:
        return pd.DataFrame()
    df = work_df.copy()
    for col in ["roi_eur", "co2_kg", "ore_eff_consuntive", "ore_eff_previste", "final_checklist_pct"]:
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    rows = []
    for tech, g in df.groupby("tecnico"):
        jobs = len(g)
        completed = g[g["stato_lavoro"] == "Completato"]
        rework_rate = round(completed.get("rework_flag", pd.Series(dtype=bool)).fillna(False).mean() * 100, 1) if not completed.empty else 0.0
        roi = round(completed["roi_eur"].sum(), 2)
        checklist = round(completed["final_checklist_pct"].mean(), 1) if not completed.empty else 0.0
        ore = round(completed["ore_eff_consuntive"].replace(0, pd.NA).fillna(completed["ore_eff_previste"]).fillna(0).sum(), 2) if not completed.empty else 0.0
        rows.append({"Tecnico": tech, "N_Lavori": jobs, "ROI": roi, "Ore": ore, "Checklist_%": checklist, "Rework_%": rework_rate})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["Benchmark_Score"] = (out["ROI"].rank(pct=True) * 40 + out["Checklist_%"].rank(pct=True) * 30 + (1 - out["Rework_%"].rank(pct=True)) * 30).round(1)
    return out.sort_values(by="Benchmark_Score", ascending=False)


def format_work_order_export(work_df: pd.DataFrame) -> pd.DataFrame:
    if work_df.empty:
        return work_df
    out = work_df.copy()
    if "checklist_state" in out.columns:
        out["checklist_state"] = out["checklist_state"].apply(lambda x: json.dumps(x, ensure_ascii=False) if isinstance(x, dict) else x)
    if "ricambi_usati" in out.columns:
        out["ricambi_usati"] = out["ricambi_usati"].apply(lambda x: json.dumps(x, ensure_ascii=False) if isinstance(x, list) else x)
    if "foto_b64" in out.columns:
        out["foto_b64"] = out["foto_b64"].apply(lambda x: "PRESENTE" if isinstance(x, str) and len(x) > 20 else "")
    return out


def create_work_order(vehicle: str, category: str, urgency: str, ore_std: float, tecnico: str, suggested_tecnico: str, mentor: str, affiancamento: bool, notes: str, planned_date: str, priority_score: float, priority_band: str, fermo_veicolo: bool, safety_risk: bool, ricambi_disponibili: bool, sla_hours: float, preventivo_ricambi_eur: float) -> dict:
    now = utc_now_iso()
    labor_prev = round(float(ore_std) * COSTO_ORARIO_OFFICINA, 2)
    return {
        "id": f"IV-{int(time.time())}-{random.randint(100, 999)}",
        "veicolo": vehicle,
        "categoria": category,
        "urgenza": urgency,
        "ore_std": round(float(ore_std), 2),
        "tecnico": tecnico,
        "suggested_tecnico": suggested_tecnico,
        "mentor": mentor,
        "affiancamento": affiancamento,
        "notes": notes,
        "stato_lavoro": "In Attesa",
        "tempo_sec": 0.0,
        "last_start": None,
        "created_at": now,
        "completed_at": None,
        "week_id": iso_week_str(now),
        "planned_date": planned_date,
        "checklist_state": {},
        "ricambi_usati": [],
        "foto_b64": "",
        "rework_flag": False,
        "rework_cause": "Nessuna",
        "skill_corrente": 0.0,
        "is_fatigued": False,
        "ore_eff_previste": 0.0,
        "ore_eff_consuntive": 0.0,
        "risparmio_h": 0.0,
        "roi_eur": 0.0,
        "olio_l": 0.0,
        "scarti_kg": 0.0,
        "energia_kwh": 0.0,
        "co2_kg": 0.0,
        "exergia_kwh": 0.0,
        "final_checklist_pct": 0.0,
        "created_by": "manager",
        "priority_score": priority_score,
        "priority_band": priority_band,
        "fermo_veicolo": fermo_veicolo,
        "safety_risk": safety_risk,
        "ricambi_disponibili": ricambi_disponibili,
        "sla_hours": sla_hours,
        "preventivo_manodopera_eur": labor_prev,
        "preventivo_ricambi_eur": round(preventivo_ricambi_eur, 2),
        "preventivo_totale_eur": round(labor_prev + preventivo_ricambi_eur, 2),
        "consuntivo_manodopera_eur": 0.0,
        "consuntivo_ricambi_eur": round(preventivo_ricambi_eur, 2),
        "consuntivo_totale_eur": 0.0,
        "scostamento_ore_h": 0.0,
        "scostamento_costo_eur": 0.0,
        "no_spare_required": False,
    }


def safe_literal_eval(val, default):
    if not isinstance(val, str) or val == "" or pd.isna(val):
        return default
    try:
        return ast.literal_eval(val)
    except Exception:
        return default


# =====================================================================================
# 5. CARICAMENTO DATI
# =====================================================================================
store_df, tech_profiles, work_orders, skill_snaps, audit_log = reload_data()
work_df = work_orders_df(work_orders)
tech_profiles = apply_decay_to_inactive_skills(tech_profiles, work_df)
skill_df = technician_skill_df(tech_profiles)
master_kpi = compute_master_kpis(work_df)
weekly_kpi_df = build_weekly_kpi_df(work_df)
planning_df = build_planning_df(work_df)
benchmark_df = build_benchmark_df(work_df)
notifications = build_notifications(work_df)

# =====================================================================================
# 6. HEADER APP
# =====================================================================================
st.markdown(
    f"""
    <div class="main-title">
        <h2 style="margin:0;">🧬 IVECO Integrated Workshop Hub</h2>
        <div style="margin-top:0.35rem; opacity:0.95;">Accettazione lavori, operatività mobile, DSS competenze, preventivo/consuntivo, planning, sostenibilità, rischio e tracciabilità in un unico sistema CSV</div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("⚙️ Controllo Sistema")
    st.caption("Un solo file CSV per tutta la piattaforma")
    st.metric("Store attivo", STORE_FILE)
    st.metric("Bolle totali", len(work_orders))
    st.metric("Tecnici", len(TECNICI))
    st.text_input(
        "URL pubblico app per QR",
        key="public_base_url_input",
        placeholder="https://tuo-dominio.streamlit.app",
        help="Se lasci vuoto, il sistema prova a rilevare automaticamente l'URL corrente. In deploy imposta anche PUBLIC_BASE_URL in secrets o variabili ambiente per avere QR sempre corretti.",
    )
    st.caption(f"Base URL attuale QR: {get_public_base_url()}")
    if st.button("🔄 Ricarica dati", use_container_width=True):
        st.rerun()
    export_df = load_store_df()
    st.download_button("📥 Scarica store completo CSV", data=export_df.to_csv(index=False).encode("utf-8"), file_name=f"iveco_unified_store_{dt.datetime.now().strftime('%Y%m%d_%H%M')}.csv", mime="text/csv", use_container_width=True)
    if st.button("🗑️ Reset totale piattaforma", use_container_width=True):
        if os.path.exists(STORE_FILE):
            os.remove(STORE_FILE)
        st.rerun()

# =====================================================================================
# 7. ROUTING OPERATORE / MANAGER
# =====================================================================================
params = st.query_params
b_id = params.get("bolla_id")

if b_id:
    job = work_orders.get(b_id)
    if not job:
        st.error("⚠️ Bolla non trovata. Scansiona un QR valido.")
        if st.button("Torna alla Home"):
            st.query_params.clear()
            st.rerun()
        st.stop()

    st.title(f"🛠️ Scheda Operatore - {job['veicolo']}")
    st.caption(f"ID: {job['id']} | Categoria: {job['categoria']} | Tecnico: {job['tecnico']} | Priorità: {job.get('priority_band', job['urgenza'])}")

    projected = estimate_job_metrics(job, tech_profiles, work_df, is_rework=job.get("rework_flag", False))
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Stato", job["stato_lavoro"])
    c2.metric("Minuti lavorati", int(float(job.get("tempo_sec", 0)) // 60))
    c3.metric("Ore std", round(float(job.get("ore_std", 0.0)), 2))
    c4.metric("Ore eff previste", projected["ore_eff"])
    c5.metric("Checklist attuale", f"{calculate_checklist_progress(job)[2]}%")

    with st.container(border=True):
        st.subheader("⏱️ Gestione avanzamento")
        cols = st.columns(3)
        if job["stato_lavoro"] in ["In Attesa", "Sospeso"]:
            if cols[0].button("▶️ Avvia intervento", use_container_width=True, type="primary"):
                job["last_start"] = time.time()
                job["stato_lavoro"] = "In Corso"
                upsert_entity("work_order", job["id"], job)
                add_audit("start_job", f"Avvio intervento {job['id']}", {"job_id": job["id"]})
                st.rerun()
        if job["stato_lavoro"] == "In Corso":
            if cols[1].button("⏸️ Sospendi", use_container_width=True):
                delta = time.time() - float(job.get("last_start") or time.time())
                job["tempo_sec"] = float(job.get("tempo_sec", 0.0)) + delta
                job["last_start"] = None
                job["stato_lavoro"] = "Sospeso"
                upsert_entity("work_order", job["id"], job)
                add_audit("pause_job", f"Sospeso intervento {job['id']}", {"job_id": job["id"]})
                st.rerun()

    with st.container(border=True):
        st.subheader("📸 Evidenza fotografica")
        foto_data = job.get("foto_b64", "")
        if isinstance(foto_data, str) and len(foto_data) > 20:
            st.image(base64.b64decode(foto_data), caption="Foto intervento", use_container_width=True)
            if st.button("🗑️ Elimina foto", use_container_width=True):
                job["foto_b64"] = ""
                upsert_entity("work_order", job["id"], job)
                add_audit("delete_photo", f"Eliminata foto su {job['id']}", {"job_id": job["id"]})
                st.rerun()
        else:
            up = st.file_uploader("Carica foto del guasto/intervento", type=["jpg", "jpeg", "png"])
            if up is not None:
                job["foto_b64"] = encode_img(up)
                upsert_entity("work_order", job["id"], job)
                add_audit("upload_photo", f"Caricata foto su {job['id']}", {"job_id": job["id"]})
                st.success("Foto salvata")
                st.rerun()

    with st.container(border=True):
        st.subheader("📋 Checklist qualità, note e ricambi")
        tasks = CHECKLIST_DATA.get(job["categoria"], [])
        checklist_state = job.get("checklist_state", {}) if isinstance(job.get("checklist_state", {}), dict) else {}
        for i, task in enumerate(tasks):
            suffix = " - CRITICO" if task.get("critical") else ""
            checklist_state[f"chk_{i}"] = st.checkbox(f"{task['task']} ({task['risk']}){suffix}", value=bool(checklist_state.get(f"chk_{i}", False)))
        job["checklist_state"] = checklist_state
        job["ricambi_usati"] = st.multiselect("Ricambi utilizzati", RICAMBI_CATALOGO, default=job.get("ricambi_usati", []) if isinstance(job.get("ricambi_usati", []), list) else [])
        csp1, csp2 = st.columns(2)
        job["no_spare_required"] = csp1.checkbox("Nessun ricambio necessario", value=bool(job.get("no_spare_required", False)))
        job["rework_flag"] = csp2.checkbox("Segnala rework / rifacimento", value=bool(job.get("rework_flag", False)))
        if job["rework_flag"]:
            job["rework_cause"] = st.selectbox("Causa rework", REWORK_CAUSES[1:], index=max(0, REWORK_CAUSES[1:].index(job.get("rework_cause", REWORK_CAUSES[1])) if job.get("rework_cause", "Nessuna") in REWORK_CAUSES[1:] else 0))
        else:
            job["rework_cause"] = "Nessuna"
        job["consuntivo_ricambi_eur"] = st.number_input("Costo ricambi consuntivo (€)", min_value=0.0, value=float(job.get("consuntivo_ricambi_eur", job.get("preventivo_ricambi_eur", 0.0))), step=10.0)
        job["notes"] = st.text_area("Note operative", value=str(job.get("notes", "")), height=110)

        if st.button("💾 Salva avanzamento", use_container_width=True):
            upsert_entity("work_order", job["id"], job)
            add_audit("save_progress", f"Salvataggio avanzamento {job['id']}", {"job_id": job["id"]})
            st.success("Dati sincronizzati correttamente")

    with st.container(border=True):
        st.subheader("✅ Chiusura intervento")
        readiness = evaluate_closure_readiness(job)
        if readiness["ready"]:
            st.success("Prerequisiti di chiusura soddisfatti.")
        else:
            st.warning("Prima della chiusura completa questi punti: " + "; ".join(readiness["blockers"]))
        override_close = st.checkbox("Consenti chiusura con override responsabile", value=False)
        if st.button("🏁 Completa intervento", type="primary", use_container_width=True, disabled=job["stato_lavoro"] == "Completato"):
            if not readiness["ready"] and not override_close:
                st.error("Chiusura bloccata: completa checklist/foto/ricambi oppure attiva l'override.")
            else:
                if job["stato_lavoro"] == "In Corso":
                    delta = time.time() - float(job.get("last_start") or time.time())
                    job["tempo_sec"] = float(job.get("tempo_sec", 0.0)) + delta
                    job["last_start"] = None
                _, latest_profiles, latest_work_orders, _, _ = reload_data()
                latest_work_df = work_orders_df(latest_work_orders)
                metrics = estimate_job_metrics(job, latest_profiles, latest_work_df, is_rework=bool(job.get("rework_flag", False)))
                done, total, checklist_pct, _ = calculate_checklist_progress(job)
                ore_cons = round(float(job.get("tempo_sec", 0.0)) / 3600, 2) if float(job.get("tempo_sec", 0.0)) > 0 else metrics["ore_eff"]
                job.update({
                    "stato_lavoro": "Completato",
                    "completed_at": utc_now_iso(),
                    "skill_corrente": metrics["skill_corrente"],
                    "is_fatigued": metrics["is_fatigued"],
                    "ore_eff_previste": metrics["ore_eff"],
                    "ore_eff_consuntive": ore_cons,
                    "risparmio_h": metrics["risparmio_h"],
                    "roi_eur": metrics["roi_eur"],
                    "olio_l": metrics["olio_l"],
                    "scarti_kg": metrics["scarti_kg"],
                    "energia_kwh": metrics["energia_kwh"],
                    "co2_kg": metrics["co2_kg"],
                    "exergia_kwh": metrics["exergia_kwh"],
                    "final_checklist_pct": checklist_pct,
                    "mentor": metrics["mentor"],
                    "affiancamento": metrics["affiancamento"],
                    "rework_flag": metrics["rework_flag"],
                    "consuntivo_manodopera_eur": round(ore_cons * COSTO_ORARIO_OFFICINA, 2),
                    "consuntivo_totale_eur": round((ore_cons * COSTO_ORARIO_OFFICINA) + float(job.get("consuntivo_ricambi_eur", 0.0)), 2),
                    "scostamento_ore_h": round(ore_cons - float(job.get("ore_std", 0.0)), 2),
                    "scostamento_costo_eur": round(((ore_cons * COSTO_ORARIO_OFFICINA) + float(job.get("consuntivo_ricambi_eur", 0.0))) - float(job.get("preventivo_totale_eur", 0.0)), 2),
                    "closure_override": override_close,
                })
                upsert_entity("work_order", job["id"], job)
                latest_profiles = update_technician_after_completion(job, latest_profiles)
                add_audit("complete_job", f"Completato intervento {job['id']}", {"job_id": job["id"], "tecnico": job["tecnico"], "override": override_close, "rework": job.get("rework_cause", "Nessuna")})
                st.success("Intervento completato e KPI aggiornati correttamente")
                st.balloons()
                st.rerun()
    st.stop()

# -------------------------------------------------------------------------------------
# VISTA MANAGER / MASTER HUB
# -------------------------------------------------------------------------------------
st.markdown("### 🎯 Cruscotto direzionale unificato")
mc1, mc2, mc3, mc4, mc5, mc6 = st.columns(6)
mc1.metric("💰 ROI totale", f"{master_kpi['roi_tot']:.2f} €")
mc2.metric("🌍 CO₂ totale", f"{master_kpi['co2_tot']:.1f} kg")
mc3.metric("🔥 Exergia", f"{master_kpi['exergia_tot']:.1f} kWh")
mc4.metric("📂 Bolle aperte", master_kpi["open_jobs"])
mc5.metric("⚠️ Fuzzy Risk", f"{master_kpi['indice_fuzzy']:.1f}%")
mc6.metric("📈 First Time Fix", f"{master_kpi['ftf_rate']:.1f}%")

if notifications:
    with st.expander(f"🔔 Notifiche operative ({len(notifications)})", expanded=True):
        for alert in notifications:
            func = st.error if alert["severity"] == "error" else st.warning if alert["severity"] == "warning" else st.info
            func(f"**{alert['title']}** — {alert['text']}")

manager_tabs = st.tabs([
    "🆕 Accettazione & Assegnazione",
    "📅 Planning & Operatività",
    "📋 Master Dashboard KPI",
    "🔄 Flussi & Sostenibilità",
    "⚠️ Rischio Fuzzy",
    "🧠 Competenze & Team",
    "📈 Trend, Storico & Benchmark",
    "🗃️ Audit & Data Hub",
])

with manager_tabs[0]:
    st.subheader("Creazione manuale bolla con motore DSS integrato")
    st.markdown("L'assegnazione è **manuale**, ma il sistema propone il tecnico migliore usando skill, carico, fatica, planning, mentoring e priorità composita.")
    left, right = st.columns([1.1, 1.5])
    with left:
        with st.form("nuova_bolla_integrata", clear_on_submit=False):
            st.text_input("Targa / Modello veicolo", key="wo_veicolo")
            st.selectbox("Area intervento", CATEGORIE, key="wo_categoria")
            st.selectbox("Urgenza percepita", URGENZE, key="wo_urgenza")
            st.date_input("Data pianificata", key="wo_planned_date")
            st.number_input("Ore standard previste", min_value=0.5, max_value=24.0, step=0.5, key="wo_ore_std")
            st.checkbox("Veicolo fermo / indisponibile", key="wo_fermo")
            st.checkbox("Impatto sicurezza", key="wo_safety")
            st.checkbox("Ricambi disponibili", key="wo_ricambi")
            st.number_input("SLA / scadenza promessa (ore)", min_value=1.0, max_value=168.0, step=1.0, key="wo_sla")
            st.number_input("Preventivo ricambi (€)", min_value=0.0, step=10.0, key="wo_prev_ricambi")
            st.text_area("Descrizione / note di accettazione", height=90, key="wo_note")
            submitted_preview = st.form_submit_button("Analizza assegnazione")
        veicolo = st.session_state.get("wo_veicolo", "").strip()
        categoria = st.session_state.get("wo_categoria", CATEGORIE[0])
        urgenza = st.session_state.get("wo_urgenza", URGENZE[0])
        planned_date = st.session_state.get("wo_planned_date", local_now().date())
        ore_std = float(st.session_state.get("wo_ore_std", 3.0))
        fermo_veicolo = bool(st.session_state.get("wo_fermo", True))
        safety_risk = bool(st.session_state.get("wo_safety", False))
        ricambi_disponibili = bool(st.session_state.get("wo_ricambi", True))
        sla_hours = float(st.session_state.get("wo_sla", 24.0))
        preventivo_ricambi = float(st.session_state.get("wo_prev_ricambi", 0.0))
        note = st.session_state.get("wo_note", "")
        if submitted_preview and veicolo:
            priority_score, priority_band = compute_priority_score(urgenza, fermo_veicolo, safety_risk, ricambi_disponibili, sla_hours, ore_std)
            candidates = get_assignment_candidates(categoria, urgenza, work_df, tech_profiles, ore_std, planned_date.isoformat(), priority_score)
            st.session_state["candidate_table"] = candidates.to_dict("records")
            st.session_state["candidate_context"] = {
                "veicolo": veicolo, "categoria": categoria, "urgenza": urgenza, "ore_std": ore_std, "note": note,
                "planned_date": planned_date.isoformat(), "priority_score": priority_score, "priority_band": priority_band,
                "fermo_veicolo": fermo_veicolo, "safety_risk": safety_risk, "ricambi_disponibili": ricambi_disponibili,
                "sla_hours": sla_hours, "preventivo_ricambi": preventivo_ricambi,
            }
        elif submitted_preview and not veicolo:
            st.warning("Inserisci almeno targa o modello veicolo prima di analizzare l'assegnazione.")
    with right:
        if st.session_state.get("candidate_table"):
            cand_df = pd.DataFrame(st.session_state["candidate_table"])
            ctx = st.session_state.get("candidate_context", {})
            top = cand_df.iloc[0]
            p1, p2, p3 = st.columns(3)
            p1.metric("Priority score", ctx.get("priority_score", 0.0))
            p2.metric("Classe priorità", ctx.get("priority_band", "-"))
            p3.metric("Data pianificata", ctx.get("planned_date", "-"))
            st.success(f"Suggerimento DSS: **{top['Tecnico']}** | Skill {top['Skill']} | Carico proiettato {top['Carico_Proiettato_h']} h | Stato carico: {top['Overload_Status']} | Mentor: {top['Mentor']}")
            display_cols = [
                "Tecnico", "Skill", "Carico_Attuale_h", "Carico_Giorno_h", "Ore_Eff_Previste", "Carico_Proiettato_h",
                "Carico_Giorno_Proiettato_h", "Media_Giornaliera_h", "Capacita_Sett_h", "Affaticato", "Overload_Status",
                "Mentor", "Affiancamento", "Suitability_Score"
            ]
            st.dataframe(cand_df[display_cols], use_container_width=True, hide_index=True, height=320)
            with st.expander("🔎 Spiegazione del suggerimento DSS"):
                explain_cols = ["Tecnico", "Score_Skill", "Score_Urgenza", "Score_Priorita", "Penalty_Carico", "Penalty_Fatica", "Penalty_Overload", "Suitability_Score"]
                st.dataframe(cand_df[explain_cols], use_container_width=True, hide_index=True)
                st.caption("Il punteggio finale combina skill, urgenza, priorità composita, carico operativo e penalità di fatica/overload.")
            selected = st.selectbox("Tecnico definitivo", cand_df["Tecnico"].tolist(), index=0)
            sel_row = cand_df[cand_df["Tecnico"] == selected].iloc[0]
            alternative = find_best_alternative(cand_df, selected)
            override_aff = st.checkbox("Forza affiancamento", value=(sel_row["Affiancamento"] == "SÌ"))
            mentor_options = ["Nessuno"] + [t for t in TECNICI if t != selected]
            manual_mentor = st.selectbox("Mentor / supporto", mentor_options, index=0 if sel_row["Mentor"] == "Nessuno" else mentor_options.index(sel_row["Mentor"]))
            overload_status = sel_row["Overload_Status"]
            override_critical = False
            if overload_status == "ATTENZIONE":
                txt = f"Il tecnico **{selected}** supererà la capacità consigliata ({sel_row['Carico_Proiettato_h']} h su {sel_row['Capacita_Sett_h']} h)."
                if alternative is not None:
                    txt += f" Alternativa consigliata: **{alternative['Tecnico']}** con score {alternative['Suitability_Score']:.2f}."
                st.warning(txt)
            elif overload_status == "CRITICO":
                txt = f"Overload critico su **{selected}**: carico proiettato {sel_row['Carico_Proiettato_h']} h su {sel_row['Capacita_Sett_h']} h."
                if alternative is not None:
                    txt += f" Si consiglia fortemente **{alternative['Tecnico']}**."
                st.error(txt)
                override_critical = st.checkbox("Confermo comunque l'assegnazione in overload critico", value=False)
            else:
                st.info(f"Carico sotto controllo: {sel_row['Carico_Proiettato_h']} h su capacità teorica {sel_row['Capacita_Sett_h']} h.")

            create_disabled = overload_status == "CRITICO" and not override_critical
            if st.button("✅ Crea bolla e genera QR", type="primary", use_container_width=True, disabled=create_disabled):
                ctx = st.session_state.get("candidate_context") or {}
                mentor_value = manual_mentor if override_aff else (sel_row["Mentor"] if sel_row["Mentor"] != "Nessuno" else "Nessuno")
                job = create_work_order(
                    vehicle=ctx["veicolo"], category=ctx["categoria"], urgency=ctx["urgenza"], ore_std=ctx["ore_std"], tecnico=selected,
                    suggested_tecnico=str(top["Tecnico"]), mentor=mentor_value, affiancamento=bool(override_aff), notes=ctx.get("note", ""),
                    planned_date=ctx["planned_date"], priority_score=float(ctx["priority_score"]), priority_band=ctx["priority_band"],
                    fermo_veicolo=bool(ctx["fermo_veicolo"]), safety_risk=bool(ctx["safety_risk"]), ricambi_disponibili=bool(ctx["ricambi_disponibili"]),
                    sla_hours=float(ctx["sla_hours"]), preventivo_ricambi_eur=float(ctx["preventivo_ricambi"]),
                )
                pred = estimate_job_metrics(job, tech_profiles, work_df, is_rework=False)
                job.update({
                    "skill_corrente": pred["skill_corrente"], "is_fatigued": pred["is_fatigued"], "ore_eff_previste": pred["ore_eff"],
                    "carico_attuale_h": float(sel_row["Carico_Attuale_h"]), "carico_proiettato_h": float(sel_row["Carico_Proiettato_h"]),
                    "overload_status": overload_status, "capacity_reference_h": float(sel_row["Capacita_Sett_h"]),
                })
                upsert_entity("work_order", job["id"], job)
                add_audit("create_job", f"Creata bolla {job['id']}", {"job_id": job["id"], "tecnico": selected, "categoria": ctx["categoria"], "overload_status": overload_status})
                st.session_state["last_created_job_id"] = job["id"]
                st.session_state["candidate_table"] = None
                st.session_state["candidate_context"] = None
                reset_new_job_form()
                st.rerun()
        else:
            st.info("Inserisci i dati a sinistra e premi 'Analizza assegnazione' per attivare il DSS.")

    st.divider()
    st.subheader("Storico veicolo in fase di accettazione")
    vh_ctx = st.session_state.get("candidate_context") or {}
    vh = get_vehicle_history(work_df, vh_ctx.get("veicolo", ""))
    if not vh.empty:
        hist_cols = [c for c in ["id", "created_at", "categoria", "tecnico", "stato_lavoro", "rework_flag", "rework_cause", "consuntivo_totale_eur"] if c in vh.columns]
        st.dataframe(vh[hist_cols], use_container_width=True, hide_index=True)
    else:
        st.info("Nessuno storico trovato per il veicolo attualmente in accettazione.")

    st.divider()
    st.subheader("QR accesso operatore")
    if not work_df.empty:
        latest_jobs = work_df.sort_values(by="created_at", ascending=False).head(10)
        job_options = latest_jobs["id"].tolist()
        default_index = 0
        if st.session_state.get("last_created_job_id") in job_options:
            default_index = job_options.index(st.session_state["last_created_job_id"])
            st.success(f"Bolla {st.session_state['last_created_job_id']} registrata correttamente.")
        selected_job_id = st.selectbox("Seleziona una bolla", job_options, index=default_index, format_func=lambda x: f"{x} - {work_orders.get(x, {}).get('veicolo', 'Veicolo')}")
        url = build_operator_url(selected_job_id)
        q1, q2 = st.columns([1, 1.3])
        with q1:
            st.image(get_qr(url), caption=f"QR accesso bolla {selected_job_id}")
        with q2:
            st.code(url, language=None)
            st.caption("Il tecnico può aprire la scheda operatore da smartphone tramite link o QR.")
            st.info("Per un uso da telefono in deploy, pubblica l'app e imposta l'URL pubblico nella sidebar oppure tramite PUBLIC_BASE_URL nei secrets/variabili ambiente.")
    else:
        st.info("Nessuna bolla disponibile per la generazione del QR.")

with manager_tabs[1]:
    st.subheader("Planning operativo e monitoraggio live")
    if work_df.empty:
        st.info("Nessuna bolla presente nel sistema.")
    else:
        plan_day = st.date_input("Filtra planning per data", value=local_now().date(), key="plan_day")
        day_df = work_df[work_df["planned_date"].astype(str) == plan_day.isoformat()].copy() if "planned_date" in work_df.columns else pd.DataFrame()
        c1, c2 = st.columns([1.2, 1])
        with c1:
            st.markdown("#### Carico giornaliero per tecnico")
            day_load_map = get_daily_load_map(work_df, plan_day.isoformat())
            load_view = pd.DataFrame({"Tecnico": list(day_load_map.keys()), "Ore previste": list(day_load_map.values())}).sort_values(by="Ore previste", ascending=False)
            st.dataframe(load_view, use_container_width=True, hide_index=True)
        with c2:
            st.markdown("#### Bolle pianificate del giorno")
            if not day_df.empty:
                show_cols = [c for c in ["id", "veicolo", "categoria", "tecnico", "priority_band", "stato_lavoro", "ore_eff_previste", "overload_status"] if c in day_df.columns]
                st.dataframe(day_df[show_cols], use_container_width=True, hide_index=True, height=280)
            else:
                st.info("Nessuna bolla pianificata nel giorno selezionato.")

        st.divider()
        st.markdown("#### Monitoraggio lavori in tempo reale")
        live_df = work_df.copy().sort_values(by=["planned_date", "priority_score", "created_at"], ascending=[True, False, False])
        live_df["Minuti"] = (pd.to_numeric(live_df["tempo_sec"], errors="coerce").fillna(0) // 60).astype(int)
        live_df["Checklist %"] = live_df.apply(lambda r: calculate_checklist_progress(r.to_dict())[2], axis=1)
        present_cols = [c for c in ["id", "planned_date", "veicolo", "categoria", "priority_band", "tecnico", "mentor", "stato_lavoro", "Minuti", "ore_std", "ore_eff_previste", "ore_eff_consuntive", "Checklist %", "overload_status"] if c in live_df.columns]
        st.dataframe(live_df[present_cols], use_container_width=True, hide_index=True)

        ch1, ch2, ch3 = st.columns(3)
        ch1.bar_chart(live_df["stato_lavoro"].value_counts())
        ch2.bar_chart(live_df["categoria"].value_counts())
        ch3.bar_chart(live_df.groupby("tecnico")["ore_eff_previste"].sum().sort_values(ascending=False))

        st.divider()
        st.subheader("Gestione rapida bolla")
        job_manage = st.selectbox("Seleziona bolla da modificare", live_df["id"].tolist(), key="manage_job")
        managed = work_orders[job_manage]
        m1, m2, m3, m4 = st.columns(4)
        new_state = m1.selectbox("Nuovo stato", STATI, index=STATI.index(managed["stato_lavoro"]))
        new_tech = m2.selectbox("Riassegna tecnico", TECNICI, index=TECNICI.index(managed["tecnico"]))
        new_mentor = m3.selectbox("Mentor", ["Nessuno"] + [t for t in TECNICI if t != new_tech], index=0 if managed.get("mentor", "Nessuno") == "Nessuno" else (["Nessuno"] + [t for t in TECNICI if t != new_tech]).index(managed.get("mentor", "Nessuno")))
        new_day = m4.date_input("Nuova data pianificata", value=dt.date.fromisoformat(str(managed.get("planned_date", local_now().date()))[:10]), key="new_day")
        projected = get_assignment_candidates(managed["categoria"], managed.get("urgenza", "MEDIA"), work_df[work_df["id"] != managed["id"]] if "id" in work_df.columns else work_df, tech_profiles, float(managed.get("ore_std", 0.0)), new_day.isoformat(), float(managed.get("priority_score", 30.0)))
        row = projected[projected["Tecnico"] == new_tech].iloc[0]
        if row["Overload_Status"] == "ATTENZIONE":
            st.warning(f"Riassegnando a **{new_tech}** il carico proiettato salirà a {row['Carico_Proiettato_h']} h su {row['Capacita_Sett_h']} h.")
        elif row["Overload_Status"] == "CRITICO":
            st.error(f"Riassegnazione critica: **{new_tech}** arriverebbe a {row['Carico_Proiettato_h']} h su {row['Capacita_Sett_h']} h.")
        if st.button("💾 Applica modifica", use_container_width=True):
            managed["stato_lavoro"] = new_state
            managed["tecnico"] = new_tech
            managed["mentor"] = new_mentor
            managed["planned_date"] = new_day.isoformat()
            managed["affiancamento"] = new_mentor != "Nessuno"
            remetrics = estimate_job_metrics(managed, tech_profiles, work_df, is_rework=managed.get("rework_flag", False))
            managed["skill_corrente"] = remetrics["skill_corrente"]
            managed["is_fatigued"] = remetrics["is_fatigued"]
            managed["ore_eff_previste"] = remetrics["ore_eff"]
            managed["carico_proiettato_h"] = float(row["Carico_Proiettato_h"])
            managed["overload_status"] = row["Overload_Status"]
            managed["capacity_reference_h"] = float(row["Capacita_Sett_h"])
            upsert_entity("work_order", managed["id"], managed)
            add_audit("edit_job", f"Modificata bolla {managed['id']}", {"job_id": managed["id"], "overload_status": row["Overload_Status"]})
            st.success("Bolla aggiornata correttamente")
            st.rerun()

with manager_tabs[2]:
    st.subheader("Master Dashboard KPI")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Lead time medio", f"{master_kpi['lead_time_h']:.1f} h")
    c2.metric("Checklist compliance", f"{master_kpi['checklist_compliance']:.1f}%")
    c3.metric("Delay rate", f"{master_kpi['delay_rate']:.1f}%")
    c4.metric("Utilizzo medio team", f"{master_kpi['utilization_avg']:.1f}%")
    r1, r2, r3 = st.columns(3)
    with r1:
        st.info("🛠️ Operatività & Social")
        st.write(f"**Affiancamenti totali:** {master_kpi['affiancamenti']}")
        st.write(f"**Interventi completati:** {master_kpi['completed_jobs']}")
        st.write(f"**Rework registrati:** {master_kpi['rework_tot']}")
        st.write(f"**First Time Fix:** {master_kpi['ftf_rate']} %")
    with r2:
        st.success("💶 Preventivo vs consuntivo")
        if not work_df.empty:
            prev_tot = float(work_df.get("preventivo_totale_eur", pd.Series(dtype=float)).fillna(0).sum()) if "preventivo_totale_eur" in work_df.columns else 0.0
            cons_tot = float(work_df.get("consuntivo_totale_eur", pd.Series(dtype=float)).fillna(0).sum()) if "consuntivo_totale_eur" in work_df.columns else 0.0
            st.write(f"**Preventivo totale:** {round(prev_tot,2)} €")
            st.write(f"**Consuntivo totale:** {round(cons_tot,2)} €")
            st.write(f"**Scostamento:** {round(cons_tot - prev_tot,2)} €")
    with r3:
        st.warning("🌱 Sostenibilità")
        st.write(f"**Olio gestito:** {master_kpi['olio_tot']} L")
        st.write(f"**Scarti prodotti:** {master_kpi['scarti_tot']} kg")
        st.write(f"**Energia contabilizzata:** {master_kpi['energia_tot']} kWh")
        st.write(f"**CO₂ totale:** {master_kpi['co2_tot']} kg")
    st.divider()
    if not weekly_kpi_df.empty:
        st.dataframe(weekly_kpi_df, use_container_width=True, hide_index=True)
    if not work_df.empty:
        comp = work_df.copy()
        comp_view = comp[[c for c in ["id", "veicolo", "preventivo_totale_eur", "consuntivo_totale_eur", "scostamento_costo_eur", "ore_std", "ore_eff_consuntive", "scostamento_ore_h"] if c in comp.columns]].copy()
        st.markdown("#### Scostamenti per bolla")
        st.dataframe(comp_view.sort_values(by="scostamento_costo_eur", ascending=False), use_container_width=True, hide_index=True)

with manager_tabs[3]:
    st.subheader("Mappatura dei flussi e sostenibilità")
    if work_df.empty:
        st.info("Nessun dato disponibile.")
    else:
        selectable_weeks = sorted(work_df["week_id"].dropna().unique().tolist())
        sel_week = st.selectbox("Seleziona settimana", selectable_weeks, index=len(selectable_weeks)-1)
        df_plot = work_df[work_df["week_id"] == sel_week].copy()
        labels = ["Motore", "Elettronica", "Freni", "Cambio", "Valore (€)", "Scarti (unità)", "Exergia (kWh)"]
        cat_idx = {"Motore": 0, "Elettronica": 1, "Freni": 2, "Cambio": 3}
        s, t, v, c = [], [], [], []
        for cat in CATEGORIE:
            df_cat = df_plot[df_plot["categoria"] == cat]
            if not df_cat.empty:
                s += [cat_idx[cat], cat_idx[cat], cat_idx[cat]]
                t += [4, 5, 6]
                v += [max(1, float(df_cat.get("roi_eur", 0).sum())), max(1, float((df_cat.get("olio_l", 0).sum() + df_cat.get("scarti_kg", 0).sum()) * 10)), max(1, float(df_cat.get("exergia_kwh", 0).sum() * 10))]
                c += ["rgba(46, 204, 113, 0.4)", "rgba(231, 76, 60, 0.4)", "rgba(241, 196, 15, 0.4)"]
        st.plotly_chart(go.Figure(data=[go.Sankey(node=dict(pad=15, thickness=20, label=labels), link=dict(source=s, target=t, value=v, color=c))]), use_container_width=True)
        x1, x2 = st.columns(2)
        x1.plotly_chart(px.bar(df_plot, x="categoria", y="co2_kg", color="categoria", title="CO₂ per categoria"), use_container_width=True)
        x2.plotly_chart(px.pie(df_plot, names="categoria", values="exergia_kwh", title="Ripartizione exergia dissipata"), use_container_width=True)
        if "rework_cause" in df_plot.columns:
            cause_df = df_plot[df_plot["rework_flag"] == True].copy()
            if not cause_df.empty:
                st.plotly_chart(px.histogram(cause_df, x="rework_cause", color="categoria", title="Cause di rework"), use_container_width=True)

with manager_tabs[4]:
    st.subheader("Fuzzy Risk Index")
    gauge = go.Figure(go.Indicator(mode="gauge+number", value=master_kpi["indice_fuzzy"], title={"text": "Vulnerabilità Sistemica %"}, gauge={"axis": {"range": [0, 100]}, "steps": [{"range": [0, 40], "color": "green"}, {"range": [40, 80], "color": "yellow"}, {"range": [80, 100], "color": "red"}] }))
    st.plotly_chart(gauge, use_container_width=True)
    if not work_df.empty:
        risk_df = work_df.copy()
        risk_df["risk_driver"] = risk_df.apply(lambda r: "Bassa skill" if float(r.get("skill_corrente", 0)) < 3.5 else ("Fatica" if bool(r.get("is_fatigued", False)) else ("Overload" if r.get("overload_status", "") in ["ATTENZIONE", "CRITICO"] else "Controllato")), axis=1)
        st.plotly_chart(px.histogram(risk_df, x="tecnico", color="risk_driver", title="Driver di rischio per tecnico"), use_container_width=True)

with manager_tabs[5]:
    st.subheader("Matrice competenze, mentoring e team")
    if not skill_df.empty:
        tech_sel = st.selectbox("Seleziona tecnico", TECNICI, key="skill_tech")
        val = skill_df[skill_df["Tecnico"] == tech_sel][CATEGORIE].values.flatten().tolist()
        radar = go.Figure(data=go.Scatterpolar(r=val + [val[0]], theta=CATEGORIE + [CATEGORIE[0]], fill="toself"))
        radar.update_layout(title=f"Profilo skill - {tech_sel}")
        st.plotly_chart(radar, use_container_width=True)
        s1, s2 = st.columns(2)
        s1.plotly_chart(px.bar(skill_df, x="Tecnico", y="Teaching_Score", title="Punti mentoring / leadership"), use_container_width=True)
        heat = px.imshow(skill_df.set_index("Tecnico")[CATEGORIE], text_auto=True, aspect="auto", title="Heatmap skill team")
        s2.plotly_chart(heat, use_container_width=True)
        st.divider()
        st.subheader("Aggiornamento manuale skill")
        edit_tech = st.selectbox("Tecnico da modificare", TECNICI, key="edit_tech")
        prof = tech_profiles[edit_tech]
        ec1, ec2, ec3, ec4, ec5 = st.columns(5)
        new_vals = {
            "Tecnico": edit_tech,
            "Motore": ec1.slider("Motore", 1.0, 5.0, float(prof.get("Motore", 1.0)), 0.1),
            "Elettronica": ec2.slider("Elettronica", 1.0, 5.0, float(prof.get("Elettronica", 1.0)), 0.1),
            "Freni": ec3.slider("Freni", 1.0, 5.0, float(prof.get("Freni", 1.0)), 0.1),
            "Cambio": ec4.slider("Cambio", 1.0, 5.0, float(prof.get("Cambio", 1.0)), 0.1),
            "Teaching_Score": ec5.slider("Teaching", 0.0, 10.0, float(prof.get("Teaching_Score", 0.0)), 0.1),
        }
        if st.button("💾 Salva skill manuali", use_container_width=True):
            upsert_entity("technician", edit_tech, new_vals)
            append_entity("skill_snapshot", f"SNAP-{edit_tech}-{int(time.time()*1000)}", {"Tecnico": edit_tech, "week_id": iso_week_str(utc_now_iso()), "source": "manual_edit", **new_vals})
            add_audit("edit_skill", f"Aggiornate skill {edit_tech}", {"tecnico": edit_tech})
            st.success("Profilo tecnico aggiornato")
            st.rerun()

with manager_tabs[6]:
    st.subheader("Trend, storico veicolo e benchmark")
    if not weekly_kpi_df.empty:
        st.plotly_chart(px.line(weekly_kpi_df, x="week_id", y=["ROI_€", "Exergia_Persa_kWh"], markers=True, title="Disaccoppiamento: profitto vs spreco energetico"), use_container_width=True)
        st.plotly_chart(px.line(weekly_kpi_df, x="week_id", y=["CO2_Totale_Kg", "Errori_Rework"], markers=True, title="CO₂ e rework nel tempo"), use_container_width=True)
    if not benchmark_df.empty:
        st.markdown("#### Benchmark tecnico")
        st.dataframe(benchmark_df, use_container_width=True, hide_index=True)
        st.plotly_chart(px.scatter(benchmark_df, x="Rework_%", y="ROI", size="N_Lavori", hover_name="Tecnico", title="Benchmark ROI vs Rework"), use_container_width=True)
    st.divider()
    st.markdown("#### Storico veicolo")
    vehicle_query = st.text_input("Cerca targa / modello")
    if vehicle_query:
        vh = get_vehicle_history(work_df, vehicle_query)
        if vh.empty:
            st.info("Nessuna bolla trovata per il veicolo cercato.")
        else:
            cols = [c for c in ["id", "created_at", "planned_date", "categoria", "tecnico", "stato_lavoro", "rework_flag", "rework_cause", "preventivo_totale_eur", "consuntivo_totale_eur"] if c in vh.columns]
            st.dataframe(vh[cols], use_container_width=True, hide_index=True)

with manager_tabs[7]:
    st.markdown("### 🧠 Archivio centrale e tracciabilità completa")
    st.subheader("🗃️ Data hub unificato")
    st.caption("Tutti i dati sono salvati nello stesso file CSV, separati per tipologia di record.")
    total_records = len(store_df)
    total_types = store_df["entity_type"].nunique() if not store_df.empty else 0
    total_jobs = len(work_df) if not work_df.empty else 0
    total_audit = len(audit_log) if audit_log else 0
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Record totali", total_records)
    k2.metric("Tipologie record", total_types)
    k3.metric("Bolle archiviate", total_jobs)
    k4.metric("Eventi audit", total_audit)
    st.divider()
    left, right = st.columns([0.85, 1.65], gap="large")
    with left:
        ent_counts = store_df["entity_type"].value_counts().rename_axis("entity_type").reset_index(name="count").sort_values(by="count", ascending=False)
        st.dataframe(ent_counts, use_container_width=True, hide_index=True, height=260, column_config={"entity_type": st.column_config.TextColumn("Tipologia record"), "count": st.column_config.NumberColumn("Conteggio", format="%d")})
    with right:
        preview_df = store_df.copy().sort_values(by="updated_at", ascending=False).head(20).copy()
        preview_df["preview_json"] = preview_df["data_json"].astype(str).apply(lambda x: x[:90] + "..." if len(x) > 90 else x)
        preview_df = preview_df[["entity_type", "entity_id", "created_at", "updated_at", "preview_json"]].rename(columns={"entity_type": "Tipologia", "entity_id": "ID record", "created_at": "Creato il", "updated_at": "Aggiornato il", "preview_json": "Anteprima dati"})
        st.dataframe(preview_df, use_container_width=True, hide_index=True, height=420)
    st.divider()
    st.markdown("#### Ispezione dettagliata record")
    record_options = store_df.sort_values(by="updated_at", ascending=False)["entity_id"].tolist() if not store_df.empty else []
    selected_record_id = st.selectbox("Seleziona un record da ispezionare", options=record_options, index=0 if record_options else None, placeholder="Nessun record disponibile")
    if selected_record_id:
        selected_row = store_df[store_df["entity_id"] == selected_record_id].iloc[0]
        d1, d2, d3, d4 = st.columns(4)
        d1.info(f"**Tipologia**\n\n{selected_row['entity_type']}")
        d2.info(f"**ID**\n\n{selected_row['entity_id']}")
        d3.info(f"**Creato il**\n\n{selected_row['created_at']}")
        d4.info(f"**Aggiornato il**\n\n{selected_row['updated_at']}")
        try:
            st.json(json.loads(selected_row["data_json"]), expanded=False)
        except Exception:
            st.code(str(selected_row["data_json"]), language=None)
    st.divider()
    st.subheader("📤 Export bolle e audit")
    export_jobs = format_work_order_export(work_df)
    cexp1, cexp2 = st.columns([1, 1])
    with cexp1:
        if not export_jobs.empty:
            st.download_button("📥 Scarica bolle operative CSV", export_jobs.to_csv(index=False).encode("utf-8"), file_name=f"iveco_bolle_{dt.datetime.now().strftime('%Y%m%d_%H%M')}.csv", mime="text/csv", use_container_width=True)
        else:
            st.info("Nessuna bolla disponibile per l'export.")
    with cexp2:
        st.download_button("📥 Scarica data hub completo CSV", store_df.to_csv(index=False).encode("utf-8"), file_name=f"iveco_data_hub_{dt.datetime.now().strftime('%Y%m%d_%H%M')}.csv", mime="text/csv", use_container_width=True)
    audit_df = pd.DataFrame.from_dict(audit_log, orient="index") if audit_log else pd.DataFrame()
    if not audit_df.empty:
        st.markdown("#### Audit trail")
        st.dataframe(audit_df.sort_values(by="ts", ascending=False), use_container_width=True, hide_index=True, height=320)
