# -*- coding: utf-8 -*-
# =========================
# TendAI — FULL CODE (Part 1/2)
# Base, Storage, i18n, Intake, Care engine, Schedules (jobs), Commands
# =========================

import os, re, json, uuid, logging, random
from datetime import datetime, timedelta, timezone, date, time as dtime
from typing import List, Dict, Optional, Set, Tuple
from difflib import SequenceMatcher

from dotenv import load_dotenv
from langdetect import detect, DetectorFactory

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
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
DEFAULT_EVENING_LOCAL  = "20:00"
DEFAULT_MIDDAY_LOCAL   = "13:00"
DEFAULT_WEEKLY_LOCAL   = "10:00"  # воскресенье
DEFAULT_QUIET_HOURS = "22:00-08:00"

AUTO_MAX_PER_DAY = 2  # защита от спама авто-уведомлений

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
    if not code: return "en"
    c = code.split("-")[0].lower()
    return c if c in SUPPORTED else "en"

T: Dict[str, Dict[str, str]] = {
    "en": {
        "welcome": "Hi! I’m TendAI — a caring health & longevity assistant.\nShort, useful, friendly. Let’s do a 40s intake to personalize.",
        "help": "Short checkups, 24–48h plans, reminders, daily check-ins.\nCommands: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /evening_on 20:00 /evening_off /settz +2 /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
        "privacy": "I’m not a medical service. Minimal data for reminders only. Use /delete_data to erase.",
        "quick_title": "Quick actions",
        "quick_h60": "⚡ Health in 60s",
        "quick_er": "🚑 Emergency info",
        "quick_lab": "🧪 Lab",
        "quick_rem": "⏰ Reminder",
        "profile_intro": "Quick intake (~40s). Use buttons or type.",
        "write": "✍️ Write", "skip": "⏭️ Skip",
        "start_where": "Where do you want to start? (symptom/sleep/nutrition/labs/habits/longevity)",
        "saved_profile": "Saved: ",
        "daily_gm": "Good morning! 🌞 How do you feel today?",
        "gm_excellent": "👍 Excellent", "gm_ok": "🙂 Okay", "gm_tired": "😐 Tired", "gm_pain": "🤕 In pain", "gm_skip": "⏭️ Skip today",
        "mood_note": "✍️ Comment", "mood_thanks": "Thanks! Have a smooth day 👋",
        "h60_btn": "Health in 60 seconds",
        "h60_intro": "Briefly write what’s bothering you (e.g., “headache”, “fatigue”). I’ll give 3 key tips in 60s.",
        "h60_t1": "Possible causes", "h60_t2": "Do now (next 24–48h)", "h60_t3": "When to see a doctor", "h60_serious":"Serious to rule out",
        "ask_fb":"Was this helpful?","fb_good":"👍 Like","fb_bad":"👎 Dislike","fb_free":"📝 Feedback","fb_write":"Write a short feedback:","fb_thanks":"Thanks for your feedback! ✅",
        "youth_pack":"Youth Pack","gm_energy":"⚡ Energy","gm_energy_q":"Your energy (1–5)?","gm_energy_done":"Logged energy — thanks!","gm_evening_btn":"⏰ Remind this evening",
        "hydrate_btn":"💧 Hydration","hydrate_nudge":"💧 Time for a glass of water","skintip_btn":"🧴 Skin/Body tip","skintip_sent":"Tip sent.","daily_tip_prefix":"🍎 Daily tip:",
        "challenge_btn":"🎯 7-day hydration challenge","challenge_started":"Challenge started! I’ll track your daily check-ins.","challenge_progress":"Challenge progress: {d}/{len} days.",
        "cycle_btn":"🩸 Cycle","cycle_consent":"Track your cycle for gentle timing tips?","cycle_ask_last":"Enter last period date (YYYY-MM-DD):","cycle_ask_len":"Average cycle length (e.g., 28):","cycle_saved":"Cycle tracking saved.",
        "quiet_saved":"Quiet hours saved: {qh}", "set_quiet_btn":"🌙 Quiet hours", "ask_quiet":"Type quiet hours as HH:MM-HH:MM (local), e.g. 22:00-08:00",
        "evening_intro":"Evening check-in:","evening_tip_btn":"🪄 Tip of the day","evening_set":"Evening check-in set to {t} (local).","evening_off":"Evening check-in disabled.",
        "ask_consent":"May I send a follow-up later to check how you feel?","yes":"Yes","no":"No",
        "unknown":"I need a bit more info: where exactly and how long?","thanks":"Got it 🙌","back":"◀ Back","exit":"Exit",
        "paused_on":"Notifications paused. Use /resume to enable.","paused_off":"Notifications resumed.","deleted":"All your data was deleted. Use /start to begin again.",
        "life_today":"Today is your {n}-th day of life 🎉. Target — 36,500 (100y).","life_percent":"You’ve already passed {p}% toward 100 years.","life_estimate":"(estimated by age, set birth_date for accuracy)",
        "px":"Considering your profile: {sex}, {age}y; goal — {goal}.",
        "act_rem_4h":"⏰ Remind in 4h","act_rem_eve":"⏰ This evening","act_rem_morn":"⏰ Tomorrow morning","act_save_episode":"💾 Save episode","act_ex_neck":"🧘 5-min neck routine","act_er":"🚑 Emergency info",
    },
    "ru": {
        "welcome": "Привет! Я TendAI — заботливый ассистент здоровья и долголетия.\nКоротко, полезно и по-дружески. Давайте мини-опрос (~40с) для персонализации.",
        "help": "Короткие проверки, план на 24–48 ч, напоминания, ежедневные чек-ины.\nКоманды: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /evening_on 20:00 /evening_off /settz +3 /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
        "privacy": "Я не заменяю врача. Данные — только для напоминаний. /delete_data — удалить.",
        "quick_title":"Быстрые действия","quick_h60":"⚡ Здоровье за 60 сек","quick_er":"🚑 Срочно в скорую","quick_lab":"🧪 Лаборатория","quick_rem":"⏰ Напоминание",
        "profile_intro":"Быстрый опрос (~40с). Можно кнопками или текстом.","write":"✍️ Написать","skip":"⏭️ Пропустить",
        "start_where":"С чего начнём? (симптом/сон/питание/анализы/привычки/долголетие)","saved_profile":"Сохранил: ",
        "daily_gm":"Доброе утро! 🌞 Как сегодня самочувствие?","gm_excellent":"👍 Отлично","gm_ok":"🙂 Нормально","gm_tired":"😐 Устал","gm_pain":"🤕 Болит","gm_skip":"⏭️ Пропустить",
        "mood_note":"✍️ Комментарий","mood_thanks":"Спасибо! Хорошего дня 👋",
        "h60_btn":"Здоровье за 60 секунд","h60_intro":"Коротко напишите, что беспокоит (например: «болит голова», «усталость»). Дам 3 ключевых совета за 60 сек.",
        "h60_t1":"Возможные причины","h60_t2":"Что сделать сейчас (24–48 ч)","h60_t3":"Когда обратиться к врачу","h60_serious":"Что серьёзное исключить",
        "ask_fb":"Это было полезно?","fb_good":"👍 Нравится","fb_bad":"👎 Не полезно","fb_free":"📝 Отзыв","fb_write":"Напишите короткий отзыв:","fb_thanks":"Спасибо за отзыв! ✅",
        "youth_pack":"Молодёжный пакет","gm_energy":"⚡ Энергия","gm_energy_q":"Как энергия (1–5)?","gm_energy_done":"Записал энергию — спасибо!","gm_evening_btn":"⏰ Напомнить вечером",
        "hydrate_btn":"💧 Гидратация","hydrate_nudge":"💧 Время для стакана воды","skintip_btn":"🧴 Советы для кожи/тела","skintip_sent":"Совет отправлен.","daily_tip_prefix":"🍎 Подсказка дня:",
        "challenge_btn":"🎯 Челлендж 7 дней (вода)","challenge_started":"Челлендж начат!","challenge_progress":"Прогресс: {d}/{len} дней.",
        "cycle_btn":"🩸 Цикл","cycle_consent":"Отслеживать цикл и давать мягкие подсказки?","cycle_ask_last":"Введите дату последних месячных (ГГГГ-ММ-ДД):","cycle_ask_len":"Средняя длина цикла (например, 28):","cycle_saved":"Отслеживание цикла сохранено.",
        "quiet_saved":"Тихие часы сохранены: {qh}","set_quiet_btn":"🌙 Тихие часы","ask_quiet":"Введите ЧЧ:ММ-ЧЧ:ММ (локально), напр. 22:00-08:00",
        "evening_intro":"Вечерний чек-ин:","evening_tip_btn":"🪄 Совет дня","evening_set":"Вечерний чек-ин на {t} (локально).","evening_off":"Вечерний чек-ин отключён.",
        "ask_consent":"Можно прислать напоминание позже, чтобы узнать, как вы?","yes":"Да","no":"Нет",
        "unknown":"Нужно чуть больше деталей: где именно и как долго?","thanks":"Принято 🙌","back":"◀ Назад","exit":"Выйти",
        "paused_on":"Напоминания поставлены на паузу. /resume — включить.","paused_off":"Напоминания снова включены.","deleted":"Все данные удалены. /start — начать заново.",
        "life_today":"Сегодня твой {n}-й день жизни 🎉. Цель — 36 500 (100 лет).","life_percent":"Ты прошёл уже {p}% пути к 100 годам.","life_estimate":"(оценочно по возрасту — укажи birth_date для точности)",
        "px":"С учётом профиля: {sex}, {age} лет; цель — {goal}.",
        "act_rem_4h":"⏰ Напомнить через 4 ч","act_rem_eve":"⏰ Сегодня вечером","act_rem_morn":"⏰ Завтра утром","act_save_episode":"💾 Сохранить эпизод","act_ex_neck":"🧘 5-мин упражнения для шеи","act_er":"🚑 Когда срочно в скорую",
    },
    "uk": {
        "welcome":"Привіт! Я TendAI — турботливий асистент здоров’я.\nЗробімо короткий опитник (~40с) для персоналізації.",
        "help":"Короткі перевірки, план на 24–48 год, нагадування, щоденні чек-іни.\nКоманди: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /evening_on 20:00 /evening_off /settz +2 /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
        "privacy":"Я не замінюю лікаря. Мінімальні дані лише для нагадувань. /delete_data — видалити.",
        "quick_title":"Швидкі дії","quick_h60":"⚡ Здоров’я за 60 с","quick_er":"🚑 Невідкладно","quick_lab":"🧪 Лабораторія","quick_rem":"⏰ Нагадування",
        "profile_intro":"Швидкий опитник (~40с).","write":"✍️ Написати","skip":"⏭️ Пропустити",
        "start_where":"З чого почнемо? (симптом/сон/харчування/аналізи/звички/довголіття)","saved_profile":"Зберіг: ",
        "daily_gm":"Доброго ранку! 🌞 Як самопочуття сьогодні?","gm_excellent":"👍 Чудово","gm_ok":"🙂 Нормально","gm_tired":"😐 Втома","gm_pain":"🤕 Болить","gm_skip":"⏭️ Пропустити",
        "mood_note":"✍️ Коментар","mood_thanks":"Дякую! Гарного дня 👋",
        "h60_btn":"Здоров’я за 60 секунд","h60_intro":"Коротко напишіть, що турбує. Дам 3 поради за 60 с.",
        "h60_t1":"Можливі причини","h60_t2":"Що зробити зараз (24–48 год)","h60_t3":"Коли звернутись до лікаря","h60_serious":"Що серйозне виключити",
        "ask_fb":"Це було корисно?","fb_good":"👍 Подобається","fb_bad":"👎 Не корисно","fb_free":"📝 Відгук","fb_write":"Напишіть короткий відгук:","fb_thanks":"Дякую! ✅",
        "youth_pack":"Молодіжний пакет","gm_energy":"⚡ Енергія","gm_energy_q":"Енергія (1–5)?","gm_energy_done":"Занотував енергію — дякую!","gm_evening_btn":"⏰ Нагадати ввечері",
        "hydrate_btn":"💧 Гідратація","hydrate_nudge":"💧 Час для склянки води","skintip_btn":"🧴 Порада для шкіри/тіла","skintip_sent":"Надіслано.","daily_tip_prefix":"🍎 Підказка дня:",
        "challenge_btn":"🎯 Челендж 7 днів (вода)","challenge_started":"Челендж запущено!","challenge_progress":"Прогрес: {d}/{len} днів.",
        "cycle_btn":"🩸 Цикл","cycle_consent":"Відстежувати цикл і м’які поради?","cycle_ask_last":"Дата останніх менструацій (РРРР-ММ-ДД):","cycle_ask_len":"Середня довжина циклу (напр., 28):","cycle_saved":"Збережено.",
        "quiet_saved":"Тихі години збережено: {qh}","set_quiet_btn":"🌙 Тихі години","ask_quiet":"Введіть ГГ:ХХ-ГГ:ХХ (локально), напр. 22:00-08:00",
        "evening_intro":"Вечірній чек-ін:","evening_tip_btn":"🪄 Порада дня","evening_set":"Вечірній чек-ін на {t} (локально).","evening_off":"Вимкнено.",
        "ask_consent":"Можу нагадати пізніше, щоб дізнатись як ви?","yes":"Так","no":"Ні",
        "unknown":"Потрібно трохи більше деталей: де саме і як довго?","thanks":"Прийнято 🙌","back":"◀ Назад","exit":"Вийти",
        "paused_on":"Нагадування призупинені. /resume — увімкнути.","paused_off":"Знову увімкнено.","deleted":"Усі дані видалено. /start — почати знову.",
        "life_today":"Сьогодні твій {n}-й день життя 🎉. Мета — 36 500 (100 років).","life_percent":"Ти пройшов {p}% шляху до 100 років.","life_estimate":"(орієнтовно за віком — вкажи birth_date для точності)",
        "px":"З урахуванням профілю: {sex}, {age} р.; мета — {goal}.",
        "act_rem_4h":"⏰ Через 4 год","act_rem_eve":"⏰ Сьогодні ввечері","act_rem_morn":"⏰ Завтра зранку","act_save_episode":"💾 Зберегти епізод","act_ex_neck":"🧘 5-хв для шиї","act_er":"🚑 Коли терміново",
    },
}
T["es"] = T["en"]

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
def utcnow() -> datetime: return datetime.now(timezone.utc)
def iso(dt: Optional[datetime]) -> str:
    return "" if not dt else dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")

