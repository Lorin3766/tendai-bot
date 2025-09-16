# -*- coding: utf-8 -*-
# TendAI main.py — обновлено: лимитер/тихие часы, утро+вечер, Youth-команды,
# безопасные headers для Sheets, Rules (evidence), мягкий фидбек, баннер профиля (1 раз),
# тёплый тон (мысль→вопрос), 3 пресета напоминаний, конкретные варианты,
# АВТО-ПРЕДЛОЖЕНИЕ ОПРОСНИКА С ПЕРВОГО СООБЩЕНИЯ + шаги height_cm/weight_kg/supplements
# + Главный блок меню, Smart check-in (3 тапа), Quick-log привычек (HabitsLog),
#   контекстные чипы (heartburn/neck), микро-планы, локальная навигация (NJ ссылки),
#   🌐 переключатель языка (в один тап).

import os, re, json, uuid, logging, random
from datetime import datetime, timedelta, timezone, time as dtime, date
from typing import List, Tuple, Dict, Optional, Any
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
# == NEW: лист для привычек (быстрый лог) ==
ws_habits = None

USERS_HEADERS = [
    "user_id","username","lang","consent","tz_offset","checkin_hour","paused",
    "quiet_hours","last_sent_utc","sent_today","streak","challenge_id","challenge_day",
    "last_fb_asked","profile_banner_shown"
]
PROFILES_HEADERS = ["user_id","sex","age","goal","conditions","meds","allergies","sleep","activity","diet","notes","updated_at","goals","diet_focus","steps_target","cycle_enabled","cycle_last_date","cycle_avg_len",
                    "height_cm","weight_kg","supplements"]
EPISODES_HEADERS = ["episode_id","user_id","topic","started_at","baseline_severity","red_flags","plan_accepted","target","reminder_at","next_checkin_at","status","last_update","notes"]
REMINDERS_HEADERS = ["id","user_id","text","when_utc","created_at","status"]
DAILY_HEADERS = ["timestamp","user_id","mood","comment"]
FEEDBACK_HEADERS = ["timestamp","user_id","name","username","rating","comment"]
RULES_HEADERS = ["rule_id","domain","segment","lang","text","citations"]
# == NEW: HabitsLog ==
HABITS_HEADERS = ["timestamp","user_id","habit","value","meta"]

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

GSPREAD_CLIENT: Optional[gspread.client.Client] = None
SPREADSHEET_ID_FOR_INTAKE: str = ""

def _sheets_init():
    global SHEETS_ENABLED, ss, ws_feedback, ws_users, ws_profiles, ws_episodes, ws_reminders, ws_daily, ws_rules, ws_habits
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
        # NEW: быстрый лог привычек
        ws_habits   = _ensure_ws("HabitsLog", HABITS_HEADERS)

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
# NEW: Habits memory
MEM_HABITS: List[dict] = []

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
                merged = {h: r.get(h, "") for h in USERS_HEADERS}
                merged["user_id"] = str(uid)
                if username: merged["username"] = username
                if lang:     merged["lang"] = lang
                end_col = gsu.rowcol_to_a1(1, len(USERS_HEADERS)).rstrip("1")
                ws_users.update(
                    range_name=f"A{i}:{end_col}{i}",
                    values=[[merged.get(h, "") for h in USERS_HEADERS]]
                )
                return
        ws_users.append_row([base.get(h,"") for h in USERS_HEADERS])
    else:
        prev = MEM_USERS.get(uid, {})
        merged = {**base, **prev}
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

# NEW: быстрый лог привычек
def habit_add(ts: str, uid: int, habit: str, value: str = "", meta: str = ""):
    rec = [ts, str(uid), habit, value, meta]
    if SHEETS_ENABLED and ws_habits:
        ws_habits.append_row(rec)
    else:
        MEM_HABITS.append({"timestamp":ts, "user_id":str(uid), "habit":habit, "value":value, "meta":meta})

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
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_good"], callback_data="mood|good"),
         InlineKeyboardButton(T[lang]["mood_ok"], callback_data="mood|ok"),
         InlineKeyboardButton(T[lang]["mood_bad"], callback_data="mood|bad")],
        [InlineKeyboardButton(T[lang]["mood_note"], callback_data="mood|note")]
    ])
    await maybe_send(context, uid, T[lang]["daily_gm"], kb)
    prof = profiles_get(uid)
    tips = pick_nutrition_tips(lang, prof, limit=2)
    if tips:
        await maybe_send(context, uid, "• " + "\n• ".join(tips))
    phase = cycle_phase_for(uid)
    if phase:
        tip = cycle_tip(lang, phase)
        if tip:
            await maybe_send(context, uid, tip)

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

# ===== Персонализированный префикс/баннер =====
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
    return f"{sex or '—'}, {age_raw or '—'}{hw}; goal — {goal or '—'}"

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

# ===== Клавиатуры =====
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

