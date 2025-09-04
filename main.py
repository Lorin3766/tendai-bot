# -*- coding: utf-8 -*-
# =========================
# TendAI — Full Bot (Part 1/2)
# =========================

import os, re, json, uuid, logging, random
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Set
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

# ---------- OpenAI (optional) ----------
try:
    from openai import OpenAI  # type: ignore
except Exception:
    OpenAI = None  # type: ignore

# ---------- Google Sheets (robust + memory fallback) ----------
import gspread
import gspread.utils as gsu
from gspread.exceptions import SpreadsheetNotFound, WorksheetNotFound
from oauth2client.service_account import ServiceAccountCredentials


# ---------------- Boot & Config ----------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
DetectorFactory.seed = 0

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

SHEET_NAME = os.getenv("SHEET_NAME", "TendAI Sheets")
SHEET_ID = os.getenv("SHEET_ID", "")
ALLOW_CREATE_SHEET = os.getenv("ALLOW_CREATE_SHEET", "0") == "1"

DEFAULT_CHECKIN_LOCAL = "08:30"
DEFAULT_EVENING_LOCAL = "20:00"
DEFAULT_QUIET_HOURS = "22:00-08:00"

AUTO_MAX_PER_DAY = 2  # защита от спама авто-уведомлений

# OpenAI client (по возможности)
oai = None
if OpenAI and OPENAI_API_KEY:
    try:
        os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
        oai = OpenAI()
    except Exception as e:
        logging.error(f"OpenAI init error: {e}")
        oai = None


# ---------------- i18n & text ----------------
SUPPORTED = {"ru", "en", "uk", "es"}

def norm_lang(code: Optional[str]) -> str:
    if not code:
        return "en"
    c = code.split("-")[0].lower()
    return c if c in SUPPORTED else "en"

