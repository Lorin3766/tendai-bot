# -*- coding: utf-8 -*-
# TendAI main.py — обновлено:
# - ежедневные напоминания: утро + ВЕЧЕР (/checkin_evening HH:MM)
# - восстановление расписаний на старте
# - клиппинг /settz в диапазон −12…+14
# - безопасные Google Sheets (headers, fallbacks), memory fallback
# - очистка /delete_data со снятием джобов
# - PRO-intake плагин (опционально)
# - меню, чипы, мини-планы, Youth-команды, мягкий фидбек и т.п.
#
# ⚠️ Эта часть — 1/2. В конце файла есть маркер «ЧАСТЬ 2».
# В ЧАСТИ 2 находятся callback-router, вспомогательные функции и entrypoint.

import os, re, json, uuid, logging, random
from datetime import datetime, timedelta, timezone, time as dtime, date
from typing import List, Tuple, Dict, Optional, Any
from difflib import SequenceMatcher

from dotenv import load_dotenv
from langdetect import detect, DetectorFactory

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
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
            await q.message.reply_text("PRO-опрос недоступен на этом деплое. Используйте /profile.")
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
DEFAULT_CHECKIN_LOCAL = "08:30"   # дефолтное утро
DEFAULT_EVENING_LOCAL = "20:30"   # дефолтный вечер

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
        "help": "Short checkups, 24–48h plans, reminders, daily check-ins.\nCommands: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_evening 20:30 /checkin_off /settz +2 /health60 /energy /mood /water /skin /ru /uk /en /es /menu",
        "privacy": "TendAI is not a medical service and can’t replace a doctor. We provide navigation and self-care tips. Minimal data stored for reminders. /delete_data to erase.",
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
        "start_where":"Where do you want to start now? — or tap /menu",
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
        "skin_title": "Skin/Body tip:",
        # Main menu labels
        "m_menu_title": "Main menu",
        "m_sym": "🧭 Symptoms",
        "m_h60": "🩺 Health in 60 seconds",
        "m_mini": "🔁 Mini-plans",
        "m_care": "🧪 Find care",
        "m_hab": "📊 Habits Quick-log",
        "m_rem": "🗓 Remind me",
        "m_lang": "🌐 Language",
        "m_privacy": "🔒 Privacy & how it works",
        "m_smart": "🧠 Smart check-in",
        "m_soon": "🏠 At-home labs/ECG — coming soon",
        # Chips
        "chips_hb": "Avoid triggers • OTC options • When to see a doctor",
        "chips_neck": "5-min routine • Heat/Ice tips • Red flags",
    },
    "ru": {
        "welcome":"Привет! Я TendAI — ассистент здоровья и долголетия.\nРасскажи, что беспокоит; я подскажу. Сначала короткий опрос (~40с), чтобы советы были точнее.",
        "help":"Короткие проверки, план на 24–48 ч, напоминания, ежедневные чек-ины.\nКоманды: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_evening 20:30 /checkin_off /settz +3 /health60 /energy /mood /water /skin /ru /uk /en /es /menu",
        "privacy":"TendAI не заменяет врача. Это навигация и советы по самопомощи. Храним минимум данных для напоминаний. /delete_data — удалить.",
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
        "start_where":"С чего начнём? — или нажми /menu",
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
        "h60_intro": "Коротко напишите, что беспокоит. Я дам 3 ключевых совета за 60 секунд.",
        "h60_t1": "Возможные причины",
        "h60_t2": "Что сделать сейчас (24–48 ч)",
        "h60_t3": "Когда обратиться к врачу",
        "h60_serious": "Что серьёзное исключить",
        "energy_title": "Энергия на сегодня:",
        "water_prompt": "Выпей 300–500 мл воды. Напомнить через 2 часа?",
        "skin_title": "Совет для кожи/тела:",
        "m_menu_title": "Главное меню",
        "m_sym": "🧭 Симптомы",
        "m_h60": "🩺 Здоровье за 60 секунд",
        "m_mini": "🔁 Мини-планы",
        "m_care": "🧪 Куда обратиться",
        "m_hab": "📊 Быстрый лог привычек",
        "m_rem": "🗓 Напомнить",
        "m_lang": "🌐 Язык",
        "m_privacy": "🔒 Приватность и как это работает",
        "m_smart": "🧠 Смарт-чек-ин",
        "m_soon": "🏠 Домашние анализы/ЭКГ — скоро",
        "chips_hb": "Избегать триггеры • OTC-варианты • Когда к врачу",
        "chips_neck": "Рутина 5 мин • Тепло/лед • Красные флаги",
    }
}
# Наследуем uk от ru и переопределяем отличия
T["uk"] = {**T["ru"], **{
    "help": "Короткі перевірки, план на 24–48 год, нагадування, щоденні чек-іни.\nКоманди: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_evening 20:30 /checkin_off /settz +2 /health60 /energy /mood /water /skin /ru /uk /en /es /menu",
    "daily_pm":"Вечірній чек-ін: як пройшов день?",
    "act_rem_2h": "⏰ Нагадати через 2 год",
    "energy_title": "Енергія на сьогодні:",
    "water_prompt": "Випий 300–500 мл води. Нагадати через 2 години?",
    "skin_title": "Догляд за шкірою/тілом:",
    "m_menu_title": "Головне меню",
    "m_sym": "🧭 Симптоми",
    "m_h60": "🩺 Здоровʼя за 60 секунд",
    "m_mini": "🔁 Міні-плани",
    "m_care": "🧪 Куди звернутись",
    "m_hab": "📊 Швидкий лог звичок",
    "m_rem": "🗓 Нагадати",
    "m_lang": "🌐 Мова",
    "m_privacy": "🔒 Приватність і як це працює",
    "m_smart": "🧠 Смарт-чек-ін",
    "m_soon": "🏠 Домашні аналізи/ЕКГ — скоро",
    "chips_hb": "Уникати тригери • OTC-варіанти • Коли до лікаря",
    "chips_neck": "Рутина 5 хв • Тепло/лід • Червоні прапори",
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
ws_feedback = ws_users = ws_profiles = ws_episodes = ws_reminders = ws_daily = ws_rules = ws_habits = None

# === Canonical headers + safe reader ===
USERS_HEADERS = [
    "user_id","username","lang","consent","tz_offset","checkin_hour","paused",
    "quiet_hours","last_sent_utc","sent_today","streak","challenge_id","challenge_day",
    "last_fb_asked","profile_banner_shown","evening_hour",
    "pending_q","name"  # [PATCH] добавлены поля
]
PROFILES_HEADERS = ["user_id","sex","age","goal","conditions","meds","allergies","sleep","activity","diet","notes","updated_at","goals","diet_focus","steps_target","cycle_enabled","cycle_last_date","cycle_avg_len","height_cm","weight_kg","supplements"]
EPISODES_HEADERS = ["episode_id","user_id","topic","started_at","baseline_severity","red_flags","plan_accepted","target","reminder_at","next_checkin_at","status","last_update","notes"]
REMINDERS_HEADERS = ["id","user_id","text","when_utc","created_at","status"]
DAILY_HEADERS = ["timestamp","user_id","mood","comment"]
FEEDBACK_HEADERS = ["timestamp","user_id","name","username","rating","comment"]
RULES_HEADERS = ["rule_id","domain","segment","lang","text","citations"]
HABITS_HEADERS = ["timestamp","user_id","type","value","unit","streak"]

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
                ws = ss.add_worksheet(title=title, rows=2000, cols=max(20, len(headers)))
                ws.append_row(headers)
            vals = ws.get_all_values()
            if not vals:
                ws.append_row(headers)
            else:
                head = vals[0]
                if len(head) < len(headers):
                    pad = headers[len(head):]
                    ws.update(range_name=f"{gsu.rowcol_to_a1(1,len(head)+1)}:{gsu.rowcol_to_a1(1,len(headers))}", values=[pad])
            return ws

        ws_feedback = _ensure_ws("Feedback", FEEDBACK_HEADERS)
        ws_users    = _ensure_ws("Users", USERS_HEADERS)
        ws_profiles = _ensure_ws("Profiles", PROFILES_HEADERS)
        ws_episodes = _ensure_ws("Episodes", EPISODES_HEADERS)
        ws_reminders= _ensure_ws("Reminders", REMINDERS_HEADERS)
        ws_daily    = _ensure_ws("DailyCheckins", DAILY_HEADERS)
        ws_rules    = _ensure_ws("Rules", RULES_HEADERS)
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
        "profile_banner_shown": "no",
        "evening_hour": DEFAULT_EVENING_LOCAL,
        "pending_q": "no",  # [PATCH]
        "name": "",         # [PATCH]
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
                ws_users.update(range_name=f"A{i}:{end_col}{i}",
                                values=[[merged.get(h, "") for h in USERS_HEADERS]])
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
    rec = {"id":rid,"user_id":str(uid),"text":text,"when_utc":iso(when_utc),"_created_at":iso(utcnow()),"status":"scheduled"}
    if SHEETS_ENABLED:
        ws_reminders.append_row([rec.get("id",""), rec.get("user_id",""), rec.get("text",""), rec.get("when_utc",""), rec.get("_created_at",""), rec.get("status","")])
    else:
        MEM_REMINDERS.append({"id":rid,"user_id":str(uid),"text":text,"when_utc":iso(when_utc),"created_at":iso(utcnow()),"status":"scheduled"})
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

# --- HABITS LOG ---
def habits_add(uid: int, typ: str, value: Optional[str], unit: Optional[str]) -> int:
    ts = iso(utcnow())
    rec = {"timestamp":ts,"user_id":str(uid),"type":typ,"value":value or "1","unit":unit or "", "streak":"0"}
    if SHEETS_ENABLED:
        ws_habits.append_row([rec.get(h,"") for h in HABITS_HEADERS])
        rows = ws_records(ws_habits, HABITS_HEADERS)
        rows = [r for r in rows if r.get("user_id")==str(uid) and r.get("type")==typ]
    else:
        MEM_HABITS.append(rec)
        rows = [r for r in MEM_HABITS if r.get("user_id")==str(uid) and r.get("type")==typ]
    def _to_date(r):
        try:
            dt = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S%z").astimezone(timezone.utc).date()
            return dt
        except Exception:
            return utcnow().date()
    rows_sorted = sorted(rows, key=lambda r: r["timestamp"], reverse=True)
    today = (utcnow()).date()
    streak = 0
    expected = today
    for r in rows_sorted:
        d = _to_date(r)
        if d == expected:
            streak = 1 if streak == 0 else streak + 1
            expected = expected - timedelta(days=1)
        elif d < expected:
            break
    if rows_sorted:
        rows_sorted[0]["streak"] = str(streak)
    return streak

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

# ---------- UPDATED time parsing (supports am/pm) ----------
def parse_hhmm_any(s: str) -> Optional[str]:
    """Парсит '16:20', '4:20 pm', '4pm', '12am' → 'HH:MM' (24ч)."""
    if not s:
        return None
    txt = s.strip().lower().replace(".", "")
    m = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', txt)
    if not m:
        return None
    h = int(m.group(1))
    mnt = int(m.group(2) or "0")
    ampm = m.group(3)
    if ampm:
        if ampm == "am":
            h = 0 if h == 12 else h
        else:  # pm
            h = 12 if h == 12 else h + 12
    if not (0 <= h <= 23 and 0 <= mnt <= 59):
        return None
    return f"{h:02d}:{mnt:02d}"

def hhmm_tuple(hhmm:str)->Tuple[int,int]:
    """Теперь понимает и '4:20 pm'."""
    norm = parse_hhmm_any(hhmm) or "08:30"
    m = re.search(r'([01]?\d|2[0-3]):([0-5]\d)', norm)
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

# === Вечер: отдельный джоб (Users.evening_hour) ===
def schedule_morning_evening(app, uid:int, tz_off:int, lang:str):
    if not _has_jq_app(app): return
    for j in app.job_queue.get_jobs_by_name(f"daily_e_{uid}"):
        j.schedule_removal()
    hhmm = users_get(uid).get("evening_hour") or DEFAULT_EVENING_LOCAL
    h_e, m_e = hhmm_tuple(hhmm); h_e = (h_e - tz_off) % 24
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

# === ПРАВКА 1: корректный сброс sent_today при смене локального дня ===
def can_send(uid: int) -> bool:
    u = users_get(uid)
    if (u.get("paused") or "").lower() == "yes":
        return False
    if _in_quiet(uid, utcnow()):
        return False

    # корректный сброс лимита по новому локальному дню
    tz_off = int(str(u.get("tz_offset") or "0"))
    today_local = (utcnow() + timedelta(hours=tz_off)).date()

    last = (u.get("last_sent_utc") or "").strip()
    last_local = None
    if last:
        try:
            last_local = (datetime.strptime(last, "%Y-%m-%d %H:%M:%S%z")
                          .astimezone(timezone.utc) + timedelta(hours=tz_off)).date()
        except Exception:
            last_local = None

    sent_today = int(str(u.get("sent_today") or "0"))
    if (not last_local) or (last_local != today_local):
        sent_today = 0
        users_set(uid, "sent_today", "0")

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

# === ПРАВКА 2: maybe_send с force/count и форс-чек-ины в джобах ===
async def maybe_send(context, uid, text, kb=None, *, force=False, count=True):
    if force or can_send(uid):
        try:
            await context.bot.send_message(uid, text, reply_markup=kb)
            if count:
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
    # форс-чек-ин (вне лимитера, не увеличивает счётчик)
    await maybe_send(context, uid, T[lang]["daily_gm"], kb, force=True, count=False)

    prof = profiles_get(uid)
    tips = pick_nutrition_tips(lang, prof, limit=2)
    if tips:
        await maybe_send(context, uid, "• " + "\n• ".join(tips))

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
    # форс-чек-ин (вне лимитера, не увеличивает счётчик)
    await maybe_send(context, uid, T[lang]["daily_pm"], kb, force=True, count=False)

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

# ===== Персонализированный баннер профиля =====
def _ru_age_phrase(age_str: str) -> str:
    try:
        n = int(re.search(r"\d+", age_str).group())
    except Exception:
        return age_str
    last2 = n % 100
    last1 = n % 10
    if 11 <= last2 <= 14: word = "лет"
    elif last1 == 1:      word = "год"
    elif 2 <= last1 <= 4: word = "года"
    else:                 word = "лет"
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
    if row: rows.append
# =========================
# ======= ЧАСТЬ 2 =========
# =========================
# (продолжение) callback-router, /name, мини-планы, регистрация хэндлеров, entrypoint
# =========================

# --- [PATCH] страховка: если «именные»/anti-spam/факты не попали в ЧАСТЬ 1, объявим их тут ---
if "sanitize_name" not in globals():
    def sanitize_name(raw: str) -> str:
        s = (raw or "").strip()
        s = re.sub(r"[^A-Za-zА-Яа-яЁёІіЇїЄєҐґ'’\-\. ]+", "", s)
        s = re.sub(r"\s{2,}", " ", s)
        return s[:30]

if "display_name" not in globals():
    def display_name(uid: int) -> str:
        u = users_get(uid)
        name = (u.get("name") or "").strip()
        if name:
            return name
        # как фолбэк — username без @
        username = (u.get("username") or "").strip().lstrip("@")
        return username

if "set_name" not in globals():
    def set_name(uid: int, name: str):
        name = sanitize_name(name)
        if name:
            users_set(uid, "name", name)

if "ensure_ask_name" not in globals():
    async def ensure_ask_name(uid: int, lang: str) -> bool:
        """Один раз попросить имя. Возвращает True, если задали вопрос (и дальше можно return из хэндлера)."""
        u = users_get(uid)
        if (u.get("name") or "").strip():
            return False
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✍️ " + ("Написать имя" if lang!="en" else "Type your name"), callback_data="name|ask")],
            [InlineKeyboardButton(T[lang]["skip"], callback_data="name|skip")]
        ])
        try:
            # используем мягкий текст — подставка {name} произойдёт в maybe_send-обёртке
            await context.application.bot.send_message(uid, "Как к вам обращаться, {name}?")
        except Exception:
            pass
        try:
            await context.application.bot.send_message(uid,
                "Напишите имя одним сообщением (например: «Алексей»)." if lang!="en" else "Please send your name in one message (e.g., “Alex”).",
                reply_markup=kb)
        except Exception:
            pass
        sessions.setdefault(uid, {})["awaiting_name"] = True
        return True

