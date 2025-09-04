# -*- coding: utf-8 -*-
# =========================
# TendAI — Part 1: Base, UX, Intake, Personalization
# =========================

import os, re, json, uuid, logging, random
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Set
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

# ---------- OpenAI (optional) ----------
from openai import OpenAI

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

AUTO_MAX_PER_DAY = 2  # защита от спама авто-уведомлений, включим в Части 2

# OpenAI client (по возможности)
oai: Optional[OpenAI] = None
try:
    if OPENAI_API_KEY:
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
        # Tone — коротко, тепло, без «давления»
        "welcome": "Hi! I’m TendAI — a caring health buddy. Short, useful, friendly.\nLet’s do a 40s intake to tailor help.",
        "help": "I can: quick checkups, 24–48h plans, reminders, daily check-ins.\nCommands: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /evening_on 20:00 /evening_off /settz +2 /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
        "privacy": "I’m not a medical service. Minimal data for reminders only. Use /delete_data to erase.",
        # Quickbar (Hybrid UI — всегда видна)
        "quick_title": "Quick actions",
        "quick_h60": "⚡ Health in 60s",
        "quick_er": "🚑 Emergency",
        "quick_lab": "🧪 Lab",
        "quick_rem": "⏰ Reminder",
        # Intake
        "profile_intro": "Quick intake (~40s). One question at a time.",
        "write": "✍️ Type answer",
        "skip": "⏭️ Skip",
        "start_where": "Where to start? symptom / sleep / nutrition / labs / habits / longevity",
        "saved_profile": "Saved: ",
        # Daily check-in
        "daily_gm": "Good morning! 🌞 How do you feel today?",
        "gm_excellent": "👍 Excellent",
        "gm_ok": "🙂 Okay",
        "gm_tired": "😐 Tired",
        "gm_pain": "🤕 In pain",
        "gm_skip": "⏭️ Skip today",
        "mood_note": "✍️ Comment",
        "mood_thanks": "Thanks! Have a smooth day 👋",
        # Health60
        "h60_btn": "Health in 60 seconds",
        "h60_intro": "What’s bothering you? (e.g., “headache”, “fatigue”, “stomach pain”). I’ll give 3 key tips.",
        "h60_t1": "Possible causes",
        "h60_t2": "Do now (24–48h)",
        "h60_t3": "When to see a doctor",
        "h60_serious": "Serious to rule out",
        "ask_fb": "Was this helpful?",
        "fb_good": "👍 Like",
        "fb_bad": "👎 Dislike",
        "fb_free": "📝 Feedback",
        "fb_write": "Write a short feedback:",
        "fb_thanks": "Thanks for your feedback! ✅",
        # Youth pack / motivation
        "youth_pack": "Youth Pack",
        "gm_energy": "⚡ Energy",
        "gm_energy_q": "Energy (1–5)?",
        "gm_energy_done": "Logged energy — thanks!",
        "gm_evening_btn": "⏰ Remind this evening",
        "hydrate_btn": "💧 Hydration",
        "hydrate_nudge": "💧 Time for a glass of water",
        "skintip_btn": "🧴 Skin/Body tip",
        "skintip_sent": "Tip sent.",
        "daily_tip_prefix": "🍎 Tip:",
        "challenge_btn": "🎯 7-day hydration challenge",
        "challenge_started": "Challenge started!",
        "challenge_progress": "Progress: {d}/{len} days.",
        # Cycle
        "cycle_btn": "🩸 Cycle",
        "cycle_consent": "Track cycle for gentle timing tips?",
        "cycle_ask_last": "Enter last period date (YYYY-MM-DD):",
        "cycle_ask_len": "Average cycle length (e.g., 28):",
        "cycle_saved": "Cycle tracking saved.",
        # Quiet hours / reminders
        "quiet_saved": "Quiet hours saved: {qh}",
        "set_quiet_btn": "🌙 Quiet hours",
        "ask_quiet": "Type quiet hours: HH:MM-HH:MM (local), e.g. 22:00-08:00",
        "evening_intro": "Evening check-in:",
        "evening_tip_btn": "🪄 Tip of the day",
        "evening_set": "Evening check-in at {t} (local).",
        "evening_off": "Evening check-in off.",
        # Other
        "ask_consent": "May I follow up later to check on you?",
        "yes": "Yes", "no": "No",
        "unknown": "Need a bit more detail: where exactly and for how long?",
        "thanks": "Got it 🙌",
        "back": "◀ Back",
        "exit": "Exit",
        "paused_on": "Notifications paused. /resume to enable.",
        "paused_off": "Notifications resumed.",
        "deleted": "All your data was deleted. Use /start to begin again.",
        # Life metrics
        "life_today": "Today is your {n}-th day of life 🎉. Target — 36,500 (100y).",
        "life_percent": "You’ve passed {p}% toward 100 years.",
        "life_estimate": "(estimated by age; set birth_date for accuracy)",
        # Routing prefix
        "px": "Profile: {sex}, {age} y; goal — {goal}.",
        # Reminder confirms
        "act_rem_4h": "⏰ In 4h",
        "act_rem_eve": "⏰ This evening",
        "act_rem_morn": "⏰ Tomorrow morning",
        "act_save_episode": "💾 Save episode",
        "act_ex_neck": "🧘 5-min neck routine",
        "act_er": "🚑 Emergency info",
        # Social proof snippets
        "sp_sleep": "70% of people your age learned 1 sleep trigger in a week.",
        "sp_water": "Most improve energy after 7 days of steady hydration.",
    },
    "ru": {
        "welcome": "Привет! Я TendAI — заботливый напарник по здоровью. Коротко, полезно, по-дружески.\nСделаем 40-сек опрос для персонализации?",
        "help": "Могу: мини-чеки, план на 24–48 ч, напоминания, утренние чек-ины.\nКоманды: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /evening_on 20:00 /evening_off /settz +3 /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
        "privacy": "Я не медсервис. Храним минимум для напоминаний. /delete_data — удалить.",
        "quick_title": "Быстрые действия",
        "quick_h60": "⚡ Здоровье за 60 сек",
        "quick_er": "🚑 Срочно в скорую",
        "quick_lab": "🧪 Лаборатория",
        "quick_rem": "⏰ Напоминание",
        "profile_intro": "Короткий опрос (~40с). Вопросы по одному.",
        "write": "✍️ Написать ответ",
        "skip": "⏭️ Пропустить",
        "start_where": "С чего начнём? симптом / сон / питание / анализы / привычки / долголетие",
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
        "h60_intro": "Что беспокоит? («головная боль», «усталость», «живот»). Дам 3 ключевых совета.",
        "h60_t1": "Возможные причины",
        "h60_t2": "Что сделать сейчас (24–48 ч)",
        "h60_t3": "Когда к врачу",
        "h60_serious": "Что серьёзное исключить",
        "ask_fb": "Это было полезно?",
        "fb_good": "👍 Нравится",
        "fb_bad": "👎 Не полезно",
        "fb_free": "📝 Отзыв",
        "fb_write": "Напишите короткий отзыв:",
        "fb_thanks": "Спасибо! ✅",
        "youth_pack": "Молодёжный пакет",
        "gm_energy": "⚡ Энергия",
        "gm_energy_q": "Энергия (1–5)?",
        "gm_energy_done": "Записал — спасибо!",
        "gm_evening_btn": "⏰ Напомнить вечером",
        "hydrate_btn": "💧 Гидратация",
        "hydrate_nudge": "💧 Время для стакана воды",
        "skintip_btn": "🧴 Совет по коже/телу",
        "skintip_sent": "Отправил.",
        "daily_tip_prefix": "🍎 Подсказка:",
        "challenge_btn": "🎯 Челлендж 7 дней (вода)",
        "challenge_started": "Челлендж запущен!",
        "challenge_progress": "Прогресс: {d}/{len} дн.",
        "cycle_btn": "🩸 Цикл",
        "cycle_consent": "Отслеживать цикл и мягкие подсказки?",
        "cycle_ask_last": "Дата последних месячных (ГГГГ-ММ-ДД):",
        "cycle_ask_len": "Средняя длина цикла (напр., 28):",
        "cycle_saved": "Сохранил отслеживание.",
        "quiet_saved": "Тихие часы: {qh}",
        "set_quiet_btn": "🌙 Тихие часы",
        "ask_quiet": "ЧЧ:ММ-ЧЧ:ММ, напр. 22:00-08:00",
        "evening_intro": "Вечерний чек-ин:",
        "evening_tip_btn": "🪄 Совет дня",
        "evening_set": "Вечерний чек-ин в {t} (локально).",
        "evening_off": "Вечерний чек-ин выключен.",
        "ask_consent": "Можно напомнить позже и спросить, как вы?",
        "yes": "Да", "no": "Нет",
        "unknown": "Чуть больше деталей: где именно и сколько длится?",
        "thanks": "Принято 🙌",
        "back": "◀ Назад",
        "exit": "Выйти",
        "paused_on": "Напоминания на паузе. /resume — включить.",
        "paused_off": "Напоминания снова включены.",
        "deleted": "Данные удалены. /start — начать заново.",
        "life_today": "Сегодня твой {n}-й день жизни 🎉. Цель — 36 500 (100 лет).",
        "life_percent": "Пройдено {p}% пути к 100 годам.",
        "life_estimate": "(оценочно по возрасту; укажи birth_date для точности)",
        "px": "Профиль: {sex}, {age} лет; цель — {goal}.",
        "act_rem_4h": "⏰ Через 4 ч",
        "act_rem_eve": "⏰ Сегодня вечером",
        "act_rem_morn": "⏰ Завтра утром",
        "act_save_episode": "💾 Сохранить эпизод",
        "act_ex_neck": "🧘 Шея 5 мин",
        "act_er": "🚑 Когда срочно",
        "sp_sleep": "70% твоего возраста находят 1 триггер сна за неделю.",
        "sp_water": "Большинство чувствуют больше энергии после 7 дней воды.",
    },
    "uk": {
        "welcome":"Привіт! Я TendAI — турботливий помічник. Коротко, корисно, по-дружньому.\nЗробімо 40с опитник для персоналізації?",
        "help":"Можу: міні-чеки, план 24–48 год, нагадування, ранкові чек-іни.\nКоманди: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /evening_on 20:00 /evening_off /settz +2 /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
        "privacy":"Я не медсервіс. Зберігаю мінімум для нагадувань. /delete_data — стерти.",
        "quick_title":"Швидкі дії",
        "quick_h60":"⚡ Здоров’я за 60 с",
        "quick_er":"🚑 Невідкладно",
        "quick_lab":"🧪 Лабораторія",
        "quick_rem":"⏰ Нагадування",
        "profile_intro":"Короткий опитник (~40с). Питання по одному.",
        "write":"✍️ Написати відповідь",
        "skip":"⏭️ Пропустити",
        "start_where":"З чого почнемо? симптом / сон / харчування / аналізи / звички / довголіття",
        "saved_profile":"Зберіг: ",
        "daily_gm":"Доброго ранку! 🌞 Як самопочуття?",
        "gm_excellent":"👍 Чудово",
        "gm_ok":"🙂 Нормально",
        "gm_tired":"😐 Втома",
        "gm_pain":"🤕 Болить",
        "gm_skip":"⏭️ Пропустити",
        "mood_note":"✍️ Коментар",
        "mood_thanks":"Дякую! Гарного дня 👋",
        "h60_btn":"Здоров’я за 60 секунд",
        "h60_intro":"Що турбує? («головний біль», «втома»...). Дам 3 поради.",
        "h60_t1":"Можливі причини",
        "h60_t2":"Що зробити зараз (24–48 год)",
        "h60_t3":"Коли до лікаря",
        "h60_serious":"Що серйозне виключити",
        "ask_fb":"Це було корисно?",
        "fb_good":"👍 Подобається",
        "fb_bad":"👎 Не корисно",
        "fb_free":"📝 Відгук",
        "fb_write":"Напишіть короткий відгук:",
        "fb_thanks":"Дякую! ✅",
        "youth_pack":"Молодіжний пакет",
        "gm_energy":"⚡ Енергія",
        "gm_energy_q":"Енергія (1–5)?",
        "gm_energy_done":"Записав — дякую!",
        "gm_evening_btn":"⏰ Нагадати ввечері",
        "hydrate_btn":"💧 Гідратація",
        "hydrate_nudge":"💧 Час для склянки води",
        "skintip_btn":"🧴 Порада для шкіри/тіла",
        "skintip_sent":"Надіслано.",
        "daily_tip_prefix":"🍎 Підказка:",
        "challenge_btn":"🎯 Челендж 7 днів (вода)",
        "challenge_started":"Челендж запущено!",
        "challenge_progress":"Прогрес: {d}/{len} днів.",
        "cycle_btn":"🩸 Цикл",
        "cycle_consent":"Відстежувати цикл і м’які поради?",
        "cycle_ask_last":"Дата останніх менструацій (РРРР-ММ-ДД):",
        "cycle_ask_len":"Середня довжина циклу (напр., 28):",
        "cycle_saved":"Збережено.",
        "quiet_saved":"Тихі години: {qh}",
        "set_quiet_btn":"🌙 Тихі години",
        "ask_quiet":"ГГ:ХХ-ГГ:ХХ, напр. 22:00-08:00",
        "evening_intro":"Вечірній чек-ін:",
        "evening_tip_btn":"🪄 Порада дня",
        "evening_set":"Вечірній чек-ін о {t} (локально).",
        "evening_off":"Вимкнено.",
        "ask_consent":"Можу нагадати пізніше?",
        "yes":"Так", "no":"Ні",
        "unknown":"Треба трохи більше деталей: де саме і як довго?",
        "thanks":"Прийнято 🙌",
        "back":"◀ Назад",
        "exit":"Вийти",
        "paused_on":"Нагадування на паузі. /resume — увімкнути.",
        "paused_off":"Знову увімкнені.",
        "deleted":"Дані видалено. /start — почати знову.",
        "life_today":"Сьогодні твій {n}-й день життя 🎉. Мета — 36 500 (100 років).",
        "life_percent":"Пройдено {p}% шляху до 100 років.",
        "life_estimate":"(орієнтовно за віком; вкажи birth_date для точності)",
        "px":"Профіль: {sex}, {age} р.; мета — {goal}.",
        "act_rem_4h":"⏰ Через 4 год",
        "act_rem_eve":"⏰ Сьогодні ввечері",
        "act_rem_morn":"⏰ Завтра зранку",
        "act_save_episode":"💾 Зберегти епізод",
        "act_ex_neck":"🧘 Шия 5 хв",
        "act_er":"🚑 Коли терміново",
        "sp_sleep":"70% твого віку знаходять 1 тригер сну за тиждень.",
        "sp_water":"Більшість відчувають більше енергії після 7 днів води.",
    },
}
T["es"] = T["en"]  # простая заглушка

