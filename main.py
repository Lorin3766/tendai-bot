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
        # Quickbar
        "quick_title":"Quick actions",
        "quick_rem":"⏰ Reminder",
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
        # Quickbar
        "quick_title":"Быстрые действия",
        "quick_rem":"⏰ Напоминание",
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
        "remind_when":"Коли нагадати та спитати самопочуття?",
        "accept_opts":["✅ Так","🔁 Пізніше","✖️ Ні"],
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
        # Quickbar
        "quick_title":"Швидкі дії",
        "quick_rem":"⏰ Нагадування",
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
    return ws.row_values(1) if ws and ws.row_values(1) else []

def _ws_ensure_columns(ws, desired_headers: List[str]):
    """
    Надёжное добавление недостающих заголовков + авторасширение листа.
    """
    try:
        current = _ws_headers(ws)
        # если лист пуст — сначала обеспечим нужное число колонок
        if not current:
            if ws.col_count < len(desired_headers):
                ws.add_cols(len(desired_headers) - ws.col_count)
            ws.append_row(desired_headers)
            return
        # если не хватает колонок — расширяем сетку
        if ws.col_count < len(desired_headers):
            ws.add_cols(len(desired_headers) - ws.col_count)
        # дописываем недостающие заголовки справа
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
                # создаём с большим запасом колонок
                ws = ss.add_worksheet(title=title, rows=4000, cols=max(50, len(headers)))
                ws.append_row(headers)
            if not ws.get_all_values():
                ws.append_row(headers)
            _ws_ensure_columns(ws, headers)
            return ws

        ws_feedback   = _ensure_ws("Feedback",   ["timestamp","user_id","name","username","rating","comment"])
        ws_users      = _ensure_ws("Users",      ["user_id","username","lang","consent","tz_offset","checkin_hour","evening_hour","paused","last_seen","last_auto_date","last_auto_count"])
        ws_profiles   = _ensure_ws("Profiles",   [
            "user_id","sex","age","goal","goals","conditions","meds","allergies",
            "sleep","activity","diet","diet_focus","steps_target","habits",
            "cycle_enabled","cycle_last_date","cycle_avg_len","last_cycle_tip_date",
            "quiet_hours","consent_flags","notes","updated_at","city"
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
    return MEM_PROFILES.get(uid, {}
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


# ------------- Commands -------------
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

    prof = profiles_get(user.id)
    if profile_is_incomplete(prof):
        await start_mini_intake(context, update.effective_chat.id, lang, user.id)
    else:
        await update.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))

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


# ---------- Daily Check-in job ----------
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
        [InlineKeyboardButton(T[lang]["gm_evening_btn"], callback_data="youth:gm_evening")]
    ])
    try:
        await context.bot.send_message(uid, T[lang]["daily_gm"], reply_markup=kb)
    except Exception as e:
        logging.error(f"job_daily_checkin send error: {e}")


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

async def _process_health60(uid: int, lang: str, text: str, msg_obj):
    prof = profiles_get(uid) or {}
    prefix = personalized_prefix(lang, prof)
    plan = health60_make_plan(lang, text, prof)
    final = (prefix + "\n" if prefix else "") + plan

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["accept_opts"][0], callback_data="h60|accept|yes"),
         InlineKeyboardButton(T[lang]["accept_opts"][1], callback_data="h60|accept|later"),
         InlineKeyboardButton(T[lang]["accept_opts"][2], callback_data="h60|accept|no")],
        [InlineKeyboardButton(T[lang]["act_rem_4h"],  callback_data="h60|rem|4h"),
         InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="h60|rem|eve"),
         InlineKeyboardButton(T[lang]["act_rem_morn"],callback_data="h60|rem|morn")],
        [InlineKeyboardButton(T[lang]["act_save_episode"], callback_data="h60|episode|save"),
         InlineKeyboardButton(T[lang]["act_ex_neck"],      callback_data="h60|neck"),
         InlineKeyboardButton(T[lang]["act_er"],           callback_data="h60|er")]
    ])
    await msg_obj.reply_text(final, reply_markup=kb)


# ---------- Youth pack ----------
async def cmd_energy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    row = [InlineKeyboardButton(str(i), callback_data=f"energy|rate|{i}") for i in range(1,6)]
    kb = InlineKeyboardMarkup([row])
    await update.message.reply_text(T[lang]["gm_energy_q"], reply_markup=kb)

