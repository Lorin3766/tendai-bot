# -*- coding: utf-8 -*-
# ===== TendAI — Part 1/2: База и UX =====
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
from gspread.exceptions import SpreadsheetNotFound, WorksheetNotFound
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
        "welcome": (
            "Hi! I’m TendAI — your health & longevity assistant.\n"
            "I build an *AI habit profile* (age, surgeries, chronic, meds/supps, routine & habits) "
            "and tailor advice more precisely over time. "
            "Let’s do a short intake (40–60s). You can also use quick buttons anytime."
        ),
        "help": "Short checkups, 24–48h plans, reminders, daily check-ins.\n"
                "Commands: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 "
                "/checkin_off /evening_on 20:00 /evening_off /settz +2 /health60 /mood /energy /hydrate "
                "/skintip /cycle /youth /life /ru /uk /en /es",
        "privacy": "TendAI is not a medical service and can’t replace a doctor. "
                   "We store minimal data for reminders. /delete_data to erase.",
        "paused_on": "Notifications paused. Use /resume to enable.",
        "paused_off": "Notifications resumed.",
        "deleted": "All your data was deleted. Use /start to begin again.",
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
        # Quickbar / hybrid
        "quick_title":"Quick actions",
        "quick_health":"⚡ Health in 60s",
        "quick_er":"🚑 ER info",
        "quick_lab":"🧪 Lab",
        "quick_rem":"⏰ Reminder",
        # Morning check-in
        "daily_gm":"Good morning! 🌞 How do you feel today?",
        "mood_excellent":"👍 Great","mood_ok":"🙂 Okay","mood_tired":"😐 Tired","mood_pain":"🤕 Pain","mood_skip":"⏭️ Skip",
        "mood_note":"✍️ Comment",
        "mood_thanks":"Thanks! Have a smooth day 👋",
        "mood_cmd":"How do you feel right now?",
        # Health60
        "h60_btn": "Health in 60 seconds",
        "h60_intro": "Briefly write what bothers you (e.g., “headache”, “fatigue”, “stomach pain”). I’ll give you 3 key tips in 60 seconds.",
        "h60_t1": "Possible causes",
        "h60_t2": "Do now (next 24–48h)",
        "h60_t3": "When to see a doctor",
        "h60_serious": "Serious to rule out",
        "plan_accept":"Will you try this today?",
        "accept_opts":["✅ Yes","🔁 Later","✖️ No"],
        "remind_when":"When shall I check on you?",
        "remind_opts":["in 4h","this evening","tomorrow morning","no need"],
        "thanks":"Got it 🙌",
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
        "streak_day": "Day of care",
        "challenge_btn": "🎯 7-day hydration challenge",
        "challenge_started": "Challenge started! I’ll track your daily check-ins.",
        "challenge_progress": "Challenge progress: {d}/{len} days.",
        "cycle_btn": "🩸 Cycle",
        "cycle_consent": "Would you like to track your cycle for gentle timing tips?",
        "cycle_ask_last": "Enter the date of your last period (YYYY-MM-DD):",
        "cycle_ask_len": "Average cycle length in days (e.g., 28):",
        "cycle_saved": "Cycle tracking saved.",
        "quiet_saved": "Quiet hours saved: {qh}",
        "set_quiet_btn": "🌙 Set quiet hours",
        "ask_quiet": "Type quiet hours as HH:MM-HH:MM (local), e.g. 22:00-08:00",
        "evening_intro": "Evening check-in:",
        "evening_tip_btn": "🪄 Tip of the day",
        "evening_set": "Evening check-in set to {t} (local).",
        "evening_off": "Evening check-in disabled.",
        # ER & misc
        "act_er":"🚑 Emergency info",
        "er_text":"If symptoms worsen, severe shortness of breath, chest pain, confusion, or persistent high fever — seek urgent care/emergency.",
        # Feedback
        "ask_fb":"Was this helpful?",
        "fb_thanks":"Thanks for your feedback! ✅",
        "fb_write":"Write a short feedback message:",
        "fb_good":"👍 Like",
        "fb_bad":"👎 Dislike",
        "fb_free":"📝 Feedback",
        # Life metrics
        "life_today":"Today is your {n}-th day of life 🎉. Target — 36,500 (100 years).",
        "life_percent":"You’ve passed {p}% of the way to 100 years.",
        "life_estimate":"(estimated by age)",
        "life_bar":"Progress: {bar} {p}%",
        # Personalization prefix
        "px":"Considering your profile: {sex}, {age}y; goal — {goal}.",
        "back":"◀ Back",
        "exit":"Exit",
    },
    "ru": {
        "welcome": (
            "Привет! Я TendAI — ассистент здоровья и долголетия.\n"
            "Я формирую *AI-профиль привычек* (возраст, операции, хронические, добавки, режим и привычки) "
            "и со временем точнее подстраиваю советы. "
            "Давайте короткий опрос (40–60с). Быстрые кнопки доступны всегда."
        ),
        "help":"Короткие проверки, план на 24–48 ч, напоминания, ежедневные чек-ины.\n"
               "Команды: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 "
               "/checkin_off /evening_on 20:00 /evening_off /settz +3 /health60 /mood /energy /hydrate "
               "/skintip /cycle /youth /life /ru /uk /en /es",
        "privacy":"TendAI не заменяет врача. Храним минимум данных для напоминаний. /delete_data — удалить.",
        "paused_on":"Напоминания на паузе. /resume — включить.",
        "paused_off":"Напоминания снова включены.",
        "deleted":"Все данные удалены. /start — начать заново.",
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
        # Quickbar / hybrid
        "quick_title":"Быстрые действия",
        "quick_health":"⚡ Здоровье за 60 сек",
        "quick_er":"🚑 Срочно в скорую",
        "quick_lab":"🧪 Лаборатория",
        "quick_rem":"⏰ Напоминание",
        # Morning check-in
        "daily_gm":"Доброе утро! 🌞 Как сегодня самочувствие?",
        "mood_excellent":"👍 Отлично","mood_ok":"🙂 Нормально","mood_tired":"😐 Устал","mood_pain":"🤕 Болит","mood_skip":"⏭️ Пропустить",
        "mood_note":"✍️ Комментарий",
        "mood_thanks":"Спасибо! Хорошего дня 👋",
        "mood_cmd":"Как сейчас самочувствие?",
        # Health60
        "h60_btn": "Здоровье за 60 секунд",
        "h60_intro": "Коротко напишите, что беспокоит (например: «болит голова», «усталость», «боль в животе»). Дам 3 ключевых совета за 60 секунд.",
        "h60_t1": "Возможные причины",
        "h60_t2": "Что сделать сейчас (24–48 ч)",
        "h60_t3": "Когда обратиться к врачу",
        "h60_serious": "Что серьёзное исключить",
        "plan_accept":"Готовы попробовать сегодня?",
        "accept_opts":["✅ Да","🔁 Позже","✖️ Нет"],
        "remind_when":"Когда напомнить и спросить самочувствие?",
        "remind_opts":["через 4 часа","вечером","завтра утром","не надо"],
        "thanks":"Принято 🙌",
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
        "streak_day": "День заботы",
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
        # ER & misc
        "act_er":"🚑 Когда срочно в скорую",
        "er_text":"Если нарастает, сильная одышка, боль в груди, спутанность, стойкая высокая температура — как можно скорее к неотложке/скорой.",
        # Feedback
        "ask_fb":"Это было полезно?",
        "fb_thanks":"Спасибо за отзыв! ✅",
        "fb_write":"Напишите короткий отзыв одним сообщением:",
        "fb_good":"👍 Нравится",
        "fb_bad":"👎 Не полезно",
        "fb_free":"📝 Отзыв",
        # Life metrics
        "life_today":"Сегодня твой {n}-й день жизни 🎉. Цель — 36 500 (100 лет).",
        "life_percent":"Ты прошёл уже {p}% пути к 100 годам.",
        "life_estimate":"(оценочно по возрасту)",
        "life_bar":"Прогресс: {bar} {p}%",
        # Personalization prefix
        "px":"С учётом профиля: {sex}, {age} лет; цель — {goal}.",
        "back":"◀ Назад",
        "exit":"Выйти",
    },
    "uk": {
        "welcome":"Привіт! Я TendAI — асистент здоров’я та довголіття.\n"
                  "Я створюю *AI-профіль звичок* і з часом точніше підлаштовую поради. "
                  "Почнімо з короткого опитника (40–60с). Швидкі кнопки доступні завжди.",
        "help":"Короткі перевірки, план на 24–48 год, нагадування, щоденні чек-іни.\n"
               "Команди: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 "
               "/checkin_off /evening_on 20:00 /evening_off /settz +2 /health60 /mood /energy /hydrate "
               "/skintip /cycle /youth /life /ru /uk /en /es",
        "privacy":"TendAI не замінює лікаря. Зберігаємо мінімум даних для нагадувань. /delete_data — видалити.",
        "paused_on":"Нагадування призупинені. /resume — увімкнути.",
        "paused_off":"Нагадування знову увімкнені.",
        "deleted":"Усі дані видалено. /start — почати знову.",
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
        "quick_title":"Швидкі дії",
        "quick_health":"⚡ Здоров’я за 60 сек",
        "quick_er":"🚑 Терміново в швидку",
        "quick_lab":"🧪 Лабораторія",
        "quick_rem":"⏰ Нагадування",
        "daily_gm":"Доброго ранку! 🌞 Як сьогодні самопочуття?",
        "mood_excellent":"👍 Чудово","mood_ok":"🙂 Нормально","mood_tired":"😐 Втома","mood_pain":"🤕 Болю","mood_skip":"⏭️ Пропустити",
        "mood_note":"✍️ Коментар",
        "mood_thanks":"Дякую! Гарного дня 👋",
        "mood_cmd":"Як почуваєтесь зараз?",
        "h60_btn": "Здоров’я за 60 секунд",
        "h60_intro": "Коротко напишіть, що турбує… Я дам 3 ключові поради за 60 секунд.",
        "h60_t1": "Можливі причини",
        "h60_t2": "Що зробити зараз (24–48 год)",
        "h60_t3": "Коли звернутися до лікаря",
        "h60_serious": "Що серйозне виключити",
        "plan_accept":"Готові спробувати сьогодні?",
        "accept_opts":["✅ Так","🔁 Пізніше","✖️ Ні"],
        "remind_when":"Коли нагадати та спитати самопочуття?",
        "remind_opts":["через 4 год","увечері","завтра вранці","не треба"],
        "thanks":"Прийнято 🙌",
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
        "streak_day": "День турботи",
        "challenge_btn": "🎯 Челендж 7 днів (вода)",
        "challenge_started": "Челендж запущено!",
        "challenge_progress": "Прогрес: {d}/{len} днів.",
        "cycle_btn": "🩸 Цикл",
        "cycle_consent": "Відстежувати цикл?",
        "cycle_ask_last": "Дата останніх менструацій (РРРР-ММ-ДД):",
        "cycle_ask_len": "Середня тривалість (напр., 28):",
        "cycle_saved": "Відстеження збережено.",
        "quiet_saved": "Тихі години збережено: {qh}",
        "set_quiet_btn": "🌙 Тихі години",
        "ask_quiet": "ГГ:ХХ-ГГ:ХХ, напр. 22:00-08:00",
        "evening_intro": "Вечірній чек-ін:",
        "evening_tip_btn": "🪄 Порада дня",
        "evening_set": "Вечірній чек-ін встановлено на {t} (локально).",
        "evening_off": "Вимкнено.",
        "act_er":"🚑 Коли терміново в швидку",
        "er_text":"Якщо посилюється… — якнайшвидше до невідкладної.",
        "ask_fb":"Чи було це корисно?",
        "fb_thanks":"Дякую за відгук! ✅",
        "fb_write":"Напишіть короткий відгук:",
        "fb_good":"👍 Подобається",
        "fb_bad":"👎 Не корисно",
        "fb_free":"📝 Відгук",
        "life_today":"Сьогодні твій {n}-й день життя 🎉. Мета — 36 500 (100 років).",
        "life_percent":"Ти пройшов {p}% шляху до 100 років.",
        "life_estimate":"(орієнтовно за віком)",
        "life_bar":"Прогрес: {bar} {p}%",
        "px":"З урахуванням профілю: {sex}, {age} р.; мета — {goal}.",
        "back":"◀ Назад",
        "exit":"Вийти",
    },
}
T["es"] = T["en"]  # заглушка


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
        lang = _user_lang(q.from_user.id)
        await context.bot.send_message(update.effective_chat.id, T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
        await show_quickbar(context, update.effective_chat.id, lang)


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
        profiles_upsert(uid, answers)
        sessions[uid]["mini_active"] = False
        # первичный набросок ai_profile
        ap = {"goals": answers.get("goal",""), "habits": answers.get("habits",""), "diet_focus": answers.get("diet_focus","")}
        profiles_upsert(uid, {"ai_profile": json.dumps(ap, ensure_ascii=False), "updated_at": iso(utcnow())})
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
    return ws.row_values(1) if ws and ws.row_values(1) else []

def _ws_ensure_columns(ws, desired_headers: List[str]):
    """
    Надёжное добавление недостающих заголовков + авторасширение листа.
    """
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
            except gspread.WorksheetNotFound:
                ws = ss.add_worksheet(title=title, rows=4000, cols=max(50, len(headers)))
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
            "quiet_hours","consent_flags","notes","updated_at","city",
            "surgeries","ai_profile","birth_date"
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
    return ws.row_values(1)

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

def profiles_upsert(uid: int, data: Dict[str, str]):
    data = dict(data or {})
    data["user_id"] = str(uid)
    data.setdefault("updated_at", iso(utcnow()))
    if SHEETS_ENABLED:
        vals = ws_profiles.get_all_records()
        hdr = _headers(ws_profiles)
        for k in data:
            if k not in hdr:
                _ws_ensure_columns(ws_profiles, hdr + [k]); hdr = _headers(ws_profiles)
        end_col = gsu.rowcol_to_a1(1, len(hdr)).rstrip("1")
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                merged = {**r, **data}
                ws_profiles.update(f"A{i}:{end_col}{i}", [[merged.get(h, "") for h in hdr]])
                return
        ws_profiles.append_row([data.get(h, "") for h in hdr])
    else:
        MEM_PROFILES[uid] = {**MEM_PROFILES.get(uid, {}), **data}

def feedback_add(ts: str, uid: int, name: str, username: str, rating: str, comment: str):
    rec = {"timestamp": ts, "user_id": str(uid), "name": name, "username": username, "rating": rating, "comment": comment}
    if SHEETS_ENABLED:
        ws_feedback.append_row([rec.get(h, "") for h in _headers(ws_feedback)])
    else:
        MEM_FEEDBACK.append(rec)

def daily_add(ts: str, uid: int, mood: str="", comment: str="", energy: Optional[int]=None):
    rec = {"timestamp": ts, "user_id": str(uid), "mood": mood, "comment": comment, "energy": "" if energy is None else str(energy)}
    if SHEETS_ENABLED:
        ws_daily.append_row([rec.get(h, "") for h in _headers(ws_daily)])
    else:
        MEM_DAILY.append(rec)

def episode_create(uid: int, topic: str, severity: int=5, red: str="") -> str:
    eid = str(uuid.uuid4())
    rec = {"episode_id": eid, "user_id": str(uid), "topic": topic, "started_at": iso(utcnow()),
           "baseline_severity": str(severity), "red_flags": red, "plan_accepted": "", "target":"", "reminder_at":"",
           "next_checkin_at":"", "status":"open", "last_update": iso(utcnow()), "notes":""}
    if SHEETS_ENABLED:
        ws_episodes.append_row([rec.get(h, "") for h in _headers(ws_episodes)])
    else:
        MEM_EPISODES.append(rec)
    return eid

def episode_find_open(uid: int) -> Optional[dict]:
    store = ws_episodes.get_all_records() if SHEETS_ENABLED else MEM_EPISODES
    for r in store:
        if str(r.get("user_id")) == str(uid) and (r.get("status") or "") == "open":
            return r
    return None

def episode_set(eid: str, field: str, value: str):
    if SHEETS_ENABLED:
        vals = ws_episodes.get_all_records()
        hdr = _headers(ws_episodes)
        if field not in hdr:
            _ws_ensure_columns(ws_episodes, hdr + [field]); hdr = _headers(ws_episodes)
        for i, r in enumerate(vals, start=2):
            if str(r.get("episode_id")) == str(eid):
                ws_episodes.update_cell(i, hdr.index(field)+1, value); return
    else:
        for r in MEM_EPISODES:
            if r["episode_id"] == eid:
                r[field] = value; return

def challenge_get(uid: int) -> Optional[dict]:
    store = ws_challenges.get_all_records() if SHEETS_ENABLED else MEM_CHALLENGES
    for r in store:
        if str(r.get("user_id")) == str(uid) and str(r.get("status")) != "done":
            return r
    return None

def challenge_start(uid: int, length_days:int=7):
    rec = {"user_id": str(uid), "challenge_id": str(uuid.uuid4()), "name": "hydrate7",
           "start_date": iso(utcnow()), "length_days": str(length_days), "days_done":"0", "status":"active"}
    if SHEETS_ENABLED:
        ws_challenges.append_row([rec.get(h, "") for h in _headers(ws_challenges)])
    else:
        MEM_CHALLENGES.append(rec)

def reminder_add(uid: int, text: str, when_utc: datetime) -> str:
    rid = str(uuid.uuid4())
    rec = {"id": rid, "user_id": str(uid), "text": text, "when_utc": iso(when_utc), "created_at": iso(utcnow()), "status":"open"}
    if SHEETS_ENABLED:
        ws_reminders.append_row([rec.get(h, "") for h in _headers(ws_reminders)])
    else:
        MEM_REMINDERS.append(rec)
    return rid


# ---------- Time & quiet hours helpers ----------
def _user_tz_off(uid:int) -> int:
    try:
        return int(str(users_get(uid).get("tz_offset") or "0"))
    except Exception:
        return 0

def hhmm_tuple(hhmm: str) -> Tuple[int,int]:
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", str(hhmm or ""))
    if not m: return (8,30)
    return (int(m.group(1)), int(m.group(2)))

def user_local_now(uid:int) -> datetime:
    off = _user_tz_off(uid)
    return utcnow() + timedelta(hours=off)

def local_to_utc_dt(uid:int, dt_local: datetime) -> datetime:
    off = _user_tz_off(uid)
    return (dt_local - timedelta(hours=off)).astimezone(timezone.utc)

def utc_to_local_dt(uid:int, dt_utc: datetime) -> datetime:
    off = _user_tz_off(uid)
    return (dt_utc + timedelta(hours=off))

def _user_quiet_hours(uid:int) -> str:
    prof = profiles_get(uid) or {}
    qh = (prof.get("quiet_hours") or DEFAULT_QUIET_HOURS).strip()
    return qh if re.match(r"^\s*([01]?\d|2[0-3]):[0-5]\d-([01]?\d|2[0-3]):[0-5]\d\s*$", qh) else DEFAULT_QUIET_HOURS

def adjust_out_of_quiet(dt_local: datetime, qh: str) -> datetime:
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-3]):([0-5]\d)\s*$", qh)
    if not m: return dt_local
    start = dt_local.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    end   = dt_local.replace(hour=int(m.group(3)), minute=int(m.group(4)), second=0, microsecond=0)
    if start <= end:
        if start <= dt_local <= end: return end
    else:
        if dt_local >= start or dt_local <= end:
            return end if dt_local <= end else (start + timedelta(days=1))
    return dt_local


