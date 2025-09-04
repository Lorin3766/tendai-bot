# -*- coding: utf-8 -*-
# ========== TendAI — Part 1/2: база и UX ==========

import os, re, json, uuid, logging, math, random
from datetime import datetime, timedelta, timezone, time as dtime, date
from typing import List, Tuple, Dict, Optional, Set
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

from openai import OpenAI

# ---------- Google Sheets (robust + memory fallback) ----------
import gspread
from gspread.exceptions import SpreadsheetNotFound, WorksheetNotFound, APIError
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
DEFAULT_EVENING_LOCAL = "20:00"
DEFAULT_QUIET_HOURS = "22:00-08:00"
AUTO_MAX_PER_DAY = 2

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
        "help": "Short checkups, 24–48h plans, reminders, daily check-ins.\nCommands: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /evening_on 20:00 /evening_off /settz +2 /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
        "privacy": "TendAI is not a medical service and can’t replace a doctor. We store minimal data for reminders. /delete_data to erase.",
        "ask_consent": "May I send you a follow-up to check how you feel later?",
        "yes":"Yes","no":"No",
        "unknown":"I need a bit more info: where exactly and for how long?",
        "profile_intro":"Quick intake (~40s). Use buttons or type your answer.",
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
        "start_where":"Where do you want to start now? (symptom/sleep/nutrition/labs/habits/longevity)",
        "daily_gm":"Good morning! Quick daily check-in:",
        "mood_excellent":"👍 Great","mood_ok":"🙂 Okay","mood_tired":"😐 Tired","mood_pain":"🤕 Pain","mood_skip":"⏭ Skip",
        "mood_note":"✍️ Comment",
        "mood_thanks":"Thanks! Have a smooth day 👋",
        "mood_cmd":"How do you feel right now?",
        "er_text":"If symptoms worsen, severe shortness of breath, chest pain, confusion, or persistent high fever — seek urgent care/emergency.",
        "quick_title":"Quick actions",
        "quick_h60":"⚡ Health in 60s",
        "quick_er":"🚑 Emergency",
        "quick_lab":"🧪 Lab",
        "quick_rem":"⏰ Reminder",
        # Health60
        "h60_btn": "Health in 60 seconds",
        "h60_intro": "Write briefly what bothers you (e.g., “headache”, “fatigue”, “stomach pain”). I’ll give you 3 key tips in 60 seconds.",
        "h60_t1": "Possible causes",
        "h60_t2": "Do now (24–48h)",
        "h60_t3": "When to see a doctor",
        "h60_serious": "Serious to rule out",
        # Youth Pack
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
        "set_quiet_btn": "🌙 Quiet hours",
        "ask_quiet": "Type quiet hours as HH:MM-HH:MM (local), e.g. 22:00-08:00",
        "evening_intro": "Evening check-in:",
        "evening_tip_btn": "🪄 Tip of the day",
        "evening_set": "Evening check-in set to {t} (local).",
        "evening_off": "Evening check-in disabled.",
        # Life metrics
        "life_today": "Today is your {n}-th day of life 🎉. Goal — 36,500 (100y).",
        "life_pct": "You’ve passed {p}%.",
        "life_estimate": "(Estimated from age; set /profile birth date for accuracy.)",
        # Social proof
        "sp_sleep": "70% of your age group found their sleep triggers.",
        "sp_water": "Most peers feel better after simple hydration tracking.",
    },
    "ru": {
        "welcome":"Привет! Я TendAI — ассистент здоровья и долголетия.\nРасскажи, что беспокоит; я подскажу. Сначала короткий опрос (~40с), чтобы советы были точнее.",
        "help":"Короткие проверки, план на 24–48 ч, напоминания, ежедневные чек-ины.\nКоманды: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /evening_on 20:00 /evening_off /settz +3 /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
        "privacy":"TendAI не заменяет врача. Храним минимум данных для напоминаний. /delete_data — удалить.",
        "ask_consent":"Можно прислать напоминание позже, чтобы узнать, как вы?",
        "yes":"Да","no":"Нет",
        "unknown":"Нужно чуть больше деталей: где именно и сколько длится?",
        "profile_intro":"Быстрый опрос (~40с). Можно нажимать кнопки или писать свой ответ.",
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
        "start_where":"С чего начнём? (симптом/сон/питание/анализы/привычки/долголетие)",
        "daily_gm":"Доброе утро! 🌞 Как сегодня самочувствие?",
        "mood_excellent":"👍 Отлично","mood_ok":"🙂 Нормально","mood_tired":"😐 Устал","mood_pain":"🤕 Болит","mood_skip":"⏭ Пропустить",
        "mood_note":"✍️ Комментарий",
        "mood_thanks":"Спасибо! Хорошего дня 👋",
        "mood_cmd":"Как сейчас самочувствие?",
        "er_text":"Если нарастает, сильная одышка, боль в груди, спутанность, стойкая высокая температура — срочно в неотложку/скорую.",
        "quick_title":"Быстрые действия",
        "quick_h60":"⚡ Здоровье за 60 сек",
        "quick_er":"🚑 Срочно в скорую",
        "quick_lab":"🧪 Лаборатория",
        "quick_rem":"⏰ Напоминание",
        # Health60
        "h60_btn": "Здоровье за 60 секунд",
        "h60_intro": "Коротко напишите, что беспокоит (например: «болит голова», «усталость», «боль в животе»). Дам 3 ключевых совета за 60 секунд.",
        "h60_t1": "Возможные причины",
        "h60_t2": "Что сделать сейчас (24–48 ч)",
        "h60_t3": "Когда обратиться к врачу",
        "h60_serious": "Что серьёзное исключить",
        # Youth Pack
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
        "set_quiet_btn": "🌙 Тихие часы",
        "ask_quiet": "Введите тихие часы как ЧЧ:ММ-ЧЧ:ММ (локально), напр. 22:00-08:00",
        "evening_intro": "Вечерний чек-ин:",
        "evening_tip_btn": "🪄 Совет дня",
        "evening_set": "Вечерний чек-ин установлен на {t} (локально).",
        "evening_off": "Вечерний чек-ин отключён.",
        # Life metrics
        "life_today": "Сегодня твой {n}-й день жизни 🎉. Цель — 36 500 (100 лет).",
        "life_pct": "Ты прошёл {p}%.",
        "life_estimate": "(Оценочно по возрасту; укажи дату рождения в /profile для точности.)",
        # Social proof
        "sp_sleep": "70% твоего возраста нашли триггеры сна.",
        "sp_water": "Большинство чувствуют себя лучше после простого учёта воды.",
    },
    "uk": {
        "welcome":"Привіт! Я TendAI — асистент здоров’я та довголіття.\nРозкажи, що турбує; я підкажу. Спершу швидкий опитник (~40с) для точніших порад.",
        "help":"Короткі перевірки, план на 24–48 год, нагадування, щоденні чек-іни.\nКоманди: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /evening_on 20:00 /evening_off /settz +2 /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
        "privacy":"TendAI не замінює лікаря. Зберігаємо мінімум даних для нагадувань. /delete_data — видалити.",
        "ask_consent":"Можу надіслати нагадування пізніше, щоб дізнатися, як ви?",
        "yes":"Так","no":"Ні",
        "unknown":"Потрібно трохи більше: де саме і скільки триває?",
        "profile_intro":"Швидкий опитник (~40с). Можна натискати кнопки або писати свій варіант.",
        "p_step_1":"Крок 1/8. Стать:",
        "p_step_2":"Крок 2/8. Вік:",
        "p_step_3":"Крок 3/8. Головна мета:",
        "p_step_4":"Крок 4/8. Хронічні хвороби:",
        "p_step_5":"Крок 5/8. Ліки/добавки/алергії:",
        "p_step_6":"Крок 6/8. Сон (відбій/підйом, напр. 23:30/07:00):",
        "p_step_7":"Крок 7/8. Активність:",
        "p_step_8":"Крок 8/8. Харчування переважно:",
        "write":"✍️ Написати",
        "skip":"⏭️ Пропустити",
        "saved_profile":"Зберіг: ",
        "start_where":"З чого почнемо? (симптом/сон/харчування/аналізи/звички/довголіття)",
        "daily_gm":"Доброго ранку! 🌞 Як сьогодні самопочуття?",
        "mood_excellent":"👍 Чудово","mood_ok":"🙂 Нормально","mood_tired":"😐 Втома","mood_pain":"🤕 Болить","mood_skip":"⏭ Пропустити",
        "mood_note":"✍️ Коментар",
        "mood_thanks":"Дякую! Гарного дня 👋",
        "mood_cmd":"Як почуваєтесь зараз?",
        "er_text":"Якщо посилюється, сильна задишка, біль у грудях, сплутаність, тривала висока температура — терміново до невідкладної/швидкої.",
        "quick_title":"Швидкі дії",
        "quick_h60":"⚡ Здоров’я за 60 с",
        "quick_er":"🚑 Невідкладно",
        "quick_lab":"🧪 Лабораторія",
        "quick_rem":"⏰ Нагадування",
        # Health60
        "h60_btn": "Здоров’я за 60 секунд",
        "h60_intro": "Коротко напишіть, що турбує (наприклад: «болить голова», «втома», «біль у животі»). Дам 3 ключові поради за 60 секунд.",
        "h60_t1": "Можливі причини",
        "h60_t2": "Що зробити зараз (24–48 год)",
        "h60_t3": "Коли звернутися до лікаря",
        "h60_serious": "Що серйозне виключити",
        # Youth Pack
        "youth_pack": "Молодіжний пакет",
        "gm_energy": "⚡ Енергія",
        "gm_energy_q": "Як енергія (1–5)?",
        "gm_energy_done": "Енергію записано — дякую!",
        "gm_evening_btn": "⏰ Нагадати ввечері",
        "hydrate_btn": "💧 Гідратація",
        "hydrate_nudge": "💧 Час для склянки води",
        "skintip_btn": "🧴 Порада для шкіри/тіла",
        "skintip_sent": "Пораду надіслано.",
        "daily_tip_prefix": "🍎 Підказка дня:",
        "set_quiet_btn": "🌙 Тихі години",
        "ask_quiet": "Введіть тихі години як ГГ:ХХ-ГГ:ХХ (локально), напр. 22:00-08:00",
        "evening_intro": "Вечірній чек-ін:",
        "evening_tip_btn": "🪄 Порада дня",
        "evening_set": "Вечірній чек-ін встановлено на {t} (локально).",
        "evening_off": "Вечірній чек-ін вимкнено.",
        # Life metrics
        "life_today": "Сьогодні твій {n}-й день життя 🎉. Мета — 36 500 (100 років).",
        "life_pct": "Ти пройшов {p}%.",
        "life_estimate": "(Оціночно за віком; вкажи дату народження в /profile для точності.)",
        # Social proof
        "sp_sleep": "70% твого віку знайшли тригери сну.",
        "sp_water": "Більшість почувається краще після простого обліку води.",
    },
}
T["es"] = T["en"]  # простая заглушка


