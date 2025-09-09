# -*- coding: utf-8 -*-
# TendAI main.py — обновлено: лимитер/тихие часы, утро+вечер, Youth-команды,
# безопасные headers для Sheets, Rules (evidence), мягкий фидбек, баннер профиля (1 раз),
# тёплый тон (мысль→вопрос), 3 пресета напоминаний, конкретные варианты,
# АВТО-ПРЕДЛОЖЕНИЕ ОПРОСНИКА С ПЕРВОГО СООБЩЕНИЯ + шаги height_cm/weight_kg/supplements

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
        "welcome": "Hi! I’m TendAI — your health & longevity assistant.\nDescribe what’s bothering you; I’ll guide you. Let’s do a quick 40s intake to tailor advice.",
        "help": "Short checkups, 24–48h plans, reminders, daily check-ins.\nCommands: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /energy /mood /water /skin /ru /uk /en /es",
        "privacy": "TendAI is not a medical service and can’t replace a doctor. We store minimal data for reminders. /delete_data to erase.",
        "paused_on": "Notifications paused. Use /resume to enable.",
        "paused_off": "Notifications resumed.",
        "deleted": "All your data was deleted. Use /start to begin again.",
        "ask_consent": "May I send you a follow-up to check how you feel later?",
        "yes":"Yes","no":"No",
        "unknown":"I need a bit more info: where exactly and for how long?",
        "profile_intro":"Quick intake (~40s). Use buttons or type your answer.",
        "p_step_1":"Step 1/10. Sex:",
        "p_step_2":"Step 2/10. Age:",
        "p_step_3":"Step 3/10. Height (cm):",
        "p_step_4":"Step 4/10. Weight (kg):",
        "p_step_5":"Step 5/10. Main goal:",
        "p_step_6":"Step 6/10. Chronic conditions:",
        "p_step_7":"Step 7/10. Meds:",
        "p_step_8":"Step 8/10. Supplements:",
        "p_step_9":"Step 9/10. Sleep (bed/wake, e.g., 23:30/07:00):",
        "p_step_10":"Step 10/10. Activity:",
        "write":"✍️ Write",
        "skip":"⏭️ Skip",
        "saved_profile":"Saved: ",
        "start_where":"Where do you want to start now? (symptom/sleep/nutrition/labs/habits/longevity)",
        "daily_gm":"Good morning! Quick daily check-in:",
        "daily_pm":"Evening check-in: how was your day?",
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
        "remind_opts":["in 4h","this evening","tomorrow morning","no need"],
        "thanks":"Got it 🙌",
        "checkin_ping":"Quick check-in: how is it now (0–10)?",
        "checkin_better":"Nice! Keep it up 💪",
        "checkin_worse":"Sorry to hear. If any red flags or pain ≥7/10 — consider medical help.",
        "act_rem_2h":"⏰ Remind in 2h",
        "act_rem_4h":"⏰ Remind in 4h",
        "act_rem_eve":"⏰ This evening",
        "act_rem_morn":"⏰ Tomorrow morning",
        "act_save_episode":"💾 Save as episode",
        "act_ex_neck":"🧘 5-min neck routine",
        "act_find_lab":"🧪 Find a lab",
        "act_er":"🚑 Emergency info",
        "act_city_prompt":"Type your city/area so I can suggest a lab (text only).",
        "act_saved":"Saved.",
        "er_text":"If symptoms worsen, severe shortness of breath, chest pain, confusion, or persistent high fever — seek urgent care/emergency.",
        "px":"Considering your profile: {sex}, {age}y; goal — {goal}.",
        "back":"◀ Back",
        "exit":"Exit",
        "ask_fb":"Was this helpful?",
        "fb_thanks":"Thanks for your feedback! ✅",
        "fb_write":"Write a short feedback message:",
        "fb_good":"👍 Like",
        "fb_bad":"👎 Dislike",
        "fb_free":"📝 Feedback",
        "h60_btn": "Health in 60 seconds",
        "h60_intro": "Write briefly what bothers you (e.g., “headache”, “fatigue”, “stomach pain”). I’ll give you 3 key tips in 60 seconds.",
        "h60_t1": "Possible causes",
        "h60_t2": "Do now (next 24–48h)",
        "h60_t3": "When to see a doctor",
        "h60_serious": "Serious to rule out",
        # Youth quick labels
        "energy_title": "Energy for today:",
        "water_prompt": "Drink 300–500 ml of water. Remind in 2 hours?",
        "skin_title": "Skin/Body tip:"
    },
    "ru": {
        "welcome":"Привет! Я TendAI — ассистент здоровья и долголетия.\nРасскажи, что беспокоит; я подскажу. Сначала короткий опрос (~40с), чтобы советы были точнее.",
        "help":"Короткие проверки, план на 24–48 ч, напоминания, ежедневные чек-ины.\nКоманды: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +3 /health60 /energy /mood /water /skin /ru /uk /en /es",
        "privacy":"TendAI не заменяет врача. Храним минимум данных для напоминаний. /delete_data — удалить.",
        "paused_on":"Напоминания поставлены на паузу. /resume — включить.",
        "paused_off":"Напоминания снова включены.",
        "deleted":"Все данные удалены. /start — начать заново.",
        "ask_consent":"Можно прислать напоминание позже, чтобы узнать, как вы?",
        "yes":"Да","no":"Нет",
        "unknown":"Нужно чуть больше деталей: где именно и сколько длится?",
        "profile_intro":"Быстрый опрос (~40с). Можно нажимать кнопки или писать свой ответ.",
        "p_step_1":"Шаг 1/10. Пол:",
        "p_step_2":"Шаг 2/10. Возраст:",
        "p_step_3":"Шаг 3/10. Рост (см):",
        "p_step_4":"Шаг 4/10. Вес (кг):",
        "p_step_5":"Шаг 5/10. Главная цель:",
        "p_step_6":"Шаг 6/10. Хронические болезни:",
        "p_step_7":"Шаг 7/10. Лекарства:",
        "p_step_8":"Шаг 8/10. Добавки:",
        "p_step_9":"Шаг 9/10. Сон (отбой/подъём, напр. 23:30/07:00):",
        "p_step_10":"Шаг 10/10. Активность:",
        "write":"✍️ Написать",
        "skip":"⏭️ Пропустить",
        "saved_profile":"Сохранил: ",
        "start_where":"С чего начнём? (симптом/сон/питание/анализы/привычки/долголетие)",
        "daily_gm":"Доброе утро! Быстрый чек-ин:",
        "daily_pm":"Вечерний чек-ин: как прошёл день?",
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
        "remind_opts":["через 4 часа","вечером","завтра утром","не надо"],
        "thanks":"Принято 🙌",
        "checkin_ping":"Коротко: как сейчас по шкале 0–10?",
        "checkin_better":"Отлично! Продолжаем 💪",
        "checkin_worse":"Если есть «красные флаги» или боль ≥7/10 — лучше обратиться к врачу.",
        "act_rem_2h":"⏰ Напомнить через 2 ч",
        "act_rem_4h":"⏰ Напомнить через 4 ч",
        "act_rem_eve":"⏰ Сегодня вечером",
        "act_rem_morn":"⏰ Завтра утром",
        "act_save_episode":"💾 Сохранить эпизод",
        "act_ex_neck":"🧘 5-мин упражнения для шеи",
        "act_find_lab":"🧪 Найти лабораторию",
        "act_er":"🚑 Когда срочно в скорую",
        "act_city_prompt":"Напишите город/район, чтобы подсказать лабораторию (текстом).",
        "act_saved":"Сохранено.",
        "er_text":"Если нарастает, сильная одышка, боль в груди, спутанность, стойкая высокая температура — как можно скорее к неотложке/скорой.",
        "px":"С учётом профиля: {sex}, {age} лет; цель — {goal}.",
        "back":"◀ Назад",
        "exit":"Выйти",
        "ask_fb":"Это было полезно?",
        "fb_thanks":"Спасибо за отзыв! ✅",
        "fb_write":"Напишите короткий отзыв одним сообщением:",
        "fb_good":"👍 Нравится",
        "fb_bad":"👎 Не полезно",
        "fb_free":"📝 Отзыв",
        "h60_btn": "Здоровье за 60 секунд",
        "h60_intro": "Коротко напишите, что беспокоит (например: «болит голова», «усталость», «боль в животе»). Я дам 3 ключевых совета за 60 секунд.",
        "h60_t1": "Возможные причины",
        "h60_t2": "Что сделать сейчас (24–48 ч)",
        "h60_t3": "Когда обратиться к врачу",
        "h60_serious": "Что серьёзное исключить",
        "energy_title": "Энергия на сегодня:",
        "water_prompt": "Выпей 300–500 мл воды. Напомнить через 2 часа?",
        "skin_title": "Совет для кожи/тела:"
    }
}
# Наследуем uk от ru и переопределяем отличия
T["uk"] = {**T["ru"], **{
    "help": "Короткі перевірки, план на 24–48 год, нагадування, щоденні чек-іни.\nКоманди: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /energy /mood /water /skin /ru /uk /en /es",
    "daily_pm":"Вечірній чек-ін: як пройшов день?",
    "act_rem_2h": "⏰ Нагадати через 2 год",
    "energy_title": "Енергія на сьогодні:",
    "water_prompt": "Випий 300–500 мл води. Нагадати через 2 години?",
    "skin_title": "Догляд за шкірою/тілом:"
}}
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
    # показать баннер в следующий ответ
    users_set(uid, "profile_banner_shown", "no")
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

