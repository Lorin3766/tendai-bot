# -*- coding: utf-8 -*-
# TendAI main.py — обновлено:
# - ежедневные напоминания: утро + ВЕЧЕР (/checkin_evening HH:MM)
# - восстановление расписаний на старте
# - клиппинг /settz в диапазон −12…+14
# - безопасные Google Sheets (headers, fallbacks), memory fallback
# - очистка /delete_data со снятием джобов
# - PRO-intake плагин (опционально)
# - меню, чипы, мини-планы, Youth-команды, мягкий фидбек и т.п.
# - ДОБАВЛЕНО (без удаления логики): имя пользователя, анти-спам вопросов, reflect_facts,
#   send_plan/send_sleep_plan, обёртка maybe_send с подстановкой {name}, /name.

# ⚠️ Эта часть — 1/2. В конце файла есть маркер «=== ЧАСТЬ 2 будет далее ===».

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
        # ⬇️ добавлено {name}
        "welcome": "Hi{comma} {name}! I’m TendAI — your health & longevity assistant.\nDescribe what’s bothering you; I’ll guide you. Let’s do a quick 40s intake to tailor advice."
                   .replace("{comma}", ","),  # аккуратная запятая перед именем
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
        # ⬇️ добавлено {name}
        "start_where":"Where do you want to start now, {name}? — or tap /menu",
        # ⬇️ добавлено {name}
        "daily_gm":"Good morning, {name}! Quick daily check-in:",
        "daily_pm":"Evening check-in, {name}: how was your day?",
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
        # ⬇️ добавлено {name}
        "plan_header":"Your 24–48h plan, {name}:",
        "plan_accept":"Will you try this today?",
        "accept_opts":["✅ Yes","🔁 Later","✖️ No"],
        "remind_when":"When shall I check on you?",
        "remind_opts":["in 4h","this evening","tomorrow morning","no need"],
        # ⬇️ добавлено {name}
        "thanks":"Got it, {name} 🙌",
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
        # ⬇️ добавлено {name}
        "welcome":"Привет, {name}! Я TendAI — ассистент здоровья и долголетия.\nРасскажи, что беспокоит; я подскажу. Сначала короткий опрос (~40с), чтобы советы были точнее.",
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
        # ⬇️ добавлено {name}
        "start_where":"С чего начнём, {name}? — или нажми /menu",
        # ⬇️ добавлено {name}
        "daily_gm":"Доброе утро, {name}! Быстрый чек-ин:",
        "daily_pm":"Вечерний чек-ин, {name}: как прошёл день?",
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
        # ⬇️ добавлено {name}
        "plan_header":"Ваш план на 24–48 часов, {name}:",
        "plan_accept":"Готовы попробовать сегодня?",
        "accept_opts":["✅ Да","🔁 Позже","✖️ Нет"],
        "remind_when":"Когда напомнить и спросить самочувствие?",
        "remind_opts":["через 4 часа","вечером","завтра утром","не надо"],
        # ⬇️ добавлено {name}
        "thanks":"Принято, {name} 🙌",
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
    "daily_pm":"Вечірній чек-ін, {name}: як пройшов день?",
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
    "last_fb_asked","profile_banner_shown","evening_hour"
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

# NEW: простое хранилище имени (без изменения схемы Sheets)
NAME_STORE: Dict[int, str] = {}

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

# ========= ✨ ДОБАВЛЕНО: утилиты имени =========
def sanitize_name(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"[\n\r\t]+", " ", s)
    s = re.sub(r"[<>{}\[\]\\/|^~`@#$%^&*=+_]+", "", s)  # убрать мусорные символы
    s = re.sub(r"\s{2,}", " ", s)
    s = s[:32]
    return s

def display_name(uid: int) -> str:
    # приоритет: sessions -> NAME_STORE -> username (без @)
    s = sessions.get(uid, {}).get("display_name") or NAME_STORE.get(uid, "")
    if s: return s
    u = users_get(uid)
    un = (u.get("username") or "").lstrip("@")
    return un

def set_name(uid: int, name: str):
    name = sanitize_name(name)
    sessions.setdefault(uid, {})["display_name"] = name
    NAME_STORE[uid] = name

async def ensure_ask_name(context: ContextTypes.DEFAULT_TYPE, chat_id: int, uid: int, lang: str) -> bool:
    """Если имя не задано — спросим один раз и вернём True (чтобы caller мог return)."""
    if display_name(uid):
        return False
    if sessions.get(uid, {}).get("awaiting_name"):
        return True
    sessions.setdefault(uid, {})["awaiting_name"] = True
    prompt = {
        "ru": "Как вас звать? Напишите коротко (например: Ирина).",
        "uk": "Як вас звати? Напишіть коротко (наприклад: Ірина).",
        "en": "What should I call you? One word is fine (e.g., Alex).",
        "es": "¿Cómo te llamas? Una palabra está bien (p. ej., Alex).",
    }[lang]
    await context.bot.send_message(chat_id, prompt)
    return True

async def try_handle_name_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, text: str, lang: str) -> bool:
    """Если мы ждали имя — сохранить и подтвердить, вернуть True (consume)."""
    if not sessions.get(uid, {}).get("awaiting_name"):
        return False
    sessions[uid]["awaiting_name"] = False
    name = sanitize_name(text)
    if len(name) < 2:
        again = {
            "ru":"Имя слишком короткое. Напишите, пожалуйста, ещё раз.",
            "uk":"Занадто коротке ім’я. Напишіть, будь ласка, ще раз.",
            "en":"That looks too short. Please send your name again.",
            "es":"Ese nombre es muy corto. Inténtalo de nuevo, por favor.",
        }[lang]
        sessions[uid]["awaiting_name"] = True
        await update.message.reply_text(again)
        return True
    set_name(uid, name)
    ok = {
        "ru": f"Приятно познакомиться, {name}! 😊",
        "uk": f"Приємно познайомитись, {name}! 😊",
        "en": f"Nice to meet you, {name}! 😊",
        "es": f"¡Encantado, {name}! 😊",
    }[lang]
    await update.message.reply_text(ok)
    return True

# ========= ✨ ДОБАВЛЕНО: «один вопрос за раз» =========
def clear_pending(uid: int):
    sessions.setdefault(uid, {})["pending_q"] = False