def detect_lang_from_text(text: str, fallback: str) -> str:
    s = (text or "").strip()
    if not s: return fallback
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
    text = ("Чтобы советы были точнее, пройдите короткий опрос. Можно пропустить."
            if lang!="en" else
            "To personalize answers, please take a short intake. You can skip.")
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
        "es":[("Ninguno","none"),("Cardio","cvd"),("Tiroides","endocrine"),("GI","gi"),("Alergia","allergy"),("Otro","other")],
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
        # первичный AI профиль
        profiles_upsert(uid, {"ai_profile": json.dumps({"v":1,"habits":answers.get("habits","")}, ensure_ascii=False)})
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
    try: return ws.row_values(1) if ws else []
    except Exception: return []

def _ws_ensure_columns(ws, desired_headers: List[str]):
    try:
        current = _ws_headers(ws)
        if not current:
            if ws.col_count < len(desired_headers):
                ws.add_cols(len(desired_headers) - ws.col_count)
            ws.append_row(desired_headers); return
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
        if not creds_json: raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")
        creds = json.loads(creds_json)
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds, scope)
        gclient = gspread.authorize(credentials)
        GSPREAD_CLIENT = gclient

        try:
            ss = gclient.open_by_key(SHEET_ID) if SHEET_ID else gclient.open(SHEET_NAME)
        except SpreadsheetNotFound:
            if ALLOW_CREATE_SHEET: ss = gclient.create(SHEET_NAME)
            else: raise

        SPREADSHEET_ID_FOR_INTAKE = getattr(ss, "id", SHEET_ID or "")

        def _ensure_ws(title: str, headers: List[str]):
            try:
                ws = ss.worksheet(title)
            except WorksheetNotFound:
                ws = ss.add_worksheet(title=title, rows=4000, cols=max(60, len(headers)))
                ws.append_row(headers)
            if not ws.get_all_values(): ws.append_row(headers)
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
        "user_id": str(uid), "username": username or "", "lang": lang, "consent": "no",
        "tz_offset": "0", "checkin_hour": DEFAULT_CHECKIN_LOCAL, "evening_hour": "",
        "paused": "no", "last_seen": iso(utcnow()), "last_auto_date": "", "last_auto_count": "0",
        "streak": "0","streak_best":"0","gm_last_date": "",
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
            ws_users.append_row([base.get(h, "") for h in hdr]); return
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
                        _ws_ensure_columns(ws_users, hdr + [field]); hdr = _headers(ws_users)
                    ws_users.update_cell(i, hdr.index(field)+1, value); return
        except Exception as e:
            logging.warning(f"users_set -> memory fallback: {e}")
    u = MEM_USERS.setdefault(uid, {}); u[field] = value

