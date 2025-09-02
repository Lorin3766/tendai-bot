# -*- coding: utf-8 -*-
import os, re, json, uuid, logging, math, random
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

from openai import OpenAI
# === PRO-INTAKE ===
from intake_pro import register_intake_pro

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

# Evening target local time (used if user presses "remind this evening")
DEFAULT_EVENING_LOCAL = "20:00"

# Quiet hours default (local) if not set in profile (22:00-08:00)
DEFAULT_QUIET_HOURS = "22:00-08:00"

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
        "help": "Short checkups, 24–48h plans, reminders, daily check-ins.\nCommands: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /ru /uk /en /es\nPlus: /energy /hydrate /skintip /cycle /youth",
        "privacy": "TendAI is not a medical service and can’t replace a doctor. We store minimal data for reminders. /delete_data to erase.",
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
        "daily_gm":"Good morning! Quick daily check-in:",
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
        # feedback
        "ask_fb":"Was this helpful?",
        "fb_thanks":"Thanks for your feedback! ✅",
        "fb_write":"Write a short feedback message:",
        "fb_good":"👍 Like",
        "fb_bad":"👎 Dislike",
        "fb_free":"📝 Feedback",
        # Health60
        "h60_btn": "Health in 60 seconds",
        "h60_intro": "Write briefly what bothers you (e.g., “headache”, “fatigue”, “stomach pain”). I’ll give you 3 key tips in 60 seconds.",
        "h60_t1": "Possible causes",
        "h60_t2": "Do now (next 24–48h)",
        "h60_t3": "When to see a doctor",
        "h60_serious": "Serious to rule out",

        # === Youth Pack additions ===
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
    },
    "ru": {
        "welcome":"Привет! Я TendAI — ассистент здоровья и долголетия.\nРасскажи, что беспокоит; я подскажу. Сначала короткий опрос (~40с), чтобы советы были точнее.",
        "help":"Короткие проверки, план на 24–48 ч, напоминания, ежедневные чек-ины.\nКоманды: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +3 /health60 /ru /uk /en /es\nДоп.: /energy /hydrate /skintip /cycle /youth",
        "privacy":"TendAI не заменяет врача. Храним минимум данных для напоминаний. /delete_data — удалить.",
        "paused_on":"Напоминания поставлены на паузу. /resume — включить.",
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
        "daily_gm":"Доброе утро! Быстрый чек-ин:",
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
        # feedback
        "ask_fb":"Это было полезно?",
        "fb_thanks":"Спасибо за отзыв! ✅",
        "fb_write":"Напишите короткий отзыв одним сообщением:",
        "fb_good":"👍 Нравится",
        "fb_bad":"👎 Не полезно",
        "fb_free":"📝 Отзыв",
        # Health60
        "h60_btn": "Здоровье за 60 секунд",
        "h60_intro": "Коротко напишите, что беспокоит (например: «болит голова», «усталость», «боль в животе»). Я дам 3 ключевых совета за 60 секунд.",
        "h60_t1": "Возможные причины",
        "h60_t2": "Что сделать сейчас (24–48 ч)",
        "h60_t3": "Когда обратиться к врачу",
        "h60_serious": "Что серьёзное исключить",

        # === Youth Pack additions ===
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
    },
    "uk": {
        "welcome":"Привіт! Я TendAI — асистент здоров’я та довголіття.\nРозкажи, що турбує; я підкажу. Спершу швидкий опитник (~40с) для точніших порад.",
        "help":"Короткі перевірки, план на 24–48 год, нагадування, щоденні чек-іни.\nКоманди: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /ru /uk /en /es\nДод.: /energy /hydrate /skintip /cycle /youth",
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
        "daily_gm":"Доброго ранку! Швидкий чек-ін:",
        "mood_good":"😃 Добре","mood_ok":"😐 Нормально","mood_bad":"😣 Погано","mood_note":"✍️ Коментар",
        "mood_thanks":"Дякую! Гарного дня 👋",
        "triage_pain_q1":"Де болить?",
        "triage_pain_q1_opts":["Голова","Горло","Спина","Живіт","Інше"],
        "triage_pain_q2":"Який характер болю?",
        "triage_pain_q2_opts":["Тупий","Гострий","Пульсуючий","Тиснучий"],
        "triage_pain_q3":"Як довго триває?",
        "triage_pain_q3_opts":["<3год","3–24год",">1 дня",">1 тижня"],
        "triage_pain_q4":"Оцініть біль (0–10):",
        "triage_pain_q5":"Є щось із цього зараз?",
        "triage_pain_q5_opts":["Висока температура","Блювання","Слабкість/оніміння","Проблеми з мовою/зором","Травма","Немає"],
        "plan_header":"Ваш план на 24–48 год:",
        "plan_accept":"Готові спробувати сьогодні?",
        "accept_opts":["✅ Так","🔁 Пізніше","✖️ Ні"],
        "remind_when":"Коли нагадати та спитати самопочуття?",
        "remind_opts":["через 4 год","увечері","завтра вранці","не треба"],
        "thanks":"Прийнято 🙌",
        "checkin_ping":"Коротко: як зараз за шкалою 0–10?",
        "checkin_better":"Чудово! Продовжуємо 💪",
        "checkin_worse":"Якщо є «червоні прапорці» або біль ≥7/10 — краще звернутися до лікаря.",
        "act_rem_4h":"⏰ Нагадати через 4 год",
        "act_rem_eve":"⏰ Сьогодні ввечері",
        "act_rem_morn":"⏰ Завтра зранку",
        "act_save_episode":"💾 Зберегти епізод",
        "act_ex_neck":"🧘 5-хв вправи для шиї",
        "act_find_lab":"🧪 Знайти лабораторію",
        "act_er":"🚑 Коли терміново в швидку",
        "act_city_prompt":"Напишіть місто/район, щоб порадити лабораторію (текстом).",
        "act_saved":"Збережено.",
        "er_text":"Якщо посилюється, сильна задишка, біль у грудях, сплутаність, тривала висока температура — якнайшвидше до невідкладної/швидкої.",
        "px":"З урахуванням профілю: {sex}, {age} р.; мета — {goal}.",
        "back":"◀ Назад",
        "exit":"Вийти",
        # feedback
        "ask_fb":"Чи було це корисно?",
        "fb_thanks":"Дякую за відгук! ✅",
        "fb_write":"Напишіть короткий відгук одним повідомленням:",
        "fb_good":"👍 Подобається",
        "fb_bad":"👎 Не корисно",
        "fb_free":"📝 Відгук",
        # Health60
        "h60_btn": "Здоров’я за 60 секунд",
        "h60_intro": "Коротко напишіть, що турбує (наприклад: «болить голова», «втома», «біль у животі»). Дам 3 ключові поради за 60 секунд.",
        "h60_t1": "Можливі причини",
        "h60_t2": "Що зробити зараз (24–48 год)",
        "h60_t3": "Коли звернутися до лікаря",
        "h60_serious": "Що серйозне виключити",

        # === Youth Pack additions ===
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
        "challenge_started": "Челендж запущено! Я враховуватиму ваші щоденні чек-іни.",
        "challenge_progress": "Прогрес челенджу: {d}/{len} днів.",
        "cycle_btn": "🩸 Цикл",
        "cycle_consent": "Бажаєте відстежувати цикл для м’яких порад у потрібні дні?",
        "cycle_ask_last": "Вкажіть дату останніх менструацій (РРРР-ММ-ДД):",
        "cycle_ask_len": "Середня тривалість циклу (наприклад, 28):",
        "cycle_saved": "Відстеження циклу збережено.",
        "quiet_saved": "Тихі години збережено: {qh}",
        "set_quiet_btn": "🌙 Тихі години",
        "ask_quiet": "Введіть тихі години як ГГ:ХХ-ГГ:ХХ (локально), напр. 22:00-08:00",
    },
}
T["es"] = T["en"]  # простая заглушка

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
        render_cb = context.application.bot_data.get("render_menu_cb")
        if callable(render_cb):
            await render_cb(update, context)
        else:
            await context.application.bot.send_message(q.message.chat_id, "/start")