def is_question(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t: return False
    if "?" in t: return True
    # простые эвристики в разных языках
    kws = [
        "как", "когда", "почему", "зачем", "что делать", "нужно ли", "можно ли",
        "how", "when", "why", "should", "can", "what to do",
        "як", "коли", "чому",
        "¿", "qué", "cómo", "cuándo", "por qué"
    ]
    return any(k in t for k in kws)

async def ask_one(context: ContextTypes.DEFAULT_TYPE, uid: int, text: str, kb=None):
    """Явная отправка вопроса с отметкой pending."""
    sessions.setdefault(uid, {})["pending_q"] = True
    await context.bot.send_message(uid, text, reply_markup=kb)

# ========= ✨ ДОБАВЛЕНО: «зеркало фактов» =========
def reflect_facts(text: str, lang: str) -> str:
    t = (text or "").lower()
    # сон
    m = re.search(r"(сплю|sleep)\s*(\d{1,2})(?:[–-](\d{1,2}))?\s*час", t)
    if m:
        a = int(m.group(2)); b = int(m.group(3) or a)
        rng = f"{a}–{b}" if a != b else f"{a}"
        return {
            "ru": f"Понял: сон ~{rng} ч. Учту это в рекомендациях.",
            "uk": f"Зрозумів: сон ~{rng} год. Врахую далі.",
            "en": f"Got it: sleep ~{rng} h. I’ll factor this in.",
            "es": f"Entendido: sueño ~{rng} h. Lo tendré en cuenta.",
        }[lang]
    # стресс
    if any(k in t for k in ["стресс", "stress", "estres", "estrés"]):
        return {
            "ru":"Вижу: много стресса. Дам мягкие шаги без перегруза.",
            "uk":"Бачу: багато стресу. Дам м’які кроки без перевантаження.",
            "en":"Noted: high stress. I’ll keep tips gentle and doable.",
            "es":"Anotado: alto estrés. Mantendré pasos suaves y asumibles.",
        }[lang]
    # вода
    if any(k in t for k in ["мало пью", "не пью", "мало воды", "little water", "low water"]):
        return {
            "ru":"Понял: воды маловато. Предложу напоминание на воду, ок?",
            "uk":"Зрозумів: води замало. Запропоную нагадування про воду, гаразд?",
            "en":"Got it: low water intake. I can remind you to hydrate, ok?",
            "es":"Entendido: poca agua. Puedo recordarte hidratarte, ¿ok?",
        }[lang]
    return ""

# ========= ✨ ДОБАВЛЕНО: универсальный мини-план + план по сну =========
async def send_plan(context: ContextTypes.DEFAULT_TYPE, uid: int, lang: str, title: str, bullets: List[str], ctas: List[Tuple[str,str]]):
    body = f"{title}\n" + "\n".join([f"• {b}" for b in bullets])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(lbl, callback_data=data)] for (lbl, data) in ctas])
    await maybe_send(context, uid, body, kb=kb, force=True, count=False)

async def send_sleep_plan(context: ContextTypes.DEFAULT_TYPE, uid: int, lang: str):
    title = {
        "ru": "Мини-план сна (3 шага на сегодня):",
        "uk": "Міні-план сну (3 кроки на сьогодні):",
        "en": "Sleep mini-plan (3 steps for today):",
        "es": "Mini plan de sueño (3 pasos para hoy):",
    }[lang]
    bullets = {
        "ru": [
            "Экран-детокс 30–60 минут перед сном.",
            "Лёгкий перекус за 2–3 часа до сна (не поздно).",
            "Короткая релаксация 60 сек перед кроватью.",
        ],
        "uk": [
            "30–60 хв без екранів перед сном.",
            "Легкий перекус за 2–3 год до сну.",
            "Коротка релаксація 60 с перед ліжком.",
        ],
        "en": [
            "30–60 min screen-detox before bed.",
            "Light snack 2–3h before sleep (not late).",
            "60-sec relaxation just before bed.",
        ],
        "es": [
            "30–60 min sin pantallas antes de dormir.",
            "Snack ligero 2–3h antes de dormir.",
            "Relajación de 60 s antes de la cama.",
        ],
    }[lang]
    ctas = [
        ("⏰ Сегодня 22:30" if lang=="ru" else ("⏰ Сьогодні 22:30" if lang=="uk" else ("⏰ Today 22:30" if lang=="en" else "⏰ Hoy 22:30")), "plan|sleep|2230"),
        ("🧘 60 сек. релаксация" if lang!="en" else "🧘 60-sec relax", "plan|sleep|relax"),
        ("👍 Всё понятно" if lang=="ru" else ("👍 Все зрозуміло" if lang=="uk" else ("👍 Got it" if lang=="en" else "👍 Entendido")), "plan|ok"),
    ]
    await send_plan(context, uid, lang, title, bullets, ctas)

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

#  === ОРИГИНАЛЬНАЯ maybe_send ПЕРЕИМЕНОВАНА В _send_raw ===
async def _send_raw(context, uid, text, kb=None, *, force=False, count=True):
    if force or can_send(uid):
        try:
            await context.bot.send_message(uid, text, reply_markup=kb)
            if count:
                mark_sent(uid)
        except Exception as e:
            logging.error(f"send fail: {e}")

#  === ✨ НОВАЯ ОБЁРТКА maybe_send: {name} + «один вопрос за раз» ===
async def maybe_send(context, uid, text, kb=None, *, force=False, count=True):
    # подстановка имени
    name = display_name(uid) or ""
    safe_text = (text or "").replace("{name}", name)
    # анти-спам вопросов (без изменения существующих вызовов)
    if not force and is_question(safe_text) and sessions.get(uid, {}).get("pending_q"):
        # уже ждём ответ на предыдущий вопрос — не дублируем
        return
    if is_question(safe_text):
        sessions.setdefault(uid, {})["pending_q"] = True
    await _send_raw(context, uid, safe_text, kb=kb, force=force, count=count)

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
    lang = norm_lang(users_get(uid).get("lang") or "en")
    text = T[lang]["thanks"]
    for r in reminders_all_records():
        if r.get("id")==rid:
            text = r.get("text") or text; break
    # через maybe_send — чтобы сработала подстановка {name}
    try:
        await maybe_send(context, uid, text)
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

def inline_main_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["m_h60"], callback_data="menu|h60")],
        [InlineKeyboardButton(T[lang]["m_sym"], callback_data="menu|sym")],
        [InlineKeyboardButton(T[lang]["m_mini"], callback_data="menu|mini")],
        [InlineKeyboardButton(T[lang]["m_care"], callback_data="menu|care")],
        [InlineKeyboardButton(T[lang]["m_hab"], callback_data="menu|hab")],
        [InlineKeyboardButton(T[lang]["m_rem"], callback_data="menu|rem")],
        [InlineKeyboardButton(T[lang]["m_lang"], callback_data="menu|lang")],
        [InlineKeyboardButton(T[lang]["m_privacy"], callback_data="menu|privacy")],
        [InlineKeyboardButton(T[lang]["m_smart"], callback_data="menu|smart")],
        [InlineKeyboardButton(T[lang]["m_soon"], callback_data="menu|coming")]
    ])

def inline_symptoms_menu(lang: str) -> InlineKeyboardMarkup:
    labels = {"en":["Headache","Heartburn","Fatigue","Other"],
              "ru":["Головная боль","Изжога","Усталость","Другое"],
              "uk":["Головний біль","Печія","Втома","Інше"],
              "es":["Dolor de cabeza","Acidez","Fatiga","Otro"]}[lang]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(labels[0], callback_data="sym|headache"),
         InlineKeyboardButton(labels[1], callback_data="sym|heartburn")],
        [InlineKeyboardButton(labels[2], callback_data="sym|fatigue"),
         InlineKeyboardButton(labels[3], callback_data="sym|other")],
        [InlineKeyboardButton(T[lang]["back"], callback_data="menu|root")]
    ])

def inline_miniplans_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Neck pain 5-min" if lang=="en" else "Шея 5 мин", callback_data="mini|neck")],
        [InlineKeyboardButton("Sleep reset (3 nights)" if lang=="en" else "Сон-ресет (3 ночи)", callback_data="mini|sleepreset")],
        [InlineKeyboardButton("Heartburn: 3 steps" if lang=="en" else "Изжога: 3 шага", callback_data="mini|heartburn")],
        [InlineKeyboardButton("Hydration on hot days" if lang=="en" else "Гидратация в жару", callback_data="mini|hydration")],
        [InlineKeyboardButton(T[lang]["back"], callback_data="menu|root")]
    ])

