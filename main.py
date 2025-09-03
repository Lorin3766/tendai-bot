# -*- coding: utf-8 -*-
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
        "welcome": "Hi! I’m TendAI — your health & longevity assistant.\nDescribe what’s bothering you; I’ll guide you. Let’s do a quick 40s intake to tailor advice.",
        "help": "Short checkups, 24–48h plans, reminders, daily check-ins.\nCommands: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /evening_on 20:00 /evening_off /settz +2 /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
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
        "mood_cmd":"How do you feel right now?",
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
    },
    "ru": {
        "welcome":"Привет! Я TendAI — ассистент здоровья и долголетия.\nРасскажи, что беспокоит; я подскажу. Сначала короткий опрос (~40с), чтобы советы были точнее.",
        "help":"Короткие проверки, план на 24–48 ч, напоминания, ежедневные чек-ины.\nКоманды: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /evening_on 20:00 /evening_off /settz +3 /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
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
        "mood_cmd":"Как сейчас самочувствие?",
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
    },
    "uk": {
        "welcome":"Привіт! Я TendAI — асистент здоров’я та довголіття.\nРозкажи, що турбує; я підкажу. Спершу швидкий опитник (~40с) для точніших порад.",
        "help":"Короткі перевірки, план на 24–48 год, нагадування, щоденні чек-іни.\nКоманди: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /evening_on 20:00 /evening_off /settz +2 /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
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
        "mood_cmd":"Як почуваєтесь зараз?",
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
        "evening_intro": "Вечірній чек-ін:",
        "evening_tip_btn": "🪄 Порада дня",
        "evening_set": "Вечірній чек-ін встановлено на {t} (локально).",
        "evening_off": "Вечірній чек-ін вимкнено.",
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
        # просто показываем меню
        lang = _user_lang(q.from_user.id)
        await context.bot.send_message(update.effective_chat.id, T[lang]["start_where"], reply_markup=inline_topic_kb(lang))


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
        # Меню + постоянные быстрые кнопки
        await context.bot.send_message(chat_id, T[lang]["start_where"],
                                       reply_markup=kb_merge(inline_topic_kb(lang), quick_actions_kb(lang)))
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

# === Quick Actions row (везде доступны) ===
def quick_actions_kb(lang: str) -> InlineKeyboardMarkup:
    labels = {
        "ru": [("⚡ 60 сек", "qa|h60"), ("🚑 Срочно", "qa|er"), ("🧪 Лаборатория", "qa|lab"), ("⏰ Напоминание", "qa|rem")],
        "uk": [("⚡ 60 сек", "qa|h60"), ("🚑 Терміново", "qa|er"), ("🧪 Лабораторія", "qa|lab"), ("⏰ Нагадування", "qa|rem")],
        "en": [("⚡ Health60", "qa|h60"), ("🚑 Emergency", "qa|er"), ("🧪 Lab", "qa|lab"), ("⏰ Reminder", "qa|rem")],
        "es": [("⚡ 60s Salud", "qa|h60"), ("🚑 Urgencias", "qa|er"), ("🧪 Laboratorio", "qa|lab"), ("⏰ Recordatorio", "qa|rem")],
    }
    row = [InlineKeyboardButton(txt, callback_data=cb) for txt, cb in labels.get(lang, labels["en"])]
    return InlineKeyboardMarkup([row])

def kb_merge(base: Optional[InlineKeyboardMarkup], extra: Optional[InlineKeyboardMarkup]) -> Optional[InlineKeyboardMarkup]:
    if not base and not extra:
        return None
    rows = []
    if base:  rows.extend(base.inline_keyboard)
    if extra: rows.extend(extra.inline_keyboard)
    return InlineKeyboardMarkup(rows)

async def send_unique(msg_obj, uid: int, text: str, reply_markup=None, force: bool = False):
    if force or not is_duplicate_question(uid, text):
        lang = _user_lang(uid)
        qa = quick_actions_kb(lang)
        await msg_obj.reply_text(text, reply_markup=kb_merge(reply_markup, qa))


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
    try:
        current = _ws_headers(ws)
        if not current:
            ws.append_row(desired_headers)
            return
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
                ws = ss.add_worksheet(title=title, rows=4000, cols=max(20, len(headers)))
                ws.append_row(headers)
            if not ws.get_all_values():
                ws.append_row(headers)
            _ws_ensure_columns(ws, headers)
            return ws

        ws_feedback = _ensure_ws("Feedback", ["timestamp","user_id","name","username","rating","comment"])
        ws_users = _ensure_ws("Users", ["user_id","username","lang","consent","tz_offset","checkin_hour","evening_hour","paused","last_seen","last_auto_date","last_auto_count"])
        ws_profiles = _ensure_ws("Profiles", [
            "user_id","sex","age","goal","goals","conditions","meds","allergies",
            "sleep","activity","diet","diet_focus","steps_target","habits",
            "cycle_enabled","cycle_last_date","cycle_avg_len","last_cycle_tip_date",
            "quiet_hours","consent_flags","notes","updated_at"
        ])
        ws_episodes = _ensure_ws("Episodes", ["episode_id","user_id","topic","started_at","baseline_severity","red_flags",
                                              "plan_accepted","target","reminder_at","next_checkin_at","status","last_update","notes"])
        ws_daily = _ensure_ws("DailyCheckins",["timestamp","user_id","mood","energy","comment"])
        ws_rules = _ensure_ws("Rules", ["rule_id","topic","segment","trigger","advice_text","citation","last_updated","enabled"])
        ws_challenges = _ensure_ws("Challenges", ["user_id","challenge_id","name","start_date","length_days","days_done","status"])
        ws_reminders = _ensure_ws("Reminders", ["id","user_id","text","when_utc","created_at","status"])

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


# --------- Time & policy helpers ---------
def hhmm_tuple(hhmm:str)->Tuple[int,int]:
    m = re.search(r'([01]?\d|2[0-3]):([0-5]\d)', hhmm.strip())
    return (int(m.group(1)), int(m.group(2))) if m else (8,30)

def local_to_utc_hour_min(tz_offset_hours:int, hhmm:str)->Tuple[int,int]:
    h,m = hhmm_tuple(hhmm); return ((h - tz_offset_hours) % 24, m)

def parse_quiet_hours(qh: str)->Tuple[Tuple[int,int], Tuple[int,int]]:
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
    return local_dt >= start or local_dt < end

def adjust_out_of_quiet(local_dt: datetime, qh: str)->datetime:
    if not is_in_quiet(local_dt, qh):
        return local_dt
    (sh,sm),(eh,em) = parse_quiet_hours(qh)
    end = local_dt.replace(hour=eh, minute=em, second=0, microsecond=0)
    if (sh,sm) > (eh,em) and local_dt.time() < dtime(eh,em):
        return end
    return end + timedelta(days=1)

def _user_lang(uid: int) -> str:
    return norm_lang(users_get(uid).get("lang") or "en")

def _user_tz_off(uid: int) -> int:
    try:
        return int(str(users_get(uid).get("tz_offset") or "0"))
    except Exception:
        return 0

def _user_quiet_hours(uid: int) -> str:
    prof = profiles_get(uid) or {}
    qh = (prof.get("quiet_hours") or "").strip()
    return qh if qh else DEFAULT_QUIET_HOURS

def user_local_now(uid: int) -> datetime:
    return utcnow() + timedelta(hours=_user_tz_off(uid))

def local_to_utc_dt(uid: int, local_dt: datetime) -> datetime:
    return (local_dt - timedelta(hours=_user_tz_off(uid))).replace(tzinfo=timezone.utc)

def _same_local_day(d1: datetime, d2: datetime) -> bool:
    return d1.date() == d2.date()

def can_send_auto(uid: int) -> bool:
    u = users_get(uid)
    now_local = user_local_now(uid)
    last_date = (u.get("last_auto_date") or "")
    count = int(str(u.get("last_auto_count") or "0"))
    if last_date:
        try:
            last_d = datetime.strptime(last_date, "%Y-%m-%d").date()
        except:
            last_d = None
    else:
        last_d = None
    if (last_d is None) or (last_d != now_local.date()):
        users_set(uid, "last_auto_date", now_local.date().isoformat())
        users_set(uid, "last_auto_count", "0")
        count = 0
    return count < AUTO_MAX_PER_DAY

def inc_auto(uid: int):
    u = users_get(uid)
    now_local = user_local_now(uid)
    last_date = (u.get("last_auto_date") or "")
    count = int(str(u.get("last_auto_count") or "0"))
    if last_date != now_local.date().isoformat():
        users_set(uid, "last_auto_date", now_local.date().isoformat())
        users_set(uid, "last_auto_count", "1")
    else:
        users_set(uid, "last_auto_count", str(count+1))

def update_last_seen(uid: int):
    users_set(uid, "last_seen", iso(utcnow()))


# --------- Scheduling from sheet on start ---------
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
        lang = norm_lang(u.get("lang") or "en")
        schedule_daily_checkin(app, uid, tz_off, hhmm, lang)
        eh = (u.get("evening_hour") or "").strip()
        if eh:
            schedule_evening_checkin(app, uid, tz_off, eh, lang)

def schedule_daily_checkin(app, uid:int, tz_off:int, hhmm_local:str, lang:str):
    if not _has_jq_app(app):
        logging.warning(f"JobQueue not available – skip daily scheduling for uid={uid}.")
        return
    for j in app.job_queue.get_jobs_by_name(f"daily_{uid}"):
        j.schedule_removal()
    h_utc, m_utc = local_to_utc_hour_min(tz_off, hhmm_local)
    t = dtime(hour=h_utc, minute=m_utc, tzinfo=timezone.utc)
    app.job_queue.run_daily(job_daily_checkin, time=t, name=f"daily_{uid}", data={"user_id":uid,"lang":lang})

def schedule_evening_checkin(app, uid:int, tz_off:int, hhmm_local:str, lang:str):
    if not _has_jq_app(app):
        logging.warning(f"JobQueue not available – skip evening scheduling for uid={uid}.")
        return
    for j in app.job_queue.get_jobs_by_name(f"evening_{uid}"):
        j.schedule_removal()
    h_utc, m_utc = local_to_utc_hour_min(tz_off, hhmm_local)
    t = dtime(hour=h_utc, minute=m_utc, tzinfo=timezone.utc)
    app.job_queue.run_daily(job_evening_checkin, time=t, name=f"evening_{uid}", data={"user_id":uid,"lang":lang})


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


# -------- Rules helpers (evidence-based) --------
def _build_segment(profile: dict) -> str:
    goal = (profile.get("goal") or "").lower()
    sex = (profile.get("sex") or "").lower()
    age = 0
    m = re.search(r"\d+", str(profile.get("age") or ""))
    if m: age = int(m.group(0))
    band = age_to_band(age)
    return f"{goal}|{sex}|{band}"

def rules_fetch(topic: str, segment: str) -> Optional[dict]:
    try:
        rows = ws_rules.get_all_records() if SHEETS_ENABLED else MEM_RULES
        for r in rows:
            if str(r.get("enabled","1")).strip() in {"1","yes","true"} and \
               (r.get("topic","").strip().lower() == topic.strip().lower()) and \
               (r.get("segment","").strip().lower() == segment.strip().lower()):
                return r
        for r in rows:
            if str(r.get("enabled","1")).strip() in {"1","yes","true"} and \
               (r.get("topic","").strip().lower() == topic.strip().lower()) and \
               (not str(r.get("segment","")).strip()):
                return r
    except Exception as e:
        logging.warning(f"rules_fetch error: {e}")
    return None


# ---------- Content builders ----------
def _get_daily_tip(profile: dict, lang: str) -> str:
    seg = _build_segment(profile)
    r = rules_fetch("nutrition", seg)
    if r:
        cit = r.get("citation","").strip()
        base = r.get("advice_text","").strip()
        tip = base if not cit else f"{base} ({cit})"
        return tip
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
    seg = _build_segment({"goal":"","sex":sex,"age":age})
    r = rules_fetch("skin", seg)
    if r:
        cit = r.get("citation","").strip()
        base = r.get("advice_text","").strip()
        return base if not cit else f"{base} ({cit})"
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


# --------- UI helpers ----------
def inline_numbers_0_10()->InlineKeyboardMarkup:
    row = [InlineKeyboardButton(str(i), callback_data=f"num|{i}") for i in range(0,11)]
    return InlineKeyboardMarkup([row[:6], row[6:]])

def inline_list(options: List[str], prefix: str)->InlineKeyboardMarkup:
    rows, row = [], []
    for opt in options:
        row.append(InlineKeyboardButton(str(opt), callback_data=f"{prefix}|{opt}"))
        if len(row) == 3:
            rows.append(row); row=[]
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def inline_topic_kb(lang: str)->InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["h60_btn"], callback_data="menu|h60"),
         InlineKeyboardButton(T[lang]["youth_pack"], callback_data="menu|youth")],
        [InlineKeyboardButton(T[lang]["gm_energy"], callback_data="menu|energy"),
         InlineKeyboardButton(T[lang]["hydrate_btn"], callback_data="menu|hydrate")],
        [InlineKeyboardButton(T[lang]["skintip_btn"], callback_data="menu|skintip"),
         InlineKeyboardButton(T[lang]["cycle_btn"], callback_data="menu|cycle")],
    ])