# ---------- Quickbar & menus ----------
def quickbar_kb(lang: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(T[lang]["quick_health"], callback_data="menu|h60")],
        [InlineKeyboardButton(T[lang]["quick_er"],     callback_data="menu|er"),
         InlineKeyboardButton(T[lang]["quick_lab"],    callback_data="menu|lab")],
        [InlineKeyboardButton(T[lang]["quick_rem"],    callback_data="menu|rem")],
    ]
    return InlineKeyboardMarkup(rows)

async def show_quickbar(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str):
    try:
        await context.bot.send_message(chat_id, T[lang]["quick_title"], reply_markup=quickbar_kb(lang))
    except Exception as e:
        logging.warning(f"quickbar send failed: {e}")

def inline_topic_kb(lang: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(T[lang]["h60_btn"], callback_data="menu|h60")],
        [InlineKeyboardButton(T[lang]["hydrate_btn"], callback_data="menu|hydrate"),
         InlineKeyboardButton(T[lang]["gm_energy"],   callback_data="menu|energy")],
        [InlineKeyboardButton(T[lang]["set_quiet_btn"], callback_data="youth:set_quiet"),
         InlineKeyboardButton(T[lang]["gm_evening_btn"], callback_data="youth:gm_evening")],
    ]
    return InlineKeyboardMarkup(rows)