# --- Personalized prefix before LLM reply ---
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

def age_to_band(age: int) -> str:
    if age <= 0: return "unknown"
    if age <= 25: return "18–25"
    if age <= 35: return "26–35"
    if age <= 45: return "36–45"
    if age <= 60: return "46–60"
    return "60+"

def _user_lang(uid: int) -> str:
    return norm_lang((users_get(uid) or {}).get("lang") or "en")

def clip_lines(s: str, max_lines: int = 5) -> str:
    lines = [l.rstrip() for l in (s or "").splitlines()]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines])


# ===== ONBOARDING GATE & soft intake (трогаем с первого слова) =====
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

# === MINI-INTAKE ===
MINI_KEYS = ["age","sex","conditions","goal","meds_allergies","activity","diet_focus","steps_target","habits","birth_date"]
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
        "label":{"ru":"Хроника:","en":"Chronic conditions:","uk":"Хронічні стани:","es":"Condiciones crónicas:"}
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
        "label":{"ru":"Питание чаще:","en":"Diet mostly:","uk":"Харчування переважно:","es":"Dieta:"}
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
        "label":{"ru":"Привычки (ближе всего):","en":"Habits (pick closest):","uk":"Звички (оберіть):","es":"Hábitos:"}
    },
    "birth_date": {
        "ru":[], "en":[], "uk":[], "es":[],
        "label":{"ru":"Дата рождения (ГГГГ-ММ-ДД) — по желанию:","en":"Birth date (YYYY-MM-DD) — optional:","uk":"Дата народження (РРРР-ММ-ДД) — опційно:","es":"Fecha de nacimiento (AAAA-MM-DD) — opcional:"}
    },
}

