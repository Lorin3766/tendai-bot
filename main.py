# -*- coding: utf-8 -*-
import os, re, json, uuid, logging
from time import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

# langdetect по возможности
try:
    from langdetect import detect, DetectorFactory
    DetectorFactory.seed = 0
except Exception:
    detect = None

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # ← поставь gpt-5 если доступен
SHEET_NAME = os.getenv("SHEET_NAME", "TendAI Feedback")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing")

# OpenAI client
oai = OpenAI(api_key=OPENAI_API_KEY) if (OPENAI_API_KEY and OpenAI) else None
logging.info(f"OPENAI enabled: {bool(OPENAI_API_KEY)} | client: {bool(oai)} | model: {OPENAI_MODEL}")

# Google Sheets init
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not creds_json:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")
creds_dict = json.loads(creds_json)
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gclient = gspread.authorize(credentials)
ss = gclient.open(SHEET_NAME)

def _get_or_create_ws(title: str, headers: list[str]):
    try:
        ws = ss.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=2000, cols=20)
        ws.append_row(headers)
    vals = ws.get_all_values()
    if not vals:
        ws.append_row(headers)
    return ws

ws_feedback = _get_or_create_ws(
    "Feedback", ["timestamp", "user_id", "name", "username", "rating", "comment"]
)
ws_users = _get_or_create_ws(
    "Users", ["user_id", "username", "lang", "consent", "tz_offset", "checkin_hour", "paused"]
)
ws_episodes = _get_or_create_ws(
    "Episodes",
    [
        "episode_id", "user_id", "topic", "started_at",
        "baseline_severity", "red_flags", "plan_accepted",
        "target", "reminder_at", "next_checkin_at",
        "status", "last_update", "notes",
    ],
)

# =========================
# State (RAM)
# =========================
sessions: dict[int, dict] = {}

# =========================
# i18n
# =========================
SUPPORTED = {"ru", "en", "uk", "es"}
def norm_lang(code: str | None) -> str:
    if not code:
        return "en"
    c = code.split("-")[0].lower()
    if c.startswith("ua"):
        c = "uk"
    return c if c in SUPPORTED else "en"

