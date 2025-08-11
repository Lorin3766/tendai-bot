# -*- coding: utf-8 -*-
"""
TendAI — чат-первый ассистент здоровья и долголетия
— Естественный диалог (без нижних кнопок), LLM на каждое сообщение.
— Интро-опросник (6 вопросов) с инлайн-кнопками. /intake — запустить вручную.
— Отзывы: 👍/👎 и текстовые комментарии → Google Sheets (лист Feedback) c контекстом.
— Эпизоды, напоминания и чек-ины сохраняются в Google Sheets.
"""

import os, re, json, uuid, logging, hashlib, time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# langdetect (по возможности)
try:
    from langdetect import detect, DetectorFactory
    DetectorFactory.seed = 0
except Exception:
    detect = None

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ===== OpenAI =====
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# ===== Google Sheets =====
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# =========================
# Boot & Config
# =========================
load_dotenv()
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # поставь gpt-5, если доступна
SHEET_NAME      = os.getenv("SHEET_NAME", "TendAI Feedback")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing")

# OpenAI client
oai = OpenAI(api_key=OPENAI_API_KEY) if (OPENAI_API_KEY and OpenAI) else None
logging.info(f"OPENAI enabled={bool(OPENAI_API_KEY)} model={OPENAI_MODEL}")

# Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not creds_json:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")
credentials = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), scope)
gclient = gspread.authorize(credentials)
ss = gclient.open(SHEET_NAME)

def _get_or_create_ws(title: str, headers: list[str]):
    try:
        ws = ss.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=4000, cols=24)
        ws.append_row(headers)
    if not ws.get_all_values():
        ws.append_row(headers)
    return ws

ws_feedback = _get_or_create_ws("Feedback", ["timestamp","user_id","context","username","rating","comment"])
ws_users    = _get_or_create_ws("Users", ["user_id","username","lang","consent","tz_offset","checkin_hour","paused","intake_ts"])
ws_eps      = _get_or_create_ws("Episodes", [
    "episode_id","user_id","topic","started_at","baseline_severity","red_flags",
    "plan_accepted","target","reminder_at","next_checkin_at","status","last_update","notes"
])
ws_intake   = _get_or_create_ws("Intake", [
    "timestamp","user_id","username","lang",
    "age_band","sex_at_birth","chronic","meds","allergy","pregnancy"
])

# =========================
# State (RAM)
# =========================
# sessions[user_id] = {
#   "chat_history": [...],
#   "answers": {...},
#   "mode": "chat"|"await_consent"|"await_rating"|"await_plan"|"await_reminder"|"intake",
#   "episode_id": "...",
#   "awaiting_comment": bool,
#   "feedback_context": str,
#   "last_advice_hash": str,
#   "last_feedback_prompt_ts": float,
#   "intake": {"q":1..6, "ans":{key:value}}
# }
sessions: dict[int, dict] = {}

# =========================
# i18n
# =========================
SUPPORTED = {"ru","en","uk","es"}
def norm_lang(code: str | None) -> str:
    if not code: return "en"
    c = code.split("-")[0].lower()
    if c.startswith("ua"): c = "uk"
    return c if c in SUPPORTED else "en"