async def start_mini_intake(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: int):
    sessions[uid] = {"mini_active": True, "mini_step": 0, "mini_answers": {}}
    await context.bot.send_message(chat_id, {
        "ru":"🔎 Мини-опрос для персонализации (4–6 кликов).",
        "uk":"🔎 Міні-опитування для персоналізації (4–6 кліків).",
        "en":"🔎 Mini-intake for personalization (4–6 taps).",
        "es":"🔎 Mini-intake para personalización (4–6 toques).",
    }[lang], reply_markup=ReplyKeyboardRemove())
    await ask_next_mini(context, chat_id, lang, uid)

def build_mini_kb(lang: str, key: str) -> InlineKeyboardMarkup:
    opts = MINI_STEPS[key].get(lang, [])
    rows, row = [], []
    for label, val in opts:
        row.append(InlineKeyboardButton(label, callback_data=f"mini|choose|{key}|{val}"))
        if len(row) == 3:
            rows.append(row); row=[]
    if row: rows.append(row)
    if key in MINI_FREE_KEYS or (not opts):
        rows.append([InlineKeyboardButton(T[lang]["write"], callback_data=f"mini|write|{key}")])
    rows.append([InlineKeyboardButton(T[lang]["skip"], callback_data=f"mini|skip|{key}")])
    return InlineKeyboardMarkup(rows)

async def ask_next_mini(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: int):
    s = sessions.get(uid, {})
    step_idx = s.get("mini_step", 0)
    if step_idx >= len(MINI_KEYS):
        answers = s.get("mini_answers", {})
        if answers.get("meds_allergies"):
            profiles_upsert(uid, {"meds":answers["meds_allergies"], "allergies":answers["meds_allergies"]})
        if answers.get("birth_date"):
            profiles_upsert(uid, {"birth_date": answers["birth_date"]})
        profiles_upsert(uid, answers)
        sessions[uid]["mini_active"] = False
        # первичный AI-профиль привычек
        profiles_upsert(uid, {"ai_profile": json.dumps({"v":1,"habits":answers.get("habits","")}, ensure_ascii=False)})
        # меню и быстрые кнопки
        await context.bot.send_message(chat_id, T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
        await show_quickbar(context, chat_id, lang)
        return
    key = MINI_KEYS[step_idx]
    label = MINI_STEPS[key]["label"][lang]
    await context.bot.send_message(chat_id, label, reply_markup=build_mini_kb(lang, key))

def mini_handle_choice(uid: int, key: str, value: str):
    s = sessions.setdefault(uid, {"mini_active": True, "mini_step": 0, "mini_answers": {}})
    s["mini_answers"][key] = value
    s["mini_step"] = int(s.get("mini_step", 0)) + 1

def should_trigger_mini(uid: int) -> bool:
    prof = profiles_get(uid) or {}
    s = sessions.setdefault(uid, {})
    if s.get("mini_active"):
        return False
    return profile_is_incomplete(prof)


# ---------- Anti-duplicate ----------
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
    out = clip_lines(text, 5)
    if force or not is_duplicate_question(uid, out):
        await msg_obj.reply_text(out, reply_markup=reply_markup)


# -------- Sheets (with memory fallback) --------
SHEETS_ENABLED = True
ss = None
ws_feedback = ws_users = ws_profiles = ws_episodes = ws_reminders = ws_daily = None
ws_rules = ws_challenges = None

GSPREAD_CLIENT: Optional[gspread.client.Client] = None
SPREADSHEET_ID_FOR_INTAKE: str = ""

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
    global SHEETS_ENABLED, ss, ws_feedback, ws_users, ws_profiles, ws_episodes, ws_reminders, ws_daily, ws_rules, ws_challenges
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
        logging.error(f"SHEETS disabled (fallback to memory). Reason: {e}")

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
MEM_SYMPTOMS: Dict[int, Dict[str,int]] = {}  # счётчики повторяющихся жалоб


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
           "baseline_severity":str(severity),"red_flags":red,"plan_accepted":"","target":"",
           "reminder_at":"","next_checkin_at":"","status":"open","last_update":iso(utcnow()),"notes":""}
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
            ws_reminders.append_row([row.get(h, "") for h in hdr])
            return rid
        except Exception as e:
            logging.warning(f"reminder_add -> memory fallback: {e}")
    MEM_REMINDERS.append(row)
    return rid