# === PRO-INTAKE (INLINE, без отдельного файла) ===
IPRO_KEYS = ["sex", "age", "goal", "conditions", "meds_allergies", "sleep"]

def register_intake_pro(app, **kwargs):
    """
    Встроенный PRO-интейк. Кнопка: callback_data='intake:start'.
    Перехватывает текст на шагах с фритекстом раньше общего хэндлера (group=1).
    """
    async def _ipro_ask_next(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: int, state: dict):
        step_idx = int(state.get("step", 0))
        if step_idx >= len(IPRO_KEYS):
            answers = state.get("answers", {})
            if answers.get("meds_allergies"):
                answers["meds"] = answers["meds_allergies"]
                answers["allergies"] = answers["meds_allergies"]
            try:
                profiles_upsert(uid, answers)
            except Exception as e:
                logging.error(f"ipro save error: {e}")
            context.user_data[GATE_FLAG_KEY] = True
            try:
                await context.bot.send_message(chat_id, T[_user_lang(uid)]["start_where"],
                                               reply_markup=kb_merge(inline_topic_kb(_user_lang(uid)), quick_actions_kb(_user_lang(uid))))
            except Exception as e:
                logging.error(f"ipro follow-up send error: {e}")
            state.clear(); state.update({"active": False})
            return

        key = IPRO_KEYS[step_idx]
        # Используем локализованные метки из MINI_STEPS; для sleep — шаг 6
        label = MINI_STEPS.get(key, {}).get("label", {}).get(lang) or T[lang]["p_step_6"]
        # Собираем клавиатуру
        opts = MINI_STEPS.get(key, {}).get(lang, [])
        rows, row = [], []
        for label_btn, val in opts:
            row.append(InlineKeyboardButton(label_btn, callback_data=f"intake|choose|{key}|{val}"))
            if len(row) == 3:
                rows.append(row); row=[]
        if row: rows.append(row)
        if key in ("meds_allergies","sleep") or not opts:
            rows.append([InlineKeyboardButton(T[lang]["write"], callback_data=f"intake|write|{key}")])
        rows.append([InlineKeyboardButton(T[lang]["skip"], callback_data=f"intake|skip|{key}")])
        kb = InlineKeyboardMarkup(rows)
        await context.bot.send_message(chat_id, label, reply_markup=kb)

    async def ipro_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        uid = q.from_user.id
        lang = _user_lang(uid)
        data = q.data or ""
        s = sessions.setdefault(uid, {})
        ipro = s.setdefault("ipro", {"active": False, "step": 0, "answers": {}, "wait_key": None})

        if data == "intake:start":
            ipro.update({"active": True, "step": 0, "answers": {}, "wait_key": None})
            try:
                await q.edit_message_text(T[lang]["profile_intro"])
            except Exception:
                pass
            await _ipro_ask_next(context, update.effective_chat.id, lang, uid, ipro)
            return

        if not data.startswith("intake|"):
            return
        _, action, key, *rest = data.split("|")
        if action == "choose" and len(rest) == 1:
            val = rest[0]
            ipro["answers"][key] = val
            ipro["step"] = int(ipro["step"]) + 1
            await _ipro_ask_next(context, update.effective_chat.id, lang, uid, ipro)
        elif action == "skip":
            ipro["step"] = int(ipro["step"]) + 1
            await _ipro_ask_next(context, update.effective_chat.id, lang, uid, ipro)
        elif action == "write":
            ipro["wait_key"] = key
            try:
                await q.edit_message_text(T[lang]["write"] + "…")
            except Exception:
                pass

    async def ipro_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        lang = _user_lang(uid)
        s = sessions.setdefault(uid, {})
        ipro = s.get("ipro") or {}
        if not ipro.get("active"):
            return
        wk = ipro.get("wait_key")
        if not wk:
            return
        text = (update.message.text or "").strip()
        ipro["answers"][wk] = text
        ipro["wait_key"] = None
        ipro["step"] = int(ipro.get("step", 0)) + 1
        await _ipro_ask_next(context, update.effective_chat.id, lang, uid, ipro)

    # ВАЖНО: паттерн должен ловить 'intake|...' (без двоеточия!)
    app.add_handler(CallbackQueryHandler(ipro_cb, pattern=r"^intake"), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ipro_text), group=1)
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
    "Given a symptom text and a brief user profile, produce a compact JSON ONLY with keys: "
    "{\"causes\": [\"...\"], \"serious\": \"...\", \"do_now\": [\"...\"], \"see_doctor\": [\"...\"]}. "
    "Rules: 2–4 simple causes, exactly 1 serious item to rule out, "
    "3–5 do_now concrete steps for the next 24–48h, 2–3 see_doctor cues. "
    "No extra keys, no prose outside JSON."
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
# ---------- Quick actions (всегда доступные) ----------
def quick_actions_kb(lang: str) -> InlineKeyboardMarkup:
    if lang == "ru":
        b1, b2, b3, b4 = "⚡ Здоровье за 60 сек", "🚑 Срочно в скорую", "🧪 Лаборатория", "⏰ Напоминание"
    elif lang == "uk":
        b1, b2, b3, b4 = "⚡ Здоров’я за 60 с", "🚑 Терміново в швидку", "🧪 Лабораторія", "⏰ Нагадування"
    elif lang == "es":
        b1, b2, b3, b4 = "⚡ Salud en 60 s", "🚑 Urgencias", "🧪 Laboratorio", "⏰ Recordatorio"
    else:
        b1, b2, b3, b4 = "⚡ Health in 60s", "🚑 Emergency", "🧪 Lab", "⏰ Reminder"

    rows = [
        [InlineKeyboardButton(b1, callback_data="qa|h60"),
         InlineKeyboardButton(b2, callback_data="qa|er")],
        [InlineKeyboardButton(b3, callback_data="qa|lab"),
         InlineKeyboardButton(b4, callback_data="qa|rem")]
    ]
    return InlineKeyboardMarkup(rows)