# ===== Главный экран меню (новый) =====
def _menu_labels(lang: str) -> dict:
    base = "en" if lang not in T else lang
    L = {
        "en": {
            "title": "Choose what to do:",
            "h60": "🩺 Health in 60 seconds",
            "sym": "🧭 Symptoms",
            "sym_headache": "Headache",
            "sym_heartburn": "Heartburn",
            "sym_fatigue": "Fatigue",
            "sym_other": "Other",
            "mini": "🔁 Mini-plans",
            "mini_neck": "Neck pain 5-min",
            "mini_sleep": "Sleep reset",
            "mini_hb": "Heartburn • 3 steps",
            "care": "🧪 Find care",
            "care_labs": "Labs near me",
            "care_urgent": "Urgent care",
            "care_free": "Free clinics (NJ)",
            "hab": "📊 Habits Quick-log",
            "hab_water": "💧Water",
            "hab_steps": "🚶Steps",
            "hab_sleep": "😴Sleep",
            "hab_stress": "🧠Stress",
            "hab_weight": "⚖️Weight",
            "rem": "🗓 Remind me",
            "lang": "🌐 Language",
            "privacy": "🔒 Privacy & how it works",
            "smart": "🧠 Smart check-in",
            "coming": "Coming soon",
        },
        "ru": {
            "title": "Выберите действие:",
            "h60": "🩺 Здоровье за 60 секунд",
            "sym": "🧭 Симптомы",
            "sym_headache": "Головная боль",
            "sym_heartburn": "Изжога",
            "sym_fatigue": "Усталость",
            "sym_other": "Другое",
            "mini": "🔁 Мини-планы",
            "mini_neck": "Шея • 5 мин",
            "mini_sleep": "Сон • перезагрузка",
            "mini_hb": "Изжога • 3 шага",
            "care": "🧪 Найти помощь",
            "care_labs": "Лаборатории рядом",
            "care_urgent": "Неотложка (Urgent care)",
            "care_free": "Бесплатные клиники (NJ)",
            "hab": "📊 Быстрый лог привычек",
            "hab_water": "💧Вода",
            "hab_steps": "🚶Шаги",
            "hab_sleep": "😴Сон",
            "hab_stress": "🧠Стресс",
            "hab_weight": "⚖️Вес",
            "rem": "🗓 Напомнить",
            "lang": "🌐 Язык",
            "privacy": "🔒 Конфиденциальность и как это работает",
            "smart": "🧠 Smart check-in",
            "coming": "Скоро",
        }
    }
    return L.get(base, L["en"])

def _kb_main_menu(lang: str) -> InlineKeyboardMarkup:
    L = _menu_labels(lang)
    rows = [
        [InlineKeyboardButton(L["h60"], callback_data="menu|h60"),
         InlineKeyboardButton(L["sym"], callback_data="menu|sym")],
        [InlineKeyboardButton(L["mini"], callback_data="menu|mini"),
         InlineKeyboardButton(L["care"], callback_data="menu|care")],
        [InlineKeyboardButton(L["hab"], callback_data="menu|hab"),
         InlineKeyboardButton(L["rem"], callback_data="menu|rem")],
        [InlineKeyboardButton(L["lang"], callback_data="menu|lang"),
         InlineKeyboardButton(L["privacy"], callback_data="menu|privacy")],
        [InlineKeyboardButton(L["smart"], callback_data="menu|smart"),
         InlineKeyboardButton("🏠 At-home labs/US/ECG", callback_data="menu|coming")]
    ]
    return InlineKeyboardMarkup(rows)

async def render_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None) or "en")
    await context.bot.send_message(update.effective_chat.id, _menu_labels(lang)["title"], reply_markup=_kb_main_menu(lang))

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