# === NEW: MINI-INTAKE (с первого сообщения) ===
MINI_KEYS = ["sex","age","goal","diet_focus","steps_target"]
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
}

async def start_mini_intake(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: int):
    sessions[uid] = {"mini_active": True, "mini_step": 0, "mini_answers": {}}
    await context.bot.send_message(chat_id, {
        "ru":"🔎 Мини-опрос для персонализации (4–5 кликов).",
        "uk":"🔎 Міні-опитування для персоналізації (4–5 кліків).",
        "en":"🔎 Mini-intake for personalization (4–5 taps).",
        "es":"🔎 Mini-intake para personalización (4–5 toques).",
    }[lang], reply_markup=ReplyKeyboardRemove())
    await ask_next_mini(context, chat_id, lang, uid)

def build_mini_kb(lang: str, key: str) -> InlineKeyboardMarkup:
    opts = MINI_STEPS[key][lang]
    rows, row = [], []
    for label, val in opts:
        row.append(InlineKeyboardButton(label, callback_data=f"mini|choose|{key}|{val}"))
        if len(row) == 3:
            rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([InlineKeyboardButton(T[lang]["skip"], callback_data=f"mini|skip|{key}")])
    return InlineKeyboardMarkup(rows)

async def ask_next_mini(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: int):
    s = sessions.get(uid, {})
    step_idx = s.get("mini_step", 0)
    if step_idx >= len(MINI_KEYS):
        # save into Profiles and finish
        profiles_upsert(uid, s.get("mini_answers", {}))
        sessions[uid]["mini_active"] = False
        await context.bot.send_message(chat_id, T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
        # show gate for full PRO intake
        if not context.user_data.get(GATE_FLAG_KEY):
            await gate_show(Update(update_id=0, message=None), context)  # safe fallback
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

# === Сохраняем gspread client и id таблицы для register_intake_pro ===
GSPREAD_CLIENT: Optional[gspread.client.Client] = None
SPREADSHEET_ID_FOR_INTAKE: str = ""

def _ws_headers(ws):
    return ws.row_values(1) if ws and ws.row_values(1) else []

def _ws_ensure_columns(ws, desired_headers: List[str]):
    """Ensure worksheet has at least all desired headers; append missing at end."""
    try:
        current = _ws_headers(ws)
        if not current:
            ws.append_row(desired_headers)
            return
        missing = [h for h in desired_headers if h not in current]
        if missing:
            # add missing columns at end
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
                ws = ss.add_worksheet(title=title, rows=2000, cols=max(20, len(headers)))
                ws.append_row(headers)
            if not ws.get_all_values():
                ws.append_row(headers)
            # ensure new headers (idempotent)
            _ws_ensure_columns(ws, headers)
            return ws

        ws_feedback = _ensure_ws("Feedback", ["timestamp","user_id","name","username","rating","comment"])
        ws_users = _ensure_ws("Users", ["user_id","username","lang","consent","tz_offset","checkin_hour","paused"])
        # Profiles — extended headers with personalization fields
        ws_profiles = _ensure_ws("Profiles", [
            "user_id","sex","age","goal","goals","conditions","meds","allergies",
            "sleep","activity","diet","diet_focus","steps_target",
            "cycle_enabled","cycle_last_date","cycle_avg_len",
            "quiet_hours","consent_flags","notes","updated_at"
        ])
        ws_episodes = _ensure_ws("Episodes", ["episode_id","user_id","topic","started_at","baseline_severity","red_flags",
                                              "plan_accepted","target","reminder_at","next_checkin_at","status","last_update","notes"])
        # Daily checkins sheet extended with energy
        ws_daily = _ensure_ws("DailyCheckins",["timestamp","user_id","mood","energy","comment"])
        # Optional: Rules (evidence-based tips)
        ws_rules = _ensure_ws("Rules", ["rule_id","topic","segment","trigger","advice_text","citation","last_updated","enabled"])
        # Optional: Challenges
        ws_challenges = _ensure_ws("Challenges", ["user_id","challenge_id","name","start_date","length_days","days_done","status"])

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
        "paused": "no"
    }

    if SHEETS_ENABLED:
        vals = ws_users.get_all_records()
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                ws_users.update(f"A{i}:G{i}", [[base[k] for k in _headers(ws_users)]])
                return
        ws_users.append_row([base[k] for k in _headers(ws_users)])
    else:
        MEM_USERS[uid] = base

def users_set(uid: int, field: str, value: str):
    if SHEETS_ENABLED:
        vals = ws_users.get_all_records()
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                hdr = _headers(ws_users)
                # expand headers if needed
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

