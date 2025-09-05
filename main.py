# -*- coding: utf-8 -*-
# TendAI main.py — обновлено: лимитер/тихие часы, утро+вечер, Youth-команды,
# безопасные headers для Sheets, Rules (evidence), тёплый стиль (мысль→вопрос),
# Health60, профиль-плашка 1 раз, city-сохранение, feedback ≤1/день, Preferences.

import os, re, json, uuid, logging, random
from datetime import datetime, timedelta, timezone, time as dtime, date
from typing import List, Tuple, Dict, Optional
from difflib import SequenceMatcher

from dotenv import load_dotenv
from langdetect import detect, DetectorFactory

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# --- SAFE import of optional PRO-intake plugin ---
try:
    from intake_pro import register_intake_pro  # noqa: F401
    HAVE_INTAKE_PRO = True
except Exception:
    HAVE_INTAKE_PRO = False
    def register_intake_pro(app, gclient=None, on_complete_cb=None):
        logging.warning("intake_pro not found — PRO-опрос отключён на этом деплое.")
        async def _fallback_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            await q.message.reply_text(
                "PRO-опрос недоступен на этом деплое. Используйте /profile."
            )
        app.add_handler(CallbackQueryHandler(_fallback_cb, pattern=r"^intake:"))

from openai import OpenAI

# ---------- Google Sheets (robust + memory fallback) ----------
import gspread
from gspread.exceptions import SpreadsheetNotFound
import gspread.utils as gsu
from oauth2client.service_account import ServiceAccountCredentials

# ---------------- Boot & Config ----------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
DetectorFactory.seed = 0

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

SHEET_NAME = os.getenv("SHEET_NAME", "TendAI Sheets")
SHEET_ID = os.getenv("SHEET_ID", "")
ALLOW_CREATE_SHEET = os.getenv("ALLOW_CREATE_SHEET", "0") == "1"
DEFAULT_CHECKIN_LOCAL = "08:30"

oai: Optional[OpenAI] = None
try:
    if OPENAI_API_KEY:
        os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
    oai = OpenAI()
except Exception as e:
    logging.error(f"OpenAI init error: {e}")
    oai = None

# ---------------- i18n ----------------
SUPPORTED = {"ru", "en", "uk", "es"}

def norm_lang(code: Optional[str]) -> str:
    if not code:
        return "en"
    c = code.split("-")[0].lower()
    return c if c in SUPPORTED else "en"

T = {
    "en": {
        "welcome": "Hi! I’m TendAI — your health & longevity assistant.\nTell me what we focus on today — nutrition, sleep or activity?",
        "help": "Short checkups, 24–48h plans, reminders, daily check-ins.\nCommands: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /energy /mood /water /skin /ru /uk /en /es",
        "privacy": "TendAI is not a medical service and can’t replace a doctor. We store minimal data for reminders. /delete_data to erase.",
        "paused_on": "Notifications paused. Use /resume to enable.",
        "paused_off": "Notifications resumed.",
        "deleted": "All your data was deleted. Use /start to begin again.",
        "ask_consent": "May I send you a gentle follow-up later?",
        "yes":"Yes","no":"No",
        "unknown":"I need a bit more info — where exactly and how long?",
        "profile_intro":"Quick intake (~40s). Buttons or your text — as you prefer.",
        "p_step_1":"Step 1/8. Sex:",
        "p_step_2":"Step 2/8. Age:",
        "p_step_3":"Step 3/8. Main goal:",
        "p_step_4":"Step 4/8. Chronic conditions:",
        "p_step_5":"Step 5/8. Meds/supplements/allergies:",
        "p_step_6":"Step 6/8. Sleep (bed/wake, e.g., 23:30/07:00):",
        "p_step_7":"Step 7/8. Activity:",
        "p_step_8":"Step 8/8. Diet most of the time:",
        "write":"✍️ Write",
        "skip":"⏭️ Skip",
        "saved_profile":"Saved: ",
        "start_where":"Where shall we start now — symptom, sleep or nutrition?",
        "daily_gm":"Good morning! Quick check-in:",
        "mood_good":"😃 Good","mood_ok":"😐 Okay","mood_bad":"😣 Poor","mood_note":"✍️ Comment",
        "mood_thanks":"Thanks! Have a smooth day 👋",
        "triage_pain_q1":"Where does it hurt?",
        "triage_pain_q1_opts":["Head","Throat","Back","Belly","Other"],
        "triage_pain_q2":"What kind of pain?",
        "triage_pain_q2_opts":["Dull","Sharp","Pulsating","Pressing"],
        "triage_pain_q3":"How long has it lasted?",
        "triage_pain_q3_opts":["<3h","3–24h",">1 day",">1 week"],
        "triage_pain_q4":"Rate the pain (0–10):",
        "triage_pain_q5":"Any of these now?",
        "triage_pain_q5_opts":["High fever","Vomiting","Weakness/numbness","Speech/vision problems","Trauma","None"],
        "plan_header":"Your 24–48h plan:",
        "plan_accept":"Will you try this today?",
        "accept_opts":["✅ Yes","🔁 Later","✖️ No"],
        "remind_when":"When shall I check on you?",
        # оставляем 3 пресета: +4h, evening, morning
        "thanks":"Got it 🙌",
        "checkin_ping":"Quick check-in: how is it now (0–10)?",
        "checkin_better":"Nice! Keep it up 💪",
        "checkin_worse":"If any red flags or pain ≥7/10 — consider medical help.",
        "act_rem_4h":"⏰ in 4h",
        "act_rem_eve":"⏰ this evening",
        "act_rem_morn":"⏰ tomorrow morning",
        "act_save_episode":"💾 Save",
        "act_ex_neck":"🧘 Neck 5-min",
        "act_find_lab":"🧪 Find a lab",
        "act_er":"🚑 Emergency info",
        "act_city_prompt":"Type your city/area so I can suggest a lab (text only).",
        "act_saved":"Saved.",
        "er_text":"If symptoms worsen, severe shortness of breath, chest pain, confusion, or persistent high fever — seek urgent care/emergency.",
        "px":"{sex_ru}, {age} — цель: {goal_ru}.",
        "back":"◀ Back",
        "exit":"Exit",
        "ask_fb":"Was this helpful?",
        "fb_thanks":"Thanks for your feedback! ✅",
        "fb_write":"Write a short feedback message:",
        "fb_good":"👍 Like",
        "fb_bad":"👎 Dislike",
        "fb_free":"📝 Feedback",
        "h60_btn": "Health in 60 s",
        "h60_intro": "Write briefly what bothers you (e.g., “headache”, “fatigue”, “stomach pain”). I’ll give 3 key tips now.",
        "h60_t1": "Possible causes",
        "h60_t2": "Do today (24–48h)",
        "h60_t3": "When to see a doctor",
        "h60_serious": "Serious to rule out",
        # Youth quick labels
        "energy_title": "Energy for today:",
        "water_prompt": "Drink 300–500 ml of water. Remind in 4 hours?",
        "skin_title": "Skin/Body tip:"
    },
    "ru": {
        "welcome":"Привет! Я TendAI — ассистент здоровья и долголетия.\nКуда сфокусируемся — питание, сон или активность?",
        "help":"Короткие проверки, план на 24–48 ч, напоминания, ежедневные чек-ины.\nКоманды: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +3 /health60 /energy /mood /water /skin /ru /uk /en /es",
        "privacy":"TendAI не заменяет врача. Храним минимум данных для напоминаний. /delete_data — удалить.",
        "paused_on":"Напоминания поставлены на паузу. /resume — включить.",
        "paused_off":"Напоминания снова включены.",
        "deleted":"Все данные удалены. /start — начать заново.",
        "ask_consent":"Можно прислать мягкое напоминание позже?",
        "yes":"Да","no":"Нет",
        "unknown":"Нужно чуть больше деталей — где именно и сколько длится?",
        "profile_intro":"Быстрый опрос (~40с). Можно нажимать кнопки или написать свой ответ.",
        "p_step_1":"Шаг 1/8. Пол:",
        "p_step_2":"Шаг 2/8. Возраст:",
        "p_step_3":"Шаг 3/8. Главная цель:",
        "p_step_4":"Шаг 4/8. Хронические болезни:",
        "p_step_5":"Шаг 5/8. Лекарства/добавки/аллергии:",
        "p_step_6":"Шаг 6/8. Сон (отбой/подъём, напр. 23:30/07:00):",
        "p_step_7":"Шаг 7/8. Активность:",
        "p_step_8":"Шаг 8/8. Питание чаще всего:",
        "write":"✍️ Написать",
        "skip":"⏭️ Пропустить",
        "saved_profile":"Сохранил: ",
        "start_where":"С чего начнём — симптом, сон или питание?",
        "daily_gm":"Доброе утро! Быстрый чек-ин:",
        "mood_good":"😃 Хорошо","mood_ok":"😐 Нормально","mood_bad":"😣 Плохо","mood_note":"✍️ Комментарий",
        "mood_thanks":"Спасибо! Хорошего дня 👋",
        "triage_pain_q1":"Где болит?",
        "triage_pain_q1_opts":["Голова","Горло","Спина","Живот","Другое"],
        "triage_pain_q2":"Какой характер боли?",
        "triage_pain_q2_opts":["Тупая","Острая","Пульсирующая","Давящая"],
        "triage_pain_q3":"Как долго длится?",
        "triage_pain_q3_opts":["<3ч","3–24ч",">1 дня",">1 недели"],
        "triage_pain_q4":"Оцените боль (0–10):",
        "triage_pain_q5":"Есть ли что-то из этого сейчас?",
        "triage_pain_q5_opts":["Высокая температура","Рвота","Слабость/онемение","Нарушение речи/зрения","Травма","Нет"],
        "plan_header":"Ваш план на 24–48 часов:",
        "plan_accept":"Готовы попробовать сегодня?",
        "accept_opts":["✅ Да","🔁 Позже","✖️ Нет"],
        "remind_when":"Когда напомнить и спросить самочувствие?",
        "thanks":"Принято 🙌",
        "checkin_ping":"Коротко: как сейчас по шкале 0–10?",
        "checkin_better":"Отлично! Продолжаем 💪",
        "checkin_worse":"Если есть «красные флаги» или боль ≥7/10 — лучше обратиться к врачу.",
        "act_rem_4h":"⏰ через 4 ч",
        "act_rem_eve":"⏰ Сегодня вечером",
        "act_rem_morn":"⏰ Завтра утром",
        "act_save_episode":"💾 Сохранить",
        "act_ex_neck":"🧘 Шея 5 мин",
        "act_find_lab":"🧪 Найти лабораторию",
        "act_er":"🚑 Когда срочно в скорую",
        "act_city_prompt":"Напишите город/район — подскажу лабораторию (текстом).",
        "act_saved":"Сохранено.",
        "er_text":"Если нарастает, сильная одышка, боль в груди, спутанность, стойкая высокая температура — как можно скорее к неотложке/скорой.",
        "px":"{sex_ru}, {age} — цель: {goal_ru}.",
        "back":"◀ Назад",
        "exit":"Выйти",
        "ask_fb":"Это было полезно?",
        "fb_thanks":"Спасибо за отзыв! ✅",
        "fb_write":"Напишите короткий отзыв одним сообщением:",
        "fb_good":"👍 Нравится",
        "fb_bad":"👎 Не полезно",
        "fb_free":"📝 Отзыв",
        "h60_btn": "Здоровье за 60 сек",
        "h60_intro": "Коротко напишите, что беспокоит (например: «болит голова», «усталость», «боль в животе»). Дам 3 ключевые подсказки.",
        "h60_t1": "Возможные причины",
        "h60_t2": "Что сделать сегодня (24–48 ч)",
        "h60_t3": "Когда обратиться к врачу",
        "h60_serious": "Что серьёзное исключить",
        "energy_title": "Энергия на сегодня:",
        "water_prompt": "Выпей 300–500 мл воды. Напомнить через 4 часа?",
        "skin_title": "Совет для кожи/тела:"
    },
    "uk": {
        "help": "Короткі перевірки, план на 24–48 год, нагадування, щоденні чек-іни.\nКоманди: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /energy /mood /water /skin /ru /uk /en /es",
        "act_rem_4h": "⏰ через 4 год",
        "energy_title": "Енергія на сьогодні:",
        "water_prompt": "Випий 300–500 мл води. Нагадати через 4 години?",
        "skin_title": "Догляд за шкірою/тілом:"
    }
}
T["es"] = T["en"]