T = {
    "en": {
        "welcome": "Hi! I’m TendAI — your health & longevity assistant.\nChoose a topic below or briefly tell me what’s going on.",
        "menu": ["Pain", "Throat/Cold", "Sleep", "Stress", "Digestion", "Energy"],
        "help": "I help with short checkups, a 24–48h plan, and gentle follow-ups.\nCommands: /help, /privacy, /pause, /resume, /delete_data, /lang, /feedback",
        "privacy": "TendAI isn’t a medical service. We store minimal data for reminders. /delete_data to erase.",
        "paused_on": "Notifications paused. Use /resume to enable.",
        "paused_off": "Notifications resumed.",
        "deleted": "All your data was deleted. You can /start again anytime.",
        "ask_consent": "May I check in with you later about how you feel?",
        "yes": "Yes", "no": "No",
        "plan_header": "Your 24–48h plan:",
        "plan_accept": "Will you try this today?",
        "accept_opts": ["✅ Yes", "🔁 Later", "✖️ No"],
        "remind_when": "When shall I check on you?",
        "remind_opts": ["in 4h", "this evening", "tomorrow morning", "no need"],
        "thanks": "Got it 🙌",
        "checkin_ping": "Quick check-in: how is it now (0–10)?",
        "checkin_better": "Nice! Keep it up 💪",
        "checkin_worse": "Sorry to hear. If you have red flags or pain ≥7/10, please consider medical help.",
        "comment_prompt": "Write your comment now. Or send /skip to pass.",
        "comment_saved": "Feedback saved, thank you! 🙌",
        "skip_ok": "Skipped.",
        "help_lang": "Use /lang ru|en|uk|es to change language.",
        "oos": "I’m here for health, self-care and habits. Tell me what’s going on or pick a topic below.",
        "fb_prompt": "Would you like to leave quick feedback?",
        "fb_thanks": "Thanks for your feedback! 💛",
        "fb_like": "👍 Useful",
        "fb_dislike": "👎 Didn’t help",
        "fb_write": "✍️ Write a comment",
    },
    "ru": {
        "welcome": "Привет! Я TendAI — ассистент здоровья и долголетия.\nВыбери тему ниже или коротко опиши, что беспокоит.",
        "menu": ["Боль", "Горло/простуда", "Сон", "Стресс", "Пищеварение", "Энергия"],
        "help": "Помогаю короткой проверкой, планом на 24–48 ч и бережными чек-инами.\nКоманды: /help, /privacy, /pause, /resume, /delete_data, /lang, /feedback",
        "privacy": "TendAI не заменяет врача. Храним минимум данных для напоминаний. /delete_data — удалить всё.",
        "paused_on": "Напоминания на паузе. Включить: /resume",
        "paused_off": "Напоминания снова включены.",
        "deleted": "Все ваши данные удалены. Можно начать заново через /start.",
        "ask_consent": "Можно я напишу позже, чтобы узнать, как вы себя чувствуете?",
        "yes": "Да", "no": "Нет",
        "plan_header": "Ваш план на 24–48 часов:",
        "plan_accept": "Готовы попробовать сегодня?",
        "accept_opts": ["✅ Да", "🔁 Позже", "✖️ Нет"],
        "remind_when": "Когда напомнить и спросить самочувствие?",
        "remind_opts": ["через 4 часа", "вечером", "завтра утром", "не надо"],
        "thanks": "Принято 🙌",
        "checkin_ping": "Коротко: как сейчас по шкале 0–10?",
        "checkin_better": "Отлично! Продолжаем 💪",
        "checkin_worse": "Если появились «красные флаги» или боль ≥7/10 — лучше обратиться к врачу.",
        "comment_prompt": "Напишите комментарий сейчас. Или /skip — пропустить.",
        "comment_saved": "Отзыв сохранён, спасибо! 🙌",
        "skip_ok": "Пропущено.",
        "help_lang": "Используйте /lang ru|en|uk|es чтобы сменить язык.",
        "oos": "Я здесь для вопросов здоровья, самочувствия и привычек. Опишите коротко, что происходит, или выберите тему внизу.",
        "fb_prompt": "Оставите быстрый отзыв?",
        "fb_thanks": "Спасибо за отзыв! 💛",
        "fb_like": "👍 Полезно",
        "fb_dislike": "👎 Не помогло",
        "fb_write": "✍️ Написать отзыв",
    },
    "uk": {
        "welcome": "Привіт! Я TendAI — асистент здоров’я та довголіття.\nОбери тему або коротко опиши, що турбує.",
        "menu": ["Біль", "Горло/застуда", "Сон", "Стрес", "Травлення", "Енергія"],
        "help": "Допомагаю короткою перевіркою, планом на 24–48 год і чеками.\nКоманди: /help, /privacy, /pause, /resume, /delete_data, /lang, /feedback",
        "privacy": "TendAI не замінює лікаря. /delete_data — видалити все.",
        "paused_on": "Нагадування призупинені. Увімкнути: /resume",
        "paused_off": "Нагадування знову увімкнені.",
        "deleted": "Дані видалено. Можна /start знову.",
        "ask_consent": "Можу написати пізніше, щоб дізнатися, як ви?",
        "yes": "Так", "no": "Ні",
        "plan_header": "Ваш план на 24–48 год:",
        "plan_accept": "Спробуємо сьогодні?",
        "accept_opts": ["✅ Так", "🔁 Пізніше", "✖️ Ні"],
        "remind_when": "Коли нагадати та спитати самопочуття?",
        "remind_opts": ["через 4 год", "увечері", "завтра вранці", "не треба"],
        "thanks": "Прийнято 🙌",
        "checkin_ping": "Коротко: як зараз (0–10)?",
        "checkin_better": "Чудово! Продовжуємо 💪",
        "checkin_worse": "Якщо є «червоні прапорці» або біль ≥7/10 — краще звернутися до лікаря.",
        "comment_prompt": "Напишіть коментар зараз. /skip — пропустити.",
        "comment_saved": "Відгук збережено, дякуємо! 🙌",
        "skip_ok": "Пропущено.",
        "help_lang": "Використовуйте /lang ru|en|uk|es щоб змінити мову.",
        "oos": "Я тут для тем про здоров’я та самопіклування. Опишіть, що відбувається, або оберіть тему нижче.",
        "fb_prompt": "Залишити швидкий відгук?",
        "fb_thanks": "Дякуємо! 💛",
        "fb_like": "👍 Корисно",
        "fb_dislike": "👎 Не допомогло",
        "fb_write": "✍️ Написати відгук",
    },
    "es": {
        "welcome": "¡Hola! Soy TendAI, tu asistente de salud y longevidad.\nElige un tema o cuéntame brevemente qué pasa.",
        "menu": ["Dolor", "Garganta/Resfriado", "Sueño", "Estrés", "Digestión", "Energía"],
        "help": "Chequeos breves, plan 24–48 h y seguimientos.\nComandos: /help, /privacy, /pause, /resume, /delete_data, /lang, /feedback",
        "privacy": "TendAI no sustituye a un médico. /delete_data para borrar.",
        "paused_on": "Recordatorios pausados. /resume para activarlos.",
        "paused_off": "Recordatorios activados.",
        "deleted": "Datos borrados. Puedes /start de nuevo.",
        "ask_consent": "¿Puedo escribirte más tarde para saber cómo sigues?",
        "yes": "Sí", "no": "No",
        "plan_header": "Tu plan para 24–48 h:",
        "plan_accept": "¿Lo intentas hoy?",
        "accept_opts": ["✅ Sí", "🔁 Más tarde", "✖️ No"],
        "remind_when": "¿Cuándo te escribo para revisar?",
        "remind_opts": ["en 4 h", "esta tarde", "mañana por la mañana", "no hace falta"],
        "thanks": "¡Hecho! 🙌",
        "checkin_ping": "Revisión rápida: ¿cómo estás ahora (0–10)?",
        "checkin_better": "¡Bien! Sigue así 💪",
        "checkin_worse": "Si hay señales de alarma o dolor ≥7/10, considera atención médica.",
        "comment_prompt": "Escribe tu comentario ahora. /skip para omitir.",
        "comment_saved": "¡Comentario guardado, gracias! 🙌",
        "skip_ok": "Omitido.",
        "help_lang": "Usa /lang ru|en|uk|es para cambiar idioma.",
        "oos": "Estoy para temas de salud y autocuidado. Cuéntame o elige un tema abajo.",
        "fb_prompt": "¿Opinión rápida?",
        "fb_thanks": "¡Gracias! 💛",
        "fb_like": "👍 Útil",
        "fb_dislike": "👎 No ayudó",
        "fb_write": "✍️ Escribir comentario",
    },
}
def t(lang: str, key: str) -> str:
    return T.get(lang, T["en"]).get(key, T["en"].get(key, key))