# --- Personalized prefix shown before LLM reply ---
def personalized_prefix(lang: str, profile: dict) -> str:
    sex = (profile.get("sex") or "").strip()
    goal = (profile.get("goal") or "").strip()
    cond = (profile.get("conditions") or "").strip()
    habits = (profile.get("habits") or "").strip()
    age_raw = str(profile.get("age") or "")
    m = re.search(r"\d+", age_raw)
    age = m.group(0) if m else ""
    parts = []
    if sex: parts.append(sex)
    if age: parts.append(f"{age}y")
    if goal: parts.append(f"goal: {goal}")
    if cond: parts.append(f"hx: {cond}")
    if habits: parts.append(f"habits: {habits}")
    if parts:
        return " · ".join(parts)
    return ""


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

def age_to_band(age: int) -> str:
    if age <= 0: return "unknown"
    if age <= 25: return "18–25"
    if age <= 35: return "26–35"
    if age <= 45: return "36–45"
    if age <= 60: return "46–60"
    return "60+"

# ===== ONBOARDING GATE (Шторка) =====
GATE_FLAG_KEY = "menu_unlocked"

def _is_menu_unlocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get(GATE_FLAG_KEY):
        return True
    prof = profiles_get(update.effective_user.id) or {}
    return not profile_is_incomplete(prof)


# === MINI-INTAKE (с первого сообщения) ===
MINI_KEYS = ["sex","age","goal","diet_focus","steps_target","conditions","meds_allergies","habits"]
MINI_FREE_KEYS: Set[str] = {"meds_allergies"}
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
        "label":{"ru":"Цель:","en":"Main goal:","uk":"Мета:","es":"Objetivo:"}
    },
    "diet_focus": {
        "ru":[("Сбаланс.","balanced"),("Низкоугл","lowcarb"),("Растит.","plant"),("Нерегул.","irregular")],
        "en":[("Balanced","balanced"),("Low-carb","lowcarb"),("Plant-based","plant"),("Irregular","irregular")],
        "uk":[("Збаланс.","balanced"),("Маловугл.","lowcarb"),("Рослинне","plant"),("Нерегул.","irregular")],
        "es":[("Equilibrada","balanced"),("Baja carb.","lowcarb"),("Vegetal","plant"),("Irregular","irregular")],
        "label":{"ru":"Питание:","en":"Diet mostly:","uk":"Харчування:","es":"Dieta:"}
    },
    "steps_target": {
        "ru":[("<5к","5000"),("5–8к","8000"),("8–12к","12000"),("Спорт","15000")],
        "en":[("<5k","5000"),("5–8k","8000"),("8–12k","12000"),("Sport","15000")],
        "uk":[("<5к","5000"),("5–8к","8000"),("8–12к","12000"),("Спорт","15000")],
        "es":[("<5k","5000"),("5–8k","8000"),("8–12k","12000"),("Deporte","15000")],
        "label":{"ru":"Шаги/активность:","en":"Steps/activity:","uk":"Кроки/активність:","es":"Pasos/actividad:"}
    },
    "conditions": {
        "ru":[("Нет","none"),("Сердечно-сосуд.","cvd"),("ЩЖ/эндокр.","endocrine"),("ЖКТ","gi"),("Аллергия","allergy"),("Другое","other")],
        "en":[("None","none"),("Cardio/vascular","cvd"),("Thyroid/endocrine","endocrine"),("GI","gi"),("Allergy","allergy"),("Other","other")],
        "uk":[("Немає","none"),("Серцево-суд.","cvd"),("ЩЗ/ендокр.","endocrine"),("ШКТ","gi"),("Алергія","allergy"),("Інше","other")],
        "es":[("Ninguno","none"),("Cardio/vascular","cvd"),("Tiroides/endócr.","endocrine"),("GI","gi"),("Alergia","allergy"),("Otro","other")],
        "label":{"ru":"Хронические состояния:","en":"Chronic conditions:","uk":"Хронічні стани:","es":"Condiciones crónicas:"}
    },
    "meds_allergies": {
        "ru":[], "en":[], "uk":[], "es":[],
        "label":{"ru":"Лекарства/добавки/аллергии (коротко):","en":"Meds/supplements/allergies (short):","uk":"Ліки/добавки/алергії (коротко):","es":"Medicamentos/suplementos/alergias (corto):"}
    },
    "habits": {
        "ru":[("Не курю","no_smoke"),("Курю","smoke"),("Алкоголь редко","alc_low"),("Алкоголь часто","alc_high"),("Кофеин 0–1","caf_low"),("Кофеин 2–3","caf_mid"),("Кофеин 4+","caf_high")],
        "en":[("No smoking","no_smoke"),("Smoking","smoke"),("Alcohol rare","alc_low"),("Alcohol often","alc_high"),("Caffeine 0–1","caf_low"),("Caffeine 2–3","caf_mid"),("Caffeine 4+","caf_high")],
        "uk":[("Не курю","no_smoke"),("Курю","smoke"),("Алкоголь рідко","alc_low"),("Алкоголь часто","alc_high"),("Кофеїн 0–1","caf_low"),("Кофеїн 2–3","caf_mid"),("Кофеїн 4+","caf_high")],
        "es":[("No fuma","no_smoke"),("Fuma","smoke"),("Alcohol raro","alc_low"),("Alcohol a menudo","alc_high"),("Cafeína 0–1","caf_low"),("Cafeína 2–3","caf_mid"),("Cafeína 4+","caf_high")],
        "label":{"ru":"Привычки (выберите ближе всего):","en":"Habits (pick closest):","uk":"Звички (оберіть ближче):","es":"Hábitos (elige):"}
    },
}