# ===== Profile (10 шагов) =====
PROFILE_STEPS = [
    {"key":"sex","opts":{"ru":[("Мужчина","male"),("Женщина","female"),("Другое","other")],
                         "en":[("Male","male"),("Female","female"),("Other","other")],
                         "uk":[("Чоловіча","male"),("Жіноча","female"),("Інша","other")],
                         "es":[("Hombre","male"),("Mujer","female"),("Otro","other")]}},
    {"key":"age","opts":{"ru":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
                         "en":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
                         "uk":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
                         "es":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")]}},
    {"key":"height_cm","opts":{"ru":[("160","160"),("170","170"),("180","180")],
                               "en":[("160","160"),("170","170"),("180","180")],
                               "uk":[("160","160"),("170","170"),("180","180")],
                               "es":[("160","160"),("170","170"),("180","180")]}},
    {"key":"weight_kg","opts":{"ru":[("60","60"),("75","75"),("90","90")],
                               "en":[("60","60"),("75","75"),("90","90")],
                               "uk":[("60","60"),("75","75"),("90","90")],
                               "es":[("60","60"),("75","75"),("90","90")]}},
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
    {"key":"supplements","opts":{"ru":[("Нет","none"),("Омега-3","omega3"),("Креатин","creatine"),("Протеин","protein"),("Другое","other")],
                                "en":[("None","none"),("Omega-3","omega3"),("Creatine","creatine"),("Protein","protein"),("Other","other")],
                                "uk":[("Немає","none"),("Омега-3","omega3"),("Креатин","creatine"),("Протеїн","protein"),("Інше","other")],
                                "es":[("Ninguno","none"),("Omega-3","omega3"),("Creatina","creatine"),("Proteína","protein"),("Otro","other")]}},
    {"key":"sleep","opts":{"ru":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
                           "en":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Irregular","irregular")],
                           "uk":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
                           "es":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Irregular","irregular")]}},
    {"key":"activity","opts":{"ru":[("<5к шагов","<5k"),("5–8к","5-8k"),("8–12к","8-12k"),("Спорт регулярно","sport")],
                             "en":[("<5k steps","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Regular sport","sport")],
                             "uk":[("<5к кроків","<5k"),("5–8к","5-8k"),("8–12к","8-12k"),("Спорт регулярно","sport")],
                             "es":[("<5k pasos","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Deporte regular","sport")]}},
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
        key_to_idx = {"sex":1,"age":2,"height_cm":3,"weight_kg":4,"goal":5,"conditions":6,"meds":7,"supplements":8,"sleep":9,"activity":10}
        num = key_to_idx.get(step["key"], idx+1)
        await context.bot.send_message(chat_id, T[lang][f"p_step_{num}"], reply_markup=kb)
        return
    prof = profiles_get(uid); summary=[]
    for k in ["sex","age","height_cm","weight_kg","goal","conditions","meds","supplements","sleep","activity","diet"]:
        v = prof.get(k) or sessions.get(uid,{}).get(k,"")
        if v: summary.append(f"{k}: {v}")
    profiles_upsert(uid, {})
    sessions[uid]["profile_active"] = False
    users_set(uid, "profile_banner_shown", "no")
    await context.bot.send_message(chat_id, T[lang]["saved_profile"] + "; ".join(summary))
    await context.bot.send_message(chat_id, T[lang]["start_where"], reply_markup=inline_topic_kb(lang))

# ===== Основной текстовый обработчик =====
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    text = (update.message.text or "").strip()
    logging.info(f"INCOMING uid={uid} text={text[:200]}")
    urec = users_get(uid)

    # новый пользователь
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
        context.user_data["lang"] = lang_guess
        await gate_show(update, context)
        return

    saved_lang = norm_lang(urec.get("lang") or getattr(user,"language_code",None))
    detected_lang = detect_lang_from_text(text, saved_lang)
    if detected_lang != saved_lang:
        users_set(uid,"lang",detected_lang)
    lang = detected_lang
    sessions.setdefault(uid, {})["last_user_text"] = text

    sc = detect_serious(text)
    if sc:
        sessions.setdefault(uid,{})["mode"] = "serious"
        sessions[uid]["serious_condition"] = sc
        prof = profiles_get(uid)
        plan = pain_plan(lang, [], prof)
        await update.message.reply_text("\n".join(plan), reply_markup=inline_actions(lang))
        ask_feedback_soft(uid, context, lang)
        return

    if sessions.get(uid, {}).get("awaiting_daily_comment"):
        daily_add(iso(utcnow()), uid, "note", text)
        sessions[uid]["awaiting_daily_comment"] = False
        await update.message.reply_text(T[lang]["mood_thanks"]); return

    if sessions.get(uid, {}).get("awaiting_free_feedback"):
        sessions[uid]["awaiting_free_feedback"] = False
        feedback_add(iso(utcnow()), uid, "free", user.username, "", text)
        await update.message.reply_text(T[lang]["fb_thanks"]); return

    if sessions.get(uid, {}).get("awaiting_city"):
        sessions[uid]["awaiting_city"] = False
        await update.message.reply_text(T[lang]["thanks"]); return

    if sessions.get(uid, {}).get("awaiting_h60"):
        sessions[uid]["awaiting_h60"] = False
        prof = profiles_get(uid)
        low = text.lower()
        if any(word in low for word in ["белок","protein","больше белка","↑белок"]):
            if lang=="ru":
                msg = "Под тебя подойдёт сегодня:\n• Творог 200 г + огурец\n• Омлет 2 яйца + овощи\n• Сардины 1 банка + салат\nВыбери вариант — подстрою дальше."
            elif lang=="uk":
                msg = "На сьогодні підійде:\n• Сир 200 г + огірок\n• Омлет 2 яйця + овочі\n• Сардини 1 банка + салат\nОбери варіант — підлаштую далі."
            else:
                msg = "Good picks for today:\n• Cottage cheese 200 g + cucumber\n• 2-egg omelet + veggies\n• Sardines (1 can) + salad\nPick one — I’ll tailor next."
            await update.message.reply_text(msg, reply_markup=inline_actions(lang))
        else:
            await update.message.reply_text(T[lang]["unknown"], reply_markup=inline_actions(lang))
        ask_feedback_soft(uid, context, lang)
        return

    if sessions.get(uid, {}).get("p_wait_key"):
        key = sessions[uid]["p_wait_key"]; sessions[uid]["p_wait_key"] = None
        val = text
        if key in {"age","height_cm","weight_kg"}:
            m = re.search(r'\d{1,3}', text)
            if m: val = m.group(0)
        profiles_upsert(uid,{key:val}); sessions[uid][key]=val
        users_set(uid, "profile_banner_shown", "no")
        await advance_profile_ctx(context, update.effective_chat.id, lang, uid); return

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

    if should_show_profile_banner(uid):
        prof = profiles_get(uid)
        banner = profile_banner(lang, prof)
        if banner.strip().strip("—"):
            await update.message.reply_text(banner)
        users_set(uid, "profile_banner_shown", "yes")

    prof = profiles_get(uid)
    data = llm_router_answer(text, lang, prof)

    msg = apply_warm_tone(data.get("assistant_reply") or T[lang]["unknown"], lang)
    await update.message.reply_text(msg, reply_markup=inline_actions(lang))

    # === Контекстные чипы после ответа ===
    low = text.lower()
    if any(k in low for k in ["heartburn","изжога","burning after meals","жжёт после еды","reflux"]):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Avoid triggers", callback_data="chip|hb|triggers")],
            [InlineKeyboardButton("OTC options", callback_data="chip|hb|otc")],
            [InlineKeyboardButton("When to see a doctor", callback_data="chip|hb|red")]
        ])
        await update.message.reply_text("Quick choices for heartburn:", reply_markup=kb)
    if any(k in low for k in ["neck pain","боль в шее","шея"]):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("5-min routine", callback_data="chip|neck|routine")],
            [InlineKeyboardButton("Heat/ice tips", callback_data="chip|neck|heat")],
            [InlineKeyboardButton("Red flags", callback_data="chip|neck|red")]
        ])
        await update.message.reply_text("Quick help for neck pain:", reply_markup=kb)

    ask_feedback_soft(uid, context, lang)
    for one in (data.get("followups") or [])[:2]:
        await send_unique(update.message, uid, apply_warm_tone(one, lang), force=True)
    return
