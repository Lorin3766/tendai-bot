# -*- coding: utf-8 -*-
# TendAI main.py — полная версия с доработками:
# 1) Имя пользователя: sanitize_name/display_name/set_name/ensure_ask_name/try_handle_name_reply
#    + подстановка {name} ВО ВСЕХ исходящих текстах через обёртку maybe_send
#    + /name для смены имени
# 2) «Один вопрос за раз»: pending_q + is_question/clear_pending, реализовано в обёртке maybe_send
# 3) Лёгкая персонализация: reflect_facts(...) — отзеркаливание фактов
# 4) «Завершение» мини-планом + CTA: send_plan()/send_sleep_plan() + callback’и
# 5) Вечерние чек-ины, восстановление расписаний, безопасные Google Sheets, лимитер и т.п.
#
# ⚠️ Файл разбит на ДВЕ РАВНЫЕ ЧАСТИ. ЭТО — ЧАСТЬ 1/2.
# ЧАСТЬ 2 содержит: callback-router on_callback(), функции для CTA,
# завершающий entrypoint main() и регистрацию обработчиков.

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
        "welcome": "Hi {name}! I’m TendAI — your health & longevity assistant.\nDescribe what’s bothering you; I’ll guide you. Let’s do a quick 40s intake to tailor advice.",
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
        "plan_header":"Your 24–48h plan:",
        "plan_accept":"Will you try this today?",
        "accept_opts":["✅ Yes","🔁 Later","✖️ No"],
        "remind_when":"When shall I check on you?",
        "remind_opts":["in 4h","this evening","tomorrow morning","no need"],
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
        # Name ask/confirm
        "ask_name": "How should I address you? (send a short name)",
        "name_saved": "Nice to meet you, {name}! I’ll use it next time ✨",
    },
    "ru": {
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
        "start_where":"С чего начнём? — или нажми /menu",
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
        "plan_header":"Ваш план на 24–48 часов:",
        "plan_accept":"Готовы попробовать сегодня?",
        "accept_opts":["✅ Да","🔁 Позже","✖️ Нет"],
        "remind_when":"Когда напомнить и спросить самочувствие?",
        "remind_opts":["через 4 часа","вечером","завтра утром","не надо"],
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
        "ask_name": "Как к вам обращаться? (короткое имя одним словом)",
        "name_saved": "Рад знакомству, {name}! Буду так и обращаться ✨",
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
    "ask_name": "Як до вас звертатися? (коротке ім’я одним словом)",
    "name_saved": "Приємно познайомитись, {name}! Так і звертатимусь ✨",
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
    "last_fb_asked","profile_banner_shown","evening_hour","pending_q"
]
PROFILES_HEADERS = ["user_id","sex","age","goal","conditions","meds","allergies","sleep","activity","diet","notes","updated_at","goals","diet_focus","steps_target","cycle_enabled","cycle_last_date","cycle_avg_len","height_cm","weight_kg","supplements","name"]
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
        "pending_q": "no",
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

# === ПРАВКА 2: maybe_send (оригинал) — мы её завернём позже новой обёрткой ===
async def maybe_send(context, uid, text, kb=None, *, force=False, count=True):
    if force or can_send(uid):
        try:
            await context.bot.send_message(uid, text, reply_markup=kb)
            if count:
                mark_sent(uid)
        except Exception as e:
            logging.error(f"send fail: {e}")