# === Canonical headers + safe reader ===
USERS_HEADERS = [
    "user_id","username","lang","consent","tz_offset","checkin_hour","paused",
    "quiet_hours","last_sent_utc","sent_today","streak","challenge_id","challenge_day",
    # мягкий фидбек и баннер профиля
    "last_fb_asked","profile_banner_shown"
]
PROFILES_HEADERS = ["user_id","sex","age","goal","conditions","meds","allergies","sleep","activity","diet","notes","updated_at","goals","diet_focus","steps_target","cycle_enabled","cycle_last_date","cycle_avg_len",
                    # новые поля
                    "height_cm","weight_kg","supplements"]
EPISODES_HEADERS = ["episode_id","user_id","topic","started_at","baseline_severity","red_flags","plan_accepted","target","reminder_at","next_checkin_at","status","last_update","notes"]
REMINDERS_HEADERS = ["id","user_id","text","when_utc","created_at","status"]
DAILY_HEADERS = ["timestamp","user_id","mood","comment"]
FEEDBACK_HEADERS = ["timestamp","user_id","name","username","rating","comment"]
RULES_HEADERS = ["rule_id","domain","segment","lang","text","citations"]

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
    global SHEETS_ENABLED, ss, ws_feedback, ws_users, ws_profiles, ws_episodes, ws_reminders, ws_daily, ws_rules
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
                ws = ss.add_worksheet(title=title, rows=1000, cols=max(20, len(headers)))
                ws.append_row(headers)
            # выравниваем заголовки при расхождении
            vals = ws.get_all_values()
            if not vals:
                ws.append_row(headers)
            else:
                head = vals[0]
                if len(head) < len(headers):
                    pad = headers[len(head):]
                    ws.update(range_name=f"{gsu.rowcol_to_a1(1,len(head)+1)}:{gsu.rowcol_to_a1(1,len(headers))}",
                              values=[pad])
            return ws

        ws_feedback = _ensure_ws("Feedback", FEEDBACK_HEADERS)
        ws_users    = _ensure_ws("Users", USERS_HEADERS)
        ws_profiles = _ensure_ws("Profiles", PROFILES_HEADERS)
        ws_episodes = _ensure_ws("Episodes", EPISODES_HEADERS)
        ws_reminders= _ensure_ws("Reminders", REMINDERS_HEADERS)
        ws_daily    = _ensure_ws("DailyCheckins", DAILY_HEADERS)
        ws_rules    = _ensure_ws("Rules", RULES_HEADERS)
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