def inline_findcare_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Labs near me" if lang=="en" else "Лабы рядом", callback_data="care|labsnear")],
        [InlineKeyboardButton("Urgent care" if lang=="en" else "Неотложка", callback_data="care|urgent")],
        [InlineKeyboardButton("Free clinics (NJ)" if lang=="en" else "Бесплатные клиники (NJ)", callback_data="care|free_nj")],
        [InlineKeyboardButton(T[lang]["back"], callback_data="menu|root")]
    ])

def inline_habits_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💧 Water",   callback_data="hab|water"),
         InlineKeyboardButton("🚶 Steps",   callback_data="hab|steps")],
        [InlineKeyboardButton("😴 Sleep",   callback_data="hab|sleep"),
         InlineKeyboardButton("🧠 Stress",  callback_data="hab|stress")],
        [InlineKeyboardButton("⚖️ Weight",  callback_data="hab|weight")],
        [InlineKeyboardButton(T[lang]["back"], callback_data="menu|root")]
    ])

def inline_lang_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("EN", callback_data="lang|en"),
         InlineKeyboardButton("RU", callback_data="lang|ru"),
         InlineKeyboardButton("UK", callback_data="lang|uk"),
         InlineKeyboardButton("ES", callback_data="lang|es")],
        [InlineKeyboardButton(T[lang]["back"], callback_data="menu|root")]
    ])

def inline_smart_checkin(lang: str) -> InlineKeyboardMarkup:
    lab = {"en":["I’m OK","Pain","Tired","Stressed","Heartburn","Other"],
           "ru":["Я ок","Боль","Устал","Стресс","Изжога","Другое"],
           "uk":["Все ок","Біль","Втома","Стрес","Печія","Інше"],
           "es":["Estoy bien","Dolor","Cansado","Estrés","Acidez","Otro"]}[lang]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lab[0], callback_data="smart|ok"),
         InlineKeyboardButton(lab[1], callback_data="smart|pain")],
        [InlineKeyboardButton(lab[2], callback_data="smart|tired"),
         InlineKeyboardButton(lab[3], callback_data="smart|stress")],
        [InlineKeyboardButton(lab[4], callback_data="smart|hb"),
         InlineKeyboardButton(lab[5], callback_data="smart|other")],
        [InlineKeyboardButton(T[lang]["back"], callback_data="menu|root")]
    ])

# ===== Контекстные чипы, микропланы и справки =====
def chips_for_text(text: str, lang: str) -> Optional[InlineKeyboardMarkup]:
    low = (text or "").lower()
    hb_kw = any(k in low for k in ["heartburn","burning after meals","изжог","жжёт","жжет","печія","кислота"])
    neck_kw = any(k in low for k in ["neck pain","neck","шея","затылок","ший"])
    if hb_kw:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Avoid triggers" if lang=="en" else "Избегать триггеры", callback_data="chip|hb|triggers")],
            [InlineKeyboardButton("OTC options", callback_data="chip|hb|otc")],
            [InlineKeyboardButton("When to see a doctor" if lang=="en" else "Когда к врачу", callback_data="chip|hb|red")]
        ])
    if neck_kw:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("5-min routine", callback_data="chip|neck|routine")],
            [InlineKeyboardButton("Heat/Ice tips" if lang=="en" else "Тепло/лёд", callback_data="chip|neck|heat")],
            [InlineKeyboardButton("Red flags", callback_data="chip|neck|red")]
        ])
    return None

def microplan_text(key: str, lang: str) -> str:
    if key=="neck":
        return {"ru":"Шея 5 мин:\n1) Медленные наклоны вперёд/назад ×5\n2) Повороты в стороны ×5\n3) Полукруги подбородком ×5\n4) Растяжка трапеций 2×20с.",
                "uk":"Шия 5 хв:\n1) Нахили вперед/назад ×5\n2) Повороти в сторони ×5\n3) Півкола підборіддям ×5\n4) Розтяжка трапецій 2×20с.",
                "en":"Neck 5-min:\n1) Slow flex/extend ×5\n2) Rotations L/R ×5\n3) Chin semicircles ×5\n4) Upper-trap stretch 2×20s.",
                "es":"Cuello 5 min:\n1) Flex/ext lenta ×5\n2) Giros izq/der ×5\n3) Semicírculos con barbilla ×5\n4) Estiramiento trapecio sup. 2×20s."}[lang]
    if key=="sleepreset":
        return {"ru":"Сон-ресет (3 ночи):\nН1: экран-детокс 60м + отбой фикс.\nН2: 15м вне кровати при пробуждениях.\nН3: свет утром 10–15м, кофе до 14:00.",
                "uk":"Сон-ресет (3 ночі):\nН1: детокс екранів 60 хв + фіксований відбій.\nН2: 15 хв поза ліжком при пробудженнях.\nН3: світло вранці 10–15 хв, кава до 14:00.",
                "en":"Sleep reset (3 nights):\nN1: 60-min screen detox + fixed bedtime.\nN2: 15-min out of bed if awake.\nN3: AM light 10–15m; caffeine by 2pm.",
                "es":"Reinicio del sueño (3 noches):\nN1: 60 min sin pantallas + hora fija.\nN2: 15 min fuera de la cama si despiertas.\nN3: Luz AM 10–15m; café hasta 14:00."}[lang]
    if key=="heartburn":
        return {"ru":"Изжога — 3 шага:\n1) Порции меньше, не ложиться 3ч после еды.\n2) Триггеры: жирное, алкоголь, мята, шоколад, кофе — убрать.\n3) OTC: антацид по инструкции 2–3 дня.",
                "uk":"Печія — 3 кроки:\n1) Менші порції, не лягати 3 год після їжі.\n2) Тригери: жирне, алкоголь, м’ята, шоколад, кава — прибрати.\n3) OTC: антацид за інстр. 2–3 дні.",
                "en":"Heartburn — 3 steps:\n1) Smaller meals; avoid lying 3h after.\n2) Remove triggers: fatty foods, alcohol, mint, chocolate, coffee.\n3) OTC antacid 2–3 days as directed.",
                "es":"Acidez — 3 pasos:\n1) Comidas pequeñas; no recostarse 3h.\n2) Evitar: grasas, alcohol, menta, chocolate, café.\n3) Antiácido OTC 2–3 días según etiqueta."}[lang]
    if key=="hydration":
        return {"ru":"Гидратация в жару:\nВода 200–300 мл каждый час активности; соль/электролиты при длительной жаре; светлая одежда и тень.",
                "uk":"Гідратація в спеку:\nВода 200–300 мл щогодини активності; електроліти за тривалої спеки; світлий одяг і тінь.",
                "en":"Hot-day hydration:\n200–300 ml water each active hour; add electrolytes if prolonged heat; light clothing & shade.",
                "es":"Hidratación en calor:\n200–300 ml de agua por hora activa; electrolitos si el calor es prolongado; ropa clara y sombra."}[lang]
    return ""