def profiles_get(uid: int) -> dict:
    if SHEETS_ENABLED:
        try:
            for r in ws_profiles.get_all_records():
                if str(r.get("user_id")) == str(uid): return r
        except Exception as e:
            logging.warning(f"profiles_get fallback: {e}")
    return MEM_PROFILES.get(uid, {})

def profiles_upsert(uid: int, patch: dict):
    patch = dict(patch or {}); patch["user_id"] = str(uid); patch["updated_at"] = iso(utcnow())
    if SHEETS_ENABLED:
        try:
            vals = ws_profiles.get_all_records(); hdr = _headers(ws_profiles)
            if hdr:
                for k in patch.keys():
                    if k not in hdr:
                        _ws_ensure_columns(ws_profiles, hdr + [k]); hdr = _headers(ws_profiles)
            for i, r in enumerate(vals, start=2):
                if str(r.get("user_id")) == str(uid):
                    merged = {**r, **patch}
                    ws_profiles.update(f"A{i}:{gsu.rowcol_to_a1(i, len(hdr))}", [[merged.get(h, "") for h in hdr]]); return
            ws_profiles.append_row([patch.get(h, "") for h in hdr]); return
        except Exception as e:
            logging.warning(f"profiles_upsert -> memory fallback: {e}")
    MEM_PROFILES[uid] = {**MEM_PROFILES.get(uid, {}), **patch}

def feedback_add(ts: str, uid: int, name: str, username: str, rating: str, comment: str):
    row = {"timestamp":ts, "user_id":str(uid), "name":name, "username":username, "rating":rating, "comment":comment}
    if SHEETS_ENABLED:
        try:
            hdr = _headers(ws_feedback); ws_feedback.append_row([row.get(h, "") for h in hdr]); return
        except Exception as e:
            logging.warning(f"feedback_add -> memory fallback: {e}")
    MEM_FEEDBACK.append(row)

def daily_add(ts: str, uid: int, mood: str="", comment: str="", energy: Optional[int]=None):
    row = {"timestamp":ts, "user_id":str(uid), "mood":mood, "energy":("" if energy is None else str(energy)), "comment":comment}
    if SHEETS_ENABLED:
        try:
            hdr = _headers(ws_daily); ws_daily.append_row([row.get(h, "") for h in hdr]); return
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
            hdr = _headers(ws_episodes); ws_episodes.append_row([row.get(h, "") for h in hdr])
        except Exception as e:
            logging.warning(f"episode_create -> memory fallback: {e}"); MEM_EPISODES.append(row)
    else: MEM_EPISODES.append(row)
    return eid

def episode_set(eid: str, key: str, val: str):
    if SHEETS_ENABLED:
        try:
            vals = ws_episodes.get_all_records(); hdr = _headers(ws_episodes)
            for i, r in enumerate(vals, start=2):
                if r.get("episode_id") == eid:
                    if key not in hdr: _ws_ensure_columns(ws_episodes, hdr + [key]); hdr=_headers(ws_episodes)
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
            hdr = _headers(ws_reminders); ws_reminders.append_row([row.get(h, "") for h in hdr]); return rid
        except Exception as e:
            logging.warning(f"reminder_add -> memory fallback: {e}")
    MEM_REMINDERS.append(row); return rid

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
            hdr = _headers(ws_challenges); ws_challenges.append_row([row.get(h,"") for h in hdr]); return
        except Exception as e:
            logging.warning(f"challenge_start -> memory fallback: {e}")
    MEM_CHALLENGES.append(row)


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


# ---------- Time & Quiet hours ----------
def _user_tz_off(uid: int) -> int:
    try: return int((users_get(uid) or {}).get("tz_offset") or "0")
    except: return 0

def user_local_now(uid: int) -> datetime:
    return datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(hours=_user_tz_off(uid))

def hhmm_tuple(hhmm: str) -> Tuple[int,int]:
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

def _has_jq_ctx(context: ContextTypes.DEFAULT_TYPE) -> bool:
    try: return bool(context.application.job_queue)
    except Exception: return False


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
        try: a = int(re.search(r"\d+", str((profile or {}).get("age","") or "0")).group(0))
        except Exception: a = 0
        days = max(0, a*365)
    percent = min(100, round(days/36500*100, 1))
    return {"days_lived": days, "percent_to_100": percent}

def progress_bar(percent: float, width: int=12) -> str:
    fill = int(round(width*percent/100.0))
    return "█"*fill + "░"*(width-fill)


# ------------- LLM Router (concise) -------------
SYS_ROUTER = (
    "You are TendAI — a concise, warm, professional health & longevity assistant (not a doctor). "
    "Always answer strictly in {lang}. Keep replies short (<=6 lines + up to 4 bullets). "
    "Personalize using the profile (sex/age/goal/conditions, diet_focus, steps_target). "
    "TRIAGE: ask 1–2 clarifiers first; recommend ER only for clear red flags. "
    "Return JSON ONLY: {\"intent\":\"symptom|nutrition|sleep|labs|habits|longevity|other\","
    "\"assistant_reply\":\"...\",\"followups\":[\"...\"],\"needs_more\":true,\"red_flags\":false,\"confidence\":0.0}"
)