async def start_mini_intake(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: int):
    sessions[uid] = {"mini_active": True, "mini_step": 0, "mini_answers": {}}
    await context.bot.send_message(chat_id, {
        "ru":"🔎 Мини-опроc для персонализации (4–6 кликов).",
        "uk":"🔎 Міні-опитування для персоналізації (4–6 торкань).",
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
        profiles_upsert(uid, answers)
        sessions[uid]["mini_active"] = False
        await context.bot.send_message(chat_id, T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
        # показать постоянные быстрые кнопки
        await show_quickbar(context, chat_id, lang)
        return
    key = MINI_KEYS[step_idx]
    label = MINI_STEPS[key]["label"][lang]
    await context.bot.send_message(chat_id, label, reply_markup=build_mini_kb(lang, key))

def mini_handle_choice(uid: int, key: str, value: str):
    s = sessions.setdefault(uid, {"mini_active": True, "mini_step": 0, "mini_answers": {}})
    s["mini_answers"][key] = value
    s["mini_step"] = int(s.get("mini_step", 0)) + 1


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
ws_feedback = ws_users = ws_profiles = ws_episodes = ws_reminders = ws_daily = None
ws_rules = ws_challenges = None

GSPREAD_CLIENT: Optional[gspread.client.Client] = None
SPREADSHEET_ID_FOR_INTAKE: str = ""

def _ws_headers(ws):
    try:
        row = ws.row_values(1)
        return row if row else []
    except Exception:
        return []

def _ws_ensure_columns(ws, desired_headers: List[str]):
    """
    Надёжное добавление недостающих заголовков + авторасширение листа.
    (фикс для 'AA1 exceeds grid limits')
    """
    try:
        current = _ws_headers(ws)
        # если лист пуст — сначала обеспечим нужное число колонок
        if not current:
            if ws.col_count < len(desired_headers):
                ws.add_cols(len(desired_headers) - ws.col_count)
            ws.append_row(desired_headers)
            return

        # если не хватает колонок до длины desired_headers — расширяем
        if ws.col_count < len(desired_headers):
            ws.add_cols(len(desired_headers) - ws.col_count)

        # дописываем недостающие заголовки справа
        missing = [h for h in desired_headers if h not in current]
        if missing:
            # гарантия, что хватит места под все missing
            need_extra = len(missing)
            if ws.col_count < len(current) + need_extra:
                ws.add_cols(len(current) + need_extra - ws.col_count)
            for h in missing:
                ws.update_cell(1, len(current)+1, h)
                current.append(h)
    except APIError as e:
        logging.warning(f"ensure columns APIError for {getattr(ws,'title','?')}: {e}")
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
            except gspread.WorksheetNotFound:
                # создаём с запасом колонок
                ws = ss.add_worksheet(title=title, rows=4000, cols=max(50, len(headers)))
                # убедимся, что колонок хватает и первая строка — заголовки
                if ws.col_count < len(headers):
                    ws.add_cols(len(headers) - ws.col_count)
                ws.append_row(headers)

            if not _ws_headers(ws):
                if ws.col_count < len(headers):
                    ws.add_cols(len(headers) - ws.col_count)
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

# --------- Sessions ----------
sessions: Dict[int, dict] = {}


# -------- Sheets wrappers --------
def _headers(ws):
    return _ws_headers(ws)

def users_get(uid: int) -> dict:
    if SHEETS_ENABLED:
        for r in ws_users.get_all_records():
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
        vals = ws_users.get_all_records()
        hdr = _headers(ws_users)
        # диапазон для апдейта
        end_col = gsu.rowcol_to_a1(1, len(hdr)).rstrip("1")
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                merged = {**r, **{k: base[k] for k in base if not str(r.get(k) or "").strip()}}
                ws_users.update(f"A{i}:{end_col}{i}", [[merged.get(h, "") for h in hdr]])
                return
        ws_users.append_row([base.get(h, "") for h in hdr])
    else:
        MEM_USERS[uid] = {**MEM_USERS.get(uid, {}), **base}

def users_set(uid: int, field: str, value: str):
    if SHEETS_ENABLED:
        vals = ws_users.get_all_records()
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                hdr = _headers(ws_users)
                if field not in hdr:
                    _ws_ensure_columns(ws_users, hdr + [field])
                    hdr = _headers(ws_users)
                ws_users.update_cell(i, hdr.index(field)+1, value)
                return
    else:
        u = MEM_USERS.setdefault(uid, {})
        u[field] = value

def profiles_get(uid: int) -> dict:
    if SHEETS_ENABLED:
        for r in ws_profiles.get_all_records():
            if str(r.get("user_id")) == str(uid):
                return r
        return {}
    return MEM_PROFILES.get(uid, {})

def profiles_upsert(uid: int, patch: dict):
    patch = {k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)) for k, v in patch.items()}
    if SHEETS_ENABLED:
        vals = ws_profiles.get_all_records()
        hdr = _headers(ws_profiles)
        # убедимся, что все поля есть
        needed = list(set(hdr) | set(patch.keys()))
        if needed != hdr:
            _ws_ensure_columns(ws_profiles, needed)
            hdr = _headers(ws_profiles)
        # поиск строки
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                merged = {**r, **patch}
                ws_profiles.update(f"A{i}:{gsu.rowcol_to_a1(1, len(hdr)).rstrip('1')}{i}", [[merged.get(h, "") for h in hdr]])
                return
        base = {"user_id": str(uid)}
        merged = {**base, **{h:"" for h in hdr if h not in base}, **patch}
        ws_profiles.append_row([merged.get(h, "") for h in hdr])
    else:
        MEM_PROFILES[uid] = {**MEM_PROFILES.get(uid, {}), **patch}

def feedback_add(ts: str, uid: int, name: str, username: str, rating: str, comment: str):
    row = {"timestamp":ts,"user_id":str(uid),"name":name,"username":username,"rating":rating,"comment":comment}
    if SHEETS_ENABLED:
        hdr = _headers(ws_feedback)
        _ws_ensure_columns(ws_feedback, hdr)
        ws_feedback.append_row([row.get(h,"") for h in hdr])
    else:
        MEM_FEEDBACK.append(row)

def daily_add(ts: str, uid: int, mood: str, comment: str, energy: Optional[int]=None):
    row = {"timestamp":ts,"user_id":str(uid),"mood":mood,"energy":("" if energy is None else str(energy)),"comment":comment}
    if SHEETS_ENABLED:
        hdr = _headers(ws_daily)
        _ws_ensure_columns(ws_daily, hdr)
        ws_daily.append_row([row.get(h,"") for h in hdr])
    else:
        MEM_DAILY.append(row)

def episode_create(uid: int, topic: str, severity: int=5, red: str="") -> str:
    eid = str(uuid.uuid4())
    row = {"episode_id":eid,"user_id":str(uid),"topic":topic,"started_at":iso(utcnow()),
           "baseline_severity":str(severity),"red_flags":red,"plan_accepted":"","target":"","reminder_at":"",
           "next_checkin_at":"","status":"open","last_update":iso(utcnow()),"notes":""}
    if SHEETS_ENABLED:
        hdr = _headers(ws_episodes)
        _ws_ensure_columns(ws_episodes, hdr)
        ws_episodes.append_row([row.get(h,"") for h in hdr])
    else:
        MEM_EPISODES.append(row)
    return eid

def episode_find_open(uid: int) -> Optional[dict]:
    if SHEETS_ENABLED:
        for r in ws_episodes.get_all_records():
            if str(r.get("user_id")) == str(uid) and str(r.get("status","")) == "open":
                return r
        return None
    for r in reversed(MEM_EPISODES):
        if r["user_id"] == str(uid) and r.get("status") == "open":
            return r
    return None

def episode_set(eid: str, field: str, value: str):
    if SHEETS_ENABLED:
        vals = ws_episodes.get_all_records()
        hdr = _headers(ws_episodes)
        for i, r in enumerate(vals, start=2):
            if str(r.get("episode_id")) == str(eid):
                if field not in hdr:
                    _ws_ensure_columns(ws_episodes, hdr + [field]); hdr = _headers(ws_episodes)
                ws_episodes.update_cell(i, hdr.index(field)+1, value)
                return
    else:
        for r in MEM_EPISODES:
            if r["episode_id"] == eid:
                r[field] = value
                return

def challenge_get(uid: int) -> Optional[dict]:
    if SHEETS_ENABLED:
        for r in ws_challenges.get_all_records():
            if str(r.get("user_id")) == str(uid) and str(r.get("status","")) != "done":
                return r
        return None
    for r in MEM_CHALLENGES:
        if r["user_id"] == str(uid) and r.get("status") != "done":
            return r
    return None

def challenge_start(uid: int, name: str="Hydration 7d", length: int=7):
    row = {"user_id":str(uid),"challenge_id":str(uuid.uuid4()),"name":name,"start_date":iso(utcnow()),
           "length_days":str(length),"days_done":"0","status":"active"}
    if SHEETS_ENABLED:
        ws_challenges.append_row([row.get(h,"") for h in _headers(ws_challenges)])
    else:
        MEM_CHALLENGES.append(row)

def reminder_add(uid: int, text: str, when_utc: datetime) -> str:
    rid = str(uuid.uuid4())
    row = {"id":rid,"user_id":str(uid),"text":text,"when_utc":iso(when_utc),"created_at":iso(utcnow()),"status":"open"}
    if SHEETS_ENABLED:
        ws_reminders.append_row([row.get(h,"") for h in _headers(ws_reminders)])
    else:
        MEM_REMINDERS.append(row)
    return rid

def reminder_close(rid: str):
    if SHEETS_ENABLED:
        vals = ws_reminders.get_all_records()
        hdr = _headers(ws_reminders)
        for i, r in enumerate(vals, start=2):
            if str(r.get("id")) == str(rid):
                ws_reminders.update_cell(i, hdr.index("status")+1, "sent")
                return
    else:
        for r in MEM_REMINDERS:
            if r["id"] == rid:
                r["status"] = "sent"
                return

def update_last_seen(uid: int):
    users_set(uid, "last_seen", iso(utcnow()))

def _user_lang(uid: int) -> str:
    u = users_get(uid)
    return norm_lang(u.get("lang") or "en")


# ---------- Quick UI ----------
def quickbar_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["quick_h60"], callback_data="menu|h60")],
        [InlineKeyboardButton(T[lang]["quick_er"],  callback_data="menu|er"),
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
        [InlineKeyboardButton(T[lang]["h60_btn"], callback_data="menu|h60")],
        [InlineKeyboardButton("Sleep", callback_data="menu|sleep"),
         InlineKeyboardButton("Nutrition", callback_data="menu|nutri")],
        [InlineKeyboardButton("Labs", callback_data="menu|lab"),
         InlineKeyboardButton("Habits", callback_data="menu|habits")],
        [InlineKeyboardButton("Longevity", callback_data="menu|youth")]
    ])