# ---------------- Helpers ----------------
def utcnow():
    return datetime.now(timezone.utc)

def iso(dt: Optional[datetime]) -> str:
    return "" if not dt else dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")

def detect_lang_from_text(text: str, fallback: str) -> str:
    s = (text or "").strip()
    if not s:
        return fallback
    low = s.lower()
    if re.search(r"[а-яёіїєґ]", low):
        return "uk" if re.search(r"[іїєґ]", low) else "ru"
    try:
        return norm_lang(detect(s))
    except Exception:
        return fallback

def profile_is_incomplete(profile_row: dict) -> bool:
    keys = ["sex","age","goal"]
    return sum(1 for k in keys if str(profile_row.get(k) or "").strip()) < 2

# ===== ONBOARDING GATE =====
GATE_FLAG_KEY = "menu_unlocked"

def _is_menu_unlocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get(GATE_FLAG_KEY):
        return True
    prof = profiles_get(update.effective_user.id) or {}
    return not profile_is_incomplete(prof)

async def gate_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang", "en")
    kb = [
        [InlineKeyboardButton("🧩 Пройти опрос (40–60 сек)" if lang!="en" else "🧩 Take the 40–60s intake", callback_data="intake:start")],
        [InlineKeyboardButton("➡️ Позже — показать меню" if lang!="en" else "➡️ Later — open menu", callback_data="gate:skip")],
    ]
    text = ("Чтобы советы были точнее, пройдите короткий опрос. Можно пропустить и сделать позже."
            if lang!="en" else
            "To personalize answers, please take a short intake. You can skip and do it later.")
    await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(kb))

async def gate_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "gate:skip":
        context.user_data[GATE_FLAG_KEY] = True
        await q.edit_message_text("Ок, открываю меню…" if context.user_data.get("lang","en")!="en" else "OK, opening the menu…")
        render_cb = context.application.bot_data.get("render_menu_cb")
        if callable(render_cb):
            await render_cb(update, context)
        else:
            await context.application.bot.send_message(q.message.chat_id, "/start")

async def _ipro_save_to_sheets_and_open_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, profile: dict):
    uid = update.effective_user.id
    profiles_upsert(uid, {
        "sex": profile.get("sex") or "",
        "age": profile.get("age") or "",
        "goal": profile.get("goal") or "",
        "conditions": ", ".join(sorted(profile.get("chronic", []))) if isinstance(profile.get("chronic"), set) else (profile.get("chronic") or ""),
        "meds": profile.get("meds") or "",
        "activity": profile.get("hab_activity") or "",
        "sleep": profile.get("hab_sleep") or "",
        "notes": ", ".join(sorted(profile.get("complaints", []))) if isinstance(profile.get("complaints"), set) else (profile.get("complaints") or ""),
    })
    # показать плашку профиля 1 раз после изменения
    sessions.setdefault(uid, {})["profile_banner_pending"] = True
    context.user_data[GATE_FLAG_KEY] = True
    render_cb = context.application.bot_data.get("render_menu_cb")
    if callable(render_cb):
        await render_cb(update, context)
    else:
        await context.application.bot.send_message(update.effective_chat.id, "/start")

# ---------- Anti-duplicate questions ----------
def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()

def is_duplicate_question(uid: int, text: str, thresh: float = 0.93) -> bool:
    s = sessions.setdefault(uid, {})
    asked = s.setdefault("asked_prompts", [])
    for prev in asked[-4:]:
        if _ratio(prev, text) >= thresh:
            return True
    asked.append(text)
    if len(asked) > 16:
        s["asked_prompts"] = asked[-16:]
    return False

async def send_unique(msg_obj, uid: int, text: str, reply_markup=None, force: bool = False):
    if force or not is_duplicate_question(uid, text):
        await msg_obj.reply_text(text, reply_markup=reply_markup)

# -------- Sheets (with memory fallback) --------
SHEETS_ENABLED = True
ss = None
ws_feedback = ws_users = ws_profiles = ws_episodes = ws_reminders = ws_daily = ws_rules = None
ws_prefs = None  # NEW: Preferences

# === Canonical headers + safe reader (фикс дубликатов хедеров) ===
USERS_HEADERS = ["user_id","username","lang","consent","tz_offset","checkin_hour","paused","quiet_hours","last_sent_utc","sent_today","streak","challenge_id","challenge_day","last_fb_asked"]
PROFILES_HEADERS = ["user_id","sex","age","goal","conditions","meds","allergies","sleep","activity","diet","notes","updated_at","goals","diet_focus","steps_target","cycle_enabled","cycle_last_date","cycle_avg_len","city","height","weight"]
EPISODES_HEADERS = ["episode_id","user_id","topic","started_at","baseline_severity","red_flags","plan_accepted","target","reminder_at","next_checkin_at","status","last_update","notes"]
REMINDERS_HEADERS = ["id","user_id","text","when_utc","created_at","status"]
DAILY_HEADERS = ["timestamp","user_id","mood","comment"]
FEEDBACK_HEADERS = ["timestamp","user_id","name","username","rating","comment"]
RULES_HEADERS = ["rule_id","domain","segment","lang","text","citations"]
PREFS_HEADERS = ["user_id","likes_json","dislikes_json","meal_budget","reminder_preset"]  # NEW

def ws_records(ws, expected_headers):
    try:
        return ws.get_all_records(expected_headers=expected_headers, default_blank="")
    except Exception as e:
        logging.error(f"ws_records fallback ({getattr(ws,'title','?')}): {e}")
        vals = ws.get_all_values()
        if not vals: return []
        body = vals[1:]
        out = []
        for row in body:
            row = (row + [""] * len(expected_headers))[:len(expected_headers)]
            out.append({h: row[i] for i, h in enumerate(expected_headers)})
        return out

# === Сохраняем gspread client и id таблицы для register_intake_pro ===
GSPREAD_CLIENT: Optional[gspread.client.Client] = None
SPREADSHEET_ID_FOR_INTAKE: str = ""

def _sheets_init():
    global SHEETS_ENABLED, ss, ws_feedback, ws_users, ws_profiles, ws_episodes, ws_reminders, ws_daily, ws_rules, ws_prefs
    global GSPREAD_CLIENT, SPREADSHEET_ID_FOR_INTAKE
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if not creds_json:
            raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")
        creds = json.loads(creds_json)
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds, scope)
        gclient = gspread.authorize(credentials)
        GSPREAD_CLIENT = gclient

        try:
            ss = gclient.open_by_key(SHEET_ID) if SHEET_ID else gclient.open(SHEET_NAME)
        except SpreadsheetNotFound:
            if ALLOW_CREATE_SHEET:
                ss = gclient.create(SHEET_NAME)
            else:
                raise

        try:
            SPREADSHEET_ID_FOR_INTAKE = ss.id
        except Exception:
            SPREADSHEET_ID_FOR_INTAKE = SHEET_ID or ""

        def _ensure_ws(title: str, headers: List[str]):
            try:
                ws = ss.worksheet(title)
            except gspread.WorksheetNotFound:
                ws = ss.add_worksheet(title=title, rows=2000, cols=max(20, len(headers)))
                ws.append_row(headers)
            if not ws.get_all_values():
                ws.append_row(headers)
            # расширить заголовки при апдейтах схемы:
            cur_hdr = ws.row_values(1)
            if len(cur_hdr) < len(headers):
                for i in range(len(cur_hdr), len(headers)):
                    ws.update_cell(1, i+1, headers[i])
            return ws

        ws_feedback = _ensure_ws("Feedback", FEEDBACK_HEADERS)
        ws_users    = _ensure_ws("Users", USERS_HEADERS)
        ws_profiles = _ensure_ws("Profiles", PROFILES_HEADERS)
        ws_episodes = _ensure_ws("Episodes", EPISODES_HEADERS)
        ws_reminders= _ensure_ws("Reminders", REMINDERS_HEADERS)
        ws_daily    = _ensure_ws("DailyCheckins", DAILY_HEADERS)
        ws_rules    = _ensure_ws("Rules", RULES_HEADERS)
        ws_prefs    = _ensure_ws("Preferences", PREFS_HEADERS)  # NEW
        logging.info("Google Sheets connected.")
    except Exception as e:
        SHEETS_ENABLED = False
        logging.error(f"SHEETS disabled (fallback to memory). Reason: {e}")

_sheets_init()

# --------- Memory fallback stores ----------
MEM_USERS: Dict[int, dict] = {}
MEM_PROFILES: Dict[int, dict] = {}
MEM_EPISODES: List[dict] = []
MEM_REMINDERS: List[dict] = []
MEM_FEEDBACK: List[dict] = []
MEM_DAILY: List[dict] = []
MEM_RULES: List[dict] = []
MEM_PREFS: Dict[int, dict] = {}