async def cmd_hydrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    tip = T[lang]["hydrate_nudge"]
    daily_add(iso(utcnow()), uid, mood="", comment=tip, energy=None)
    await update.message.reply_text(tip)

async def cmd_skintip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    prof = profiles_get(uid) or {}
    age = int(re.search(r"\d+", str(prof.get("age") or "0")).group(0)) if re.search(r"\d+", str(prof.get("age") or "")) else 0
    sex = (prof.get("sex") or "").lower()
    tip = _get_skin_tip(lang, sex, age)
    await update.message.reply_text(f"{T[lang]['daily_tip_prefix']} {tip}")

async def cmd_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["yes"], callback_data="cycle|consent|yes"),
                                InlineKeyboardButton(T[lang]["no"],  callback_data="cycle|consent|no")]])
    await update.message.reply_text(T[lang]["cycle_consent"], reply_markup=kb)

async def cmd_youth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["gm_energy"], callback_data="menu|energy"),
         InlineKeyboardButton(T[lang]["hydrate_btn"], callback_data="menu|hydrate")],
        [InlineKeyboardButton(T[lang]["skintip_btn"], callback_data="menu|skintip"),
         InlineKeyboardButton(T[lang]["gm_evening_btn"], callback_data="youth:gm_evening")],
        [InlineKeyboardButton(T[lang]["set_quiet_btn"], callback_data="youth:set_quiet"),
         InlineKeyboardButton(T[lang]["challenge_btn"], callback_data="youth:challenge")]
    ])
    await update.message.reply_text(T[lang]["youth_pack"], reply_markup=kb)