# ---------- <<< БЛОК ИМЕНИ И АНТИ-СПАМА >>> ----------
# sanitize_name / display_name / set_name / ensure_ask_name / try_handle_name_reply
NAME_RE = re.compile(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ'’\-\. ]+")

def sanitize_name(raw: str) -> str:
    s = (raw or "").strip()
    m = NAME_RE.findall(s)
    s = "".join(m).strip()
    s = re.sub(r"\s+", " ", s)
    s = s[:24]
    # Первая буква заглавная
    try:
        return s if not s else s[0].upper() + s[1:]
    except Exception:
        return s

def display_name(uid: int) -> str:
    # 1) Profiles.name; 2) сохранённое в сессии; 3) telegram username
    prof = profiles_get(uid) or {}
    if prof.get("name"):
        return str(prof.get("name")).strip()
    s = sessions.get(uid, {}).get("name")
    if s:
        return s
    u = users_get(uid)
    if u.get("username"):
        return u["username"]
    return ""

def set_name(uid: int, name: str):
    name = sanitize_name(name)
    sessions.setdefault(uid, {})["name"] = name
    try:
        profiles_upsert(uid, {"name": name})
    except Exception as e:
        logging.warning(f"set_name fallback: {e}")

def ensure_ask_name(uid: int, lang: str) -> bool:
    """Возвращает True если мы задали вопрос об имени и надо прервать дальнейшую логику."""
    if display_name(uid):
        return False
    # Пометим ожидание имени и pending_q
    sessions.setdefault(uid, {})["awaiting_name"] = True
    users_set(uid, "pending_q", "yes")
    # используем новую обёртку maybe_send (после её определения подставит {name})
    # Здесь у нас ещё нет context => вернём True; вопрос зададим из вызывающего хэндлера
    return True

def try_handle_name_reply(uid: int, text: str, lang: str) -> Optional[str]:
    """Если ждём имя — принять, сохранить и вернуть текст-подтверждение; иначе None."""
    if not sessions.get(uid, {}).get("awaiting_name"):
        return None
    candidate = sanitize_name(text)
    if not candidate or len(candidate) < 2:
        # просим короткое имя ещё раз
        return {"ru":"Пожалуйста, короткое имя одним словом.","uk":"Будь ласка, коротке ім’я одним словом.","en":"Please send a short name (one word).","es":"Please send a short name (one word)."}[lang]
    sessions[uid]["awaiting_name"] = False
    users_set(uid, "pending_q", "no")
    set_name(uid, candidate)
    return (T[lang]["name_saved"]).replace("{name}", candidate)

# ---------- «Один вопрос за раз» ----------
def clear_pending(uid: int):
    users_set(uid, "pending_q", "no")

def is_question(text: str) -> bool:
    if not text: return False
    low = text.lower().strip()
    if "?" in low: return True
    return any(k in low for k in ["how", "when", "what", "когда", "как", "что", "сколько", "чи", "коли"])

def ask_one(uid: int, text: str, kb=None, tag: str = "q"):
    # вспомогательная отправка вопроса (использует maybe_send-обёртку)
    sessions.setdefault(uid, {})["last_q_tag"] = tag
    # отправка произойдёт через maybe_send

# --- Переопределяем maybe_send: подстановка {name} + анти-спам вопросов ---
_send_raw = maybe_send  # сохраним оригинал

async def maybe_send(context, uid, text, kb=None, *, force=False, count=True):
    # подстановка имени
    text = (text or "").replace("{name}", display_name(uid) or "")
    # анти-спам: не отправляем новый вопрос, если есть pending_q
    u = users_get(uid)
    if not force and (u.get("pending_q") or "no") == "yes" and is_question(text):
        logging.info(f"[pending_q] drop question for uid={uid}")
        return
    # если это вопрос — поставим pending_q
    if is_question(text):
        users_set(uid, "pending_q", "yes")
    else:
        # не-вопросы не трогаем флаг
        pass
    await _send_raw(context, uid, text, kb, force=force, count=count)

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
        await context.bot.send_message(uid, text.replace("{name}", display_name(uid) or ""))
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
    nm = (profile.get("name") or "").strip()
    if lang == "ru":
        sex_ru = {"male":"мужчина","female":"женщина","other":"человек"}.get(sex, "человек")
        age_ru = _ru_age_phrase(age_raw or "—")
        goal_ru = {"longevity":"долголетие","energy":"энергия","sleep":"сон","weight":"похудение","strength":"сила"}.get(goal, goal or "—")
        hw = f", {ht}см/{wt}кг" if (ht or wt) else ""
        lead = (nm + " — ") if nm else ""
        return f"{lead}{sex_ru}, {age_ru}{hw}; цель — {goal_ru}"
    if lang == "uk":
        hw = f", {ht}см/{wt}кг" if (ht or wt) else ""
        lead = (nm + " — ") if nm else ""
        return f"{lead}{sex or '—'}, {age_raw or '—'}{hw}; ціль — {goal or '—'}"
    if lang == "es":
        hw = f", {ht}cm/{wt}kg" if (ht or wt) else ""
        lead = (nm + " — ") if nm else ""
        return f"{lead}{sex or '—'}, {age_raw or '—'}{hw}; objetivo — {goal or '—'}"
    hw = f", {ht}cm/{wt}kg" if (ht or wt) else ""
    lead = (nm + " — ") if nm else ""
    return f"{lead}{sex or '—'}, {age_raw or '—'}{hw}; goal — {goal or '—'}"

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
                    "uk":"Червоні прапори: слабкість рук, оніміння, травма, гарячка, біль >7/10, прогресує — до лікаря/невідкладної.",
                    "en":"Red flags: arm weakness/numbness, trauma, fever, pain >7/10, rapid progression — seek care.",
                    "es":"Banderas rojas: debilidad/entumecimiento en brazos, trauma, fiebre, dolor >7/10, progresión rápida — atención médica."}[lang]
    return ""

def care_links(kind: str, lang: str, city_hint: Optional[str]=None) -> str:
    if kind=="labsnear":
        q = "labs near me" if lang=="en" else "лаборатории рядом"
        return f"🔗 Google Maps: https://www.google.com/maps/search/{q.replace(' ','+')}"