# ---------- Time & TZ helpers ----------
def _user_tz_off(uid: int) -> int:
    try:
        return int(str(users_get(uid).get("tz_offset") or "0"))
    except Exception:
        return 0

def hhmm_tuple(hhmm: str) -> Tuple[int,int]:
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", (hhmm or "").strip())
    if not m: return (8,30)
    return (int(m.group(1)), int(m.group(2)))

def user_local_now(uid: int) -> datetime:
    off = _user_tz_off(uid)
    return utcnow() + timedelta(hours=off)

def local_to_utc_dt(uid: int, local_dt: datetime) -> datetime:
    off = _user_tz_off(uid)
    return (local_dt - timedelta(hours=off)).astimezone(timezone.utc)

def adjust_out_of_quiet(target_local: datetime, quiet: str) -> datetime:
    """
    Если целевое время попадает в тихие часы, сдвигаем на конец «окна».
    quiet: 'HH:MM-HH:MM'
    """
    try:
        m = re.match(r"^\s*([01]?\d|2[0-3]):[0-5]\d-([01]?\d|2[0-3]):[0-5]\d\s*$", quiet or "")
        if not m: return target_local
        start = m.group(0).split("-")[0]; end = m.group(0).split("-")[1]
        s_h, s_m = hhmm_tuple(start); e_h, e_m = hhmm_tuple(end)
        start_t = target_local.replace(hour=s_h, minute=s_m, second=0, microsecond=0)
        end_t   = target_local.replace(hour=e_h, minute=e_m, second=0, microsecond=0)
        if end_t <= start_t:  # окно через полночь
            if target_local.time() >= dtime(hour=s_h, minute=s_m) or target_local.time() < dtime(hour=e_h, minute=e_m):
                return end_t if target_local.time() < dtime(hour=e_h, minute=e_m) else end_t + timedelta(days=1)
            return target_local
        else:
            if start_t <= target_local <= end_t:
                return end_t
            return target_local
    except Exception:
        return target_local

def _user_quiet_hours(uid: int) -> str:
    p = profiles_get(uid) or {}
    return (p.get("quiet_hours") or DEFAULT_QUIET_HOURS).strip()


# ---------- Life metrics ----------
def life_metrics(profile: dict) -> Dict[str, int]:
    """
    Если есть birth_date (YYYY-MM-DD) — считаем точно.
    Иначе оценка по age * 365.
    """
    today = date.today()
    bd = (profile.get("birth_date") or "").strip()
    days = 0
    if re.match(r"^\d{4}-\d{2}-\d{2}$", bd):
        try:
            y, m, d = [int(x) for x in bd.split("-")]
            born = date(y, m, d)
            days = (today - born).days
        except Exception:
            days = 0
    if days <= 0:
        try:
            a = int(re.findall(r"\d+", str(profile.get("age") or "0"))[0])
        except Exception:
            a = 0
        days = max(0, a * 365)
    pct = min(100, round(days / 36500 * 100, 1))  # к 100 годам
    return {"days_lived": days, "percent_to_100": pct}

def progress_bar(percent: float, width: int = 12) -> str:
    full = int(round(percent/100 * width))
    return "█" * full + "░" * (width - full)