# ------------- LLM Router (with personalization) -------------
SYS_ROUTER = (
    "You are TendAI — a concise, warm, professional health & longevity assistant (not a doctor). "
    "Always answer strictly in {lang}. Keep replies short (<=6 lines + up to 4 bullets). "
    "Personalize using profile (sex/age/goal/conditions, diet_focus, steps_target). "
    "TRIAGE: ask 1–2 clarifiers first; advise ER only for clear red flags. "
    "Return JSON ONLY: {\"intent\":\"symptom\"|\"nutrition\"|\"sleep\"|\"labs\"|\"habits\"|\"longevity\"|\"other\","
    "\"assistant_reply\":\"string\",\"followups\":[\"string\"],\"needs_more\":true,\"red_flags\":false,\"confidence\":0.0}"
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
            messages=[
                {"role":"system","content":sys},
                {"role":"user","content":text}
            ]
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
    "You are TendAI — a concise, warm, professional health & longevity assistant (not a doctor). "
    "Always answer strictly in {lang}. Keep it short and practical. "
    "Given a symptom text and a brief user profile, produce JSON ONLY: "
    "{\"causes\":[\"...\"],\"serious\":\"...\",\"do_now\":[\"...\"],\"see_doctor\":[\"...\"]}. "
    "Rules: 2–4 simple causes, exactly 1 serious item to rule out, "
    "3–5 do_now concrete steps for the next 24–48h, 2–3 see_doctor cues."
)