# --------- Sessions ----------
sessions: Dict[int, dict] = {}

# -------- Sheets helpers/wrappers --------
def _headers(ws):
    return ws.row_values(1)

def users_get(uid: int) -> dict:
    if SHEETS_ENABLED:
        for r in ws_records(ws_users, USERS_HEADERS):
            if str(r.get("user_id")) == str(uid):
                return r
        return {}
    return MEM_USERS.get(uid, {})

def users_upsert(uid: int, username: str, lang: str):
    base_defaults = {
        "user_id": str(uid),
        "username": username or "",
        "lang": lang,
        "consent": "no",
        "tz_offset": "0",
        "checkin_hour": DEFAULT_CHECKIN_LOCAL,
        "paused": "no",
        "quiet_hours": "22:00-08:00",
        "last_sent_utc": "",
        "sent_today": "0",
        "streak": "0",
        "challenge_id": "",
        "challenge_day": "",
        "last_fb_asked": ""
    }
    if SHEETS_ENABLED:
        rows = ws_records(ws_users, USERS_HEADERS)
        for i, r in enumerate(rows, start=2):
            if str(r.get("user_id")) == str(uid):
                merged = {**base_defaults, **r}
                if username: merged["username"] = username
                if lang:     merged["lang"] = lang
                ws_users.update(range_name=f"A{i}:N{i}", values=[[merged.get(h,"") for h in USERS_HEADERS]])
                return
        ws_users.append_row([base_defaults.get(h,"") for h in USERS_HEADERS])
    else:
        cur = MEM_USERS.get(uid, {})
        merged = {**base_defaults, **cur}
        if username: merged["username"] = username
        if lang:     merged["lang"] = lang
        MEM_USERS[uid] = merged

def users_set(uid: int, field: str, value: str):
    if SHEETS_ENABLED:
        vals = ws_records(ws_users, USERS_HEADERS)
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                hdr = USERS_HEADERS
                if field in hdr:
                    ws_users.update_cell(i, hdr.index(field)+1, value)
                return
    else:
        u = MEM_USERS.setdefault(uid, {})
        u[field] = value

def _merge_profile_row(row: dict, data: dict) -> dict:
    merged = dict(row or {})
    for k, v in data.items():
        merged[k] = "" if v is None else (", ".join(v) if isinstance(v, list) else str(v))
    merged["updated_at"] = iso(utcnow())
    return merged

def profiles_get(uid: int) -> dict:
    if SHEETS_ENABLED:
        for r in ws_records(ws_profiles, PROFILES_HEADERS):
            if str(r.get("user_id")) == str(uid):
                return r
        return {}
    return MEM_PROFILES.get(uid, {})

def profiles_upsert(uid: int, data: dict):
    if SHEETS_ENABLED:
        hdr = PROFILES_HEADERS
        current, idx = None, None
        for i, r in enumerate(ws_records(ws_profiles, PROFILES_HEADERS), start=2):
            if str(r.get("user_id")) == str(uid):
                current, idx = r, i
                break
        merged = _merge_profile_row(current or {"user_id": str(uid)}, data)
        values = [merged.get(h,"") for h in hdr]
        end_col = gsu.rowcol_to_a1(1, len(hdr)).rstrip("1")
        if idx:
            ws_profiles.update(range_name=f"A{idx}:{end_col}{idx}", values=[values])
        else:
            ws_profiles.append_row(values)
    else:
        row = MEM_PROFILES.setdefault(uid, {"user_id": str(uid)})
        for k,v in data.items():
            row[k] = "" if v is None else (", ".join(v) if isinstance(v,list) else str(v))
        row["updated_at"] = iso(utcnow())
    # отметить, что профиль обновили — покажем плашку один раз
    sessions.setdefault(uid, {})["profile_banner_pending"] = True

def prefs_get(uid: int) -> dict:
    if SHEETS_ENABLED:
        for r in ws_records(ws_prefs, PREFS_HEADERS):
            if str(r.get("user_id")) == str(uid):
                return r
        return {}
    return MEM_PREFS.get(uid, {})

def prefs_upsert(uid: int, data: dict):
    if SHEETS_ENABLED:
        rows = ws_records(ws_prefs, PREFS_HEADERS)
        idx = None
        row = {"user_id": str(uid), "likes_json":"{}", "dislikes_json":"{}", "meal_budget":"", "reminder_preset":""}
        for i, r in enumerate(rows, start=2):
            if str(r.get("user_id")) == str(uid):
                row.update(r); idx = i; break
        for k,v in data.items():
            row[k] = v
        vals = [[row.get(h,"") for h in PREFS_HEADERS]]
        end_col = gsu.rowcol_to_a1(1, len(PREFS_HEADERS)).rstrip("1")
        if idx:
            ws_prefs.update(range_name=f"A{idx}:{end_col}{idx}", values=vals)
        else:
            ws_prefs.append_row(vals[0])
    else:
        row = MEM_PREFS.setdefault(uid, {"user_id": str(uid)})
        row.update(data)

def episode_create(uid: int, topic: str, severity: int, red: str) -> str:
    eid = f"{uid}-{uuid.uuid4().hex[:8]}"
    now = iso(utcnow())
    rec = {"episode_id":eid,"user_id":str(uid),"topic":topic,"started_at":now,
           "baseline_severity":str(severity),"red_flags":red,"plan_accepted":"0",
           "target":"<=3/10","reminder_at":"","next_checkin_at":"","status":"open",
           "last_update":now,"notes":""}
    if SHEETS_ENABLED:
        ws_episodes.append_row([rec.get(h,"") for h in EPISODES_HEADERS])
    else:
        MEM_EPISODES.append(rec)
    return eid

def episode_find_open(uid: int) -> Optional[dict]:
    if SHEETS_ENABLED:
        for r in ws_records(ws_episodes, EPISODES_HEADERS):
            if r.get("user_id")==str(uid) and r.get("status")=="open":
                return r
        return None
    for r in MEM_EPISODES:
        if r["user_id"]==str(uid) and r["status"]=="open":
            return r
    return None

def episode_set(eid: str, field: str, value: str):
    if SHEETS_ENABLED:
        vals = ws_episodes.get_all_values(); hdr = vals[0]
        if field not in hdr:
            return
        col = hdr.index(field)+1
        for i in range(2, len(vals)+1):
            if ws_episodes.cell(i,1).value == eid:
                ws_episodes.update_cell(i,col,value)
                ws_episodes.update_cell(i,hdr.index("last_update")+1, iso(utcnow()))
                return
    else:
        for r in MEM_EPISODES:
            if r["episode_id"]==eid:
                r[field]=value; r["last_update"]=iso(utcnow()); return

def feedback_add(ts, uid, name, username, rating, comment):
    if SHEETS_ENABLED:
        ws_feedback.append_row([ts,str(uid),name,username or "",rating,comment])
    else:
        MEM_FEEDBACK.append({"timestamp":ts,"user_id":str(uid),"name":name,"username":username or "","rating":rating,"comment":comment})

def reminder_add(uid: int, text: str, when_utc: datetime):
    rid = f"{uid}-{uuid.uuid4().hex[:6]}"
    rec = {"id":rid,"user_id":str(uid),"text":text,"when_utc":iso(when_utc),"created_at":iso(utcnow()),"status":"scheduled"}
    if SHEETS_ENABLED:
        ws_reminders.append_row([rec.get(h,"") for h in REMINDERS_HEADERS])
    else:
        MEM_REMINDERS.append(rec)
    return rid

def reminders_all_records():
    if SHEETS_ENABLED:
        return ws_records(ws_reminders, REMINDERS_HEADERS)
    return MEM_REMINDERS.copy()

def reminders_mark_sent(rid: str):
    if SHEETS_ENABLED:
        vals = ws_reminders.get_all_values()
        for i in range(2, len(vals)+1):
            if ws_reminders.cell(i,1).value == rid:
                ws_reminders.update_cell(i,6,"sent"); return
    else:
        for r in MEM_REMINDERS:
            if r["id"]==rid:
                r["status"]="sent"; return

def daily_add(ts, uid, mood, comment):
    if SHEETS_ENABLED:
        ws_daily.append_row([ts,str(uid),mood,comment or ""])
    else:
        MEM_DAILY.append({"timestamp":ts,"user_id":str(uid),"mood":mood,"comment":comment or ""})

# --------- JobQueue helper ----------
def _has_jq_app(app) -> bool:
    return getattr(app, "job_queue", None) is not None