def challenge_get(uid: int) -> Optional[dict]:
    if SHEETS_ENABLED:
        try:
            for r in ws_challenges.get_all_records():
                if r.get("user_id")==str(uid) and r.get("status")!="done":
                    return r
        except Exception as e:
            logging.warning(f"challenge_get fallback: {e}")
    for r in MEM_CHALLENGES:
        if r.get("user_id")==str(uid) and r.get("status")!="done":
            return r
    return None

def challenge_start(uid: int, name: str="water7", length_days: int=7):
    row = {"user_id":str(uid),"challenge_id":str(uuid.uuid4()),"name":name,"start_date":iso(utcnow()),
           "length_days":str(length_days),"days_done":"0","status":"active"}
    if SHEETS_ENABLED:
        try:
            hdr = _headers(ws_challenges)
            ws_challenges.append_row([row.get(h,"") for h in hdr]); return
        except Exception as e:
            logging.warning(f"challenge_start -> memory fallback: {e}")
    MEM_CHALLENGES.append(row)


# ---------- Quickbar & Menus (гибридный UI) ----------
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


# ---------- Time & Quiet hours (используем в Части 2) ----------
def _user_tz_off(uid: int) -> int:
    try:
        return int((users_get(uid) or {}).get("tz_offset") or "0")
    except:
        return 0

def user_local_now(uid: int) -> datetime:
    return datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(hours=_user_tz_off(uid))

def hhmm_tuple(hhmm: str) -> (int,int):
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", hhmm or "")
    if not m: return (8,30)
    return (int(m.group(1)), int(m.group(2)))

def local_to_utc_dt(uid: int, local_dt: datetime) -> datetime:
    return local_dt - timedelta(hours=_user_tz_off(uid))

def _user_quiet_hours(uid: int) -> str:
    prof = profiles_get(uid) or {}
    return (prof.get("quiet_hours") or DEFAULT_QUIET_HOURS).strip()

def adjust_out_of_quiet(local_dt: datetime, qh: str) -> datetime:
    m = re.match(r"^\s*([01]?\d|2[0-3]):[0-5]\d-([01]?\d|2[0-3]):[0-5]\d\s*$", qh or "")
    if not m: return local_dt
    start, end = qh.split("-")
    sh, sm = hhmm_tuple(start); eh, em = hhmm_tuple(end)
    st = local_dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
    en = local_dt.replace(hour=eh, minute=em, second=0, microsecond=0)
    if st < en:
        in_quiet = (st <= local_dt < en)
    else:
        in_quiet = (local_dt >= st or local_dt < en)
    return en if in_quiet else local_dt


# ---------- Life metrics ----------
def life_metrics(profile: dict) -> Dict[str,int]:
    b = (profile or {}).get("birth_date","").strip()
    days = None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", b):
        try:
            y,m,d = map(int, b.split("-"))
            bd = datetime(y,m,d,tzinfo=timezone.utc)
            days = (utcnow()-bd).days
        except Exception:
            days = None
    if days is None:
        try:
            a = int(re.search(r"\d+", str((profile or {}).get("age","") or "0")).group(0))
        except Exception:
            a = 0
        days = max(0, a*365)
    percent = min(100, round(days/36500*100, 1))
    return {"days_lived": days, "percent_to_100": percent}

def progress_bar(percent: float, width: int=12) -> str:
    fill = int(round(width*percent/100.0))
    return "█"*fill + "░"*(width-fill)


# ---------- Motivation helpers (streaks, social proof) ----------
def update_streak(uid: int, date_str: str):
    u = users_get(uid) or {}
    last = (u.get("gm_last_date") or "").strip()
    streak = int(u.get("streak") or "0")
    best = int(u.get("streak_best") or "0")
    if last:
        try:
            prev = datetime.strptime(last, "%Y-%m-%d").date()
            cur = datetime.strptime(date_str, "%Y-%m-%d").date()
            if (cur - prev).days == 1:
                streak += 1
            elif (cur - prev).days == 0:
                pass
            else:
                streak = 1
        except:
            streak = max(1, streak)
    else:
        streak = 1
    best = max(best, streak)
    users_set(uid, "streak", str(streak))
    users_set(uid, "streak_best", str(best))
    users_set(uid, "gm_last_date", date_str)
    return streak, best

def social_proof_line(lang: str, age_band: str) -> str:
    # можно усложнить персонализацию позже
    if random.random() < 0.5:
        return T[lang]["sp_sleep"]
    return T[lang]["sp_water"]


# ------------- LLM Router (concise) -------------
SYS_ROUTER = (
    "You are TendAI — a concise, warm, professional health & longevity assistant (not a doctor). "
    "Always answer strictly in {lang}. Keep replies very short (<=5 lines). "
    "Personalize using the profile (sex/age/goal/conditions, diet_focus, steps_target). "
    "TRIAGE: ask 1 clarifier first only when necessary; avoid alarmism. "
    "Return JSON ONLY: {\"intent\":\"symptom|nutrition|sleep|labs|habits|longevity|other\","
    "\"assistant_reply\":\"...\",\"followups\":[\"...\"],\"needs_more\":true,\"red_flags\":false,\"confidence\":0.0}"
)

def llm_router_answer(text: str, lang: str, profile: dict) -> dict:
    if not oai:
        return {"intent":"other","assistant_reply":T[lang]["unknown"],"followups":[],"needs_more":True,"red_flags":False,"confidence":0.3}
    sys = SYS_ROUTER.replace("{lang}", lang) + f"\nUserProfile: {json.dumps(profile, ensure_ascii=False)}"
    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL, temperature=0.25, max_tokens=420,
            response_format={"type":"json_object"},
            messages=[{"role":"system","content":sys},{"role":"user","content":text}]
        )
        data = json.loads(resp.choices[0].message.content.strip())
        if "followups" not in data or data["followups"] is None:
            data["followups"] = []
        return data
    except Exception as e:
        logging.error(f"router LLM error: {e}")
        return {"intent":"other","assistant_reply":T[lang]["unknown"],"followups":[],"needs_more":True,"red_flags":False,"confidence":0.3}


# ===== Health60 =====
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


# ---------- Lightweight content helpers ----------
def _get_skin_tip(lang: str, sex: str, age: int) -> str:
    pool_ru = [
        "Мягкое SPF каждый день — лучшая инвестиция в кожу.",
        "Душ тёплой водой 3–5 мин — защита барьера кожи.",
        "Умывалка без SLS + крем сразу после воды — база."
    ]
    pool_en = [
        "Daily light SPF is the most underrated skin investment.",
        "Keep showers warm, not hot; 3–5 minutes helps your skin barrier.",
        "Use a gentle cleanser and moisturize right after water."
    ]
    pools = {"ru": pool_ru, "uk": pool_ru, "en": pool_en, "es": pool_en}
    return random.choice(pools.get(lang, pool_en))

def _get_daily_tip(profile: dict, lang: str) -> str:
    base = {
        "ru": ["1 мин дыхания 4-6 — и пульс спокойнее.", "Держи воду рядом — глоток каждый раз, как берёшь телефон."],
        "uk": ["1 хв дихання 4-6 — і пульс спокійніший.", "Склянка води поруч — ковток щоразу, як береш телефон."],
        "en": ["Try 1 min of 4-6 breathing — pulse calms.", "Keep water nearby — sip every time you unlock your phone."]
    }
    return random.choice(base.get(lang, base["en"]))