def health60_make_plan(lang: str, symptom_text: str, profile: dict) -> str:
    fallback_map = {
        "ru": (
            f"{T['ru']['h60_t1']}:\n• Наиболее вероятные бытовые причины\n"
            f"{T['ru']['h60_serious']}: • Исключить редкие, но серьёзные состояния при ухудшении\n\n"
            f"{T['ru']['h60_t2']}:\n• Вода 300–500 мл\n• Короткий отдых 15–20 мин\n• Проветривание, меньше экранов\n\n"
            f"{T['ru']['h60_t3']}:\n• Усиление симптомов\n• Высокая температура/«красные флаги»\n• Боль ≥7/10"
        ),
        "uk": (
            f"{T['uk']['h60_t1']}:\n• Найімовірні побутові причини\n"
            f"{T['uk']['h60_serious']}: • Виключити рідкісні, але серйозні стани при погіршенні\n\n"
            f"{T['uk']['h60_t2']}:\n• Вода 300–500 мл\n• Відпочинок 15–20 хв\n• Провітрювання, менше екранів\n\n"
            f"{T['uk']['h60_t3']}:\n• Посилення симптомів\n• Висока температура/«червоні прапорці»\n• Біль ≥7/10"
        ),
        "en": (
            f"{T['en']['h60_t1']}:\n• Most likely everyday causes\n"
            f"{T['en']['h60_serious']}: • Rule out rare but serious issues if worsening\n\n"
            f"{T['en']['h60_t2']}:\n• Drink 300–500 ml water\n• 15–20 min rest\n• Ventilate, reduce screens\n\n"
            f"{T['en']['h60_t3']}:\n• Worsening symptoms\n• High fever/red flags\n• Pain ≥7/10"
        ),
    }
    fallback = fallback_map.get(lang, fallback_map["en"])

    if not oai:
        return fallback

    sys = SYS_H60.replace("{lang}", lang)
    user = {
        "symptom": (symptom_text or "").strip()[:500],
        "profile": {k: profile.get(k, "") for k in ["sex","age","goal","conditions","meds","sleep","activity","diet","diet_focus","steps_target"]}
    }
    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.2,
            max_tokens=420,
            response_format={"type": "json_object"},
            messages=[
                {"role":"system","content": sys},
                {"role":"user","content": json.dumps(user, ensure_ascii=False)}
            ]
        )
        data = json.loads(resp.choices[0].message.content.strip())
        causes = _fmt_bullets(data.get("causes") or [])
        serious = (data.get("serious") or "").strip()
        do_now = _fmt_bullets(data.get("do_now") or [])
        see_doc = _fmt_bullets(data.get("see_doctor") or [])

        parts = []
        if causes:
            parts.append(f"{T[lang]['h60_t1']}:\n{causes}")
        if serious:
            parts.append(f"{T[lang]['h60_serious']}: {serious}")
        if do_now:
            parts.append(f"\n{T[lang]['h60_t2']}:\n{do_now}")
        if see_doc:
            parts.append(f"\n{T[lang]['h60_t3']}:\n{see_doc}")
        return "\n".join(parts).strip()
    except Exception as e:
        logging.error(f"health60 LLM error: {e}")
        return fallback


# ---------- Scheduling stubs (safe, no NameError) ----------
def _has_jq_ctx(context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        return hasattr(context.application, "job_queue") and (context.application.job_queue is not None)
    except Exception:
        return False

def schedule_daily_checkin(app, uid:int, tz_off:int, hhmm:str, lang:str):
    try:
        # Часть 2 добавит реальное планирование; здесь — безопасный no-op.
        for j in app.job_queue.get_jobs_by_name(f"daily_{uid}"):
            j.schedule_removal()
    except Exception:
        pass

def schedule_evening_checkin(app, uid:int, tz_off:int, hhmm:str, lang:str):
    try:
        for j in app.job_queue.get_jobs_by_name(f"evening_{uid}"):
            j.schedule_removal()
    except Exception:
        pass

def schedule_from_sheet_on_start(app):
    # Заглушка — в Ч.2 можно подхватить напоминания/чек-ины из таблицы
    return


# ---------- One-off reminder job (stubbed) ----------
async def job_oneoff_reminder(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id"); rid = d.get("reminder_id")
    try:
        await context.bot.send_message(uid, T[_user_lang(uid)]["thanks"])
    except Exception as e:
        logging.error(f"job_oneoff_reminder send error: {e}")


# ---------- Helpers for daily/evening ----------
def _schedule_oneoff_with_sheet(context: ContextTypes.DEFAULT_TYPE, uid: int, when_local: datetime, text: str):
    qh = _user_quiet_hours(uid)
    adjusted_local = adjust_out_of_quiet(when_local, qh)
    when_utc = local_to_utc_dt(uid, adjusted_local)
    rid = reminder_add(uid, text, when_utc)
    if _has_jq_ctx(context):
        delay = max(60, (when_utc - utcnow()).total_seconds())
        try:
            context.application.job_queue.run_once(job_oneoff_reminder, when=delay, data={"user_id":uid,"reminder_id":rid})
        except Exception as e:
            logging.error(f"run_once failed: {e}")
    return adjusted_local

def _format_hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


# ---------- Morning/Evening jobs (реализация в Ч.2) ----------
async def job_daily_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id"); lang = norm_lang((d.get("lang") or "en"))
    if not uid: return
    u = users_get(uid)
    if (u.get("paused") or "").lower() == "yes":
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_excellent"], callback_data="gm|mood|excellent"),
         InlineKeyboardButton(T[lang]["mood_ok"],        callback_data="gm|mood|ok")],
        [InlineKeyboardButton(T[lang]["mood_tired"],     callback_data="gm|mood|tired"),
         InlineKeyboardButton(T[lang]["mood_pain"],      callback_data="gm|mood|pain")],
        [InlineKeyboardButton(T[lang]["mood_skip"],      callback_data="gm|skip")]
    ])
    try:
        await context.bot.send_message(uid, T[lang]["daily_gm"], reply_markup=kb)
    except Exception as e:
        logging.error(f"job_daily_checkin send error: {e}")

async def job_evening_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id"); lang = norm_lang((d.get("lang") or "en"))
    if not uid: return
    u = users_get(uid)
    if (u.get("paused") or "").lower() == "yes":
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["evening_tip_btn"], callback_data="youth:tip")],
        [InlineKeyboardButton("0", callback_data="num|0"),
         InlineKeyboardButton("3", callback_data="num|3"),
         InlineKeyboardButton("6", callback_data="num|6"),
         InlineKeyboardButton("8", callback_data="num|8"),
         InlineKeyboardButton("10", callback_data="num|10")]
    ])
    try:
        await context.bot.send_message(uid, T[lang]["evening_intro"], reply_markup=kb)
    except Exception as e:
        logging.error(f"job_evening_checkin send error: {e}")


# ---------- Misc helpers ----------
def _user_lang(uid:int) -> str:
    return norm_lang((users_get(uid) or {}).get("lang") or "en")

def update_last_seen(uid:int):
    users_set(uid, "last_seen", iso(utcnow()))

def _get_skin_tip(lang:str, sex:str, age:int) -> str:
    bank = {
        "ru": [
            "Умывание тёплой водой и 30–60 сек мягкого массажа — улучшает микроциркуляцию.",
            "Солнцезащита каждое утро — самый дешевый anti-age."
        ],
        "en": [
            "Rinse with warm water and gentle 30–60s massage — boosts microcirculation.",
            "Daily sunscreen is the most cost-effective anti-age step."
        ],
        "uk": [
            "Умивання теплою водою та 30–60с масаж — краща мікроциркуляція.",
            "Сонцезахист щоранку — найдешевший anti-age."
        ]
    }
    return random.choice(bank.get(lang, bank["en"]))