# --------- Sessions ----------
sessions: Dict[int, dict] = {}

# -------- Sheets wrappers --------
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
    base = {
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
        "last_fb_asked": "",
        "profile_banner_shown": "no"
    }
    if SHEETS_ENABLED:
        vals = ws_records(ws_users, USERS_HEADERS)
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                end_col = gsu.rowcol_to_a1(1, len(USERS_HEADERS)).rstrip("1")
                ws_users.update(range_name=f"A{i}:{end_col}{i}", values=[[base.get(h,"") for h in USERS_HEADERS]])
                return
        ws_users.append_row([base.get(h,"") for h in USERS_HEADERS])
    else:
        MEM_USERS[uid] = base

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
        if not current:
            current = {"user_id": str(uid)}
        for k,v in data.items():
            current[k] = "" if v is None else (", ".join(v) if isinstance(v,list) else str(v))
        current["updated_at"] = iso(utcnow())
        values = [current.get(h,"") for h in hdr]
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
        # вечерний (отдельный текст)
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

# === Вечер: отдельный джоб и планировщик только для вечера ===
def schedule_morning_evening(app, uid:int, tz_off:int, lang:str, evening="20:30"):
    if not _has_jq_app(app): return
    for j in app.job_queue.get_jobs_by_name(f"daily_e_{uid}"):
        j.schedule_removal()
    h_e, m_e = hhmm_tuple(evening); h_e = (h_e - tz_off) % 24
    app.job_queue.run_daily(
        job_evening_checkin,
        dtime(hour=h_e, minute=m_e, tzinfo=timezone.utc),
        name=f"daily_e_{uid}",
        data={"user_id":uid,"lang":lang}
    )

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

# ===== LLM Router =====
SYS_ROUTER = (
    "You are TendAI — a concise, warm, professional health & longevity assistant (not a doctor). "
    "Always answer strictly in {lang}. Keep replies short (<=6 lines + up to 4 bullets). "
    "Personalize using the provided profile (sex/age/goal/conditions). "
    "TRIAGE: ask 1–2 clarifiers first; advise ER only for clear red flags. "
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
        return data
    except Exception as e:
        logging.error(f"router LLM error: {e}")
        return {"intent":"other","assistant_reply":T[lang]["unknown"],"followups":[],"needs_more":True,"red_flags":False,"confidence":0.3}

# ===== Rules-based подсказки =====
def rules_match(seg: str, prof: dict) -> bool:
    if not seg:
        return True
    for part in seg.split("&"):
        m = re.match(r'(\w+)\s*(>=|<=|=|>|<)\s*([\w\-]+)', part.strip())
        if not m:
            return False
        k, op, v = m.groups()
        pv = (prof.get(k) or prof.get(k.lower()) or "")
        if k in ("age", "steps_target", "cycle_avg_len","height_cm","weight_kg"):
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

# ===== Мини-логика цикла =====
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
            "ru":"Фаза менструации: мягче к себе, железо/белок, сон приоритет.",
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

async def job_daily_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, lang = d.get("user_id"), d.get("lang","en")
    u = users_get(uid)
    if (u.get("paused") or "").lower()=="yes":
        return
    # приветствие + настроение (утро)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_good"], callback_data="mood|good"),
         InlineKeyboardButton(T[lang]["mood_ok"], callback_data="mood|ok"),
         InlineKeyboardButton(T[lang]["mood_bad"], callback_data="mood|bad")],
        [InlineKeyboardButton(T[lang]["mood_note"], callback_data="mood|note")]
    ])
    await maybe_send(context, uid, T[lang]["daily_gm"], kb)

    # 1–2 совета по питанию из Rules
    prof = profiles_get(uid)
    tips = pick_nutrition_tips(lang, prof, limit=2)
    if tips:
        await maybe_send(context, uid, "• " + "\n• ".join(tips))

    # деликатный совет по фазе цикла (если включено)
    phase = cycle_phase_for(uid)
    if phase:
        tip = cycle_tip(lang, phase)
        if tip:
            await maybe_send(context, uid, tip)

# Новый вечерний джоб — другой текст
async def job_evening_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, lang = d.get("user_id"), d.get("lang","en")
    u = users_get(uid)
    if (u.get("paused") or "").lower()=="yes":
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_good"], callback_data="mood|good"),
         InlineKeyboardButton(T[lang]["mood_ok"],   callback_data="mood|ok"),
         InlineKeyboardButton(T[lang]["mood_bad"],  callback_data="mood|bad")],
        [InlineKeyboardButton(T[lang]["mood_note"], callback_data="mood|note")]
    ])
    await maybe_send(context, uid, T[lang]["daily_pm"], kb)