# (ЧАСТЬ 2 продолжит: остальные care_links ветки, Youth-команды, /name, /start и все хэндлеры,
# reflect_facts(), send_plan(), send_sleep_plan(), callback-router и entrypoint)
# =========================
# ======= ЧАСТЬ 2 =========
# =========================
# Доп. утилиты (имя, pending_q, «зеркало фактов»), обёртка maybe_send,
# мини-планы, callback-router, регистрация хэндлеров и entrypoint.
# =========================

# ---------- (1) Имя пользователя ----------
def sanitize_name(raw: str) -> str:
    s = (raw or "").strip()
    # только буквы/цифры/пробел/дефис/точка, обрезаем до 24
    s = re.sub(r"[^A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9 \-\.]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:24]

def display_name(uid: int) -> str:
    # храним «имя» в сессии (не ломаем Sheets-хедеры)
    return sessions.get(uid, {}).get("name") or ""

def set_name(uid: int, name: str):
    sessions.setdefault(uid, {})["name"] = sanitize_name(name)

def ensure_ask_name(uid: int, lang: str) -> bool:
    """Спросить имя ровно один раз. Возвращает True, если вопрос отправлен (и нужно return у вызывающего)."""
    s = sessions.setdefault(uid, {})
    if display_name(uid) or s.get("name_asked"):
        return False
    s["awaiting_name"] = True
    s["name_asked"] = True
    # вопрос — считается вопросом для anti-spam
    txt = {"ru": "Как к вам обращаться? (имя/ник)", "uk": "Як до вас звертатися? (імʼя/нік)",
           "en": "How should I call you? (name/nick)", "es": "¿Cómo debo llamarte? (nombre/nick)"}[lang]
    # ставим pending_q для «один вопрос за раз»
    s["pending_q"] = True
    # отправляем напрямую ботом (не трогая лимитер)
    try:
        # используем _send_raw, если уже обернули maybe_send, иначе напрямую
        if "_send_raw" in globals():
            context = None  # нет контекста на прямом вызове; пошлём через bot из sessions (нет) — fallback:
        raise RuntimeError  # форсируем переход в except, где пошлём update-less способом ниже
    except Exception:
        # У нас нет context/бота в этой точке, так что задающий ensure вызывается из команд/хэндлеров,
        # там имя будет спрошено через maybe_send / прямую отправку. Вернём True, чтобы вызывающий сделал return.
        pass
    return True

def try_handle_name_reply(uid: int, text: str, lang: str) -> bool:
    """Если ждём имя — принять, сохранить, ответить; вернуть True, чтобы остановить дальнейшую обработку."""
    s = sessions.setdefault(uid, {})
    if not s.get("awaiting_name"):
        return False
    name = sanitize_name(text)
    if not name or len(name) < 2:
        # мягко переспрашиваем
        s["pending_q"] = True
        return False
    set_name(uid, name)
    s["awaiting_name"] = False
    s["pending_q"] = False
    # ответим через плановый ход — основной on_text сам отправит следующее
    return True

# ---------- (2) «Один вопрос за раз» ----------
def clear_pending(uid: int):
    sessions.setdefault(uid, {}).pop("pending_q", None)

def is_question(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if "?" in t:
        return True
    # простые маркеры вопроса для ru/en/uk/es
    kws = [
        "как", "что", "когда", "где", "можно", "нужно", "почему", "сколько",
        "how", "what", "when", "where", "should", "why", "which", "can",
        "як", "коли", "де", "чому", "скільки",
        "cómo", "qué", "cuándo", "dónde", "por qué", "cuánto"
    ]
    return any(t.startswith(k) or f" {k} " in t for k in kws)

def ask_one(uid: int, text: str, kb=None, tag: str = "q"):
    """Опциональный явный вопрос, без изменения существующих вызовов."""
    sessions.setdefault(uid, {})["pending_q"] = True

# ---------- Подмена maybe_send ----------
# Сохраняем оригинальную версию и объявляем «обёртку»
if "maybe_send" in globals():
    _send_raw = maybe_send  # noqa: F811
else:
    async def _send_raw(context, uid, text, kb=None, *, force=False, count=True):
        try:
            await context.bot.send_message(uid, text, reply_markup=kb)
            if count:
                mark_sent(uid)
        except Exception as e:
            logging.error(f"_send_raw fail: {e}")

async def maybe_send(context, uid, text, kb=None, *, force=False, count=True):  # noqa: F811
    # подстановка имени
    try:
        text = (text or "").replace("{name}", display_name(uid) or "")
    except Exception:
        pass
    # анти-спам «один вопрос за раз»
    s = sessions.setdefault(uid, {})
    if s.get("pending_q") and not force and is_question(text):
        return
    if is_question(text):
        s["pending_q"] = True
    # отправка
    await _send_raw(context, uid, text, kb=kb, force=force, count=count)

# ---------- (3) «Зеркало фактов» ----------
def reflect_facts(text: str) -> str:
    low = (text or "").lower()
    facts = []
    # простые паттерны: сон, стресс, вода, боль
    m = re.search(r'(\d{1,2})\s*[-–]?\s*(\d{1,2})?\s*час', low)
    if m:
        a = m.group(1); b = m.group(2)
        rng = f"{a}–{b}" if b else a
        facts.append({"ru": f"Понял: спите ~{rng} часов.", "uk": f"Зрозумів: спите ~{rng} год.",
                      "en": f"Got it: you sleep ~{rng}h.", "es": f"Entiendo: duermes ~{rng}h."})
    if any(k in low for k in ["стресс", "нервни", "перегора", "burnout", "stress", "estres", "estresó"]):
        facts.append({"ru": "Много стресса — учту в советах.", "uk": "Багато стресу — врахую в порадах.",
                      "en": "High stress — I’ll factor that in.", "es": "Mucho estrés — lo tendré en cuenta."})
    if any(k in low for k in ["мало воды", "мало пью", "dehydrate", "little water", "не пью"]):
        facts.append({"ru": "Нехватка воды отмечена.", "uk": "Нестача води відмічена.",
                      "en": "Noted low hydration.", "es": "Hidratación baja: anotado."})
    if not facts:
        return ""
    # вернём одну строку
    lang = "ru" if re.search(r"[а-яё]", low) else "en"
    return facts[0].get(lang, facts[0]["en"])

# ---------- (4) Мини-план + CTA ----------
def send_plan_text(title: str, bullets: list[str], lang: str) -> str:
    t = title.strip()
    body = "\n".join([f"• {b}" for b in bullets if b.strip()])
    return f"{t}\n{body}"

async def send_plan(uid: int, title: str, bullets: list[str], ctas: list[tuple[str, str]], q, lang: str):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(lbl, callback_data=data)] for (lbl, data) in ctas])
    await _reply_cbsafe(q, send_plan_text(title, bullets, lang), kb)

