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
# === PRO-INTAKE (опционально) ===
try:
    from intake_pro import register_intake_pro  # noqa
except Exception:
    register_intake_pro = None

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

# Max auto messages per local day (policy)
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

        # New: evening check-in
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

        # New: evening check-in
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

        # New: evening check-in
        "evening_intro": "Вечірній чек-ін:",
        "evening_tip_btn": "🪄 Порада дня",
        "evening_set": "Вечірній чек-ін встановлено на {t} (локально).",
        "evening_off": "Вечірній чек-ін вимкнено.",
    },
}
# Для простоты оставим испанский как английский (заглушка)
T["es"] = T["en"]

# --- Personalized prefix shown before LLM reply ---
def personalized_prefix(lang: str, profile: dict) -> str:
    """
    Строит короткий префикс. Показываем его, если заполнены ≥2 из полей sex/age/goal.
    Безопасно работает даже если ключей/языка нет.
    """
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
        render_cb = context.application.bot_data.get("render_menu_cb")
        if callable(render_cb):
            await render_cb(update, context)
        else:
            await context.application.bot.send_message(q.message.chat_id, "/start")

# === NEW: MINI-INTAKE (с первого сообщения) ===
# добавили новые шаги: conditions, meds_allergies (фритекст), habits
MINI_KEYS = ["sex","age","goal","diet_focus","steps_target","conditions","meds_allergies","habits"]
MINI_FREE_KEYS: Set[str] = {"meds_allergies"}  # требующие фритекста по кнопке "✍️"
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
    # для ключей с фритекстом — отдельная кнопка ✍️
    if key in MINI_FREE_KEYS or (not opts):
        rows.append([InlineKeyboardButton(T[lang]["write"], callback_data=f"mini|write|{key}")])
    rows.append([InlineKeyboardButton(T[lang]["skip"], callback_data=f"mini|skip|{key}")])
    return InlineKeyboardMarkup(rows)