def _get_daily_tip(profile:dict, lang:str) -> str:
    tips = {
        "ru": ["Сделай 20 глубоких вдохов — мозг скажет «спасибо».", "5 минут на свет и воздух — перезагрузка."],
        "en": ["Take 20 deep breaths — your brain will thank you.", "5 minutes of light & air — soft reset."],
        "uk": ["Зроби 20 глибоких вдихів — мозок скаже «дякую».", "5 хвилин світла та повітря — перезапуск."]
    }
    return random.choice(tips.get(lang, tips["en"]))


# ===== Life metrics =====
def progress_bar(percent: float, width:int=12) -> str:
    p = max(0, min(100, int(round(percent))))
    done = max(0, min(width, int(round(width * p / 100.0))))
    return "█"*done + "░"*(width-done)

def life_metrics(profile: dict) -> Dict[str, int]:
    # Если есть birth_date (YYYY-MM-DD) — точнее; иначе оценочно по age*365
    today = date.today()
    bd = (profile.get("birth_date") or "").strip()
    days = 0
    if re.match(r"^\d{4}-\d{2}-\d{2}$", bd):
        try:
            y,m,d = map(int, bd.split("-")); born = date(y,m,d)
            days = (today - born).days
        except Exception:
            days = 0
    if days <= 0:
        try:
            age = int(re.findall(r"\d+", str(profile.get("age") or "0"))[0])
        except Exception:
            age = 0
        days = max(0, age*365)
    percent = min(100.0, round(days/365.0/100.0*100, 1))  # к 100 годам
    return {"days_lived": days, "percent_to_100": percent}


# ------------- Commands (shells) -------------
async def post_init(app):
    me = await app.bot.get_me()
    logging.info(f"BOT READY: @{me.username} (id={me.id})")
    try:
        schedule_from_sheet_on_start(app)
    except Exception as e:
        logging.warning(f"schedule_from_sheet_on_start failed: {e}")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = norm_lang(getattr(user, "language_code", None))
    users_upsert(user.id, user.username or "", lang)
    context.user_data["lang"] = lang
    sessions.setdefault(user.id, {})["last_user_text"] = "/start"
    update_last_seen(user.id)

    await update.message.reply_text(T[lang]["welcome"], reply_markup=ReplyKeyboardRemove())
    # Гибрид: показываем quickbar сразу
    await show_quickbar(context, update.effective_chat.id, lang)

    prof = profiles_get(user.id)
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
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, user.id, tz_off, hhmm, lang)
        eh = (u.get("evening_hour") or "").strip()
        if eh:
            schedule_evening_checkin(context.application, user.id, tz_off, eh, lang)
    else:
        logging.warning("JobQueue not available on /start – daily/evening check-in not scheduled.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    update_last_seen(update.effective_user.id)
    await update.message.reply_text(T[lang]["help"])
    await show_quickbar(context, update.effective_chat.id, lang)

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    update_last_seen(update.effective_user.id)
    await update.message.reply_text(T[lang]["privacy"])

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "yes")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    update_last_seen(uid)
    await update.message.reply_text(T[lang]["paused_on"])

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "no")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    update_last_seen(uid)
    await update.message.reply_text(T[lang]["paused_off"])

async def cmd_delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    update_last_seen(uid)
    if SHEETS_ENABLED:
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
    else:
        MEM_USERS.pop(uid, None); MEM_PROFILES.pop(uid, None)
        global MEM_EPISODES, MEM_REMINDERS, MEM_DAILY
        MEM_EPISODES = [r for r in MEM_EPISODES if r["user_id"]!=str(uid)]
        MEM_REMINDERS = [r for r in MEM_REMINDERS if r["user_id"]!=str(uid)]
        MEM_DAILY = [r for r in MEM_DAILY if r["user_id"]!=str(uid)]
    lang = norm_lang(getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(T[lang]["deleted"], reply_markup=ReplyKeyboardRemove())

# ---------- /profile ----------
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🧩 Start intake" if lang=="en" else "🧩 Начать опрос",
                                                      callback_data="intake:start")]])
    await update.message.reply_text(T[lang]["profile_intro"], reply_markup=kb)

# ---------- /settz ----------
async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    txt = (update.message.text or "").strip()
    m = re.search(r"(-?\+?\d{1,2})", txt)
    if not m:
        await update.message.reply_text("Usage: /settz +2  |  Пример: /settz +3")
        return
    try:
        off = int(m.group(1))
        off = max(-12, min(14, off))
        users_set(uid, "tz_offset", str(off))
        await update.message.reply_text(f"UTC offset set: {off:+d}")
        u = users_get(uid)
        hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
        if _has_jq_ctx(context):
            schedule_daily_checkin(context.application, uid, off, hhmm, lang)
    except Exception as e:
        logging.error(f"/settz error: {e}")
        await update.message.reply_text("Failed to set timezone offset.")

# ---------- /checkin_on /checkin_off ----------
async def cmd_checkin_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    txt = (update.message.text or "").strip()
    m = re.search(r"([01]?\d|2[0-3]):([0-5]\d)", txt)
    hhmm = m.group(0) if m else DEFAULT_CHECKIN_LOCAL
    users_set(uid, "checkin_hour", hhmm)
    users_set(uid, "paused", "no")
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, uid, _user_tz_off(uid), hhmm, lang)
    await update.message.reply_text(f"Daily check-in set to {hhmm} (local).")

async def cmd_checkin_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if _has_jq_ctx(context):
        for j in context.application.job_queue.get_jobs_by_name(f"daily_{uid}"):
            j.schedule_removal()
    users_set(uid, "paused", "yes")
    await update.message.reply_text("Daily check-in disabled.")

# ---------- Lang switches ----------
async def cmd_lang_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "ru")
    context.user_data["lang"] = "ru"
    await update.message.reply_text("Язык: русский.")

async def cmd_lang_en(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "en")
    context.user_data["lang"] = "en"
    await update.message.reply_text("Language set: English.")

async def cmd_lang_uk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "uk")
    context.user_data["lang"] = "uk"
    await update.message.reply_text("Мова: українська.")

async def cmd_lang_es(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "es")
    context.user_data["lang"] = "es"
    await update.message.reply_text("Idioma: español (beta).")

# ---------- Health60 ----------
async def cmd_health60(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    sessions.setdefault(uid, {})["awaiting_h60_text"] = True
    await update.message.reply_text(T[lang]["h60_intro"])
# ---------- Безопасные геттеры пользователя/профиля и локальное время ----------
def _user_lang(uid: int) -> str:
    try:
        return norm_lang(users_get(uid).get("lang") or "en")
    except Exception:
        return "en"

def _user_tz_off(uid: int) -> int:
    try:
        return int(str(users_get(uid).get("tz_offset") or "0"))
    except Exception:
        return 0

def _user_quiet_hours(uid: int) -> str:
    prof = profiles_get(uid) or {}
    return (prof.get("quiet_hours") or DEFAULT_QUIET_HOURS).strip()

def hhmm_tuple(s: str) -> Tuple[int, int]:
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", s or "")
    if not m:
        return (8, 30)
    return (int(m.group(1)), int(m.group(2)))

def user_local_now(uid: int) -> datetime:
    return utcnow() + timedelta(hours=_user_tz_off(uid))

def local_to_utc_dt(uid: int, local_dt: datetime) -> datetime:
    return local_dt - timedelta(hours=_user_tz_off(uid))

def adjust_out_of_quiet(dt_local: datetime, quiet: str) -> datetime:
    """Сдвигаем время, если попадает в тихие часы HH:MM-HH:MM (локально)."""
    try:
        m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-3]):([0-5]\d)\s*$", quiet or "")
        if not m:
            return dt_local
        start = dt_local.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        end   = dt_local.replace(hour=int(m.group(3)), minute=int(m.group(4)), second=0, microsecond=0)
        inside = False
        if start <= end:
            inside = start <= dt_local <= end
        else:
            # диапазон через полночь
            inside = not (end < dt_local < start)
        return (end + timedelta(minutes=5)) if inside else dt_local
    except Exception:
        return dt_local