def chip_text(domain: str, kind: str, lang: str) -> str:
    if domain=="hb":
        if kind=="triggers":
            return {"ru":"Изжога — триггеры: жирное, острое, шоколад, кофе, цитрусы, мята, алкоголь. Последний приём пищи за 3 ч до сна.",
                    "uk":"Печія — тригери: жирне, гостре, шоколад, кава, цитрусові, м’ята, алкоголь. Останній прийом за 3 год до сну.",
                    "en":"Heartburn triggers: fatty/spicy foods, chocolate, coffee, citrus, mint, alcohol. Last meal ≥3h before bed.",
                    "es":"Desencadenantes: grasa/picante, chocolate, café, cítricos, menta, alcohol. Última comida ≥3h antes de dormir."}[lang]
        if kind=="otc":
            return {"ru":"OTC варианты при изжоге: антацид (альгиновая кислота/карбонаты), кратко 2–3 дня. Если часто повторяется — обсудить с врачом.",
                    "uk":"OTC варіанти: антацид (альгінати/карбонати) на 2–3 дні. Якщо часто — до лікаря.",
                    "en":"OTC: antacid (alginates/carbonates) for 2–3 days. If frequent — discuss with a clinician.",
                    "es":"OTC: antiácido (alginatos/carbonatos) 2–3 días. Si es frecuente, consulta médica."}[lang]
        if kind=="red":
            return {"ru":"Когда к врачу при изжоге: дисфагия, рвота кровью, чёрный стул, потеря веса, ночные боли, >2–3 нед несмотря на меры.",
                    "uk":"Коли до лікаря: дисфагія, блювання кровʼю, чорний стілець, втрата ваги, нічний біль, >2–3 тиж попри заходи.",
                    "en":"See a doctor if: trouble swallowing, vomiting blood, black stools, weight loss, nocturnal pain, >2–3 weeks despite measures.",
                    "es":"Acude al médico si: disfagia, vómito con sangre, heces negras, pérdida de peso, dolor nocturno, >2–3 semanas pese a medidas."}[lang]
    if domain=="neck":
        if kind=="routine":
            return microplan_text("neck", lang)
        if kind=="heat":
            return {"ru":"Шея: первые 48 ч лучше холод 10–15 мин ×2–3/д; затем тепло для расслабления; лёгкая растяжка без боли.",
                    "uk":"Шия: перші 48 год — холод 10–15 хв ×2–3/д; далі тепло; легка розтяжка без болю.",
                    "en":"Neck: first 48h prefer ice 10–15 min ×2–3/day, then heat for relaxation; gentle stretch without pain.",
                    "es":"Cuello: primeras 48h hielo 10–15 min ×2–3/día, luego calor; estiramientos suaves sin dolor."}[lang]
        if kind=="red":
            return {"ru":"Красные флаги: слабость рук, онемение, травма, лихорадка, боль >7/10, быстро прогрессирует — к врачу/неотложке.",
                    "uk":"Червоні прапори: слабкість рук, оніміння, травма, гарячка, біль >7/10, прогресія — до лікаря/невідкладної.",
                    "en":"Red flags: arm weakness/numbness, trauma, fever, pain >7/10, rapid progression — seek care.",
                    "es":"Banderas rojas: debilidad/entumecimiento en brazos, trauma, fiebre, dolor >7/10, progresión rápida — atención médica."}[lang]
    return ""

def care_links(kind: str, lang: str, city_hint: Optional[str]=None) -> str:
    if kind=="labsnear":
        q = "labs near me" if lang=="en" else "лаборатории рядом"
        return f"🔗 Google Maps: https://www.google.com/maps/search/{q.replace(' ','+')}"
    if kind=="urgent":
        q = "urgent care near me" if lang=="en" else "неотложка рядом"
        return f"🔗 Google Maps: https://www.google.com/maps/search/{q.replace(' ','+')}"
    if kind=="free_nj":
        return "🔗 Free clinics NJ: https://www.google.com/maps/search/free+clinic+New+Jersey"
    return ""

# ===== Youth-команды =====
async def cmd_energy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    tips = {
      "en": ["1) 10-min brisk walk now (raise pulse).","2) 300–500 ml water + light protein.","3) 20-min screen detox to refresh focus."],
      "ru": ["1) Быстрая ходьба 10 мин.","2) 300–500 мл воды + лёгкий белок.","3) 20 мин без экрана — разгрузка внимания."],
      "uk": ["1) Швидка ходьба 10 хв.","2) 300–500 мл води + легкий білок.","3) 20 хв без екрана — перезавантаження уваги."],
      "es": ["1) Camina rápido 10 min.","2) 300–500 ml de agua + proteína ligera.","3) 20 min sin pantallas."]
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

# === ПРАВКА 3: команда быстрого самотеста JobQueue (/test_in) ===
async def cmd_test_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async def _ping(ctx):
        try:
            await ctx.bot.send_message(uid, "✅ TEST: JobQueue OK (30s).")
        except Exception as e:
            logging.error(f"test_in send error: {e}")
    if _has_jq_ctx(context):
        context.application.job_queue.run_once(
            lambda c: context.application.create_task(_ping(c)),
            when=30
        )
        await update.message.reply_text("⏱️ Test scheduled in 30s.")
    else:
        await update.message.reply_text("❌ JobQueue unavailable.")

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
    # ... (без изменений; список шагов остаётся как в твоём коде)
]

# (остальной код профиля — без изменений, см. оригинал)

# ===== РЕНДЕР ГЛАВНОГО МЕНЮ =====
async def render_main_menu(update_or_cb: Update, context: ContextTypes.DEFAULT_TYPE):
    if update_or_cb.callback_query:
        chat_id = update_or_cb.callback_query.message.chat.id
        uid = update_or_cb.callback_query.from_user.id
    else:
        chat_id = update_or_cb.effective_chat.id
        uid = update_or_cb.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update_or_cb.effective_user, "language_code", None) or "en")
    await context.bot.send_message(chat_id, f"{T[lang]['m_menu_title']}", reply_markup=inline_main_menu(lang))

# ===== Основной текстовый обработчик =====
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    text = (update.message.text or "").strip()
    # ✨ новый шаг: сброс pending-вопроса
    clear_pending(uid)

    logging.info(f"INCOMING uid={uid} text={text[:200]}")
    urec = users_get(uid)

    # ✨ если сейчас ждём имя — обработаем и остановимся
    lang_guess = norm_lang(getattr(user, "language_code", None) or "en")
    if await try_handle_name_reply(update, context, uid, text, lang_guess):
        return

    # первый заход
    if not urec:
        lang_guess = detect_lang_from_text(text, norm_lang(getattr(user, "language_code", None)))
        users_upsert(uid, user.username or "", lang_guess)
        sessions.setdefault(uid, {})["last_user_text"] = text
        # через maybe_send — чтобы подставился {name} (пока пустой, ок)
        await maybe_send(context, uid, T[lang_guess]["welcome"])
        await update.message.reply_text(T[lang_guess]["m_menu_title"], reply_markup=inline_main_menu(lang_guess))
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

    # ✨ «зеркало фактов» — мягкая персональная строка, без счётчика и лимитера
    fact = reflect_facts(text, lang)
    if fact:
        await maybe_send(context, uid, fact, force=True, count=False)

    sc = detect_serious(text)
    if sc:
        sessions.setdefault(uid,{})["mode"] = "serious"
        sessions[uid]["serious_condition"] = sc
        prof = profiles_get(uid)
        plan = pain_plan(lang, [], prof)
        await maybe_send(context, uid, "\n".join(plan), kb=inline_actions(lang))
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

    # ожидание веса (без изменений)
    # ...

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
        chips = chips_for_text(text, lang)
        if chips:
            await update.message.reply_text(T[lang]["chips_hb"] if "hb" in str(chips.inline_keyboard[0][0].callback_data) else T[lang]["chips_neck"], reply_markup=chips)
        ask_feedback_soft(uid, context, lang)
        return

    # ... (остальная логика on_text без изменений; ниже ещё вставки в ЧАСТИ 2)