def _has_jq_ctx(context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        return getattr(context.application, "job_queue", None) is not None
    except Exception:
        return False

# --------- Scheduling (restore) ---------
def schedule_from_sheet_on_start(app):
    if not _has_jq_app(app):
        logging.warning("JobQueue not available – skip scheduling on start.")
        return
    now = utcnow()
    src = ws_records(ws_episodes, EPISODES_HEADERS) if SHEETS_ENABLED else MEM_EPISODES
    for r in src:
        if r.get("status")!="open": continue
        eid = r.get("episode_id"); uid = int(r.get("user_id"))
        nca = r.get("next_checkin_at") or ""
        if not nca: continue
        try:
            dt_ = datetime.strptime(nca, "%Y-%m-%d %H:%M:%S%z")
        except:
            continue
        delay = max(60, (dt_-now).total_seconds())
        app.job_queue.run_once(job_checkin_episode, when=delay, data={"user_id":uid,"episode_id":eid})
    for r in reminders_all_records():
        if (r.get("status") or "")!="scheduled": continue
        uid = int(r.get("user_id")); rid=r.get("id")
        try:
            dt_ = datetime.strptime(r.get("when_utc"), "%Y-%m-%d %H:%M:%S%z")
        except:
            continue
        delay = max(60,(dt_-now).total_seconds())
        app.job_queue.run_once(job_oneoff_reminder, when=delay, data={"user_id":uid,"reminder_id":rid})
    src_u = ws_records(ws_users, USERS_HEADERS) if SHEETS_ENABLED else list(MEM_USERS.values())
    for u in src_u:
        if (u.get("paused") or "").lower()=="yes": continue
        uid = int(u.get("user_id"))
        tz_off = int(str(u.get("tz_offset") or "0"))
        hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
        schedule_daily_checkin(app, uid, tz_off, hhmm, norm_lang(u.get("lang") or "en"))
        schedule_morning_evening(app, uid, tz_off, norm_lang(u.get("lang") or "en"))

def hhmm_tuple(hhmm:str)->Tuple[int,int]:
    m = re.search(r'([01]?\d|2[0-3]):([0-5]\d)', hhmm.strip())
    return (int(m.group(1)), int(m.group(2))) if m else (8,30)

def local_to_utc_hour_min(tz_offset_hours:int, hhmm:str)->Tuple[int,int]:
    h,m = hhmm_tuple(hhmm); return ((h - tz_offset_hours) % 24, m)

def schedule_daily_checkin(app, uid:int, tz_off:int, hhmm_local:str, lang:str):
    if not _has_jq_app(app):
        logging.warning(f"JobQueue not available – skip daily scheduling for uid={uid}.")
        return
    for j in app.job_queue.get_jobs_by_name(f"daily_{uid}"):
        j.schedule_removal()
    h_utc, m_utc = local_to_utc_hour_min(tz_off, hhmm_local)
    t = dtime(hour=h_utc, minute=m_utc, tzinfo=timezone.utc)
    app.job_queue.run_daily(job_daily_checkin, time=t, name=f"daily_{uid}", data={"user_id":uid,"lang":lang})

# === Утро+Вечер по локальному времени (simple) ===
def schedule_morning_evening(app, uid:int, tz_off:int, lang:str, morning="08:30", evening="20:30"):
    if not _has_jq_app(app): return
    for name in [f"daily_m_{uid}", f"daily_e_{uid}"]:
        for j in app.job_queue.get_jobs_by_name(name): j.schedule_removal()
    h_m, m_m = hhmm_tuple(morning); h_m = (h_m - tz_off) % 24
    h_e, m_e = hhmm_tuple(evening); h_e = (h_e - tz_off) % 24
    app.job_queue.run_daily(job_daily_checkin, dtime(hour=h_m, minute=m_m, tzinfo=timezone.utc),
                            name=f"daily_m_{uid}", data={"user_id":uid,"lang":lang})
    app.job_queue.run_daily(job_daily_checkin, dtime(hour=h_e, minute=m_e, tzinfo=timezone.utc),
                            name=f"daily_e_{uid}", data={"user_id":uid,"lang":lang})

# ------------- Лимитер авто-сообщений + тихие часы -------------
def _in_quiet(uid: int, now_utc: datetime) -> bool:
    u = users_get(uid)
    q = (u.get("quiet_hours") or "").strip()
    if not q: return False
    m = re.match(r'(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})', q)
    if not m: return False
    tz_off = int(str(u.get("tz_offset") or "0"))
    local = now_utc + timedelta(hours=tz_off)
    start = local.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    end   = local.replace(hour=int(m.group(3)), minute=int(m.group(4)), second=0, microsecond=0)
    if end <= start:
        return local >= start or local <= end
    return start <= local <= end

def can_send(uid: int) -> bool:
    u = users_get(uid)
    if (u.get("paused") or "").lower() == "yes": return False
    if _in_quiet(uid, utcnow()): return False
    sent_today = int(str(u.get("sent_today") or "0"))
    return sent_today < 2

def mark_sent(uid: int):
    u = users_get(uid)
    tz_off = int(str(u.get("tz_offset") or "0"))
    last = u.get("last_sent_utc") or ""
    today_local = (utcnow() + timedelta(hours=tz_off)).date()
    last_local  = None
    if last:
        try:
            last_local = (datetime.strptime(last, "%Y-%m-%d %H:%M:%S%z").astimezone(timezone.utc) + timedelta(hours=tz_off)).date()
        except:
            last_local = None
    sent = 0 if (not last_local or last_local != today_local) else int(str(u.get("sent_today") or "0"))
    users_set(uid, "sent_today", str(sent + 1))
    users_set(uid, "last_sent_utc", iso(utcnow()))

async def maybe_send(context: ContextTypes.DEFAULT_TYPE, uid: int, text: str, kb=None):
    if can_send(uid):
        try:
            await context.bot.send_message(uid, text, reply_markup=kb)
            mark_sent(uid)
        except Exception as e:
            logging.error(f"send fail: {e}")

# --- Helper: feedback ≤ 1/день ---
def can_ask_feedback(uid: int) -> bool:
    u = users_get(uid)
    tz_off = int(str(u.get("tz_offset") or "0"))
    last_fb = (u.get("last_fb_asked") or "").strip()
    today_local = (utcnow() + timedelta(hours=tz_off)).date().isoformat()
    if not last_fb or last_fb != today_local:
        return True
    return False

def mark_feedback_asked(uid: int):
    u = users_get(uid)
    tz_off = int(str(u.get("tz_offset") or "0"))
    today_local = (utcnow() + timedelta(hours=tz_off)).date().isoformat()
    users_set(uid, "last_fb_asked", today_local)

# ------------- Jobs -------------
async def job_checkin_episode(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, eid = d.get("user_id"), d.get("episode_id")
    if not uid or not eid: return
    u = users_get(uid)
    if (u.get("paused") or "").lower()=="yes": return
    lang = norm_lang(u.get("lang") or "en")
    kb = inline_numbers_0_10()
    try:
        await context.bot.send_message(uid, T[lang]["checkin_ping"], reply_markup=kb)
        episode_set(eid, "next_checkin_at", "")
    except Exception as e:
        logging.error(f"job_checkin_episode send error: {e}")

async def job_oneoff_reminder(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, rid = d.get("user_id"), d.get("reminder_id")
    text = T[norm_lang(users_get(uid).get("lang") or "en")]["thanks"]
    for r in reminders_all_records():
        if r.get("id")==rid:
            text = r.get("text") or text; break
    try:
        await context.bot.send_message(uid, text)
    except Exception as e:
        logging.error(f"reminder send error: {e}")
    reminders_mark_sent(rid)

# ------------- LLM Router (тёплый стиль: 1 мысль → 1 вопрос) -------------
SYS_ROUTER = (
    "You are TendAI — a concise, warm, professional health & longevity assistant (not a doctor). "
    "Always answer strictly in {lang}. Keep replies ultra-brief: ≤4 lines + up to 3 bullets. "
    "Style: one thought, then one short question to move forward. "
    "Personalize using the provided profile (sex/age/goal/conditions). "
    "TRIAGE: ask up to 1 clarifier first; advise ER only for clear red flags. "
    "Return JSON ONLY like: "
    "{\"intent\":\"symptom\"|\"nutrition\"|\"sleep\"|\"labs\"|\"habits\"|\"longevity\"|\"other\","
    "\"assistant_reply\": \"string\", \"followups\": [\"string\"], \"needs_more\": true, "
    "\"red_flags\": false, \"confidence\": 0.0}"
)

def llm_router_answer(text: str, lang: str, profile: dict) -> dict:
    if not oai:
        return {"intent":"other","assistant_reply":T[lang]["unknown"],"followups":[],"needs_more":True,"red_flags":False,"confidence":0.3}
    sys = SYS_ROUTER.replace("{lang}", lang) + f"\nUserProfile: {json.dumps(profile, ensure_ascii=False)}"
    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.25,
            max_tokens=420,
            response_format={"type":"json_object"},
            messages=[{"role":"system","content":sys},{"role":"user","content":text}]
        )
        out = resp.choices[0].message.content.strip()
        data = json.loads(out)
        if "followups" not in data or data["followups"] is None:
            data["followups"] = []
        # жестко ограничим 1 уточняющий вопрос
        data["followups"] = (data.get("followups") or [])[:1]
        return data
    except Exception as e:
        logging.error(f"router LLM error: {e}")
        return {"intent":"other","assistant_reply":T[lang]["unknown"],"followups":[],"needs_more":True,"red_flags":False,"confidence":0.3}

# ===== Health60 — 3 блока + мягкий follow-up =====
def health60_make_plan(lang: str, user_text: str, profile: dict) -> str:
    if not oai:
        return T[lang]["unknown"]
    sys = (
        "You are TendAI — concise, warm, evidence-oriented (not a doctor). Language: {lang}. "
        "Return three compact sections with bullets and finish with a single short question to continue. "
        f"1) {T[lang]['h60_t1']}\n"
        f"2) {T[lang]['h60_t2']}\n"
        f"3) {T[lang]['h60_t3']}\n"
        "If red flags plausible, append one line starting with '⚠️'. End with: 'Выбери вариант?' or 'Продолжим?' (localized)."
    ).replace("{lang}", lang)
    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.2,
            max_tokens=340,
            messages=[
                {"role":"system","content": sys + "\nUserProfile: " + json.dumps(profile, ensure_ascii=False)},
                {"role":"user","content": user_text}
            ]
        )
        txt = (resp.choices[0].message.content or "").strip()
        if T[lang]["h60_t1"] not in txt:
            txt = f"{T[lang]['h60_t1']}:\n- " + txt.replace("\n", "\n- ")
        return txt
    except Exception as e:
        logging.error(f"health60_make_plan error: {e}")
        return T[lang]["unknown"]

# ===== Rules-based подсказки (доказательная база) =====
def rules_match(seg: str, prof: dict) -> bool:
    if not seg:
        return True
    for part in seg.split("&"):
        m = re.match(r'(\w+)\s*(>=|<=|=|>|<)\s*([\w\-]+)', part.strip())
        if not m:
            return False
        k, op, v = m.groups()
        pv = (prof.get(k) or prof.get(k.lower()) or "")
        if k in ("age", "steps_target", "cycle_avg_len"):
            try:
                pv = int(re.search(r'\d+', str(pv)).group())
                v = int(v)
            except Exception:
                return False
        else:
            pv = str(pv).lower()
            v = str(v).lower()
        if op == "=" and not (pv == v): return False
        if op == ">=" and not (pv >= v): return False
        if op == "<=" and not (pv <= v): return False
        if op == ">"  and not (pv >  v): return False
        if op == "<"  and not (pv <  v): return False
    return True

def _read_rules():
    if SHEETS_ENABLED:
        return ws_records(ws_rules, RULES_HEADERS)
    return MEM_RULES

def pick_nutrition_tips(lang: str, prof: dict, limit: int = 2) -> List[str]:
    tips = []
    for r in _read_rules():
        if (r.get("domain") or "").lower() != "nutrition":
            continue
        if (r.get("lang") or "en") != lang:
            continue
        if rules_match(r.get("segment") or "", prof):
            t = (r.get("text") or "").strip()
            if t:
                tips.append(t)
    random.shuffle(tips)
    return tips[:limit]

# ===== Мини-логика цикла (опционально) =====
def cycle_phase_for(uid: int) -> Optional[str]:
    prof = profiles_get(uid)
    if str(prof.get("cycle_enabled") or "").lower() not in {"1","yes","true"}:
        return None
    try:
        last = datetime.strptime(str(prof.get("cycle_last_date")), "%Y-%m-%d").date()
        avg  = int(str(prof.get("cycle_avg_len") or "28"))
    except Exception:
        return None
    day = ((utcnow().date() - last).days % max(avg, 21)) + 1
    if 1 <= day <= 5:   return "menses"
    if 6 <= day <= 13:  return "follicular"
    if 14 <= day <= 15: return "ovulation"
    return "luteal"

def cycle_tip(lang: str, phase: str) -> str:
    if lang not in T: lang = "en"
    base = {
        "menses": {
            "ru":"Фаза менструации: мягче к себе, железо/белок, приоритет сна.",
            "en":"Menses phase: go gentle, prioritize iron/protein and sleep."
        },
        "follicular": {
            "ru":"Фолликулярная фаза: лучше заходят тренировки/новые задачи.",
            "en":"Follicular phase: great for workouts and new tasks."
        },
        "ovulation": {
            "ru":"Овуляция: следи за сном и гидратацией.",
            "en":"Ovulation: watch sleep and hydration."
        },
        "luteal": {
            "ru":"Лютеиновая: магний/прогулка, стабильный сон, меньше кофеина.",
            "en":"Luteal: magnesium/walk, steady sleep, go easy on caffeine."
        }
    }
    return base.get(phase, {}).get(lang, "")

# ===== Переопределяем daily-джобу: приветствие + питание + цикл + мягкий вопрос =====
async def job_daily_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, lang = d.get("user_id"), d.get("lang","en")
    u = users_get(uid)
    if (u.get("paused") or "").lower()=="yes":
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_good"], callback_data="mood|good"),
         InlineKeyboardButton(T[lang]["mood_ok"], callback_data="mood|ok"),
         InlineKeyboardButton(T[lang]["mood_bad"], callback_data="mood|bad")],
        [InlineKeyboardButton(T[lang]["mood_note"], callback_data="mood|note")]
    ])
    await maybe_send(context, uid, T[lang]["daily_gm"], kb)

    prof = profiles_get(uid)
    tips = pick_nutrition_tips(lang, prof, limit=1)
    if tips:
        await maybe_send(context, uid, "• " + "\n• ".join(tips))

    phase = cycle_phase_for(uid)
    if phase:
        tip = cycle_tip(lang, phase)
        if tip:
            await maybe_send(context, uid, tip)

    # Автовопрос: питание или сон?
    prompt = {"ru":"Продолжим: питание или сон?","en":"Continue: nutrition or sleep?"}.get(lang, "Продовжимо: харчування чи сон?")
    await maybe_send(context, uid, prompt)