# ---------- Быстрые клавиатуры (гибрид-общение) ----------
def quickbar_kb(lang: str) -> InlineKeyboardMarkup:
    row1 = [
        InlineKeyboardButton("⚡ " + (T[lang]["h60_btn"] if "h60_btn" in T[lang] else "Health60"), callback_data="menu|h60"),
    ]
    row2 = [
        InlineKeyboardButton("🚑 " + (T[lang].get("act_er","ER")), callback_data="menu|er"),
        InlineKeyboardButton("🧪 " + (T[lang].get("act_find_lab","Lab")), callback_data="menu|lab"),
    ]
    row3 = [
        InlineKeyboardButton("⏰ " + (T[lang].get("quick_rem","Reminder")), callback_data="menu|rem")
    ]
    return InlineKeyboardMarkup([row1, row2, row3])

async def show_quickbar(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str):
    try:
        await context.bot.send_message(chat_id, (T[lang].get("quick_title") or "Quick actions"), reply_markup=quickbar_kb(lang))
    except Exception as e:
        logging.warning(f"show_quickbar error: {e}")

def inline_topic_kb(lang: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("⚡ " + (T[lang]["h60_btn"] if "h60_btn" in T[lang] else "Health60"), callback_data="menu|h60"),
         InlineKeyboardButton(T[lang].get("youth_pack","Youth"), callback_data="menu|youth")],
        [InlineKeyboardButton(T[lang].get("hydrate_btn","Hydrate"), callback_data="menu|hydrate"),
         InlineKeyboardButton(T[lang].get("skintip_btn","Skin tip"), callback_data="menu|skintip")],
        [InlineKeyboardButton("🧪 " + (T[lang].get("act_find_lab","Lab")), callback_data="menu|lab"),
         InlineKeyboardButton("⏰ " + (T[lang].get("quick_rem","Reminder")), callback_data="menu|rem")],
    ]
    return InlineKeyboardMarkup(rows)


# ---------- Хранилища обёртки, которых нет в Ч.1 ----------
def profiles_upsert(uid: int, updates: Dict[str, str]):
    if SHEETS_ENABLED:
        hdr = _headers(ws_profiles)
        _ws_ensure_columns(ws_profiles, list(dict.fromkeys(hdr + list(updates.keys()))))
        vals = ws_profiles.get_all_records()
        row_i = None
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                row_i = i
                base = {**r, **updates, "user_id": str(uid), "updated_at": iso(utcnow())}
                ws_profiles.update(f"A{i}:{gsu.rowcol_to_a1(1,len(_headers(ws_profiles))).rstrip('1')}{i}",
                                   [[base.get(h,"") for h in _headers(ws_profiles)]])
                break
        if row_i is None:
            base = {"user_id": str(uid), "updated_at": iso(utcnow())}
            base.update(updates)
            ws_profiles.append_row([base.get(h,"") for h in _headers(ws_profiles)])
    else:
        prof = MEM_PROFILES.setdefault(uid, {})
        prof.update(updates)
        prof["updated_at"] = iso(utcnow())

def feedback_add(ts: str, uid: int, name: str, username: str, rating: str, comment: str):
    if SHEETS_ENABLED:
        ws_feedback.append_row([ts, str(uid), name, username, rating, comment])
    else:
        MEM_FEEDBACK.append({"timestamp":ts,"user_id":str(uid),"name":name,"username":username,"rating":rating,"comment":comment})

def daily_add(ts: str, uid: int, mood: str, comment: str, energy: Optional[int]):
    rec = {"timestamp": ts, "user_id": str(uid), "mood": mood, "energy": ("" if energy is None else str(energy)), "comment": comment}
    if SHEETS_ENABLED:
        _ws_ensure_columns(ws_daily, list(rec.keys()))
        ws_daily.append_row([rec.get(h,"") for h in _headers(ws_daily)])
    else:
        MEM_DAILY.append(rec)

def episode_create(uid: int, topic: str, severity: int, red: str) -> str:
    eid = uuid.uuid4().hex[:12]
    rec = {
        "episode_id": eid, "user_id": str(uid), "topic": topic,
        "started_at": iso(utcnow()), "baseline_severity": str(severity),
        "red_flags": red, "plan_accepted": "", "target": "", "reminder_at": "",
        "next_checkin_at": "", "status": "open", "last_update": iso(utcnow()), "notes":""
    }
    if SHEETS_ENABLED:
        _ws_ensure_columns(ws_episodes, list(rec.keys()))
        ws_episodes.append_row([rec.get(h,"") for h in _headers(ws_episodes)])
    else:
        MEM_EPISODES.append(rec)
    return eid

def episode_find_open(uid: int) -> Optional[dict]:
    if SHEETS_ENABLED:
        vals = ws_episodes.get_all_records()
        for r in reversed(vals):
            if str(r.get("user_id")) == str(uid) and (r.get("status") or "open") != "closed":
                return r
        return None
    for r in reversed(MEM_EPISODES):
        if r["user_id"] == str(uid) and r.get("status","open") != "closed":
            return r
    return None

def episode_set(eid: str, field: str, value: str):
    if SHEETS_ENABLED:
        vals = ws_episodes.get_all_records()
        hdr = _headers(ws_episodes)
        for i, r in enumerate(vals, start=2):
            if (r.get("episode_id") or "") == eid:
                if field not in hdr:
                    _ws_ensure_columns(ws_episodes, hdr + [field]); hdr = _headers(ws_episodes)
                ws_episodes.update_cell(i, hdr.index(field)+1, value)
                ws_episodes.update_cell(i, hdr.index("last_update")+1, iso(utcnow()))
                return
    else:
        for r in MEM_EPISODES:
            if r["episode_id"] == eid:
                r[field] = value
                r["last_update"] = iso(utcnow())
                return

def reminder_add(uid: int, text: str, when_utc: datetime) -> str:
    rid = uuid.uuid4().hex[:10]
    rec = {"id": rid, "user_id": str(uid), "text": text, "when_utc": iso(when_utc), "created_at": iso(utcnow()), "status":"scheduled"}
    if SHEETS_ENABLED:
        _ws_ensure_columns(ws_reminders, list(rec.keys()))
        ws_reminders.append_row([rec.get(h,"") for h in _headers(ws_reminders)])
    else:
        MEM_REMINDERS.append(rec)
    return rid

def challenge_get(uid: int) -> Optional[dict]:
    if SHEETS_ENABLED:
        for r in ws_challenges.get_all_records():
            if str(r.get("user_id")) == str(uid) and (r.get("status") or "") != "done":
                return r
        return None
    for r in MEM_CHALLENGES:
        if str(r.get("user_id")) == str(uid) and r.get("status")!="done":
            return r
    return None