def tip_for_mood(lang: str, mood: str, profile: dict) -> str:
    if mood in ("excellent","ok"):
        return {"ru":"Супер! 5–10 минут света/движения — заряд на день.",
                "uk":"Клас! 5–10 хв світла/руху — заряд на день.",
                "en":"Nice! 5–10 min light + movement — easy energy."}[lang]
    if mood == "tired":
        return {"ru":"Мягко: стакан воды + 1 мин дыхания 4-6.",
                "uk":"Мʼяко: склянка води + 1 хв дихання 4-6.",
                "en":"Gentle: water + 1 min 4-6 breathing."}[lang]
    if mood == "pain":
        return {"ru":"Если боль усиливается/необычная — проверь красные флаги.",
                "uk":"Якщо біль посилюється/незвична — перевір прапорці.",
                "en":"If pain worsens/unusual — check red flags."}[lang]
    return _get_daily_tip(profile, lang)


# ---------- Symptoms tracker (повтор жалоб) ----------
def symptom_inc(uid: int, raw_text: str):
    raw = (raw_text or "").lower().strip()
    if not raw:
        return
    d = MEM_SYMPTOMS.setdefault(uid, {})
    key = "headache" if "голов" in raw or "head" in raw else (
          "fatigue" if "устал" in raw or "tired" in raw or "fatigue" in raw else
          "stomach" if "живот" in raw or "stomach" in raw or "abdom" in raw else
          raw.split()[0][:24])
    d[key] = d.get(key, 0) + 1

def symptom_top_triggers(uid: int, top: int=3) -> List[str]:
    d = MEM_SYMPTOMS.get(uid, {})
    return [k for k,_ in sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:top]]


# ---------- Scheduling stubs (реализация в Части 2) ----------
def schedule_from_sheet_on_start(app):  # stub
    try:
        logging.info("schedule_from_sheet_on_start: stub ok (real logic in Part 2)")
    except Exception:
        pass

def schedule_daily_checkin(app, uid: int, tz_off: int, hhmm: str, lang: str):  # stub
    logging.info(f"schedule_daily_checkin (stub) uid={uid} at {hhmm} tz={tz_off}")

def schedule_evening_checkin(app, uid: int, tz_off: int, hhmm: str, lang: str):  # stub
    logging.info(f"schedule_evening_checkin (stub) uid={uid} at {hhmm} tz={tz_off}")

async def job_oneoff_reminder(context: ContextTypes.DEFAULT_TYPE):  # stub
    d = context.job.data or {}
    uid = d.get("user_id")
    try:
        await context.bot.send_message(uid, T[_user_lang(uid)]["thanks"])
    except Exception:
        pass


# ------------- Commands (минимум для Ч.1; остальное — в Ч.2) -------------
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

    await update.message.reply_text(clip_lines(T[lang]["welcome"], 5), reply_markup=ReplyKeyboardRemove())

    prof = profiles_get(user.id) or {}
    if profile_is_incomplete(prof):
        await start_mini_intake(context, update.effective_chat.id, lang, user.id)
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
    if context.application.job_queue:
        schedule_daily_checkin(context.application, user.id, tz_off, hhmm, lang)
        eh = (u.get("evening_hour") or "").strip()
        if eh:
            schedule_evening_checkin(context.application, user.id, tz_off, eh, lang)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _user_lang(update.effective_user.id)
    update_last_seen(update.effective_user.id)
    await update.message.reply_text(clip_lines(T[lang]["help"], 5))

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _user_lang(update.effective_user.id)
    update_last_seen(update.effective_user.id)
    await update.message.reply_text(clip_lines(T[lang]["privacy"], 5))

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

# Timezone & check-ins (UI only)
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
        if context.application.job_queue:
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
    if context.application.job_queue:
        schedule_daily_checkin(context.application, uid, _user_tz_off(uid), hhmm, lang)
    await update.message.reply_text(f"Daily check-in set to {hhmm} (local).")

async def cmd_checkin_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "yes")
    await update.message.reply_text("Daily check-in disabled.")

# --- Language switches (нужны уже в Ч.1, чтобы не было NameError) ---
async def cmd_lang_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "ru"); context.user_data["lang"] = "ru"
    await update.message.reply_text("Язык: русский.")

async def cmd_lang_en(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "en"); context.user_data["lang"] = "en"
    await update.message.reply_text("Language set: English.")

async def cmd_lang_uk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "uk"); context.user_data["lang"] = "uk"
    await update.message.reply_text("Мова: українська.")

async def cmd_lang_es(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "es"); context.user_data["lang"] = "es"
    await update.message.reply_text("Idioma: español (beta).")


# -------- Свободный текст (гибрид-общение + авто-интейк) --------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    user = update.effective_user
    lang = _user_lang(uid) or norm_lang(getattr(user, "language_code", None))
    text = (update.message.text or "").strip()
    update_last_seen(uid)

    # 0) мягкий запуск мини-опроса с первого свободного сообщения
    if should_trigger_mini(uid):
        # ставим язык от первого сообщения
        users_upsert(uid, user.username or "", detect_lang_from_text(text, lang))
        context.user_data["lang"] = _user_lang(uid)
        await start_mini_intake(context, update.effective_chat.id, _user_lang(uid), uid)
        return

    # 1) Если ждём свободный ответ для конкретного шага мини-опроса
    s = sessions.setdefault(uid, {})
    free_key = s.get("awaiting_free_key")
    if free_key:
        if free_key == "birth_date" and not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            await update.message.reply_text({"ru":"Формат: ГГГГ-ММ-ДД",
                                             "uk":"Формат: РРРР-ММ-ДД",
                                             "en":"Use YYYY-MM-DD",
                                             "es":"Usa AAAA-MM-DD"}[_user_lang(uid)])
            return
        mini_handle_choice(uid, free_key, text)
        s.pop("awaiting_free_key", None)
        await ask_next_mini(context, update.effective_chat.id, _user_lang(uid), uid)
        return

    # 2) Health60 «в свободной речи»
    if "болит" in text.lower() or "pain" in text.lower() or "ache" in text.lower():
        prof = profiles_get(uid) or {}
        prefix = personalized_prefix(_user_lang(uid), prof)
        plan = health60_make_plan(_user_lang(uid), text, prof)
        symptom_inc(uid, text)
        await send_unique(update.message, uid, (prefix + "\n\n" if prefix else "") + plan)
        await show_quickbar(context, update.effective_chat.id, _user_lang(uid))
        return

    # 3) Лёгкий роутер по умолчанию (короткий ответ + быстрые кнопки)
    prof = profiles_get(uid) or {}
    data = llm_router_answer(text, _user_lang(uid), prof)
    prefix = personalized_prefix(_user_lang(uid), prof)
    reply = (prefix + "\n\n" if prefix else "") + (data.get("assistant_reply") or T[_user_lang(uid)]["unknown"])
    await send_unique(update.message, uid, reply)
    await show_quickbar(context, update.effective_chat.id, _user_lang(uid))