if "try_handle_name_reply" not in globals():
    async def try_handle_name_reply(uid: int, text: str, lang: str) -> bool:
        """Если мы ждали имя — сохранить и подтвердить. Возвращает True, если обработали сообщение."""
        if not sessions.get(uid, {}).get("awaiting_name"):
            return False
        sessions[uid]["awaiting_name"] = False
        name = sanitize_name(text)
        if not name:
            try:
                await context.application.bot.send_message(uid,
                    "Не понял. Пришлите имя буквами (например: «Мария»)." if lang!="en" else "Didn’t catch that — please send a name (e.g., “Maria”).")
            except Exception:
                pass
            return True
        set_name(uid, name)
        try:
            await context.application.bot.send_message(uid,
                (f"Принял, {name}! 👍" if lang!="en" else f"Got it, {name}! 👍"))
        except Exception:
            pass
        return True

# --- [PATCH] anti-spam вопросов (если не было в ЧАСТИ 1) ---
if "clear_pending" not in globals():
    def clear_pending(uid: int):
        try:
            users_set(uid, "pending_q", "no")
        except Exception:
            pass

if "is_question" not in globals():
    def is_question(text: str) -> bool:
        s = (text or "").strip().lower()
        if not s:
            return False
        if "?" in s:
            return True
        # простые сигналы-вопросы на нескольких языках
        q_kws = ["как", "когда", "что", "чему", "почему", "зачем",
                 "how", "when", "what", "which", "why", "where", "can", "could",
                 "як", "коли", "що", "чому", "навіщо"]
        return any(re.search(rf"\b{kw}\b", s) for kw in q_kws)