# ===== bottom keyboards
CANCEL = "❌ Отмена"
def _rkm(rows): return ReplyKeyboardMarkup(rows, resize_keyboard=True)
def main_menu(lang: str): 
    m=T[lang]["menu"]; return _rkm([m[:3], m[3:]])
def kb_numbers(): 
    nums=[str(i) for i in range(11)]; return _rkm([nums[:6], nums[6:], [CANCEL]])
def kb_accept(lang: str): 
    a=T[lang]["accept_opts"]; return _rkm([a, [CANCEL]])
def kb_remind(lang: str):
    r=T[lang]["remind_opts"]; rows=[r[:2], r[2:], [CANCEL]]; return _rkm(rows)
def kb_yesno(lang: str): return _rkm([[t(lang,"yes"), t(lang,"no")],[CANCEL]])
def kb_feedback(lang:str): return _rkm([[t(lang,"fb_like"), t(lang,"fb_dislike")],[t(lang,"fb_write")],[CANCEL]])

# =========================
# Sheets helpers
# =========================
def utcnow(): return datetime.now(timezone.utc)
def iso(dt: datetime | None) -> str:
    if not dt: return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")

def users_get_row_index(user_id: int) -> int | None:
    vals = ws_users.get_all_records()
    for i, row in enumerate(vals, start=2):
        if str(row.get("user_id")) == str(user_id):
            return i
    return None
def users_get(user_id: int) -> dict:
    vals = ws_users.get_all_records()
    for row in vals:
        if str(row.get("user_id")) == str(user_id):
            return row
    return {}