# -------- Обработчик нажатий (минимально для Ч.1, расширим в Ч.2) --------
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    uid = query.from_user.id
    lang = _user_lang(uid)
    data = query.data or ""
    update_last_seen(uid)

    try:
        if data == "intake:start":
            await query.answer()
            await start_mini_intake(context, query.message.chat_id, lang, uid)
            return

        if data == "gate:skip":
            await query.answer()
            await query.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
            await show_quickbar(context, query.message.chat_id, lang)
            return

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

        if data.startswith("consent|"):
            _, ans = data.split("|", 1)
            users_set(uid, "consent", "yes" if ans == "yes" else "no")
            await query.answer()
            await query.message.reply_text(T[lang]["thanks"])
            return

        # Быстрые действия (минимальные ответы в Ч.1)
        if data == "menu|h60":
            sessions.setdefault(uid, {})["awaiting_h60_text"] = True
            await query.answer()
            await query.message.reply_text(T[lang]["h60_intro"])
            return

        if data == "menu|er":
            await query.answer()
            txt = {"ru":"🚑 Срочно: одышка, давящая боль в груди, внезапная слабость одной стороны, спутанность, сильное кровотечение, травма головы — вызывайте скорую.",
                   "uk":"🚑 Негайно: задишка, стискаючий біль у грудях, раптова слабкість однієї сторони, сплутаність, кровотеча, травма голови — швидка.",
                   "en":"🚑 Urgent: severe shortness of breath, chest pain/pressure, sudden one-sided weakness, confusion, major bleeding, head trauma — call emergency."}[lang]
            await query.message.reply_text(txt); return

        if data == "menu|lab":
            await query.answer()
            await query.message.reply_text({"ru":"🧪 База анализов добавлю в следующем апдейте.",
                                            "uk":"🧪 Базовий перелік додам у наступному апдейті.",
                                            "en":"🧪 Baseline lab panel will land next update."}[lang])
            return

        if data == "menu|rem":
            await query.answer()
            await query.message.reply_text({"ru":"Ок! Напоминалки подключим чуть позже. Пока — вручную через /checkin_on HH:MM.",
                                            "uk":"Ок! Нагадування додамо трохи згодом. Поки — вручну через /checkin_on HH:MM.",
                                            "en":"Got it! Smart reminders soon; for now use /checkin_on HH:MM."}[lang])
            return

    except Exception as e:
        logging.error(f"cb_handler error: {e}")
    try:
        await query.answer()
    except Exception:
        pass


# -------- Регистрация команд и минимальный запуск (Ч.1) --------
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

    # Переключение языка — чтобы не падал NameError
    app.add_handler(CommandHandler("ru",           cmd_lang_ru))
    app.add_handler(CommandHandler("en",           cmd_lang_en))
    app.add_handler(CommandHandler("uk",           cmd_lang_uk))
    app.add_handler(CommandHandler("es",           cmd_lang_es))

    # Свободный текст (авто-интейк + гибрид)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=1)

    # Callback-кнопки (минимум в Ч.1)
    app.add_handler(CallbackQueryHandler(cb_handler), group=2)

    return app

def main():
    if not TELEGRAM_TOKEN:
        logging.error("TELEGRAM_TOKEN is not set in environment.")
        return
    app = build_app()
    logging.info("Starting polling (Part 1)...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
# =========================
# TendAI — Part 2: Schedules, callbacks, dialog
# =========================

# -------- Tips & social proof --------
def _tip_for_mood(lang: str, mood: str, profile: dict) -> str:
    tips_ru = {
        "excellent": "Держим темп! Сегодня добавь 5-мин растяжку шеи — бонус к самочувствию.",
        "ok": "Норм — сделай 3 глотка воды и 1 мин дыхания 4-6, станет бодрее.",
        "tired": "Попробуй 90-сек микропаузу: вдох 4, выдох 6. И стакан воды рядом.",
        "pain": "Мягче к себе: 10-мин покоя, вода, лёгкое тепло (если помогает). При ухудшении — к врачу."
    }
    tips_en = {
        "excellent": "Nice! Add a 5-min neck stretch today — tiny boost.",
        "ok": "Sip water and try 1-min 4-6 breathing — quick energy bump.",
        "tired": "90-sec micro-break: inhale 4, exhale 6. Keep water nearby.",
        "pain": "Be gentle: 10-min rest, water, light warmth if it helps. Worsening → see a doctor."
    }
    pool = {"ru": tips_ru, "uk": tips_ru, "en": tips_en, "es": tips_en}
    return pool.get(lang, tips_en).get(mood, _get_daily_tip(profile, lang))

def _tip_for_energy(lang: str, level: int, profile: dict) -> str:
    if level <= 2:
        return {"ru":"Сделай 1-мин дыхание 4-6 + стакан воды. Мини-прогулка 3–5 мин.",
                "uk":"1 хв дихання 4-6 + склянка води. Міні-прогулянка 3–5 хв.",
                "en":"Do 1-min 4-6 breathing + drink a glass of water. 3–5 min walk."}.get(lang, "Do 1-min 4-6 breathing + water. 3–5 min walk.")
    if level == 3:
        return {"ru":"Поддержим ритм: 200–300 мл воды и короткая разминка 1–2 мин.",
                "uk":"Підтримай ритм: 200–300 мл води і коротка розминка 1–2 хв.",
                "en":"Keep pace: 200–300 ml water and a 1–2 min stretch."}.get(lang, "Keep pace: water + 1–2 min stretch.")
    return {"ru":"Огонь! Зафиксируй: 5-мин активная прогулка после обеда.",
            "uk":"Круто! Зафіксуй: 5-хв активна прогулянка після обіду.",
            "en":"Great! Lock it in: 5-min brisk walk after lunch."}.get(lang, "Great! 5-min brisk walk after lunch.")

def _social_proof(lang: str, key: str) -> str:
    msg = {
        "ru": "70% пользователей твоего возраста нашли свои триггеры сна за 2 недели.",
        "uk": "70% користувачів твого віку знайшли тригери сну за 2 тижні.",
        "en": "70% of people your age discovered their sleep triggers in 2 weeks."
    }
    return msg.get(lang, msg["en"])

# -------- Jobs / Scheduling --------
def _seconds_until_local(uid: int, target_hhmm: str) -> int:
    now = user_local_now(uid)
    h, m = hhmm_tuple(target_hhmm)
    first = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if first <= now:
        first += timedelta(days=1)
    first = adjust_out_of_quiet(first, _user_quiet_hours(uid))
    return max(1, int((first - now).total_seconds()))

async def job_gm_checkin(context: ContextTypes.DEFAULT_TYPE):
    uid = context.job.data["user_id"]
    lang = _user_lang(uid)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["gm_excellent"], callback_data="gm|mood|excellent"),
         InlineKeyboardButton(T[lang]["gm_ok"],        callback_data="gm|mood|ok")],
        [InlineKeyboardButton(T[lang]["gm_tired"],     callback_data="gm|mood|tired"),
         InlineKeyboardButton(T[lang]["gm_pain"],      callback_data="gm|mood|pain")],
        [InlineKeyboardButton(T[lang]["gm_skip"],      callback_data="gm|skip")],
        [InlineKeyboardButton(T[lang]["mood_note"],    callback_data="gm|note")]
    ])
    try:
        await context.bot.send_message(uid, T[lang]["daily_gm"], reply_markup=kb)
    except Exception as e:
        logging.warning(f"job_gm_checkin send failed: {e}")