# ===== Serious keywords =====
SERIOUS_KWS = {
    "diabetes":["diabetes","диабет","сахарный","цукров","глюкоза","hba1c","гликированный"],
    "hepatitis":["hepatitis","гепатит","печень","hbs","hcv","alt","ast"],
    "cancer":["cancer","рак","онко","онколог","опухол","пухлина","tumor"],
    "tb":["tuberculosis","tb","туберкул","туберкульоз"],
}

# ===== Персонализированный префикс (плашка) =====
def _ru_sex_label(v: str) -> str:
    v = (v or "").lower()
    if v in ("male","мужской","m"): return "мужчина"
    if v in ("female","женский","f"): return "женщина"
    return "человек"

def _ru_goal_label(v: str) -> str:
    v = (v or "").lower()
    mapping = {"longevity":"долголетие","energy":"энергия","sleep":"сон","weight":"снижение веса","strength":"сила"}
    return mapping.get(v, v or "цель")

def _parse_age(v: str) -> str:
    m = re.search(r'\d{2,3}', str(v or ""))
    return m.group(0) if m else ""

def formatted_profile_banner(lang: str, profile: dict) -> str:
    # Русская формулировка «мужчина, 42; цель — долголетие»
    if lang == "ru":
        sex_ru = _ru_sex_label(profile.get("sex"))
        age_ru = _parse_age(profile.get("age")) or "—"
        goal_ru= _ru_goal_label(profile.get("goal") or profile.get("goals"))
        return T[lang]["px"].format(sex_ru=sex_ru, age=age_ru, goal_ru=goal_ru)
    # EN короче: "Profile set. Goal: longevity."
    if lang == "en":
        goal = (profile.get("goal") or profile.get("goals") or "goal").lower()
        return f"Profile set. Goal: {goal}."
    return ""  # для uk/es можно расширить при желании

def should_show_profile_banner(uid: int) -> bool:
    s = sessions.setdefault(uid, {})
    return bool(s.get("profile_banner_pending"))

def mark_profile_banner_shown(uid: int):
    sessions.setdefault(uid, {})["profile_banner_pending"] = False

def personalized_prefix(lang: str, profile: dict, uid: Optional[int]=None) -> str:
    # не повторяем каждый раз; показываем только по событию
    if uid is None or not should_show_profile_banner(uid):
        return ""
    text = formatted_profile_banner(lang, profile)
    mark_profile_banner_shown(uid)
    return text

# ===== Клавиатуры (минимум кнопок, короткие подписи) =====
def inline_numbers_0_10() -> InlineKeyboardMarkup:
    rows = []
    row1 = [InlineKeyboardButton(str(n), callback_data=f"num|{n}") for n in range(0, 6)]
    row2 = [InlineKeyboardButton(str(n), callback_data=f"num|{n}") for n in range(6, 11)]
    rows.append(row1); rows.append(row2); rows.append([InlineKeyboardButton("◀", callback_data="pain|exit")])
    return InlineKeyboardMarkup(rows)

def inline_list(options: List[str], prefix: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for opt in options:
        row.append(InlineKeyboardButton(opt, callback_data=f"{prefix}|{opt}"))
        if len(row) == 3: rows.append(row); row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def inline_topic_kb(lang: str) -> InlineKeyboardMarkup:
    label = {"ru":"🧩 Опрос 6 пунктов","uk":"🧩 Опитник (6)","en":"🧩 Intake (6 Qs)","es":"🧩 Intake (6)"}[lang]
    # максимум 6–7 кнопок: 2 ряда по 3 + профиль
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🩺 Pain", callback_data="topic|pain"),
         InlineKeyboardButton("😴 Sleep", callback_data="topic|sleep"),
         InlineKeyboardButton("🍎 Nutrition", callback_data="topic|nutrition")],
        [InlineKeyboardButton("🧪 Labs", callback_data="topic|labs"),
         InlineKeyboardButton("🔁 Habits", callback_data="topic|habits"),
         InlineKeyboardButton("🧬 Longevity", callback_data="topic|longevity")],
        [InlineKeyboardButton("👤 Profile", callback_data="topic|profile")],
        [InlineKeyboardButton(label, callback_data="intake:start")]
    ])

def inline_accept(lang: str) -> InlineKeyboardMarkup:
    labels = T[lang]["accept_opts"]
    return InlineKeyboardMarkup([[InlineKeyboardButton(labels[0], callback_data="acc|yes"),
                                  InlineKeyboardButton(labels[1], callback_data="acc|later"),
                                  InlineKeyboardButton(labels[2], callback_data="acc|no")]])

def inline_remind(lang: str) -> InlineKeyboardMarkup:
    # только 3 пресета: +4ч, вечером, утром
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["act_rem_4h"], callback_data="rem|4h")],
        [InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="rem|evening")],
        [InlineKeyboardButton(T[lang]["act_rem_morn"], callback_data="rem|morning")]
    ])

def inline_feedback_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["fb_good"], callback_data="fb|up"),
         InlineKeyboardButton(T[lang]["fb_bad"],  callback_data="fb|down")],
        [InlineKeyboardButton(T[lang]["fb_free"], callback_data="fb|text")]
    ])

def inline_actions(lang: str) -> InlineKeyboardMarkup:
    # ≤7 кнопок: H60 (1), Rem +4h (2), Rem evening (3), Rem morning (4), Ex+ER (5–6), Lab (7)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["h60_btn"], callback_data="act|h60")],
        [InlineKeyboardButton(T[lang]["act_rem_4h"],  callback_data="act|rem|4h")],
        [InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="act|rem|evening")],
        [InlineKeyboardButton(T[lang]["act_rem_morn"],callback_data="act|rem|morning")],
        [InlineKeyboardButton(T[lang]["act_ex_neck"], callback_data="act|ex|neck"),
         InlineKeyboardButton(T[lang]["act_er"],      callback_data="act|er")],
        [InlineKeyboardButton(T[lang]["act_find_lab"], callback_data="act|lab")]
    ])

# ===== Youth-пакет: команды =====
async def cmd_energy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    tips = {
      "en": [
        "1) 10-min brisk walk.",
        "2) 300–500 ml water + light protein.",
        "3) 20-min off screens. OK?"
      ],
      "ru": [
        "1) Быстрая ходьба 10 мин.",
        "2) 300–500 мл воды + лёгкий белок.",
        "3) 20 мин без экрана. Ок?"
      ],
      "uk": [
        "1) Швидка ходьба 10 хв.",
        "2) 300–500 мл води + легкий білок.",
        "3) 20 хв без екрана. Гаразд?"
      ],
      "es": [
        "1) Camina rápido 10 min.",
        "2) 300–500 ml de agua + proteína ligera.",
        "3) 20 min sin pantallas. ¿Ok?"
      ]
    }[lang]
    await update.message.reply_text(T[lang]["energy_title"] + "\n" + "\n".join(tips), reply_markup=inline_actions(lang))

async def cmd_water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["act_rem_4h"], callback_data="act|rem|4h")]])
    await update.message.reply_text(T[lang]["water_prompt"], reply_markup=kb)

async def cmd_mood(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_good"], callback_data="mood|good"),
         InlineKeyboardButton(T[lang]["mood_ok"],   callback_data="mood|ok"),
         InlineKeyboardButton(T[lang]["mood_bad"],  callback_data="mood|bad")],
        [InlineKeyboardButton(T[lang]["mood_note"], callback_data="mood|note")]
    ])
    await update.message.reply_text(T[lang]["daily_gm"], reply_markup=kb)