# ===== Build & run (команды и планировщики) =====
async def post_init(app):
    me = await app.bot.get_me()
    logging.info(f"BOT READY: @{me.username} (id={me.id})")
    # ВАЖНО: восстановим все сохранённые напоминания/чек-ины из Sheets/памяти
    schedule_from_sheet_on_start(app)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = norm_lang(getattr(user, "language_code", None))
    users_upsert(user.id, user.username or "", lang)
    context.user_data["lang"] = lang
    sessions.setdefault(user.id, {})["last_user_text"] = "/start"
    # через maybe_send, чтобы подставить {name}
    await maybe_send(context, user.id, T[lang]["welcome"])
    prof = profiles_get(user.id)
    if prof and should_show_profile_banner(user.id):
        await update.message.reply_text(profile_banner(lang, prof))
        users_set(user.id, "profile_banner_shown", "yes")
    await update.message.reply_text(T[lang]["m_menu_title"], reply_markup=inline_main_menu(lang))
    if not profiles_get(user.id) and not context.user_data.get(GATE_FLAG_KEY):
        await gate_show(update, context)
    u = users_get(user.id)
    if (u.get("consent") or "").lower() not in {"yes","no"}:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["yes"], callback_data="consent|yes"),
                                    InlineKeyboardButton(T[lang]["no"],  callback_data="consent|no")]])
        await update.message.reply_text(T[lang]["ask_consent"], reply_markup=kb)
    tz_off = int(str(u.get("tz_offset") or "0"))
    hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, user.id, tz_off, hhmm, lang)  # утро
        schedule_morning_evening(context.application, user.id, tz_off, lang)     # вечер
    else:
        logging.warning("JobQueue not available on /start – daily check-ins not scheduled.")
    # ✨ спросим имя ОДИН РАЗ и выйдем
    if await ensure_ask_name(context, update.effective_chat.id, user.id, lang):
        return

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await update.message.reply_text(T[lang]["help"])

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await update.message.reply_text(T[lang]["privacy"])

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await render_main_menu(update, context)

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "yes")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text(T[lang]["paused_on"])

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "no")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text(T[lang]["paused_off"])

# *** /delete_data: (без изменений)
# ... (как в твоём коде)

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None))
    await start_profile_ctx(context, update.effective_chat.id, lang, uid)

# *** /settz, /checkin_on, /checkin_evening, /checkin_off — (без изменений в логике)
# ... (как в твоём коде)

# ✨ НОВОЕ: /name — установить/изменить отображаемое имя
async def cmd_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        name = sanitize_name(parts[1])
        if len(name) < 2:
            msg = {
                "ru":"Имя слишком короткое. Напишите /name Имя",
                "uk":"Занадто коротке ім’я. Напишіть /name Ім’я",
                "en":"That name looks too short. Use /name Alex",
                "es":"Ese nombre es muy corto. Usa /name Alex",
            }[lang]
            await update.message.reply_text(msg); return
        set_name(uid, name)
        ok = {
            "ru": f"Готово! Буду звать вас: {name}",
            "uk": f"Готово! Звертатимусь: {name}",
            "en": f"Done! I’ll call you: {name}",
            "es": f"¡Hecho! Te llamaré: {name}",
        }[lang]
        await update.message.reply_text(ok)
    else:
        # Запустим диалог ввода имени
        sessions.setdefault(uid, {})["awaiting_name"] = True
        prompt = {
            "ru":"Как вас звать? Напишите одним словом (например: Ирина).",
            "uk":"Як вас звати? Напишіть одним словом (наприклад: Ірина).",
            "en":"What should I call you? One word is fine (e.g., Alex).",
            "es":"¿Cómo te llamas? Una palabra está bien (p. ej., Alex).",
        }[lang]
        await update.message.reply_text(prompt)

def build_app() -> "Application":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    try:
        register_intake_pro(app, GSPREAD_CLIENT, on_complete_cb=_ipro_save_to_sheets_and_open_menu)
        logging.info("Intake Pro registered.")
    except Exception as e:
        logging.warning(f"Intake Pro registration failed: {e}")
    # Commands
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("privacy",      cmd_privacy))
    app.add_handler(CommandHandler("menu",         cmd_menu))
    app.add_handler(CommandHandler("pause",        cmd_pause))
    app.add_handler(CommandHandler("resume",       cmd_resume))
    app.add_handler(CommandHandler("delete_data",  cmd_delete_data))
    app.add_handler(CommandHandler("profile",      cmd_profile))
    app.add_handler(CommandHandler("settz",        cmd_settz))
    app.add_handler(CommandHandler("checkin_on",   cmd_checkin_on))
    app.add_handler(CommandHandler("checkin_evening", cmd_checkin_evening))
    app.add_handler(CommandHandler("checkin_off",  cmd_checkin_off))
    app.add_handler(CommandHandler("health60",     cmd_health60))
    app.add_handler(CommandHandler("intake",       cmd_intake))
    # Youth
    app.add_handler(CommandHandler("energy",       cmd_energy))
    app.add_handler(CommandHandler("mood",         cmd_mood))
    app.add_handler(CommandHandler("water",        cmd_water))
    app.add_handler(CommandHandler("skin",         cmd_skin))
    # Самотест JobQueue
    app.add_handler(CommandHandler("test_in",      cmd_test_in))
    # Языки
    app.add_handler(CommandHandler("ru", lambda u,c: users_set(u.effective_user.id,"lang","ru") or u.message.reply_text("Ок, дальше отвечаю по-русски.")))
    app.add_handler(CommandHandler("en", lambda u,c: users_set(u.effective_user.id,"lang","en")  or u.message.reply_text("OK, I’ll reply in English.")))
    app.add_handler(CommandHandler("uk", lambda u,c: users_set(u.effective_user.id,"lang","uk")  or u.message.reply_text("Ок, надалі відповідатиму українською.")))
    app.add_handler(CommandHandler("es", lambda u,c: users_set(u.effective_user.id,"lang","es")  or u.message.reply_text("De acuerdo, responderé en español.")))
    # ✨ Имя
    app.add_handler(CommandHandler("name",         cmd_name))
    # Gate & callbacks
    app.add_handler(CallbackQueryHandler(gate_cb, pattern=r"^gate:"))
    # Главный CallbackQueryHandler(on_callback) — подключу в ЧАСТИ 2.
    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.bot_data["render_menu_cb"] = render_main_menu
    return app

# =========================
# === ЧАСТЬ 2 будет далее ==
# =========================
# В ЧАСТИ 2 я продолжу ровно с этого места:
# - оставшаяся часть on_text (без изменений ядра + точечная вставка send_sleep_plan)
# - on_callback (в самом начале clear_pending(uid))
# - обработчики коллбеков: remind_sleep_2230, ok_plan (+ relax подсказка)
# - _to_local / _fmt_local_* (уже есть выше), next_evening_dt/next_morning_dt (уже есть выше)
# - _schedule_oneoff (уже есть) с использованием в remind_sleep_2230
# - entrypoint run_polling
# =========================
# ======= ЧАСТЬ 2 =========
# =========================
# Доп. утилиты ИМЕНИ, anti-спам вопросов, «зеркало фактов»,
# мини-план сна + CTA, callback-router, прехуки и entrypoint.
# Всё вставлено БЕЗ правок существующих функций — только «вклейки».

from telegram.ext import ApplicationHandlerStop

# -------- Имя пользователя: хранение в сессии (не трогаем Sheets) -------