# --- [PATCH] reflect_facts (лёгкая персонализация) ---
if "reflect_facts" not in globals():
    def reflect_facts(text: str) -> str:
        low = (text or "").lower()
        facts = []
        # сон
        m = re.search(r'(\d{1,2})\s*[-–/]\s*(\d{1,2})\s*час', low) or re.search(r'(\d)\s*-\s*(\d)\s*h', low)
        if "сплю" in low or "сон" in low or "sleep" in low:
            m2 = re.search(r'(\d{1,2})\s*ч', low) or re.search(r'(\d{1,2})\s*h', low)
            if m2:
                facts.append(f"вижу: спишь ~{m2.group(1)} ч.")
        if m:
            a, b = m.group(1), m.group(2)
            facts.append(f"заметил: сон {a}–{b} ч.")
        # стресс
        if any(k in low for k in ["стресс", "stress", "тревог", "anxie"]):
            facts.append("отмечаю высокий стресс.")
        # изжога
        if any(k in low for k in ["изжог", "heartburn", "кислота"]):
            facts.append("есть жалоба на изжогу.")
        # вода
        if re.search(r'(\d{3,4})\s*мл', low) or "water" in low:
            facts.append("контроль воды — уже в фокусе.")
        if not facts:
            return ""
        return "• " + " ".join(facts)