async def send_sleep_plan(uid: int, ctx, q, lang: str):
    title = {"ru": "План на сон (сегодня):", "uk": "План на сон (сьогодні):",
             "en": "Sleep plan (tonight):", "es": "Plan de sueño (esta noche):"}[lang]
    bullets = {
        "ru": ["1) Отбой в 22:30, экран-детокс 60 минут.", "2) 60 сек дыхание: 4–7–8.", "3) Утром – свет 10–15 мин."],
        "uk": ["1) Відбій о 22:30, 60 хв без екранів.", "2) Дихання 60 с: 4–7–8.", "3) Вранці — світло 10–15 хв."],
        "en": ["1) Bedtime 22:30, 60 min screen detox.", "2) 60s 4–7–8 breathing.", "3) Morning light 10–15m."],
        "es": ["1) Dormir 22:30, 60 min sin pantallas.", "2) 60s respiración 4–7–8.", "3) Luz matutina 10–15m."]
    }[lang]
    ctas = [
        ("⏰ Сегодня 22:30" if lang != "en" else "⏰ Tonight 22:30", "sleep|2230"),
        ("🧘 60 сек. релаксация" if lang != "en" else "🧘 60s relax", "sleep|relax"),
        ("👍 Всё понятно" if lang != "en" else "👍 All clear", "sleep|ok"),
    ]
    await send_plan(uid, title, bullets, ctas, q, lang)

# ---------- Локальные утилиты времени (из Ч.1 у нас уже есть) ----------
def _to_local(uid: int, dt_utc: datetime) -> datetime:
    try:
        off = int(str(users_get(uid).get("tz_offset") or "0"))
    except Exception:
        off = 0
    return (dt_utc.astimezone(timezone.utc) + timedelta(hours=off)).replace(tzinfo=None)

def _fmt_local_when(uid: int, dt_utc: datetime) -> str:
    now_loc = _to_local(uid, utcnow())
    tgt_loc = _to_local(uid, dt_utc)
    lang = norm_lang(users_get(uid).get("lang") or "en")
    if tgt_loc.date() == now_loc.date():
        day = {"ru": "сегодня", "uk": "сьогодні", "es": "hoy", "en": "today"}[lang]
        glue = " в " if lang in ("ru", "uk") else " at "
    elif tgt_loc.date() == now_loc.date() + timedelta(days=1):
        day = {"ru": "завтра", "uk": "завтра", "es": "mañana", "en": "tomorrow"}[lang]
        glue = " в " if lang in ("ru", "uk") else " at "
    else:
        day = tgt_loc.strftime("%Y-%m-%d")
        glue = " " if lang in ("ru", "uk") else " at "
    return f"{day}{glue}{tgt_loc.strftime('%H:%M')}"

def _next_local_dt(hhmm: str, tz_off: int, base: str = "auto") -> datetime:
    now_utc = utcnow()
    now_local = now_utc + timedelta(hours=tz_off)
    h, m = hhmm_tuple(hhmm)
    target_local = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    if base == "tomorrow" or target_local <= now_local:
        target_local = target_local + timedelta(days=1)
    target_utc_naive = target_local - timedelta(hours=tz_off)
    return target_utc_naive.replace(tzinfo=timezone.utc)