# ===== Serious keywords =====
SERIOUS_KWS = {
    "diabetes":["diabetes","диабет","сахарный","цукров","глюкоза","hba1c","гликированный"],
    "hepatitis":["hepatitis","гепатит","печень hbs","hcv","alt","ast"],
    "cancer":["cancer","рак","онко","онколог","опухол","пухлина","tumor"],
    "tb":["tuberculosis","tb","туберкул","туберкульоз"],
}

def detect_serious(text: str) -> Optional[str]:
    low = (text or "").lower()
    for cond, kws in SERIOUS_KWS.items():
        if any(k in low for k in kws):
            return cond
    return None

# ===== Персонализированный префикс/баннер (показывать 1 раз) =====
def _ru_age_phrase(age_str: str) -> str:
    try:
        n = int(re.search(r"\d+", age_str).group())
    except Exception:
        return age_str
    last2 = n % 100
    last1 = n % 10
    if 11 <= last2 <= 14:
        word = "лет"
    elif last1 == 1:
        word = "год"
    elif 2 <= last1 <= 4:
        word = "года"
    else:
        word = "лет"
    return f"{n} {word}"

def profile_banner(lang: str, profile: dict) -> str:
    sex = str(profile.get("sex") or "").strip().lower()
    age_raw = str(profile.get("age") or "").strip()
    goal = (profile.get("goal") or profile.get("goals") or "").strip()
    ht = (profile.get("height_cm") or "").strip()
    wt = (profile.get("weight_kg") or "").strip()
    if lang == "ru":
        sex_ru = {"male":"мужчина","female":"женщина","other":"человек"}.get(sex, "человек")
        age_ru = _ru_age_phrase(age_raw or "—")
        goal_ru = {"longevity":"долголетие","energy":"энергия","sleep":"сон","weight":"похудение","strength":"сила"}.get(goal, goal or "—")
        hw = f", {ht}см/{wt}кг" if (ht or wt) else ""
        return f"{sex_ru}, {age_ru}{hw}; цель — {goal_ru}"
    if lang == "uk":
        hw = f", {ht}см/{wt}кг" if (ht or wt) else ""
        return f"{sex or '—'}, {age_raw or '—'}{hw}; ціль — {goal or '—'}"
    if lang == "es":
        hw = f", {ht}cm/{wt}kg" if (ht or wt) else ""
        return f"{sex or '—'}, {age_raw or '—'}{hw}; objetivo — {goal or '—'}"
    hw = f", {ht}cm/{wt}kg" if (ht or wt) else ""
    return f"{sex or '—'}, {age_raw or '—'}y{hw}; goal — {goal or '—'}"

def should_show_profile_banner(uid: int) -> bool:
    u = users_get(uid)
    return (u.get("profile_banner_shown") or "no") != "yes"

def apply_warm_tone(text: str, lang: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", (text or "").strip())

def ask_feedback_soft(uid: int, context: ContextTypes.DEFAULT_TYPE, lang: str):
    try:
        u = users_get(uid)
        last = (u.get("last_fb_asked") or "").strip()
        today = (utcnow() + timedelta(hours=int(str(u.get("tz_offset") or "0")))).date().isoformat()
        if last == today:
            return
        kb = inline_feedback_kb(lang)
        context.application.create_task(context.bot.send_message(uid, T[lang]["ask_fb"], reply_markup=kb))
        users_set(uid, "last_fb_asked", today)
    except Exception as e:
        logging.warning(f"ask_feedback_soft error: {e}")

# ===== Планы и кнопки =====
def pain_plan(lang: str, red_flags_selected: List[str], profile: dict) -> List[str]:
    flg = [s for s in red_flags_selected if s and str(s).lower() not in ["none","нет","немає","ninguno","no"]]
    if flg:
        return {"ru":["⚠️ Есть тревожные признаки. Лучше как можно скорее показаться врачу/в скорую."],
                "uk":["⚠️ Є тривожні ознаки. Варто якнайшвидше звернутися до лікаря/швидкої."],
                "en":["⚠️ Red flags present. Please seek urgent medical evaluation."],
                "es":["⚠️ Señales de alarma presentes. Busca evaluación médica urgente."]}[lang]
    age_num = 0
    try:
        age_num = int(re.search(r"\d+", str(profile.get("age") or "")).group(0))
    except Exception:
        age_num = 0
    extra = []
    if age_num >= 60:
        extra.append({"ru":"Вам 60+, будьте осторожны с НПВП; пейте воду и при ухудшении обратитесь к врачу.",
                      "uk":"Вам 60+, обережно з НПЗЗ; пийте воду, за погіршення — до лікаря.",
                      "en":"Age 60+: be careful with NSAIDs; hydrate and seek care if worsening.",
                      "es":"Edad 60+: cuidado con AINEs; hidrátate y busca atención si empeora."}[lang])
    core = {"ru":["1) Вода 400–600 мл и 15–20 мин тишины/отдыха.",
                  "2) Если нет противопоказаний — ибупрофен 200–400 мг однократно с едой.",
                  "3) Проветрить, уменьшить экран на 30–60 мин.","Цель: к вечеру боль ≤3/10."],
            "uk":["1) Вода 400–600 мл і 15–20 хв спокою.",
                  "2) Якщо нема протипоказань — ібупрофен 200–400 мг одноразово з їжею.",
                  "3) Провітрити, менше екрану 30–60 хв.","Мета: до вечора біль ≤3/10."],
            "en":["1) Drink 400–600 ml water; rest 15–20 min.",
                  "2) If no contraindications — ibuprofen 200–400 mg once with food.",
                  "3) Air the room; reduce screen time 30–60 min.","Goal: by evening pain ≤3/10."],
            "es":["1) Bebe 400–600 ml de agua; descansa 15–20 min.",
                  "2) Si no hay contraindicaciones — ibuprofeno 200–400 mg una vez con comida.",
                  "3) Ventila la habitación; reduce pantallas 30–60 min.","Meta: por la tarde dolor ≤3/10."]}[lang]
    return core + extra + [T[lang]["er_text"]]

# ===== Клавиатуры (6–7 кнопок/экран, 3 пресета напоминаний) =====
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏰ +4h" if lang=="en" else T[lang]["act_rem_4h"], callback_data="rem|4h"),
         InlineKeyboardButton("⏰ This evening" if lang=="en" else T[lang]["act_rem_eve"], callback_data="rem|evening"),
         InlineKeyboardButton("⏰ Tomorrow morning" if lang=="en" else T[lang]["act_rem_morn"], callback_data="rem|morning")]
    ])