def llm_router_answer(text: str, lang: str, profile: dict) -> dict:
    if not oai:
        # базовый фолбек-уточнение
        clar = {
            "ru":"Где именно болит и сколько длится? Есть температура/травма?",
            "uk":"Де саме болить і скільки триває? Є температура/травма?",
            "en":"Where exactly is the pain and for how long? Any fever/trauma?",
            "es":"¿Dónde exactamente y desde cuándo? ¿Fiebre/trauma?",
        }[lang]
        return {"intent":"other","assistant_reply":clar,"followups":[clar],"needs_more":True,"red_flags":False,"confidence":0.3}
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
        clar = {
            "ru":"Где именно и как давно? Есть ли покраснение, температура, травма?",
            "uk":"Де саме і як давно? Є почервоніння/температура/травма?",
            "en":"Where exactly and since when? Any redness, fever or injury?",
            "es":"¿Dónde exactamente y desde cuándo? ¿Enrojecimiento, fiebre o lesión?",
        }[lang]
        return {"intent":"other","assistant_reply":clar,"followups":[clar],"needs_more":True,"red_flags":False,"confidence":0.3}


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
    if not oai: return fallback
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
        causes  = _fmt_bullets(data.get("causes") or [])
        serious = (data.get("serious") or "").strip()
        do_now  = _fmt_bullets(data.get("do_now") or [])
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


# ---------- Lightweight content helpers ----------
def _get_skin_tip(lang: str, sex: str, age: int) -> str:
    pool_ru = [
        "Мягкое SPF каждый день — самая недооценённая инвестиция в кожу.",
        "Душ: тёплая вода, не горячая; 3–5 минут — кожа скажет спасибо.",
        "Умывалка без SLS, увлажняющий крем сразу после воды."
    ]
    pool_en = [
        "Daily light SPF is the most underrated skin investment.",
        "Keep showers warm, not hot; 3–5 minutes protects your skin barrier.",
        "Use a gentle cleanser and moisturize right after water."
    ]
    pools = {"ru": pool_ru, "uk": pool_ru, "en": pool_en, "es": pool_en}
    return random.choice(pools.get(lang, pool_en))

def _get_daily_tip(profile: dict, lang: str) -> str:
    base = {
        "ru": ["1 минуту дыхания 4-6 — и пульс спокойнее.", "Стакан воды рядом — глоток каждый раз, как разблокируешь телефон."],
        "uk": ["1 хвилина дихання 4-6 — пульс спокійніший.", "Склянка води поруч — ковток щоразу як розблоковуєш телефон."],
        "en": ["Try 1 minute of 4-6 breathing — heart rate calms down.", "Keep a glass of water nearby — sip when you unlock your phone."]
    }
    return random.choice(base.get(lang, base["en"]))


# ---------- CARE engine (habits memory & nudges) ----------
def _ai_obj(profile: dict) -> dict:
    try:
        raw = profile.get("ai_profile") or "{}"
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return {}

def _ai_get(profile: dict, key: str, default=None):
    return _ai_obj(profile).get(key, default)

def _ai_set(uid: int, key: str, value):
    p = profiles_get(uid) or {}
    obj = _ai_obj(p); obj[key] = value
    profiles_upsert(uid, {"ai_profile": json.dumps(obj, ensure_ascii=False)})

def learn_from_text(uid: int, text: str):
    """очень лёгкая эвристика — наращиваем счётчики привычек, триггеры"""
    low = (text or "").lower()
    p = profiles_get(uid) or {}
    obj = _ai_obj(p)
    def inc(k, dv=1):
        obj[k] = int(obj.get(k, 0)) + dv
    # простые триггеры
    if any(w in low for w in ["кофе","coffee","espresso","капучино","latte"]): inc("caf_mentions")
    if any(w in low for w in ["голова","headache","мигрень","migraine"]): inc("headache_mentions")
    if any(w in low for w in ["сон","sleep","insomnia","бессон"]): inc("sleep_mentions")
    if any(w in low for w in ["вода","hydrate","жажда","water"]): inc("water_prompt")
    profiles_upsert(uid, {"ai_profile": json.dumps(obj, ensure_ascii=False)})

def life_stage_from_profile(profile: dict) -> str:
    try:
        a = int(re.search(r"\d+", str(profile.get("age","") or "0")).group(0))
    except Exception:
        a = 0
    if a <= 25: return "20s"
    if a <= 40: return "30s"
    if a <= 55: return "40s-50s"
    return "60+"

def build_care_nudge(uid: int, lang: str) -> str:
    p = profiles_get(uid) or {}
    stage = life_stage_from_profile(p)
    ai = _ai_obj(p)
    # приоритеты
    if int(ai.get("headache_mentions",0)) >= 2:
        return {"ru":"Заметь триггеры головной боли: кофеин, экран, недосып — сегодня попробуй 10-мин прогулку без телефона.",
                "uk":"Зверни увагу на тригери головного болю: кофеїн, екран, недосип — сьогодні 10-хв прогулянка без телефону.",
                "en":"Track headache triggers: caffeine, screens, poor sleep — try a 10-min walk phone-free today.",
                "es":"Detecta desencadenantes de dolor de cabeza: cafeína, pantallas, sueño — camina 10 min sin móvil."}[lang]
    if stage=="20s":
        return {"ru":"⚡ Мини-челлендж: 2 стакана воды до полудня — отметь реакцию энергии.",
                "uk":"⚡ Міні-челендж: 2 склянки води до полудня — відміть енергію.",
                "en":"⚡ Mini-challenge: 2 glasses of water before noon — notice energy boost.",
                "es":"⚡ Mini-reto: 2 vasos de agua antes del mediodía — nota la energía."}[lang]
    # дефолтная подсказка
    return f"{T[lang]['daily_tip_prefix']} {_get_daily_tip(p, lang)}"


# ---------- Auto-message limiter ----------
def _auto_limit_ok(uid: int) -> bool:
    u = users_get(uid) or {}
    today = datetime.utcnow().date().isoformat()
    cnt = int(str(u.get("last_auto_count") or "0"))
    d   = (u.get("last_auto_date") or "")
    return (d != today) or (cnt < AUTO_MAX_PER_DAY)

def _auto_inc(uid: int):
    u = users_get(uid) or {}
    today = datetime.utcnow().date().isoformat()
    if (u.get("last_auto_date") or "") != today:
        users_set(uid, "last_auto_date", today); users_set(uid, "last_auto_count", "1"); return
    try: cnt = int(str(u.get("last_auto_count") or "0")) + 1
    except: cnt = 1
    users_set(uid, "last_auto_count", str(cnt))


# ---------- Scheduling ----------
def schedule_from_sheet_on_start(app: Application):  # placeholder if нужно
    logging.info("schedule_from_sheet_on_start: ok")

def _schedule_at_local(app: Application, uid: int, hhmm: str, data: dict, func, days: Optional[List[int]]=None):
    """days=None => daily; days=[6] => weekly on Sunday (0=Mon)"""
    try:
        off = _user_tz_off(uid)
        h,m = hhmm_tuple(hhmm)
        # Run daily or weekly
        if days is None:
            app.job_queue.run_daily(func, time=dtime(hour=h, minute=m), name=f"daily-{func.__name__}-{uid}",
                                    data=data)
        else:
            for d in days:
                app.job_queue.run_daily(func, time=dtime(hour=h, minute=m), days=(d,),
                                        name=f"weekly-{func.__name__}-{uid}-{d}", data=data)
    except Exception as e:
        logging.warning(f"_schedule_at_local error: {e}")

def schedule_daily_checkin(app: Application, uid: int, tz_off: int, hhmm: str, lang: str):
    _schedule_at_local(app, uid, hhmm, {"t":"gm","user_id":uid}, job_scheduled_ping)