# ====== PART 2 / CONTINUATION ======
# (Дополнение: главное меню, чипы, smart check-in, quick-log, care links, lang switch)
from urllib.parse import quote_plus

# ---------- Lazy HabitsLog (Sheets) ----------
HABITS_HEADERS = ["timestamp","user_id","kind","value","note"]
try:
    ws_habits  # may exist if Part 1 created it
except NameError:
    ws_habits = None
try:
    MEM_HABITS  # may exist
except NameError:
    MEM_HABITS = []

def _ensure_habits_ws():
    global ws_habits
    if not SHEETS_ENABLED:
        return
    try:
        if ws_habits is None:
            try:
                ws_habits = ss.worksheet("HabitsLog")
            except Exception:
                ws_habits = ss.add_worksheet(title="HabitsLog", rows=2000, cols=max(10, len(HABITS_HEADERS)))
                ws_habits.append_row(HABITS_HEADERS)
        vals = ws_habits.get_all_values()
        if not vals:
            ws_habits.append_row(HABITS_HEADERS)
        else:
            head = vals[0]
            if len(head) < len(HABITS_HEADERS):
                pad = HABITS_HEADERS[len(head):]
                ws_habits.update(
                    range_name=f"{gsu.rowcol_to_a1(1,len(head)+1)}:{gsu.rowcol_to_a1(1,len(HABITS_HEADERS))}",
                    values=[pad]
                )
    except Exception as e:
        logging.error(f"HabitsLog init error: {e}")

def habits_add(uid: int, kind: str, value: Optional[str] = "", note: str = ""):
    ts = iso(utcnow())
    if SHEETS_ENABLED:
        _ensure_habits_ws()
        try:
            ws_habits.append_row([ts, str(uid), kind, str(value or ""), note or ""])
        except Exception as e:
            logging.error(f"habits_add sheets error: {e}")
            MEM_HABITS.append({"timestamp":ts,"user_id":str(uid),"kind":kind,"value":str(value or ""), "note":note})
    else:
        MEM_HABITS.append({"timestamp":ts,"user_id":str(uid),"kind":kind,"value":str(value or ""), "note":note})

# ---------- Main Menu ----------
def build_main_menu_kb(lang: str) -> InlineKeyboardMarkup:
    # локализация ярлыков
    L = {
        "h60": {"ru":"🩺 Здоровье за 60 сек","uk":"🩺 Здоровʼя за 60 с","en":"🩺 Health in 60s","es":"🩺 Salud en 60s"},
        "sym": {"ru":"🧭 Симптомы","uk":"🧭 Симптоми","en":"🧭 Symptoms","es":"🧭 Síntomas"},
        "mini":{"ru":"🔁 Мини-планы","uk":"🔁 Міні-плани","en":"🔁 Mini-plans","es":"🔁 Mini-planes"},
        "care":{"ru":"🧪 Найти помощь","uk":"🧪 Знайти допомогу","en":"🧪 Find care","es":"🧪 Buscar atención"},
        "hab": {"ru":"📊 Быстрый лог","uk":"📊 Швидкий лог","en":"📊 Habits Quick-log","es":"📊 Registro rápido"},
        "rem": {"ru":"🗓 Напомнить","uk":"🗓 Нагадати","en":"🗓 Remind me","es":"🗓 Recuérdame"},
        "lang":{"ru":"🌐 Язык","uk":"🌐 Мова","en":"🌐 Language","es":"🌐 Idioma"},
        "priv":{"ru":"🔒 Приватность и как это работает","uk":"🔒 Приватність та як це працює",
                "en":"🔒 Privacy & how it works","es":"🔒 Privacidad y cómo funciona"},
        "smart":{"ru":"⚡ Smart check-in","uk":"⚡ Smart check-in","en":"⚡ Smart check-in","es":"⚡ Smart check-in"},
        "soon":{"ru":"🏠 Домашние анализы — soon","uk":"🏠 Домашні аналізи — soon","en":"🏠 At-home labs — coming soon","es":"🏠 Pruebas en casa — pronto"}
    }
    kb = [
        [InlineKeyboardButton(L["h60"][lang], callback_data="menu|h60")],
        [InlineKeyboardButton(L["sym"][lang],  callback_data="menu|sym"),
         InlineKeyboardButton(L["mini"][lang], callback_data="menu|mini")],
        [InlineKeyboardButton(L["care"][lang], callback_data="menu|care"),
         InlineKeyboardButton(L["hab"][lang],  callback_data="menu|hab")],
        [InlineKeyboardButton(L["rem"][lang],  callback_data="menu|rem"),
         InlineKeyboardButton(L["smart"][lang],callback_data="menu|smart")],
        [InlineKeyboardButton(L["lang"][lang], callback_data="menu|lang")],
        [InlineKeyboardButton(L["priv"][lang], callback_data="menu|privacy")],
        [InlineKeyboardButton(L["soon"][lang], callback_data="menu|coming")]
    ]
    return InlineKeyboardMarkup(kb)