def sanitize_name(raw: str) -> str:
    s = (raw or "").strip()
    # берём только буквы/пробел/дефис, срезаем длину
    s = re.sub(r"[^A-Za-zА-Яа-яЁёІіЇїЄєҐґ\-'\s]", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    # Первая буква заглавная, остальное как есть
    return s[:32].title()

def set_name(uid: int, name: str) -> None:
    s = sessions.setdefault(uid, {})
    s["name"] = sanitize_name(name)
    s["asked_name"] = True  # чтобы не спрашивать снова

def display_name(uid: int) -> str:
    s = sessions.get(uid, {})
    if s.get("name"):
        return s["name"]
    # fallback: Telegram username без @
    u = users_get(uid)
    nick = (u.get("username") or "").strip().lstrip("@")
    return nick

def ensure_ask_name(uid: int, lang: str) -> bool:
    """
    Если имя ещё не спрашивали и его нет — спрашиваем ОДИН раз.
    Возвращает True, если отправили вопрос (чтобы прервать дальнейшую обработку).
    """
    s = sessions.setdefault(uid, {})
    if s.get("asked_name") or s.get("name"):
        return False
    prompt = "Как к вам обращаться? Напишите имя одной строкой." if lang != "en" else "How should I address you? Please send your first name."
    # отметим, что спрашивали
    s["awaiting_name"] = True
    s["asked_name"] = True
    # используем обёртку maybe_send (с анти-спам и подстановкой)
    # здесь force=True, чтобы гарантированно ушёл вопрос
    context = None  # будет подставлен в прехуках; здесь оставим заглушку
    return True  # сам вывод сделаем в прехуке, где есть context

def try_handle_name_reply(uid: int, text: str, lang: str) -> Optional[str]:
    """
    Если ждём имя — сохранить и вернуть текст-подтверждение (или None, если это не имя).
    """
    s = sessions.setdefault(uid, {})
    if not s.get("awaiting_name"):
        return None
    cand = sanitize_name(text)
    if len(cand) < 2:
        return "Имя слишком короткое. Напишите, пожалуйста, как к вам обращаться (например: Анна)." if lang != "en" else "That looks too short. Please send your first name (e.g., Anna)."
    set_name(uid, cand)
    s["awaiting_name"] = False
    return f"Отлично, {cand}! Буду использовать ваше имя." if lang != "en" else f"Great, {cand}! I’ll use this name."

# -------- Anti-spam вопросов («один вопрос за раз») --------

def clear_pending(uid: int) -> None:
    sessions.setdefault(uid, {}).pop("pending_q", None)

def is_question(text: str) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    if "?" in t:
        return True
    q_kw = [
        "когда", "где", "зачем", "почему", "как", "сколько", "что", "можно ли",
        "when", "where", "why", "how", "how much", "what", "can i", "should i"
    ]
    return any(t.startswith(w) or f"{w} " in t for w in q_kw)

# Сохраним оригинальную maybe_send и обернём её
_send_raw_ref = maybe_send

async def maybe_send(context, uid, text, kb=None, *, force=False, count=True):
    lang = norm_lang(users_get(uid).get("lang") or "en")
    name = display_name(uid) or ""

    # Адресное обращение: если есть {name} — подставим;
    # иначе для ключевых коротких фраз добавим имя в начало.
    if "{name}" in (text or ""):
        text = (text or "").replace("{name}", name)
    else:
        key_variants = {
            T[lang]["daily_gm"],
            T[lang]["daily_pm"],
            T[lang]["thanks"],
        }
        if name and (text or "").strip() in key_variants:
            text = f"{name}, {text}"

    # «Один вопрос за раз»: если уже ждём ответ на вопрос и это снова вопрос — не шлём.
    if not force and is_question(text) and sessions.setdefault(uid, {}).get("pending_q"):
        return

    # Если отправляем вопрос — ставим флаг pending_q
    if is_question(text):
        sessions.setdefault(uid, {})["pending_q"] = True

    # Отправка оригинальной функцией
    await _send_raw_ref(context, uid, text, kb, force=force, count=count)

# -------- «Зеркало фактов» (лёгкая персонализация) --------

def reflect_facts(text: str, lang: str = "ru") -> str:
    t = (text or "").lower()
    # простые паттерны
    m_sleep = re.search(r"(сплю|сон|sleep).{0,8}(\d{1,2})[–\-—/]?(\d{1,2})?\s*час", t)
    if m_sleep:
        a = m_sleep.group(2); b = m_sleep.group(3)
        span = f"{a}–{b}" if b else a
        return f"Запомнил: сон {span} часов — учту в советах." if lang != "en" else f"Got it: you sleep ~{span} h — I’ll factor this in."
    if any(w in t for w in ["стресс", "перегораю", "burnout", "stress", "anxious"]):
        return "Вижу высокий стресс — буду мягче и короче в шагах." if lang != "en" else "I see high stress — I’ll keep steps gentle and short."
    if any(w in t for w in ["кофе", "coffee"]):
        return "Учту кофе — подскажу лимит по времени и дозе." if lang != "en" else "Noted coffee — I’ll suggest time & dose limits."
    if any(w in t for w in ["вода", "мало пью", "hydrate", "dehydrated"]):
        return "Недостаток воды — добавлю простой план гидратации." if lang != "en" else "Low hydration — I’ll add a simple hydration plan."
    return ""

# -------- Универсальный мини-план + частный для сна --------

async def send_plan(context, uid: int, lang: str, title: str, bullets: list[str], ctas: list[tuple[str, str]]):
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(lbl, callback_data=data)] for (lbl, data) in ctas]
    )
    body = f"{title}\n• " + "\n• ".join(bullets)
    await maybe_send(context, uid, body, kb=kb, force=True, count=False)

async def send_sleep_plan(context, uid: int, lang: str):
    title = "План на сон (минимум шума) 🛏️" if lang != "en" else "Sleep mini-plan 🛏️"
    bullets = [
        "Экран-детокс 30–60 мин перед сном" if lang != "en" else "30–60 min screen detox",
        "Лёгкая релаксация 60 сек" if lang != "en" else "60-sec relaxation",
        "Фиксируем отбой сегодня (22:30)" if lang != "en" else "Set bedtime today (22:30)",
    ]
    ctas = [
        ("⏰ Сегодня 22:30", "plan|sleep|2230"),
        ("🧘 60 сек. релаксация", "plan|sleep|relax"),
        ("👍 Всё понятно", "plan|ok"),
    ] if lang != "en" else [
        ("⏰ Today 22:30", "plan|sleep|2230"),
        ("🧘 60-sec relax", "plan|sleep|relax"),
        ("👍 All good", "plan|ok"),
    ]
    await send_plan(context, uid, lang, title, bullets, ctas)

def _next_local_2230(uid: int) -> datetime:
    tz_off = int(str(users_get(uid).get("tz_offset") or "0"))
    return _next_local_dt("22:30", tz_off, base="auto")