# --- [PATCH] мини-план + CTA ---
async def send_plan(context: ContextTypes.DEFAULT_TYPE, uid: int, title: str, bullets: List[str], ctas: List[Tuple[str, str]]):
    lang = norm_lang(users_get(uid).get("lang") or "en")
    txt = (title + "\n" + "\n".join(f"• {b}" for b in bullets)).strip()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(lbl, callback_data=data) for (lbl, data) in ctas]])
    await maybe_send(context, uid, txt, kb, force=True, count=False)

async def send_sleep_plan(context: ContextTypes.DEFAULT_TYPE, uid: int):
    lang = norm_lang(users_get(uid).get("lang") or "en")
    title = "План сна на сегодня:" if lang!="en" else "Sleep plan for tonight:"
    bullets = {
        "ru": [
            "Экран-детокс 60 мин перед сном",
            "Лёгкий перекус за 3–4 ч до сна, кофе — до 14:00",
            "Утром 10–15 мин дневного света"
        ],
        "uk": [
            "Детокс екранів 60 хв перед сном",
            "Легкий перекус за 3–4 год до сну, кава — до 14:00",
            "Вранці 10–15 хв денного світла"
        ],
        "en": [
            "60-min screen detox before bed",
            "Light snack 3–4h before bed; caffeine by 2pm",
            "Morning light 10–15 min"
        ],
        "es": [
            "60 min sin pantallas antes de dormir",
            "Refrigerio 3–4h antes; café hasta las 14:00",
            "Luz de la mañana 10–15 min"
        ]
    }[lang]
    ctas = [
        ("⏰ Сегодня 22:30" if lang!="en" else "⏰ Tonight 22:30", "plan|sleep|rem2230"),
        ("🧘 60 сек. релаксация" if lang!="en" else "🧘 60s relax", "plan|sleep|relax"),
        ("👍 Всё понятно" if lang!="en" else "👍 All set", "plan|sleep|ok"),
    ]
    await send_plan(context, uid, title, bullets, ctas)