T: Dict[str, Dict[str, str]] = {
    "en": {
        "welcome": "Hi! I’m TendAI — a caring health & longevity assistant.\nWe’ll keep it short, useful, and friendly. Let’s do a 40s intake to tailor advice.",
        "help": "Short checkups, 24–48h plans, reminders, daily check-ins.\nCommands: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /evening_on 20:00 /evening_off /settz +2 /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
        "privacy": "I’m not a medical service and can’t replace a doctor. Minimal data is stored for reminders. Use /delete_data to erase.",
        "quick_title": "Quick actions",
        "quick_h60": "⚡ Health in 60s",
        "quick_er": "🚑 Emergency info",
        "quick_lab": "🧪 Lab",
        "quick_rem": "⏰ Reminder",
        "profile_intro": "Quick intake (~40s). Use buttons or type your answer.",
        "write": "✍️ Write",
        "skip": "⏭️ Skip",
        "start_where": "Where do you want to start? (symptom/sleep/nutrition/labs/habits/longevity)",
        "saved_profile": "Saved: ",
        "daily_gm": "Good morning! 🌞 How do you feel today?",
        "gm_excellent": "👍 Excellent",
        "gm_ok": "🙂 Okay",
        "gm_tired": "😐 Tired",
        "gm_pain": "🤕 In pain",
        "gm_skip": "⏭️ Skip today",
        "mood_note": "✍️ Comment",
        "mood_thanks": "Thanks! Have a smooth day 👋",
        "h60_btn": "Health in 60 seconds",
        "h60_intro": "Briefly write what’s bothering you (e.g., “headache”, “fatigue”, “stomach pain”). I’ll give 3 key tips in 60 seconds.",
        "h60_t1": "Possible causes",
        "h60_t2": "Do now (next 24–48h)",
        "h60_t3": "When to see a doctor",
        "h60_serious": "Serious to rule out",
        "ask_fb": "Was this helpful?",
        "fb_good": "👍 Like",
        "fb_bad": "👎 Dislike",
        "fb_free": "📝 Feedback",
        "fb_write": "Write a short feedback message:",
        "fb_thanks": "Thanks for your feedback! ✅",
        "youth_pack": "Youth Pack",
        "gm_energy": "⚡ Energy",
        "gm_energy_q": "How’s your energy (1–5)?",
        "gm_energy_done": "Logged energy — thanks!",
        "gm_evening_btn": "⏰ Remind this evening",
        "hydrate_btn": "💧 Hydration",
        "hydrate_nudge": "💧 Time for a glass of water",
        "skintip_btn": "🧴 Skin/Body tip",
        "skintip_sent": "Tip sent.",
        "daily_tip_prefix": "🍎 Daily tip:",
        "challenge_btn": "🎯 7-day hydration challenge",
        "challenge_started": "Challenge started! I’ll track your daily check-ins.",
        "challenge_progress": "Challenge progress: {d}/{len} days.",
        "cycle_btn": "🩸 Cycle",
        "cycle_consent": "Would you like to track your cycle for gentle timing tips?",
        "cycle_ask_last": "Enter the date of your last period (YYYY-MM-DD):",
        "cycle_ask_len": "Average cycle length in days (e.g., 28):",
        "cycle_saved": "Cycle tracking saved.",
        "quiet_saved": "Quiet hours saved: {qh}",
        "set_quiet_btn": "🌙 Quiet hours",
        "ask_quiet": "Type quiet hours as HH:MM-HH:MM (local), e.g. 22:00-08:00",
        "evening_intro": "Evening check-in:",
        "evening_tip_btn": "🪄 Tip of the day",
        "evening_set": "Evening check-in set to {t} (local).",
        "evening_off": "Evening check-in disabled.",
        "ask_consent": "May I send you a follow-up later to check how you feel?",
        "yes": "Yes", "no": "No",
        "unknown": "I need a bit more info: where exactly and for how long?",
        "thanks": "Got it 🙌",
        "back": "◀ Back",
        "exit": "Exit",
        "paused_on": "Notifications paused. Use /resume to enable.",
        "paused_off": "Notifications resumed.",
        "deleted": "All your data was deleted. Use /start to begin again.",
        "life_today": "Today is your {n}-th day of life 🎉. Target — 36,500 (100y).",
        "life_percent": "You’ve already passed {p}% toward 100 years.",
        "life_estimate": "(estimated by age, set birth_date for accuracy)",
        "px": "Considering your profile: {sex}, {age}y; goal — {goal}.",
        "act_rem_4h": "⏰ Remind in 4h",
        "act_rem_eve": "⏰ This evening",
        "act_rem_morn": "⏰ Tomorrow morning",
        "act_save_episode": "💾 Save episode",
        "act_ex_neck": "🧘 5-min neck routine",
        "act_er": "🚑 Emergency info",
    },
    "ru": {
        "welcome": "Привет! Я TendAI — заботливый ассистент здоровья и долголетия.\nБудем кратко, полезно и по-дружески. Давайте короткий опрос (~40с) для персонализации.",
        "help": "Короткие проверки, план на 24–48 ч, напоминания, ежедневные чек-ины.\nКоманды: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /evening_on 20:00 /evening_off /settz +3 /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
        "privacy": "Я не заменяю врача. Храним минимум данных для напоминаний. /delete_data — удалить.",
        "quick_title": "Быстрые действия",
        "quick_h60": "⚡ Здоровье за 60 сек",
        "quick_er": "🚑 Срочно в скорую",
        "quick_lab": "🧪 Лаборатория",
        "quick_rem": "⏰ Напоминание",
        "profile_intro": "Быстрый опрос (~40с). Можно нажимать кнопки или писать свой ответ.",
        "write": "✍️ Написать",
        "skip": "⏭️ Пропустить",
        "start_where": "С чего начнём? (симптом/сон/питание/анализы/привычки/долголетие)",
        "saved_profile": "Сохранил: ",
        "daily_gm": "Доброе утро! 🌞 Как сегодня самочувствие?",
        "gm_excellent": "👍 Отлично",
        "gm_ok": "🙂 Нормально",
        "gm_tired": "😐 Устал",
        "gm_pain": "🤕 Болит",
        "gm_skip": "⏭️ Пропустить",
        "mood_note": "✍️ Комментарий",
        "mood_thanks": "Спасибо! Хорошего дня 👋",
        "h60_btn": "Здоровье за 60 секунд",
        "h60_intro": "Коротко напишите, что беспокоит (например: «болит голова», «усталость», «боль в животе»). Дам 3 ключевых совета за 60 секунд.",
        "h60_t1": "Возможные причины",
        "h60_t2": "Что сделать сейчас (24–48 ч)",
        "h60_t3": "Когда обратиться к врачу",
        "h60_serious": "Что серьёзное исключить",
        "ask_fb": "Это было полезно?",
        "fb_good": "👍 Нравится",
        "fb_bad": "👎 Не полезно",
        "fb_free": "📝 Отзыв",
        "fb_write": "Напишите короткий отзыв:",
        "fb_thanks": "Спасибо за отзыв! ✅",
        "youth_pack": "Молодёжный пакет",
        "gm_energy": "⚡ Энергия",
        "gm_energy_q": "Как энергия (1–5)?",
        "gm_energy_done": "Записал энергию — спасибо!",
        "gm_evening_btn": "⏰ Напомнить вечером",
        "hydrate_btn": "💧 Гидратация",
        "hydrate_nudge": "💧 Время для стакана воды",
        "skintip_btn": "🧴 Советы для кожи/тела",
        "skintip_sent": "Совет отправлен.",
        "daily_tip_prefix": "🍎 Подсказка дня:",
        "challenge_btn": "🎯 Челлендж 7 дней (вода)",
        "challenge_started": "Челлендж начат! Буду учитывать ваши ежедневные чек-ины.",
        "challenge_progress": "Прогресс челленджа: {d}/{len} дней.",
        "cycle_btn": "🩸 Цикл",
        "cycle_consent": "Хотите отслеживать цикл и получать мягкие подсказки вовремя?",
        "cycle_ask_last": "Введите дату последних месячных (ГГГГ-ММ-ДД):",
        "cycle_ask_len": "Средняя длина цикла (например, 28):",
        "cycle_saved": "Отслеживание цикла сохранено.",
        "quiet_saved": "Тихие часы сохранены: {qh}",
        "set_quiet_btn": "🌙 Тихие часы",
        "ask_quiet": "Введите тихие часы как ЧЧ:ММ-ЧЧ:ММ (локально), напр. 22:00-08:00",
        "evening_intro": "Вечерний чек-ин:",
        "evening_tip_btn": "🪄 Совет дня",
        "evening_set": "Вечерний чек-ин установлен на {t} (локально).",
        "evening_off": "Вечерний чек-ин отключён.",
        "ask_consent": "Можно прислать напоминание позже, чтобы узнать, как вы?",
        "yes": "Да", "no": "Нет",
        "unknown": "Нужно чуть больше деталей: где именно и сколько длится?",
        "thanks": "Принято 🙌",
        "back": "◀ Назад",
        "exit": "Выйти",
        "paused_on": "Напоминания поставлены на паузу. /resume — включить.",
        "paused_off": "Напоминания снова включены.",
        "deleted": "Все данные удалены. /start — начать заново.",
        "life_today": "Сегодня твой {n}-й день жизни 🎉. Цель — 36 500 (100 лет).",
        "life_percent": "Ты прошёл уже {p}% пути к 100 годам.",
        "life_estimate": "(оценочно по возрасту — укажи birth_date для точности)",
        "px": "С учётом профиля: {sex}, {age} лет; цель — {goal}.",
        "act_rem_4h": "⏰ Напомнить через 4 ч",
        "act_rem_eve": "⏰ Сегодня вечером",
        "act_rem_morn": "⏰ Завтра утром",
        "act_save_episode": "💾 Сохранить эпизод",
        "act_ex_neck": "🧘 5-мин упражнения для шеи",
        "act_er": "🚑 Когда срочно в скорую",
    },
    "uk": {
        "welcome":"Привіт! Я TendAI — турботливий асистент здоров’я.\nЗробімо короткий опитник (~40с) для персоналізації.",
        "help":"Короткі перевірки, план на 24–48 год, нагадування, щоденні чек-іни.\nКоманди: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /evening_on 20:00 /evening_off /settz +2 /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
        "privacy":"Я не замінюю лікаря. Зберігаємо мінімум даних для нагадувань. /delete_data — видалити.",
        "quick_title":"Швидкі дії",
        "quick_h60":"⚡ Здоров’я за 60 с",
        "quick_er":"🚑 Невідкладно",
        "quick_lab":"🧪 Лабораторія",
        "quick_rem":"⏰ Нагадування",
        "profile_intro":"Швидкий опитник (~40с). Можна натискати кнопки або писати відповідь.",
        "write":"✍️ Написати",
        "skip":"⏭️ Пропустити",
        "start_where":"З чого почнемо? (симптом/сон/харчування/аналізи/звички/довголіття)",
        "saved_profile":"Зберіг: ",
        "daily_gm":"Доброго ранку! 🌞 Як самопочуття сьогодні?",
        "gm_excellent":"👍 Чудово",
        "gm_ok":"🙂 Нормально",
        "gm_tired":"😐 Втома",
        "gm_pain":"🤕 Болить",
        "gm_skip":"⏭️ Пропустити",
        "mood_note":"✍️ Коментар",
        "mood_thanks":"Дякую! Гарного дня 👋",
        "h60_btn":"Здоров’я за 60 секунд",
        "h60_intro":"Коротко напишіть, що турбує. Дам 3 ключові поради за 60 секунд.",
        "h60_t1":"Можливі причини",
        "h60_t2":"Що зробити зараз (24–48 год)",
        "h60_t3":"Коли звернутись до лікаря",
        "h60_serious":"Що серйозне виключити",
        "ask_fb":"Це було корисно?",
        "fb_good":"👍 Подобається",
        "fb_bad":"👎 Не корисно",
        "fb_free":"📝 Відгук",
        "fb_write":"Напишіть короткий відгук:",
        "fb_thanks":"Дякую за відгук! ✅",
        "youth_pack":"Молодіжний пакет",
        "gm_energy":"⚡ Енергія",
        "gm_energy_q":"Як енергія (1–5)?",
        "gm_energy_done":"Записав енергію — дякую!",
        "gm_evening_btn":"⏰ Нагадати ввечері",
        "hydrate_btn":"💧 Гідратація",
        "hydrate_nudge":"💧 Час для склянки води",
        "skintip_btn":"🧴 Порада для шкіри/тіла",
        "skintip_sent":"Пораду надіслано.",
        "daily_tip_prefix":"🍎 Підказка дня:",
        "challenge_btn":"🎯 Челендж 7 днів (вода)",
        "challenge_started":"Челендж запущено!",
        "challenge_progress":"Прогрес: {d}/{len} днів.",
        "cycle_btn":"🩸 Цикл",
        "cycle_consent":"Відстежувати цикл і м’які поради?",
        "cycle_ask_last":"Вкажіть дату останніх менструацій (РРРР-ММ-ДД):",
        "cycle_ask_len":"Середня довжина циклу (напр., 28):",
        "cycle_saved":"Відстеження циклу збережено.",
        "quiet_saved":"Тихі години збережено: {qh}",
        "set_quiet_btn":"🌙 Тихі години",
        "ask_quiet":"Введіть як ГГ:ХХ-ГГ:ХХ (локально), напр. 22:00-08:00",
        "evening_intro":"Вечірній чек-ін:",
        "evening_tip_btn":"🪄 Порада дня",
        "evening_set":"Вечірній чек-ін на {t} (локально).",
        "evening_off":"Вимкнено вечірній чек-ін.",
        "ask_consent":"Можу надіслати нагадування пізніше, щоб дізнатись як ви?",
        "yes":"Так", "no":"Ні",
        "unknown":"Потрібно трохи більше деталей: де саме і як довго?",
        "thanks":"Прийнято 🙌",
        "back":"◀ Назад",
        "exit":"Вийти",
        "paused_on":"Нагадування призупинені. /resume — увімкнути.",
        "paused_off":"Нагадування знову увімкнені.",
        "deleted":"Усі дані видалено. /start — почати знову.",
        "life_today":"Сьогодні твій {n}-й день життя 🎉. Мета — 36 500 (100 років).",
        "life_percent":"Ти пройшов {p}% шляху до 100 років.",
        "life_estimate":"(орієнтовно за віком — вкажи birth_date для точності)",
        "px":"З урахуванням профілю: {sex}, {age} р.; мета — {goal}.",
        "act_rem_4h":"⏰ Нагадати через 4 год",
        "act_rem_eve":"⏰ Сьогодні ввечері",
        "act_rem_morn":"⏰ Завтра зранку",
        "act_save_episode":"💾 Зберегти епізод",
        "act_ex_neck":"🧘 5-хв для шиї",
        "act_er":"🚑 Коли терміново",
    },
}
T["es"] = T["en"]  # простая заглушка