# ---------- Callback Query Handler ----------
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

    # Menu quick actions
    if data.startswith("menu|"):
        act = data.split("|",1)[1]
        if act == "h60":
            sessions.setdefault(uid, {})["awaiting_h60_text"] = True
            await context.bot.send_message(uid, T[lang]["h60_intro"])
        elif act == "energy":
            row = [InlineKeyboardButton(str(i), callback_data=f"energy|rate|{i}") for i in range(1,6)]
            kb = InlineKeyboardMarkup([row])
            await context.bot.send_message(uid, T[lang]["gm_energy_q"], reply_markup=kb)
        elif act == "hydrate":
            tip = T[lang]["hydrate_nudge"]
            daily_add(iso(utcnow()), uid, mood="", comment=tip, energy=None)
            await context.bot.send_message(uid, tip)
        elif act == "skintip":
            prof = profiles_get(uid) or {}
            age = int(re.search(r"\d+", str(prof.get("age") or "0")).group(0)) if re.search(r"\d+", str(prof.get("age") or "")) else 0
            sex = (prof.get("sex") or "").lower()
            tip = _get_skin_tip(lang, sex, age)
            await context.bot.send_message(uid, f"{T[lang]['daily_tip_prefix']} {tip}")
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

    # Health60 actions
    if data.startswith("h60|"):
        parts = data.split("|")
        sub = parts[1] if len(parts)>1 else ""
        if sub == "accept":
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(uid, T[lang]["thanks"])
        elif sub == "rem":
            opt = parts[2] if len(parts)>2 else ""
            now_local = user_local_now(uid)
            if opt == "4h":
                when_local = now_local + timedelta(hours=4)
                adj = _schedule_oneoff_with_sheet(context, uid, when_local, T[lang]["thanks"])
                await context.bot.send_message(uid, _fmt_reminder_set(lang, adj, kind="4h"))
            elif opt == "eve":
                eh = users_get(uid).get("evening_hour") or DEFAULT_EVENING_LOCAL
                (hh, mm) = hhmm_tuple(eh)
                target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if target <= now_local:
                    target = target + timedelta(days=1)
                adj = _schedule_oneoff_with_sheet(context, uid, target, T[lang]["thanks"])
                await context.bot.send_message(uid, _fmt_reminder_set(lang, adj, kind="evening"))
            elif opt == "morn":
                mh = users_get(uid).get("checkin_hour") or DEFAULT_CHECKIN_LOCAL
                (hh, mm) = hhmm_tuple(mh)
                target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if target <= now_local:
                    target = target + timedelta(days=1)
                adj = _schedule_oneoff_with_sheet(context, uid, target, T[lang]["thanks"])
                await context.bot.send_message(uid, _fmt_reminder_set(lang, adj, kind="morning"))
        elif sub == "episode":
            act = parts[2] if len(parts)>2 else ""
            if act == "save":
                eid = episode_create(uid, topic="h60", severity=5, red="")
                sessions.setdefault(uid, {})["last_eid"] = eid
                await context.bot.send_message(uid, T[lang]["act_saved"])
        elif sub == "neck":
            text_map = {
                "ru": "🧘 5 минут для шеи:\n• Медленные наклоны: вперёд/назад/в стороны ×6\n• Повороты головы ×6\n• Плечи: подъём/круги ×10\n• Мягкое вытяжение, без боли\n• Дыхание 4–6 циклов",
                "uk": "🧘 5 хв для шиї:\n• Похили: вперед/назад/в сторони ×6\n• Повороти голови ×6\n• Плечі: підйом/кола ×10\n• М’яке розтягнення без болю\n• Дихання 4–6 циклів",
                "es": "🧘 5 min para el cuello:\n• Inclinaciones: adelante/atrás/lados ×6\n• Giros de cabeza ×6\n• Hombros: elevación/círculos ×10\n• Estiramiento suave, sin dolor\n• Respiración 4–6 ciclos",
                "en": "🧘 5-min neck reset:\n• Slow tilts: forward/back/side ×6\n• Head turns ×6\n• Shoulders: shrug/circles ×10\n• Gentle stretch, no pain\n• Breathe 4–6 cycles",
            }
            await context.bot.send_message(uid, text_map[lang])
        elif sub == "er":
            await context.bot.send_message(uid, T[lang]["er_text"])
        return

    # Daily GM actions
    if data.startswith("gm|"):
        _, kind, *rest = data.split("|")
        if kind == "mood":
            mood = rest[0] if rest else "ok"
            daily_add(iso(utcnow()), uid, mood=mood, comment="")
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(uid, T[lang]["mood_thanks"])
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
        await context.bot.send_message(uid, T[lang]["gm_energy_done"])
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
            await context.bot.send_message(uid, T[lang]["evening_set"].format(t=eh))
        elif act == "set_quiet":
            sessions.setdefault(uid, {})["await_quiet"] = True
            await context.bot.send_message(uid, T[lang]["ask_quiet"])
        elif act == "challenge":
            if challenge_get(uid):
                d = challenge_get(uid)
                await context.bot.send_message(uid, T[lang]["challenge_progress"].format(d=d.get("days_done","0"), len=d.get("length_days","7")))
            else:
                challenge_start(uid)
                await context.bot.send_message(uid, T[lang]["challenge_started"])
        elif act == "tip":
            prof = profiles_get(uid) or {}
            tip = _get_daily_tip(prof, lang)
            await context.bot.send_message(uid, f"{T[lang]['daily_tip_prefix']} {tip}")
        return

    # Cycle flow
    if data.startswith("cycle|consent|"):
        val = data.split("|")[-1]
        if val == "yes":
            sessions.setdefault(uid, {})["await_cycle_last"] = True
            await context.bot.send_message(uid, T[lang]["cycle_ask_last"])
        else:
            await context.bot.send_message(uid, T[lang]["thanks"])
        return

    # Mini-intake free text trigger (handled via msg_text)
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
                await context.bot.send_message(uid, T[lang]["checkin_better"])
            else:
                await context.bot.send_message(uid, T[lang]["checkin_worse"])
        else:
            await context.bot.send_message(uid, T[lang]["thanks"])
        return

    # Feedback
    if data.startswith("fb|"):
        kind = data.split("|")[1]
        if kind in {"good","bad"}:
            feedback_add(iso(utcnow()), uid, name="", username=users_get(uid).get("username",""), rating=("1" if kind=="bad" else "5"), comment="")
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(uid, T[lang]["fb_thanks"])
        elif kind == "free":
            sessions.setdefault(uid, {})["await_fb_msg"] = True
            await q.edit_message_text(T[lang]["fb_write"])
        return