# --- [PATCH] лёгкое обновление некоторых текстов T[*] с {name} ---
def _inject_name_placeholders():
    for lang in list(T.keys()):
        for key in ["welcome", "daily_gm", "daily_pm", "thanks", "plan_header"]:
            try:
                s = T[lang].get(key, "")
                if s and "{name}" not in s:
                    if key == "welcome":
                        T[lang][key] = (("Привет, {name}! " if lang in ("ru","uk","es") else "Hi, {name}! ") + s)
                    elif key in ("daily_gm", "daily_pm"):
                        T[lang][key] = s + " {name}"
                    elif key == "plan_header":
                        T[lang][key] = s + " {name}"
                    else:
                        T[lang][key] = s.replace("🙌", "{name} 🙌")
            except Exception:
                pass

# --- [PATCH] расширим заголовки Users (Sheets) для name/pending_q при наличии Sheets ---
def _ensure_users_extra_columns():
    if not SHEETS_ENABLED:
        return
    try:
        hdr = _headers(ws_users)
        added = False
        for fld in ["name", "pending_q"]:
            if fld not in hdr:
                hdr.append(fld); added = True
        if added:
            end = gsu.rowcol_to_a1(1, len(hdr))
            ws_users.update(range_name=f"A1:{end}", values=[hdr])
    except Exception as e:
        logging.warning(f"users extra columns not ensured: {e}")