def inline_feedback_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["fb_good"], callback_data="fb|up"),
         InlineKeyboardButton(T[lang]["fb_bad"],  callback_data="fb|down")],
        [InlineKeyboardButton(T[lang]["fb_free"], callback_data="fb|text")]
    ])

def inline_actions(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏰ +4h" if lang=="en" else T[lang]["act_rem_4h"],  callback_data="act|rem|4h"),
         InlineKeyboardButton("⏰ This evening" if lang=="en" else T[lang]["act_rem_eve"],  callback_data="act|rem|evening"),
         InlineKeyboardButton("⏰ Tomorrow morning" if lang=="en" else T[lang]["act_rem_morn"], callback_data="act|rem|morning")],
        [InlineKeyboardButton(T[lang]["h60_btn"], callback_data="act|h60")],
        [InlineKeyboardButton(T[lang]["act_ex_neck"], callback_data="act|ex|neck")],
        [InlineKeyboardButton(T[lang]["act_find_lab"], callback_data="act|lab")],
        [InlineKeyboardButton(T[lang]["act_er"], callback_data="act|er")]
    ])

# ===== Youth-пакет: команды =====
async def cmd_energy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    tips = {
      "en": [
        "1) 10-min brisk walk now (raise pulse).",
        "2) 300–500 ml water + light protein.",
        "3) 20-min screen detox to refresh focus."
      ],
      "ru": [
        "1) Быстрая ходьба 10 мин (пульс чуть выше обычного).",
        "2) 300–500 мл воды + лёгкий белок.",
        "3) 20 мин без экрана — разгрузка внимания."
      ],
      "uk": [
        "1) Швидка ходьба 10 хв (пульс трохи вище).",
        "2) 300–500 мл води + легкий білок.",
        "3) 20 хв без екрана — перезавантаження уваги."
      ],
      "es": [
        "1) Camina rápido 10 min.",
        "2) 300–500 ml de agua + proteína ligera.",
        "3) 20 min sin pantallas."
      ]
    }[lang]
    await update.message.reply_text(T[lang]["energy_title"] + "\n" + "\n".join(tips), reply_markup=inline_actions(lang))

async def cmd_water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏰ +4h" if lang=="en" else T[lang]["act_rem_4h"], callback_data="act|rem|4h")]])
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
        "en":"Wash face 2×/day with lukewarm water, SPF in the morning, 1% niacinamide at night.",
        "uk":"Вмивання 2×/день теплою водою, SPF вранці, 1% ніацинамід ввечері.",
        "es":"Lava el rostro 2×/día con agua tibia, SPF por la mañana, 1% niacinamida por la noche."
    }[lang]
    await update.message.reply_text(T[lang]["skin_title"] + "\n" + tip, reply_markup=inline_actions(lang))

# ===== ДОБАВЛЕНО: дружелюбный хендлер приветствий (фикс regex флагов) =====
async def on_hello(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = norm_lang(getattr(user, "language_code", None) or "en")
    msg = {
        "ru": "Привет! 👋 Я здесь ради твоего здоровья. Можем начать, когда будешь готов(а). Хочешь короткий чек-ин за 60 секунд?",
        "uk": "Привіт! 👋 Я поруч для твого здоров'я. Почнемо, коли будеш готов(а). Хочеш короткий чек-ін за 60 секунд?",
        "en": "Hi! 👋 I’m here for your health journey. We can start whenever you’re ready. Want a 60-second daily check-in?",
        "es": "¡Hola! 👋 Estoy aquí para tu salud. Empezamos cuando quieras. ¿Un check-in de 60 segundos?"
    }[lang]
    await update.message.reply_text(msg)

# ===== Pain triage вспомогательные =====
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

# ====== Health60 =====
async def cmd_health60(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None))
    sessions.setdefault(uid, {})["awaiting_h60"] = True
    await update.message.reply_text(T[lang]["h60_intro"])

# ===== /intake кнопка =====
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