# ------------- LLM Router (with personalization) -------------
SYS_ROUTER = (
    "You are TendAI — a concise, warm, professional health & longevity assistant (not a doctor). "
    "Answer strictly in {lang}. Keep replies short (<=5 lines + up to 4 bullets). "
    "Personalize with profile (sex/age/goal/conditions/habits). "
    "TRIAGE: ask 1–2 clarifiers first; ER only for clear red flags. "
    "Return JSON ONLY: {\"intent\":\"symptom|nutrition|sleep|labs|habits|longevity|other\",\"assistant_reply\":\"...\",\"followups\":[\"...\"],\"needs_more\":true,\"red_flags\":false,\"confidence\":0.0}"
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


# ===== Health60 =====
def _fmt_bullets(items: list) -> str:
    return "\n".join([f"• {x}" for x in items if isinstance(x, str) and x.strip()])

SYS_H60 = (
    "You are TendAI — a concise, warm, professional health assistant (not a doctor). "
    "Answer strictly in {lang}. Keep it short and practical. "
    "Given a symptom text and a brief profile, output JSON ONLY: "
    "{\"causes\":[\"...\"],\"serious\":\"...\",\"do_now\":[\"...\"],\"see_doctor\":[\"...\"]}. "
    "2–4 causes, exactly 1 serious, 3–5 do_now steps, 2–3 see_doctor cues."
)

def health60_make_plan(lang: str, symptom_text: str, profile: dict) -> str:
    fallback_map = {
        "ru": (f"{T['ru']['h60_t1']}:\n• Наиболее вероятные бытовые причины\n"
               f"{T['ru']['h60_serious']}: • Исключить редкие, но серьёзные при ухудшении\n\n"
               f"{T['ru']['h60_t2']}:\n• Вода 300–500 мл\n• Отдых 15–20 мин\n• Проветривание, меньше экранов\n\n"
               f"{T['ru']['h60_t3']}:\n• Усиление симптомов\n• Высокая температура\n• Боль ≥7/10"),
        "uk": (f"{T['uk']['h60_t1']}:\n• Найімовірні побутові причини\n"
               f"{T['uk']['h60_serious']}: • Виключити рідкісні, але серйозні при погіршенні\n\n"
               f"{T['uk']['h60_t2']}:\n• Вода 300–500 мл\n• Відпочинок 15–20 хв\n• Провітрювання, менше екранів\n\n"
               f"{T['uk']['h60_t3']}:\n• Посилення симптомів\n• Висока температура\n• Біль ≥7/10"),
        "en": (f"{T['en']['h60_t1']}:\n• Most likely everyday causes\n"
               f"{T['en']['h60_serious']}: • Rule out rare but serious if worsening\n\n"
               f"{T['en']['h60_t2']}:\n• Drink 300–500 ml water\n• 15–20 min rest\n• Ventilate, reduce screens\n\n"
               f"{T['en']['h60_t3']}:\n• Worsening\n• High fever\n• Pain ≥7/10"),
    }
    fallback = fallback_map.get(lang, fallback_map["en"])

    if not oai:
        return fallback

    sys = SYS_H60.replace("{lang}", lang)
    user = {
        "symptom": (symptom_text or "").strip()[:500],
        "profile": {k: profile.get(k, "") for k in ["sex","age","goal","conditions","meds","sleep","activity","diet","diet_focus","steps_target","habits"]}
    }
    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.2,
            max_tokens=420,
            response_format={"type": "json_object"},
            messages=[{"role":"system","content": sys},{"role":"user","content": json.dumps(user, ensure_ascii=False)}]
        )
        data = json.loads(resp.choices[0].message.content.strip())
        causes = _fmt_bullets(data.get("causes") or [])
        serious = (data.get("serious") or "").strip()
        do_now = _fmt_bullets(data.get("do_now") or [])
        see_doc = _fmt_bullets(data.get("see_doctor") or [])

        parts = []
        if causes:  parts.append(f"{T[lang]['h60_t1']}:\n{causes}")
        if serious: parts.append(f"{T[lang]['h60_serious']}: {serious}")
        if do_now:  parts.append(f"\n{T[lang]['h60_t2']}:\n{do_now}")
        if see_doc: parts.append(f"\n{T[lang]['h60_t3']}:\n{see_doc}")
        return "\n".join(parts).strip()
    except Exception as e:
        logging.error(f"health60 LLM error: {e}")
        return fallback
# ========= PART 2 — BEHAVIOR & LOGIC =========
# Всё ниже дополняет Часть 1. Функции и имена из Ч.1 не ломаются.

# ---------- Safety helpers (если их нет в Ч.1 — используем эти; если есть — переопределение безопасно) ----------
def _user_lang(uid: int) -> str:
    try:
        return norm_lang(users_get(uid).get("lang") or "en")
    except Exception:
        return "en"

def update_last_seen(uid: int):
    try:
        users_set(uid, "last_seen", iso(utcnow()))
    except Exception as e:
        logging.warning(f"update_last_seen fallback: {e}")

def _user_tz_off(uid: int) -> int:
    try:
        u = users_get(uid)
        return int(str(u.get("tz_offset") or "0"))
    except Exception:
        return 0

def user_local_now(uid: int) -> datetime:
    return utcnow() + timedelta(hours=_user_tz_off(uid))

def hhmm_tuple(hhmm: str) -> Tuple[int,int]:
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", hhmm or "")
    return (int(m.group(1)), int(m.group(2))) if m else (8,30)

def local_to_utc_dt(uid: int, local_dt: datetime) -> datetime:
    return local_dt - timedelta(hours=_user_tz_off(uid))

def adjust_out_of_quiet(local_dt: datetime, qh: str) -> datetime:
    try:
        m = re.match(r"^\s*([01]?\d:[0-5]\d)-([01]?\d:[0-5]\d)\s*$", qh or "")
        if not m:
            return local_dt
        a_h,a_m = hhmm_tuple(m.group(1))
        b_h,b_m = hhmm_tuple(m.group(2))
        start = local_dt.replace(hour=a_h, minute=a_m, second=0, microsecond=0)
        end   = local_dt.replace(hour=b_h, minute=b_m, second=0, microsecond=0)
        if start <= end:
            in_quiet = (start <= local_dt <= end)
        else:
            # окно «на ночь» (например 22:00-08:00)
            in_quiet = (local_dt >= start) or (local_dt <= end)
        return end if in_quiet else local_dt
    except Exception:
        return local_dt

def _has_jq_ctx(obj) -> bool:
    try:
        return bool(getattr(getattr(obj, "application", obj), "job_queue", None))
    except Exception:
        return False

# ---------- Sheets wrappers (fallback safe) ----------
def reminder_add(uid: int, text: str, when_utc: datetime) -> str:
    rid = str(uuid.uuid4())
    row = {
        "id": rid, "user_id": str(uid), "text": text,
        "when_utc": iso(when_utc), "created_at": iso(utcnow()), "status": "pending"
    }
    if SHEETS_ENABLED and ws_reminders:
        try:
            hdr = _headers(ws_reminders)
            _ws_ensure_columns(ws_reminders, hdr)
            ws_reminders.append_row([row.get(h,"") for h in hdr])
            return rid
        except Exception as e:
            logging.warning(f"reminder_add sheet fail -> mem: {e}")
    MEM_REMINDERS.append(row)
    return rid

def reminder_close(rid: str):
    if SHEETS_ENABLED and ws_reminders:
        try:
            vals = ws_reminders.get_all_records()
            hdr  = _headers(ws_reminders)
            for i, r in enumerate(vals, start=2):
                if str(r.get("id")) == rid:
                    c = hdr.index("status")+1 if "status" in hdr else None
                    if c: ws_reminders.update_cell(i, c, "sent")
                    return
        except Exception as e:
            logging.warning(f"reminder_close sheet fail: {e}")
    for r in MEM_REMINDERS:
        if r.get("id")==rid:
            r["status"]="sent"; break