async def job_evening_checkin(context: ContextTypes.DEFAULT_TYPE):
    uid = context.job.data["user_id"]
    lang = _user_lang(uid)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["evening_tip_btn"], callback_data="evening|tip")],
        [InlineKeyboardButton(T[lang]["hydrate_btn"],     callback_data="hydrate|nudge")]
    ])
    try:
        await context.bot.send_message(uid, T[lang]["evening_intro"], reply_markup=kb)
    except Exception as e:
        logging.warning(f"job_evening_checkin send failed: {e}")

def schedule_daily_checkin(app, uid: int, tz_off: int, hhmm: str, lang: str):
    # cancel previous
    try:
        for j in app.job_queue.jobs():
            if j.name == f"gm:{uid}":
                j.schedule_removal()
    except Exception:
        pass
    delay = _seconds_until_local(uid, hhmm)
    app.job_queue.run_repeating(job_gm_checkin, interval=86400, first=delay,
                                name=f"gm:{uid}", data={"user_id": uid})

def schedule_evening_checkin(app, uid: int, tz_off: int, hhmm: str, lang: str):
    try:
        for j in app.job_queue.jobs():
            if j.name == f"eve:{uid}":
                j.schedule_removal()
    except Exception:
        pass
    delay = _seconds_until_local(uid, hhmm)
    app.job_queue.run_repeating(job_evening_checkin, interval=86400, first=delay,
                                name=f"eve:{uid}", data={"user_id": uid})

async def job_oneoff_reminder(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id")
    text = d.get("text") or T[_user_lang(uid)]["thanks"]
    try:
        await context.bot.send_message(uid, text)
    except Exception:
        pass

def schedule_oneoff_reminder(app, uid: int, when_local: datetime, text: str):
    when_local = adjust_out_of_quiet(when_local, _user_quiet_hours(uid))
    when_utc = local_to_utc_dt(uid, when_local)
    # store + schedule
    rid = reminder_add(uid, text, when_utc)
    delay = max(1, int((when_local - user_local_now(uid)).total_seconds()))
    app.job_queue.run_once(job_oneoff_reminder, when=delay, data={"user_id": uid, "reminder_id": rid, "text": text})

# -------- Commands (remaining) --------
async def cmd_energy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    row = [InlineKeyboardButton(str(i), callback_data=f"energy|rate|{i}") for i in range(1,6)]
    kb = InlineKeyboardMarkup([row])
    await update.message.reply_text(T[lang]["gm_energy_q"], reply_markup=kb)

async def cmd_hydrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    await update.message.reply_text(T[lang]["hydrate_nudge"])
    await show_quickbar(context, update.effective_chat.id, lang)

async def cmd_skintip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    prof = profiles_get(uid) or {}
    tip = _get_skin_tip(lang, prof.get("sex",""), int(re.search(r"\d+", str(prof.get("age","") or "0")).group(0)) if re.search(r"\d+", str(prof.get("age","") or "0")) else 0)
    await update.message.reply_text(f"{T[lang]['daily_tip_prefix']} {tip}")

async def cmd_evening_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang=_user_lang(uid)
    txt = (update.message.text or "").strip()
    m = re.search(r"([01]?\d|2[0-3]):([0-5]\d)", txt)
    hhmm = m.group(0) if m else DEFAULT_EVENING_LOCAL
    users_set(uid, "evening_hour", hhmm)
    if _has_jq_ctx(context):
        schedule_evening_checkin(context.application, uid, _user_tz_off(uid), hhmm, lang)
    await update.message.reply_text(T[lang]["evening_set"].format(t=hhmm))

async def cmd_evening_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang=_user_lang(uid)
    users_set(uid, "evening_hour", "")
    # cancel job
    try:
        for j in context.application.job_queue.jobs():
            if j.name == f"eve:{uid}":
                j.schedule_removal()
    except Exception:
        pass
    await update.message.reply_text(T[lang]["evening_off"])

async def cmd_quiet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang=_user_lang(uid)
    txt = (update.message.text or "").strip()
    m = re.search(r"([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-3]):([0-5]\d)", txt)
    if not m:
        await update.message.reply_text(T[lang]["ask_quiet"]); return
    qh = m.group(0).replace(" ", "")
    profiles_upsert(uid, {"quiet_hours": qh})
    await update.message.reply_text(T[lang]["quiet_saved"].format(qh=qh))

async def cmd_life(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang=_user_lang(uid)
    prof = profiles_get(uid) or {}
    met = life_metrics(prof)
    bar = progress_bar(met["percent_to_100"])
    await update.message.reply_text(
        f"{T[lang]['life_today'].format(n=met['days_lived'])}\n"
        f"{T[lang]['life_percent'].format(p=met['percent_to_100'])}\n"
        f"{bar} {met['percent_to_100']}%")

# -------- CallbackQuery handler --------
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    uid = query.from_user.id
    lang = _user_lang(uid)
    prof = profiles_get(uid) or {}
    await query.answer()

    # Mini-intake
    if data.startswith("intake:start"):
        sessions.setdefault(uid, {})["mini_active"] = True
        await start_mini_intake(context, update.effective_chat.id, lang, uid)
        return
    if data.startswith("mini|choose|"):
        _,_, key, val = data.split("|", 3)
        mini_handle_choice(uid, key, val)
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return
    if data.startswith("mini|write|"):
        _,_, key = data.split("|", 2)
        sessions.setdefault(uid, {})["awaiting_mini_key"] = key
        await query.edit_message_text(T[lang]["write"])
        return
    if data.startswith("mini|skip|"):
        _,_, key = data.split("|", 2)
        mini_handle_choice(uid, key, "")
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return

    # GM (morning)
    if data.startswith("gm|mood|"):
        mood = data.split("|",2)[2]
        daily_add(iso(utcnow()), uid, mood=mood, comment="")
        # streaks
        u = users_get(uid)
        today = user_local_now(uid).date().isoformat()
        last = (u.get("gm_last_date") or "")
        if last and last == (user_local_now(uid)-timedelta(days=1)).date().isoformat():
            new_streak = str(int(u.get("streak") or "0") + 1)
        elif last == today:
            new_streak = u.get("streak") or "1"
        else:
            new_streak = "1"
        users_set(uid, "streak", new_streak)
        users_set(uid, "streak_best", str(max(int(u.get("streak_best") or "0"), int(new_streak))))
        users_set(uid, "gm_last_date", today)

        tip = _tip_for_mood(lang, mood, prof)
        sp = _social_proof(lang, "sleep")
        await query.edit_message_text(f"{tip}\n\n{sp}")
        await show_quickbar(context, update.effective_chat.id, lang)
        return
    if data == "gm|note":
        sessions.setdefault(uid, {})["awaiting_mood_note"] = True
        await query.edit_message_text(T[lang]["mood_write"] if "mood_write" in T[lang] else T[lang]["fb_write"])
        return
    if data == "gm|skip":
        await query.edit_message_text(T[lang]["mood_thanks"])
        return

    # Energy
    if data.startswith("energy|rate|"):
        level = int(data.split("|",2)[2])
        daily_add(iso(utcnow()), uid, energy=level)
        tip = _tip_for_energy(lang, level, prof)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["gm_evening_btn"], callback_data="rem|eve")]])
        await query.edit_message_text(f"{T[lang]['gm_energy_done']}\n{tip}", reply_markup=kb)
        return

    # Quick menu
    if data == "menu|h60":
        sessions.setdefault(uid, {})["awaiting_h60_text"] = True
        await query.edit_message_text(T[lang]["h60_intro"])
        return
    if data == "menu|er":
        await query.edit_message_text("🚑 Если сильная грудная боль, одышка, признаки инсульта, кровь в рвоте/стуле — вызывай скорую немедленно.")
        return
    if data == "menu|lab":
        await query.edit_message_text("🧪 Базовый чек-лист: общий анализ крови, ферритин, ТТГ, витамин D, B12. Делай по согласованию с врачом.")
        return
    if data == "menu|rem":
        rm = InlineKeyboardMarkup([
            [InlineKeyboardButton(T[lang]["act_rem_4h"], callback_data="rem|in|4h")],
            [InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="rem|eve")],
            [InlineKeyboardButton(T[lang]["act_rem_morn"], callback_data="rem|morn")],
        ])
        await query.edit_message_text("Выбери напоминание:", reply_markup=rm)
        return

    # Hydration & tips
    if data == "hydrate|nudge":
        await query.edit_message_text(T[lang]["hydrate_nudge"])
        return
    if data == "evening|tip":
        await query.edit_message_text(f"{T[lang]['daily_tip_prefix']} {_get_daily_tip(prof, lang)}")
        return

    # Reminders
    if data.startswith("rem|"):
        when = data.split("|",2)[2]
        now_local = user_local_now(uid)
        if when == "eve":
            hhmm = (users_get(uid).get("evening_hour") or DEFAULT_EVENING_LOCAL)
            h,m = hhmm_tuple(hhmm)
            target = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now_local:
                target += timedelta(days=1)
        elif when == "morn":
            hhmm = (users_get(uid).get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
            h,m = hhmm_tuple(hhmm)
            target = now_local.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=1 if now_local.time() > dtime(hour=h, minute=m) else 0)
        elif when == "in|4h" or when == "4h":
            target = now_local + timedelta(hours=4)
        else:
            # explicit format HH:MM
            if re.match(r"^\d{1,2}:\d{2}$", when):
                h,m = hhmm_tuple(when); target = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
                if target <= now_local: target += timedelta(days=1)
            else:
                target = now_local + timedelta(hours=4)
        schedule_oneoff_reminder(context.application, uid, target, T[lang]["thanks"])
        await query.edit_message_text("⏰ Ок, напомню.")
        return