# --- Personalized prefix shown before LLM reply ---
def personalized_prefix(lang: str, profile: dict) -> str:
    sex = (profile.get("sex") or "").strip()
    goal = (profile.get("goal") or "").strip()
    age_raw = str(profile.get("age") or "")
    m = re.search(r"\d+", age_raw)
    age = m.group(0) if m else ""
    if sum(bool(x) for x in (sex, age, goal)) >= 2:
        tpl = (T.get(lang) or T["en"]).get("px", T["en"]["px"])
        return tpl.format(sex=sex or "—", age=age or "—", goal=goal or "—")
    return ""


# ---------------- Helpers ----------------
def utcnow() -> datetime:
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

def _user_lang(uid: int) -> str:
    return norm_lang((users_get(uid) or {}).get("lang") or "en")


# ===== ONBOARDING GATE (мини-опрос) =====
GATE_FLAG_KEY = "menu_unlocked"

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


# === MINI-INTAKE ===
MINI_KEYS = ["sex","age","goal","conditions","meds_allergies","activity","diet_focus","steps_target","habits","birth_date"]
MINI_FREE_KEYS: Set[str] = {"meds_allergies","birth_date"}
MINI_STEPS = {
    "sex": {
        "ru":[("Мужской","male"),("Женский","female"),("Другое","other")],
        "en":[("Male","male"),("Female","female"),("Other","other")],
        "uk":[("Чоловіча","male"),("Жіноча","female"),("Інша","other")],
        "es":[("Hombre","male"),("Mujer","female"),("Otro","other")],
        "label":{"ru":"Пол:","en":"Sex:","uk":"Стать:","es":"Sexo:"}
    },
    "age": {
        "ru":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "en":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "uk":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "es":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "label":{"ru":"Возраст:","en":"Age:","uk":"Вік:","es":"Edad:"}
    },
    "goal": {
        "ru":[("Энергия","energy"),("Сон","sleep"),("Похудение","weight"),("Сила","strength"),("Долголетие","longevity")],
        "en":[("Energy","energy"),("Sleep","sleep"),("Weight","weight"),("Strength","strength"),("Longevity","longevity")],
        "uk":[("Енергія","energy"),("Сон","sleep"),("Вага","weight"),("Сила","strength"),("Довголіття","longevity")],
        "es":[("Energía","energy"),("Sueño","sleep"),("Peso","weight"),("Fuerza","strength"),("Longevidad","longevity")],
        "label":{"ru":"Главная цель:","en":"Main goal:","uk":"Мета:","es":"Objetivo:"}
    },
    "conditions": {
        "ru":[("Нет","none"),("Сердечно-сосуд.","cvd"),("ЩЖ/эндокр.","endocrine"),("ЖКТ","gi"),("Аллергия","allergy"),("Другое","other")],
        "en":[("None","none"),("Cardio/vascular","cvd"),("Thyroid/endocrine","endocrine"),("GI","gi"),("Allergy","allergy"),("Other","other")],
        "uk":[("Немає","none"),("Серцево-суд.","cvd"),("ЩЗ/ендокр.","endocrine"),("ШКТ","gi"),("Алергія","allergy"),("Інше","other")],
        "es":[("Ninguno","none"),("Cardio/vascular","cvd"),("Tiroides","endocrine"),("GI","gi"),("Alergia","allergy"),("Otro","other")],
        "label":{"ru":"Хронические состояния:","en":"Chronic conditions:","uk":"Хронічні стани:","es":"Condiciones crónicas:"}
    },
    "meds_allergies": {
        "ru":[], "en":[], "uk":[], "es":[],
        "label":{"ru":"Лекарства/добавки/аллергии (коротко):","en":"Meds/supplements/allergies (short):","uk":"Ліки/добавки/алергії (коротко):","es":"Medicamentos/suplementos/alergias (corto):"}
    },
    "activity": {
        "ru":[("Низкая","low"),("Средняя","mid"),("Высокая","high"),("Спорт","sport")],
        "en":[("Low","low"),("Medium","mid"),("High","high"),("Sport","sport")],
        "uk":[("Низька","low"),("Середня","mid"),("Висока","high"),("Спорт","sport")],
        "es":[("Baja","low"),("Media","mid"),("Alta","high"),("Deporte","sport")],
        "label":{"ru":"Активность:","en":"Activity:","uk":"Активність:","es":"Actividad:"}
    },
    "diet_focus": {
        "ru":[("Сбаланс.","balanced"),("Низкоугл.","lowcarb"),("Растит.","plant"),("Нерегул.","irregular")],
        "en":[("Balanced","balanced"),("Low-carb","lowcarb"),("Plant-based","plant"),("Irregular","irregular")],
        "uk":[("Збаланс.","balanced"),("Маловугл.","lowcarb"),("Рослинне","plant"),("Нерегул.","irregular")],
        "es":[("Equilibrada","balanced"),("Baja carb.","lowcarb"),("Vegetal","plant"),("Irregular","irregular")],
        "label":{"ru":"Питание чаще всего:","en":"Diet mostly:","uk":"Харчування переважно:","es":"Dieta:"}
    },
    "steps_target": {
        "ru":[("<5к","5000"),("5–8к","8000"),("8–12к","12000"),("Спорт","15000")],
        "en":[("<5k","5000"),("5–8k","8000"),("8–12k","12000"),("Sport","15000")],
        "uk":[("<5к","5000"),("5–8к","8000"),("8–12к","12000"),("Спорт","15000")],
        "es":[("<5k","5000"),("5–8k","8000"),("8–12k","12000"),("Deporte","15000")],
        "label":{"ru":"Шаги/активность:","en":"Steps/activity:","uk":"Кроки/активність:","es":"Pasos/actividad:"}
    },
    "habits": {
        "ru":[("Не курю","no_smoke"),("Курю","smoke"),("Алк. редко","alc_low"),("Алк. часто","alc_high"),("Кофеин 0–1","caf_low"),("Кофеин 2–3","caf_mid"),("Кофеин 4+","caf_high")],
        "en":[("No smoking","no_smoke"),("Smoking","smoke"),("Alcohol rare","alc_low"),("Alcohol often","alc_high"),("Caffeine 0–1","caf_low"),("Caffeine 2–3","caf_mid"),("Caffeine 4+","caf_high")],
        "uk":[("Не курю","no_smoke"),("Курю","smoke"),("Алк. рідко","alc_low"),("Алк. часто","alc_high"),("Кофеїн 0–1","caf_low"),("Кофеїн 2–3","caf_mid"),("Кофеїн 4+","caf_high")],
        "es":[("No fuma","no_smoke"),("Fuma","smoke"),("Alcohol raro","alc_low"),("Alcohol a menudo","alc_high"),("Cafeína 0–1","caf_low"),("Cafeína 2–3","caf_mid"),("Cafeína 4+","caf_high")],
        "label":{"ru":"Привычки (выберите ближе всего):","en":"Habits (pick closest):","uk":"Звички (оберіть ближче):","es":"Hábitos (elige):"}
    },
    "birth_date": {
        "ru":[], "en":[], "uk":[], "es":[],
        "label":{"ru":"Дата рождения (ГГГГ-ММ-ДД) — по желанию:","en":"Birth date (YYYY-MM-DD) — optional:","uk":"Дата народження (РРРР-ММ-ДД) — за бажанням:","es":"Fecha de nacimiento (AAAA-MM-DD) — opcional:"}
    },
}

# ---- Sheets + memory fallback ----
SHEETS_ENABLED = True
ss = None
ws_feedback = ws_users = ws_profiles = ws_episodes = ws_reminders = ws_daily = None
ws_rules = ws_challenges = None
GSPREAD_CLIENT: Optional[gspread.client.Client] = None

def _ws_headers(ws):
    try:
        return ws.row_values(1) if ws else []
    except Exception:
        return []

def _ws_ensure_columns(ws, desired_headers: List[str]):
    try:
        current = _ws_headers(ws)
        if not current:
            if ws.col_count < len(desired_headers):
                ws.add_cols(len(desired_headers) - ws.col_count)
            ws.append_row(desired_headers)
            return
        if ws.col_count < len(desired_headers):
            ws.add_cols(len(desired_headers) - ws.col_count)
        missing = [h for h in desired_headers if h not in current]
        if missing:
            for h in missing:
                ws.update_cell(1, len(current)+1, h)
                current.append(h)
    except Exception as e:
        logging.warning(f"ensure columns failed for {getattr(ws,'title','?')}: {e}")