async def cmd_skin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    tip = {
        "ru":"Умывание 2×/день тёплой водой, SPF утром, 1% ниацинамид вечером.",
        "en":"Wash face 2×/day lukewarm, SPF AM, 1% niacinamide PM.",
        "uk":"Вмивання 2×/день теплою водою, SPF зранку, 1% ніацинамід ввечері.",
        "es":"Lava el rostro 2×/día tibia, SPF por la mañana, 1% niacinamida por la noche."
    }[lang]
    await update.message.reply_text(T[lang]["skin_title"] + "\n" + tip, reply_markup=inline_actions(lang))
# --- Pain triage keyboards helper ---
def _kb_for_code(lang: str, code: str):
    if code == "painloc":
        kb = inline_list(T[lang]["triage_pain_q1_opts"], "painloc")
    elif code == "painkind":
        kb = inline_list(T[lang]["triage_pain_q2_opts"], "painkind")
    elif code == "paindur":
        kb = inline_list(T[lang]["triage_pain_q3_opts"], "paindur")
    elif code == "num":
        kb = inline_numbers_0_10()
    elif code == "painrf":
        kb = inline_list(T[lang]["triage_pain_q5_opts"], "painrf")
    else:
        kb = None
    if kb:
        rows = kb.inline_keyboard + [[InlineKeyboardButton(T[lang]["back"], callback_data="pain|exit")]]
        return InlineKeyboardMarkup(rows)
    return None

# ===== Post init =====
async def post_init(app):
    me = await app.bot.get_me()
    logging.info(f"BOT READY: @{me.username} (id={me.id})")

# ===== Commands =====
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = norm_lang(getattr(user, "language_code", None))
    users_upsert(user.id, user.username or "", lang)
    context.user_data["lang"] = lang
    sessions.setdefault(user.id, {})["last_user_text"] = "/start"

    # приветствие + меню
    await update.message.reply_text(T[lang]["welcome"], reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))

    # одноразовая плашка профиля при старте, если профиль заполнен
    prof = profiles_get(user.id) or {}
    if not profile_is_incomplete(prof):
        sessions.setdefault(user.id, {})["profile_banner_pending"] = True
        banner = personalized_prefix(lang, prof, user.id)
        if banner:
            await update.message.reply_text(banner)

    # спросить про согласие только если ещё не отвечал
    u = users_get(user.id)
    if (u.get("consent") or "").lower() not in {"yes","no"}:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["yes"], callback_data="consent|yes"),
                                    InlineKeyboardButton(T[lang]["no"],  callback_data="consent|no")]])
        await update.message.reply_text(T[lang]["ask_consent"], reply_markup=kb)

    # поставить расписание
    tz_off = int(str(u.get("tz_offset") or "0"))
    hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, user.id, tz_off, hhmm, lang)
        schedule_morning_evening(context.application, user.id, tz_off, lang)
    else:
        logging.warning("JobQueue not available on /start – daily check-ins not scheduled.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await update.message.reply_text(T[lang]["help"])

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await update.message.reply_text(T[lang]["privacy"])

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "yes")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text(T[lang]["paused_on"])

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "no")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text(T[lang]["paused_off"])

async def cmd_delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if SHEETS_ENABLED:
        def _wipe(ws, col_name):
            try:
                vals = ws.get_all_values()
                if not vals: return
                hdr = vals[0]
                if col_name not in hdr: return
                col = hdr.index(col_name) + 1
                for i in range(len(vals), 1, -1):
                    if ws.cell(i, col).value == uid:
                        ws.delete_rows(i)
            except Exception as e:
                logging.warning(f"wipe failed for {getattr(ws,'title','?')}: {e}")

        _wipe(ws_users, "user_id")
        _wipe(ws_profiles, "user_id")
        _wipe(ws_episodes, "user_id")
        _wipe(ws_reminders, "user_id")
        _wipe(ws_daily, "user_id")
        _wipe(ws_feedback, "user_id")
        _wipe(ws_prefs, "user_id")
    else:
        MEM_USERS.pop(int(uid), None)
        MEM_PROFILES.pop(int(uid), None)
        global MEM_EPISODES, MEM_REMINDERS, MEM_DAILY, MEM_FEEDBACK, MEM_PREFS
        MEM_EPISODES = [r for r in MEM_EPISODES if r.get("user_id") != uid]
        MEM_REMINDERS = [r for r in MEM_REMINDERS if r.get("user_id") != uid]
        MEM_DAILY     = [r for r in MEM_DAILY     if r.get("user_id") != uid]
        MEM_FEEDBACK  = [r for r in MEM_FEEDBACK  if r.get("user_id") != uid]
        MEM_PREFS.pop(int(uid), None)

    lang = norm_lang(getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(T[lang]["deleted"], reply_markup=ReplyKeyboardRemove())

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None))
    await start_profile_ctx(context, update.effective_chat.id, lang, uid)

async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    parts = (update.message.text or "").split()
    if len(parts)<2 or not re.fullmatch(r"[+-]?\d{1,2}", parts[1]):
        await update.message.reply_text({"ru":"Формат: /settz +3","uk":"Формат: /settz +2",
                                         "en":"Usage: /settz +3","es":"Uso: /settz +3"}[lang]); return
    off = int(parts[1]); users_set(uid,"tz_offset",str(off))
    hhmm = users_get(uid).get("checkin_hour") or DEFAULT_CHECKIN_LOCAL
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, uid, off, hhmm, lang)
        schedule_morning_evening(context.application, uid, off, lang)
    await update.message.reply_text({"ru":f"Сдвиг часового пояса: {off}ч",
                                     "uk":f"Зсув: {off} год",
                                     "en":f"Timezone offset: {off}h",
                                     "es":f"Desfase horario: {off}h"}[lang])

async def cmd_checkin_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    parts = (update.message.text or "").split(maxsplit=1)
    hhmm = DEFAULT_CHECKIN_LOCAL
    if len(parts)==2:
        m = re.search(r'([01]?\d|2[0-3]):([0-5]\d)', parts[1])
        if m: hhmm = m.group(0)
    users_set(uid,"checkin_hour",hhmm)
    tz_off = int(str(users_get(uid).get("tz_offset") or "0"))
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, uid, tz_off, hhmm, lang)
        schedule_morning_evening(context.application, uid, tz_off, lang)
    else:
        logging.warning("JobQueue not available – daily check-in not scheduled.")
    await update.message.reply_text({"ru":f"Ежедневный чек-ин включён ({hhmm}).",
                                     "uk":f"Щоденний чек-ін увімкнено ({hhmm}).",
                                     "en":f"Daily check-in enabled ({hhmm}).",
                                     "es":f"Check-in diario activado ({hhmm})."}[lang])

async def cmd_checkin_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if _has_jq_ctx(context):
        for name in [f"daily_{uid}", f"daily_m_{uid}", f"daily_e_{uid}"]:
            for j in context.application.job_queue.get_jobs_by_name(name):
                j.schedule_removal()
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text({"ru":"Ежедневный чек-ин выключен.",
                                     "uk":"Щоденний чек-ін вимкнено.",
                                     "en":"Daily check-in disabled.",
                                     "es":"Check-in diario desactivado."}[lang])

async def cmd_health60(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None))
    sessions.setdefault(uid, {})["awaiting_h60"] = True
    await update.message.reply_text(T[lang]["h60_intro"])

async def cmd_intake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None) or "en")
    txt  = {"ru":"🧩 PRO-опрос: 6 ключевых вопросов. Готовы начать?",
            "uk":"🧩 PRO-опитник: 6 ключових питань. Починаємо?",
            "en":"🧩 PRO intake: 6 quick questions. Ready?",
            "es":"🧩 PRO intake: 6 quick questions. Ready?"}[lang]
    start_label = {"ru":"▶️ Начать","uk":"▶️ Почати","en":"▶️ Start","es":"▶️ Start"}[lang]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(start_label, callback_data="intake:start")]])
    await update.message.reply_text(txt, reply_markup=kb)