T = {
    "ru": {
        "welcome":"Привет! Я TendAI — тёплый ассистент по здоровью и долголетию. Расскажи коротко, что происходит — помогу и предложу аккуратный план.",
        "help":"Команды: /help, /privacy, /pause, /resume, /delete_data, /lang <ru|en|uk|es>, /feedback, /intake",
        "privacy":"Я не заменяю врача. Даю мягкие рекомендации и чек-ины. Данные можно удалить через /delete_data.",
        "consent":"Можно время от времени спрашивать самочувствие? Напишите «да» или «нет».",
        "thanks":"Спасибо, услышал.",
        "checkin_prompt":"Короткий чек-ин: как сейчас по шкале 0–10? Напишите число.",
        "rate_req":"Оцените, пожалуйста, состояние сейчас одним числом 0–10.",
        "plan_try":"Попробуете сегодня? Напишите: «да», «позже» или «нет».",
        "remind_when":"Когда напомнить: «через 4 часа», «вечером», «завтра утром» или «не надо»?",
        "remind_ok":"Принято 🙌",
        "feedback_hint":"Если было полезно — можно отправить 👍 или 👎, и при желании написать короткий отзыв.",
        "deleted":"✅ Данные удалены. /start — начать заново.",
        # Intake
        "intake_intro":"Для большей точности — 60-сек. мини-опрос (по желанию). Можно пропустить.",
        "intake_start":"Начать",
        "intake_skip":"Пропустить",
        "intake_done":"Готово! Спасибо. Можем продолжать.",
        "intake_q1":"Возрастной диапазон?",
        "intake_q2":"Пол при рождении?",
        "intake_q3":"Хронические состояния?",
        "intake_q4":"Регулярные лекарства?",
        "intake_q5":"Аллергии на лекарства?",
        "intake_q6":"Возможна ли беременность сейчас?",
        "use_buttons":"Пожалуйста, выберите вариант кнопкой ниже (или «Пропустить»).",
    },
    "en": {
        "welcome":"Hi! I’m TendAI — a warm health & longevity assistant. Tell me briefly what’s going on and I’ll help with a gentle plan.",
        "help":"Commands: /help, /privacy, /pause, /resume, /delete_data, /lang <ru|en|uk|es>, /feedback, /intake",
        "privacy":"I’m not a doctor. I offer gentle self-care and check-ins. You can wipe data via /delete_data.",
        "consent":"May I check in with you from time to time? Please reply “yes” or “no”.",
        "thanks":"Thanks, got it.",
        "checkin_prompt":"Quick check-in: how is it now (0–10)? Please reply with a number.",
        "rate_req":"Please rate your state now 0–10 with a single number.",
        "plan_try":"Will you try this today? Reply: “yes”, “later” or “no”.",
        "remind_when":"When should I check in: “in 4h”, “this evening”, “tomorrow morning” or “no need”?",
        "remind_ok":"Got it 🙌",
        "feedback_hint":"If this helped, send 👍 or 👎, and add a short comment if you like.",
        "deleted":"✅ Data deleted. /start to begin again.",
        # Intake
        "intake_intro":"For better accuracy, a 60-sec mini-intake (optional). You can skip.",
        "intake_start":"Start",
        "intake_skip":"Skip",
        "intake_done":"All set — thanks. Let’s continue.",
        "intake_q1":"Age range?",
        "intake_q2":"Sex at birth?",
        "intake_q3":"Chronic conditions?",
        "intake_q4":"Regular medications?",
        "intake_q5":"Drug allergies?",
        "intake_q6":"Could you be pregnant now?",
        "use_buttons":"Please pick an option below (or “Skip”).",
    },
    "uk": {
        "welcome":"Привіт! Я TendAI — теплий асистент зі здоров’я та довголіття. Коротко опишіть, що відбувається — підкажу м’який план.",
        "help":"Команди: /help, /privacy, /pause, /resume, /delete_data, /lang <ru|en|uk|es>, /feedback, /intake",
        "privacy":"Я не лікар. Пропоную м’які кроки та чек-іни. Дані можна стерти через /delete_data.",
        "consent":"Можу час від часу писати, щоб дізнатись, як ви? Відповідь: «так» або «ні».",
        "thanks":"Дякую, почув.",
        "checkin_prompt":"Короткий чек-ін: як зараз (0–10)? Напишіть число.",
        "rate_req":"Оцініть, будь ласка, 0–10 одним числом.",
        "plan_try":"Спробуєте сьогодні? Відповідь: «так», «пізніше» або «ні».",
        "remind_when":"Коли нагадати: «через 4 год», «увечері», «завтра вранці» чи «не треба»?",
        "remind_ok":"Прийнято 🙌",
        "feedback_hint":"Якщо було корисно — надішліть 👍 або 👎 і короткий коментар.",
        "deleted":"✅ Дані видалено. /start — почати знову.",
        # Intake
        "intake_intro":"Для кращої точності — 60-сек. міні-опитувальник (за бажанням). Можна пропустити.",
        "intake_start":"Почати",
        "intake_skip":"Пропустити",
        "intake_done":"Готово! Дякуємо. Продовжимо.",
        "intake_q1":"Віковий діапазон?",
        "intake_q2":"Стать при народженні?",
        "intake_q3":"Хронічні стани?",
        "intake_q4":"Регулярні ліки?",
        "intake_q5":"Алергії на ліки?",
        "intake_q6":"Чи можлива вагітність зараз?",
        "use_buttons":"Будь ласка, оберіть варіант нижче (або «Пропустити»).",
    },
    "es": {
        "welcome":"¡Hola! Soy TendAI — un asistente cálido de salud y longevidad. Cuéntame brevemente y te daré un plan suave.",
        "help":"Comandos: /help, /privacy, /pause, /resume, /delete_data, /lang <ru|en|uk|es>, /feedback, /intake",
        "privacy":"No soy médico. Ofrezco autocuidado y seguimientos. Borra tus datos con /delete_data.",
        "consent":"¿Puedo escribirte de vez en cuando para revisar? Responde «sí» o «no».",
        "thanks":"¡Gracias!",
        "checkin_prompt":"Revisión rápida: ¿cómo estás ahora (0–10)? Escribe un número.",
        "rate_req":"Valóralo ahora 0–10 con un solo número.",
        "plan_try":"¿Lo intentas hoy? Responde: «sí», «más tarde» o «no».",
        "remind_when":"¿Cuándo te escribo: «en 4 h», «esta tarde», «mañana por la mañana» o «no hace falta»?",
        "remind_ok":"¡Hecho! 🙌",
        "feedback_hint":"Si te ayudó, envía 👍 o 👎 y, si quieres, un breve comentario.",
        "deleted":"✅ Datos borrados. /start para empezar de nuevo.",
        # Intake
        "intake_intro":"Para mayor precisión, un mini-cuestionario de 60 s (opcional). Puedes omitirlo.",
        "intake_start":"Empezar",
        "intake_skip":"Omitir",
        "intake_done":"Listo, gracias. Sigamos.",
        "intake_q1":"Rango de edad?",
        "intake_q2":"Sexo al nacer?",
        "intake_q3":"Enfermedades crónicas?",
        "intake_q4":"Medicaciones habituales?",
        "intake_q5":"Alergias a fármacos?",
        "intake_q6":"¿Podrías estar embarazada ahora?",
        "use_buttons":"Elige una opción abajo (o «Omitir»).",
    },
}
def t(lang: str, key: str) -> str:
    return T.get(lang, T["en"]).get(key, T["en"].get(key, key))