# ===== Profile (расширено до 10 шагов) =====
PROFILE_STEPS = [
    {"key":"sex","opts":{"ru":[("Мужчина","male"),("Женщина","female"),("Другое","other")],
                         "en":[("Male","male"),("Female","female"),("Other","other")],
                         "uk":[("Чоловіча","male"),("Жіноча","female"),("Інша","other")],
                         "es":[("Hombre","male"),("Mujer","female"),("Otro","other")]}}
    ,
    {"key":"age","opts":{"ru":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
                         "en":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
                         "uk":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
                         "es":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")]}}
    ,
    {"key":"height_cm","opts":{"ru":[("160","160"),("170","170"),("180","180")],
                               "en":[("160","160"),("170","170"),("180","180")],
                               "uk":[("160","160"),("170","170"),("180","180")],
                               "es":[("160","160"),("170","170"),("180","180")]}}
    ,
    {"key":"weight_kg","opts":{"ru":[("60","60"),("75","75"),("90","90")],
                               "en":[("60","60"),("75","75"),("90","90")],
                               "uk":[("60","60"),("75","75"),("90","90")],
                               "es":[("60","60"),("75","75"),("90","90")]}}
    ,
    {"key":"goal","opts":{"ru":[("Похудение","weight"),("Энергия","energy"),("Сон","sleep"),("Долголетие","longevity"),("Сила","strength")],
                          "en":[("Weight","weight"),("Energy","energy"),("Sleep","sleep"),("Longevity","longevity"),("Strength","strength")],
                          "uk":[("Вага","weight"),("Енергія","energy"),("Сон","sleep"),("Довголіття","longevity"),("Сила","strength")],
                          "es":[("Peso","weight"),("Energía","energy"),("Sueño","sleep"),("Longevidad","longevity"),("Fuerza","strength")]}}
    ,
    {"key":"conditions","opts":{"ru":[("Нет","none"),("Гипертония","hypertension"),("Диабет","diabetes"),("Щитовидка","thyroid"),("Другое","other")],
                               "en":[("None","none"),("Hypertension","hypertension"),("Diabetes","diabetes"),("Thyroid","thyroid"),("Other","other")],
                               "uk":[("Немає","none"),("Гіпертонія","hypertension"),("Діабет","diabetes"),("Щитоподібна","thyroid"),("Інше","other")],
                               "es":[("Ninguna","none"),("Hipertensión","hypertension"),("Diabetes","diabetes"),("Tiroides","thyroid"),("Otra","other")]}}
    ,
    {"key":"meds","opts":{"ru":[("Нет","none"),("Магний","magnesium"),("Витамин D","vitd"),("Аллергии есть","allergies"),("Другое","other")],
                          "en":[("None","none"),("Magnesium","magnesium"),("Vitamin D","vitd"),("Allergies","allergies"),("Other","other")],
                          "uk":[("Немає","none"),("Магній","magnesium"),("Вітамін D","vitd"),("Алергії","allergies"),("Інше","other")],
                          "es":[("Ninguno","none"),("Magnesio","magnesium"),("Vitamina D","vitd"),("Alergias","allergies"),("Otro","other")]}}
    ,
    {"key":"supplements","opts":{"ru":[("Нет","none"),("Омега-3","omega3"),("Креатин","creatine"),("Протеин","protein"),("Другое","other")],
                                "en":[("None","none"),("Omega-3","omega3"),("Creatine","creatine"),("Protein","protein"),("Other","other")],
                                "uk":[("Немає","none"),("Омега-3","omega3"),("Креатин","creatine"),("Протеїн","protein"),("Інше","other")],
                                "es":[("Ninguno","none"),("Omega-3","omega3"),("Ninguno","none"),("Omega-3","omega3"),("Creatina","creatine"),("Proteína","protein"),("Otro","other")]}
    }
]
PROFILE_TOTAL = len(PROFILE_STEPS)

# ===== /start, /help, /privacy, язык =====
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = update.effective_user
    lang = norm_lang(getattr(user, "language_code", None) or "en")
    # upsert user row (без записи согласия и т.п.)
    users_upsert(uid, user.username or "", lang)
    context.user_data["lang"] = lang

    # показать персональный баннер (1 раз) при наличии профиля
    prof = profiles_get(uid)
    if prof and should_show_profile_banner(uid):
        banner = profile_banner(lang, prof)
        try:
            await update.message.reply_text("👤 Your health summary\n" + banner)
        finally:
            users_set(uid, "profile_banner_shown", "yes")

    await update.message.reply_text(T[lang]["welcome"], reply_markup=inline_topic_kb(lang))

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang") or norm_lang(getattr(update.effective_user, "language_code", None) or "en")
    await update.message.reply_text(T[lang]["help"])

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang") or norm_lang(getattr(update.effective_user, "language_code", None) or "en")
    await update.message.reply_text(T[lang]["privacy"])

async def cmd_lang_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "ru")
    context.user_data["lang"] = "ru"
    await update.message.reply_text("Язык переключён на русский.")

async def cmd_lang_en(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "en")
    context.user_data["lang"] = "en"
    await update.message.reply_text("Language set to English.")

async def cmd_lang_uk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "uk")
    context.user_data["lang"] = "uk"
    await update.message.reply_text("Мову встановлено: українська.")

async def cmd_lang_es(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "es")
    context.user_data["lang"] = "es"
    await update.message.reply_text("Idioma cambiado a español.")

# ===== Пауза/резюм/удаление =====
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "yes")
    lang = context.user_data.get("lang","en")
    await update.message.reply_text(T[lang]["paused_on"])

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "no")
    lang = context.user_data.get("lang","en")
    await update.message.reply_text(T[lang]["paused_off"])

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # мягкое обнуление: очищаем в памяти/таблицах основные записи
    if SHEETS_ENABLED:
        # Небольшой «мягкий» сброс: помечаем paused и профайл
        users_set(uid, "paused", "yes")
        users_set(uid, "profile_banner_shown", "no")
    else:
        MEM_USERS.pop(uid, None)
        MEM_PROFILES.pop(uid, None)
        for arr in (MEM_EPISODES, MEM_REMINDERS, MEM_FEEDBACK, MEM_DAILY):
            to_del = [i for i,x in enumerate(arr) if str(x.get("user_id"))==str(uid)]
            for i in reversed(to_del): arr.pop(i)
    lang = context.user_data.get("lang","en")
    await update.message.reply_text(T[lang]["deleted"])