def _sheets_init():
    global SHEETS_ENABLED, ss, ws_feedback, ws_users, ws_profiles, ws_episodes, ws_reminders, ws_daily, ws_rules, ws_challenges, GSPREAD_CLIENT
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

        def _ensure_ws(title: str, headers: List[str]):
            try:
                ws = ss.worksheet(title)
            except WorksheetNotFound:
                ws = ss.add_worksheet(title=title, rows=4000, cols=max(60, len(headers)))
                ws.append_row(headers)
            if not ws.get_all_values():
                ws.append_row(headers)
            _ws_ensure_columns(ws, headers)
            return ws

        ws_feedback   = _ensure_ws("Feedback",   ["timestamp","user_id","name","username","rating","comment"])
        ws_users      = _ensure_ws("Users",      ["user_id","username","lang","consent","tz_offset","checkin_hour","evening_hour","paused","last_seen","last_auto_date","last_auto_count","streak","streak_best","gm_last_date"])
        ws_profiles   = _ensure_ws("Profiles",   [
            "user_id","sex","age","goal","goals","conditions","meds","allergies",
            "sleep","activity","diet","diet_focus","steps_target","habits",
            "cycle_enabled","cycle_last_date","cycle_avg_len","last_cycle_tip_date",
            "quiet_hours","consent_flags","notes","updated_at","city","surgeries","ai_profile","birth_date"
        ])
        ws_episodes   = _ensure_ws("Episodes",   ["episode_id","user_id","topic","started_at","baseline_severity","red_flags","plan_accepted","target","reminder_at","next_checkin_at","status","last_update","notes"])
        ws_daily      = _ensure_ws("DailyCheckins",["timestamp","user_id","mood","energy","comment"])
        ws_rules      = _ensure_ws("Rules",      ["rule_id","topic","segment","trigger","advice_text","citation","last_updated","enabled"])
        ws_challenges = _ensure_ws("Challenges", ["user_id","challenge_id","name","start_date","length_days","days_done","status"])
        ws_reminders  = _ensure_ws("Reminders",  ["id","user_id","text","when_utc","created_at","status"])

        logging.info("Google Sheets connected.")
    except Exception as e:
        SHEETS_ENABLED = False
        logging.error(f"SHEETS disabled (fallback to memory) | Possibly 429 quota exceeded. Reason: {e}")

_sheets_init()

# --------- Memory fallback stores ----------
MEM_USERS: Dict[int, dict] = {}
MEM_PROFILES: Dict[int, dict] = {}
MEM_EPISODES: List[dict] = []
MEM_REMINDERS: List[dict] = []
MEM_FEEDBACK: List[dict] = []
MEM_DAILY: List[dict] = []
MEM_CHALLENGES: List[dict] = []
MEM_RULES: List[dict] = []

# --------- Sessions (runtime) ----------
sessions: Dict[int, dict] = {}

# -------- Wrappers: Users / Profiles / Daily / Episodes / Reminders / Feedback / Challenges --------
def _headers(ws): return _ws_headers(ws)

def users_get(uid: int) -> dict:
    if SHEETS_ENABLED:
        try:
            for r in ws_users.get_all_records():
                if str(r.get("user_id")) == str(uid):
                    return r
        except Exception as e:
            logging.warning(f"users_get fallback: {e}")
    return MEM_USERS.get(uid, {})

def users_upsert(uid: int, username: str, lang: str):
    base = {
        "user_id": str(uid),
        "username": username or "",
        "lang": lang,
        "consent": "no",
        "tz_offset": "0",
        "checkin_hour": DEFAULT_CHECKIN_LOCAL,
        "evening_hour": "",
        "paused": "no",
        "last_seen": iso(utcnow()),
        "last_auto_date": "",
        "last_auto_count": "0",
        "streak": "0",
        "streak_best": "0",
        "gm_last_date": "",
    }
    if SHEETS_ENABLED:
        try:
            vals = ws_users.get_all_records()
            hdr = _headers(ws_users)
            end_col = gsu.rowcol_to_a1(1, len(hdr)).rstrip("1")
            for i, r in enumerate(vals, start=2):
                if str(r.get("user_id")) == str(uid):
                    merged = {**r, **{k: base[k] for k in base if not str(r.get(k) or "").strip()}}
                    ws_users.update(f"A{i}:{end_col}{i}", [[merged.get(h, "") for h in hdr]])
                    return
            ws_users.append_row([base.get(h, "") for h in hdr])
            return
        except Exception as e:
            logging.warning(f"users_upsert -> memory fallback: {e}")
    MEM_USERS[uid] = {**MEM_USERS.get(uid, {}), **base}

def users_set(uid: int, field: str, value: str):
    if SHEETS_ENABLED:
        try:
            vals = ws_users.get_all_records()
            for i, r in enumerate(vals, start=2):
                if str(r.get("user_id")) == str(uid):
                    hdr = _headers(ws_users)
                    if field not in hdr:
                        _ws_ensure_columns(ws_users, hdr + [field])
                        hdr = _headers(ws_users)
                    ws_users.update_cell(i, hdr.index(field)+1, value)
                    return
        except Exception as e:
            logging.warning(f"users_set -> memory fallback: {e}")
    u = MEM_USERS.setdefault(uid, {})
    u[field] = value

def profiles_get(uid: int) -> dict:
    if SHEETS_ENABLED:
        try:
            for r in ws_profiles.get_all_records():
                if str(r.get("user_id")) == str(uid):
                    return r
        except Exception as e:
            logging.warning(f"profiles_get fallback: {e}")
    return MEM_PROFILES.get(uid, {})

def profiles_upsert(uid: int, patch: dict):
    patch = dict(patch or {})
    patch["user_id"] = str(uid)
    patch["updated_at"] = iso(utcnow())
    if SHEETS_ENABLED:
        try:
            vals = ws_profiles.get_all_records()
            hdr = _headers(ws_profiles)
            if hdr:
                for k in patch.keys():
                    if k not in hdr:
                        _ws_ensure_columns(ws_profiles, hdr + [k])
                        hdr = _headers(ws_profiles)
            for i, r in enumerate(vals, start=2):
                if str(r.get("user_id")) == str(uid):
                    merged = {**r, **patch}
                    ws_profiles.update(f"A{i}:{gsu.rowcol_to_a1(i, len(hdr))}", [[merged.get(h, "") for h in hdr]])
                    return
            ws_profiles.append_row([patch.get(h, "") for h in hdr])
            return
        except Exception as e:
            logging.warning(f"profiles_upsert -> memory fallback: {e}")
    MEM_PROFILES[uid] = {**MEM_PROFILES.get(uid, {}), **patch}

def feedback_add(ts: str, uid: int, name: str, username: str, rating: str, comment: str):
    row = {"timestamp":ts, "user_id":str(uid), "name":name, "username":username, "rating":rating, "comment":comment}
    if SHEETS_ENABLED:
        try:
            hdr = _headers(ws_feedback)
            ws_feedback.append_row([row.get(h, "") for h in hdr])
            return
        except Exception as e:
            logging.warning(f"feedback_add -> memory fallback: {e}")
    MEM_FEEDBACK.append(row)

def daily_add(ts: str, uid: int, mood: str="", comment: str="", energy: Optional[int]=None):
    row = {"timestamp":ts, "user_id":str(uid), "mood":mood, "energy":("" if energy is None else str(energy)), "comment":comment}
    if SHEETS_ENABLED:
        try:
            hdr = _headers(ws_daily)
            ws_daily.append_row([row.get(h, "") for h in hdr])
            return
        except Exception as e:
            logging.warning(f"daily_add -> memory fallback: {e}")
    MEM_DAILY.append(row)

def episode_create(uid: int, topic: str, severity: int=5, red: str="") -> str:
    eid = str(uuid.uuid4())
    row = {"episode_id":eid,"user_id":str(uid),"topic":topic,"started_at":iso(utcnow()),
           "baseline_severity":str(severity),"red_flags":red,"plan_accepted":"","target":"","reminder_at":"","next_checkin_at":"",
           "status":"open","last_update":iso(utcnow()),"notes":""}
    if SHEETS_ENABLED:
        try:
            hdr = _headers(ws_episodes)
            ws_episodes.append_row([row.get(h, "") for h in hdr])
        except Exception as e:
            logging.warning(f"episode_create -> memory fallback: {e}")
            MEM_EPISODES.append(row)
    else:
        MEM_EPISODES.append(row)
    return eid