# ---------- Уточнённый утренний чек-ин (с быстрыми кнопками) ----------
async def job_daily_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id"); lang = norm_lang((d.get("lang") or "en"))
    if not uid:
        return
    u = users_get(uid)
    if (u.get("paused") or "").lower() == "yes":
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_good"], callback_data="gm|mood|good"),
         InlineKeyboardButton(T[lang]["mood_ok"],   callback_data="gm|mood|ok"),
         InlineKeyboardButton(T[lang]["mood_bad"],  callback_data="gm|mood|bad")],
        [InlineKeyboardButton(T[lang]["mood_note"], callback_data="gm|note")],
        [InlineKeyboardButton(T[lang]["gm_evening_btn"], callback_data="youth:gm_evening")],
        # Быстрые действия — всегда под рукой
        [InlineKeyboardButton("⚡", callback_data="qa|h60"),
         InlineKeyboardButton("🚑", callback_data="qa|er"),
         InlineKeyboardButton("🧪", callback_data="qa|lab"),
         InlineKeyboardButton("⏰", callback_data="qa|rem")]
    ])
    try:
        await context.bot.send_message(uid, T[lang]["daily_gm"], reply_markup=kb)
    except Exception as e:
        logging.error(f"job_daily_checkin send error: {e}")


# ---------- Callback Query Handler (дополнен qa|...) ----------
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    lang = _user_lang(uid)
    data = (q.data or "")

    # Gate skip
    if data == "gate:skip":
        await gate_cb(update, context)
        return

    # Consent
    if data.startswith("consent|"):
        val = data.split("|",1)[1]
        users_set(uid, "consent", "yes" if val=="yes" else "no")
        await q.edit_message_text(T[lang]["thanks"])
        return

    # Mini-intake
    if data.startswith("mini|"):
        _, action, key, *rest = data.split("|")
        if action == "choose":
            value = rest[0] if rest else ""
            mini_handle_choice(uid, key, value)
        elif action == "skip":
            s = sessions.setdefault(uid, {"mini_active": True, "mini_step": 0, "mini_answers": {}})
            s["mini_step"] = int(s.get("mini_step", 0)) + 1
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return

    # Главные меню-кнопки
    if data.startswith("menu|"):
        act = data.split("|",1)[1]
        if act == "h60":
            sessions.setdefault(uid, {})["awaiting_h60_text"] = True
            await context.bot.send_message(uid, T[lang]["h60_intro"], reply_markup=quick_actions_kb(lang))
        elif act == "energy":
            row = [InlineKeyboardButton(str(i), callback_data=f"energy|rate|{i}") for i in range(1,6)]
            kb = InlineKeyboardMarkup([row])
            await context.bot.send_message(uid, T[lang]["gm_energy_q"], reply_markup=kb)
        elif act == "hydrate":
            tip = T[lang]["hydrate_nudge"]
            daily_add(iso(utcnow()), uid, mood="", comment=tip, energy=None)
            await context.bot.send_message(uid, tip, reply_markup=quick_actions_kb(lang))
        elif act == "skintip":
            prof = profiles_get(uid) or {}
            age = int(re.search(r"\d+", str(prof.get("age") or "0")).group(0)) if re.search(r"\d+", str(prof.get("age") or "")) else 0
            sex = (prof.get("sex") or "").lower()
            tip = _get_skin_tip(lang, sex, age)
            await context.bot.send_message(uid, f"{T[lang]['daily_tip_prefix']} {tip}", reply_markup=quick_actions_kb(lang))
        elif act == "cycle":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["yes"], callback_data="cycle|consent|yes"),
                                        InlineKeyboardButton(T[lang]["no"],  callback_data="cycle|consent|no")]])
            await context.bot.send_message(uid, T[lang]["cycle_consent"], reply_markup=kb)
        elif act == "youth":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(T[lang]["gm_energy"], callback_data="menu|energy"),
                 InlineKeyboardButton(T[lang]["hydrate_btn"], callback_data="menu|hydrate")],
                [InlineKeyboardButton(T[lang]["skintip_btn"], callback_data="menu|skintip"),
                 InlineKeyboardButton(T[lang]["gm_evening_btn"], callback_data="youth:gm_evening")],
                [InlineKeyboardButton(T[lang]["set_quiet_btn"], callback_data="youth:set_quiet"),
                 InlineKeyboardButton(T[lang]["challenge_btn"], callback_data="youth:challenge")]
            ])
            await context.bot.send_message(uid, T[lang]["youth_pack"], reply_markup=kb)
        return

    # Быстрые действия qa|...
    if data.startswith("qa|"):
        kind = data.split("|", 1)[1]
        if kind == "h60":
            sessions.setdefault(uid, {})["awaiting_h60_text"] = True
            await context.bot.send_message(uid, T[lang]["h60_intro"], reply_markup=quick_actions_kb(lang))
        elif kind == "er":
            await context.bot.send_message(uid, T[lang]["er_text"], reply_markup=quick_actions_kb(lang))
        elif kind == "lab":
            sessions.setdefault(uid, {})["await_lab_city"] = True
            await context.bot.send_message(uid, T[lang]["act_city_prompt"])
        elif kind == "rem":
            kb = InlineKeyboardMarkup([  # используем существующие обработчики h60|rem|...
                [InlineKeyboardButton(T[lang]["remind_opts"][0], callback_data="h60|rem|4h")],
                [InlineKeyboardButton(T[lang]["remind_opts"][1], callback_data="h60|rem|eve")],
                [InlineKeyboardButton(T[lang]["remind_opts"][2], callback_data="h60|rem|morn")],
                [InlineKeyboardButton(T[lang]["remind_opts"][3], callback_data="h60|rem|none")]
            ])
            await context.bot.send_message(uid, T[lang]["remind_when"], reply_markup=kb)
        return

    # Health60 actions
    if data.startswith("h60|"):
        parts = data.split("|")
        sub = parts[1] if len(parts)>1 else ""
        if sub == "accept":
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(uid, T[lang]["thanks"], reply_markup=quick_actions_kb(lang))
        elif sub == "rem":
            opt = parts[2] if len(parts)>2 else ""
            now_local = user_local_now(uid)
            if opt == "4h":
                when_local = now_local + timedelta(hours=4)
                adj = _schedule_oneoff_with_sheet(context, uid, when_local, T[lang]["thanks"])
                await context.bot.send_message(uid, _fmt_reminder_set(lang, adj, kind="4h"), reply_markup=quick_actions_kb(lang))
            elif opt == "eve":
                eh = users_get(uid).get("evening_hour") or DEFAULT_EVENING_LOCAL
                (hh, mm) = hhmm_tuple(eh)
                target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if target <= now_local:
                    target = target + timedelta(days=1)
                adj = _schedule_oneoff_with_sheet(context, uid, target, T[lang]["thanks"])
                await context.bot.send_message(uid, _fmt_reminder_set(lang, adj, kind="evening"), reply_markup=quick_actions_kb(lang))
            elif opt == "morn":
                mh = users_get(uid).get("checkin_hour") or DEFAULT_CHECKIN_LOCAL
                (hh, mm) = hhmm_tuple(mh)
                target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if target <= now_local:
                    target = target + timedelta(days=1)
                adj = _schedule_oneoff_with_sheet(context, uid, target, T[lang]["thanks"])
                await context.bot.send_message(uid, _fmt_reminder_set(lang, adj, kind="morning"), reply_markup=quick_actions_kb(lang))
        elif sub == "episode":
            act = parts[2] if len(parts)>2 else ""
            if act == "save":
                eid = episode_create(uid, topic="h60", severity=5, red="")
                sessions.setdefault(uid, {})["last_eid"] = eid
                await context.bot.send_message(uid, T[lang]["act_saved"], reply_markup=quick_actions_kb(lang))
        elif sub == "neck":
            text_map = {
                "ru": "🧘 5 минут для шеи:\n• Медленные наклоны: вперёд/назад/в стороны ×6\n• Повороты головы ×6\n• Плечи: подъём/круги ×10\n• Мягкое вытяжение, без боли\n• Дыхание 4–6 циклов",
                "uk": "🧘 5 хв для шиї:\n• Похили: вперед/назад/в сторони ×6\n• Повороти голови ×6\n• Плечі: підйом/кола ×10\n• М’яке розтягнення без болю\n• Дихання 4–6 циклів",
                "es": "🧘 5 min para el cuello:\n• Inclinaciones: adelante/atrás/lados ×6\n• Giros de cabeza ×6\n• Hombros: elevación/círculos ×10\n• Estiramiento suave, sin dolor\n• Respiración 4–6 ciclos",
                "en": "🧘 5-min neck reset:\n• Slow tilts: forward/back/side ×6\n• Head turns ×6\n• Shoulders: shrug/circles ×10\n• Gentle stretch, no pain\n• Breathe 4–6 cycles",
            }
            await context.bot.send_message(uid, text_map[lang], reply_markup=quick_actions_kb(lang))
        elif sub == "er":
            await context.bot.send_message(uid, T[lang]["er_text"], reply_markup=quick_actions_kb(lang))
        return

    # Daily GM actions
    if data.startswith("gm|"):
        _, kind, *rest = data.split("|")
        if kind == "mood":
            mood = rest[0] if rest else "ok"
            daily_add(iso(utcnow()), uid, mood=mood, comment="")
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(uid, T[lang]["mood_thanks"], reply_markup=quick_actions_kb(lang))
        elif kind == "note":
            sessions.setdefault(uid, {})["await_gm_note"] = True
            await q.edit_message_text(T[lang]["fb_write"])
        return

    # Energy rating
    if data.startswith("energy|rate|"):
        try:
            val = int(data.split("|")[-1])
        except:
            val = None
        daily_add(iso(utcnow()), uid, mood="", comment="energy", energy=val)
        await q.edit_message_reply_markup(None)
        await context.bot.send_message(uid, T[lang]["gm_energy_done"], reply_markup=quick_actions_kb(lang))
        return

    # Youth pack shortcuts
    if data.startswith("youth:"):
        act = data.split(":",1)[1]
        if act == "gm_evening":
            u = users_get(uid)
            off = _user_tz_off(uid)
            eh = (u.get("evening_hour") or DEFAULT_EVENING_LOCAL)
            users_set(uid, "evening_hour", eh)
            if _has_jq_ctx(context):
                schedule_evening_checkin(context.application, uid, off, eh, lang)
            await context.bot.send_message(uid, T[lang]["evening_set"].format(t=eh), reply_markup=quick_actions_kb(lang))
        elif act == "set_quiet":
            sessions.setdefault(uid, {})["await_quiet"] = True
            await context.bot.send_message(uid, T[lang]["ask_quiet"])
        elif act == "challenge":
            if challenge_get(uid):
                d = challenge_get(uid)
                await context.bot.send_message(uid, T[lang]["challenge_progress"].format(d=d.get("days_done","0"), len=d.get("length_days","7")), reply_markup=quick_actions_kb(lang))
            else:
                challenge_start(uid)
                await context.bot.send_message(uid, T[lang]["challenge_started"], reply_markup=quick_actions_kb(lang))
        elif act == "tip":
            prof = profiles_get(uid) or {}
            tip = _get_daily_tip(prof, lang)
            await context.bot.send_message(uid, f"{T[lang]['daily_tip_prefix']} {tip}", reply_markup=quick_actions_kb(lang))
        return

    # Cycle flow
    if data.startswith("cycle|consent|"):
        val = data.split("|")[-1]
        if val == "yes":
            sessions.setdefault(uid, {})["await_cycle_last"] = True
            await context.bot.send_message(uid, T[lang]["cycle_ask_last"])
        else:
            await context.bot.send_message(uid, T[lang]["thanks"], reply_markup=quick_actions_kb(lang))
        return

    # Mini-intake free text trigger
    if data.startswith("mini|write|"):
        key = data.split("|",2)[2]
        s = sessions.setdefault(uid, {"mini_active": True, "mini_step": 0, "mini_answers": {}})
        s["mini_wait_key"] = key
        await q.edit_message_text(T[lang]["write"] + "…")
        return

    # Numeric 0–10 check-in (episodes)
    if data.startswith("num|"):
        try:
            val = int(data.split("|")[1])
        except:
            val = 5
        ep = episode_find_open(uid)
        if ep:
            if val <= 3:
                episode_set(ep["episode_id"], "status", "closed")
                await context.bot.send_message(uid, T[lang]["checkin_better"], reply_markup=quick_actions_kb(lang))
            else:
                await context.bot.send_message(uid, T[lang]["checkin_worse"], reply_markup=quick_actions_kb(lang))
        else:
            await context.bot.send_message(uid, T[lang]["thanks"], reply_markup=quick_actions_kb(lang))
        return

    # Feedback
    if data.startswith("fb|"):
        kind = data.split("|")[1]
        if kind in {"good","bad"}:
            feedback_add(iso(utcnow()), uid, name="", username=users_get(uid).get("username",""), rating=("1" if kind=="bad" else "5"), comment="")
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(uid, T[lang]["fb_thanks"], reply_markup=quick_actions_kb(lang))
        elif kind == "free":
            sessions.setdefault(uid, {})["await_fb_msg"] = True
            await q.edit_message_text(T[lang]["fb_write"])
        return