# ===== Настройка часового пояса и время чек-ина =====
async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    parts = (update.message.text or "").split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /settz +2  (integer hours offset)")
        return
    try:
        off = int(parts[1])
    except Exception:
        await update.message.reply_text("Use an integer offset, e.g., +2 or -5")
        return
    users_set(uid, "tz_offset", str(off))
    # Перепланируем
    if _has_jq_ctx(context):
        lang = norm_lang(users_get(uid).get("lang") or "en")
        schedule_daily_checkin(context.application, uid, off, users_get(uid).get("checkin_hour") or DEFAULT_CHECKIN_LOCAL, lang)
        schedule_morning_evening(context.application, uid, off, lang)
    await update.message.reply_text("Time zone updated.")

async def cmd_checkin_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    parts = (update.message.text or "").split()
    hhmm = parts[1] if len(parts) >= 2 else DEFAULT_CHECKIN_LOCAL
    users_set(uid, "checkin_hour", hhmm)
    users_set(uid, "paused", "no")
    if _has_jq_ctx(context):
        u = users_get(uid)
        tz = int(str(u.get("tz_offset") or "0"))
        lang = norm_lang(u.get("lang") or "en")
        schedule_daily_checkin(context.application, uid, tz, hhmm, lang)
        schedule_morning_evening(context.application, uid, tz, lang)
    await update.message.reply_text("Daily check-ins enabled.")

async def cmd_checkin_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "yes")
    await update.message.reply_text("Daily check-ins paused. Use /resume to enable.")

# ===== CallbackQuery роутер (укороченный, чтобы не менять основу) =====
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    lang = context.user_data.get("lang") or norm_lang(users_get(uid).get("lang") or "en")

    data = q.data or ""
    if data.startswith("topic|"):
        await q.message.edit_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
        return

    if data == "intake:start":
        # делегируем PRO-опросу, если подключён
        try:
            reg_cb = context.application.bot_data.get("intake_pro_cb")
            if callable(reg_cb):
                await reg_cb(update, context)
                return
        except Exception:
            pass
        await q.message.reply_text(T[lang]["profile_intro"])
        # мини-шаг 1 профиля
        await show_profile_step(update, context, 0)
        return

    if data.startswith("mood|"):
        mood = data.split("|",1)[1]
        daily_add(iso(utcnow()), uid, mood, "")
        await q.message.reply_text(T[lang]["mood_thanks"])
        ask_feedback_soft(uid, context, lang)
        return

    if data.startswith("fb|"):
        kind = data.split("|",1)[1]
        if kind == "up":
            feedback_add(iso(utcnow()), uid, update.effective_user.full_name, update.effective_user.username, "up", "")
            await q.message.reply_text(T[lang]["fb_thanks"])
        elif kind == "down":
            feedback_add(iso(utcnow()), uid, update.effective_user.full_name, update.effective_user.username, "down", "")
            await q.message.reply_text(T[lang]["fb_thanks"])
        elif kind == "text":
            sessions.setdefault(uid, {})["awaiting_fb"] = True
            await q.message.reply_text(T[lang]["fb_write"])
        return

    if data.startswith("act|"):
        _, kind, *rest = data.split("|")
        if kind == "rem":
            when = rest[0] if rest else "4h"
            dt = utcnow()
            if when == "4h": dt += timedelta(hours=4)
            elif when == "evening":
                # локальный «вечер» 20:30, переведём в UTC
                u = users_get(uid); off = int(str(u.get("tz_offset") or "0"))
                local_now = utcnow() + timedelta(hours=off)
                local_eve = local_now.replace(hour=20, minute=30, second=0, microsecond=0)
                if local_eve < local_now: local_eve += timedelta(days=1)
                dt = local_eve - timedelta(hours=off)
            elif when == "morning":
                u = users_get(uid); off = int(str(u.get("tz_offset") or "0"))
                local_now = utcnow() + timedelta(hours=off)
                local_mrn = local_now.replace(hour=8, minute=30, second=0, microsecond=0) + timedelta(days=1)
                dt = local_mrn - timedelta(hours=off)
            rid = reminder_add(uid, T[lang]["thanks"], dt)
            await q.message.reply_text(f"Reminder set ({rid}).")
        elif kind == "h60":
            await cmd_health60(update, context)
        elif kind == "ex" and rest and rest[0]=="neck":
            await q.message.reply_text("Neck mobility: slow circles ×5 each side; chin tucks ×10; scapular squeezes ×10.")
        elif kind == "lab":
            await q.message.reply_text(T[lang]["act_city_prompt"])
        elif kind == "er":
            await q.message.reply_text(T[lang]["er_text"])
        return

    if data.startswith("pain|") and data.endswith("|exit"):
        await q.message.reply_text("OK.")
        return

    # профиль — переключение шагов и сохранение
    if data.startswith("ps|"):
        await handle_profile_cb(update, context, data)
        return