def users_upsert(user_id: int, username: str, lang: str):
    idx = users_get_row_index(user_id)
    row = [str(user_id), username or "", lang, "no", "0", "", "no"]
    if idx: ws_users.update(f"A{idx}:G{idx}", [row])
    else: ws_users.append_row(row)
def users_set(user_id: int, field: str, value: str):
    idx = users_get_row_index(user_id); 
    if not idx: return
    headers = ws_users.row_values(1)
    if field in headers:
        col = headers.index(field) + 1
        ws_users.update_cell(idx, col, value)

def episode_create(user_id: int, topic: str, baseline_severity: int, red_flags: str) -> str:
    eid = f"{user_id}-{uuid.uuid4().hex[:8]}"
    now = iso(utcnow())
    ws_episodes.append_row([
        eid, str(user_id), topic, now,
        str(baseline_severity), red_flags, "0", "<=3/10",
        "", "", "open", now, ""
    ])
    return eid
def episode_find_open(user_id: int) -> dict | None:
    vals = ws_episodes.get_all_records()
    for row in vals:
        if str(row.get("user_id")) == str(user_id) and row.get("status") == "open":
            return row
    return None
def episode_set(eid: str, field: str, value: str):
    vals = ws_episodes.get_all_values()
    headers = vals[0]
    if field not in headers: return
    col = headers.index(field) + 1
    for i in range(2, len(vals) + 1):
        if ws_episodes.cell(i, 1).value == eid:
            ws_episodes.update_cell(i, col, value)
            ws_episodes.update_cell(i, headers.index("last_update") + 1, iso(utcnow()))
            return

def schedule_from_sheet_on_start(app):
    vals = ws_episodes.get_all_records()
    now = utcnow()
    for row in vals:
        if row.get("status") != "open": continue
        eid = row.get("episode_id"); uid = int(row.get("user_id"))
        nca = row.get("next_checkin_at") or ""
        if not nca: continue
        try: dt = datetime.strptime(nca, "%Y-%m-%d %H:%M:%S%z")
        except Exception: continue
        delay = (dt - now).total_seconds()
        if delay < 60: delay = 60
        app.job_queue.run_once(job_checkin, when=delay, data={"user_id": uid, "episode_id": eid})

# =========================
# Helpers
# =========================
def send_once(uid: int, key: str, now_ts: float, cooldown: float = 8.0) -> bool:
    s = sessions.setdefault(uid, {})
    last = s.get("last_send", {})
    ts = last.get(key, 0.0)
    if now_ts - ts >= cooldown:
        last[key] = now_ts
        s["last_send"] = last
        sessions[uid] = s
        return True
    return False

# ===== LLM =====
def _json_from_text(raw: str) -> dict:
    try:
        m = re.search(r"\{[\s\S]*\}", raw)
        return json.loads(m.group(0)) if m else json.loads(raw)
    except Exception:
        return {}

def llm_route(uid: int, lang: str, user_text: str) -> dict:
    """
    Возвращает JSON:
      assistant: текст для пользователя (коротко, тепло),
      stage: one of [followup, plan_ready, escalate, out],
      slots: {intent, loc, kind, duration, severity, red}
    """
    if not oai:
        return {}
    hist = sessions.setdefault(uid, {}).setdefault("chat_history", [])[-8:]
    sys = (
        "You are TendAI, a warm health & self-care assistant. "
        "Speak naturally in the user's language, max 4 sentences, supportive, no diagnoses. "
        "When info is missing, ask one specific question to progress. "
        "Also extract triage fields. Return ONLY JSON with keys: "
        "assistant (string), stage (followup|plan_ready|escalate|out), "
        "slots (object with optional keys: intent in [pain, throat, sleep, stress, digestion, energy]; "
        "loc in [Head, Throat, Back, Belly, Chest, Other]; kind in [Dull, Sharp, Throbbing, Burning, Pressing]; "
        "duration in [\"<3h\",\"3–24h\",\">1 day\",\">1 week\"] or free text; "
        "severity int 0..10; red in [\"High fever\",\"Vomiting\",\"Weakness/numbness\",\"Speech/vision issues\",\"Trauma\",\"None\"])."
    )
    msgs = [{"role":"system","content":sys}] + hist + [{"role":"user","content":f"[lang={lang}] {user_text}"}]
    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.2,
            max_tokens=400,
            messages=msgs,
        )
        raw = (resp.choices[0].message.content or "").strip()
        j = _json_from_text(raw)
        if not isinstance(j, dict): j = {}
        # save compact history with assistant natural text (not JSON)
        a = j.get("assistant", "")
        hist.append({"role":"user","content":user_text})
        hist.append({"role":"assistant","content":a[:1000]})
        sessions[uid]["chat_history"] = hist[-10:]
        return j
    except Exception as e:
        logging.warning(f"LLM error: {e}")
        return {}