# -------- Callback-router --------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(q.from_user, "language_code", None) or "en")

    # снимаем флаг «ждём ответ на вопрос»
    clear_pending(uid)

    # Вспомогалки: короткие редакторы/ответчики
    async def _ok(txt: str, kb=None):
        await _reply_cbsafe(q, txt, kb)

    try:
        # ===== Согласие на напоминания =====
        if data.startswith("consent|"):
            val = data.split("|", 1)[1]
            users_set(uid, "consent", "yes" if val == "yes" else "no")
            await _ok(T[lang]["thanks"])
            return

        # ===== Профиль 10 шагов =====
        if data.startswith("p|choose|"):
            _, _, key, val = _parse_cb(data)
            profiles_upsert(uid, {key: val})
            sessions.setdefault(uid, {})[key] = val
            await advance_profile_ctx(context, q.message.chat.id, lang, uid)
            await q.answer()
            return
        if data.startswith("p|write|"):
            _, _, key = data.split("|", 2)
            sessions.setdefault(uid, {})["p_wait_key"] = key
            await _ok(T[lang]["write"])
            await q.answer()
            return
        if data.startswith("p|skip|"):
            await advance_profile_ctx(context, q.message.chat.id, lang, uid)
            await q.answer()
            return

        # ===== Меню =====
        if data == "menu|root":
            await render_main_menu(update, context); await q.answer(); return
        if data == "menu|h60":
            await _ok(T[lang]["h60_intro"]); sessions.setdefault(uid, {})["awaiting_h60"] = True; await q.answer(); return
        if data == "menu|sym":
            await _ok(T[lang]["m_menu_title"], kb=inline_symptoms_menu(lang)); await q.answer(); return
        if data == "menu|mini":
            await _ok(T[lang]["m_menu_title"], kb=inline_miniplans_menu(lang)); await q.answer(); return
        if data == "menu|care":
            await _ok(T[lang]["m_menu_title"], kb=inline_findcare_menu(lang)); await q.answer(); return
        if data == "menu|hab":
            await _ok(T[lang]["m_menu_title"], kb=inline_habits_menu(lang)); await q.answer(); return
        if data == "menu|rem":
            await _ok("Выберите напоминание:" if lang!="en" else "Pick a reminder:", kb=inline_remind(lang)); await q.answer(); return
        if data == "menu|lang":
            await _ok("Язык / Language:", kb=inline_lang_menu(lang)); await q.answer(); return
        if data == "menu|privacy":
            await _ok(T[lang]["privacy"]); await q.answer(); return
        if data == "menu|smart":
            await _ok("Как сейчас?" if lang!="en" else "How are you now?", kb=inline_smart_checkin(lang)); await q.answer(); return
        if data == "menu|coming":
            await _ok(T[lang]["m_soon"]); await q.answer(); return

        # ===== Язык =====
        if data.startswith("lang|"):
            _, lang_code = data.split("|", 1)
            users_set(uid, "lang", norm_lang(lang_code))
            await _ok("Готово. Обновил язык." if lang_code!="en" else "Done. Language updated.")
            await q.answer(); return

        # ===== Симптомы из меню =====
        if data.startswith("sym|"):
            topic = data.split("|",1)[1]
            if topic == "headache":
                await _ok(microplan_text("neck", lang), kb=inline_actions(lang))
            elif topic == "heartburn":
                await _ok(microplan_text("heartburn", lang), kb=inline_actions(lang))
            elif topic == "fatigue":
                await send_sleep_plan(context, uid, lang);  # мини-план сна
            else:
                await _ok(T[lang]["unknown"])
            await q.answer(); return

        # ===== Smart-чек-ин =====
        if data.startswith("smart|"):
            kind = data.split("|",1)[1]
            if kind == "ok":
                await _ok(T[lang]["mood_thanks"])
            elif kind == "pain":
                # старт триажа боли
                s = sessions.setdefault(uid, {"topic":"pain","step":1,"answers":{}})
                s["topic"]="pain"; s["step"]=1; s["answers"]={}
                await _ok(T[lang]["triage_pain_q1"], kb=_kb_for_code(lang, "painloc"))
            elif kind == "hb":
                await _ok(microplan_text("heartburn", lang), kb=inline_actions(lang))
            elif kind in {"tired","stress"}:
                await send_sleep_plan(context, uid, lang)
            else:
                await _ok(T[lang]["unknown"])
            await q.answer(); return

        # ===== Чипы/микро-советы =====
        if data.startswith("chip|"):
            _, domain, kind = data.split("|", 2)
            await _ok(chip_text(domain, kind, lang)); await q.answer(); return

        # ===== Действия =====
        if data.startswith("act|rem|"):
            when_key = data.split("|", 2)[2]
            rid = _schedule_oneoff(context.application, uid, when_key, lang)
            if rid:
                when = {"4h": utcnow()+timedelta(hours=4), "evening": next_evening_dt(uid), "morning": next_morning_dt(uid)}[when_key]
                await _ok(("Ок, напомню " if lang!="en" else "Okay, I’ll remind ") + _fmt_local_when(uid, when))
            else:
                await _ok("Не удалось поставить напоминание." if lang!="en" else "Failed to schedule reminder.")
            await q.answer(); return

        if data == "act|h60":
            sessions.setdefault(uid, {})["awaiting_h60"] = True
            await _ok(T[lang]["h60_intro"]); await q.answer(); return

        if data == "act|ex|neck":
            await _ok(microplan_text("neck", lang)); await q.answer(); return

        if data == "act|lab":
            sessions.setdefault(uid, {})["awaiting_city"] = True
            await _ok(T[lang]["act_city_prompt"]); await q.answer(); return

        if data == "act|er":
            await _ok(T[lang]["er_text"]); await q.answer(); return

        # ===== Напоминания из меню =====
        if data.startswith("rem|"):
            when_key = data.split("|",1)[1]
            rid = _schedule_oneoff(context.application, uid, when_key, lang)
            if rid:
                when = {"4h": utcnow()+timedelta(hours=4), "evening": next_evening_dt(uid), "morning": next_morning_dt(uid)}[when_key]
                await _ok(("Ок, напомню " if lang!="en" else "Okay, I’ll remind ") + _fmt_local_when(uid, when))
            else:
                await _ok("Не удалось поставить напоминание." if lang!="en" else "Failed to schedule reminder.")
            await q.answer(); return

        # ===== Pain triage (кнопки) =====
        if data == "pain|exit":
            sessions.pop(uid, None)
            await _ok(T[lang]["m_menu_title"], kb=inline_main_menu(lang)); await q.answer(); return

        if data.startswith("painloc|"):
            s = sessions.setdefault(uid, {"topic":"pain","step":1,"answers":{}})
            s["topic"]="pain"; s["step"]=2; s["answers"]["loc"]=data.split("|",1)[1]
            await _ok(T[lang]["triage_pain_q2"], kb=_kb_for_code(lang, "painkind")); await q.answer(); return

        if data.startswith("painkind|"):
            s = sessions.setdefault(uid, {"topic":"pain","step":2,"answers":{}})
            s["topic"]="pain"; s["step"]=3; s["answers"]["kind"]=data.split("|",1)[1]
            await _ok(T[lang]["triage_pain_q3"], kb=_kb_for_code(lang, "paindur")); await q.answer(); return

        if data.startswith("paindur|"):
            s = sessions.setdefault(uid, {"topic":"pain","step":3,"answers":{}})
            s["topic"]="pain"; s["step"]=4; s["answers"]["dur"]=data.split("|",1)[1]
            await _ok(T[lang]["triage_pain_q4"], kb=_kb_for_code(lang, "num")); await q.answer(); return

        if data.startswith("num|"):
            # если это шаг триажа — сохраняем как severity
            s = sessions.setdefault(uid, {})
            if s.get("topic") == "pain" and s.get("step") == 4:
                s["answers"]["severity"] = int(data.split("|",1)[1])
                s["step"] = 5
                await _ok(T[lang]["triage_pain_q5"], kb=_kb_for_code(lang, "painrf"))
                await q.answer(); return
            # иначе — трактуем как ответ на быструю шкалу (например, смарт-чек-ин)
            await _ok(T[lang]["thanks"]); await q.answer(); return

        if data.startswith("painrf|"):
            s = sessions.setdefault(uid, {"topic":"pain","step":5,"answers":{}})
            s["answers"]["rf"] = data.split("|",1)[1]
            sev = int(s["answers"].get("severity", 5))
            red = s["answers"].get("rf","")
            prof = profiles_get(uid)
            plan = pain_plan(lang, [red], prof)
            eid = episode_create(uid, "pain", sev, red)
            # План + быстрые действия
            await _ok(T[lang]["plan_header"] + "\n" + "\n".join(plan), kb=inline_actions(lang))
            await q.answer(); return

        if data.startswith("acc|"):
            choice = data.split("|",1)[1]
            ep = episode_find_open(uid)
            if ep:
                episode_set(ep["episode_id"], "plan_accepted", "1" if choice=="yes" else "0")
                # простая логика: если согласился — спросим вечером; иначе — завтра утром
                when_key = "evening" if choice=="yes" else "morning"
                rid = _schedule_oneoff(context.application, uid, when_key, lang, text=T[lang]["checkin_ping"])
                if rid:
                    when = next_evening_dt(uid) if when_key=="evening" else next_morning_dt(uid)
                    await _ok(("Ок, проверю " if lang!="en" else "Got it, I’ll check ") + _fmt_local_when(uid, when))
                else:
                    await _ok(T[lang]["thanks"])
            else:
                await _ok(T[lang]["thanks"])
            await q.answer(); return

        # ===== Обратная связь =====
        if data == "fb|up":
            feedback_add(iso(utcnow()), uid, "inline", q.from_user.username, "up", "")
            await _ok(T[lang]["fb_thanks"]); await q.answer(); return
        if data == "fb|down":
            feedback_add(iso(utcnow()), uid, "inline", q.from_user.username, "down", "")
            await _ok(T[lang]["fb_thanks"]); await q.answer(); return
        if data == "fb|text":
            sessions.setdefault(uid, {})["awaiting_free_feedback"] = True
            await _ok(T[lang]["fb_write"]); await q.answer(); return

        # ===== Care меню =====
        if data.startswith("care|"):
            kind = data.split("|",1)[1]
            await _ok(care_links(kind, lang)); await q.answer(); return

        # ===== Intake fallback =====
        if data == "intake:start":
            # если PRO-плагин не перехватил — дадим 10-шаговый профиль
            await start_profile_ctx(context, q.message.chat.id, lang, uid)
            await q.answer(); return

        # Неизвестный коллбек — мягко отвечаем
        await _ok(T[lang]["unknown"]); await q.answer()
    except Exception as e:
        logging.error(f"on_callback error: {e}")
        try:
            await q.answer()
        except Exception:
            pass