# ===== mini-профиль UI =====
async def show_profile_step(update: Update, context: ContextTypes.DEFAULT_TYPE, step_idx: int):
    uid = update.effective_user.id
    lang = context.user_data.get("lang", "en")
    if step_idx >= PROFILE_TOTAL:
        await update.effective_chat.send_message(T[lang]["saved_profile"] + "OK")
        users_set(uid, "profile_banner_shown", "no")  # чтобы показать свежий баннер в следующий раз
        # разблокировать меню
        context.user_data[GATE_FLAG_KEY] = True
        render_cb = context.application.bot_data.get("render_menu_cb")
        if callable(render_cb):
            await render_cb(update, context)
        else:
            await update.effective_chat.send_message(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
        return

    step = PROFILE_STEPS[step_idx]
    key = step["key"]
    title = {
        "sex": ("p_step_1"),
        "age": ("p_step_2"),
        "height_cm": ("p_step_3"),
        "weight_kg": ("p_step_4"),
        "goal": ("p_step_5"),
        "conditions": ("p_step_6"),
        "meds": ("p_step_7"),
        "supplements": ("p_step_8"),
    }.get(key, "profile_intro")
    opts = step.get("opts", {}).get(lang, [])
    if opts:
        rows = []
        row = []
        for label, val in opts:
            row.append(InlineKeyboardButton(label, callback_data=f"ps|set|{key}|{val}|{step_idx}"))
            if len(row) == 3:
                rows.append(row); row = []
        if row: rows.append(row)
        rows.append([InlineKeyboardButton(T[lang]["skip"], callback_data=f"ps|skip|{step_idx}")])
        await update.effective_chat.send_message(T[lang][title], reply_markup=InlineKeyboardMarkup(rows))
    else:
        await update.effective_chat.send_message(T[lang][title])

async def handle_profile_cb(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    q = update.callback_query
    uid = update.effective_user.id
    lang = context.user_data.get("lang", "en")
    parts = data.split("|")
    if len(parts) < 3:
        return
    action = parts[1]
    if action == "set":
        _,_, key, value, idx = parts
        idx = int(idx)
        cur = profiles_get(uid) or {"user_id": str(uid)}
        cur[key] = value
        profiles_upsert(uid, cur)
        await q.edit_message_text(T[lang]["saved_profile"] + f"{key}={value}")
        await show_profile_step(update, context, idx+1)
    elif action == "skip":
        _,_, idx = parts
        idx = int(idx)
        await q.edit_message_text(T[lang]["skip"])
        await show_profile_step(update, context, idx+1)

# ===== on_text =====
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    lang = context.user_data.get("lang") or detect_lang_from_text(text, norm_lang(getattr(update.effective_user,"language_code",None) or "en"))

    # ожидаем свободный фидбек?
    if sessions.setdefault(uid, {}).pop("awaiting_fb", False):
        feedback_add(iso(utcnow()), uid, update.effective_user.full_name, update.effective_user.username, "free", text)
        await update.message.reply_text(T[lang]["fb_thanks"])
        return

    # Health60 короткий режим
    if sessions.setdefault(uid, {}).get("awaiting_h60"):
        sessions[uid]["awaiting_h60"] = False
        # 3 блока ответа (очень кратко)
        await update.message.reply_text(
            f"**{T[lang]['h60_t1']}**\n— 1–2 вероятные причины\n\n"
            f"**{T[lang]['h60_t2']}**\n— 2–3 действия сейчас\n\n"
            f"**{T[lang]['h60_t3']}**\n— когда обращаться",
        )
        ask_feedback_soft(uid, context, lang)
        return

    # обнаружение «серьёзных» ключевых слов
    serious = detect_serious(text)
    if serious:
        await update.message.reply_text(T[lang]["h60_serious"] + "\n" + T[lang]["er_text"])
        return

    # LLM роутер (коротко)
    prof = profiles_get(uid) or {}
    data = llm_router_answer(text, lang, prof)
    reply = apply_warm_tone(data.get("assistant_reply") or T[lang]["unknown"], lang)
    await update.message.reply_text(reply, reply_markup=inline_actions(lang))
    ask_feedback_soft(uid, context, lang)

# ===== build_app(): регистрация всех хендлеров =====
def build_app() -> "Application":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Дадим доступ intake_pro плагину зарегистрировать свои хендлеры, если есть
    def _on_intake_complete(update: Update, context: ContextTypes.DEFAULT_TYPE, profile: dict):
        # сохраняем профиль и открываем меню
        return context.application.create_task(_ipro_save_to_sheets_and_open_menu(update, context, profile))

    try:
        if HAVE_INTAKE_PRO:
            register_intake_pro(app, gclient=GSPREAD_CLIENT, on_complete_cb=_on_intake_complete)
            app.bot_data["intake_pro_cb"] = None  # сам плагин вешает свои коллбэки
    except Exception as e:
        logging.warning(f"register_intake_pro failed: {e}")

    # Команды
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("pause",   cmd_pause))
    app.add_handler(CommandHandler("resume",  cmd_resume))
    app.add_handler(CommandHandler("delete_data", cmd_delete))

    app.add_handler(CommandHandler("settz",       cmd_settz))
    app.add_handler(CommandHandler("checkin_on",  cmd_checkin_on))
    app.add_handler(CommandHandler("checkin_off", cmd_checkin_off))

    app.add_handler(CommandHandler("ru", cmd_lang_ru))
    app.add_handler(CommandHandler("en", cmd_lang_en))
    app.add_handler(CommandHandler("uk", cmd_lang_uk))
    app.add_handler(CommandHandler("es", cmd_lang_es))

    # Youth & быстродействующие режимы
    app.add_handler(CommandHandler("energy",   cmd_energy))
    app.add_handler(CommandHandler("water",    cmd_water))
    app.add_handler(CommandHandler("mood",     cmd_mood))
    app.add_handler(CommandHandler("skin",     cmd_skin))
    app.add_handler(CommandHandler("health60", cmd_health60))
    app.add_handler(CommandHandler("intake",   cmd_intake))

    # ===== Приветствия (исправленный regex-флаг): ДО общего Text-хендлера =====
    app.add_handler(
        MessageHandler(
            filters.Regex(re.compile(r"^(hi|hello|hey|hola|ola|salut|привет|здравствуй|здравствуйте)\b", re.IGNORECASE)),
            on_hello
        )
    )

    # CallbackQuery
    app.add_handler(CallbackQueryHandler(on_cb))

    # Общий текстовый хендлер (последним)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Восстановить расписания
    schedule_from_sheet_on_start(app)
    return app

# ===== main =====
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set")
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