def next_evening_dt(uid: int) -> datetime:
    u = users_get(uid)
    tz_off = int(str(u.get("tz_offset") or "0"))
    hhmm = (u.get("evening_hour") or DEFAULT_EVENING_LOCAL)
    return _next_local_dt(hhmm, tz_off, base="auto")

def next_morning_dt(uid: int) -> datetime:
    u = users_get(uid)
    tz_off = int(str(u.get("tz_offset") or "0"))
    hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
    return _next_local_dt(hhmm, tz_off, base="auto")

def _schedule_oneoff(app, uid: int, when_key: str, lang: str, text: Optional[str] = None) -> Optional[str]:
    if not _has_jq_app(app):
        logging.warning("JobQueue not available – skip oneoff reminder.")
        return None
    now = utcnow()
    if when_key == "4h":
        when = now + timedelta(hours=4)
    elif when_key == "evening":
        when = next_evening_dt(uid)
    elif when_key == "morning":
        when = next_morning_dt(uid)
    else:
        return None
    rid = reminder_add(uid, text or T[lang]["thanks"], when)
    delay = max(5, (when - now).total_seconds())
    app.job_queue.run_once(job_oneoff_reminder, when=delay, data={"user_id": uid, "reminder_id": rid})
    return rid

# ---------- (5) Колбэки мини-плана сна ----------
async def cb_remind_sleep_2230(q, context: ContextTypes.DEFAULT_TYPE, uid: int, lang: str):
    u = users_get(uid)
    tz = int(str(u.get("tz_offset") or "0"))
    when = _next_local_dt("22:30", tz, base="auto")
    rid = reminder_add(uid, {"ru": "🌙 Отбой 22:30", "uk": "🌙 Відбій 22:30",
                             "en": "🌙 Bedtime 22:30", "es": "🌙 Dormir 22:30"}[lang], when)
    if _has_jq_ctx(context):
        delay = max(5, (when - utcnow()).total_seconds())
        context.application.job_queue.run_once(job_oneoff_reminder, when=delay,
                                               data={"user_id": uid, "reminder_id": rid})
    await _reply_cbsafe(q, {"ru": f"Готово, напомню { _fmt_local_when(uid, when) }.",
                            "uk": f"Готово, нагадаю { _fmt_local_when(uid, when) }.",
                            "en": f"Done, I’ll remind you { _fmt_local_when(uid, when) }.",
                            "es": f"Listo, te recordaré { _fmt_local_when(uid, when) }."}[lang])

async def cb_ok_plan(q, context: ContextTypes.DEFAULT_TYPE, uid: int, lang: str):
    # Мягкое подтверждение + утренний пинг
    when = next_morning_dt(uid)
    rid = reminder_add(uid, {"ru": "Как прошла ночь? 🌙", "uk": "Як пройшла ніч? 🌙",
                             "en": "How was the night? 🌙", "es": "¿Cómo fue la noche? 🌙"}[lang], when)
    if _has_jq_ctx(context):
        delay = max(5, (when - utcnow()).total_seconds())
        context.application.job_queue.run_once(job_oneoff_reminder, when=delay,
                                               data={"user_id": uid, "reminder_id": rid})
    await _reply_cbsafe(q, {"ru": "Принято. Утром спрошу, как прошло 🌙",
                            "uk": "Прийнято. Вранці спитаю, як минуло 🌙",
                            "en": "Got it. I’ll check in tomorrow morning 🌙",
                            "es": "Entendido. Te pregunto mañana por la mañana 🌙"}[lang])

# ---------- (6) Обновим тексты с {name} (привет/утро/вечер/спасибо/заголовок плана) ----------
def _inject_name_tokens():
    for l in T.keys():
        for key, patch in [
            ("welcome",  ("{name}, ", "{name}, ")),
            ("daily_gm", ("{name}, ", "{name}, ")),
            ("daily_pm", ("{name}: ", "{name}: ")),
            ("thanks",   (" {name}", " {name}")),
            ("plan_header", (" {name}", " {name}")),
        ]:
            try:
                val = T[l].get(key, "")
                if "{name}" not in val:
                    # лёгкое и безопасное внедрение
                    if key in ("welcome", "daily_gm"):
                        T[l][key] = (("Привет, " if l=="ru" and key=="welcome" else "") + patch[0] + val) if l != "en" else ("Hi, " + patch[0] + val if key=="welcome" else patch[0] + val)
                    else:
                        T[l][key] = val + patch[1]
            except Exception:
                pass

_inject_name_tokens()