def schedule_evening_checkin(app: Application, uid: int, tz_off: int, hhmm: str, lang: str):
    _schedule_at_local(app, uid, hhmm, {"t":"eve","user_id":uid}, job_scheduled_ping)

def schedule_midday_care(app: Application, uid: int, tz_off: int, hhmm: str=DEFAULT_MIDDAY_LOCAL, lang: str="en"):
    _schedule_at_local(app, uid, hhmm, {"t":"care","user_id":uid}, job_daily_care)

def schedule_weekly_report(app: Application, uid: int, tz_off: int, hhmm: str=DEFAULT_WEEKLY_LOCAL, weekday: int=6, lang: str="en"):
    _schedule_at_local(app, uid, hhmm, {"t":"weekly","user_id":uid}, job_weekly_report, days=[weekday])

async def job_oneoff_reminder(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}; uid = d.get("user_id")
    try:
        await context.bot.send_message(uid, T[_user_lang(uid)]["thanks"])
    except Exception: pass

async def job_scheduled_ping(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}; uid = d.get("user_id"); t = d.get("t")
    lang = _user_lang(uid)
    if not _auto_limit_ok(uid): return
    if t=="gm":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(T[lang]["gm_excellent"], callback_data="gm|mood|excellent"),
             InlineKeyboardButton(T[lang]["gm_ok"],        callback_data="gm|mood|ok")],
            [InlineKeyboardButton(T[lang]["gm_tired"],     callback_data="gm|mood|tired"),
             InlineKeyboardButton(T[lang]["gm_pain"],      callback_data="gm|mood|pain")],
            [InlineKeyboardButton(T[lang]["gm_skip"],      callback_data="gm|skip")]
        ])
        await context.bot.send_message(uid, T[lang]["daily_gm"], reply_markup=kb)
        _auto_inc(uid)
    elif t=="eve":
        await context.bot.send_message(uid, T[lang]["evening_intro"])
        _auto_inc(uid)

async def job_daily_care(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}; uid = d.get("user_id")
    lang = _user_lang(uid)
    if not _auto_limit_ok(uid): return
    tip = build_care_nudge(uid, lang)
    await context.bot.send_message(uid, tip)
    _auto_inc(uid)

async def job_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}; uid = d.get("user_id")
    lang = _user_lang(uid)
    # краткая сводка по неделе из памяти (MEM_DAILY)
    last7 = [r for r in MEM_DAILY if r.get("user_id")==str(uid)][-7:]
    good = sum(1 for r in last7 if r.get("mood") in {"excellent","ok"})
    txt = {
        "ru": f"Итоги недели: {len(last7)} чек-инов, {good} — ок/отлично. Продолжим мягко 😊",
        "uk": f"Підсумки тижня: {len(last7)} чек-інів, {good} — ок/чудово. Продовжимо м’яко 😊",
        "en": f"Weekly wrap: {len(last7)} check-ins, {good} felt ok/great. Keeping it gentle 😊",
        "es": f"Semana: {len(last7)} check-ins, {good} ok/genial. Suave y constante 😊",
    }[lang]
    await context.bot.send_message(uid, txt)


# ------------- Commands (UI only; behavior completed in Part 2: on_text/callbacks) -------------
async def post_init(app: Application):
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
    # авто-детект языка по первому слову пользователя (если есть), иначе по Telegram
    det = detect_lang_from_text((update.message.text or ""), getattr(user, "language_code", None) or "en")
    lang = norm_lang(det)
    users_upsert(user.id, user.username or "", lang)
    context.user_data["lang"] = lang
    sessions.setdefault(user.id, {})["last_user_text"] = "/start"
    update_last_seen(user.id)

    await update.message.reply_text(T[lang]["welcome"], reply_markup=ReplyKeyboardRemove())

    prof = profiles_get(user.id) or {}
    if profile_is_incomplete(prof):
        await start_mini_intake(context, update.effective_chat.id, lang, user.id)
    else:
        await update.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
        await show_quickbar(context, update.effective_chat.id, lang)

    u = users_get(user.id)
    if (u.get("consent") or "").lower() not in {"yes","no"}:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["yes"], callback_data="consent|yes"),
                                    InlineKeyboardButton(T[lang]["no"],  callback_data="consent|no")]])
        await update.message.reply_text(T[lang]["ask_consent"], reply_markup=kb)

    tz_off = int(str(u.get("tz_offset") or "0"))
    hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, user.id, tz_off, hhmm, lang)
        eh = (u.get("evening_hour") or DEFAULT_EVENING_LOCAL).strip()
        schedule_evening_checkin(context.application, user.id, tz_off, eh, lang)
        schedule_midday_care(context.application, user.id, tz_off, DEFAULT_MIDDAY_LOCAL, lang)
        schedule_weekly_report(context.application, user.id, tz_off, DEFAULT_WEEKLY_LOCAL, 6, lang)

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

# Timezone & check-ins
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
            eh = (u.get("evening_hour") or DEFAULT_EVENING_LOCAL).strip()
            schedule_evening_checkin(context.application, uid, off, eh, lang)
            schedule_midday_care(context.application, uid, off, DEFAULT_MIDDAY_LOCAL, lang)
            schedule_weekly_report(context.application, uid, off, DEFAULT_WEEKLY_LOCAL, 6, lang)
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
        schedule_daily_checkin(context.application, uid, _user_tz_off(uid), hhmm, lang)
    await update.message.reply_text(f"Daily check-in set to {hhmm} (local).")

async def cmd_checkin_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "yes")
    await update.message.reply_text("Daily check-in disabled.")

async def cmd_evening_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    txt = (update.message.text or "").strip()
    m = re.search(r"([01]?\d|2[0-3]):([0-5]\d)", txt)
    hhmm = m.group(0) if m else DEFAULT_EVENING_LOCAL
    users_set(uid, "evening_hour", hhmm)
    if _has_jq_ctx(context):
        schedule_evening_checkin(context.application, uid, _user_tz_off(uid), hhmm, lang)
    await update.message.reply_text(T[lang]["evening_set"].format(t=hhmm))

async def cmd_evening_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    users_set(uid, "evening_hour", "")
    await update.message.reply_text(T[lang]["evening_off"])

# Lang switches
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

# Youth / Health60 / Hydration / Skin / Cycle shells
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

async def cmd_hydrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    await update.message.reply_text(T[lang]["hydrate_nudge"])
    daily_add(iso(utcnow()), uid, comment="hydrate_button")
    await show_quickbar(context, update.effective_chat.id, lang)

async def cmd_skintip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    p = profiles_get(uid) or {}
    age = 0
    try: age = int(re.search(r"\d+", str(p.get("age","") or "0")).group(0))
    except: pass
    tip = _get_skin_tip(lang, (p.get("sex") or ""), age)
    await update.message.reply_text(tip)

async def cmd_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["yes"], callback_data="cycle|yes"),
                                InlineKeyboardButton(T[lang]["no"],  callback_data="cycle|no")]])
    await update.message.reply_text(T[lang]["cycle_consent"], reply_markup=kb)

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    p = profiles_get(uid) or {}
    pref = personalized_prefix(lang, p)
    lm  = life_metrics(p)
    bar = progress_bar(lm["percent_to_100"])
    txt = {
        "ru": f"{pref}\n{T['ru']['life_today'].format(n=lm['days_lived'])}\n{T['ru']['life_percent'].format(p=lm['percent_to_100'])}\n{bar} {lm['percent_to_100']}%",
        "uk": f"{pref}\n{T['uk']['life_today'].format(n=lm['days_lived'])}\n{T['uk']['life_percent'].format(p=lm['percent_to_100'])}\n{bar} {lm['percent_to_100']}%",
        "en": f"{pref}\n{T['en']['life_today'].format(n=lm['days_lived'])}\n{T['en']['life_percent'].format(p=lm['percent_to_100'])}\n{bar} {lm['percent_to_100']}%",
        "es": f"{pref}\n{T['en']['life_today'].format(n=lm['days_lived'])}\n{T['en']['life_percent'].format(p=lm['percent_to_100'])}\n{bar} {lm['percent_to_100']}%",
    }[lang]
    await update.message.reply_text(txt)
    # ---------- Rules (evidence-based) ----------