def _fmt_reminder_set(lang: str, when_local: datetime, kind: str=""):
    hhmm = _format_hhmm(when_local)
    if lang == "ru":
        base = {"4h": f"⏰ Напомню около {hhmm} (лок.)",
                "evening": f"⏰ Напоминание на {hhmm} (вечером, лок.)",
                "morning": f"⏰ Напоминание на {hhmm} (утром, лок.)"}
    elif lang == "uk":
        base = {"4h": f"⏰ Нагадаю близько {hhmm} (лок.)",
                "evening": f"⏰ Нагадування на {hhmm} (увечері, лок.)",
                "morning": f"⏰ Нагадування на {hhmm} (зранку, лок.)"}
    elif lang == "es":
        base = {"4h": f"⏰ Recordatorio ~{hhmm} (local)",
                "evening": f"⏰ Recordatorio {hhmm} (tarde, local)",
                "morning": f"⏰ Recordatorio {hhmm} (mañana, local)"}
    else:
        base = {"4h": f"⏰ Reminder around {hhmm} (local)",
                "evening": f"⏰ Reminder for {hhmm} (evening, local)",
                "morning": f"⏰ Reminder for {hhmm} (morning, local)"}
    return base.get(kind, base["4h"])


# ---------- Evening Check-in job ----------
async def job_evening_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id"); lang = norm_lang((d.get("lang") or "en"))
    if not uid:
        return
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


# ---------- Extra commands ----------
async def cmd_evening_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    txt = (update.message.text or "")
    m = re.search(r"([01]?\d|2[0-3]):([0-5]\d)", txt)
    hhmm = m.group(0) if m else DEFAULT_EVENING_LOCAL
    users_set(uid, "evening_hour", hhmm)
    if _has_jq_ctx(context):
        schedule_evening_checkin(context.application, uid, _user_tz_off(uid), hhmm, lang)
    await update.message.reply_text(T[lang]["evening_set"].format(t=hhmm))

async def cmd_evening_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    if _has_jq_ctx(context):
        for j in context.application.job_queue.get_jobs_by_name(f"evening_{uid}"):
            j.schedule_removal()
    users_set(uid, "evening_hour", "")
    await update.message.reply_text(T[lang]["evening_off"])

async def cmd_mood(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_good"], callback_data="gm|mood|good"),
         InlineKeyboardButton(T[lang]["mood_ok"],   callback_data="gm|mood|ok"),
         InlineKeyboardButton(T[lang]["mood_bad"],  callback_data="gm|mood|bad")],
        [InlineKeyboardButton(T[lang]["mood_note"], callback_data="gm|note")]
    ])
    await update.message.reply_text(T[lang]["mood_cmd"], reply_markup=kb)


# ---------- General text handler ----------
async def msg_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    text = (update.message.text or "").strip()
    update_last_seen(uid)
    s = sessions.setdefault(uid, {})

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
        await update.message.reply_text(T[lang]["mood_thanks"])
        return

    # Quiet hours
    if s.get("await_quiet"):
        qh = text if re.match(r"^\s*([01]?\d|2[0-3]):[0-5]\d-([01]?\d|2[0-3]):[0-5]\d\s*$", text) else DEFAULT_QUIET_HOURS
        profiles_upsert(uid, {"quiet_hours": qh})
        s["await_quiet"] = False
        await update.message.reply_text(T[lang]["quiet_saved"].format(qh=qh))
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
        await update.message.reply_text(T[lang]["cycle_saved"])
        return

    # Feedback free-form
    if s.get("await_fb_msg"):
        feedback_add(iso(utcnow()), uid, name="", username=users_get(uid).get("username",""), rating="0", comment=text[:800])
        s["await_fb_msg"] = False
        await update.message.reply_text(T[lang]["fb_thanks"])
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

    # Router default (concise assistant mode)
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
    await send_unique(update.message, uid, final)


# ---------- Main ----------
def main():
    if not TELEGRAM_TOKEN:
        logging.error("TELEGRAM_TOKEN is not set")
        return
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # PRO-intake (group 0/1)
    register_intake_pro(app)  # фиксированный pattern r"^intake"

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

    # General callbacks & messages (after PRO groups 0/1)
    app.add_handler(CallbackQueryHandler(cb_handler), group=2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_text), group=2)

    logging.info("Starting polling…")
    app.run_polling()

if __name__ == "__main__":
    main()