# >>> Overwrite ask_next_mini to avoid calling gate_show with dummy Update
async def ask_next_mini(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: int):
    s = sessions.get(uid, {})
    step_idx = s.get("mini_step", 0)
    if step_idx >= len(MINI_KEYS):
        # финальная запись
        answers = s.get("mini_answers", {})
        # спец-обработка meds_allergies: пишем и в meds, и в allergies (одним текстом)
        if answers.get("meds_allergies"):
            profiles_upsert(uid, {"meds":answers["meds_allergies"], "allergies":answers["meds_allergies"]})
        profiles_upsert(uid, answers)
        sessions[uid]["mini_active"] = False
        await context.bot.send_message(chat_id, T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
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
        # Profiles — extended headers with personalization fields
        ws_profiles = _ensure_ws("Profiles", [
            "user_id","sex","age","goal","goals","conditions","meds","allergies",
            "sleep","activity","diet","diet_focus","steps_target","habits",
            "cycle_enabled","cycle_last_date","cycle_avg_len","last_cycle_tip_date",
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
        # Reminders
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
                # merge (не перезатираем вручную выставленные значения)
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

# --------- Time helpers (tz + quiet hours) ---------
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

# --------- AUTO: day-limit helpers ----------
def _user_lang(uid: int) -> str:
    return norm_lang(users_get(uid).get("lang") or "en")

def _user_tz_off(uid: int) -> int:
    try:
        return int(str(users_get(uid).get("tz_offset") or "0"))
    except Exception:
        return 0

def user_local_now(uid: int) -> datetime:
    return utcnow() + timedelta(hours=_user_tz_off(uid))

def local_to_utc_dt(uid: int, local_dt: datetime) -> datetime:
    return (local_dt - timedelta(hours=_user_tz_off(uid))).replace(tzinfo=timezone.utc)

def _user_quiet_hours(uid: int) -> str:
    prof = profiles_get(uid) or {}
    qh = (prof.get("quiet_hours") or "").strip()
    return qh if qh else DEFAULT_QUIET_HOURS

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
        # новый день
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

# --------- Scheduling from sheet (existing reminders) ---------
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
        # evening?
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
        # тихие часы — не затрагиваем: это ответ на активный эпизод
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
    # простая схема: goal|sex|ageband
    return f"{goal}|{sex}|{band}"

def rules_fetch(topic: str, segment: str) -> Optional[dict]:
    try:
        rows = ws_rules.get_all_records() if SHEETS_ENABLED else MEM_RULES
        # сначала точное совпадение
        for r in rows:
            if str(r.get("enabled","1")).strip() in {"1","yes","true"} and \
               (r.get("topic","").strip().lower() == topic.strip().lower()) and \
               (r.get("segment","").strip().lower() == segment.strip().lower()):
                return r
        # затем fallback по topic
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
    # Rules override
    seg = _build_segment(profile)
    r = rules_fetch("nutrition", seg)
    if r:
        cit = r.get("citation","").strip()
        base = r.get("advice_text","").strip()
        tip = base if not cit else f"{base} ({cit})"
        return tip

    # Simple segmented tips (fallback)
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
    # Rules override (skin)
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

# --------- UI helpers (клавиатуры) ----------
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

# ----- Synonyms fallback for pain triage -----
PAIN_LOC_SYNS = {
    "ru": {"Голова":["голова","голове","головная","мигрень","висок","темя","лоб"],
           "Горло":["горло","в горле","ангина","тонзиллит"],
           "Спина":["спина","в спине","поясница","пояснич","лопатк","позвон","сзади"],
           "Живот":["живот","внизу живота","эпигастр","желудок","киш","подребер"],
           "Другое":["другое"]},
    "uk": {"Голова":["голова","в голові","мігрень","скроня","лоб"],
           "Горло":["горло","в горлі","ангіна","тонзиліт"],
           "Спина":["спина","поперек","лопатк","хребет"],
           "Живіт":["живіт","внизу живота","шлунок","киш"],
           "Інше":["інше"]},
    "en": {"Head":["head","headache","migraine","temple","forehead"],
           "Throat":["throat","sore throat","tonsil"],
           "Back":["back","lower back","spine","shoulder blade"],
           "Belly":["belly","stomach","abdomen","tummy","epigastr"],
           "Other":["other"]},
    "es": {"Head":["cabeza","dolor de cabeza","migraña","sien","frente"],
           "Throat":["garganta","dolor de garganta","amígdala"],
           "Back":["espalda","lumbar","columna","omóplato"],
           "Belly":["vientre","estómago","abdomen","barriga","epigastrio"],
           "Other":["otro","otra","otros"]}
}
PAIN_KIND_SYNS = {
    "ru":{"Тупая":["туп","ноющ","тянущ"],"Острая":["остр","колющ","режущ"],
          "Пульсирующая":["пульс"],"Давящая":["давит","сдавлив","стягив"]},
    "uk":{"Тупий":["туп","ниюч"],"Гострий":["гостр","колюч","ріжуч"],
          "Пульсуючий":["пульс"],"Тиснучий":["тисн","стискає"]},
    "en":{"Dull":["dull","aching","pulling"],"Sharp":["sharp","stabbing","cutting"],
          "Pulsating":["puls","throbb"],"Pressing":["press","tight","squeez"]},
    "es":{"Dull":["sordo","leve","molesto"],"Sharp":["agudo","punzante","cortante"],
          "Pulsating":["pulsátil","palpitante"],"Pressing":["opresivo","presión","apretado"]}
}
RED_FLAG_SYNS = {
    "ru":{"Высокая температура":["высокая темп","жар","39","40"],
          "Рвота":["рвота","тошнит и рв","блюёт","блюет"],
          "Слабость/онемение":["онем","слабость в конеч","провисло","асимметрия"],
          "Нарушение речи/зрения":["речь","говорить не","зрение","двоит","искры"],
          "Травма":["травма","удар","падение","авария"],
          "Нет":["нет","ничего","none","нема","відсут"]},
    "uk":{"Висока температура":["висока темп","жар","39","40"],
          "Блювання":["блюван","рвота"],
          "Слабкість/оніміння":["онім","слабк","провисло"],
          "Проблеми з мовою/зором":["мова","говорити","зір","двоїть"],
          "Травма":["травма","удар","падіння","аварія"],
          "Немає":["нема","ні","відсут","none"]},
    "en":{"High fever":["high fever","fever","39","102"],
          "Vomiting":["vomit","throwing up"],
          "Weakness/numbness":["numb","weakness","droop"],
          "Speech/vision problems":["speech","vision","double"],
          "Trauma":["trauma","injury","fall","accident"],
          "None":["none","no"]},
    "es":{"High fever":["fiebre alta","fiebre","39","40"],
          "Vomiting":["vómito","vomitar"],
          "Weakness/numbness":["debilidad","entumecimiento","caída facial"],
          "Speech/vision problems":["habla","visión","doble"],
          "Trauma":["trauma","lesión","caída","accidente"],
          "None":["ninguno","no"]}
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

# ------------- Commands (часть продолжится ниже в Части 2) -------------
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
        # Evening, если задан
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
# ====== CONTINUATION (PART 2/2) ======

# ---------- Helpers (lang switch, local time, reminders) ----------
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
    # local_dt here is a naive "local" aware only by offset; convert by subtracting tz_offset
    return (local_dt - timedelta(hours=_user_tz_off(uid))).replace(tzinfo=timezone.utc)

def _schedule_oneoff_with_sheet(context: ContextTypes.DEFAULT_TYPE, uid: int, when_local: datetime, text: str):
    # adjust for quiet hours
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

# ---------- /profile (fallback button) ----------
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    # If pro intake is registered it will handle "intake:start"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🧩 Start intake" if lang=="en" else "🧩 Начать опрос",
                                                      callback_data="intake:start")]])
    await update.message.reply_text(T[lang]["profile_intro"], reply_markup=kb)