def rules_lookup(topic: str, segment: str, lang: str) -> Optional[str]:
    """Ищем готовый совет из листа Rules (доказательная база)."""
    try:
        if SHEETS_ENABLED and ws_rules:
            rows = ws_rules.get_all_records()
            for r in rows:
                if (r.get("enabled","").strip().lower() in {"1","true","yes"}) and \
                   (r.get("topic","").strip().lower() == (topic or "").strip().lower()):
                    seg = (r.get("segment") or "").strip().lower()
                    if not seg or seg == (segment or "").strip().lower():
                        txt = (r.get("advice_text") or "").strip()
                        if not txt:
                            continue
                        # Язык: если в тексте есть маркеры `[[ru:...]]`, `[[en:...]]`
                        m = re.search(r"\[\[\s*"+re.escape(lang)+r"\s*:(.*?)\]\]", txt, re.DOTALL)
                        if m:
                            return m.group(1).strip()
                        return txt
    except Exception as e:
        logging.warning(f"rules_lookup fallback: {e}")
    return None


# ---------- Gentle one-liners (персональные короткие подсказки) ----------
def tiny_care_tip(lang: str, mood: str, profile: dict) -> str:
    age_s = str(profile.get("age",""))
    goal = (profile.get("goal") or "").lower()
    # Простая персонализация
    if lang == "ru":
        if mood == "excellent": return "🔥 Отлично! Сохраним ритм — 5 минут движения к кофе?"
        if mood == "ok":        return "🙂 Чуть добавим энергии: 300 мл воды и 1 минуту дыхания 4–6."
        if mood == "tired":     return "😴 Усталость: короткая прогулка 5–7 мин и стакан воды помогут."
        if mood == "pain":      return "🤕 Наблюдаем за болью. Если усиливается — отвечай «⚡ 60 сек» для плана."
        return "👍 Бережём темп и воду рядом."
    if lang == "uk":
        if mood == "excellent": return "🔥 Круто! 5 хвилин руху до кави?"
        if mood == "ok":        return "🙂 300 мл води і 1 хв дихання 4–6 — інакше."
        if mood == "tired":     return "😴 Втома: прогулянка 5–7 хв + склянка води."
        if mood == "pain":      return "🤕 Спостерігаємо за болем. Якщо посилюється — натисни «⚡ 60 c»."
        return "👍 Бережемо темп і воду поруч."
    # en/es (коротко)
    if mood == "excellent": return "🔥 Nice! Lock the win — 5 min walk before coffee?"
    if mood == "ok":        return "🙂 300 ml water + 1-min 4–6 breathing."
    if mood == "tired":     return "😴 Try 5–7 min walk + water."
    if mood == "pain":      return "🤕 If worsening, tap “⚡ 60s” for a quick plan."
    return "👍 Keep the pace and keep water nearby."


# ---------- Challenge: water7 ----------
def _challenge_tick(uid: int) -> Optional[str]:
    ch = challenge_get(uid)
    if not ch:
        return None
    try:
        done = int(ch.get("days_done") or "0")
        length = int(ch.get("length_days") or "7")
    except Exception:
        done, length = 0, 7
    done = min(length, done + 1)
    # Update in sheet/memory
    if SHEETS_ENABLED and ws_challenges:
        try:
            vals = ws_challenges.get_all_records()
            for i, r in enumerate(vals, start=2):
                if r.get("user_id")==str(uid) and r.get("challenge_id")==ch.get("challenge_id"):
                    ws_challenges.update_cell(i, ( _headers(ws_challenges).index("days_done")+1 ), str(done))
                    if done >= length:
                        ws_challenges.update_cell(i, ( _headers(ws_challenges).index("status")+1 ), "done")
                    break
        except Exception as e:
            logging.warning(f"challenge_tick -> memory fallback: {e}")
            ch["days_done"] = str(done)
            if done >= length: ch["status"] = "done"
    else:
        ch["days_done"] = str(done)
        if done >= length: ch["status"] = "done"
    return f"{T[_user_lang(uid)]['challenge_progress'].format(d=done, len=length)}"


# ---------- Quiet hours & limiter ----------
def _auto_allowed(uid: int) -> bool:
    today = datetime.utcnow().date().isoformat()
    u = users_get(uid) or {}
    last = (u.get("last_auto_date") or "")
    cnt = int(str(u.get("last_auto_count") or "0") or "0")
    if last != today:
        users_set(uid, "last_auto_date", today)
        users_set(uid, "last_auto_count", "0")
        cnt = 0
    return cnt < AUTO_MAX_PER_DAY

def _auto_inc(uid: int):
    today = datetime.utcnow().date().isoformat()
    u = users_get(uid) or {}
    last = (u.get("last_auto_date") or "")
    cnt = int(str(u.get("last_auto_count") or "0") or "0")
    if last != today:
        users_set(uid, "last_auto_date", today); users_set(uid, "last_auto_count", "1")
    else:
        users_set(uid, "last_auto_count", str(cnt+1))


# ---------- Scheduling (реализация) ----------
def _remove_jobs(app, name: str):
    if not getattr(app, "job_queue", None):
        return
    for j in list(app.job_queue.jobs()):
        if j.name == name:
            j.schedule_removal()

def _run_daily(app, name: str, hour_local: int, minute_local: int, tz_off: int, data: dict, callback):
    """Регистрируем ежедневную задачу с учётом часового офсета пользователя (без DST)."""
    if not getattr(app, "job_queue", None):
        return
    _remove_jobs(app, name)
    utc_h = (hour_local - tz_off) % 24
    t = dtime(hour=utc_h, minute=minute_local, tzinfo=timezone.utc)
    app.job_queue.run_daily(callback, time=t, data=data, name=name)

def schedule_daily_checkin(app, uid: int, tz_off: int, hhmm: str, lang: str):
    h, m = hhmm_tuple(hhmm)
    _run_daily(app, f"gm_{uid}", h, m, tz_off, {"user_id": uid, "kind": "gm"}, job_gm)

def schedule_evening_checkin(app, uid: int, tz_off: int, hhmm: str, lang: str):
    h, m = hhmm_tuple(hhmm)
    _run_daily(app, f"eve_{uid}", h, m, tz_off, {"user_id": uid, "kind": "eve"}, job_evening)


async def job_gm(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id")
    lang = _user_lang(uid)
    if not _auto_allowed(uid):  # лимитер
        return
    local_now = user_local_now(uid)
    if adjust_out_of_quiet(local_now, _user_quiet_hours(uid)) != local_now:
        return  # тихие часы
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
        _auto_inc(uid)
    except Exception as e:
        logging.warning(f"job_gm send failed: {e}")

async def job_evening(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id")
    lang = _user_lang(uid)
    if not _auto_allowed(uid):
        return
    local_now = user_local_now(uid)
    if adjust_out_of_quiet(local_now, _user_quiet_hours(uid)) != local_now:
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["evening_tip_btn"], callback_data="eve|tip")],
        [InlineKeyboardButton(T[lang]["hydrate_btn"],     callback_data="water|nudge")]
    ])
    try:
        await context.bot.send_message(uid, T[lang]["evening_intro"], reply_markup=kb)
        _auto_inc(uid)
    except Exception as e:
        logging.warning(f"job_evening send failed: {e}")