def episode_set(eid: str, key: str, val: str):
    if SHEETS_ENABLED:
        try:
            vals = ws_episodes.get_all_records()
            hdr = _headers(ws_episodes)
            for i, r in enumerate(vals, start=2):
                if r.get("episode_id") == eid:
                    if key not in hdr:
                        _ws_ensure_columns(ws_episodes, hdr + [key]); hdr=_headers(ws_episodes)
                    row = [r.get(h, "") if h!=key else val for h in hdr]
                    ws_episodes.update(f"A{i}:{gsu.rowcol_to_a1(i,len(hdr))}", [row]); return
        except Exception as e:
            logging.warning(f"episode_set -> memory fallback: {e}")
    for r in MEM_EPISODES:
        if r.get("episode_id")==eid:
            r[key]=val; return

def episode_find_open(uid: int) -> Optional[dict]:
    if SHEETS_ENABLED:
        try:
            for r in ws_episodes.get_all_records():
                if str(r.get("user_id"))==str(uid) and (r.get("status")=="open"):
                    return r
        except Exception as e:
            logging.warning(f"episode_find_open fallback: {e}")
    for r in reversed(MEM_EPISODES):
        if r.get("user_id")==str(uid) and r.get("status")=="open":
            return r
    return None

def reminder_add(uid: int, text: str, when_utc: datetime) -> str:
    rid = str(uuid.uuid4())
    row = {"id":rid,"user_id":str(uid),"text":text,"when_utc":iso(when_utc),"created_at":iso(utcnow()),"status":"open"}
    if SHEETS_ENABLED:
        try:
            hdr = _headers(ws_reminders)
            ws_reminders.append_row([row.get(h, "") for h in hdr]); return rid
        except Exception as e:
            logging.warning(f"reminder_add -> memory fallback: {e}")
    MEM_REMINDERS.append(row); return rid

# ---------- Quickbar & Menus ----------
def quickbar_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["quick_h60"], callback_data="menu|h60")],
        [InlineKeyboardButton(T[lang]["quick_er"], callback_data="menu|er"),
         InlineKeyboardButton(T[lang]["quick_lab"], callback_data="menu|lab")],
        [InlineKeyboardButton(T[lang]["quick_rem"], callback_data="menu|rem")]
    ])

async def show_quickbar(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str):
    try:
        await context.bot.send_message(chat_id, T[lang]["quick_title"], reply_markup=quickbar_kb(lang))
    except Exception as e:
        logging.warning(f"show_quickbar failed: {e}")

def inline_topic_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ " + T[lang]["h60_btn"], callback_data="menu|h60")],
        [InlineKeyboardButton("🧪 Lab", callback_data="menu|lab"),
         InlineKeyboardButton("🩺 Sleep", callback_data="menu|sleep")],
        [InlineKeyboardButton("🥗 Food", callback_data="menu|food"),
         InlineKeyboardButton("🏃 Habits", callback_data="menu|habits")],
    ])

# ---------- LLM helpers ----------
def _fmt_bullets(items: list) -> str:
    return "\n".join([f"• {x}" for x in items if isinstance(x, str) and x.strip()])

SYS_H60 = (
    "You are TendAI — a concise, warm, professional assistant (not a doctor). "
    "Answer strictly in {lang}. JSON ONLY: {\"causes\":[\"...\"],\"serious\":\"...\",\"do_now\":[\"...\"],\"see_doctor\":[\"...\"]}. "
    "Rules: 2–4 simple causes; exactly 1 serious item to rule out; 3–5 concrete do_now steps; 2–3 see_doctor cues."
)

def health60_make_plan(lang: str, symptom_text: str, profile: dict) -> str:
    fallback_map = {
        "ru": (f"{T['ru']['h60_t1']}:\n• Бытовые причины\n{T['ru']['h60_serious']}: • Исключить редкие, но серьёзные при ухудшении\n\n"
               f"{T['ru']['h60_t2']}:\n• Вода 300–500 мл\n• Отдых 15–20 мин\n• Проветривание\n\n"
               f"{T['ru']['h60_t3']}:\n• Ухудшение\n• Высокая температура/красные флаги\n• Боль ≥7/10"),
        "uk": (f"{T['uk']['h60_t1']}:\n• Побутові причини\n{T['uk']['h60_serious']}: • Виключити рідкісні, але серйозні при погіршенні\n\n"
               f"{T['uk']['h60_t2']}:\n• Вода 300–500 мл\n• Відпочинок 15–20 хв\n• Провітрювання\n\n"
               f"{T['uk']['h60_t3']}:\n• Погіршення\n• Висока температура/прапорці\n• Біль ≥7/10"),
        "en": (f"{T['en']['h60_t1']}:\n• Everyday causes\n{T['en']['h60_serious']}: • Rule out rare but serious if worsening\n\n"
               f"{T['en']['h60_t2']}:\n• Drink 300–500 ml water\n• 15–20 min rest\n• Ventilate\n\n"
               f"{T['en']['h60_t3']}:\n• Worsening\n• High fever/red flags\n• Pain ≥7/10"),
    }
    fallback = fallback_map.get(lang, fallback_map["en"])
    if not oai:
        return fallback
    sys = SYS_H60.replace("{lang}", lang)
    user = {"symptom": (symptom_text or "").strip()[:500],
            "profile": {k: profile.get(k, "") for k in ["sex","age","goal","conditions","meds","sleep","activity","diet","diet_focus","steps_target"]}}
    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL, temperature=0.2, max_tokens=420,
            response_format={"type":"json_object"},
            messages=[{"role":"system","content":sys},{"role":"user","content":json.dumps(user, ensure_ascii=False)}]
        )
        data = json.loads(resp.choices[0].message.content.strip())
        causes = _fmt_bullets(data.get("causes") or [])
        serious = (data.get("serious") or "").strip()
        do_now = _fmt_bullets(data.get("do_now") or [])
        see_doc = _fmt_bullets(data.get("see_doctor") or [])
        parts = []
        if causes: parts.append(f"{T[lang]['h60_t1']}:\n{causes}")
        if serious: parts.append(f"{T[lang]['h60_serious']}: {serious}")
        if do_now: parts.append(f"\n{T[lang]['h60_t2']}:\n{do_now}")
        if see_doc: parts.append(f"\n{T[lang]['h60_t3']}:\n{see_doc}")
        return "\n".join(parts).strip()
    except Exception as e:
        logging.error(f"health60 LLM error: {e}")
        return fallback

def _get_skin_tip(lang: str) -> str:
    pool_ru = [
        "Мягкое SPF каждый день — самая недооценённая инвестиция в кожу.",
        "Душ: тёплая вода, не горячая; 3–5 минут — кожа скажет спасибо.",
        "Умывалка без SLS, увлажняющий крем после воды — минимум, который работает."
    ]
    pool_en = [
        "Daily light SPF is the most underrated skin investment.",
        "Keep showers warm, not hot; 3–5 minutes helps your skin barrier.",
        "Use a gentle cleanser and moisturize right after water."
    ]
    pools = {"ru": pool_ru, "uk": pool_ru, "en": pool_en, "es": pool_en}
    return random.choice(pools.get(lang, pool_en))