def profiles_upsert(uid: int, data: dict):
    if SHEETS_ENABLED:
        hdr = _headers(ws_profiles)
        current, idx = None, None
        for i, r in enumerate(ws_profiles.get_all_records(), start=2):
            if str(r.get("user_id")) == str(uid):
                current, idx = r, i
                break
        if not current:
            current = {"user_id": str(uid)}
        for k,v in data.items():
            if k not in hdr:
                _ws_ensure_columns(ws_profiles, hdr + [k])
                hdr = _headers(ws_profiles)
            current[k] = "" if v is None else (", ".join(v) if isinstance(v,list) else str(v))
        current["updated_at"] = iso(utcnow())

        values = [current.get(h,"") for h in hdr]
        end_col = gsu.rowcol_to_a1(1, len(hdr)).rstrip("1")
        if idx:
            ws_profiles.update(f"A{idx}:{end_col}{idx}", [values])
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
        ws_episodes.append_row([rec[k] for k in _headers(ws_episodes)])
    else:
        MEM_EPISODES.append(rec)
    return eid

def episode_find_open(uid: int) -> Optional[dict]:
    if SHEETS_ENABLED:
        for r in ws_episodes.get_all_records():
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
            _ws_ensure_columns(ws_episodes, hdr + [field]); hdr = _headers(ws_episodes)
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
        ws_reminders.append_row([rec[k] for k in _headers(ws_reminders)])
    else:
        MEM_REMINDERS.append(rec)
    return rid

def reminders_all_records():
    if SHEETS_ENABLED:
        return ws_reminders.get_all_records()
    return MEM_REMINDERS.copy()

def reminders_mark_sent(rid: str):
    if SHEETS_ENABLED:
        vals = ws_reminders.get_all_values()
        hdr = vals[0]
        for i in range(2, len(vals)+1):
            if ws_reminders.cell(i,1).value == rid:
                ws_reminders.update_cell(i,hdr.index("status")+1,"sent"); return
    else:
        for r in MEM_REMINDERS:
            if r["id"]==rid:
                r["status"]="sent"; return

def daily_add(ts, uid, mood, comment, energy: Optional[int]=None):
    if SHEETS_ENABLED:
        hdr = _headers(ws_daily)
        row = {"timestamp":ts,"user_id":str(uid),"mood":mood,"energy":("" if energy is None else str(energy)),"comment":comment or ""}
        ws_daily.append_row([row.get(h,"") for h in hdr])
    else:
        MEM_DAILY.append({"timestamp":ts,"user_id":str(uid),"mood":mood,"energy":energy,"comment":comment or ""})

# ---------- Challenges ----------
def challenge_start(uid: int, name="hydrate7", length_days=7):
    rec = {"user_id":str(uid),"challenge_id":f"{uid}-hydr7","name":name,
           "start_date":date.today().isoformat(),"length_days":str(length_days),
           "days_done":"0","status":"active"}
    if SHEETS_ENABLED:
        ws_challenges.append_row([rec[k] for k in _headers(ws_challenges)])
    else:
        MEM_CHALLENGES.append(rec)

def challenge_get(uid: int):
    if SHEETS_ENABLED:
        for r in ws_challenges.get_all_records():
            if r.get("user_id")==str(uid) and r.get("status")=="active":
                return r
        return None
    for r in MEM_CHALLENGES:
        if r["user_id"]==str(uid) and r["status"]=="active":
            return r
    return None

def challenge_inc(uid: int):
    if SHEETS_ENABLED:
        vals = ws_challenges.get_all_values(); hdr = vals[0]
        for i in range(2, len(vals)+1):
            if ws_challenges.cell(i, hdr.index("user_id")+1).value == str(uid) and \
               ws_challenges.cell(i, hdr.index("status")+1).value == "active":
                cur = int(ws_challenges.cell(i, hdr.index("days_done")+1).value or "0")
                ws_challenges.update_cell(i, hdr.index("days_done")+1, str(cur+1)); return cur+1
        return 0
    else:
        for r in MEM_CHALLENGES:
            if r["user_id"]==str(uid) and r["status"]=="active":
                r["days_done"]=str(int(r["days_done"])+1); return int(r["days_done"])
        return 0

# --------- JobQueue helper ----------
def _has_jq_app(app) -> bool:
    return getattr(app, "job_queue", None) is not None