# ---------- /settz (+/-HH) ----------
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
        # Reschedule daily
        u = users_get(uid)
        hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
        if _has_jq_ctx(context):
            schedule_daily_checkin(context.application, uid, off, hhmm, lang)
    except Exception as e:
        logging.error(f"/settz error: {e}")
        await update.message.reply_text("Failed to set timezone offset.")

# ---------- /checkin_on HH:MM & /checkin_off ----------
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

# ---------- Language quick switches ----------
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

# ---------- Youth pack commands ----------
async def cmd_energy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    # buttons 1-5
    row = [InlineKeyboardButton(str(i), callback_data=f"energy|rate|{i}") for i in range(1,6)]
    kb = InlineKeyboardMarkup([row])
    await update.message.reply_text(T[lang]["gm_energy_q"], reply_markup=kb)

async def cmd_hydrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    tip = T[lang]["hydrate_nudge"]
    # log as daily with mood="", comment=tip
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
        await ask_next_mini(context, q.message.chat_id, lang, uid)
        return

    # Menu quick actions
    if data.startswith("menu|"):
        act = data.split("|",1)[1]
        fake_update = update  # reuse context; send new prompts
        if act == "h60":
            sessions.setdefault(uid, {})["awaiting_h60_text"] = True
            await context.bot.send_message(uid, T[lang]["h60_intro"])
        elif act == "energy":
            await cmd_energy(fake_update, context)
        elif act == "hydrate":
            await cmd_hydrate(fake_update, context)
        elif act == "skintip":
            await cmd_skintip(fake_update, context)
        elif act == "cycle":
            await cmd_cycle(fake_update, context)
        elif act == "youth":
            await cmd_youth(fake_update, context)
        return

    # Health60 flow actions
    if data.startswith("h60|"):
        parts = data.split("|")
        sub = parts[1] if len(parts)>1 else ""
        if sub == "accept":
            choice = parts[2] if len(parts)>2 else "later"
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(uid, T[lang]["thanks"])
        elif sub == "rem":
            opt = parts[2] if len(parts)>2 else ""
            now_local = user_local_now(uid)
            if opt == "4h":
                when_local = now_local + timedelta(hours=4)
                adj = _schedule_oneoff_with_sheet(context, uid, when_local, T[lang]["thanks"])
                await context.bot.send_message(uid, f"⏰ Ok, {T[lang]['act_rem_4h'][2:]} — {adj.strftime('%b %d %H:%M')}")
            elif opt == "eve":
                h, m = hhmm_tuple(DEFAULT_EVENING_LOCAL)
                when_local = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
                if when_local < now_local:
                    when_local += timedelta(days=1)
                adj = _schedule_oneoff_with_sheet(context, uid, when_local, T[lang]["thanks"])
                await context.bot.send_message(uid, f"⏰ {T[lang]['gm_evening_btn']} — {adj.strftime('%b %d %H:%M')}")
            elif opt == "morn":
                h, m = hhmm_tuple(DEFAULT_CHECKIN_LOCAL)
                when_local = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
                if when_local < now_local:
                    when_local += timedelta(days=1)
                adj = _schedule_oneoff_with_sheet(context, uid, when_local, T[lang]["thanks"])
                await context.bot.send_message(uid, f"⏰ {T[lang]['act_rem_morn']} — {adj.strftime('%b %d %H:%M')}")
        elif sub == "episode":
            act2 = parts[2] if len(parts)>2 else ""
            if act2 == "save":
                # minimal save: create an open episode if none
                ep = episode_find_open(uid)
                if not ep:
                    eid = episode_create(uid, topic="general", severity=3, red="None")
                    await context.bot.send_message(uid, f"{T[lang]['act_saved']} (id: {eid})")
                else:
                    await context.bot.send_message(uid, T[lang]["act_saved"])
        elif sub == "neck":
            txt = "Neck routine: slow circles ×5 each side; chin tucks ×8; gentle stretch 20–30s." if lang=="en" else \
                  "Шея: медленные круги ×5 в каждую сторону; подтягивания подбородка ×8; мягкая растяжка 20–30с."
            await context.bot.send_message(uid, txt)
        elif sub == "er":
            await context.bot.send_message(uid, T[lang]["er_text"])
        return

    # Numeric scale for episode check-in (0–10)
    if data.startswith("num|"):
        try:
            score = int(data.split("|",1)[1])
        except Exception:
            score = 0
        ep = episode_find_open(uid)
        if ep:
            episode_set(ep["episode_id"], "last_score", str(score))
            if score <= 3:
                await q.edit_message_text(T[lang]["checkin_better"])
            else:
                await q.edit_message_text(T[lang]["checkin_worse"])
        else:
            await q.edit_message_text(T[lang]["thanks"])
        return

    # Daily GM
    if data.startswith("gm|"):
        sub = data.split("|",2)[1]
        if sub == "mood":
            mood = data.split("|",3)[2]
            daily_add(iso(utcnow()), uid, mood=mood, comment="", energy=None)
            await q.edit_message_text(T[lang]["mood_thanks"])
        elif sub == "note":
            sessions.setdefault(uid, {})["awaiting_gm_note"] = True
            await context.bot.send_message(uid, T[lang]["fb_write"])
        return

    # Energy 1–5
    if data.startswith("energy|rate|"):
        try:
            val = int(data.split("|",2)[2])
        except Exception:
            val = None
        daily_add(iso(utcnow()), uid, mood="", comment="", energy=val)
        await q.edit_message_text(T[lang]["gm_energy_done"])
        # if challenge running, increment
        ch = challenge_get(uid)
        if ch:
            done = challenge_inc(uid)
            await context.bot.send_message(uid, T[lang]["challenge_progress"].format(d=done, len=ch.get("length_days","7")))
        return

    # Youth quick actions
    if data.startswith("youth:"):
        sub = data.split(":",1)[1]
        if sub == "gm_evening":
            now_local = user_local_now(uid)
            h, m = hhmm_tuple(DEFAULT_EVENING_LOCAL)
            when_local = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
            if when_local < now_local:
                when_local += timedelta(days=1)
            adj = _schedule_oneoff_with_sheet(context, uid, when_local, T[lang]["hydrate_nudge"])
            await context.bot.send_message(uid, f"⏰ {T[lang]['gm_evening_btn']} — {adj.strftime('%b %d %H:%M')}")
        elif sub == "set_quiet":
            sessions.setdefault(uid, {})["awaiting_quiet"] = True
            await context.bot.send_message(uid, T[lang]["ask_quiet"])
        elif sub == "challenge":
            if challenge_get(uid):
                await context.bot.send_message(uid, "Challenge already active.")
            else:
                challenge_start(uid)
                await context.bot.send_message(uid, T[lang]["challenge_started"])
        return

    # Feedback
    if data.startswith("fb|"):
        kind = data.split("|",1)[1]
        ts = iso(utcnow())
        u = update.effective_user
        if kind == "good":
            feedback_add(ts, uid, u.full_name, u.username, "good", "")
            await q.edit_message_text(T[lang]["fb_thanks"])
        elif kind == "bad":
            feedback_add(ts, uid, u.full_name, u.username, "bad", "")
            await q.edit_message_text(T[lang]["fb_thanks"])
        elif kind == "free":
            sessions.setdefault(uid, {})["awaiting_feedback"] = True
            await context.bot.send_message(uid, T[lang]["fb_write"])
        return

    # Cycle tracking
    if data.startswith("cycle|"):
        parts = data.split("|")
        if len(parts)>=3 and parts[1]=="consent":
            if parts[2]=="yes":
                sessions.setdefault(uid, {})["awaiting_cycle_last"] = True
                await context.bot.send_message(uid, T[lang]["cycle_ask_last"])
            else:
                profiles_upsert(uid, {"cycle_enabled":"no"})
                await context.bot.send_message(uid, T[lang]["thanks"])
        return