# ---------- Scheduling stubs (реализация хронометок в Part 2) ----------
def _has_jq_ctx(context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        return bool(context.application.job_queue)
    except Exception:
        return False

def schedule_from_sheet_on_start(app):  # stub
    logging.info("schedule_from_sheet_on_start: stub (реальная логика — в Части 2)")

def schedule_daily_checkin(app, uid: int, tz_off: int, hhmm: str, lang: str):  # stub
    logging.info(f"schedule_daily_checkin (stub) uid={uid} at {hhmm} tz={tz_off}")

def schedule_evening_checkin(app, uid: int, tz_off: int, hhmm: str, lang: str):  # stub
    logging.info(f"schedule_evening_checkin (stub) uid={uid} at {hhmm} tz={tz_off}")

# ---------- Commands ----------
async def post_init(app):
    me = await app.bot.get_me()
    logging.info(f"BOT READY: @{me.username} (id={me.id})")
    try:
        schedule_from_sheet_on_start(app)
    except Exception as e:
        logging.warning(f"schedule_from_sheet_on_start failed: {e}")

def update_last_seen(uid: int):
    users_set(uid, "last_seen", iso(utcnow()))

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = norm_lang(getattr(user, "language_code", None))
    users_upsert(user.id, user.username or "", lang)
    context.user_data["lang"] = lang
    sessions.setdefault(user.id, {})["last_user_text"] = "/start"
    update_last_seen(user.id)

    await update.message.reply_text(T[lang]["welcome"], reply_markup=ReplyKeyboardRemove())

    prof = profiles_get(user.id) or {}
    if profile_is_incomplete(prof):
        await gate_show(update, context)
    else:
        await update.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
        await show_quickbar(context, update.effective_chat.id, lang)

    u = users_get(user.id)
    if (u.get("consent") or "").lower() not in {"yes","no"}:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["yes"], callback_data="consent|yes"),
                                    InlineKeyboardButton(T[lang]["no"], callback_data="consent|no")]])
        await update.message.reply_text(T[lang]["ask_consent"], reply_markup=kb)

    tz_off = int(str(u.get("tz_offset") or "0"))
    hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, user.id, tz_off, hhmm, lang)
        eh = (u.get("evening_hour") or "").strip()
        if eh:
            schedule_evening_checkin(context.application, user.id, tz_off, eh, lang)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _user_lang(update.effective_user.id)
    update_last_seen(update.effective_user.id)
    await update.message.reply_text(T[lang]["help"])

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _user_lang(update.effective_user.id)
    update_last_seen(update.effective_user.id)
    await update.message.reply_text(T[lang]["privacy"])

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "yes")
    lang = _user_lang(uid); update_last_seen(uid)
    await update.message.reply_text(T[lang]["paused_on"])

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "no")
    lang = _user_lang(uid); update_last_seen(uid)
    await update.message.reply_text(T[lang]["paused_off"])

async def cmd_delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    update_last_seen(uid)
    if SHEETS_ENABLED:
        try:
            vals = ws_users.get_all_values()
            for i in range(2, len(vals)+1):
                if ws_users.cell(i,1).value == str(uid):
                    ws_users.delete_rows(i); break
            try:
                vals = ws_profiles.get_all_values()
                idxs = []
                for i in range(2, len(vals)+1):
                    if ws_profiles.cell(i,1).value == str(uid):
                        idxs.append(i)
                for shift, i in enumerate(idxs):
                    ws_profiles.delete_rows(i - shift)
            except Exception:
                pass
        except Exception as e:
            logging.warning(f"delete_data sheets err: {e}")
    else:
        MEM_USERS.pop(uid, None); MEM_PROFILES.pop(uid, None)
        global MEM_EPISODES, MEM_REMINDERS, MEM_DAILY
        MEM_EPISODES = [r for r in MEM_EPISODES if r["user_id"]!=str(uid)]
        MEM_REMINDERS = [r for r in MEM_REMINDERS if r["user_id"]!=str(uid)]
        MEM_DAILY = [r for r in MEM_DAILY if r["user_id"]!=str(uid)]
    lang = norm_lang(getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(T[lang]["deleted"], reply_markup=ReplyKeyboardRemove())

async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    txt = (update.message.text or "").strip()
    m = re.search(r"(-?\+?\d{1,2})", txt)
    if not m:
        await update.message.reply_text("Usage: /settz +2  |  Пример: /settz +3")
        return
    try:
        off = int(m.group(1)); off = max(-12, min(14, off))
        users_set(uid, "tz_offset", str(off))
        await update.message.reply_text(f"UTC offset set: {off:+d}")
        u = users_get(uid); hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
        if _has_jq_ctx(context):
            schedule_daily_checkin(context.application, uid, off, hhmm, lang)
    except Exception as e:
        logging.error(f"/settz error: {e}")
        await update.message.reply_text("Failed to set timezone offset.")

async def cmd_checkin_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    txt = (update.message.text or "").strip()
    m = re.search(r"([01]?\d|2[0-3]):([0-5]\d)", txt)
    hhmm = m.group(0) if m else DEFAULT_CHECKIN_LOCAL
    users_set(uid, "checkin_hour", hhmm); users_set(uid, "paused", "no")
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, uid, int((users_get(uid) or {}).get("tz_offset") or 0), hhmm, lang)
    await update.message.reply_text(f"Daily check-in set to {hhmm} (local).")

async def cmd_checkin_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "yes")
    await update.message.reply_text("Daily check-in disabled.")

async def cmd_health60(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    sessions.setdefault(uid, {})["awaiting_h60_text"] = True
    await update.message.reply_text(T[lang]["h60_intro"])
    await show_quickbar(context, update.effective_chat.id, lang)

async def cmd_mood(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["gm_excellent"], callback_data="gm|mood|excellent"),
         InlineKeyboardButton(T[lang]["gm_ok"],        callback_data="gm|mood|ok")],
        [InlineKeyboardButton(T[lang]["gm_tired"],     callback_data="gm|mood|tired"),
         InlineKeyboardButton(T[lang]["gm_pain"],      callback_data="gm|mood|pain")],
        [InlineKeyboardButton(T[lang]["gm_skip"],      callback_data="gm|skip")],
        [InlineKeyboardButton(T[lang]["mood_note"],    callback_data="gm|note")]
    ])
    await update.message.reply_text(T[lang]["daily_gm"], reply_markup=kb)

async def cmd_energy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    row = [InlineKeyboardButton(str(i), callback_data=f"energy|rate|{i}") for i in range(1,6)]
    kb = InlineKeyboardMarkup([row])
    await update.message.reply_text(T[lang]["gm_energy_q"], reply_markup=kb)

# ---------- Text handler ----------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    text = (update.message.text or "").strip()
    sess = sessions.setdefault(uid, {})

    # мини-опрос: свободный ввод
    if sess.get("awaiting_free_key"):
        key = sess.pop("awaiting_free_key")
        ma = sess.setdefault("mini_answers", {})
        ma[key] = text
        # шаг вперёд
        step = int(sess.get("mini_step", 0)) + 1
        sess["mini_step"] = step
        await update.message.reply_text(T[lang]["thanks"])
        # показать следующий шаг
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return

    # заметка к настроению
    if sess.get("awaiting_mood_note"):
        sess.pop("awaiting_mood_note", None)
        daily_add(iso(utcnow()), uid, comment=text)
        await update.message.reply_text(T[lang]["mood_thanks"])
        return

    # Health60 свободный ввод
    if sess.get("awaiting_h60_text"):
        sess["awaiting_h60_text"] = False
        prof = profiles_get(uid) or {}
        plan = health60_make_plan(lang, text, prof)
        px = personalized_prefix(lang, prof)
        reply = (px + "\n\n" if px else "") + plan
        await update.message.reply_text(reply)
        await show_quickbar(context, update.effective_chat.id, lang)
        return

    # По умолчанию — просто сохраняем "последний текст"
    sess["last_user_text"] = text
    await update.message.reply_text(T[lang]["unknown"])