def daily_add(ts: str, uid: int, mood: str, comment: str, energy: Optional[int]=None):
    row = {"timestamp": ts, "user_id": str(uid), "mood": mood, "energy": str(energy or ""), "comment": comment or ""}
    if SHEETS_ENABLED and ws_daily:
        try:
            hdr = _headers(ws_daily)
            _ws_ensure_columns(ws_daily, hdr)
            ws_daily.append_row([row.get(h,"") for h in hdr])
            return
        except Exception as e:
            logging.warning(f"daily_add sheet fail -> mem: {e}")
    MEM_DAILY.append(row)

def feedback_add(ts: str, uid: int, name: str, username: str, rating: str, comment: str):
    row = {"timestamp": ts, "user_id": str(uid), "name": name, "username": username, "rating": rating, "comment": comment}
    if SHEETS_ENABLED and ws_feedback:
        try:
            hdr = _headers(ws_feedback)
            _ws_ensure_columns(ws_feedback, hdr)
            ws_feedback.append_row([row.get(h,"") for h in hdr])
            return
        except Exception as e:
            logging.warning(f"feedback_add sheet fail -> mem: {e}")
    MEM_FEEDBACK.append(row)

def profiles_upsert(uid: int, patch: Dict[str,str]):
    if SHEETS_ENABLED and ws_profiles:
        try:
            vals = ws_profiles.get_all_records()
            hdr  = _headers(ws_profiles)
            if "user_id" not in hdr:
                hdr = ["user_id"] + hdr; _ws_ensure_columns(ws_profiles, hdr)
            for i, r in enumerate(vals, start=2):
                if str(r.get("user_id")) == str(uid):
                    for k,v in patch.items():
                        if k not in hdr:
                            _ws_ensure_columns(ws_profiles, hdr+[k]); hdr=_headers(ws_profiles)
                        ws_profiles.update_cell(i, hdr.index(k)+1, v)
                    ws_profiles.update_cell(i, hdr.index("updated_at")+1 if "updated_at" in hdr else len(hdr), iso(utcnow()))
                    return
            # нет записи — создаём
            base = {"user_id":str(uid), "updated_at": iso(utcnow())}
            base.update(patch)
            row = [base.get(h,"") for h in hdr]
            ws_profiles.append_row(row)
            return
        except Exception as e:
            logging.warning(f"profiles_upsert sheet fail -> mem: {e}")
    prof = MEM_PROFILES.setdefault(uid, {})
    prof.update(patch)
    prof["updated_at"] = iso(utcnow())

def episode_create(uid: int, topic: str, severity: int=5, red: str="") -> str:
    eid = str(uuid.uuid4())
    row = {"episode_id": eid, "user_id": str(uid), "topic": topic, "started_at": iso(utcnow()),
           "baseline_severity": str(severity), "red_flags": red, "plan_accepted":"", "target":"",
           "reminder_at":"", "next_checkin_at":"", "status":"open", "last_update": iso(utcnow()), "notes":""}
    if SHEETS_ENABLED and ws_episodes:
        try:
            hdr = _headers(ws_episodes); _ws_ensure_columns(ws_episodes, hdr)
            ws_episodes.append_row([row.get(h,"") for h in hdr])
        except Exception as e:
            logging.warning(f"episode_create sheet fail -> mem: {e}")
    else:
        MEM_EPISODES.append(row)
    return eid

def episode_find_open(uid: int) -> Optional[dict]:
    try:
        items = []
        if SHEETS_ENABLED and ws_episodes:
            for r in ws_episodes.get_all_records():
                if str(r.get("user_id"))==str(uid) and (r.get("status") or "")=="open":
                    items.append(r)
        else:
            for r in MEM_EPISODES:
                if r.get("user_id")==str(uid) and r.get("status")=="open":
                    items.append(r)
        items.sort(key=lambda x: x.get("last_update") or "", reverse=True)
        return items[0] if items else None
    except Exception:
        return None

def episode_set(eid: str, field: str, value: str):
    if SHEETS_ENABLED and ws_episodes:
        try:
            vals = ws_episodes.get_all_records(); hdr=_headers(ws_episodes)
            if field not in hdr:
                _ws_ensure_columns(ws_episodes, hdr+[field]); hdr=_headers(ws_episodes)
            for i,r in enumerate(vals, start=2):
                if str(r.get("episode_id"))==eid:
                    ws_episodes.update_cell(i, hdr.index(field)+1, value)
                    ws_episodes.update_cell(i, hdr.index("last_update")+1, iso(utcnow()))
                    return
        except Exception as e:
            logging.warning(f"episode_set sheet fail: {e}")
    for r in MEM_EPISODES:
        if r.get("episode_id")==eid:
            r[field]=value; r["last_update"]=iso(utcnow()); break

def challenge_get(uid: int) -> Optional[dict]:
    if SHEETS_ENABLED and ws_challenges:
        try:
            for r in ws_challenges.get_all_records():
                if str(r.get("user_id"))==str(uid) and (r.get("status") or "")!="done":
                    return r
        except Exception:
            pass
    for r in MEM_CHALLENGES:
        if r.get("user_id")==str(uid) and r.get("status")!="done":
            return r
    return None

def challenge_start(uid: int, days: int=7):
    row = {"user_id":str(uid), "challenge_id":str(uuid.uuid4()), "name":"water7",
           "start_date": date.today().isoformat(), "length_days": str(days), "days_done":"0", "status":"active"}
    if SHEETS_ENABLED and ws_challenges:
        try:
            hdr=_headers(ws_challenges); _ws_ensure_columns(ws_challenges, hdr)
            ws_challenges.append_row([row.get(h,"") for h in hdr]); return
        except Exception as e:
            logging.warning(f"challenge_start sheet fail -> mem: {e}")
    MEM_CHALLENGES.append(row)

def challenge_mark_progress(uid: int, inc: int=1) -> Optional[int]:
    # увеличивает days_done, возвращает прогресс
    if SHEETS_ENABLED and ws_challenges:
        try:
            vals=ws_challenges.get_all_records(); hdr=_headers(ws_challenges)
            for i,r in enumerate(vals, start=2):
                if str(r.get("user_id"))==str(uid) and (r.get("status") or "")=="active":
                    d=max(0,int(str(r.get("days_done") or "0")))+inc
                    ws_challenges.update_cell(i, hdr.index("days_done")+1, str(d))
                    if d>=int(str(r.get("length_days") or "7")):
                        ws_challenges.update_cell(i, hdr.index("status")+1, "done")
                    return d
        except Exception as e:
            logging.warning(f"challenge_mark_progress sheet fail: {e}")
            return None
    for r in MEM_CHALLENGES:
        if r.get("user_id")==str(uid) and r.get("status")=="active":
            d=max(0,int(r.get("days_done","0")))+inc
            r["days_done"]=str(d)
            if d>=int(r.get("length_days","7")):
                r["status"]="done"
            return d
    return None

# ---------- Tips ----------
def _get_skin_tip(lang: str, sex: str, age: int) -> str:
    bank = {
        "en": [
            "SPF every day (even if cloudy).", "150–300 min/wk light movement helps skin.",
            "Protein ~1.2 g/kg supports skin repair.", "Sleep 7–9h — best free cosmetic."
        ],
        "ru": [
            "SPF каждый день (даже в пасмурно).", "150–300 мин/нед лёгкого движения — коже на пользу.",
            "Белок ~1.2 г/кг помогает восстановлению кожи.", "Сон 7–9 ч — лучший бесплатный косметолог."
        ],
        "uk": [
            "SPF щодня (навіть у хмарність).", "150–300 хв/тиж руху — користь для шкіри.",
            "Білок ~1.2 г/кг — підтримка відновлення.", "Сон 7–9 год — найкраща косметика."
        ],
        "es": [
            "SPF a diario (aunque esté nublado).", "150–300 min/sem de movimiento ayuda a la piel.",
            "Proteína ~1.2 g/kg para reparar.", "Dormir 7–9 h — la mejor cosmética."
        ],
    }
    return random.choice(bank.get(lang, bank["en"]))