def _has_jq_ctx(context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        return getattr(context.application, "job_queue", None) is not None
    except Exception:
        return False

# --------- Time helpers (tz + quiet hours) ---------
def hhmm_tuple(hhmm:str)->Tuple[int,int]:
    m = re.search(r'([01]?\d|2[0-3]):([0-5]\d)', hhmm.strip())
    return (int(m.group(1)), int(m.group(2))) if m else (8,30)

def local_to_utc_hour_min(tz_offset_hours:int, hhmm:str)->Tuple[int,int]:
    h,m = hhmm_tuple(hhmm); return ((h - tz_offset_hours) % 24, m)

def parse_quiet_hours(qh: str)->Tuple[Tuple[int,int], Tuple[int,int]]:
    # "22:00-08:00" -> ((22,0),(8,0))
    try:
        a,b = qh.split("-")
        return hhmm_tuple(a), hhmm_tuple(b)
    except:
        return hhmm_tuple(DEFAULT_QUIET_HOURS.split("-")[0]), hhmm_tuple(DEFAULT_QUIET_HOURS.split("-")[1])

def is_in_quiet(local_dt: datetime, qh: str)->bool:
    (sh,sm),(eh,em) = parse_quiet_hours(qh)
    start = local_dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end   = local_dt.replace(hour=eh, minute=em, second=0, microsecond=0)
    if (sh,sm) <= (eh,em):
        return start <= local_dt < end
    # overnight wrap
    return local_dt >= start or local_dt < end

def adjust_out_of_quiet(local_dt: datetime, qh: str)->datetime:
    if not is_in_quiet(local_dt, qh):
        return local_dt
    (sh,sm),(eh,em) = parse_quiet_hours(qh)
    # If in quiet, move to next end
    end = local_dt.replace(hour=eh, minute=em, second=0, microsecond=0)
    if (sh,sm) > (eh,em) and local_dt.time() < dtime(eh,em):
        # quiet wraps overnight; if before end, today end already in morning
        return end
    return end + timedelta(days=1)

# --------- Scheduling ---------
def schedule_from_sheet_on_start(app):
    if not _has_jq_app(app):
        logging.warning("JobQueue not available – skip scheduling on start.")
        return

    now = utcnow()
    src = ws_episodes.get_all_records() if SHEETS_ENABLED else MEM_EPISODES
    for r in src:
        if r.get("status")!="open":
            continue
        eid = r.get("episode_id"); uid = int(r.get("user_id"))
        nca = r.get("next_checkin_at") or ""
        if not nca:
            continue
        try:
            dt_ = datetime.strptime(nca, "%Y-%m-%d %H:%M:%S%z")
        except:
            continue
        delay = max(60, (dt_-now).total_seconds())
        app.job_queue.run_once(job_checkin_episode, when=delay, data={"user_id":uid,"episode_id":eid})

    for r in reminders_all_records():
        if (r.get("status") or "")!="scheduled":
            continue
        uid = int(r.get("user_id")); rid=r.get("id")
        try:
            dt_ = datetime.strptime(r.get("when_utc"), "%Y-%m-%d %H:%M:%S%z")
        except:
            continue
        delay = max(60,(dt_-now).total_seconds())
        app.job_queue.run_once(job_oneoff_reminder, when=delay, data={"user_id":uid,"reminder_id":rid})

    src_u = ws_users.get_all_records() if SHEETS_ENABLED else list(MEM_USERS.values())
    for u in src_u:
        if (u.get("paused") or "").lower()=="yes":
            continue
        uid = int(u.get("user_id"))
        tz_off = int(str(u.get("tz_offset") or "0"))
        hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
        schedule_daily_checkin(app, uid, tz_off, hhmm, norm_lang(u.get("lang") or "en"))

def schedule_daily_checkin(app, uid:int, tz_off:int, hhmm_local:str, lang:str):
    if not _has_jq_app(app):
        logging.warning(f"JobQueue not available – skip daily scheduling for uid={uid}.")
        return
    for j in app.job_queue.get_jobs_by_name(f"daily_{uid}"):
        j.schedule_removal()
    h_utc, m_utc = local_to_utc_hour_min(tz_off, hhmm_local)
    t = dtime(hour=h_utc, minute=m_utc, tzinfo=timezone.utc)
    app.job_queue.run_daily(job_daily_checkin, time=t, name=f"daily_{uid}", data={"user_id":uid,"lang":lang})

# ------------- Jobs -------------
async def job_checkin_episode(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, eid = d.get("user_id"), d.get("episode_id")
    if not uid or not eid:
        return
    u = users_get(uid)
    if (u.get("paused") or "").lower()=="yes":
        return
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

def _get_daily_tip(profile: dict, lang: str) -> str:
    # Simple segmented tips; could be extended by Rules sheet
    goal = (profile.get("goal") or profile.get("goals") or "").lower()
    age = int(re.search(r"\d+", str(profile.get("age") or "0")).group(0)) if re.search(r"\d+", str(profile.get("age") or "")) else 0
    sex = (profile.get("sex") or "").lower()
    tips_common = {
        "en":[
            "Add 15–20 g protein to one meal today.",
            "Swap a sugary drink for water or tea.",
            "Add a fist of veggies at lunch.",
            "Take a 10-minute walk after meals.",
        ],
        "ru":[
            "Добавьте 15–20 г белка к одному приёму пищи.",
            "Замените сладкий напиток на воду или чай.",
            "Добавьте кулак овощей к обеду.",
            "10-мин прогулка после еды.",
        ],
        "uk":[
            "Додайте 15–20 г білка до одного прийому.",
            "Замініть солодкий напій на воду або чай.",
            "Додайте кулак овочів до обіду.",
            "10-хв прогулянка після їжі.",
        ],
        "es":[
            "Añade 15–20 g de proteína a una comida.",
            "Cambia la bebida azucarada por agua o té.",
            "Añade un puño de verduras al almuerzo.",
            "Camina 10 min tras la comida.",
        ],
    }[lang]
    if goal == "weight":
        tips = {
            "en":["Aim ≥25 g protein at breakfast.","Keep dinner carbs modest today."],
            "ru":["Завтрак ≥25 г белка.","Ужин с умеренными углеводами."],
            "uk":["Сніданок ≥25 г білка.","На вечерю помірні вуглеводи."],
            "es":["Desayuno ≥25 g proteína.","Cena con pocos carbohidratos."],
        }[lang]
    elif goal == "energy":
        tips = {
            "en":["Drink 300–500 ml water on waking.","Get 5–10 min daylight before noon."],
            "ru":["300–500 мл воды после пробуждения.","5–10 мин дневного света до полудня."],
            "uk":["300–500 мл води після пробудження.","5–10 хв денного світла до полудня."],
            "es":["300–500 ml de agua al despertar.","5–10 min de luz natural antes del mediodía."],
        }[lang]
    else:
        tips = []
    pool = tips + tips_common
    return random.choice(pool)

def _get_skin_tip(lang: str, sex: str, age: int) -> str:
    if lang=="ru":
        base = [
            "Ежедневно SPF 30+ утром; смывать вечером.",
            "Умывание тёплой водой, без жёсткого скраба.",
            "Ввести увлажняющий крем после душа.",
        ]
    elif lang=="uk":
        base = [
            "Щоденно SPF 30+ вранці; змивати ввечері.",
            "Вмивання теплою водою, без жорстких скрабів.",
            "Додайте зволожувальний крем після душу.",
        ]
    elif lang=="es":
        base = [
            "SPF 30+ cada mañana; retirar por la noche.",
            "Lavar con agua tibia, sin exfoliantes agresivos.",
            "Añade crema hidratante tras la ducha.",
        ]
    else:
        base = [
            "Use SPF 30+ each morning; cleanse at night.",
            "Wash with lukewarm water; skip harsh scrubs.",
            "Moisturizer after shower helps skin barrier.",
        ]
    return random.choice(base)

def _cycle_tip(lang: str, phase: str) -> Optional[str]:
    if phase=="luteal":
        return {"ru":"Лютеиновая фаза: чуть больше сна и воды; фокус на белке и железе.",
                "uk":"Лютеїнова фаза: трохи більше сну і води; білок та залізо.",
                "en":"Luteal phase: add sleep & hydration; focus on protein and iron.",
                "es":"Fase lútea: más sueño e hidratación; prioriza proteína y hierro."}[lang]
    if phase=="follicular":
        return {"ru":"Фолликулярная фаза: энергия ↑ — хорошо планировать тренировки.",
                "uk":"Фолікулярна фаза: енергія ↑ — добре планувати тренування.",
                "en":"Follicular phase: energy ↑ — good time for training.",
                "es":"Fase folicular: energía ↑ — buen momento para entrenar."}[lang]
    if phase=="ovulation":
        return {"ru":"Овуляция: поддержите белок и гидратацию.",
                "uk":"Овуляція: підтримайте білок та гідратацію.",
                "en":"Ovulation: support protein and hydration.",
                "es":"Ovulación: prioriza proteína e hidratación."}[lang]
    return None

def _cycle_phase(last_date_str: str, avg_len: int) -> Optional[str]:
    try:
        last = datetime.strptime(last_date_str, "%Y-%m-%d").date()
        day = (date.today() - last).days % max(avg_len, 21)
        if day < 10: return "follicular"
        if 10 <= day < 14: return "ovulation"
        return "luteal"
    except Exception:
        return None

async def job_daily_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, lang = d.get("user_id"), d.get("lang","en")
    u = users_get(uid)
    if (u.get("paused") or "").lower()=="yes":
        return

    # Compose dynamic text: tip + optional cycle line + challenge progress
    prof = profiles_get(uid)
    tip = _get_daily_tip(prof, lang)
    cycle_line = ""
    if (prof.get("cycle_enabled") or "").lower() == "yes" and (prof.get("cycle_last_date") or ""):
        phase = _cycle_phase(prof.get("cycle_last_date"), int(prof.get("cycle_avg_len") or "28"))
        if phase:
            ct = _cycle_tip(lang, phase)
            if ct:
                cycle_line = f"\n{ct}"

    ch = challenge_get(uid)
    ch_line = ""
    if ch:
        ch_line = "\n" + T[lang]["challenge_progress"].format(d=int(ch.get("days_done") or "0"), len=int(ch.get("length_days") or "7"))

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_good"], callback_data="mood|good"),
         InlineKeyboardButton(T[lang]["mood_ok"], callback_data="mood|ok"),
         InlineKeyboardButton(T[lang]["mood_bad"], callback_data="mood|bad")],
        [InlineKeyboardButton("⚡1", callback_data="energy|1"),
         InlineKeyboardButton("2", callback_data="energy|2"),
         InlineKeyboardButton("3", callback_data="energy|3"),
         InlineKeyboardButton("4", callback_data="energy|4"),
         InlineKeyboardButton("5", callback_data="energy|5")],
        [InlineKeyboardButton(T[lang]["gm_evening_btn"], callback_data="act|rem|evening"),
         InlineKeyboardButton(T[lang]["hydrate_btn"], callback_data="act|hydration")],
        [InlineKeyboardButton(T[lang]["skintip_btn"], callback_data="act|skintip"),
         InlineKeyboardButton(T[lang]["set_quiet_btn"], callback_data="act|quiet"),
         InlineKeyboardButton(T[lang]["cycle_btn"], callback_data="act|cycle")],
    ])
    try:
        await context.bot.send_message(
            uid,
            f"{T[lang]['daily_gm']}\n{T[lang]['daily_tip_prefix']} {tip}{cycle_line}{ch_line}",
            reply_markup=kb
        )
    except Exception as e:
        logging.error(f"daily checkin error: {e}")