# Выполним патчи сразу при импорте этой части
try:
    _inject_name_placeholders()
    _ensure_users_extra_columns()
except Exception:
    pass

# --- /name ---
async def cmd_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None) or "en")
    args = (update.message.text or "").split(maxsplit=1)
    if len(args) == 2:
        name = sanitize_name(args[1])
        if not name:
            await update.message.reply_text("Пришлите имя буквами, напр.: /name Мария" if lang!="en" else "Send a valid name, e.g.: /name Alex")
            return
        set_name(uid, name)
        await update.message.reply_text(("Ок, буду обращаться: " if lang!="en" else "Okay, I’ll call you: ") + name)
        return
    # без аргумента — повторно спросим имя
    sessions.setdefault(uid, {})["awaiting_name"] = True
    await update.message.reply_text("Как к вам обращаться? Напишите имя одним сообщением." if lang!="en" else "How should I address you? Send your name in one message.")

# --- вспомогательные выводы/микроответы ---
async def _send_chip_text(q, domain: str, kind: str, lang: str):
    txt = chip_text(domain, kind, lang)
    if txt:
        await _send_or_edit_info(q, txt)

async def _handle_reminder_click(q, context, when_key: str, lang: str):
    uid = q.from_user.id
    rid = _schedule_oneoff(context.application, uid, when_key, lang)
    if rid:
        when = {"4h":"через 4 часа" if lang!="en" else "in 4h",
                "evening":"сегодня вечером" if lang!="en" else "this evening",
                "morning":"завтра утром" if lang!="en" else "tomorrow morning"}[when_key]
        await _reply_cbsafe(q, ("Готово, напомню " if lang!="en" else "Done, I’ll remind ") + when + ".")
    else:
        await _reply_cbsafe(q, "Сейчас не могу поставить напоминание." if lang!="en" else "I can’t set a reminder right now.")

# --- колбэки мини-плана сна ---
async def cb_remind_sleep_2230(q, context):
    uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    # ближайшие 22:30 локального времени
    when = _next_local_dt("22:30", int(str(users_get(uid).get("tz_offset") or "0")), base="auto")
    rid = reminder_add(uid, "🛌 Ложиться спать в 22:30", when)
    delay = max(5, (when - utcnow()).total_seconds())
    if _has_jq_app(context.application):
        context.application.job_queue.run_once(job_oneoff_reminder, when=delay, data={"user_id": uid, "reminder_id": rid})
    await _reply_cbsafe(q, "Готово, напомню в " + _fmt_local_when(uid, when) + ".")

async def cb_ok_plan(q, context):
    uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await _reply_cbsafe(q, "Принято. Утром спрошу, как прошло 🌙" if lang!="en" else "Got it. I’ll check in tomorrow morning 🌙")