# ===== Profile steps =====
PROFILE_STEPS = [
    {"key":"sex","opts":{"ru":[("Мужской","male"),("Женский","female"),("Другое","other")],
                         "en":[("Male","male"),("Female","female"),("Other","other")],
                         "uk":[("Чоловіча","male"),("Жіноча","female"),("Інша","other")],
                         "es":[("Hombre","male"),("Mujer","female"),("Otro","other")]}},
    {"key":"age","opts":{"ru":[("18–25","22"),("26–35","30"),("36–45","42"),("46–60","50"),("60+","65")],
                         "en":[("18–25","22"),("26–35","30"),("36–45","42"),("46–60","50"),("60+","65")],
                         "uk":[("18–25","22"),("26–35","30"),("36–45","42"),("46–60","50"),("60+","65")],
                         "es":[("18–25","22"),("26–35","30"),("36–45","42"),("60+","65")]}},
    {"key":"goal","opts":{"ru":[("Похудение","weight"),("Энергия","energy"),("Сон","sleep"),("Долголетие","longevity"),("Сила","strength")],
                          "en":[("Weight","weight"),("Energy","energy"),("Sleep","sleep"),("Longevity","longevity"),("Strength","strength")],
                          "uk":[("Вага","weight"),("Енергія","energy"),("Сон","sleep"),("Довголіття","longevity"),("Сила","strength")],
                          "es":[("Peso","weight"),("Energía","energy"),("Sueño","sleep"),("Longevidad","longevity"),("Fuerza","strength")]}},
    {"key":"conditions","opts":{"ru":[("Нет","none"),("Гипертония","hypertension"),("Диабет","diabetes"),("Щитовидка","thyroid"),("Другое","other")],
                               "en":[("None","none"),("Hypertension","hypertension"),("Diabetes","diabetes"),("Thyroid","thyroid"),("Other","other")],
                               "uk":[("Немає","none"),("Гіпертонія","hypertension"),("Діабет","diabetes"),("Щитоподібна","thyroid"),("Інше","other")],
                               "es":[("Ninguna","none"),("Hipertensión","hypertension"),("Diabetes","diabetes"),("Tiroides","thyroid"),("Otra","other")]}},
    {"key":"meds","opts":{"ru":[("Нет","none"),("Магний","magnesium"),("Витамин D","vitd"),("Аллергии есть","allergies"),("Другое","other")],
                          "en":[("None","none"),("Magnesium","magnesium"),("Vitamin D","vitd"),("Allergies","allergies"),("Other","other")],
                          "uk":[("Немає","none"),("Магній","magnesium"),("Вітамін D","vitd"),("Алергії","allergies"),("Інше","other")],
                          "es":[("Ninguno","none"),("Magnesio","magnesium"),("Vitamina D","vitd"),("Alergias","allergies"),("Otro","other")]}},
    {"key":"sleep","opts":{"ru":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
                           "en":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Irregular","irregular")],
                           "uk":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
                           "es":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Irregular","irregular")]}},
    {"key":"activity","opts":{"ru":[("<5к шагов","<5k"),("5–8к","5-8k"),("8–12к","8-12k"),("Спорт регулярно","sport")],
                             "en":[("<5k steps","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Regular sport","sport")],
                             "uk":[("<5к кроків","<5k"),("5–8к","5-8k"),("8–12к","8-12k"),("Спорт регулярно","sport")],
                             "es":[("<5k pasos","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Deporte regular","sport")]}},
    {"key":"diet","opts":{"ru":[("Сбалансировано","balanced"),("Низкоугл/кето","lowcarb"),("Вегетар/веган","plant"),("Нерегулярно","irregular")],
                          "en":[("Balanced","balanced"),("Low-carb/keto","lowcarb"),("Vegetarian/vegan","plant"),("Irregular","irregular")],
                          "uk":[("Збалансовано","balanced"),("Маловугл/кето","lowcarb"),("Вегетар/веган","plant"),("Нерегулярно","irregular")],
                          "es":[("Equilibrada","balanced"),("Baja en carb/keto","lowcarb"),("Vegetariana/vegana","plant"),("Irregular","irregular")]}}
]

def build_profile_kb(lang:str, key:str, opts:List[Tuple[str,str]])->InlineKeyboardMarkup:
    rows=[]; row=[]
    for label,val in opts:
        row.append(InlineKeyboardButton(label, callback_data=f"p|choose|{key}|{val}"))
        if len(row)==3: rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([InlineKeyboardButton(T[lang]["write"], callback_data=f"p|write|{key}"),
                 InlineKeyboardButton(T[lang]["skip"],  callback_data=f"p|skip|{key}")])
    return InlineKeyboardMarkup(rows)

async def start_profile_ctx(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: int):
    sessions[uid] = {"profile_active": True, "p_step": 0, "p_wait_key": None}
    await context.bot.send_message(chat_id, T[lang]["profile_intro"], reply_markup=ReplyKeyboardRemove())
    step = PROFILE_STEPS[0]
    kb = build_profile_kb(lang, step["key"], step["opts"][lang])
    await context.bot.send_message(chat_id, T[lang]["p_step_1"], reply_markup=kb)

async def advance_profile_ctx(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: int):
    s = sessions.get(uid, {})
    s["p_step"] += 1
    if s["p_step"] < len(PROFILE_STEPS):
        idx = s["p_step"]; step = PROFILE_STEPS[idx]
        kb = build_profile_kb(lang, step["key"], step["opts"][lang])
        await context.bot.send_message(chat_id, T[lang][f"p_step_{idx+1}"], reply_markup=kb)
        return
    prof = profiles_get(uid); summary=[]
    for k in ["sex","age","goal","conditions","meds","sleep","activity","diet","city"]:
        v = prof.get(k) or sessions.get(uid,{}).get(k,"")
        if v: summary.append(f"{k}: {v}")
    profiles_upsert(uid, {})
    sessions[uid]["profile_active"] = False
    sessions[uid]["profile_banner_pending"] = True  # показать плашку один раз
    await context.bot.send_message(chat_id, T[lang]["saved_profile"] + "; ".join(summary))
    # сразу показать плашку
    banner = personalized_prefix(lang, profiles_get(uid), uid)
    if banner:
        await context.bot.send_message(chat_id, banner)
    await context.bot.send_message(chat_id, T[lang]["start_where"], reply_markup=inline_topic_kb(lang))

# ===== Main text handler =====
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    text = (update.message.text or "").strip()
    logging.info(f"INCOMING uid={uid} text={text[:200]}")
    urec = users_get(uid)
    if not urec:
        lang_guess = detect_lang_from_text(text, norm_lang(getattr(user, "language_code", None)))
        users_upsert(uid, user.username or "", lang_guess)
        sessions.setdefault(uid, {})["last_user_text"] = text
        await update.message.reply_text(T[lang_guess]["welcome"], reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text(T[lang_guess]["start_where"], reply_markup=inline_topic_kb(lang_guess))
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang_guess]["yes"], callback_data="consent|yes"),
                                    InlineKeyboardButton(T[lang_guess]["no"],  callback_data="consent|no")]])
        await update.message.reply_text(T[lang_guess]["ask_consent"], reply_markup=kb)
        if _has_jq_ctx(context):
            schedule_daily_checkin(context.application, uid, 0, DEFAULT_CHECKIN_LOCAL, lang_guess)
            schedule_morning_evening(context.application, uid, 0, lang_guess)
        await gate_show(update, context)
        return

    saved_lang = norm_lang(urec.get("lang") or getattr(user,"language_code",None))
    detected_lang = detect_lang_from_text(text, saved_lang)
    if detected_lang != saved_lang:
        users_set(uid,"lang",detected_lang)
    lang = detected_lang
    sessions.setdefault(uid, {})["last_user_text"] = text

    # серьёзные состояния
    sc = detect_serious(text)
    if sc:
        sessions.setdefault(uid,{})["mode"] = "serious"
        sessions[uid]["serious_condition"] = sc
        prof = profiles_get(uid)
        prefix = personalized_prefix(lang, prof, uid)
        plan = pain_plan(lang, [], prof)  # мягкий план + предупреждение
        msg = ((prefix + "\n") if prefix else "") + "\n".join(plan)
        await update.message.reply_text(msg, reply_markup=inline_actions(lang))
        if can_ask_feedback(uid):
            try:
                await update.message.reply_text(T[lang]["ask_fb"], reply_markup=inline_feedback_kb(lang))
                mark_feedback_asked(uid)
            except Exception:
                pass
        return

    # дневной комментарий
    if sessions.get(uid, {}).get("awaiting_daily_comment"):
        daily_add(iso(utcnow()), uid, "note", text)
        sessions[uid]["awaiting_daily_comment"] = False
        await update.message.reply_text(T[lang]["mood_thanks"]); return

    # свободный фидбек
    if sessions.get(uid, {}).get("awaiting_free_feedback"):
        sessions[uid]["awaiting_free_feedback"] = False
        feedback_add(iso(utcnow()), uid, "free", user.username, "", text)
        await update.message.reply_text(T[lang]["fb_thanks"]); return

    # город для лаборатории
    if sessions.get(uid, {}).get("awaiting_city"):
        sessions[uid]["awaiting_city"] = False
        city = text.strip()
        if city:
            profiles_upsert(uid, {"city": city})
        await update.message.reply_text(T[lang]["thanks"]); return

    # Health60
    if sessions.get(uid, {}).get("awaiting_h60"):
        sessions[uid]["awaiting_h60"] = False
        prof = profiles_get(uid)
        prefix = personalized_prefix(lang, prof, uid)
        plan = health60_make_plan(lang, text, prof)
        msg = ((prefix + "\n") if prefix else "") + str(plan)
        await update.message.reply_text(msg, reply_markup=inline_actions(lang))
        if can_ask_feedback(uid):
            try:
                await update.message.reply_text(T[lang]["ask_fb"], reply_markup=inline_feedback_kb(lang))
                mark_feedback_asked(uid)
            except Exception:
                pass
        return

    # ввод ручного ответа на шаге профиля
    if sessions.get(uid, {}).get("p_wait_key"):
        key = sessions[uid]["p_wait_key"]; sessions[uid]["p_wait_key"] = None
        val = text
        if key=="age":
            m = re.search(r'\d{2,3}', text)
            if m: val = m.group(0)
        profiles_upsert(uid,{key:val}); sessions[uid][key]=val
        await advance_profile_ctx(context, update.effective_chat.id, lang, uid); return

    # === Pain triage flow (коротко)
    s = sessions.get(uid, {})
    if s.get("topic") == "pain":
        if re.search(r"\b(stop|exit|back|назад|выход|выйти)\b", text.lower()):
            sessions.pop(uid, None)
            await update.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
            return
        if s.get("step") == 1:
            await send_unique(update.message, uid, T[lang]["triage_pain_q2"], reply_markup=_kb_for_code(lang, "painkind")); return
        if s.get("step") == 2:
            await send_unique(update.message, uid, T[lang]["triage_pain_q3"], reply_markup=_kb_for_code(lang, "paindur")); return
        if s.get("step") == 3:
            await update.message.reply_text(T[lang]["triage_pain_q4"], reply_markup=_kb_for_code(lang, "num")); return
        if s.get("step") == 4:
            m = re.fullmatch(r"(?:10|[0-9])", text)
            if m:
                sev = int(m.group(0)); s.setdefault("answers", {})["severity"] = sev; s["step"] = 5
                await update.message.reply_text(T[lang]["triage_pain_q5"], reply_markup=_kb_for_code(lang, "painrf")); return
            await update.message.reply_text(T[lang]["triage_pain_q4"], reply_markup=_kb_for_code(lang, "num")); return

    # Роутер LLM для обычного текста (коротко + один вопрос)
    prof = profiles_get(uid)
    data = llm_router_answer(text, lang, prof)
    prefix = personalized_prefix(lang, prof, uid)
    reply = ((prefix + "\n") if prefix else "") + (data.get("assistant_reply") or T[lang]["unknown"])
    await update.message.reply_text(reply, reply_markup=inline_actions(lang))
    # один короткий follow-up, если есть
    for one in (data.get("followups") or [])[:1]:
        await send_unique(update.message, uid, one, force=True)
    # запрос фидбека не чаще 1/день
    if can_ask_feedback(uid):
        try:
            await update.message.reply_text(T[lang]["ask_fb"], reply_markup=inline_feedback_kb(lang))
            mark_feedback_asked(uid)
        except Exception:
            pass
    return