# ---------- TEXT messages ----------
async def msg_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    utext = (update.message.text or "").strip()
    lang = _user_lang(uid)
    s = sessions.setdefault(uid, {})

    # Feedback message
    if s.get("awaiting_feedback"):
        ts = iso(utcnow())
        u = update.effective_user
        feedback_add(ts, uid, u.full_name, u.username, "free", utext[:500])
        s["awaiting_feedback"] = False
        await update.message.reply_text(T[lang]["fb_thanks"])
        return

    # GM note
    if s.get("awaiting_gm_note"):
        daily_add(iso(utcnow()), uid, mood="", comment=utext[:400], energy=None)
        s["awaiting_gm_note"] = False
        await update.message.reply_text(T[lang]["mood_thanks"])
        return

    # Quiet hours
    if s.get("awaiting_quiet"):
        try:
            # validate
            parse_quiet_hours(utext)
            profiles_upsert(uid, {"quiet_hours": utext})
            await update.message.reply_text(T[lang]["quiet_saved"].format(qh=utext))
        except Exception:
            await update.message.reply_text("Format: 22:00-08:00")
        s["awaiting_quiet"] = False
        return

    # Cycle tracking steps
    if s.get("awaiting_cycle_last"):
        # expect YYYY-MM-DD
        try:
            datetime.strptime(utext[:10], "%Y-%m-%d")
            profiles_upsert(uid, {"cycle_enabled":"yes","cycle_last_date":utext[:10]})
            s["awaiting_cycle_last"] = False
            s["awaiting_cycle_len"] = True
            await update.message.reply_text(T[lang]["cycle_ask_len"])
            return
        except Exception:
            await update.message.reply_text("Format: YYYY-MM-DD")
            return
    if s.get("awaiting_cycle_len"):
        try:
            L = int(re.search(r"\d+", utext).group(0))
            L = max(21, min(40, L))
            profiles_upsert(uid, {"cycle_avg_len":str(L)})
            s["awaiting_cycle_len"] = False
            await update.message.reply_text(T[lang]["cycle_saved"])
            # optional phase tip
            prof = profiles_get(uid) or {}
            phase = _cycle_phase(prof.get("cycle_last_date",""), int(prof.get("cycle_avg_len") or 28))
            tip = _cycle_tip(lang, phase) if phase else None
            if tip:
                await update.message.reply_text(tip)
        except Exception:
            await update.message.reply_text("Enter a number like 28")
        return

    # Health60 symptom
    if s.get("awaiting_h60_text"):
        s["awaiting_h60_text"] = False
        await _process_health60(uid, lang, utext, update.message)
        return

    # Router: main smart assistant
    prof = profiles_get(uid) or {}
    # detect language from text but stick to user pref if not clear
    det_lang = detect_lang_from_text(utext, lang)
    lang = det_lang or lang
    users_set(uid, "lang", lang)
    context.user_data["lang"] = lang

    data = llm_router_answer(utext, lang, prof)
    prefix = personalized_prefix(lang, prof)
    body = data.get("assistant_reply") or T[lang]["unknown"]
    final = (prefix + "\n" if prefix else "") + body

    # feedback row
    fb_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["fb_good"], callback_data="fb|good"),
         InlineKeyboardButton(T[lang]["fb_bad"],  callback_data="fb|bad"),
         InlineKeyboardButton(T[lang]["fb_free"], callback_data="fb|free")]
    ])
    await send_unique(update.message, uid, final, reply_markup=fb_kb, force=True)