# ---------- CallbackQueryHandler ----------
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    lang = _user_lang(uid)
    data = q.data or ""
    sess = sessions.setdefault(uid, {})

    # Гейт/интейк
    if data == "intake:start":
        sess.clear()
        sess.update({"mini_active": True, "mini_step": 0, "mini_answers": {}})
        await q.edit_message_reply_markup(None)
        await context.bot.send_message(update.effective_chat.id, T[lang]["profile_intro"])
        # первый шаг
        label = MINI_STEPS[MINI_KEYS[0]]["label"][lang]
        await context.bot.send_message(update.effective_chat.id, label, reply_markup=build_mini_kb(lang, MINI_KEYS[0]))
        return
    if data == "gate:skip":
        await q.edit_message_reply_markup(None)
        await context.bot.send_message(update.effective_chat.id, T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
        await show_quickbar(context, update.effective_chat.id, lang)
        return

    # MINI choices
    if data.startswith("mini|"):
        parts = data.split("|")
        action = parts[1] if len(parts)>1 else ""
        if action == "choose" and len(parts) >= 4:
            key, val = parts[2], parts[3]
            ma = sess.setdefault("mini_answers", {})
            ma[key] = val
            sess["mini_step"] = int(sess.get("mini_step", 0)) + 1
            await q.edit_message_reply_markup(None)
            await ask_next_mini(context, update.effective_chat.id, lang, uid)
            return
        if action == "write" and len(parts) >= 3:
            key = parts[2]
            sess["awaiting_free_key"] = key
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(update.effective_chat.id, T[lang]["write"])
            return
        if action == "skip" and len(parts) >= 3:
            # просто двигаем шаг
            sess["mini_step"] = int(sess.get("mini_step", 0)) + 1
            await q.edit_message_reply_markup(None)
            await ask_next_mini(context, update.effective_chat.id, lang, uid)
            return

    # Consent
    if data.startswith("consent|"):
        choice = data.split("|",1)[1]
        users_set(uid, "consent", "yes" if choice=="yes" else "no")
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(T[lang]["thanks"])
        return

    # Main menu routing
    if data.startswith("menu|"):
        route = data.split("|",1)[1]
        if route == "h60":
            sessions.setdefault(uid, {})["awaiting_h60_text"] = True
            await q.message.reply_text(T[lang]["h60_intro"])
            return
        if route == "rem":
            rid = reminder_add(uid, "Hydrate", utcnow()+timedelta(hours=4))
            await q.message.reply_text("Reminder set (4h).")
            return
        if route == "er":
            await q.message.reply_text("If severe chest pain, trouble breathing, confusion, or fainting — call local emergency services.")
            return
        await q.message.reply_text(T[lang]["thanks"])
        return

    # Good morning mood
    if data.startswith("gm|"):
        parts = data.split("|")
        action = parts[1] if len(parts)>1 else ""
        if action == "mood" and len(parts)>=3:
            mood = parts[2]
            daily_add(iso(utcnow()), uid, mood=mood)
            await q.edit_message_reply_markup(None)
            await q.message.reply_text(T[lang]["mood_thanks"])
            return
        if action == "note":
            sessions.setdefault(uid,{})["awaiting_mood_note"] = True
            await q.message.reply_text(T[lang]["mood_note"])
            return
        if action == "skip":
            await q.edit_message_reply_markup(None)
            await q.message.reply_text(T[lang]["thanks"])
            return

    # Energy
    if data.startswith("energy|rate|"):
        try:
            score = int(data.split("|")[2])
        except Exception:
            score = None
        daily_add(iso(utcnow()), uid, energy=score or 0)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(T[lang]["gm_energy_done"])
        return

# ====== END OF PART 1/2 ======
# =========================
# TendAI — Part 2: Logic, callbacks, run
# =========================

# -------- Доп. команды (вечерний чек-ин, советы, профиль, цикл) --------
async def cmd_evening_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    txt = (update.message.text or "").strip()
    m = re.search(r"([01]?\d|2[0-3]):([0-5]\d)", txt)
    hhmm = m.group(0) if m else DEFAULT_EVENING_LOCAL
    users_set(uid, "evening_hour", hhmm)
    if _has_jq_ctx(context):
        schedule_evening_checkin(context.application, uid, _user_tz_off(uid), hhmm, lang)
    await update.message.reply_text(T[lang]["evening_set"].format(t=hhmm))

async def cmd_evening_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    users_set(uid, "evening_hour", "")
    await update.message.reply_text(T[lang]["evening_off"])

async def cmd_hydrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    await update.message.reply_text(T[lang]["hydrate_nudge"])

async def cmd_skintip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    prof = profiles_get(uid) or {}
    sex = (prof.get("sex") or "")
    try:
        age = int(re.search(r"\d+", str(prof.get("age","") or "0")).group(0))
    except Exception:
        age = 0
    await update.message.reply_text(f"{T[lang]['daily_tip_prefix']} {_get_skin_tip(lang, sex, age)}")

async def cmd_youth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    prof = profiles_get(uid) or {}
    lm = life_metrics(prof)
    bar = progress_bar(lm["percent_to_100"])
    text = (T[lang]["youth_pack"] + "\n" +
            T[lang]["life_today"].format(n=lm["days_lived"]) + "\n" +
            T[lang]["life_percent"].format(p=lm["percent_to_100"]) + "\n" +
            f"{bar}")
    await update.message.reply_text(text)

async def cmd_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    # простое включение трекинга — дальнейшие шаги в меню (cb_handler)
    profiles_upsert(uid, {"cycle_enabled":"yes"})
    await update.message.reply_text(T[lang]["cycle_consent"])

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    u = users_get(uid) or {}
    p = profiles_get(uid) or {}
    key_order = ["sex","age","goal","conditions","meds","allergies","activity","diet_focus","steps_target","habits","birth_date","quiet_hours","city"]
    parts = [f"id: {uid}", f"lang: {u.get('lang','')}  tz: {u.get('tz_offset','0')}"]
    for k in key_order:
        v = str(p.get(k,"")).strip()
        if v:
            parts.append(f"{k}: {v}")
    await update.message.reply_text(T[lang]["saved_profile"] + "; ".join(parts))


# -------- Свободный текст: мини-опрос, заметки, Health60 --------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    lang = _user_lang(uid)
    text = (update.message.text or "").strip()
    update_last_seen(uid)

    s = sessions.setdefault(uid, {})

    # ввод для "mini|write|<key>"
    free_key = s.get("awaiting_free_key")
    if free_key:
        # валидация для birth_date
        if free_key == "birth_date":
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
                await update.message.reply_text({"ru":"Формат: ГГГГ-ММ-ДД",
                                                 "uk":"Формат: РРРР-ММ-ДД",
                                                 "en":"Use YYYY-MM-DD",
                                                 "es":"Usa AAAA-MM-DD"}[lang])
                return
        # сохранить ответ и шаг вперёд
        mini_handle_choice(uid, free_key, text)
        s.pop("awaiting_free_key", None)
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return

    # заметка к настроению
    if s.get("awaiting_mood_note"):
        daily_add(iso(utcnow()), uid, mood="", comment=text)
        s.pop("awaiting_mood_note", None)
        await update.message.reply_text(T[lang]["mood_thanks"])
        return

    # Health60 свободное описание симптома
    if s.get("awaiting_h60_text"):
        s["awaiting_h60_text"] = False
        prof = profiles_get(uid) or {}
        prefix = personalized_prefix(lang, prof)
        plan = health60_make_plan(lang, text, prof)
        output = (prefix + "\n\n" if prefix else "") + plan
        # быстрые действия под ответом
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(T[lang]["act_rem_4h"], callback_data="h60|rem|4h"),
             InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="h60|rem|eve")],
            [InlineKeyboardButton(T[lang]["act_ex_neck"], callback_data="h60|neck"),
             InlineKeyboardButton(T[lang]["act_save_episode"], callback_data="h60|save")],
            [InlineKeyboardButton(T[lang]["fb_good"], callback_data="h60|fb|good"),
             InlineKeyboardButton(T[lang]["fb_bad"],  callback_data="h60|fb|bad"),
             InlineKeyboardButton(T[lang]["fb_free"], callback_data="h60|fb|free")],
        ])
        await update.message.reply_text(output, reply_markup=kb)
        return

    # если ничего не ожидаем — мягкий роутер (простая заглушка)
    prof = profiles_get(uid) or {}
    data = llm_router_answer(text, lang, prof)
    prefix = personalized_prefix(lang, prof)
    reply = (prefix + "\n\n" if prefix else "") + (data.get("assistant_reply") or T[lang]["unknown"])
    await send_unique(update.message, uid, reply)
    await show_quickbar(context, update.effective_chat.id, lang)