# ===== Plans / Hypotheses
def build_hypotheses(lang: str, ans: dict) -> list[tuple[str,float,str]]:
    loc = (ans.get("loc") or "").lower()
    kind = (ans.get("kind") or "").lower()
    duration = (ans.get("duration") or "").lower()
    sev = int(ans.get("severity", 5))
    H=[]
    def add(name,score,because): H.append((name,float(score),because))

    if "head" in loc or "голова" in loc or "cabeza" in loc:
        if "throbb" in kind or "пульс" in kind: add("Migraine-like", 0.7 + 0.05*(sev>=6), "Throbbing + moderate/severe")
        if "press" in kind or "tight" in kind or "дав" in kind: add("Tension-type", 0.6, "Pressing/tight")
        if "3–24" in duration or ">1 day" in duration or ">1 дня" in duration: add("Tension/sinus", 0.4, "Many hours")

    if "back" in loc or "спина" in loc or "espalda" in loc:
        if "shoot" in kind or "прострел" in kind: add("Radicular pain", 0.7, "Shooting below knee/‘прострел’")
        else: add("Mechanical low back pain", 0.6, "Typical without red flags")

    if "belly" in loc or "живот" in loc or "abdomen" in loc or "stomach" in loc or "vientre" in loc:
        add("Dyspepsia/gastritis-like", 0.5, "Common benign causes if no red flags")

    if "chest" in loc or "груд" in loc or "pecho" in loc:
        add("Pleuritic/musculoskeletal vs respiratory", 0.6, "Depends on cough/pressure/breath")

    if "throat" in loc or "горло" in loc or "garganta" in loc:
        add("Viral sore throat", 0.6, "Common viral pattern")

    H.sort(key=lambda x:x[1], reverse=True)
    return H[:3]