# ---------- /er ----------
async def cmd_er(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    await update.message.reply_text(T[lang]["er_text"])

# ---------- Error handler ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.exception("Unhandled error", exc_info=context.error)

# ---------- Application builder & handlers ----------
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Optional: register Pro Intake module
    if register_intake_pro:
        try:
            register_intake_pro(
                app,
                gspread_client=GSPREAD_CLIENT,
                spreadsheet_id=SPREADSHEET_ID_FOR_INTAKE,
                profiles_get=profiles_get,
                profiles_upsert=profiles_upsert,
                users_get=users_get,
                T=T
            )
            logging.info("intake_pro registered.")
        except Exception as e:
            logging.warning(f"intake_pro registration failed: {e}")

    # Commands
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("privacy",      cmd_privacy))
    app.add_handler(CommandHandler("pause",        cmd_pause))
    app.add_handler(CommandHandler("resume",       cmd_resume))
    app.add_handler(CommandHandler("delete_data",  cmd_delete_data))

    app.add_handler(CommandHandler("profile",      cmd_profile))
    app.add_handler(CommandHandler("settz",        cmd_settz))
    app.add_handler(CommandHandler("checkin_on",   cmd_checkin_on))
    app.add_handler(CommandHandler("checkin_off",  cmd_checkin_off))

    app.add_handler(CommandHandler("health60",     cmd_health60))
    app.add_handler(CommandHandler("energy",       cmd_energy))
    app.add_handler(CommandHandler("hydrate",      cmd_hydrate))
    app.add_handler(CommandHandler("skintip",      cmd_skintip))
    app.add_handler(CommandHandler("cycle",        cmd_cycle))
    app.add_handler(CommandHandler("youth",        cmd_youth))
    app.add_handler(CommandHandler("er",           cmd_er))

    app.add_handler(CommandHandler("ru",           cmd_lang_ru))
    app.add_handler(CommandHandler("en",           cmd_lang_en))
    app.add_handler(CommandHandler("uk",           cmd_lang_uk))
    app.add_handler(CommandHandler("es",           cmd_lang_es))

    # Callbacks and text
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_text))

    app.add_error_handler(error_handler)

    logging.info("Starting polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

# Entrypoint
if __name__ == "__main__":
    main()