def _get_daily_tip(profile: dict, lang: str) -> str:
    tips = {
        "en": ["200 ml water now", "2-min walk break", "3 deep breaths", "5 push-ups or 20s plank"],
        "ru": ["200 мл воды сейчас", "Перерыв — 2 мин ходьбы", "3 глубоких вдоха", "5 отжиманий или 20с планки"],
        "uk": ["200 мл води зараз", "Перерва — 2 хв ходи", "3 глибокі вдихи", "5 відтискань або 20с планки"],
        "es": ["200 ml de agua ahora", "Pausa: 2 min de paseo", "3 respiraciones profundas", "5 flexiones o 20s plancha"],
    }
    return random.choice(tips.get(lang, tips["en"]))

# ---------- Life metrics ----------
def life_metrics(profile: dict) -> Dict[str, int]:
    # если есть birth_date YYYY-MM-DD — точнее, иначе оценка по возрасту
    today = date.today()
    bd = (profile.get("birth_date") or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", bd):
        y,m,d = map(int, bd.split("-"))
        born = date(y,m,d)
        days = (today - born).days
        est = False
    else:
        # оценка по age
        try:
            age = int(re.search(r"\d+", str(profile.get("age") or "0")).group(0))
        except Exception:
            age = 0
        days = max(0, age*365 + random.randint(-30,30))
        est = True
    percent = min(100, round(100.0*days/36500, 1))
    return {"days_lived": days, "percent_to_100": percent, "estimate": est}

def progress_bar(percent: float, width: int=12) -> str:
    filled = max(0, min(width, round(width*percent/100)))
    return "█"*filled + "░"*(width-filled)

# ---------- Message trimmer (≤5 строк + до 4 буллетов) ----------
def _trim_text(s: str) -> str:
    lines = [x for x in s.strip().splitlines() if x.strip()]
    # берём первые 5 строк, но сохраняем маркеры списка
    kept = []
    for ln in lines:
        kept.append(ln)
        if len(kept) >= 5:
            break
    return "\n".join(kept).strip()

# ---------- Quickbar UI (если нет в Ч.1 — переопределение безопасно) ----------
def quickbar_kb(lang: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("⚡ " + (T[lang]["h60_btn"] if "h60_btn" in T[lang] else "Health in 60s"), callback_data="menu|h60")],
        [InlineKeyboardButton("🚑 " + (T[lang].get("act_er","ER")), callback_data="menu|er")],
        [InlineKeyboardButton("🧪 " + (T[lang].get("act_find_lab","Lab")), callback_data="menu|lab")],
        [InlineKeyboardButton("⏰ " + (T[lang].get("quick_rem","Reminder")), callback_data="menu|rem")],
    ]
    return InlineKeyboardMarkup(rows)

async def show_quickbar(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str):
    try:
        await context.bot.send_message(chat_id, (T[lang].get("quick_title") or "Quick actions"), reply_markup=quickbar_kb(lang))
    except Exception as e:
        logging.warning(f"show_quickbar error: {e}")

def inline_topic_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["h60_btn"], callback_data="menu|h60"),
         InlineKeyboardButton("🧑‍⚕️ " + (T[lang].get("cycle_btn") or "Cycle"), callback_data="menu|youth")],
    ])

# ---------- Reminders: scheduler ----------
async def job_oneoff_reminder(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id"); rid = d.get("reminder_id")
    try:
        await context.bot.send_message(uid, d.get("text") or "⏰")
        if rid: reminder_close(rid)
    except Exception as e:
        logging.error(f"job_oneoff_reminder: {e}")

def schedule_daily_checkin(app, uid: int, tz_off: int, hhmm: str, lang: str):
    # run_repeating раз в 24ч, первый запуск — ближайшее локальное HH:MM
    try:
        for j in app.job_queue.get_jobs_by_name(f"daily_{uid}"):
            j.schedule_removal()
    except Exception:
        pass
    now_loc = utcnow() + timedelta(hours=tz_off)
    h,m = hhmm_tuple(hhmm)
    first = now_loc.replace(hour=h, minute=m, second=0, microsecond=0)
    if first <= now_loc:
        first += timedelta(days=1)
    delay = max(5, (first - now_loc).total_seconds())
    app.job_queue.run_repeating(job_daily_checkin, interval=86400, first=delay,
                                data={"user_id":uid, "lang":lang}, name=f"daily_{uid}")

def schedule_evening_checkin(app, uid: int, tz_off: int, hhmm: str, lang: str):
    try:
        for j in app.job_queue.get_jobs_by_name(f"evening_{uid}"):
            j.schedule_removal()
    except Exception:
        pass
    now_loc = utcnow() + timedelta(hours=tz_off)
    h,m = hhmm_tuple(hhmm)
    first = now_loc.replace(hour=h, minute=m, second=0, microsecond=0)
    if first <= now_loc:
        first += timedelta(days=1)
    delay = max(5, (first - now_loc).total_seconds())
    app.job_queue.run_repeating(job_evening_checkin, interval=86400, first=delay,
                                data={"user_id":uid, "lang":lang}, name=f"evening_{uid}")

def schedule_from_sheet_on_start(app):
    # восстановить расписания для всех пользователей
    try:
        if SHEETS_ENABLED and ws_users:
            for r in ws_users.get_all_records():
                uid = int(r.get("user_id"))
                lang = norm_lang(r.get("lang") or "en")
                if (r.get("paused") or "").lower()=="yes":
                    continue
                off = int(str(r.get("tz_offset") or "0"))
                ch = r.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL
                schedule_daily_checkin(app, uid, off, ch, lang)
                eh = (r.get("evening_hour") or "").strip()
                if eh:
                    schedule_evening_checkin(app, uid, off, eh, lang)
        else:
            # память: ничего делать — нет списка всех юзеров
            pass
    except Exception as e:
        logging.warning(f"schedule_from_sheet_on_start: {e}")

# ---------- Daily/Evening jobs ----------
async def job_daily_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id"); lang = norm_lang((d.get("lang") or "en"))
    if not uid: return
    u = users_get(uid)
    if (u.get("paused") or "").lower()=="yes":
        return

    # иногда добавляем метрику жизни
    try:
        if random.random() < 0.12:
            prof = profiles_get(uid) or {}
            lm = life_metrics(prof)
            bar = progress_bar(lm["percent_to_100"])
            note = ""
            if lm.get("estimate"):
                note = "\n" + ({"ru":"(оценочно)","uk":"(орієнтовно)","es":"(estimado)","en":"(estimated)"}[lang])
            await context.bot.send_message(uid,
                f"🎯 {T[lang].get('streak_day','Day of care')}\n"
                f"Сегодня твой {lm['days_lived']}-й день жизни. {note}".strip())
            await context.bot.send_message(uid, f"{bar} {lm['percent_to_100']}% → 100 лет")
    except Exception as e:
        logging.debug(f"life metric send fail: {e}")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👍", callback_data="gm|mood|excellent"),
         InlineKeyboardButton("🙂", callback_data="gm|mood|ok"),
         InlineKeyboardButton("😐", callback_data="gm|mood|tired"),
         InlineKeyboardButton("🤕", callback_data="gm|mood|pain")],
        [InlineKeyboardButton("⏭", callback_data="gm|skip")]
    ])
    try:
        await context.bot.send_message(uid, T[lang].get("daily_gm","Good morning! Quick daily check-in:"), reply_markup=kb)
    except Exception as e:
        logging.error(f"job_daily_checkin send error: {e}")