async def render_main_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None) or "en")
    await context.bot.send_message(update.effective_chat.id,
                                   {"ru":"Главное меню:","uk":"Головне меню:","en":"Main menu:","es":"Menú principal:"}[lang],
                                   reply_markup=build_main_menu_kb(lang))

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await render_main_menu_cb(update, context)

# ---------- Mini-plans & Chips ----------
def _plan_heartburn(lang: str) -> str:
    body = {
        "ru":[
            "🔥 Изжога — 3 шага:",
            "1) После еды — 10–15 мин ходьбы, без наклонов.",
            "2) Ужин за 3–4 часа до сна; подними изголовье на 10–15 см.",
            "3) Без кофе/алко/острого на сутки.",
            "⚠️ Срочно к врачу: черный стул, рвота с кровью, боль в груди, похудание."
        ],
        "uk":[
            "🔥 Печія — 3 кроки:",
            "1) Після їжі — 10–15 хв ходьби, без нахилів.",
            "2) Вечеря за 3–4 год до сну; підняти узголівʼя на 10–15 см.",
            "3) Без кави/алко/гострого на добу.",
            "⚠️ Негайно до лікаря: чорний кал, блювання з кровʼю, біль у грудях, схуднення."
        ],
        "en":[
            "🔥 Heartburn — 3 steps:",
            "1) Walk 10–15 min after meals; avoid bending.",
            "2) Dinner 3–4h before bed; elevate head 10–15 cm.",
            "3) Skip coffee/alcohol/spicy for 24h.",
            "⚠️ Seek care: black stools, vomiting blood, chest pain, unintended weight loss."
        ],
        "es":[
            "🔥 Acidez — 3 pasos:",
            "1) Camina 10–15 min después de comer; evita inclinarte.",
            "2) Cena 3–4h antes de dormir; eleva la cabecera 10–15 cm.",
            "3) Evita café/alcohol/picante por 24h.",
            "⚠️ Acude a un médico: heces negras, vómito con sangre, dolor torácico, pérdida de peso."
        ],
    }[lang]
    return "\n".join(body)

def _plan_sleep_reset(lang: str) -> str:
    body = {
        "ru":[
            "😴 Sleep reset (3 ночи):",
            "1) Фиксированное пробуждение (±15 мин), без дневного сна.",
            "2) Свет утром 15–20 мин; кофе до 14:00.",
            "3) Ритуал перед сном 20–30 мин, без экрана; спальня прохладная."
        ],
        "uk":[
            "😴 Sleep reset (3 ночі):",
            "1) Фіксований підйом (±15 хв), без денного сну.",
            "2) Ранкове світло 15–20 хв; кава до 14:00.",
            "3) Ритуал перед сном 20–30 хв, без екрану; прохолодна спальня."
        ],
        "en":[
            "😴 Sleep reset (3 nights):",
            "1) Fixed wake time (±15 min), no naps.",
            "2) Morning light 15–20 min; caffeine before 2 pm.",
            "3) Wind-down 20–30 min, no screens; cool bedroom."
        ],
        "es":[
            "😴 Reinicio de sueño (3 noches):",
            "1) Hora fija de despertar (±15 min), sin siestas.",
            "2) Luz matutina 15–20 min; cafeína antes de las 14:00.",
            "3) Rutina previa al sueño 20–30 min, sin pantallas; dormitorio fresco."
        ],
    }[lang]
    return "\n".join(body)

def _plan_neck(lang: str) -> str:
    # используем уже существующую подсказку для шеи из on_callback(act|ex|neck)
    return {
        "ru":"🧘 Шея 5 мин: 1) медленные наклоны вперёд/назад ×5; 2) повороты в стороны ×5; 3) полукруги подбородком ×5; 4) растяжка трапеций 2×20с.",
        "uk":"🧘 Шия 5 хв: 1) повільні нахили вперед/назад ×5; 2) повороти ×5; 3) півкола підборіддям ×5; 4) розтяжка трапецій 2×20с.",
        "en":"🧘 Neck 5 min: 1) slow flex/extend ×5; 2) rotations L/R ×5; 3) chin semicircles ×5; 4) upper-trap stretch 2×20s.",
        "es":"🧘 Cuello 5 min: 1) flex/ext. lenta ×5; 2) giros izq/der ×5; 3) semicírculos ×5; 4) estiramiento trapecio 2×20s."
    }[lang]