# =========================
# Intake options & keyboards
# =========================
INTAKE_OPTS = {
    "ru": {
        "q1":[("18-29","18–29"),("30-44","30–44"),("45-59","45–59"),("60-74","60–74"),("75+","75+")],
        "q2":[("M","Мужской"),("F","Женский"),("NA","Предпочту не указывать")],
        "q3":[("none","Нет"),("cardio_htn","Сердце/гипертония"),("diab","Сахарный диабет"),("asthma","Астма/ХОБЛ"),("kidney_liver","Почки/печень"),("autoimm","Аутоиммунные/иммуносупр."),("other","Другое")],
        "q4":[("none","Нет"),("anticoag","Антикоагулянты"),("steroids","Стероиды/иммуносупр."),("other","Другое регулярно")],
        "q5":[("none","Нет"),("nsaids","НПВС (ибупрофен и т.п.)"),("abx","Антибиотики"),("other","Другое")],
        "q6":[("yes","Да"),("no","Нет"),("na","Н/Д")],
    },
    "en": {
        "q1":[("18-29","18–29"),("30-44","30–44"),("45-59","45–59"),("60-74","60–74"),("75+","75+")],
        "q2":[("M","Male"),("F","Female"),("NA","Prefer not say")],
        "q3":[("none","None"),("cardio_htn","Heart/Hypertension"),("diab","Diabetes"),("asthma","Asthma/COPD"),("kidney_liver","Kidney/Liver"),("autoimm","Autoimmune/Immunosupp."),("other","Other")],
        "q4":[("none","None"),("anticoag","Anticoagulants"),("steroids","Steroids/Immunosupp."),("other","Other regular")],
        "q5":[("none","None"),("nsaids","NSAIDs (ibuprofen etc.)"),("abx","Antibiotics"),("other","Other")],
        "q6":[("yes","Yes"),("no","No"),("na","N/A")],
    },
    "uk": {
        "q1":[("18-29","18–29"),("30-44","30–44"),("45-59","45–59"),("60-74","60–74"),("75+","75+")],
        "q2":[("M","Чоловіча"),("F","Жіноча"),("NA","Не вказувати")],
        "q3":[("none","Немає"),("cardio_htn","Серце/Гіпертензія"),("diab","Діабет"),("asthma","Астма/ХОЗЛ"),("kidney_liver","Нирки/печінка"),("autoimm","Аутоімунні/імунодепр."),("other","Інше")],
        "q4":[("none","Немає"),("anticoag","Антикоагулянти"),("steroids","Стероїди/імунодепр."),("other","Інше регулярно")],
        "q5":[("none","Немає"),("nsaids","НПЗП (ібупрофен тощо)"),("abx","Антибіотики"),("other","Інше")],
        "q6":[("yes","Так"),("no","Ні"),("na","Н/Д")],
    },
    "es": {
        "q1":[("18-29","18–29"),("30-44","30–44"),("45-59","45–59"),("60-74","60–74"),("75+","75+")],
        "q2":[("M","Masculino"),("F","Femenino"),("NA","Prefiero no decir")],
        "q3":[("none","Ninguna"),("cardio_htn","Corazón/Hipertensión"),("diab","Diabetes"),("asthma","Asma/EPOC"),("kidney_liver","Riñón/Hígado"),("autoimm","Autoinm./Inmunosup."),("other","Otra")],
        "q4":[("none","Ninguna"),("anticoag","Anticoagulantes"),("steroids","Esteroides/Inmunosup."),("other","Otra habitual")],
        "q5":[("none","Ninguna"),("nsaids","AINEs (ibuprofeno)"),("abx","Antibióticos"),("other","Otra")],
        "q6":[("yes","Sí"),("no","No"),("na","N/A")],
    },
}
def kb_intake_start(lang: str):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(t(lang,"intake_start"), callback_data="intake|start"),
        InlineKeyboardButton(t(lang,"intake_skip"),  callback_data="intake|skip"),
    ]])
def kb_intake_q(lang: str, qnum: int):
    key = f"q{qnum}"
    rows = []
    buf=[]
    for code, label in INTAKE_OPTS[lang][key]:
        buf.append(InlineKeyboardButton(label, callback_data=f"intake|q|{qnum}|{code}"))
        if len(buf)==3:
            rows.append(buf); buf=[]
    if buf: rows.append(buf)
    rows.append([InlineKeyboardButton(t(lang,"intake_skip"), callback_data="intake|skip")])
    return InlineKeyboardMarkup(rows)

# =========================
# Sheets helpers
# =========================
def now_utc(): return datetime.now(timezone.utc)
def iso(dt: datetime | None) -> str:
    return "" if not dt else dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")

def users_row_idx(uid: int) -> int | None:
    for i, row in enumerate(ws_users.get_all_records(), start=2):
        if str(row.get("user_id")) == str(uid): return i
    return None
def users_get(uid: int) -> dict:
    for row in ws_users.get_all_records():
        if str(row.get("user_id")) == str(uid): return row
    return {}
def users_upsert(uid: int, username: str, lang: str):
    idx = users_row_idx(uid)
    row = [str(uid), username or "", lang, "no", "0", "", "no", ""]
    if idx: ws_users.update(f"A{idx}:H{idx}", [row])
    else:   ws_users.append_row(row)