async def job_evening_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id"); lang = norm_lang((d.get("lang") or "en"))
    if not uid: return
    u = users_get(uid)
    if (u.get("paused") or "").lower()=="yes":
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang].get("evening_tip_btn","Tip"), callback_data="youth:tip")],
        [InlineKeyboardButton("0", callback_data="num|0"),
         InlineKeyboardButton("3", callback_data="num|3"),
         InlineKeyboardButton("6", callback_data="num|6"),
         InlineKeyboardButton("8", callback_data="num|8"),
         InlineKeyboardButton("10", callback_data="num|10")]
    ])
    try:
        await context.bot.send_message(uid, T[lang].get("evening_intro","Evening check-in:"), reply_markup=kb)
    except Exception as e:
        logging.error(f"job_evening_checkin send error: {e}")

# ---------- Callback handler (дополненный) ----------
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    lang = _user_lang(uid)
    data = (q.data or "")
    update_last_seen(uid)

    # ===== Меню быстрых действий =====
    if data.startswith("menu|"):
        act = data.split("|",1)[1]
        if act == "h60":
            sessions.setdefault(uid, {})["awaiting_h60_text"] = True
            await context.bot.send_message(uid, T[lang]["h60_intro"])
            await show_quickbar(context, uid, lang)
            return
        if act == "er":
            await context.bot.send_message(uid, T[lang]["er_text"])
            await show_quickbar(context, uid, lang)
            return
        if act == "lab":
            sessions.setdefault(uid, {})["await_lab_city"] = True
            await context.bot.send_message(uid, T[lang]["act_city_prompt"])
            return
        if act == "rem":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(T[lang]["act_rem_4h"],  callback_data="qrem|4h")],
                [InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="qrem|eve")],
                [InlineKeyboardButton(T[lang]["act_rem_morn"],callback_data="qrem|morn")]
            ])
            await context.bot.send_message(uid, T[lang]["remind_when"], reply_markup=kb)
            return
        # оставшиеся ветки меню уже есть в Ч.1 (energy/hydrate/skintip/cycle/youth)
        # и будут обработаны их секциями, если они там определены
        # (ничего не делаем здесь)

    # ===== Общие напоминания (кроме Health60) =====
    if data.startswith("qrem|"):
        opt = data.split("|")[1]
        now_local = user_local_now(uid)
        if opt == "4h":
            when_local = now_local + timedelta(hours=4)
        elif opt == "eve":
            eh = users_get(uid).get("evening_hour") or DEFAULT_EVENING_LOCAL
            h,m = hhmm_tuple(eh)
            when_local = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
            if when_local <= now_local: when_local += timedelta(days=1)
        else:
            mh = users_get(uid).get("checkin_hour") or DEFAULT_CHECKIN_LOCAL
            h,m = hhmm_tuple(mh)
            when_local = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
            if when_local <= now_local: when_local += timedelta(days=1)
        adj = _schedule_oneoff_with_sheet(context, uid, when_local, T[lang]["thanks"])
        await context.bot.send_message(uid, _fmt_reminder_set(lang, adj, kind=("evening" if opt=="eve" else ("morning" if opt=="morn" else "4h"))))
        await show_quickbar(context, uid, lang)
        return

    # ===== Утренние кнопки =====
    if data.startswith("gm|"):
        _, kind, *rest = data.split("|")
        if kind == "mood":
            mood = rest[0] if rest else "ok"
            daily_add(iso(utcnow()), uid, mood=mood, comment="")
            # streak
            try:
                u = users_get(uid)
                today = date.today().isoformat()
                last = (u.get("gm_last_date") or "")
                streak = int(str(u.get("streak") or "0"))
                best   = int(str(u.get("streak_best") or "0"))
                if last == today:
                    pass
                else:
                    # если вчера отвечал — +1, иначе начать заново
                    yday = (date.today()-timedelta(days=1)).isoformat()
                    if last == yday:
                        streak += 1
                    else:
                        streak = 1
                    best = max(best, streak)
                users_set(uid, "gm_last_date", today)
                users_set(uid, "streak", str(streak))
                users_set(uid, "streak_best", str(best))
                # редкое соц-доказательство
                if streak % 3 == 0:
                    msg = {
                        "ru": "👍 3 дня подряд! Бонус-совет: попробуйте 2-минутную растяжку перед завтраком.",
                        "uk": "👍 3 дні поспіль! Бонус-порада: 2 хв розтяжки перед сніданком.",
                        "es": "👍 ¡3 días seguidos! Bonus: 2 min de estiramiento antes del desayuno.",
                        "en": "👍 3 days in a row! Bonus tip: 2-min stretch before breakfast."
                    }[lang]
                    await context.bot.send_message(uid, msg)
            except Exception as e:
                logging.debug(f"streak update fail: {e}")

            await q.edit_message_reply_markup(None)
            # маленький совет
            prof = profiles_get(uid) or {}
            tip = _get_daily_tip(prof, lang)
            await context.bot.send_message(uid, f"{T[lang].get('daily_tip_prefix','Tip')}: {tip}")
            await show_quickbar(context, uid, lang)
        elif kind == "skip":
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(uid, T[lang].get("thanks","Got it 🙌"))
            await show_quickbar(context, uid, lang)
        return

    # ===== Остальные ветки из Ч.1 (energy|, youth:, h60|, cycle| etc.) уже реализованы там =====
    # Этот обработчик дополняет их, не заменяя.

# ---------- Text handler extension ----------
async def msg_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    text = (update.message.text or "").strip()
    update_last_seen(uid)
    s = sessions.setdefault(uid, {})

    # город для лаборатории
    if s.get("await_lab_city"):
        city = text[:80]
        profiles_upsert(uid, {"city": city})
        s["await_lab_city"] = False
        await update.message.reply_text(T[lang].get("act_saved","Saved."))
        await show_quickbar(context, update.effective_chat.id, lang)
        return

    # Health60 ожидание
    if s.get("awaiting_h60_text"):
        s["awaiting_h60_text"] = False
        # логируем эпизод
        episode_create(uid, topic="h60", severity=5, red="")
        prof = profiles_get(uid) or {}
        prefix = personalized_prefix(lang, prof)
        plan = health60_make_plan(lang, text, prof)
        final = (prefix + "\n" if prefix else "") + _trim_text(plan)
        # кнопки уже есть в Ч.1 (_process_health60), но на всякий — простой ответ
        await update.message.reply_text(final)
        await show_quickbar(context, update.effective_chat.id, lang)
        return

    # остальная логика текста остаётся в Ч.1 — маршрутизация LLM и пр.
    # Чтобы не дублировать, вызовем базовый маршрутизатор из Ч.1 напрямую:
    try:
        prof = profiles_get(uid) or {}
        prefix = personalized_prefix(lang, prof)
        route = llm_router_answer(text, lang, prof)
        reply = route.get("assistant_reply") or T[lang]["unknown"]
        followups = route.get("followups") or []
        lines = [reply]
        if followups:
            lines.append("")
            lines.append("— " + "\n— ".join([f for f in followups if f.strip()][:4]))
        final = (prefix + "\n" if prefix else "") + _trim_text("\n".join(lines).strip())
        await update.message.reply_text(final)
        await show_quickbar(context, update.effective_chat.id, lang)
        return
    except Exception as e:
        logging.error(f"msg_text fallback error: {e}")
        await update.message.reply_text(T[lang]["unknown"])
        return

# ---------- Wiring (если Ч.1 не регистрировала хендлеры сообщений/колбэков) ----------
try:
    # Если main() из Ч.1 уже добавил эти хендлеры — следующий код не помешает.
    from telegram.ext import Application
    _APP_SINGLETON = None
except Exception:
    pass
# ========= END PART 2 =========