# ---------- (7) Callback router ----------
def _parse_cb(data: str) -> list:
    try:
        parts = (data or "").split("|")
        if len(parts) < 3:
            parts += [""] * (3 - len(parts))
        return parts
    except Exception:
        return [str(data or ""), "", ""]

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(q.from_user, "language_code", None) or "en")

    # снимаем pending для нового ответа
    clear_pending(uid)
    try:
        await q.answer()
    except Exception:
        pass

    # меню
    if data.startswith("menu|"):
        _, kind, *_ = data.split("|") + [""]
        if kind in ("root", "", None):
            await _reply_cbsafe(q, T[lang]["m_menu_title"], inline_main_menu(lang)); return
        if kind == "h60":
            sessions.setdefault(uid, {})["awaiting_h60"] = True
            await _reply_cbsafe(q, T[lang]["h60_intro"]); return
        if kind == "sym":
            await _reply_cbsafe(q, "Choose a symptom:" if lang=="en" else "Выберите симптом:", inline_symptoms_menu(lang)); return
        if kind == "mini":
            await _reply_cbsafe(q, "Mini-plans:" if lang=="en" else "Мини-планы:", inline_miniplans_menu(lang)); return
        if kind == "care":
            await _reply_cbsafe(q, "Care & places:" if lang=="en" else "Куда обратиться:", inline_findcare_menu(lang)); return
        if kind == "hab":
            await _reply_cbsafe(q, "Quick habit log:" if lang=="en" else "Быстрый лог привычек:", inline_habits_menu(lang)); return
        if kind == "rem":
            await _reply_cbsafe(q, "When to remind?" if lang=="en" else "Когда напомнить?", inline_remind(lang)); return
        if kind == "lang":
            await _reply_cbsafe(q, "Language:" if lang=="en" else "Язык:", inline_lang_menu(lang)); return
        if kind == "privacy":
            await _reply_cbsafe(q, T[lang]["privacy"]); return
        if kind == "smart":
            await _reply_cbsafe(q, T[lang]["m_smart"], inline_smart_checkin(lang)); return
        if kind == "coming":
            await _reply_cbsafe(q, T[lang]["m_soon"]); return

    # язык
    if data.startswith("lang|"):
        _, code = data.split("|", 1)
        code = code or "en"
        users_set(uid, "lang", code)
        await _reply_cbsafe(q, {"ru":"Ок, дальше по-русски.","uk":"Гаразд, далі українською.",
                                "en":"OK, I’ll reply in English.","es":"De acuerdo, responderé en español."}[code],
                           inline_main_menu(code))
        return

    # смарт-чек-ин
    if data.startswith("smart|"):
        _, val = data.split("|", 1)
        msg = {"ok":"👍", "pain":"😣", "tired":"😴", "stress":"😵‍💫", "hb":"🔥", "other":"🧭"}.get(val, "✅")
        await _reply_cbsafe(q, msg)
        return

    # настроение / чек-ины
    if data.startswith("mood|"):
        _, mood = data.split("|", 1)
        daily_add(iso(utcnow()), uid, mood, "")
        if mood == "note":
            sessions.setdefault(uid, {})["awaiting_daily_comment"] = True
            await _reply_cbsafe(q, T[lang]["fb_write"])
        else:
            await _reply_cbsafe(q, T[lang]["mood_thanks"])
        return

    # профиль (10 шагов)
    if data.startswith("p|"):
        parts = data.split("|")
        if len(parts) >= 3 and parts[1] == "choose":
            _, _, key, val = parts[0], parts[1], parts[2], (parts[3] if len(parts)>3 else "")
            profiles_upsert(uid, {key: val}); sessions.setdefault(uid, {})[key] = val
            users_set(uid, "profile_banner_shown", "no")
            await advance_profile_ctx(context, q.message.chat.id, lang, uid)
            return
        if len(parts) >= 3 and parts[1] == "write":
            key = parts[2]
            sessions.setdefault(uid, {})["p_wait_key"] = key
            await _reply_cbsafe(q, {"ru": "Напишите в одном сообщении:", "uk":"Напишіть одним повідомленням:",
                                    "en": "Please type in one message:", "es":"Escribe en un solo mensaje:"}[lang])
            return
        if len(parts) >= 3 and parts[1] == "skip":
            await advance_profile_ctx(context, q.message.chat.id, lang, uid)
            return

    # выбор темы
    if data.startswith("topic|"):
        _, topic = data.split("|", 1)
        s = _set_session(uid)
        s["topic"] = topic
        s["step"] = 1
        if topic == "pain":
            await _reply_cbsafe(q, T[lang]["triage_pain_q1"], _kb_for_code(lang, "painloc"))
        elif topic == "sleep":
            await send_sleep_plan(uid, {}, q, lang)
        elif topic == "profile":
            await start_profile_ctx(context, q.message.chat.id, lang, uid)
        else:
            await _reply_cbsafe(q, T[lang]["unknown"])
        return

    # триаж: локация боли
    if data.startswith("painloc|"):
        _, loc = data.split("|", 1)
        s = _set_session(uid)
        s.setdefault("answers", {})["loc"] = loc
        s["step"] = 2
        await _reply_cbsafe(q, T[lang]["triage_pain_q2"], _kb_for_code(lang, "painkind")); return

    # триаж: характер боли
    if data.startswith("painkind|"):
        _, kind = data.split("|", 1)
        s = _set_session(uid)
        s.setdefault("answers", {})["kind"] = kind
        s["step"] = 3
        await _reply_cbsafe(q, T[lang]["triage_pain_q3"], _kb_for_code(lang, "paindur")); return

    # триаж: длительность
    if data.startswith("paindur|"):
        _, dur = data.split("|", 1)
        s = _set_session(uid)
        s.setdefault("answers", {})["dur"] = dur
        s["step"] = 4
        await _reply_cbsafe(q, T[lang]["triage_pain_q4"], _kb_for_code(lang, "num")); return

    # триаж: оценка 0–10 (кнопками)
    if data.startswith("num|"):
        _, n = data.split("|", 1)
        try:
            sev = int(n)
        except Exception:
            sev = 5
        s = _set_session(uid)
        s.setdefault("answers", {})["severity"] = sev
        s["step"] = 5
        await _reply_cbsafe(q, T[lang]["triage_pain_q5"], _kb_for_code(lang, "painrf")); return

    # триаж: red flags
    if data.startswith("painrf|"):
        _, rf = data.split("|", 1)
        s = _set_session(uid)
        ans = s.setdefault("answers", {})
        prev = ans.get("red", [])
        if not isinstance(prev, list):
            prev = []
        if rf and rf not in prev:
            prev.append(rf)
        ans["red"] = prev
        # финал плана
        prof = profiles_get(uid)
        plan_lines = pain_plan(lang, prev, prof)
        kb = inline_accept(lang)
        await _reply_cbsafe(q, "\n".join([T[lang]["plan_header"]] + plan_lines), kb); return

    if data.startswith("pain|exit"):
        sessions.pop(uid, None)
        await _reply_cbsafe(q, T[lang]["m_menu_title"], inline_main_menu(lang)); return

    # принятие плана + напоминания
    if data.startswith("acc|"):
        _, val = data.split("|", 1)
        if val == "yes":
            await _reply_cbsafe(q, T[lang]["remind_when"], inline_remind(lang)); return
        if val == "later":
            await _reply_cbsafe(q, T[lang]["thanks"]); return
        if val == "no":
            await _reply_cbsafe(q, {"ru":"Ок, без плана. Если что — я рядом.",
                                    "uk":"Гаразд, без плану. Якщо що — я поруч.",
                                    "en":"Okay, no plan. I’m here if you need me.",
                                    "es":"De acuerdo, sin plan. Aquí estoy si me necesitas."}[lang]); return

    if data.startswith("rem|"):
        _, when_key = data.split("|", 1)
        rid = _schedule_oneoff(context.application, uid, when_key, lang)
        conf = {"ru":"Готово, напомню ", "uk":"Готово, нагадаю ",
                "en":"Done, I’ll remind you ", "es":"Listo, te recordaré "}
        if when_key == "4h":
            when = utcnow() + timedelta(hours=4)
        elif when_key == "evening":
            when = next_evening_dt(uid)
        else:
            when = next_morning_dt(uid)
        await _reply_cbsafe(q, conf[lang] + _fmt_local_when(uid, when) + "."); return

    # действия
    if data.startswith("act|"):
        _, kind, arg = (data.split("|") + ["", ""])[:3]
        if kind == "rem":
            rid = _schedule_oneoff(context.application, uid, arg, lang)
            when = (utcnow() + timedelta(hours=4)) if arg=="4h" else (next_evening_dt(uid) if arg=="evening" else next_morning_dt(uid))
            await _reply_cbsafe(q, {"ru":"Записал напоминание на ",
                                    "uk":"Записав нагадування на ",
                                    "en":"Reminder set for ",
                                    "es":"Recordatorio para "}[lang] + _fmt_local_when(uid, when)); return
        if kind == "ex" and arg == "neck":
            await _reply_cbsafe(q, microplan_text("neck", lang)); return
        if kind == "lab":
            sessions.setdefault(uid, {})["awaiting_city"] = True
            await _reply_cbsafe(q, T[lang]["act_city_prompt"]); return
        if kind == "er":
            await _reply_cbsafe(q, T[lang]["er_text"]); return
        if kind == "h60":
            sessions.setdefault(uid, {})["awaiting_h60"] = True
            await _reply_cbsafe(q, T[lang]["h60_intro"]); return

    # чипы-подсказки
    if data.startswith("chip|"):
        _, domain, kind = (data.split("|") + ["",""])[:3]
        await _reply_cbsafe(q, chip_text(domain, kind, lang)); return

    # фидбек
    if data.startswith("fb|"):
        _, val = data.split("|", 1)
        if val == "up":
            feedback_add(iso(utcnow()), uid, display_name(uid), "", "up", "")
            await _reply_cbsafe(q, T[lang]["fb_thanks"]); return
        if val == "down":
            feedback_add(iso(utcnow()), uid, display_name(uid), "", "down", "")
            await _reply_cbsafe(q, T[lang]["fb_thanks"]); return
        if val == "text":
            sessions.setdefault(uid, {})["awaiting_free_feedback"] = True
            await _reply_cbsafe(q, T[lang]["fb_write"]); return

    # мини-план сна CTA
    if data == "sleep|2230":
        await cb_remind_sleep_2230(q, context, uid, lang); return
    if data == "sleep|relax":
        txt = {"ru":"Сядьте удобно. Вдох на 4, задержка 7, выдох 8 — 6 циклов.",
               "uk":"Сядьте зручно. Вдих 4, затримка 7, видих 8 — 6 циклів.",
               "en":"Sit comfortably. Inhale 4, hold 7, exhale 8 — 6 cycles.",
               "es":"Siéntate cómodo. Inhala 4, retén 7, exhala 8 — 6 ciclos."}[lang]
        await _reply_cbsafe(q, txt); return
    if data == "sleep|ok":
        await cb_ok_plan(q, context, uid, lang); return

    # по умолчанию
    await _reply_cbsafe(q, T[lang]["unknown"])