def users_set(uid: int, field: str, value: str):
    idx = users_row_idx(uid)
    if not idx: return
    headers = ws_users.row_values(1)
    if field in headers:
        ws_users.update_cell(idx, headers.index(field)+1, value)

def episode_create(uid: int, topic: str, baseline_sev: int, red: str) -> str:
    eid = f"{uid}-{uuid.uuid4().hex[:8]}"
    ws_eps.append_row([eid, str(uid), topic, iso(now_utc()), str(baseline_sev), red,
                       "0","<=3/10","","","open", iso(now_utc()), ""])
    return eid
def episode_find_open(uid: int) -> dict | None:
    for row in ws_eps.get_all_records():
        if str(row.get("user_id")) == str(uid) and row.get("status") == "open":
            return row
    return None
def episode_set(eid: str, field: str, value: str):
    vals = ws_eps.get_all_values(); headers = vals[0]
    if field not in headers: return
    col = headers.index(field)+1
    for i in range(2, len(vals)+1):
        if ws_eps.cell(i,1).value == eid:
            ws_eps.update_cell(i, col, value)
            ws_eps.update_cell(i, headers.index("last_update")+1, iso(now_utc()))
            return

def intake_save(uid: int, username: str, lang: str, ans: dict):
    ws_intake.append_row([
        datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        str(uid), username or "", lang,
        ans.get("age_band",""), ans.get("sex_at_birth",""),
        ans.get("chronic",""), ans.get("meds",""),
        ans.get("allergy",""), ans.get("pregnancy",""),
    ])
    users_set(uid, "intake_ts", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

def intake_exists_recent(uid: int, months=6) -> bool:
    rows = ws_intake.get_all_records()
    latest = None
    for r in rows:
        if str(r.get("user_id")) == str(uid):
            ts = r.get("timestamp")
            try:
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                if (latest is None) or dt > latest: latest = dt
            except Exception:
                pass
    if not latest: return False
    return (datetime.utcnow() - latest).days < months*30

# =========================
# Feedback helpers (Sheets)
# =========================
def save_feedback(uid: int, username: str, context_label: str, rating: str, comment: str):
    """Единая точка записи отзывов в лист Feedback."""
    try:
        ws_feedback.append_row([
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            str(uid),
            context_label or "general",
            username or "",
            rating,  # "1" | "0" | "" (если только комментарий)
            comment or ""
        ])
        logging.info(f"Feedback saved: uid={uid} ctx={context_label} rating={rating} comment_len={len(comment or '')}")
    except Exception as e:
        logging.error(f"Feedback save error: {e}")

# =========================
# LLM core (chat-first)
# =========================
SYS_PROMPT = (
    "You are TendAI, a professional, warm health & longevity coach. "
    "Speak in the user's language. 2–5 sentences. Natural, supportive, specific. "
    "Never diagnose; no fear. Ask ONE focused follow-up when data is missing. "
    "For weakness/fatigue and common complaints, consider context questions: training/heat, sleep, nutrition/hydration, "
    "bowel/urination, stress, sick contacts. Encourage a 0–10 self-rating or what activities are limited. "
    "Provide a tiny micro-plan (3 concise steps) when appropriate. "
    "Add one-line red flags: high fever, shortness of breath, chest pain, one-sided weakness; advise medical care if present. "
    "Offer to close the loop: propose a check-in later (evening or next morning). "
    "Do NOT show buttons; present choices inline as short phrases. "
    "Return ONLY JSON with keys: "
    "assistant (string), "
    "next_action (one of: followup, rate_0_10, confirm_plan, pick_reminder, escalate, ask_feedback, none), "
    "slots (object; may include: intent in [pain, throat, sleep, stress, digestion, energy]; "
    "loc in [Head, Throat, Back, Belly, Chest, Other]; kind in [Dull, Sharp, Throbbing, Burning, Pressing]; "
    "duration (string), severity (int 0..10), red (string among [High fever, Vomiting, Weakness/numbness, Speech/vision issues, Trauma, None])), "
    "plan_steps (array of strings, optional)."
)

def _force_json(messages, temperature=0.2, max_tokens=500):
    if not oai: return None
    try:
        return oai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type":"json_object"},
            messages=messages,
        )
    except Exception as e:
        logging.warning(f"response_format fallback: {e}")
        return oai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=messages,
        )

def _json_from(raw: str) -> dict:
    raw = (raw or "").strip()
    try:
        m = re.search(r"\{[\s\S]*\}", raw)
        return json.loads(m.group(0)) if m else json.loads(raw)
    except Exception:
        return {}

def llm_chat(uid: int, lang: str, user_text: str) -> dict:
    hist = sessions.setdefault(uid, {}).setdefault("chat_history", [])[-12:]
    messages = [{"role":"system","content":SYS_PROMPT}] + hist + [{"role":"user","content":f"[lang={lang}] {user_text}"}]
    try:
        resp = _force_json(messages)
        content = (resp.choices[0].message.content or "").strip()
        data = _json_from(content)
        if not isinstance(data, dict):
            data = {}
        # save compact natural text into history
        a = data.get("assistant","")
        hist.append({"role":"user","content":user_text[:1000]})
        if a: hist.append({"role":"assistant","content":a[:1000]})
        sessions[uid]["chat_history"] = hist[-14:]
        logging.info(f"LLM ok | next_action={data.get('next_action')}")
        return data
    except Exception as e:
        logging.warning(f"LLM error: {e}")
        return {}