# --- основной callback-router ---
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    parts = _parse_cb(data)
    uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")

    # anti-spam: снимаем флаг ожидания вопроса при любом клике
    clear_pending(uid)

    # intake:* обрабатывает плагин (зарегистрирован ранее); просто пропустим
    if data.startswith("intake:"):
        return

    # имя
    if parts[0] == "name":
        if parts[1] == "ask":
            sessions.setdefault(uid, {})["awaiting_name"] = True
            await _reply_cbsafe(q, "Напишите имя одним сообщением." if lang!="en" else "Send your name in one message.")
            return
        if parts[1] == "skip":
            sessions.setdefault(uid, {})["awaiting_name"] = False
            await _reply_cbsafe(q, "Ок, пропустим." if lang!="en" else "Okay, skipping.")
            return

    # согласие на фоллоу-ап
    if parts[0] == "consent":
        users_set(uid, "consent", "yes" if parts[1]=="yes" else "no")
        await _reply_cbsafe(q, T[lang]["thanks"])
        return

    # меню
    if parts[0] == "menu":
        key = parts[1]
        if key == "root":
            await _reply_cbsafe(q, T[lang]["m_menu_title"], inline_main_menu(lang)); return
        if key == "h60":
            await _reply_cbsafe(q, T[lang]["h60_intro"]); sessions.setdefault(uid,{})["awaiting_h60"]=True; return
        if key == "sym":
            await _reply_cbsafe(q, "Выберите симптом:" if lang!="en" else "Choose a symptom:", inline_symptoms_menu(lang)); return
        if key == "mini":
            await _reply_cbsafe(q, "Мини-планы:", inline_miniplans_menu(lang)); return
        if key == "care":
            await _reply_cbsafe(q, "Куда обратиться:", inline_findcare_menu(lang)); return
        if key == "hab":
            await _reply_cbsafe(q, "Быстрый лог привычек:", inline_habits_menu(lang)); return
        if key == "rem":
            kb = inline_remind(lang)
            await _reply_cbsafe(q, "Выберите напоминание:", kb); return
        if key == "lang":
            await _reply_cbsafe(q, "Язык / Language:", inline_lang_menu(lang)); return
        if key == "privacy":
            await _reply_cbsafe(q, T[lang]["privacy"]); return
        if key == "smart":
            await _reply_cbsafe(q, "Быстрый смарт-чек-ин:", inline_smart_checkin(lang)); return
        if key == "coming":
            await _reply_cbsafe(q, "Скоро ✨"); return

    # язык
    if parts[0] == "lang":
        new_lang = parts[1] or "en"
        users_set(uid, "lang", new_lang)
        await _reply_cbsafe(q, "Готово." if new_lang!="en" else "Done.")
        return

    # смарт-чек-ин и настроение
    if parts[0] == "mood":
        mood = parts[1]
        if mood == "note":
            sessions.setdefault(uid,{})["awaiting_daily_comment"]=True
            await _reply_cbsafe(q, T[lang]["fb_write"]); return
        daily_add(iso(utcnow()), uid, mood, "")
        await _reply_cbsafe(q, T[lang]["mood_thanks"]); return

    # отзывы
    if parts[0] == "fb":
        kind = parts[1]
        if kind == "up":
            feedback_add(iso(utcnow()), uid, "inline", q.from_user.username, "up", "")
            await _reply_cbsafe(q, T[lang]["fb_thanks"]); return
        if kind == "down":
            feedback_add(iso(utcnow()), uid, "inline", q.from_user.username, "down", "")
            await _reply_cbsafe(q, T[lang]["fb_thanks"]); return
        if kind == "text":
            sessions.setdefault(uid,{})["awaiting_free_feedback"]=True
            await _reply_cbsafe(q, T[lang]["fb_write"]); return

    # профиль (10 шагов)
    if parts[0] == "p":
        act = parts[1]; key = parts[2]
        if act == "choose":
            val = parts[3]
            profiles_upsert(uid, {key: val})
            sessions.setdefault(uid, {})[key] = val
            users_set(uid, "profile_banner_shown", "no")
            await _reply_cbsafe(q, T[lang]["saved_profile"] + f"{key}: {val}")
            await advance_profile_ctx(context, q.message.chat.id, lang, uid)
            return
        if act == "write":
            sessions.setdefault(uid, {})["p_wait_key"] = key
            await _reply_cbsafe(q, "Введите значение одним сообщением." if lang!="en" else "Type the value in one message.")
            return
        if act == "skip":
            await advance_profile_ctx(context, q.message.chat.id, lang, uid)
            return

    # темы/симптомы
    if parts[0] == "topic":
        t = parts[1]
        s = _set_session(uid)
        if t == "pain":
            s["topic"]="pain"; s["step"]=1; s["answers"]={}
            await _reply_cbsafe(q, T[lang]["triage_pain_q1"], _kb_for_code(lang, "painloc")); return
        if t == "sleep":
            # сразу отдаём мини-план — «завершение диалога»
            await send_sleep_plan(context, uid); return
        if t in ("nutrition","labs","habits","longevity","profile"):
            await _reply_cbsafe(q, "Ок, работаю с этим." if lang!="en" else "Okay, working on it.")
            return

    # пошаговый триаж боли
    if parts[0] in ("painloc","painkind","paindur","num","painrf"):
        s = _set_session(uid)
        ans = s.setdefault("answers", {})
        if parts[0] == "painloc":
            ans["loc"] = parts[1]; s["step"] = 2
            await _reply_cbsafe(q, T[lang]["triage_pain_q2"], _kb_for_code(lang, "painkind")); return
        if parts[0] == "painkind":
            ans["kind"] = parts[1]; s["step"] = 3
            await _reply_cbsafe(q, T[lang]["triage_pain_q3"], _kb_for_code(lang, "paindur")); return
        if parts[0] == "paindur":
            ans["dur"] = parts[1]; s["step"] = 4
            await _reply_cbsafe(q, T[lang]["triage_pain_q4"], _kb_for_code(lang, "num")); return
        if parts[0] == "num":
            ans["severity"] = parts[1]; s["step"] = 5
            await _reply_cbsafe(q, T[lang]["triage_pain_q5"], _kb_for_code(lang, "painrf")); return
        if parts[0] == "painrf":
            # итоговый план
            red = parts[1]
            prof = profiles_get(uid)
            plan_lines = pain_plan(lang, [red], prof)
            kb = inline_remind(lang)
            # создаём эпизод и запланируем чек-ин при выборе пользователем; базовый — просто вывести план
            await _reply_cbsafe(q, (T[lang]["plan_header"] + "\n" + "\n".join("• "+p for p in plan_lines)).replace("{name}", display_name(uid) or ""), kb)
            # сериализация эпизода
            try:
                sev = int(re.search(r'\d+', str(_set_session(uid)["answers"].get("severity","0"))).group(0))
            except Exception:
                sev = 0
            eid = episode_create(uid, f"pain:{ans.get('loc','')}", sev, red)
            sessions[uid]["episode_id"]=eid
            return

    # «чипы»/микроподсказки
    if parts[0] == "chip":
        domain, kind = parts[1], parts[2]
        await _send_chip_text(q, domain, kind, lang); return

    # быстрые действия
    if parts[0] == "act":
        sub = parts[1]
        if sub == "rem":
            when = parts[2]
            await _handle_reminder_click(q, context, when, lang); return
        if sub == "h60":
            sessions.setdefault(uid,{})["awaiting_h60"]=True
            await _reply_cbsafe(q, T[lang]["h60_intro"]); return
        if sub == "ex" and parts[2]=="neck":
            await _reply_cbsafe(q, microplan_text("neck", lang)); return
        if sub == "lab":
            sessions.setdefault(uid,{})["awaiting_city"]=True
            await _reply_cbsafe(q, T[lang]["act_city_prompt"]); return
        if sub == "er":
            await _reply_cbsafe(q, T[lang]["er_text"]); return

    # напоминалки из меню «Remind me»
    if parts[0] == "rem":
        when = parts[1]
        await _handle_reminder_click(q, context, when, lang); return

    # мини-план сна CTA
    if parts[0] == "plan" and parts[1] == "sleep":
        if parts[2] == "rem2230":
            await cb_remind_sleep_2230(q, context); return
        if parts[2] == "relax":
            txt = "Дыхание 4-7-8: вдох 4с — пауза 7с — выдох 8с ×4–6 раз." if lang!="en" else "Breathing 4-7-8: inhale 4s — hold 7s — exhale 8s ×4–6."
            await _reply_cbsafe(q, txt); return
        if parts[2] == "ok":
            await cb_ok_plan(q, context); return

    # gate / exit внутри pain
    if parts[0] == "pain" and parts[1] == "exit":
        sessions.pop(uid, None)
        await _reply_cbsafe(q, T[lang]["m_menu_title"], inline_main_menu(lang)); return

    # по умолчанию — просто показать главное меню
    await _reply_cbsafe(q, T[lang]["m_menu_title"], inline_main_menu(lang))
    return

# --- расширение приложения дополнительными хэндлерами ---
def extend_app_handlers(app):
    # Главный callback router
    app.add_handler(CallbackQueryHandler(on_callback))
    # /name
    app.add_handler(CommandHandler("name", cmd_name))

# --- entrypoint ---
def main():
    app = build_app()
    extend_app_handlers(app)
    app.run_polling()

if __name__ == "__main__":
    main()