def challenge_start(uid: int, length_days: int = 7):
    rec = {"user_id": str(uid), "challenge_id": uuid.uuid4().hex[:8], "name":"hydrate7",
           "start_date": date.today().isoformat(), "length_days": str(length_days),
           "days_done":"0", "status":"active"}
    if SHEETS_ENABLED:
        _ws_ensure_columns(ws_challenges, list(rec.keys()))
        ws_challenges.append_row([rec.get(h,"") for h in _headers(ws_challenges)])
    else:
        MEM_CHALLENGES.append(rec)

def challenge_inc(uid: int):
    ch = challenge_get(uid)
    if not ch: return
    try:
        done = int(str(ch.get("days_done") or "0")) + 1
        length = int(str(ch.get("length_days") or "7"))
    except Exception:
        done, length = 1, 7
    ch["days_done"] = str(done)
    if done >= length:
        ch["status"] = "done"
    if SHEETS_ENABLED:
        vals = ws_challenges.get_all_records()
        hdr = _headers(ws_challenges)
        for i, r in enumerate(vals, start=2):
            if r.get("challenge_id")==ch["challenge_id"]:
                ws_challenges.update_cell(i, hdr.index("days_done")+1, ch["days_done"])
                ws_challenges.update_cell(i, hdr.index("status")+1, ch["status"])
                break


# ---------- Полезные подсказки (скин/день) ----------
def _get_skin_tip(lang: str, sex: str, age: int) -> str:
    if lang not in T: lang = "en"
    if age >= 40:
        return {"ru":"SPF 30+ ежедневно и ретиноид 2–3 раза в неделю.","uk":"SPF 30+ щодня та ретиноїд 2–3 рази на тиждень.",
                "en":"SPF 30+ daily and a retinoid 2–3×/week.","es":"SPF 30+ diario y retinoide 2–3×/sem."}[lang]
    return {"ru":"SPF 30+ и мягкий ниацинамид 2–3 раза в неделю.","uk":"SPF 30+ і м'який ніацинамід 2–3 рази на тиждень.",
            "en":"SPF 30+ and gentle niacinamide 2–3×/week.","es":"SPF 30+ y niacinamida suave 2–3×/sem."}[lang]

def _get_daily_tip(prof: dict, lang: str) -> str:
    goal = (prof.get("goal") or "").lower()
    opts = {
        "sleep": ["20–30 мин без экрана перед сном.","Тёплый душ за 60 мин помогает уснуть."],
        "energy": ["Стакан воды и 5-мин прогулка.","2× быстрые растяжки — взбодрят."],
        "weight": ["Овощи к каждому приёму пищи.","Больше белка утром — меньше тяги к сладкому."],
        "strength": ["2–3 подхода приседаний дома.","Планка 3× по 20–40с."],
    }
    arr = opts.get(goal, ["Короткая прогулка на свежем воздухе.", "Стакан воды — для мозгов полезно."])
    return random.choice(arr)


# ---------- Планировщики ----------
def _has_jq_ctx(context_or_app) -> bool:
    try:
        jq = context_or_app.job_queue if hasattr(context_or_app, "job_queue") else context_or_app.application.job_queue
        return jq is not None
    except Exception:
        return False

def _sec_until_next_local(uid: int, hhmm: str) -> int:
    now = user_local_now(uid)
    hh, mm = hhmm_tuple(hhmm)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    # тихие часы
    qh = _user_quiet_hours(uid)
    target = adjust_out_of_quiet(target, qh)
    return max(5, int((local_to_utc_dt(uid, target) - utcnow()).total_seconds()))

def schedule_daily_checkin(app, uid: int, tz_off: int, hhmm: str, lang: str):
    # убираем старые
    for j in app.job_queue.get_jobs_by_name(f"daily_{uid}"):
        j.schedule_removal()
    delay = _sec_until_next_local(uid, hhmm)
    app.job_queue.run_repeating(
        job_daily_checkin,
        interval=24*3600,
        first=delay,
        name=f"daily_{uid}",
        data={"user_id": uid, "lang": lang}
    )
    logging.info(f"scheduled daily check-in for {uid} in {delay}s")

def schedule_evening_checkin(app, uid: int, tz_off: int, hhmm: str, lang: str):
    for j in app.job_queue.get_jobs_by_name(f"evening_{uid}"):
        j.schedule_removal()
    delay = _sec_until_next_local(uid, hhmm)
    app.job_queue.run_repeating(
        job_evening_checkin,
        interval=24*3600,
        first=delay,
        name=f"evening_{uid}",
        data={"user_id": uid, "lang": lang}
    )
    logging.info(f"scheduled evening check-in for {uid} in {delay}s")

def schedule_from_sheet_on_start(app):
    try:
        # Восстановим по Users
        if SHEETS_ENABLED:
            rows = ws_users.get_all_records()
            for r in rows:
                uid = int(str(r.get("user_id") or "0") or "0")
                if uid <= 0: continue
                lang = norm_lang(r.get("lang") or "en")
                tz = int(str(r.get("tz_offset") or "0") or "0")
                hh = (r.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
                if (r.get("paused") or "no").lower() != "yes":
                    schedule_daily_checkin(app, uid, tz, hh, lang)
                eh = (r.get("evening_hour") or "").strip()
                if eh:
                    schedule_evening_checkin(app, uid, tz, eh, lang)
        else:
            for uid, u in MEM_USERS.items():
                lang = norm_lang(u.get("lang") or "en")
                tz = int(str(u.get("tz_offset") or "0") or "0")
                hh = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
                if (u.get("paused") or "no").lower() != "yes":
                    schedule_daily_checkin(app, uid, tz, hh, lang)
                eh = (u.get("evening_hour") or "").strip()
                if eh:
                    schedule_evening_checkin(app, uid, tz, eh, lang)
    except Exception as e:
        logging.warning(f"schedule_from_sheet_on_start failed: {e}")


# ---------- One-off напоминание ----------
async def job_oneoff_reminder(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id"); rid = d.get("reminder_id")
    if not uid:
        return
    try:
        await context.bot.send_message(uid, T[_user_lang(uid)]["thanks"])
    except Exception as e:
        logging.error(f"job_oneoff_reminder send error: {e}")
    # отметить выполненным
    if SHEETS_ENABLED and rid:
        try:
            vals = ws_reminders.get_all_records()
            hdr = _headers(ws_reminders)
            for i, r in enumerate(vals, start=2):
                if r.get("id")==rid:
                    ws_reminders.update_cell(i, hdr.index("status")+1, "done")
                    break
        except Exception as e:
            logging.warning(f"reminder status update failed: {e}")
    else:
        for r in MEM_REMINDERS:
            if r.get("id")==rid:
                r["status"]="done"


# ---------- Утренний джоб (перекрывает версию из Ч.1, добавляет skip/больше кнопок) ----------
async def job_daily_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id"); lang = norm_lang((d.get("lang") or "en"))
    if not uid:
        return
    u = users_get(uid)
    if (u.get("paused") or "").lower() == "yes":
        return

    # Сообщение + метрика жизни (иногда)
    greet = T[lang]["daily_gm"]
    try:
        prof = profiles_get(uid) or {}
        # с вероятностью 1/4 показать метрику
        if random.random() < 0.25:
            # Простейшая метрика по age (если birth_date нет)
            bdate = (prof.get("birth_date") or "").strip()
            days = 0
            if re.match(r"^\d{4}-\d{2}-\d{2}$", bdate):
                days = (date.today() - datetime.strptime(bdate, "%Y-%m-%d").date()).days
            else:
                try:
                    age = int(re.findall(r"\d+", str(prof.get("age") or "0"))[0])
                    days = age * 365
                except Exception:
                    days = 0
            percent = min(100, round((days / 36500) * 100, 1)) if days>0 else 0
            bar = "█" * max(1, int(percent/8)) + "░" * (12 - max(1, int(percent/8)))
            extra = f"\n{('Сегодня твой' if lang=='ru' else 'Today is your')} {days}-th day. {bar} {percent}%"
            greet += extra
    except Exception:
        pass

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👍", callback_data="gm|mood|excellent"),
         InlineKeyboardButton("🙂", callback_data="gm|mood|ok"),
         InlineKeyboardButton("😐", callback_data="gm|mood|tired"),
         InlineKeyboardButton("🤕", callback_data="gm|mood|pain")],
        [InlineKeyboardButton("⏭", callback_data="gm|skip")],
        [InlineKeyboardButton(T[lang]["gm_evening_btn"], callback_data="youth:gm_evening")]
    ])
    try:
        await context.bot.send_message(uid, greet, reply_markup=kb)
    except Exception as e:
        logging.error(f"job_daily_checkin send error: {e}")