# ---------- (8) Команда /name ----------
async def cmd_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None) or "en")
    args = context.args or []
    if args:
        name = sanitize_name(" ".join(args))
        if not name or len(name) < 2:
            await update.message.reply_text({"ru":"Имя слишком короткое.", "uk":"Імʼя закоротке.",
                                             "en":"Name is too short.", "es":"Nombre demasiado corto."}[lang])
            return
        set_name(uid, name)
        await update.message.reply_text({"ru":f"Готово, {name}!", "uk":f"Готово, {name}!",
                                         "en":f"Done, {name}!", "es":f"Listo, {name}!"}[lang])
        return
    # спросить
    sessions.setdefault(uid, {})["awaiting_name"] = True
    sessions[uid]["pending_q"] = True
    await update.message.reply_text({"ru":"Как к вам обращаться? (имя/ник)",
                                     "uk":"Як до вас звертатися? (імʼя/нік)",
                                     "en":"How should I call you? (name/nick)",
                                     "es":"¿Cómo debo llamarte? (nombre/nick)"}[lang])

# ---------- (9) Пред-хуки для входящих: clear_pending + reflect_facts + имя-ответ ----------
async def pre_text_hook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.message.text is None:
        return
    uid = update.effective_user.id
    # снимаем pending сразу при любом входящем
    clear_pending(uid)

    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None) or "en")
    text = (update.message.text or "").strip()

    # обработка ответа с именем (если ждали)
    if try_handle_name_reply(uid, text, lang):
        nm = display_name(uid)
        if nm:
            await update.message.reply_text({"ru": f"Приятно познакомиться, {nm}!",
                                             "uk": f"Радий знайомству, {nm}!",
                                             "en": f"Nice to meet you, {nm}!",
                                             "es": f"¡Encantado, {nm}!"}[lang])
        return  # не пускаем дальше — на этот вход достаточно

    # «зеркало фактов» — короткая персональная строка (без счётчика/лимитера)
    fact = reflect_facts(text)
    if fact:
        try:
            await update.message.reply_text(fact)
        except Exception as e:
            logging.debug(f"reflect_facts send skip: {e}")

async def pre_callback_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        clear_pending(update.callback_query.from_user.id)

# ---------- (10) Post-/start: спросить имя сразу после приветствия ----------
async def post_start_ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None) or "en")
    if ensure_ask_name(uid, lang):
        await update.message.reply_text({"ru":"Как к вам обращаться? (имя/ник)",
                                         "uk":"Як до вас звертатися? (імʼя/нік)",
                                         "en":"How should I call you? (name/nick)",
                                         "es":"¿Cómo debo llamarte? (nombre/nick)"}[lang])

# ---------- (11) Ошибки ----------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.exception("Unhandled error", exc_info=context.error)

# ---------- (12) Entry point ----------
def main():
    app = build_app()  # из ЧАСТИ 1

    # глобальный колбэк-роутер
    app.add_handler(CallbackQueryHandler(pre_callback_clear, pattern=r".*", block=False, group=-1))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r".*"), group=0)

    # пред-хук текста: clear_pending + имя + факты
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, pre_text_hook), group=-1)

    # имя
    app.add_handler(CommandHandler("name", cmd_name), group=0)

    # post-/start хук: спросить имя
    app.add_handler(CommandHandler("start", post_start_ask_name), group=1)

    # ошибки
    app.add_error_handler(on_error)

    # запуск
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