# ------------- LLM Router (with personalization) -------------
SYS_ROUTER = (
    "You are TendAI — a concise, warm, professional health & longevity assistant (not a doctor). "
    "Always answer strictly in {lang}. Keep replies short (<=6 lines + up to 4 bullets). "
    "Personalize recommendations using the provided profile (sex/age/goal/conditions, diet_focus, steps_target). "
    "TRIAGE: ask 1–2 clarifiers first; advise ER only for clear red flags with high confidence. "
    "Return JSON ONLY like: "
    "{\"intent\":\"symptom\"|\"nutrition\"|\"sleep\"|\"labs\"|\"habits\"|\"longevity\"|\"other\","
    "\"assistant_reply\": \"string\", \"followups\": [\"string\"], \"needs_more\": true, "
    "\"red_flags\": false, \"confidence\": 0.0}"
)
def llm_router_answer(text: str, lang: str, profile: dict) -> dict:
    # Если LLM недоступен:
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


# ===== LLM ORCHESTRATOR FOR PAIN TRIAGE =====
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

def llm_decide_next_pain_step(user_text: str, lang: str, state: dict) -> Optional[dict]:
    if not oai:
        return None

    known = state.get("answers", {})
    opts = {
        "loc": T[lang]["triage_pain_q1_opts"],
        "kind": T[lang]["triage_pain_q2_opts"],
        "duration": T[lang]["triage_pain_q3_opts"],
        "red": T[lang]["triage_pain_q5_opts"],
    }
    sys = (
        "You are a clinical triage step planner. "
        f"Language: {lang}. Reply in the user's language. "
        "You get partial fields for a PAIN complaint and a new user message. "
        "Extract any fields present from the message. Keep labels EXACTLY as in the allowed options. "
        "Field keys: loc, kind, duration, severity (0-10), red. "
        "Then decide the NEXT missing field to ask (order: loc -> kind -> duration -> severity -> red). "
        "Return STRICT JSON ONLY with keys updates, ask, kb. "
        "kb must be one of: painloc, painkind, paindur, num, painrf, done. "
        "If enough info to produce a plan (we have severity and red), set kb='done' and ask=''.\n\n"
        f"Allowed options:\nloc: {opts['loc']}\nkind: {opts['kind']}\nduration: {opts['duration']}\nred: {opts['red']}\n"
    )
    user = {"known": known, "message": user_text}
    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.1,
            max_tokens=220,
            response_format={"type":"json_object"},
            messages=[
                {"role":"system","content":sys},
                {"role":"user","content":json.dumps(user, ensure_ascii=False)}
            ]
        )
        out = resp.choices[0].message.content.strip()
        data = json.loads(out)
        if "updates" in data and isinstance(data["updates"], dict) and "severity" in data["updates"]:
            try:
                sv = int(data["updates"]["severity"])
                data["updates"]["severity"] = max(0, min(10, sv))
            except:
                data["updates"].pop("severity", None)
        return data
    except Exception as e:
        logging.error(f"llm_decide_next_pain_step error: {e}")
        return None