# =========================
# Simple NLP helpers
# =========================
def parse_rating(s: str):
    m = re.search(r"\b(10|[0-9])\b", s.strip())
    return int(m.group(1)) if m else None

YES = {
    "ru":{"да","ага","ок","хорошо","готов","сделаю"},
    "en":{"yes","ok","sure","ready","will do","yep","yeah"},
    "uk":{"так","ок","гаразд","зроблю","готовий","готова"},
    "es":{"sí","si","ok","vale","listo","lista"},
}
LATER = {
    "ru":{"позже","потом","не сейчас"},
    "en":{"later","not now"},
    "uk":{"пізніше","не зараз"},
    "es":{"más tarde","luego","no ahora"},
}
NO = {
    "ru":{"нет","не","не буду","не хочу"},
    "en":{"no","nope"},
    "uk":{"ні","не буду"},
    "es":{"no"},
}
def is_yes(lang, s): return s.lower() in YES.get(lang,set())
def is_no(lang, s): return s.lower() in NO.get(lang,set())
def is_later(lang, s): return s.lower() in LATER.get(lang,set())

def parse_reminder_code(lang: str, s: str) -> str:
    tl = s.lower()
    if any(k in tl for k in ["4h","4 h","через 4","4 часа","4 год","en 4 h","4 horas"]): return "4h"
    if any(k in tl for k in ["вечер","вечером","evening","esta tarde","увечері","вечір"]): return "evening"
    if any(k in tl for k in ["утро","утром","morning","mañana","завтра утром","завтра вранці"]): return "morning"
    if any(k in tl for k in ["не надо","не нужно","no need","none","no hace falta"]): return "none"
    return ""

# =========================
# Advice de-dupe
# =========================
def _hash_text(s: str) -> str: return hashlib.sha1((s or "").encode("utf-8")).hexdigest()
def send_nodup(uid: int, text: str, send_fn):
    if not text: return
    s = sessions.setdefault(uid, {})
    h = _hash_text(text)
    if s.get("last_advice_hash") == h:
        return
    s["last_advice_hash"] = h
    sessions[uid] = s
    return send_fn(text)

# =========================
# Jobs (check-ins)
# =========================
async def job_checkin(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    uid = data.get("user_id"); eid = data.get("episode_id")
    if not uid or not eid: return
    u = users_get(uid)
    if (u.get("paused") or "").lower() == "yes": return
    lang = u.get("lang") or "en"
    try:
        await context.bot.send_message(uid, t(lang,"checkin_prompt"))
        s = sessions.setdefault(uid, {})
        s["mode"] = "await_rating"
        s["episode_id"] = eid
        episode_set(eid, "next_checkin_at", "")
    except Exception as e:
        logging.error(f"job_checkin send error: {e}")

def reschedule_from_sheet(app):
    for row in ws_eps.get_all_records():
        if row.get("status") != "open": continue
        nca = row.get("next_checkin_at") or ""
        if not nca: continue
        try:
            dt = datetime.strptime(nca, "%Y-%m-%d %H:%M:%S%z")
        except Exception:
            continue
        delay = (dt - datetime.now(timezone.utc)).total_seconds()
        if delay < 60: delay = 60
        app.job_queue.run_once(job_checkin, when=delay, data={"user_id": int(row["user_id"]), "episode_id": row["episode_id"]})

# =========================
# Commands
# =========================
async def on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    reschedule_from_sheet(app)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    lang = users_get(uid).get("lang")
    if not lang:
        txt = (update.message.text or "").strip() if update.message else ""
        cand = None
        if detect:
            try: cand = detect(txt) if txt else None
            except Exception: cand = None
        lang = norm_lang(cand or getattr(user,"language_code",None))
        users_upsert(uid, user.username or "", lang)

    await update.message.reply_text(t(lang,"welcome"))

    # предложим с порога мини-интро-опрос если не проходил недавно
    if not intake_exists_recent(uid):
        s = sessions.setdefault(uid, {"mode":"chat","answers":{}, "chat_history":[]})
        s["mode"] = "intake"
        s["intake"] = {"q":0, "ans":{}}
        await update.message.reply_text(t(lang,"intake_intro"), reply_markup=kb_intake_start(lang))
    else:
        s = sessions.setdefault(uid, {"mode":"chat","answers":{}, "chat_history":[]})
        if (users_get(uid).get("consent") or "").lower() not in {"yes","no"}:
            s["mode"]="await_consent"
            await update.message.reply_text(t(lang,"consent"))

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang,"help"))

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang,"privacy"))

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid,"paused","yes"); await update.message.reply_text("⏸️ Paused.")

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid,"paused","no"); await update.message.reply_text("▶️ Resumed.")

async def cmd_delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    idx = users_row_idx(uid)
    if idx: ws_users.delete_rows(idx)
    vals = ws_eps.get_all_values(); to_del=[]
    for i in range(2, len(vals)+1):
        if ws_eps.cell(i,2).value == str(uid): to_del.append(i)
    for j, row_i in enumerate(to_del):
        ws_eps.delete_rows(row_i - j)
    await update.message.reply_text(t(norm_lang(getattr(update.effective_user,"language_code",None)),"deleted"))