# ===== Callback handler =====
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = (q.data or ""); uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    chat_id = q.message.chat.id

    if data.startswith("gate:"):
        await gate_cb(update, context); return

    if data.startswith("p|"):
        _, action, key, *rest = data.split("|")
        s = sessions.setdefault(uid, {"profile_active": True, "p_step": 0})
        if action == "choose":
            value = "|".join(rest) if rest else ""
            s[key] = value; profiles_upsert(uid, {key: value})
            await advance_profile_ctx(context, chat_id, lang, uid); return
        if action == "write":
            s["p_wait_key"] = key
            await q.message.reply_text({"ru":"Напишите короткий ответ:","uk":"Напишіть коротко:",
                                        "en":"Type your answer:","es":"Escribe tu respuesta:"}[lang]); return
        if action == "skip":
            profiles_upsert(uid, {key: ""})
            await advance_profile_ctx(context, chat_id, lang, uid); return

    if data.startswith("consent|"):
        users_set(uid, "consent", "yes" if data.endswith("|yes") else "no")
        try: await q.edit_message_reply_markup(reply_markup=None)
        except: pass
        await q.message.reply_text(T[lang]["thanks"]); return

    if data.startswith("mood|"):
        mood = data.split("|",1)[1]
        if mood=="note":
            sessions.setdefault(uid,{})["awaiting_daily_comment"] = True
            await q.message.reply_text({"ru":"Короткий комментарий:","uk":"Короткий коментар:",
                                        "en":"Short note:","es":"Nota corta:"}[lang]); return
        daily_add(iso(utcnow()), uid, mood, ""); await q.message.reply_text(T[lang]["mood_thanks"]); return

    if data.startswith("topic|"):
        topic = data.split("|",1)[1]
        if topic=="profile":
            await start_profile_ctx(context, chat_id, lang, uid); return
        if topic=="pain":
            sessions[uid] = {"topic":"pain","step":1,"answers":{}}
            kb = _kb_for_code(lang, "painloc")
            await q.message.reply_text(T[lang]["triage_pain_q1"], reply_markup=kb); return
        last = sessions.get(uid,{}).get("last_user_text","")
        prof = profiles_get(uid)
        prompt = f"topic:{topic}\nlast_user: {last or '—'}"
        data_llm = llm_router_answer(prompt, lang, prof)
        prefix = personalized_prefix(lang, prof, uid)
        reply = ((prefix + "\n") if prefix else "") + (data_llm.get("assistant_reply") or T[lang]["unknown"])
        await q.message.reply_text(reply, reply_markup=inline_actions(lang))
        for one in (data_llm.get("followups") or [])[:1]:
            await send_unique(q.message, uid, one, force=True)
        if can_ask_feedback(uid):
            try:
                await q.message.reply_text(T[lang]["ask_fb"], reply_markup=inline_feedback_kb(lang))
                mark_feedback_asked(uid)
            except Exception:
                pass
        return

    s = sessions.setdefault(uid, {})
    if data == "pain|exit":
        sessions.pop(uid, None)
        await q.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang)); return

    if data.startswith("painloc|"):
        s.update({"topic":"pain","step":2,"answers":{"loc": data.split("|",1)[1]}})
        await send_unique(q.message, uid, T[lang]["triage_pain_q2"], reply_markup=_kb_for_code(lang,"painkind")); return

    if data.startswith("painkind|"):
        s.setdefault("answers",{})["kind"] = data.split("|",1)[1]; s["step"]=3
        await send_unique(q.message, uid, T[lang]["triage_pain_q3"], reply_markup=_kb_for_code(lang,"paindur")); return

    if data.startswith("paindur|"):
        s.setdefault("answers",{})["duration"] = data.split("|",1)[1]; s["step"]=4
        await send_unique(q.message, uid, T[lang]["triage_pain_q4"], reply_markup=_kb_for_code(lang,"num")); return

    if data.startswith("num|"):
        if s.get("topic")=="pain" and s.get("step")==4:
            sev = int(data.split("|",1)[1])
            s.setdefault("answers",{})["severity"] = sev; s["step"]=5
            await send_unique(q.message, uid, T[lang]["triage_pain_q5"], reply_markup=_kb_for_code(lang,"painrf")); return

    if data.startswith("painrf|"):
        red = data.split("|",1)[1]
        s.setdefault("answers",{})["red"] = red
        sev = int(s["answers"].get("severity",5))
        eid = episode_create(uid, "pain", sev, red); s["episode_id"] = eid
        plan_lines = pain_plan(lang, [red], profiles_get(uid))
        prefix = personalized_prefix(lang, profiles_get(uid), uid)
        text_plan = (prefix + "\n" if prefix else "") + f"{T[lang]['plan_header']}\n" + "\n".join(plan_lines)
        await q.message.reply_text(text_plan)
        await q.message.reply_text(T[lang]["plan_accept"], reply_markup=inline_accept(lang))
        if can_ask_feedback(uid):
            try:
                await q.message.reply_text(T[lang]["ask_fb"], reply_markup=inline_feedback_kb(lang))
                mark_feedback_asked(uid)
            except Exception:
                pass
        s["step"] = 6; return

    if data.startswith("acc|"):
        accepted = "1" if data.endswith("|yes") else "0"
        if s.get("episode_id"): episode_set(s["episode_id"], "plan_accepted", accepted)
        await q.message.reply_text(T[lang]["remind_when"], reply_markup=inline_remind(lang))
        s["step"] = 7; return

    if data.startswith("rem|"):
        choice = data.split("|",1)[1]
        hours = {"4h":4, "evening":6, "morning":16}.get(choice)
        if hours and s.get("episode_id"):
            next_time = utcnow() + timedelta(hours=hours)
            episode_set(s["episode_id"], "next_checkin_at", iso(next_time))
            if _has_jq_ctx(context):
                context.application.job_queue.run_once(job_checkin_episode, when=hours*3600,
                                                       data={"user_id":uid,"episode_id":s["episode_id"]})
            else:
                logging.warning("JobQueue not available – episode follow-up not scheduled.")
        await q.message.reply_text(T[lang]["thanks"], reply_markup=inline_topic_kb(lang))
        sessions.pop(uid, None); return

    if data.startswith("act|"):
        parts = data.split("|"); kind = parts[1]
        if kind=="h60":
            sessions.setdefault(uid,{})["awaiting_h60"] = True
            await q.message.reply_text(T[lang]["h60_intro"]); return
        if kind=="rem":
            key = parts[2]; hours = {"4h":4, "evening":6, "morning":16}.get(key,4)
            when_ = utcnow() + timedelta(hours=hours)
            rid = reminder_add(uid, T[lang]["thanks"], when_)
            prefs_upsert(uid, {"reminder_preset": key})
            if _has_jq_ctx(context):
                context.application.job_queue.run_once(job_oneoff_reminder, when=hours*3600,
                                                       data={"user_id":uid,"reminder_id":rid})
            else:
                logging.warning("JobQueue not available – one-off reminder not scheduled.")
            await q.message.reply_text(T[lang]["thanks"]); return
        if kind=="save":
            episode_create(uid, "general", 0, ""); await q.message.reply_text(T[lang]["act_saved"]); return
        if kind=="ex":
            txt = {"ru":"🧘 Шея 5 мин: 1) наклоны вперёд/назад ×5; 2) повороты ×5; 3) полукруги подбородком ×5; 4) растяжка трапеций 2×20с.",
                   "uk":"🧘 Шия 5 хв: 1) нахили вперед/назад ×5; 2) повороти ×5; 3) півкола підборіддям ×5; 4) розтяжка трапецій 2×20с.",
                   "en":"🧘 Neck 5 min: 1) flex/extend ×5; 2) rotations ×5; 3) chin semicircles ×5; 4) upper-trap stretch 2×20s.",
                   "es":"🧘 Cuello 5 min: 1) flex/extensión ×5; 2) giros ×5; 3) semicírculos ×5; 4) estiramiento trapecio sup. 2×20s."}[lang]
            await q.message.reply_text(txt); return
        if kind=="lab":
            sessions.setdefault(uid,{})["awaiting_city"] = True
            await q.message.reply_text(T[lang]["act_city_prompt"]); return
        if kind=="er":
            await q.message.reply_text(T[lang]["er_text"]); return

    if data.startswith("fb|"):
        sub = data.split("|",1)[1]
        if sub == "up":
            feedback_add(iso(utcnow()), uid, "feedback_yes", q.from_user.username, 1, "")
            await q.message.reply_text(T[lang]["fb_thanks"]); return
        if sub == "down":
            feedback_add(iso(utcnow()), uid, "feedback_no",  q.from_user.username, 0, "")
            await q.message.reply_text(T[lang]["fb_thanks"]); return
        if sub == "text":
            sessions.setdefault(uid,{})["awaiting_free_feedback"] = True
            await q.message.reply_text(T[lang]["fb_write"]); return

# ---------- Build & run ----------
GCLIENT = GSPREAD_CLIENT

def build_app() -> "Application":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    try:
        register_intake_pro(app, GCLIENT, on_complete_cb=_ipro_save_to_sheets_and_open_menu)
        logging.info("Intake Pro registered.")
    except Exception as e:
        logging.warning(f"Intake Pro registration failed: {e}")
    # Commands
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("privacy",      cmd_privacy))
    app.add_handler(CommandHandler("pause",        cmd_pause))
    app.add_handler(CommandHandler("resume",       cmd_resume))
    app.add_handler(CommandHandler("delete_data",  cmd_delete_data))
    app.add_handler(CommandHandler("profile",      cmd_profile))
    app.add_handler(CommandHandler("settz",        cmd_settz))
    app.add_handler(CommandHandler("checkin_on",   cmd_checkin_on))
    app.add_handler(CommandHandler("checkin_off",  cmd_checkin_off))
    app.add_handler(CommandHandler("health60",     cmd_health60))
    app.add_handler(CommandHandler("intake",       cmd_intake))
    # Youth
    app.add_handler(CommandHandler("energy",       cmd_energy))
    app.add_handler(CommandHandler("mood",         cmd_mood))
    app.add_handler(CommandHandler("water",        cmd_water))
    app.add_handler(CommandHandler("skin",         cmd_skin))
    # Lang toggles
    app.add_handler(CommandHandler("ru", lambda u,c: users_set(u.effective_user.id,"lang","ru") or u.message.reply_text("Ок, дальше отвечаю по-русски.")))
    app.add_handler(CommandHandler("en", lambda u,c: users_set(u.effective_user.id,"lang","en")  or u.message.reply_text("OK, I’ll reply in English.")))
    app.add_handler(CommandHandler("uk", lambda u,c: users_set(u.effective_user.id,"lang","uk")  or u.message.reply_text("Ок, надалі відповідатиму українською.")))
    app.add_handler(CommandHandler("es", lambda u,c: users_set(u.effective_user.id,"lang","es")  or u.message.reply_text("De acuerdo, responderé en español.")))
    # Gate & callbacks
    app.add_handler(CallbackQueryHandler(gate_cb, pattern=r"^gate:"))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(?!intake:)"))
    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        logging.error("TELEGRAM_TOKEN is not set")
        raise SystemExit(1)
    application = build_app()
    try:
        schedule_from_sheet_on_start(application)
    except Exception as e:
        logging.warning(f"Scheduling restore failed: {e}")
    logging.info("Starting TendAI bot polling…")
    application.run_polling()