# -------- Обработчик нажатий (ЕДИНСТВЕННЫЙ cb_handler) --------
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    uid = query.from_user.id
    lang = _user_lang(uid)
    data = query.data or ""
    update_last_seen(uid)

    try:
        # --- Гейт/интейк ---
        if data == "intake:start":
            await query.answer()
            await start_mini_intake(context, query.message.chat_id, lang, uid)
            return

        if data == "gate:skip":
            await query.answer()
            await query.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
            await show_quickbar(context, query.message.chat_id, lang)
            return

        # --- Мини-опрос ---
        if data.startswith("mini|choose|"):
            _, _, key, value = data.split("|", 3)
            mini_handle_choice(uid, key, value)
            await query.answer()
            await ask_next_mini(context, query.message.chat_id, lang, uid)
            return

        if data.startswith("mini|write|"):
            _, _, key = data.split("|", 2)
            sessions.setdefault(uid, {})["awaiting_free_key"] = key
            await query.answer()
            await query.message.reply_text(T[lang]["write"])
            return

        if data.startswith("mini|skip|"):
            _, _, key = data.split("|", 2)
            mini_handle_choice(uid, key, "")
            await query.answer()
            await ask_next_mini(context, query.message.chat_id, lang, uid)
            return

        # --- Согласие на фоллоу-ап ---
        if data.startswith("consent|"):
            _, ans = data.split("|", 1)
            users_set(uid, "consent", "yes" if ans == "yes" else "no")
            await query.answer()
            await query.message.reply_text(T[lang]["thanks"])
            return

        # --- Меню быстрых действий ---
        if data == "menu|h60":
            sessions.setdefault(uid, {})["awaiting_h60_text"] = True
            await query.answer()
            await query.message.reply_text(T[lang]["h60_intro"])
            return

        if data == "menu|er":
            await query.answer()
            txt = {"ru":"🚑 Срочно: сильная одышка, боль/сдавление в груди, внезапная слабость одной стороны, спутанность сознания, кровотечение, травма головы — вызывайте скорую.",
                   "uk":"🚑 Негайно: сильна задишка, біль/стиснення у грудях, різка слабкість однієї сторони, сплутаність, кровотеча, травма голови — викликайте швидку.",
                   "en":"🚑 Urgent: severe shortness of breath, chest pain/pressure, sudden one-sided weakness, confusion, major bleeding, head trauma — call emergency."}[lang]
            await query.message.reply_text(txt)
            return

        if data == "menu|lab":
            await query.answer()
            await query.message.reply_text({"ru":"🧪 Лаб: добавлю список базовых анализов в следующем апдейте.",
                                            "uk":"🧪 Лаб: додам список базових аналізів у наступному апдейті.",
                                            "en":"🧪 Lab: a short baseline panel will be added in the next update."}[lang])
            return

        if data == "menu|sleep":
            await query.answer()
            await query.message.reply_text({"ru":"🛌 Сон: цель — стабильный график и 7–8 ч сна.",
                                            "uk":"🛌 Сон: мета — стабільний графік і 7–8 годин.",
                                            "en":"🛌 Sleep: target 7–8h, stable schedule."}[lang])
            return

        if data == "menu|food":
            await query.answer()
            await query.message.reply_text({"ru":"🥗 Питание: больше белка/клетчатки, меньше ультрапереработанного.",
                                            "uk":"🥗 Харчування: більше білка/клітковини, менше ультрапереробленого.",
                                            "en":"🥗 Food: more protein/fiber, less ultra-processed."}[lang])
            return

        if data == "menu|habits":
            await query.answer()
            await query.message.reply_text({"ru":"🏃 Привычки: 6–8k шагов/день + короткие растяжки.",
                                            "uk":"🏃 Звички: 6–8k кроків/день + короткі розтяжки.",
                                            "en":"🏃 Habits: 6–8k steps/day + short stretching."}[lang])
            return

        if data == "menu|rem":
            await query.answer()
            # пример одноразового напоминания через 4 часа
            when_local = user_local_now(uid) + timedelta(hours=4)
            qh = _user_quiet_hours(uid)
            when_local = adjust_out_of_quiet(when_local, qh)
            when_utc = local_to_utc_dt(uid, when_local)
            rid = reminder_add(uid, "gentle-followup", when_utc)
            if _has_jq_ctx(context):
                context.application.job_queue.run_once(job_oneoff_reminder, when=timedelta(seconds=max(5, (when_utc-utcnow()).total_seconds())), data={"user_id":uid,"reminder_id":rid})
            await query.message.reply_text(T[lang]["thanks"])
            return

        # --- Утренний чек-ин, настроение ---
        if data.startswith("gm|mood|"):
            _, _, mood = data.split("|", 2)
            daily_add(iso(utcnow()), uid, mood=mood)
            users_set(uid, "gm_last_date", datetime.utcnow().strftime("%Y-%m-%d"))
            await query.answer()
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["mood_note"], callback_data="gm|note")]])
            await query.message.reply_text(T[lang]["mood_thanks"], reply_markup=kb)
            return

        if data == "gm|note":
            sessions.setdefault(uid, {})["awaiting_mood_note"] = True
            await query.answer()
            await query.message.reply_text(T[lang]["mood_note"])
            return

        if data == "gm|skip":
            await query.answer()
            await query.message.reply_text(T[lang]["thanks"])
            return

        # --- Оценка энергии ---
        if data.startswith("energy|rate|"):
            _, _, val = data.split("|", 2)
            try:
                v = int(val)
            except:
                v = 3
            daily_add(iso(utcnow()), uid, energy=v)
            await query.answer()
            await query.message.reply_text(T[lang]["gm_energy_done"])
            return

        # --- Health60 быстрые действия ---
        if data == "h60|neck":
            await query.answer()
            txt = {"ru":"🧘 5 минут: круговые движения плечами, лёгкая растяжка трапеций, повороты головы без боли.",
                   "uk":"🧘 5 хв: кругові рухи плечима, мʼяка розтяжка трапецій, повороти голови без болю.",
                   "en":"🧘 5 min: shoulder rolls, gentle trapezius stretch, pain-free neck turns."}[lang]
            await query.message.reply_text(txt)
            return

        if data == "h60|save":
            eid = episode_create(uid, topic="h60", severity=5)
            episode_set(eid, "notes", "saved from Health60")
            await query.answer()
            await query.message.reply_text(T[lang]["thanks"])
            return

        if data.startswith("h60|rem|"):
            _, _, which = data.split("|", 2)
            now_local = user_local_now(uid)
            if which == "4h":
                when_local = now_local + timedelta(hours=4)
            else:  # 'eve'
                hh, mm = hhmm_tuple(users_get(uid).get("evening_hour") or DEFAULT_EVENING_LOCAL)
                when_local = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if when_local < now_local:
                    when_local += timedelta(days=1)
            when_local = adjust_out_of_quiet(when_local, _user_quiet_hours(uid))
            rid = reminder_add(uid, "h60-followup", local_to_utc_dt(uid, when_local))
            if _has_jq_ctx(context):
                delay = max(5, (local_to_utc_dt(uid, when_local)-utcnow()).total_seconds())
                context.application.job_queue.run_once(job_oneoff_reminder, when=timedelta(seconds=delay), data={"user_id":uid,"reminder_id":rid})
            await query.answer()
            await query.message.reply_text(T[lang]["thanks"])
            return

        if data.startswith("h60|fb|"):
            _, _, fb = data.split("|", 2)
            feedback_add(iso(utcnow()), uid, query.from_user.full_name or "", query.from_user.username or "", fb, "")
            if fb == "free":
                sessions.setdefault(uid, {})["awaiting_feedback_free"] = True
                await query.message.reply_text(T[lang]["fb_write"])
            else:
                await query.message.reply_text(T[lang]["fb_thanks"])
            await query.answer()
            return

    except Exception as e:
        logging.error(f"cb_handler error: {e}")

    # дефолт
    try:
        await query.answer()
    except Exception:
        pass


# -------- Регистрация команд и запуск бота --------
def build_app() -> "Application":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Команды
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("privacy",      cmd_privacy))
    app.add_handler(CommandHandler("pause",        cmd_pause))
    app.add_handler(CommandHandler("resume",       cmd_resume))
    app.add_handler(CommandHandler("delete_data",  cmd_delete_data))
    app.add_handler(CommandHandler("settz",        cmd_settz))
    app.add_handler(CommandHandler("checkin_on",   cmd_checkin_on))
    app.add_handler(CommandHandler("checkin_off",  cmd_checkin_off))
    app.add_handler(CommandHandler("evening_on",   cmd_evening_on))
    app.add_handler(CommandHandler("evening_off",  cmd_evening_off))
    app.add_handler(CommandHandler("hydrate",      cmd_hydrate))
    app.add_handler(CommandHandler("skintip",      cmd_skintip))
    app.add_handler(CommandHandler("youth",        cmd_youth))
    app.add_handler(CommandHandler("cycle",        cmd_cycle))
    app.add_handler(CommandHandler("profile",      cmd_profile))

    # Переключение языка
    app.add_handler(CommandHandler("ru",           cmd_lang_ru))
    app.add_handler(CommandHandler("en",           cmd_lang_en))
    app.add_handler(CommandHandler("uk",           cmd_lang_uk))
    app.add_handler(CommandHandler("es",           cmd_lang_es))

    # Свободный текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=1)

    # ВНИМАНИЕ: регистрация callback-хендлера ровно ОДИН раз,
    # ПОСЛЕ определения cb_handler (нет никаких _old_cb_handler)
    app.add_handler(CallbackQueryHandler(cb_handler), group=2)

    return app


def main():
    if not TELEGRAM_TOKEN:
        logging.error("TELEGRAM_TOKEN is not set in environment.")
        return
    app = build_app()
    logging.info("Starting polling...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