# -------- Команда смены имени --------

async def cmd_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None) or "en")
    args = context.args or []
    if args:
        nm = sanitize_name(" ".join(args))
        if len(nm) < 2:
            await update.message.reply_text("Имя слишком короткое." if lang!="en" else "That looks too short.")
            return
        set_name(uid, nm)
        await update.message.reply_text(("Ок, буду обращаться: " if lang!="en" else "Okay, I’ll call you ") + nm)
    else:
        sessions.setdefault(uid, {})["awaiting_name"] = True
        await update.message.reply_text("Как к вам обращаться? Напишите имя одной строкой." if lang!="en" else "How should I address you? Please send your first name.")

# -------- Прехук для входящего текста: имя + «зеркало фактов» --------

async def _pre_text_hook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Этот хук выполняется ПЕРЕД основным on_text (группа -1).
    user = update.effective_user
    uid = user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(user, "language_code", None) or "en")
    txt = (update.message.text or "").strip()

    # сбрасываем «ожидание ответа на вопрос»
    clear_pending(uid)

    # если ждём имя — обработаем немедленно
    msg = try_handle_name_reply(uid, txt, lang)
    if msg:
        await update.message.reply_text(msg)
        raise ApplicationHandlerStop()

    # если имени нет и ещё не спрашивали — спросим один раз
    s = sessions.setdefault(uid, {})
    if not s.get("name") and not s.get("awaiting_name") and not s.get("asked_name"):
        s["awaiting_name"] = True
        s["asked_name"] = True
        await update.message.reply_text("Как к вам обращаться? Напишите имя одной строкой." if lang!="en" else "How should I address you? Please send your first name.")
        raise ApplicationHandlerStop()

    # «Зеркало фактов»: короткая персональная вставка, не мешаем остальной логике
    fact = reflect_facts(txt, lang)
    if fact:
        await maybe_send(context, uid, fact, force=True, count=False)
    # не останавливаем — пусть идёт в основной on_text

# -------- План-кнопки (мини-план сна) --------

async def _cb_plan_sleep_2230(update: Update, context: ContextTypes.DEFAULT_TYPE, q):
    uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    when = _next_local_2230(uid)
    text = "Сегодня в 22:30 — подготовка ко сну 🌙" if lang!="en" else "Tonight 22:30 — wind-down 🌙"
    rid = reminder_add(uid, text, when)
    delay = max(5, (when - utcnow()).total_seconds())
    if _has_jq_ctx(context):
        context.application.job_queue.run_once(job_oneoff_reminder, when=delay, data={"user_id": uid, "reminder_id": rid})
    await _reply_cbsafe(q, ("Готово, напомню " if lang!="en" else "Done, I’ll remind ") + _fmt_local_when(uid, when))

async def _cb_plan_ok(update: Update, context: ContextTypes.DEFAULT_TYPE, q):
    uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await _reply_cbsafe(q, "Принято. Утром спрошу, как прошло 🌙" if lang!="en" else "Got it. I’ll check in tomorrow morning 🌙")

# Включаем эти обработчики внутрь общего on_callback через проверку data:
# plan|sleep|2230, plan|sleep|relax, plan|ok
# (ветви уже реализованы в on_callback → send_sleep_plan и ниже)

# Обновим on_callback для веток mini-плана сна (добавка к существующему коду выше):
_old_on_callback = on_callback  # сохранить ссылку

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    # Перехват наших plan-веток, затем — основной роутер
    if data == "plan|sleep|2230":
        await _cb_plan_sleep_2230(update, context, q); await q.answer(); return
    if data == "plan|sleep|relax":
        lang = norm_lang(users_get(q.from_user.id).get("lang") or "en")
        txt = "Релаксация 60 сек:\n• Медленный вдох 4с\n• Задержка 2с\n• Выдох 6с\n×5 циклов" if lang!="en" else "60-sec relax:\n• Inhale 4s\n• Hold 2s\n• Exhale 6s\n×5 cycles"
        await _reply_cbsafe(q, txt); await q.answer(); return
    if data == "plan|ok":
        await _cb_plan_ok(update, context, q); await q.answer(); return
    # иначе — отдать в основной роутер
    await _old_on_callback(update, context)

# -------- Расширяем приложение без правки build_app --------

def enhance_app(app):
    # Прехук текста (выполняется до on_text)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _pre_text_hook), group=-1)
    # Наш общий callback-router (после gate_cb)
    app.add_handler(CallbackQueryHandler(on_callback), group=0)
    # Команда смены имени
    app.add_handler(CommandHandler("name", cmd_name))

# -------- Entry point --------

if __name__ == "__main__":
    application = build_app()
    enhance_app(application)
    application.run_polling(drop_pending_updates=True)