async def job_oneoff_reminder(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id")
    rid = d.get("reminder_id")
    lang = _user_lang(uid)
    # читаем текст напоминания
    text = None
    if SHEETS_ENABLED and ws_reminders:
        try:
            vals = ws_reminders.get_all_records()
            for r in vals:
                if r.get("id")==rid:
                    text = r.get("text"); break
        except Exception:
            pass
    if not text:
        for r in MEM_REMINDERS:
            if r.get("id")==rid: text = r.get("text"); break
    try:
        await context.bot.send_message(uid, f"⏰ {text or T[lang]['thanks']}")
    except Exception:
        pass


# ---------- Reminders helpers ----------
def schedule_oneoff_local(app, uid: int, local_after_hours: float, text: str):
    """Запланировать разовое напоминание через N часов (локально, с тихими часами)."""
    now_local = user_local_now(uid)
    when_local = now_local + timedelta(hours=local_after_hours)
    when_local = adjust_out_of_quiet(when_local, _user_quiet_hours(uid))
    when_utc = local_to_utc_dt(uid, when_local)
    rid = reminder_add(uid, text, when_utc)
    if getattr(app, "job_queue", None):
        app.job_queue.run_once(job_oneoff_reminder, when=(when_utc - utcnow()), data={"user_id":uid, "reminder_id":rid})
    return rid


# ---------- Command stubs (улучшенные версии) ----------
async def cmd_hydrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    await update.message.reply_text(T[lang]["hydrate_nudge"])
    tip = _get_daily_tip(profiles_get(uid) or {}, lang)
    await show_quickbar(context, update.effective_chat.id, lang)
    if random.random() < 0.4:  # лёгкий бонус-совет из правил, если есть
        adv = rules_lookup("hydration", "", lang)
        if adv:
            await update.message.reply_text(f"{T[lang]['daily_tip_prefix']} {adv}")

async def cmd_skintip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    p = profiles_get(uid) or {}
    try: age = int(re.search(r"\d+", str(p.get("age","") or "0")).group(0))
    except Exception: age = 30
    await update.message.reply_text(_get_skin_tip(lang, (p.get("sex") or ""), age))

async def cmd_evening_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    txt = (update.message.text or "").strip()
    m = re.search(r"([01]?\d|2[0-3]):([0-5]\d)", txt)
    hhmm = m.group(0) if m else DEFAULT_EVENING_LOCAL
    users_set(uid, "evening_hour", hhmm)
    if _has_jq_ctx(context):
        schedule_evening_checkin(context.application, uid, _user_tz_off(uid), hhmm, lang)
    await update.message.reply_text(T[lang]["evening_set"].format(t=hhmm))

async def cmd_evening_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    users_set(uid, "evening_hour", "")
    _remove_jobs(context.application, f"eve_{uid}")
    await update.message.reply_text(T[lang]["evening_off"])

async def cmd_quiet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    txt = (update.message.text or "").strip()
    m = re.search(r"([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-3]):([0-5]\d)", txt)
    if not m:
        await update.message.reply_text(T[lang]["ask_quiet"]); return
    qh = m.group(0)
    profiles_upsert(uid, {"quiet_hours": qh})
    await update.message.reply_text(T[lang]["quiet_saved"].format(qh=qh))


# ---------- Cycle text handlers ----------
def _calc_cycle_phase(last: str, avg_len: int, today: Optional[date]=None) -> str:
    try:
        y,m,d = map(int, last.split("-"))
        lmp = date(y,m,d)
        t = today or datetime.utcnow().date()
        day = (t - lmp).days % max(24, min(40, avg_len or 28))
        if day <= 5:   return "menstruation"
        if day <= 13:  return "follicular"
        if day <= 16:  return "ovulation"
        return "luteal"
    except Exception:
        return "unknown"

def _cycle_tip(lang: str, phase: str) -> str:
    tips_ru = {
        "menstruation":"Мягкий режим: вода, сон, лёгкая растяжка.",
        "follicular":"Хорошее время для тренировок и задач с фокусом.",
        "ovulation":"Больше энергии — но следи за сном и водой.",
        "luteal":"Добавь магний/омега по согласованию с врачом, береги ритм."
    }
    tips_en = {
        "menstruation":"Gentle mode: hydration, sleep, light stretching.",
        "follicular":"Great window for training and focus tasks.",
        "ovulation":"Energy is up — guard sleep and hydration.",
        "luteal":"Consider magnesium/omega (doctor-approved), keep the pace."
    }
    src = tips_ru if lang in {"ru","uk"} else tips_en
    return src.get(phase, src["follicular"])


# ---------- Callback handler ----------
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q: return
    await q.answer()
    uid = q.from_user.id
    lang = _user_lang(uid)
    data = (q.data or "")
    parts = data.split("|")
    kind = parts[0]

    # --- MINI INTAKE ---
    if kind == "mini":
        action = parts[1]
        key = parts[2]
        if action == "choose":
            value = parts[3]
            mini_handle_choice(uid, key, value)
            await ask_next_mini(context, q.message.chat_id, lang, uid)
        elif action == "write":
            sessions.setdefault(uid, {})["awaiting_mini_free"] = key
            await q.message.reply_text(T[lang]["write"])
        elif action == "skip":
            mini_handle_choice(uid, key, "")
            await ask_next_mini(context, q.message.chat_id, lang, uid)
        return

    # --- CONSENT ---
    if kind == "consent":
        ans = parts[1]
        users_set(uid, "consent", "yes" if ans=="yes" else "no")
        await q.message.reply_text(T[lang]["thanks"])
        await show_quickbar(context, q.message.chat_id, lang)
        return

    # --- MENU ---
    if kind == "menu":
        menu_item = parts[1]
        if menu_item == "h60":
            sessions.setdefault(uid, {})["awaiting_h60_text"] = True
            await q.message.reply_text(T[lang]["h60_intro"])
        elif menu_item == "rem":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(T[lang]["act_rem_4h"], callback_data="rem|4h")],
                [InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="rem|eve")],
                [InlineKeyboardButton(T[lang]["act_rem_morn"], callback_data="rem|morn")],
            ])
            await q.message.reply_text("⏰ Выберите напоминание:", reply_markup=kb)
        elif menu_item == "er":
            await q.message.reply_text("🚑 Если сильная/внезапная боль, потеря сознания, кровотечение, паралич — вызывайте скорую немедленно.")
        elif menu_item == "lab":
            await q.message.reply_text("🧪 Лаборатория: добавлю персональные панели позже. Пока: общий анализ крови, ферритин, B12, D25-OH — по согласованию с врачом.")
        await show_quickbar(context, q.message.chat_id, lang)
        return

    # --- REMINDERS QUICK ---
    if kind == "rem":
        what = parts[1]
        if what == "4h":
            schedule_oneoff_local(context.application, uid, 4, "Проверка самочувствия")
        elif what == "eve":
            schedule_oneoff_local(context.application, uid, 8, "Вечерний чек-ин")
        elif what == "morn":
            schedule_oneoff_local(context.application, uid, 16, "Утренний чек-ин")
        await q.message.reply_text("✔️ Напоминание поставлено.")
        return

    # --- GM Mood ---
    if kind == "gm":
        sub = parts[1]
        if sub == "note":
            sessions.setdefault(uid, {})["awaiting_mood_note"] = True
            await q.message.reply_text(T[lang]["fb_write"])
            return
        if sub == "skip":
            await q.message.reply_text("Ок! Увидимся позже.")
            return
        if sub == "mood":
            mood = parts[2]
            daily_add(iso(utcnow()), uid, mood=mood)
            tip = tiny_care_tip(lang, mood, profiles_get(uid) or {})
            await q.message.reply_text(tip)
            # Бонус: тик челенджа воды
            ch = _challenge_tick(uid)
            if ch:
                await q.message.reply_text(ch)
            await show_quickbar(context, q.message.chat_id, lang)
            return

    # --- Energy rate ---
    if kind == "energy":
        if parts[1] == "rate":
            try: val = int(parts[2])
            except: val = None
            daily_add(iso(utcnow()), uid, energy=val)
            await q.message.reply_text(T[lang]["gm_energy_done"])
            await show_quickbar(context, q.message.chat_id, lang)
            return

    # --- Evening nudges/tips ---
    if kind == "eve" and parts[1] == "tip":
        prof = profiles_get(uid) or {}
        adv = rules_lookup("daily_tip", age_to_band(int(re.search(r"\d+", str(prof.get("age","") or "0")).group(0) if re.search(r"\d+", str(prof.get("age","") or "0")) else 30)), lang) \
              or _get_daily_tip(prof, lang)
        await q.message.reply_text(f"{T[lang]['daily_tip_prefix']} {adv}")
        return

    # --- Water nudge (challenge start) ---
    if kind == "water" and parts[1] == "nudge":
        await q.message.reply_text(T[lang]["hydrate_nudge"])
        if not challenge_get(uid):
            challenge_start(uid)
            await q.message.reply_text(T[lang]["challenge_started"])
        else:
            ch = _challenge_tick(uid)
            if ch: await q.message.reply_text(ch)
        return

    # --- Cycle consent ---
    if kind == "cycle":
        ans = parts[1]
        if ans == "yes":
            sessions.setdefault(uid, {})["awaiting_cycle_date"] = True
            await q.message.reply_text(T[lang]["cycle_ask_last"])
        else:
            profiles_upsert(uid, {"cycle_enabled":"no"})
            await q.message.reply_text("Ок, без отслеживания цикла.")
        return