def chips_heartburn(lang: str) -> InlineKeyboardMarkup:
    lab = {
        "tr": {"ru":"Avoid triggers","uk":"Уникати тригерів","en":"Avoid triggers","es":"Evitar desencadenantes"},
        "ot": {"ru":"OTC options","uk":"OTC варіанти","en":"OTC options","es":"Opciones OTC"},
        "rf": {"ru":"Когда к врачу","uk":"Коли до лікаря","en":"When to see a doctor","es":"Cuándo consultar"}
    }
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lab["tr"][lang], callback_data="chip|hb|triggers"),
         InlineKeyboardButton(lab["ot"][lang], callback_data="chip|hb|otc"),
         InlineKeyboardButton(lab["rf"][lang], callback_data="chip|hb|red")]
    ])

def chips_neck(lang: str) -> InlineKeyboardMarkup:
    lab = {
        "rt":{"ru":"Рутина 5 мин","uk":"Рутина 5 хв","en":"5-min routine","es":"Rutina 5 min"},
        "ht":{"ru":"Тепло/лёд","uk":"Тепло/лід","en":"Heat/Ice tips","es":"Calor/Hielo"},
        "rf":{"ru":"Когда к врачу","uk":"Коли до лікаря","en":"Red flags","es":"Señales de alarma"}
    }
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lab["rt"][lang], callback_data="chip|neck|routine"),
         InlineKeyboardButton(lab["ht"][lang], callback_data="chip|neck|heat"),
         InlineKeyboardButton(lab["rf"][lang], callback_data="chip|neck|red")]
    ])

async def maybe_show_context_chips(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str, lang: str):
    txt = (user_text or "").lower()
    if any(k in txt for k in ["heartburn","изжог","acidez","burning after meal","burning after meals","после еды жжёт","жжет после еды"]):
        await context.bot.send_message(update.effective_chat.id,
                                       {"ru":"Варианты на выбор:","uk":"Варіанти:","en":"Quick options:","es":"Opciones:"}[lang],
                                       reply_markup=chips_heartburn(lang))
    if any(k in txt for k in ["neck pain","боль в шее","ший","cuello"]):
        await context.bot.send_message(update.effective_chat.id,
                                       {"ru":"Подсказки:","uk":"Підказки:","en":"Tips:","es":"Sugerencias:"}[lang],
                                       reply_markup=chips_neck(lang))

# ---------- Find care (NJ / near me) ----------
def _map_link(q: str) -> str:
    return f"https://www.google.com/maps/search/{quote_plus(q)}"

def care_links_msg(lang: str) -> str:
    lines = {
        "ru":[
            "🧪 Лабы рядом: " + _map_link("labs near me"),
            "🏥 Urgent care: " + _map_link("urgent care near me"),
            "🆓 Бесплатные клиники (NJ): " + _map_link("free clinic new jersey")
        ],
        "uk":[
            "🧪 Лабораторії поруч: " + _map_link("labs near me"),
            "🏥 Urgent care: " + _map_link("urgent care near me"),
            "🆓 Безкоштовні клініки (NJ): " + _map_link("free clinic new jersey")
        ],
        "en":[
            "🧪 Labs near me: " + _map_link("labs near me"),
            "🏥 Urgent care: " + _map_link("urgent care near me"),
            "🆓 Free clinics (NJ): " + _map_link("free clinic new jersey")
        ],
        "es":[
            "🧪 Laboratorios cerca: " + _map_link("labs near me"),
            "🏥 Urgent care: " + _map_link("urgent care near me"),
            "🆓 Clínicas gratuitas (NJ): " + _map_link("free clinic new jersey")
        ],
    }[lang]
    return "\n".join(lines)

# ---------- Smart check-in ----------
def smart_options_kb(lang: str) -> InlineKeyboardMarkup:
    lab = {
        "ok":{"ru":"✅ Всё ок","uk":"✅ Все ок","en":"✅ I’m OK","es":"✅ Estoy bien"},
        "pn":{"ru":"🤕 Боль","uk":"🤕 Біль","en":"🤕 Pain","es":"🤕 Dolor"},
        "ti":{"ru":"🥱 Устал","uk":"🥱 Втома","en":"🥱 Tired","es":"🥱 Cansado"},
        "st":{"ru":"🧠 Стресс","uk":"🧠 Стрес","en":"🧠 Stressed","es":"🧠 Estrés"},
        "hb":{"ru":"🔥 Изжога","uk":"🔥 Печія","en":"🔥 Heartburn","es":"🔥 Acidez"},
        "ot":{"ru":"🧩 Другое","uk":"🧩 Інше","en":"🧩 Other","es":"🧩 Otro"}
    }
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lab["ok"][lang], callback_data="smart|ok")],
        [InlineKeyboardButton(lab["pn"][lang], callback_data="smart|pain"),
         InlineKeyboardButton(lab["ti"][lang], callback_data="smart|tired")],
        [InlineKeyboardButton(lab["st"][lang], callback_data="smart|stressed"),
         InlineKeyboardButton(lab["hb"][lang], callback_data="smart|heartburn")],
        [InlineKeyboardButton(lab["ot"][lang], callback_data="smart|other")]
    ])