# ---------- Сообщения: мягкий опросник на первом сообщении + быстрые кнопки ----------
async def msg_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    text = (update.message.text or "").strip()
    update_last_seen(uid)
    s = sessions.setdefault(uid, {})

    # Если профиль ещё пустой — запускаем мини-опросник сразу, без команд
    prof_now = profiles_get(uid) or {}
    if profile_is_incomplete(prof_now) and not s.get("mini_active") and not (s.get("ipro") or {}).get("active"):
        await start_mini_intake(context, update.effective_chat.id, lang, uid)
        return

    # Mini-intake free text capture
    if s.get("mini_active") and s.get("mini_wait_key"):
        key = s["mini_wait_key"]
        s["mini_wait_key"] = None
        s["mini_answers"][key] = text
        s["mini_step"] = int(s.get("mini_step", 0)) + 1
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return

    # GM note
    if s.get("await_gm_note"):
        daily_add(iso(utcnow()), uid, mood="", comment=text, energy=None)
        s["await_gm_note"] = False
        await update.message.reply_text(T[lang]["mood_thanks"], reply_markup=quick_actions_kb(lang))
        return

    # Quiet hours
    if s.get("await_quiet"):
        qh = text if re.match(r"^\s*([01]?\d|2[0-3]):[0-5]\d-([01]?\d|2[0-3]):[0-5]\d\s*$", text) else DEFAULT_QUIET_HOURS
        profiles_upsert(uid, {"quiet_hours": qh})
        s["await_quiet"] = False
        await update.message.reply_text(T[lang]["quiet_saved"].format(qh=qh), reply_markup=quick_actions_kb(lang))
        return

    # Cycle flow
    if s.get("await_cycle_last"):
        if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            profiles_upsert(uid, {"cycle_last_date": text})
            s["await_cycle_last"] = False
            s["await_cycle_len"] = True
            await update.message.reply_text(T[lang]["cycle_ask_len"])
        else:
            await update.message.reply_text(T[lang]["cycle_ask_last"])
        return
    if s.get("await_cycle_len"):
        try:
            n = int(re.findall(r"\d+", text)[0])
        except:
            n = 28
        profiles_upsert(uid, {"cycle_avg_len": str(max(21, min(40, n))), "cycle_enabled": "1"})
        s["await_cycle_len"] = False
        await update.message.reply_text(T[lang]["cycle_saved"], reply_markup=quick_actions_kb(lang))
        return

    # Feedback free-form
    if s.get("await_fb_msg"):
        feedback_add(iso(utcnow()), uid, name="", username=users_get(uid).get("username",""), rating="0", comment=text[:800])
        s["await_fb_msg"] = False
        await update.message.reply_text(T[lang]["fb_thanks"], reply_markup=quick_actions_kb(lang))
        return

    # Лаборатория: ожидаем город
    if s.get("await_lab_city"):
        city = text[:60]
        s["await_lab_city"] = False
        msg = {
            "ru": f"🧪 Ок, отмечу: {city}. Я подскажу популярные сети: Синэво/Діла/MedLab (зависит от города). Уточните нужный анализ — подскажу подготовку.",
            "uk": f"🧪 Добре, відмічаю: {city}. Популярні мережі: Сінево/Діла/MedLab (залежить від міста). Напишіть, який аналіз — підкажу підготовку.",
            "es": f"🧪 Anotado: {city}. Te puedo sugerir redes comunes según ciudad. Dime qué análisis necesitas y te digo la preparación.",
            "en": f"🧪 Noted: {city}. I can suggest common lab chains by city. Tell me which test you need and I’ll share prep tips."
        }[lang]
        await update.message.reply_text(msg, reply_markup=quick_actions_kb(lang))
        return

    # Health60 awaiting
    if s.get("awaiting_h60_text"):
        s["awaiting_h60_text"] = False
        await _process_health60(uid, lang, text, update.message)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(T[lang]["fb_good"], callback_data="fb|good"),
             InlineKeyboardButton(T[lang]["fb_bad"],  callback_data="fb|bad")],
            [InlineKeyboardButton(T[lang]["fb_free"], callback_data="fb|free")]
        ])
        await update.message.reply_text(T[lang]["ask_fb"], reply_markup=kb)
        return

    # Основной ассистент (персонализированный)
    prof = profiles_get(uid) or {}
    prefix = personalized_prefix(lang, prof)
    route = llm_router_answer(text, lang, prof)
    reply = route.get("assistant_reply") or T[lang]["unknown"]
    followups = route.get("followups") or []
    lines = [reply]
    if followups:
        lines.append("")
        lines.append("— " + "\n— ".join([f for f in followups if f.strip()][:4]))
    final = (prefix + "\n" if prefix else "") + "\n".join(lines).strip()
    await send_unique(update.message, uid, final, reply_markup=quick_actions_kb(lang))