def pain_plan(lang: str, ans: dict, hyps: list[tuple[str,float,str]]) -> list[str]:
    red = (ans.get("red") or "").lower()
    urgent = any(s in red for s in ["fever", "vomit", "weakness", "speech", "vision", "травм", "trauma"]) and (ans.get("severity", 0) >= 7)
    for name, score, because in hyps:
        if "Urgent" in name or "cardiac" in name or "Neurologic" in name or "red flag" in name:
            urgent = True
    if urgent:
        return {
            "ru": ["⚠️ Есть признаки возможной угрозы. Пожалуйста, как можно скорее обратитесь за медицинской помощью."],
            "uk": ["⚠️ Є ознаки можливої загрози. Будь ласка, якнайшвидше зверніться по медичну допомогу."],
            "en": ["⚠️ Some answers suggest urgent risks. Please seek medical care as soon as possible."],
            "es": ["⚠️ Hay señales de posible urgencia. Por favor busca atención médica lo antes posible."],
        }[lang]

    base = {
        "ru": [
            "1) Вода 400–600 мл и 15–20 минут покоя в тихом месте.",
            "2) Если нет противопоказаний — ибупрофен 200–400 мг 1 раз с едой.",
            "3) Проветрить комнату и снизить экраны на 30–60 минут.",
            "Цель: к вечеру боль ≤3/10.",
        ],
        "uk": [
            "1) 400–600 мл води і 15–20 хв тиші/відпочинку.",
            "2) Якщо немає протипоказань — ібупрофен 200–400 мг 1 раз із їжею.",
            "3) Провітрити кімнату; менше екранів 30–60 хв.",
            "Мета: до вечора біль ≤3/10.",
        ],
        "en": [
            "1) Drink 400–600 ml of water and rest 15–20 minutes in a quiet place.",
            "2) If no contraindications — ibuprofen 200–400 mg once with food.",
            "3) Air the room; reduce screen time for 30–60 minutes.",
            "Target: by evening pain ≤3/10.",
        ],
        "es": [
            "1) Bebe 400–600 ml de agua y descansa 15–20 min en un lugar tranquilo.",
            "2) Si no hay contraindicaciones — ibuprofeno 200–400 mg una vez con comida.",
            "3) Ventila la habitación; menos pantallas 30–60 min.",
            "Objetivo: por la tarde dolor ≤3/10.",
        ],
    }
    loc = (ans.get("loc") or "").lower()
    if "back" in loc or "спина" in loc or "espalda" in loc:
        extra = {"ru":["4) Тёплый компресс 10–15 мин 2–3р/день, мягкая мобилизация."],
                 "uk":["4) Теплий компрес 10–15 хв 2–3р/день, м’яка мобілізація."],
                 "en":["4) Warm compress 10–15 min 2–3×/day, gentle mobility."],
                 "es":["4) Compresa tibia 10–15 min 2–3×/día, movilidad suave."]}[lang]
        return base[lang] + extra
    if "throat" in loc or "горло" in loc or "garganta" in loc:
        extra = {"ru":["4) Тёплое питьё; полоскание солевым раствором 3–4р/день."],
                 "uk":["4) Теплі напої; полоскання сольовим розчином 3–4р/день."],
                 "en":["4) Warm fluids; saline gargles 3–4×/day."],
                 "es":["4) Líquidos tibios; gárgaras salinas 3–4×/día."]}[lang]
        return base[lang] + extra
    return base[lang]

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
        await context.bot.send_message(uid, t(lang, "checkin_ping"), reply_markup=kb_numbers())
        s = sessions.setdefault(uid, {})
        s["await_step"] = "checkin"; s["episode_id"] = eid
        episode_set(eid, "next_checkin_at", "")
    except Exception as e:
        logging.error(f"job_checkin send error: {e}")

# =========================
# Commands
# =========================
async def on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logging.info("Webhook cleared")
    except Exception:
        pass
    schedule_from_sheet_on_start(app)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = users_get(user.id).get("lang")
    if not lang:
        txt = (update.message.text or "").strip() if update.message else ""
        cand = None
        if detect:
            try: cand = detect(txt) if txt else None
            except Exception: cand = None
        lang = norm_lang(cand or getattr(user, "language_code", None))
        users_upsert(user.id, user.username or "", lang)
    await update.message.reply_text(t(lang, "welcome"), reply_markup=main_menu(lang))
    u = users_get(user.id)
    if (u.get("consent") or "").lower() not in {"yes","no"}:
        s = sessions.setdefault(user.id, {})
        s["awaiting_consent"] = True
        await update.message.reply_text(t(lang, "ask_consent"), reply_markup=kb_yesno(lang))

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang, "help"), reply_markup=main_menu(lang))

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang, "privacy"))

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "yes")
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang, "paused_on"))

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "no")
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang, "paused_off"))

async def cmd_delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    idx = users_get_row_index(uid)
    if idx: ws_users.delete_rows(idx)
    vals = ws_episodes.get_all_values(); to_delete=[]
    for i in range(2, len(vals)+1):
        if ws_episodes.cell(i,2).value == str(uid): to_delete.append(i)
    for j, row_i in enumerate(to_delete): ws_episodes.delete_rows(row_i - j)
    lang = norm_lang(getattr(update.effective_user, "language_code", None))
    await update.message.reply_text(t(lang, "deleted"), reply_markup=ReplyKeyboardRemove())

async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
        await update.message.reply_text(t(lang, "help_lang"))
        return
    candidate = norm_lang(context.args[0])
    if candidate not in SUPPORTED:
        cur = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
        await update.message.reply_text(t(cur, "help_lang"))
        return
    users_set(uid, "lang", candidate)
    await update.message.reply_text("✅", reply_markup=main_menu(candidate))

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = sessions.get(uid, {})
    if s.get("awaiting_comment"):
        s["awaiting_comment"] = False; s["feedback_context"]=""
        lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
        await update.message.reply_text(t(lang, "skip_ok"))
    else:
        lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
        await update.message.reply_text("👌", reply_markup=main_menu(lang))