# ----- Synonyms fallback for pain triage -----
PAIN_LOC_SYNS = {
    "ru": {
        "Голова": ["голова","голове","головная","мигрень","висок","темя","лоб"],
        "Горло": ["горло","в горле","ангина","тонзиллит"],
        "Спина": ["спина","в спине","поясница","пояснич","лопатк","позвон","сзади"],
        "Живот": ["живот","внизу живота","эпигастр","желудок","киш","подребер"],
        "Другое": ["другое"]
    },
    "uk": {
        "Голова": ["голова","в голові","мігрень","скроня","лоб"],
        "Горло": ["горло","в горлі","ангіна","тонзиліт"],
        "Спина": ["спина","поперек","лопатк","хребет"],
        "Живіт": ["живіт","внизу живота","шлунок","киш"],
        "Інше": ["інше"]
    },
    "en": {
        "Head": ["head","headache","migraine","temple","forehead"],
        "Throat": ["throat","sore throat","tonsil"],
        "Back": ["back","lower back","spine","shoulder blade"],
        "Belly": ["belly","stomach","abdomen","tummy","epigastr"],
        "Other": ["other"]
    },
    "es": {
        "Head": ["cabeza","dolor de cabeza","migraña","sien","frente"],
        "Throat": ["garganta","dolor de garganta","amígdala"],
        "Back": ["espalda","lumbar","columna","omóplato"],
        "Belly": ["vientre","estómago","abdomen","barriga","epigastrio"],
        "Other": ["otro","otra","otros"]
    }
}

PAIN_KIND_SYNS = {
    "ru": {
        "Тупая": ["туп","ноющ","тянущ"],
        "Острая": ["остр","колющ","режущ"],
        "Пульсирующая": ["пульс"],
        "Давящая": ["давит","сдавлив","стягив"]
    },
    "uk": {
        "Тупий": ["туп","ниюч"],
        "Гострий": ["гостр","колюч","ріжуч"],
        "Пульсуючий": ["пульс"],
        "Тиснучий": ["тисн","стискає"]
    },
    "en": {
        "Dull": ["dull","aching","pulling"],
        "Sharp": ["sharp","stabbing","cutting"],
        "Pulsating": ["puls","throbb"],
        "Pressing": ["press","tight","squeez"]
    },
    "es": {
        "Dull": ["sordo","leve","molesto"],
        "Sharp": ["agudo","punzante","cortante"],
        "Pulsating": ["pulsátil","palpitante"],
        "Pressing": ["opresivo","presión","apretado"]
    }
}

RED_FLAG_SYNS = {
    "ru": {
        "Высокая температура": ["высокая темп","жар","39","40"],
        "Рвота": ["рвота","тошнит и рв","блюёт","блюет"],
        "Слабость/онемение": ["онем","слабость в конеч","провисло","асимметрия"],
        "Нарушение речи/зрения": ["речь","говорить не","зрение","двоит","искры"],
        "Травма": ["травма","удар","падение","авария"],
        "Нет": ["нет","ничего","none","нема","відсут"]
    },
    "uk": {
        "Висока температура": ["висока темп","жар","39","40"],
        "Блювання": ["блюван","рвота"],
        "Слабкість/оніміння": ["онім","слабк","провисло"],
        "Проблеми з мовою/зором": ["мова","говорити","зір","двоїть"],
        "Травма": ["травма","удар","падіння","аварія"],
        "Немає": ["нема","ні","відсут","none"]
    },
    "en": {
        "High fever": ["high fever","fever","39","102"],
        "Vomiting": ["vomit","throwing up"],
        "Weakness/numbness": ["numb","weakness","droop"],
        "Speech/vision problems": ["speech","vision","double"],
        "Trauma": ["trauma","injury","fall","accident"],
        "None": ["none","no"]
    },
    "es": {
        "High fever": ["fiebre alta","fiebre","39","40"],
        "Vomiting": ["vómito","vomitar"],
        "Weakness/numbness": ["debilidad","entumecimiento","caída facial"],
        "Speech/vision problems": ["habla","visión","doble"],
        "Trauma": ["trauma","lesión","caída","accidente"],
        "None": ["ninguno","no"]
    }
}

def _match_from_syns(text: str, lang: str, syns: dict) -> Optional[str]:
    s = (text or "").lower()
    for label, keys in syns.get(lang, {}).items():
        for kw in keys:
            if re.search(rf"\b{re.escape(kw)}\b", s):
                return label
    best = ("", 0.0)
    for label, keys in syns.get(lang, {}).items():
        for kw in keys:
            r = SequenceMatcher(None, kw, s).ratio()
            if r > best[1]:
                best = (label, r)
    return best[0] if best[1] >= 0.72 else None

def _classify_duration(text: str, lang: str) -> Optional[str]:
    s = (text or "").lower()
    if re.search(r"\b([0-2]?\d)\s*(мин|хв|min)\b", s):
        return {"ru":"<3ч","uk":"<3год","en":"<3h","es":"<3h"}[lang]
    if re.search(r"\b([0-9]|1\d|2[0-4])\s*(час|год|hour|hr|hora|horas)\b", s):
        n = int(re.search(r"\d+", s).group(0))
        return {"ru":"<3ч" if n<3 else "3–24ч",
                "uk":"<3год" if n<3 else "3–24год",
                "en":"<3h" if n<3 else "3–24h",
                "es":"<3h" if n<3 else "3–24h"}[lang]
    if re.search(r"\b(день|дня|day|día)\b", s):
        return {"ru":">1 дня","uk":">1 дня","en":">1 day","es":">1 day"}[lang]
    if re.search(r"\b(тиж|недел|week|semana)\b", s):
        return {"ru":">1 недели","uk":">1 тижня","en":">1 week","es":">1 week"}[lang]
    if re.search(r"\b(час|год|hour|hr|hora)\b", s):
        return {"ru":"3–24ч","uk":"3–24год","en":"3–24h","es":"3–24h"}[lang]
    return None