def _smart_plan(tag: str, lang: str) -> str:
    d = {
        "ok":{
            "ru":["👍 Отлично: продолжай режим.","• Вода 300–500 мл сейчас","• 5–10 мин движения","• Вечером — 10 мин без экрана"],
            "uk":["👍 Чудово: продовжуй.","• Вода 300–500 мл","• 5–10 хв руху","• Ввечері — 10 хв без екрана"],
            "en":["👍 Great! Keep it up.","• Drink 300–500 ml water now","• Move 5–10 min","• 10-min no-screen wind-down tonight"],
            "es":["👍 ¡Genial! Sigue así.","• Bebe 300–500 ml de agua","• Muévete 5–10 min","• 10 min sin pantallas por la noche"]
        },
        "pain":{
            "ru":[_plan_neck("ru"), "⚠️ Красные флаги: слабость/онемение, травма, боль ≥7/10, прогрессирование."],
            "uk":[_plan_neck("uk"), "⚠️ Червоні прапорці: слабкість/оніміння, травма, біль ≥7/10, прогрес."],
            "en":[_plan_neck("en"), "⚠️ Red flags: weakness/numbness, trauma, pain ≥7/10, worsening."],
            "es":[_plan_neck("es"), "⚠️ Alarmas: debilidad/entumec., trauma, dolor ≥7/10, empeora."]
        },
        "tired":{
            "ru":["⚡ Энергия x3:","1) 10-мин быстрая ходьба","2) Вода 300–500 мл + лёгкий белок","3) 20-мин без экрана"],
            "uk":["⚡ Енергія x3:","1) 10-хв швидка ходьба","2) Вода 300–500 мл + легкий білок","3) 20-хв без екрана"],
            "en":["⚡ Energy x3:","1) 10-min brisk walk","2) 300–500 ml water + light protein","3) 20-min no-screen reset"],
            "es":["⚡ Energía x3:","1) 10-min caminata rápida","2) 300–500 ml agua + proteína ligera","3) 20-min sin pantallas"]
        },
        "stressed":{
            "ru":["🧠 Стресс:","1) Дыхание 4-7-8 ×4","2) 10-мин прогулка без телефона","3) Напоминание — вода через 2–4 ч?"],
            "uk":["🧠 Стрес:","1) Дихання 4-7-8 ×4","2) 10-хв прогулянка без телефону","3) Нагадування — вода через 2–4 год?"],
            "en":["🧠 Stress:","1) 4-7-8 breathing ×4","2) 10-min walk, no phone","3) Reminder — water in 2–4h?"],
            "es":["🧠 Estrés:","1) Respiración 4-7-8 ×4","2) 10-min caminata sin móvil","3) Recordatorio — agua en 2–4h?"]
        },
        "heartburn":{
            "ru":[_plan_heartburn("ru")],
            "uk":[_plan_heartburn("uk")],
            "en":[_plan_heartburn("en")],
            "es":[_plan_heartburn("es")]
        },
        "other":{
            "ru":["🧩 Опиши кратко, что беспокоит — подскажу 3 шага.","Например: «головная боль», «усталость»."],
            "uk":["🧩 Опиши коротко, що турбує — дам 3 кроки.","Напр.: «головний біль», «втома»."],
            "en":["🧩 Tell me briefly what’s wrong — I’ll give 3 steps.","E.g., “headache”, “fatigue”."],
            "es":["🧩 Dime brevemente qué pasa — te doy 3 pasos.","Ej.: “dolor de cabeza”, “fatiga”."]
        }
    }
    return "\n".join(d[tag][lang])

# ---------- Habits quick-log UI ----------
def habits_menu_kb(lang: str) -> InlineKeyboardMarkup:
    lab = {
        "w":{"ru":"💧Вода","uk":"💧Вода","en":"💧Water","es":"💧Agua"},
        "s":{"ru":"🚶Шаги","uk":"🚶Кроки","en":"🚶Steps","es":"🚶Pasos"},
        "sl":{"ru":"😴Сон","uk":"😴Сон","en":"😴Sleep","es":"😴Sueño"},
        "st":{"ru":"🧠Стресс","uk":"🧠Стрес","en":"🧠Stress","es":"🧠Estrés"},
        "wt":{"ru":"⚖️Вес","uk":"⚖️Вага","en":"⚖️Weight","es":"⚖️Peso"},
    }
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lab["w"][lang],  callback_data="hab|water"),
         InlineKeyboardButton(lab["s"][lang],  callback_data="hab|steps"),
         InlineKeyboardButton(lab["sl"][lang], callback_data="hab|sleep")],
        [InlineKeyboardButton(lab["st"][lang], callback_data="hab|stress"),
         InlineKeyboardButton(lab["wt"][lang], callback_data="hab|weight")]
    ])

# ---------- Language switch ----------
def language_switch_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("EN", callback_data="lang|en"),
         InlineKeyboardButton("RU", callback_data="lang|ru"),
         InlineKeyboardButton("UK", callback_data="lang|uk"),
         InlineKeyboardButton("ES", callback_data="lang|es")]
    ])