# ---------- Text handler (свободный диалог + уточнения) ----------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    uid = update.effective_user.id
    users_upsert(uid, update.effective_user.username or "", _user_lang(uid))
    lang = _user_lang(uid)
    txt = (update.message.text or "").strip()
    update_last_seen(uid)

    # 0) Мини-опрос если профиль пуст и опрос не активен
    prof = profiles_get(uid) or {}
    if profile_is_incomplete(prof) and not sessions.get(uid, {}).get("mini_active"):
        await start_mini_intake(context, update.effective_chat.id, lang, uid)
        return

    s = sessions.setdefault(uid, {})

    # 1) Ввод свободного поля мини-опроса
    if s.get("awaiting_mini_free"):
        key = s.pop("awaiting_mini_free")
        mini_handle_choice(uid, key, txt)
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return

    # 2) Комментарий к GM
    if s.get("awaiting_mood_note"):
        s.pop("awaiting_mood_note", None)
        daily_add(iso(utcnow()), uid, comment=txt)
        await update.message.reply_text(T[lang]["mood_thanks"])
        await show_quickbar(context, update.effective_chat.id, lang)
        return

    # 3) Цикл: дата последней менструации
    if s.get("awaiting_cycle_date"):
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", txt):
            await update.message.reply_text(T[lang]["cycle_ask_last"]); return
        profiles_upsert(uid, {"cycle_last_date": txt})
        s.pop("awaiting_cycle_date", None)
        s["awaiting_cycle_len"] = True
        await update.message.reply_text(T[lang]["cycle_ask_len"])
        return

    # 4) Цикл: средняя длина
    if s.get("awaiting_cycle_len"):
        try:
            avg = int(re.search(r"\d+", txt).group(0))
        except Exception:
            await update.message.reply_text(T[lang]["cycle_ask_len"]); return
        profiles_upsert(uid, {"cycle_avg_len": str(avg), "cycle_enabled":"yes"})
        await update.message.reply_text(T[lang]["cycle_saved"])
        s.pop("awaiting_cycle_len", None)
        phase = _calc_cycle_phase((profiles_get(uid) or {}).get("cycle_last_date",""), avg)
        await update.message.reply_text(_cycle_tip(lang, phase))
        return

    # 5) Health60 запрос
    if s.get("awaiting_h60_text"):
        s.pop("awaiting_h60_text", None)
        ep_id = episode_create(uid, topic=txt.strip()[:120], severity=5)
        plan = health60_make_plan(lang, txt, profiles_get(uid) or {})
        await update.message.reply_text(plan)
        # Быстрые действия под планом
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(T[lang]["act_rem_4h"],     callback_data="rem|4h")],
            [InlineKeyboardButton(T[lang]["act_save_episode"], callback_data=f"ep|save|{ep_id}")],
            [InlineKeyboardButton(T[lang]["act_ex_neck"],    callback_data="yt|neck")]
        ])
        await update.message.reply_text("Действия:", reply_markup=kb)
        await show_quickbar(context, update.effective_chat.id, lang)
        return

    # 6) Уточнения для очень коротких сообщений (где/как давно?)
    if len(txt) < 6:
        await update.message.reply_text(T[lang]["unknown"])
        await show_quickbar(context, update.effective_chat.id, lang)
        return

    # 7) Свободный диалог → роутер (если доступен)
    r = llm_router_answer(txt, lang, profiles_get(uid) or {})
    msg = r.get("assistant_reply") or T[lang]["unknown"]
    if (r.get("red_flags") is True):
        msg = "🚑 Важные признаки. Если ухудшается — вызывайте скорую."
    if (r.get("followups") or []) and (r.get("needs_more")):
        msg = msg + "\n\n" + _fmt_bullets(r["followups"][:3])
    pref = personalized_prefix(lang, profiles_get(uid) or {})
    if pref:
        msg = pref + "\n\n" + msg
    await update.message.reply_text(msg)
    await show_quickbar(context, update.effective_chat.id, lang)


# ---------- Extra callbacks not covered above ----------
# (не критично: заглушки на будущее)
# ep|save|<id>, yt|neck и пр.
async def _cb_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q: return
    await q.answer()
    await q.message.reply_text(T[_user_lang(q.from_user.id)]["thanks"])


# ---------- Build & Run ----------
def build_app():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is missing")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.post_init = post_init

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

    app.add_handler(CommandHandler("ru",           cmd_lang_ru))
    app.add_handler(CommandHandler("en",           cmd_lang_en))
    app.add_handler(CommandHandler("uk",           cmd_lang_uk))
    app.add_handler(CommandHandler("es",           cmd_lang_es))

    app.add_handler(CommandHandler("health60",     cmd_health60))
    app.add_handler(CommandHandler("mood",         cmd_mood))
    app.add_handler(CommandHandler("energy",       cmd_energy))
    app.add_handler(CommandHandler("hydrate",      cmd_hydrate))
    app.add_handler(CommandHandler("skintip",      cmd_skintip))

    # Menu/callbacks
    app.add_handler(CallbackQueryHandler(cb_handler), group=2)
    app.add_handler(CallbackQueryHandler(_cb_fallback), group=3)

    # Free text (последним, чтобы не перехватывать команды)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=1)

    return app


def main():
    app = build_app()
    logging.info("==> Starting TendAI bot (polling)…")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