async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /lang ru|en|uk|es"); return
    cand = norm_lang(context.args[0])
    if cand not in SUPPORTED:
        await update.message.reply_text("Usage: /lang ru|en|uk|es"); return
    users_set(uid,"lang",cand); await update.message.reply_text("✅ Language set.")

async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = sessions.setdefault(uid,{})
    s["awaiting_comment"]=True
    s["feedback_context"]= s.get("feedback_context") or "manual"
    await update.message.reply_text("Напишите короткий отзыв одним сообщением. Можно также просто отправить 👍 или 👎.")

async def cmd_intake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
    s = sessions.setdefault(uid, {"mode":"chat","answers":{}, "chat_history":[]})
    s["mode"]="intake"; s["intake"]={"q":0, "ans":{}}
    await update.message.reply_text(t(lang,"intake_intro"), reply_markup=kb_intake_start(lang))

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = sessions.setdefault(uid,{})
    s["awaiting_comment"]=False
    await update.message.reply_text("Ок, пропустили.")

# =========================
# Callback (intake)
# =========================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(q.from_user,"language_code",None))
    s = sessions.setdefault(uid, {"mode":"chat","answers":{}, "chat_history":[]})

    data = (q.data or "")
    if not data.startswith("intake|"):
        await q.message.reply_text("Пожалуйста, отвечайте текстом.")
        return

    parts = data.split("|")
    if len(parts) >= 2 and parts[1] == "start":
        s["mode"]="intake"; s["intake"]={"q":1,"ans":{}}
        await q.message.reply_text(t(lang,"intake_q1"), reply_markup=kb_intake_q(lang,1))
        return
    if len(parts) >= 2 and parts[1] == "skip":
        s["mode"]="chat"
        await q.message.reply_text(t(lang,"intake_done"))
        # предложим согласие на чек-ины, если не задано
        if (users_get(uid).get("consent") or "").lower() not in {"yes","no"}:
            s["mode"]="await_consent"
            await q.message.reply_text(t(lang,"consent"))
        return

    if len(parts) == 4 and parts[1] == "q":
        # intake|q|<num>|<code>
        try:
            qnum = int(parts[2]); code = parts[3]
        except Exception:
            return
        it = s.setdefault("intake", {"q":1, "ans":{}})
        # map code into structured keys
        keymap = {1:"age_band", 2:"sex_at_birth", 3:"chronic", 4:"meds", 5:"allergy", 6:"pregnancy"}
        it["ans"][ keymap[qnum] ] = code

        if qnum < 6:
            it["q"] = qnum + 1
            await q.message.reply_text(t(lang, f"intake_q{qnum+1}"), reply_markup=kb_intake_q(lang, qnum+1))
            return
        else:
            # save intake
            intake_save(uid, q.from_user.username or "", lang, it["ans"])
            s["mode"]="chat"; s["intake"]={"q":0, "ans":{}}
            await q.message.reply_text(t(lang,"intake_done"))
            if (users_get(uid).get("consent") or "").lower() not in {"yes","no"}:
                s["mode"]="await_consent"
                await q.message.reply_text(t(lang,"consent"))
            return

# =========================
# CHAT & Feedback handling
# =========================
THUMBS_UP = {"👍","👍🏻","👍🏼","👍🏽","👍🏾","👍🏿"}
THUMBS_DOWN = {"👎","👎🏻","👎🏼","👎🏽","👎🏾","👎🏿"}

def set_feedback_context(uid: int, context_label: str):
    s = sessions.setdefault(uid,{})
    s["feedback_context"] = context_label

def get_feedback_context(uid: int) -> str:
    return sessions.setdefault(uid,{}).get("feedback_context") or "chat"

def feedback_prompt_needed(uid: int, interval_sec=180.0) -> bool:
    s = sessions.setdefault(uid,{})
    last = s.get("last_feedback_prompt_ts", 0.0)
    now = time.time()
    if now - last > interval_sec:
        s["last_feedback_prompt_ts"] = now
        return True
    return False

# =========================
# Fallback plan (если модель не дала plan_steps)
# =========================
def fallback_plan(lang: str, ans: dict) -> list[str]:
    sev = int(ans.get("severity", 5))
    red = (ans.get("red") or "None").lower()
    urgent = any(w in red for w in ["fever","shortness","breath","одыш","chest","перед","weakness","односторон"]) and sev >= 7
    if urgent:
        return {
            "ru":[ "⚠️ Есть признаки возможной угрозы. Пожалуйста, обратитесь за медицинской помощью." ],
            "en":[ "⚠️ Some answers suggest urgent risks. Please seek medical care as soon as possible." ],
            "uk":[ "⚠️ Є ознаки можливої загрози. Зверніться до лікаря." ],
            "es":[ "⚠️ Posibles signos de urgencia. Busca atención médica lo antes posible." ],
        }[lang]
    base = {
        "ru":[ "1) Вода 400–600 мл и 15–20 минут тишины.", "2) Если нет противопоказаний — ибупрофен 200–400 мг 1 раз с едой.", "3) Пауза от экранов 30–60 мин." ],
        "en":[ "1) 400–600 ml water + 15–20 min quiet rest.", "2) If no contraindications — ibuprofen 200–400 mg once with food.", "3) Screen break 30–60 min." ],
        "uk":[ "1) 400–600 мл води + 15–20 хв тиші.", "2) Якщо немає протипоказань — ібупрофен 200–400 мг 1 раз із їжею.", "3) Перерва від екранів 30–60 хв." ],
        "es":[ "1) 400–600 ml de agua + 15–20 min de descanso.", "2) Si no hay contraindicaciones — ibuprofeno 200–400 mg una vez con comida.", "3) Descanso de pantallas 30–60 min." ],
    }[lang]
    return base