# ---------- Main ----------
def main():
    if not TELEGRAM_TOKEN:
        logging.error("TELEGRAM_TOKEN is not set")
        return
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # PRO-intake (group 0/1)
    register_intake_pro(app)

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("delete_data", cmd_delete_data))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("health60", cmd_health60))
    app.add_handler(CommandHandler("mood", cmd_mood))
    app.add_handler(CommandHandler("energy", cmd_energy))
    app.add_handler(CommandHandler("hydrate", cmd_hydrate))
    app.add_handler(CommandHandler("skintip", cmd_skintip))
    app.add_handler(CommandHandler("cycle", cmd_cycle))
    app.add_handler(CommandHandler("youth", cmd_youth))
    app.add_handler(CommandHandler("settz", cmd_settz))
    app.add_handler(CommandHandler("checkin_on", cmd_checkin_on))
    app.add_handler(CommandHandler("checkin_off", cmd_checkin_off))
    app.add_handler(CommandHandler("evening_on", cmd_evening_on))
    app.add_handler(CommandHandler("evening_off", cmd_evening_off))
    app.add_handler(CommandHandler("ru", cmd_lang_ru))
    app.add_handler(CommandHandler("uk", cmd_lang_uk))
    app.add_handler(CommandHandler("en", cmd_lang_en))
    app.add_handler(CommandHandler("es", cmd_lang_es))

    # Callbacks & messages (после PRO групп)
    app.add_handler(CallbackQueryHandler(cb_handler), group=2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_text), group=2)

    logging.info("Starting polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