async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
    s = sessions.setdefault(uid, {})
    s["awaiting_feedback_choice"] = True; s["feedback_context"] = "general"
    await update.message.reply_text(t(lang, "fb_prompt"), reply_markup=kb_feedback(lang))

# =========================
# Callback (совместимость)
# =========================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Обновление: используйте кнопки внизу 👇")

# =========================
# Text handler (CHAT-FIRST)
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    text = (update.message.text or "").strip()

    # язык
    urec = users_get(uid)
    if not urec:
        cand = None
        if detect:
            try: cand = detect(text) if text else None
            except Exception: cand = None
        lang = norm_lang(cand or getattr(user,"language_code",None))
        users_upsert(uid, user.username or "", lang)
    else:
        lang = norm_lang(urec.get("lang") or getattr(user,"language_code",None))

    s = sessions.setdefault(uid, {"flow": "chat", "answers": {}, "chat_history": []})

    # согласие
    if s.get("awaiting_consent"):
        if text in {t(lang,"yes"), t(lang,"no")}:
            users_set(uid, "consent", "yes" if text == t(lang,"yes") else "no")
            s["awaiting_consent"]=False
            await update.message.reply_text(t(lang,"thanks"), reply_markup=main_menu(lang)); return
        await update.message.reply_text("Пожалуйста, выберите кнопку ниже", reply_markup=kb_yesno(lang)); return

    # фидбек
    if s.get("awaiting_feedback_choice"):
        if text == t(lang,"fb_like"):
            ws_feedback.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), str(uid), s.get("feedback_context","general"), user.username or "", "1", ""])
            s["awaiting_feedback_choice"]=False
            await update.message.reply_text(t(lang,"fb_thanks"), reply_markup=main_menu(lang)); return
        if text == t(lang,"fb_dislike"):
            ws_feedback.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), str(uid), s.get("feedback_context","general"), user.username or "", "0", ""])
            s["awaiting_feedback_choice"]=False
            await update.message.reply_text(t(lang,"fb_thanks"), reply_markup=main_menu(lang)); return
        if text == t(lang,"fb_write"):
            s["awaiting_feedback_choice"]=False; s["awaiting_comment"]=True
            await update.message.reply_text(t(lang,"comment_prompt")); return
        if text == CANCEL:
            s["awaiting_feedback_choice"]=False
            await update.message.reply_text("👌", reply_markup=main_menu(lang)); return
        await update.message.reply_text("Пожалуйста, используйте кнопки ниже", reply_markup=kb_feedback(lang)); return

    # комментарий
    if s.get("awaiting_comment") and not text.startswith("/"):
        name = s.get("feedback_context") or "general"
        ws_feedback.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), str(uid), f"comment:{name}", user.username or "", "", text])
        s["awaiting_comment"]=False; s["feedback_context"]=""
        await update.message.reply_text(t(lang,"comment_saved"), reply_markup=main_menu(lang)); return

    # чек-ин
    if s.get("await_step") == "checkin":
        if text == CANCEL:
            s["await_step"]=0; await update.message.reply_text(t(lang,"thanks"), reply_markup=main_menu(lang)); return
        if text.isdigit() and 0 <= int(text) <= 10:
            val = int(text); ep = episode_find_open(uid)
            if ep:
                eid = ep.get("episode_id")
                episode_set(eid, "notes", f"checkin:{val}")
                if val <= 3:
                    episode_set(eid, "status", "resolved")
                    await update.message.reply_text(t(lang,"checkin_better"), reply_markup=main_menu(lang))
                else:
                    await update.message.reply_text(t(lang,"checkin_worse"), reply_markup=main_menu(lang))
            s["await_step"]=0; return
        await update.message.reply_text("Выберите число на клавиатуре ниже", reply_markup=kb_numbers()); return

    # принятие плана
    if s.get("flow") == "accept_wait":
        acc = T[lang]["accept_opts"]
        if text in acc:
            eid = s.get("episode_id")
            if text == acc[0]: episode_set(eid, "plan_accepted", "1")
            elif text == acc[1]: episode_set(eid, "plan_accepted", "later")
            elif text == acc[2]: episode_set(eid, "plan_accepted", "0")
            s["flow"]="remind_wait"
            await update.message.reply_text(t(lang,"remind_when"), reply_markup=kb_remind(lang)); return
        if text == CANCEL:
            s["flow"]="chat"; await update.message.reply_text("Ок", reply_markup=main_menu(lang)); return
        await update.message.reply_text("Пожалуйста, используйте кнопки ниже", reply_markup=kb_accept(lang)); return

    # напоминание
    if s.get("flow") == "remind_wait":
        opts = T[lang]["remind_opts"]
        if text in opts or text == CANCEL:
            code_map = {opts[0]:"4h", opts[1]:"evening", opts[2]:"morning", opts[3]:"none"}
            code = code_map.get(text, "none")
            eid = s.get("episode_id"); urec = users_get(uid)
            tz_off = 0
            try: tz_off = int(urec.get("tz_offset") or "0")
            except: tz_off = 0
            now_utc = utcnow(); user_now = now_utc + timedelta(hours=tz_off)
            if code == "4h": target_user = user_now + timedelta(hours=4)
            elif code == "evening":
                target_user = user_now.replace(hour=19, minute=0, second=0, microsecond=0)
                if target_user < user_now: target_user += timedelta(days=1)
            elif code == "morning":
                target_user = user_now.replace(hour=9, minute=0, second=0, microsecond=0)
                if target_user < user_now: target_user += timedelta(days=1)
            else: target_user = None
            if target_user:
                target_utc = target_user - timedelta(hours=tz_off)
                episode_set(eid, "next_checkin_at", iso(target_utc))
                delay = max(60, (target_utc - now_utc).total_seconds())
                context.job_queue.run_once(job_checkin, when=delay, data={"user_id": uid, "episode_id": eid})
            await update.message.reply_text(t(lang,"thanks"), reply_markup=main_menu(lang))
            s["flow"]="chat"; return
        await update.message.reply_text("Пожалуйста, используйте кнопки ниже", reply_markup=kb_remind(lang)); return

    # ========= CHAT-FIRST =========
    # зовём LLM на каждое сообщение (никаких фильтров до этого места)
    j = llm_route(uid, lang, text)
    if not j:
        # если ключа/клиента нет — мягкий фолбэк вместо «деревянных» текстов
        await update.message.reply_text(t(lang,"oos"), reply_markup=main_menu(lang))
        return

    assistant = j.get("assistant") or ""
    stage = j.get("stage") or "followup"
    slots = j.get("slots") or {}

    # отдаем натуральный ответ
    if assistant:
        await update.message.reply_text(assistant)

    # обновим слоты
    ans = s.setdefault("answers", {})
    for k in ["intent","loc","kind","duration","severity","red"]:
        v = slots.get(k)
        if v not in (None, ""): ans[k]=v

    # план/эскалация
    if stage == "escalate":
        await update.message.reply_text(t(lang,"thanks"), reply_markup=main_menu(lang))
        return

    have_all = all(k in ans for k in ["loc","kind","duration","severity","red"])
    if stage == "plan_ready" or have_all:
        eid = s.get("episode_id")
        if not eid:
            eid = episode_create(uid, ans.get("intent","pain"), int(ans.get("severity",5)), ans.get("red","None"))
            s["episode_id"]=eid
        hyps = build_hypotheses(lang, ans)
        plan_lines = pain_plan(lang, ans, hyps)
        await update.message.reply_text(f"{t(lang,'plan_header')}\n" + "\n".join(plan_lines))
        await update.message.reply_text(t(lang,"plan_accept"), reply_markup=kb_accept(lang))
        s["flow"]="accept_wait"; return

    # иначе остаёмся в беседе
    s["flow"]="chat"
    sessions[uid] = s

# =========================
# Runner
# =========================
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_startup).build()
    schedule_from_sheet_on_start(app)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("delete_data", cmd_delete_data))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("feedback", cmd_feedback))

    app.add_handler(CallbackQueryHandler(on_callback))  # совместимость

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