# -------- Health60 (quick triage) --------
SYS_H60 = (
    "You are TendAI — a concise, warm, professional health & longevity assistant (not a doctor). "
    "Always answer strictly in {lang}. Keep it short and practical. "
    "Given a symptom text and a brief user profile, produce a compact JSON ONLY with keys: "
    "{\"causes\": [\"...\"], \"serious\": \"...\", \"do_now\": [\"...\"], \"see_doctor\": [\"...\"]}. "
    "Rules: 2–4 simple causes (lay language), exactly 1 serious item to rule out, "
    "3–5 \"do_now\" concrete steps for the next 24–48h, 2–3 \"see_doctor\" cues (when to seek care). "
    "No extra keys, no prose outside JSON."
)

def _fmt_bullets(items: list) -> str:
    return "\n".join([f"• {x}" for x in items if isinstance(x, str) and x.strip()])

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


# ------------- Commands & init -------------
async def post_init(app):
    me = await app.bot.get_me()
    logging.info(f"BOT READY: @{me.username} (id={me.id})")

# >>> Overwrite ask_next_mini to avoid calling gate_show with dummy Update
async def ask_next_mini(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: int):
    s = sessions.get(uid, {})
    step_idx = s.get("mini_step", 0)
    if step_idx >= len(MINI_KEYS):
        profiles_upsert(uid, s.get("mini_answers", {}))
        sessions[uid]["mini_active"] = False
        await context.bot.send_message(chat_id, T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
        return
    key = MINI_KEYS[step_idx]
    label = MINI_STEPS[key]["label"][lang]
    await context.bot.send_message(chat_id, label, reply_markup=build_mini_kb(lang, key))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = norm_lang(getattr(user, "language_code", None))
    users_upsert(user.id, user.username or "", lang)
    context.user_data["lang"] = lang
    sessions.setdefault(user.id, {})["last_user_text"] = "/start"

    await update.message.reply_text(T[lang]["welcome"], reply_markup=ReplyKeyboardRemove())

    # 🔎 Мини-опрос с первого сообщения
    prof = profiles_get(user.id)
    if profile_is_incomplete(prof):
        await start_mini_intake(context, update.effective_chat.id, lang, user.id)
    else:
        await update.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))

    # Согласие
    u = users_get(user.id)
    if (u.get("consent") or "").lower() not in {"yes","no"}:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["yes"], callback_data="consent|yes"),
                                    InlineKeyboardButton(T[lang]["no"], callback_data="consent|no")]])
        await update.message.reply_text(T[lang]["ask_consent"], reply_markup=kb)

    # Ежедневный чекап
    tz_off = int(str(u.get("tz_offset") or "0"))
    hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, user.id, tz_off, hhmm, lang)
    else:
        logging.warning("JobQueue not available on /start – daily check-in not scheduled.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await update.message.reply_text(T[lang]["help"])

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await update.message.reply_text(T[lang]["privacy"])

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "yes")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text(T[lang]["paused_on"])

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "no")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text(T[lang]["paused_off"])

async def cmd_delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if SHEETS_ENABLED:
        vals = ws_users.get_all_values()
        for i in range(2, len(vals)+1):
            if ws_users.cell(i,1).value == str(uid):
                ws_users.delete_rows(i); break
        # also delete from other sheets (best-effort)
        try:
            vals = ws_profiles.get_all_values()
            for i in range(2, len(vals)+1):
                if ws_profiles.cell(i,1).value == str(uid):
                    ws_profiles.delete_rows(i)
        except: pass
    else:
        MEM_USERS.pop(uid, None); MEM_PROFILES.pop(uid, None)
        global MEM_EPISODES, MEM_REMINDERS, MEM_DAILY
        MEM_EPISODES = [r for r in MEM_EPISODES if r["user_id"]!=str(uid)]
        MEM_REMINDERS = [r for r in MEM_REMINDERS if r["user_id"]!=str(uid)]
        MEM_DAILY = [r for r in MEM_DAILY if r["user_id"]!=str(uid)]
    lang = norm_lang(getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(T[lang]["deleted"], reply_markup=ReplyKeyboardRemove())

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None))
    await start_profile_ctx(context, update.effective_chat.id, lang, uid)

async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    parts = (update.message.text or "").split()
    if len(parts)<2 or not re.fullmatch(r"[+-]?\d{1,2}", parts[1]):
        await update.message.reply_text({"ru":"Формат: /settz +3","uk":"Формат: /settz +2",
                                         "en":"Usage: /settz +3","es":"Uso: /settz +3"}[lang]); return
    off = int(parts[1]); users_set(uid,"tz_offset",str(off))
    hhmm = users_get(uid).get("checkin_hour") or DEFAULT_CHECKIN_LOCAL
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, uid, off, hhmm, lang)
    await update.message.reply_text({"ru":f"Сдвиг часового пояса: {off}ч",
                                     "uk":f"Зсув: {off} год",
                                     "en":f"Timezone offset: {off}h",
                                     "es":f"Desfase horario: {off}h"}[lang])

async def cmd_checkin_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    parts = (update.message.text or "").split(maxsplit=1)
    hhmm = DEFAULT_CHECKIN_LOCAL
    if len(parts)==2:
        m = re.search(r'([01]?\d|2[0-3]):([0-5]\d)', parts[1])
        if m:
            hhmm = m.group(0)
    users_set(uid,"checkin_hour",hhmm)
    tz_off = int(str(users_get(uid).get("tz_offset") or "0"))
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, uid, tz_off, hhmm, lang)
    else:
        logging.warning("JobQueue not available – daily check-in not scheduled.")
    await update.message.reply_text({"ru":f"Ежедневный чек-ин включён ({hhmm}).",
                                     "uk":f"Щоденний чек-ін увімкнено ({hhmm}).",
                                     "en":f"Daily check-in enabled ({hhmm}).",
                                     "es":f"Check-in diario activado ({hhmm})."}[lang])

async def cmd_checkin_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if _has_jq_ctx(context):
        for j in context.application.job_queue.get_jobs_by_name(f"daily_{uid}"):
            j.schedule_removal()
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text({"ru":"Ежедневный чек-ин выключен.",
                                     "uk":"Щоденний чек-ін вимкнено.",
                                     "en":"Daily check-in disabled.",
                                     "es":"Check-in diario desactivado."}[lang])

async def cmd_health60(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None))
    sessions.setdefault(uid, {})["awaiting_h60"] = True
    await update.message.reply_text(T[lang]["h60_intro"])