# ---------- Extend on_text to handle new waits ----------
_old_on_text = on_text
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(user, "language_code", None) or "en")

    # вес из quick-log
    if sessions.get(uid, {}).get("awaiting_weight"):
        sessions[uid]["awaiting_weight"] = False
        val = re.search(r'\d{1,3}(?:[.,]\d{1,2})?', (update.message.text or ""))
        if val:
            w = val.group(0).replace(",", ".")
            habits_add(uid, "weight", w)
            # обновим профиль (мягко)
            try:
                profiles_upsert(uid, {"weight_kg": w})
            except Exception:
                pass
            await update.message.reply_text({"ru":f"Записал вес: {w} кг ✅",
                                             "uk":f"Записав вагу: {w} кг ✅",
                                             "en":f"Logged weight: {w} kg ✅",
                                             "es":f"Peso registrado: {w} kg ✅"}[lang],
                                            reply_markup=build_main_menu_kb(lang))
            return
        else:
            await update.message.reply_text({"ru":"Пожалуйста, отправьте число, например 74.5",
                                             "uk":"Надішліть число, напр. 74.5",
                                             "en":"Please send a number, e.g., 74.5",
                                             "es":"Envía un número, p. ej., 74.5"}[lang])
            sessions[uid]["awaiting_weight"] = True
            return

    # иначе — оригинальная логика
    await _old_on_text(update, context)

    # Контекстные чипы после ответа
    try:
        lang_now = norm_lang(users_get(uid).get("lang") or lang)
        await maybe_show_context_chips(update, context, (update.message.text or ""), lang_now)
    except Exception as e:
        logging.debug(f"chips post-send error: {e}")

# ---------- Extend on_callback to handle new prefixes ----------
_old_on_callback = on_callback
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    chat_id = q.message.chat.id

    # === MENU family ===
    if data.startswith("menu|"):
        await q.answer()
        sub = data.split("|",1)[1]
        if sub == "h60":
            sessions.setdefault(uid, {})["awaiting_h60"] = True
            await q.message.reply_text(T[lang]["h60_intro"])
            return
        if sub == "sym":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Headache", callback_data="sym|headache"),
                 InlineKeyboardButton("Heartburn", callback_data="sym|heartburn")],
                [InlineKeyboardButton("Fatigue", callback_data="sym|fatigue"),
                 InlineKeyboardButton({"ru":"Другое","uk":"Інше","en":"Other","es":"Otro"}[lang], callback_data="sym|other")]
            ])
            await q.message.reply_text({"ru":"Выберите симптом:","uk":"Оберіть симптом:","en":"Pick a symptom:","es":"Elige un síntoma:"}[lang], reply_markup=kb)
            return
        if sub == "mini":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Neck pain 5-min", callback_data="mini|neck")],
                [InlineKeyboardButton("Sleep reset (3n)", callback_data="mini|sleep")],
                [InlineKeyboardButton("Heartburn 3 steps", callback_data="mini|hb")]
            ])
            await q.message.reply_text({"ru":"Мини-планы:","uk":"Міні-плани:","en":"Mini-plans:","es":"Mini-planes:"}[lang], reply_markup=kb)
            return
        if sub == "care":
            await q.message.reply_text(care_links_msg(lang))
            return
        if sub == "hab":
            await q.message.reply_text({"ru":"Быстрый лог привычек:","uk":"Швидкий лог звичок:","en":"Habits quick-log:","es":"Registro rápido de hábitos:"}[lang],
                                       reply_markup=habits_menu_kb(lang))
            return
        if sub == "rem":
            await q.message.reply_text(T[lang]["remind_when"], reply_markup=inline_remind(lang))
            return
        if sub == "lang":
            await q.message.reply_text({"ru":"Выберите язык:","uk":"Оберіть мову:","en":"Choose language:","es":"Elige idioma:"}[lang],
                                       reply_markup=language_switch_kb())
            return
        if sub == "privacy":
            await q.message.reply_text(T[lang]["privacy"])
            return
        if sub == "smart":
            await q.message.reply_text({"ru":"Smart check-in:","uk":"Smart check-in:","en":"Smart check-in:","es":"Smart check-in:"}[lang],
                                       reply_markup=smart_options_kb(lang))
            return
        if sub == "coming":
            txt = {
                "ru":"🏠 Домашние анализы/УЗИ/ЭКГ — скоро.\n👨‍⚕️ Tele-visit — партнёры (в разработке).\n💊 Ремайндеры приёма — скоро.",
                "uk":"🏠 Домашні аналізи/УЗД/ЕКГ — скоро.\n👨‍⚕️ Tele-visit — партнери (в розробці).\n💊 Нагадування прийому — скоро.",
                "en":"🏠 At-home labs/ultrasound/ECG — coming soon.\n👨‍⚕️ Tele-visit partners — in progress.\n💊 Medication reminders — coming soon.",
                "es":"🏠 Pruebas en casa/eco/ECG — pronto.\n👨‍⚕️ Tele-visita — socios en camino.\n💊 Recordatorios de medicación — pronto."
            }[lang]
            await q.message.reply_text(txt)
            return

    # === Language tap ===
    if data.startswith("lang|"):
        await q.answer()
        code = data.split("|",1)[1]
        if code in {"en","ru","uk","es"}:
            users_set(uid, "lang", code)
            context.user_data["lang"] = code
            await q.message.reply_text({"ru":"Готово. Язык переключен.","uk":"Готово. Мову змінено.","en":"Done. Language switched.","es":"Listo. Idioma cambiado."}[code])
            # перерисуем меню
            await render_main_menu_cb(update, context)
        return

    # === Symptoms quick ===
    if data.startswith("sym|"):
        await q.answer()
        tag = data.split("|",1)[1]
        if tag == "headache":
            await q.message.reply_text(pain_plan(lang, [], profiles_get(uid))[0] if lang in {"ru","uk","en","es"} else _plan_neck(lang))
            return
        if tag == "heartburn":
            await q.message.reply_text(_plan_heartburn(lang), reply_markup=chips_heartburn(lang))
            return
        if tag ==