# -------- Message handler (free text) --------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    # language autodetect & save
    lang_guess = detect_lang_from_text(text, _user_lang(uid))
    users_set(uid, "lang", lang_guess)
    context.user_data["lang"] = lang_guess
    lang = lang_guess

    # If profile incomplete and mini not started — trigger immediately
    prof = profiles_get(uid) or {}
    s = sessions.setdefault(uid, {})
    if profile_is_incomplete(prof) and not s.get("mini_active") and not s.get("mini_started_once"):
        s["mini_started_once"] = True
        await start_mini_intake(context, update.effective_chat.id, lang, uid)
        return

    # awaiting mini free-text
    if s.get("awaiting_mini_key"):
        key = s.pop("awaiting_mini_key")
        mini_handle_choice(uid, key, text[:200])
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return

    # awaiting mood note
    if s.get("awaiting_mood_note"):
        s["awaiting_mood_note"] = False
        daily_add(iso(utcnow()), uid, comment=text[:400])
        await update.message.reply_text(T[lang]["mood_thanks"])
        await show_quickbar(context, update.effective_chat.id, lang)
        return

    # Health60 flow
    if s.get("awaiting_h60_text"):
        s["awaiting_h60_text"] = False
        plan = health60_make_plan(lang, text, prof)
        fb = InlineKeyboardMarkup([
            [InlineKeyboardButton(T[lang]["fb_good"], callback_data="fb|good"),
             InlineKeyboardButton(T[lang]["fb_bad"],  callback_data="fb|bad")],
            [InlineKeyboardButton(T[lang]["fb_free"], callback_data="fb|free")]
        ])
        await update.message.reply_text(plan, reply_markup=fb)
        await show_quickbar(context, update.effective_chat.id, lang)
        return

    # Free dialog → router LLM (short, empathic)
    ans = llm_router_answer(text, lang, prof)
    prefix = personalized_prefix(lang, prof)
    reply = (prefix + "\n" if prefix else "") + (ans.get("assistant_reply") or T[lang]["unknown"])
    await send_unique(update.message, uid, reply[:1200])
    await show_quickbar(context, update.effective_chat.id, lang)

# Small feedback handler via text after button
async def on_free_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang=_user_lang(uid)
    sessions.setdefault(uid, {})["awaiting_free_fb"] = False
    feedback_add(iso(utcnow()), uid, update.effective_user.full_name or "", update.effective_user.username or "", "free", (update.message.text or "")[:400])
    await update.message.reply_text(T[lang]["fb_thanks"])

# -------- Post-callback text routing for feedback --------
async def on_any_callback_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # (This handler is not necessary; kept for future)
    pass

# -------- Build & run app --------
def build_app():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Commands
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
    app.add_handler(CommandHandler("quiet",        cmd_quiet))

    app.add_handler(CommandHandler("health60",     cmd_health60))
    app.add_handler(CommandHandler("mood",         cmd_mood))
    app.add_handler(CommandHandler("energy",       cmd_energy))
    app.add_handler(CommandHandler("hydrate",      cmd_hydrate))
    app.add_handler(CommandHandler("skintip",      cmd_skintip))
    app.add_handler(CommandHandler("life",         cmd_life))

    # Language quick switches from Part 1
    app.add_handler(CommandHandler("ru",           cmd_lang_ru))
    app.add_handler(CommandHandler("en",           cmd_lang_en))
    app.add_handler(CommandHandler("uk",           cmd_lang_uk))
    app.add_handler(CommandHandler("es",           cmd_lang_es))

    # Callbacks (group=2 exactly one place)
    app.add_handler(CallbackQueryHandler(cb_handler), group=2)

    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app

def main():
    if not TELEGRAM_TOKEN:
        logging.error("TELEGRAM_TOKEN not set"); return
    app = build_app()
    logging.info("Starting TendAI…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