# =========================
# Text handler (chat-first)
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    text = (update.message.text or "").strip()

    # язык
    urec = users_get(uid)
    if not urec:
        cand=None
        if detect:
            try: cand = detect(text) if text else None
            except Exception: cand=None
        lang = norm_lang(cand or getattr(user,"language_code",None))
        users_upsert(uid, user.username or "", lang)
    else:
        lang = norm_lang(urec.get("lang") or getattr(user,"language_code",None))

    s = sessions.setdefault(uid, {"mode":"chat","answers":{}, "chat_history":[]})

    # ========== Отзывы: 👍/👎 и текст ==========
    if text in THUMBS_UP:
        ctx_label = get_feedback_context(uid)
        save_feedback(uid, user.username or "", ctx_label, "1", "")
        await update.message.reply_text("Спасибо за 👍")
        return
    if text in THUMBS_DOWN:
        ctx_label = get_feedback_context(uid)
        save_feedback(uid, user.username or "", ctx_label, "0", "")
        await update.message.reply_text("Спасибо за 👎 — учту и буду полезнее.")
        return
    if s.get("awaiting_comment") and not text.startswith("/"):
        ctx_label = get_feedback_context(uid)
        save_feedback(uid, user.username or "", ctx_label, "", text)
        s["awaiting_comment"]=False
        await update.message.reply_text("Отзыв сохранён 🙌")
        return

    # ========== Ожидание согласия ==========
    if s.get("mode") == "await_consent":
        low = text.lower()
        if is_yes(lang, low):
            users_set(uid,"consent","yes"); s["mode"]="chat"
            await update.message.reply_text(t(lang,"thanks")); return
        if is_no(lang, low):
            users_set(uid,"consent","no"); s["mode"]="chat"
            await update.message.reply_text(t(lang,"thanks")); return
        await update.message.reply_text("Пожалуйста, напишите «да» или «нет»."); return

    # ========== Чек-ин (0–10) ==========
    if s.get("mode") == "await_rating":
        rating = parse_rating(text)
        if rating is None or not (0 <= rating <= 10):
            await update.message.reply_text(t(lang,"rate_req")); return
        ep = episode_find_open(uid)
        if ep:
            eid = ep["episode_id"]
            episode_set(eid,"notes",f"checkin:{rating}")
            set_feedback_context(uid, "checkin")
            if rating <= 3:
                episode_set(eid,"status","resolved")
                await update.message.reply_text({"ru":"Отлично! Рад за прогресс 💪","en":"Great! Love the progress 💪","uk":"Чудово! Гарний прогрес 💪","es":"¡Genial! Buen progreso 💪"}[lang])
            else:
                await update.message.reply_text({"ru":"Понимаю. Если появятся красные флаги — лучше обратиться к врачу.","en":"I hear you. If red flags appear, please consider medical help.","uk":"Розумію. Якщо з’являться «червоні прапорці», зверніться до лікаря.","es":"Entiendo. Si aparecen señales de alarma, consulta a un médico."}[lang])
        s["mode"]="chat"
        if feedback_prompt_needed(uid):
            await update.message.reply_text(t(lang,"feedback_hint"))
        return

    # ========== Подтверждение плана ==========
    if s.get("mode") == "await_plan":
        low = text.lower(); eid = s.get("episode_id")
        set_feedback_context(uid, "plan")
        if is_yes(lang, low):
            if eid: episode_set(eid,"plan_accepted","1")
            s["mode"]="await_reminder"
            await update.message.reply_text(t(lang,"remind_when")); return
        if is_later(lang, low):
            if eid: episode_set(eid,"plan_accepted","later")
            s["mode"]="await_reminder"
            await update.message.reply_text(t(lang,"remind_when")); return
        if is_no(lang, low):
            if eid: episode_set(eid,"plan_accepted","0")
            s["mode"]="chat"
            await update.message.reply_text({"ru":"Хорошо, без плана. Можем просто отслеживать самочувствие.","en":"Alright, no plan. We can just track how you feel.","uk":"Добре, без плану. Можемо просто відстежувати самопочуття.","es":"De acuerdo, sin plan. Podemos solo revisar cómo sigues."}[lang])
            if feedback_prompt_needed(uid):
                await update.message.reply_text(t(lang,"feedback_hint"))
            return
        await update.message.reply_text({"ru":"Ответьте «да», «позже» или «нет».","en":"Please reply “yes”, “later” or “no”.","uk":"Відповідайте «так», «пізніше» або «ні».","es":"Responde «sí», «más tarde» o «no»."}[lang])
        return

    # ========== Выбор напоминания ==========
    if s.get("mode") == "await_reminder":
        code = parse_reminder_code(lang, text)
        if not code:
            await update.message.reply_text(t(lang,"remind_when")); return
        urec = users_get(uid); tz_off = 0
        try: tz_off = int(urec.get("tz_offset") or "0")
        except Exception: tz_off = 0
        nowu = datetime.now(timezone.utc); user_now = nowu + timedelta(hours=tz_off)
        if code == "4h":
            target_user = user_now + timedelta(hours=4)
        elif code == "evening":
            target_user = user_now.replace(hour=19, minute=0, second=0, microsecond=0)
            if target_user < user_now: target_user += timedelta(days=1)
        elif code == "morning":
            target_user = user_now.replace(hour=9, minute=0, second=0, microsecond=0)
            if target_user < user_now: target_user += timedelta(days=1)
        else:
            target_user = None

        eid = s.get("episode_id")
        if target_user and eid:
            target_utc = target_user - timedelta(hours=tz_off)
            episode_set(eid,"next_checkin_at", iso(target_utc))
            delay = max(60, (target_utc - nowu).total_seconds())
            context.job_queue.run_once(job_checkin, when=delay, data={"user_id": uid, "episode_id": eid})
        await update.message.reply_text(t(lang,"remind_ok"))
        s["mode"]="chat"
        if feedback_prompt_needed(uid):
            await update.message.reply_text(t(lang,"feedback_hint"))
        return

    # ========== Intake идёт только через callback ==========
    if s.get("mode") == "intake":
        await update.message.reply_text(t(lang,"use_buttons"))
        return

    # ========== CHAT-FIRST (LLM) ==========
    data = llm_chat(uid, lang, text)
    if not data:
        # мягкий фолбэк без шаблонов
        if lang=="ru":
            await update.message.reply_text("Понимаю. Где именно ощущаете и как давно началось? Если можно — оцените по шкале 0–10.")
        elif lang=="uk":
            await update.message.reply_text("Розумію. Де саме і відколи це почалось? Якщо можете — оцініть 0–10.")
        elif lang=="es":
            await update.message.reply_text("Entiendo. ¿Dónde exactamente y desde cuándo empezó? Si puedes, valora 0–10.")
        else:
            await update.message.reply_text("I hear you. Where exactly is it and since when? If you can, rate it 0–10.")
        return

    assistant = data.get("assistant") or ""
    if assistant:
        set_feedback_context(uid, "chat")
        await send_nodup(uid, assistant, update.message.reply_text)

    # сохранить слоты
    ans = s.setdefault("answers", {})
    for k in ["intent","loc","kind","duration","severity","red"]:
        v = (data.get("slots") or {}).get(k)
        if v not in (None,""): ans[k]=v

    # если модель уже вернула шаги плана — покажем
    plan_steps = data.get("plan_steps") or []
    if plan_steps:
        set_feedback_context(uid, "plan")
        await send_nodup(uid, "\n".join(plan_steps), update.message.reply_text)

    na = data.get("next_action") or "followup"

    if na == "rate_0_10":
        s["mode"]="await_rating"
        await update.message.reply_text(t(lang,"rate_req"))
        return

    if na == "confirm_plan":
        # если нет эпизода — создадим
        eid = s.get("episode_id")
        if not eid:
            eid = episode_create(uid, ans.get("intent","pain"), int(ans.get("severity",5) or 5), ans.get("red","None") or "None")
            s["episode_id"]=eid
        # если модель не дала план — подстрахуемся
        if not plan_steps:
            set_feedback_context(uid, "plan")
            await send_nodup(uid, "\n".join(fallback_plan(lang, ans)), update.message.reply_text)
        s["mode"]="await_plan"
        await update.message.reply_text(t(lang,"plan_try"))
        return

    if na == "pick_reminder":
        s["mode"]="await_reminder"
        await update.message.reply_text(t(lang,"remind_when"))
        return

    if na == "escalate":
        set_feedback_context(uid, "chat")
        esc = {
            "ru":"⚠️ Некоторым ответам лучше уделить внимание очно. Если есть высокая температура, одышка, боль в груди или односторонняя слабость — обратитесь к врачу.",
            "en":"⚠️ Some answers are concerning. If high fever, shortness of breath, chest pain or one-sided weakness — seek medical care.",
            "uk":"⚠️ Деякі відповіді тривожні. Якщо висока температура, задишка, біль у грудях або однобічна слабкість — зверніться до лікаря.",
            "es":"⚠️ Algunas respuestas son preocupantes. Si hay fiebre alta, falta de aire, dolor en el pecho o debilidad de un lado — busca atención médica.",
        }[lang]
        await send_nodup(uid, esc, update.message.reply_text)
        if feedback_prompt_needed(uid):
            await update.message.reply_text(t(lang,"feedback_hint"))
        return

    if na == "ask_feedback" and feedback_prompt_needed(uid):
        await update.message.reply_text(t(lang,"feedback_hint"))
        # контекст оставляем текущий (обычно "chat" или "plan")
        return

    # иначе — свободный чат
    s["mode"]="chat"
    sessions[uid]=s

# =========================
# Runner
# =========================
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_startup).build()

    reschedule_from_sheet(app)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("delete_data", cmd_delete_data))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("feedback", cmd_feedback))
    app.add_handler(CommandHandler("intake", cmd_intake))
    app.add_handler(CommandHandler("skip", cmd_skip))

    app.add_handler(CallbackQueryHandler(on_callback))  # intake инлайн-кнопки

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