# ---------- streak helper ----------
def _bump_streak(uid: int, did_answer: bool):
    prof = profiles_get(uid) or {}
    today = date.today().isoformat()
    last = (prof.get("gm_last_date") or "")
    streak = int(str(prof.get("streak") or "0") or "0")
    best = int(str(prof.get("streak_best") or "0") or "0")
    if not did_answer:
        # skip не меняет
        profiles_upsert(uid, {"gm_last_date": today})
        return
    if last == today:
        # уже считали
        return
    if last and (datetime.strptime(today, "%Y-%m-%d").date() - datetime.strptime(last, "%Y-%m-%d").date()).days == 1:
        streak += 1
    else:
        streak = 1
    best = max(best, streak)
    updates = {"streak": str(streak), "streak_best": str(best), "gm_last_date": today}
    profiles_upsert(uid, updates)
    # бонус за 3 дня подряд
    if streak == 3:
        try:
            lang = _user_lang(uid)
            await_text = {
                "ru":"Молодец! 3 дня подряд — бонусный совет: короткая зарядка утром повышает концентрацию целый день.",
                "uk":"Супер! 3 дні підряд — бонусна порада: ранкова розминка підвищує концентрацію на весь день.",
                "en":"Nice! 3 days streak — bonus tip: a short morning warm-up boosts focus for the whole day.",
                "es":"¡Bien! Racha de 3 días — consejo bono: un calentamiento matutino breve mejora el enfoque."
            }[lang]
            # «ленивая» доставка: положим в память — отдадим при следующем сообщении
            s = sessions.setdefault(uid, {})
            s["bonus_msg"] = await_text
        except Exception:
            pass


# ---------- Расширяем cb_handler для гибрид-кнопок, mini и gm ----------
# (Перекрывает существующую функцию добивкой новых веток; неизменённые ветки останутся рабочими)
_old_cb_handler = cb_handler
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    lang = _user_lang(uid)
    data = (q.data or "")

    # --- новые пункты «гибрид-общения» ---
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
                [InlineKeyboardButton(T[lang]["act_rem_4h"],  callback_data="h60|rem|4h")],
                [InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="h60|rem|eve")],
                [InlineKeyboardButton(T[lang]["act_rem_morn"],callback_data="h60|rem|morn")]
            ])
            await context.bot.send_message(uid, T[lang]["remind_when"], reply_markup=kb)
            return
        # если это не новые — отдаём на старую обработку
    # --- доп. ветки мини-опроса (если их не было) ---
    if data.startswith("mini|"):
        parts = data.split("|")
        action = parts[1] if len(parts)>1 else ""
        key = parts[2] if len(parts)>2 else ""
        if action == "write":
            s = sessions.setdefault(uid, {"mini_active": True, "mini_step": 0, "mini_answers": {}})
            s["mini_wait_key"] = key
            await q.edit_message_text(T[lang]["write"] + "…")
            return
        if action == "choose":
            value = parts[3] if len(parts)>3 else ""
            mini_handle_choice(uid, key, value)
            await ask_next_mini(context, update.effective_chat.id, lang, uid)
            return
        if action == "skip":
            s = sessions.setdefault(uid, {"mini_active": True, "mini_step": 0, "mini_answers": {}})
            s["mini_step"] = int(s.get("mini_step", 0)) + 1
            await ask_next_mini(context, update.effective_chat.id, lang, uid)
            return

    # --- GM расширение: новые статусы и skip ---
    if data.startswith("gm|"):
        _, kind, *rest = data.split("|")
        if kind == "mood":
            mood = rest[0] if rest else "ok"
            daily_add(iso(utcnow()), uid, mood=mood, comment="", energy=None)
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(uid, T[lang]["mood_thanks"])
            _bump_streak(uid, did_answer=True)
            await show_quickbar(context, uid, lang)
            return
        if kind == "skip":
            await q.edit_message_reply_markup(None)
            _bump_streak(uid, did_answer=False)
            await context.bot.send_message(uid, T[lang]["thanks"])
            await show_quickbar(context, uid, lang)
            return

    # --- Принятие плана Health60 также логируем эпизод ---
    if data.startswith("h60|accept|"):
        # создаём эпизод, если ещё нет
        s = sessions.setdefault(uid, {})
        if not s.get("last_eid"):
            s["last_eid"] = episode_create(uid, topic="h60", severity=5, red="")
        await q.edit_message_reply_markup(None)
        await context.bot.send_message(uid, T[lang]["thanks"])
        await show_quickbar(context, uid, lang)
        return

    # иначе — стандартная старая обработка
    await _old_cb_handler(update, context)


# ---------- Добивка msg_text: мини-инпуты, lab-город, бонусы ----------
_old_msg_text = msg_text
async def msg_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    text = (update.message.text or "").strip()

    # город для лаборатории
    s = sessions.setdefault(uid, {})
    if s.get("await_lab_city"):
        s["await_lab_city"] = False
        profiles_upsert(uid, {"city": text})
        await update.message.reply_text(T[lang]["act_saved"])
        await show_quickbar(context, update.effective_chat.id, lang)
        return

    # мини-опрос: свободный ввод для write-полей
    if s.get("mini_active") and s.get("mini_wait_key"):
        key = s["mini_wait_key"]
        s["mini_wait_key"] = None
        s["mini_answers"][key] = text
        s["mini_step"] = int(s.get("mini_step", 0)) + 1
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return

    # бонусное сообщение за streak (ленивая доставка)
    if s.get("bonus_msg"):
        await update.message.reply_text(s.pop("bonus_msg"))

    # ожидание Health60 -> план + quickbar (оставляем вашу реализацию и дополнительно quickbar)
    if s.get("awaiting_h60_text"):
        s["awaiting_h60_text"] = False
        await _process_health60(uid, lang, text, update.message)
        await show_quickbar(context, update.effective_chat.id, lang)
        return

    # остальное — отдать в стандартный маршрутизатор + quickbar
    await _old_msg_text(update, context)
    await show_quickbar(context, update.effective_chat.id, lang)


# ---------- Утилиты обновления last_seen (если нет в Ч.1) ----------
def update_last_seen(uid: int):
    try:
        users_set(uid, "last_seen", iso(utcnow()))
    except Exception:
        pass