# === New quick commands (Youth Pack) ===
async def cmd_energy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡1", callback_data="energy|1"),
         InlineKeyboardButton("2", callback_data="energy|2"),
         InlineKeyboardButton("3", callback_data="energy|3"),
         InlineKeyboardButton("4", callback_data="energy|4"),
         InlineKeyboardButton("5", callback_data="energy|5")]
    ])
    await update.message.reply_text(T[lang]["gm_energy_q"], reply_markup=kb)

async def cmd_hydrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text(T[lang]["hydrate_nudge"])

async def cmd_skintip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = profiles_get(uid); lang = norm_lang(users_get(uid).get("lang") or "en")
    age = int(re.search(r"\d+", str(prof.get("age") or "0")).group(0)) if re.search(r"\d+", str(prof.get("age") or "")) else 0
    tip = _get_skin_tip(lang, (prof.get("sex") or ""), age)
    await update.message.reply_text(f"{T[lang]['daily_tip_prefix']} {tip}")

async def cmd_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    sessions.setdefault(uid,{})["awaiting_cycle_consent"] = True
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["yes"], callback_data="cycle|yes"),
                                InlineKeyboardButton(T[lang]["no"], callback_data="cycle|no")]])
    await update.message.reply_text(T[lang]["cycle_consent"], reply_markup=kb)

async def cmd_youth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["hydrate_btn"], callback_data="act|hydration"),
         InlineKeyboardButton(T[lang]["skintip_btn"], callback_data="act|skintip")],
        [InlineKeyboardButton(T[lang]["challenge_btn"], callback_data="act|challenge"),
         InlineKeyboardButton(T[lang]["cycle_btn"], callback_data="act|cycle")],
        [InlineKeyboardButton(T[lang]["set_quiet_btn"], callback_data="act|quiet")]
    ])
    await update.message.reply_text(T[lang]["youth_pack"], reply_markup=kb)

async def cmd_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "ru")
    await update.message.reply_text("Ок, дальше отвечаю по-русски.")

async def cmd_en(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "en")
    await update.message.reply_text("OK, I’ll reply in English.")

async def cmd_uk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "uk")
    await update.message.reply_text("Ок, надалі відповідатиму українською.")

async def cmd_es(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "es")
    await update.message.reply_text("De acuerdo, responderé en español.")


# ------------- Pain / Profile helpers -------------
def detect_or_choose_topic(lang: str, text: str) -> Optional[str]:
    return None

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
        await context.bot.send_message(chat_id, T[lang][f"p_step_{idx+1}"], reply_markup=kb)
        return
    prof = profiles_get(uid); summary=[]
    for k in ["sex","age","goal","conditions","meds","sleep","activity","diet"]:
        v = prof.get(k) or sessions.get(uid,{}).get(k,"")
        if v: summary.append(f"{k}: {v}")
    profiles_upsert(uid, {})
    sessions[uid]["profile_active"] = False
    await context.bot.send_message(chat_id, T[lang]["saved_profile"] + "; ".join(summary))
    await context.bot.send_message(chat_id, T[lang]["start_where"], reply_markup=inline_topic_kb(lang))

def personalized_prefix(lang: str, profile: dict) -> str:
    sex = profile.get("sex") or ""
    age = profile.get("age") or ""
    goal = profile.get("goal") or ""
    if not (sex or age or goal):
        return ""
    return T[lang]["px"].format(sex=sex or "—", age=age or "—", goal=goal or "—")


# ------------- Plans / Serious conditions -------------
def pain_plan(lang: str, red_flags_selected: List[str], profile: dict) -> List[str]:
    flg = [s for s in red_flags_selected if s and str(s).lower() not in ["none","нет","немає","ninguno","no"]]
    if flg:
        return {
            "ru":["⚠️ Есть тревожные признаки. Лучше как можно скорее показаться врачу/в скорую."],
            "uk":["⚠️ Є тривожні ознаки. Варто якнайшвидше звернутися до лікаря/швидкої."],
            "en":["⚠️ Red flags present. Please seek urgent medical evaluation."],
            "es":["⚠️ Señales de alarma presentes. Busca evaluación médica urgente."]
        }[lang]
    age = int(re.search(r"\d+", str(profile.get("age") or "0")).group(0)) if re.search(r"\d+", str(profile.get("age") or "")) else 0
    extra = []
    if age >= 60:
        extra.append({
            "ru":"Вам 60+, будьте осторожны с НПВП; пейте воду и при ухудшении обратитесь к врачу.",
            "uk":"Вам 60+, обережно з НПЗЗ; пийте воду, за погіршення — до лікаря.",
            "en":"Age 60+: be careful with NSAIDs; hydrate and seek care if worsening.",
            "es":"Edad 60+: cuidado con AINEs; hidrátate y busca atención si empeora."
        }[lang])
    core = {
        "ru":[
            "1) Вода 400–600 мл и 15–20 мин тишины/отдыха.",
            "2) Если нет противопоказаний — ибупрофен 200–400 мг однократно с едой.",
            "3) Проветрить, уменьшить экран на 30–60 мин.",
            "Цель: к вечеру боль ≤3/10."
        ],
        "uk":[
            "1) Вода 400–600 мл і 15–20 хв спокою.",
            "2) Якщо нема протипоказань — ібупрофен 200–400 мг одноразово з їжею.",
            "3) Провітрити, менше екрану 30–60 хв.",
            "Мета: до вечора біль ≤3/10."
        ],
        "en":[
            "1) Drink 400–600 ml water; rest 15–20 min.",
            "2) If no contraindications — ibuprofen 200–400 mg once with food.",
            "3) Air the room; reduce screen time 30–60 min.",
            "Goal: by evening pain ≤3/10."
        ],
        "es":[
            "1) Bebe 400–600 ml de agua; descansa 15–20 min.",
            "2) Si no hay contraindicaciones — ibuprofeno 200–400 mg una vez con comida.",
            "3) Ventila la habitación; reduce pantallas 30–60 min.",
            "Meta: por la tarde dolor ≤3/10."
        ],
    }[lang]
    return core + extra + [T[lang]["er_text"]]

SERIOUS_KWS = {
    "diabetes": ["diabetes","диабет","сахарный","цукров", "глюкоза", "hba1c", "гликированный","глюкоза"],
    "hepatitis": ["hepatitis","гепатит","печень hbs","hcv","alt","ast"],
    "cancer": ["cancer","рак","онко","онколог","опухол","пухлина","tumor"],
   
