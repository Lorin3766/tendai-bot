# -*- coding: utf-8 -*-
import os
import re
import json
import uuid
import logging
from datetime import datetime, timedelta, timezone, time as dtime
from typing import List, Tuple

from dotenv import load_dotenv
from langdetect import detect, DetectorFactory

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI
import gspread
from gspread.exceptions import SpreadsheetNotFound
from oauth2client.service_account import ServiceAccountCredentials

# ---------------------------
# Boot & Config
# ---------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
DetectorFactory.seed = 0

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
SHEET_NAME = os.getenv("SHEET_NAME", "TendAI Sheets")
SHEET_ID = os.getenv("SHEET_ID", "")  # рекомендуется указать ID
DEFAULT_CHECKIN_LOCAL = "08:30"

# OpenAI клиент
oai = None
if OPENAI_API_KEY:
    try:
        oai = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        logging.error(f"OpenAI init error: {e}")
        oai = None

# Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not creds_json:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")
creds_dict = json.loads(creds_json)
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gclient = gspread.authorize(credentials)

# Открываем книгу по ID, иначе по имени; если нет доступа — создаём
try:
    ss = gclient.open_by_key(SHEET_ID) if SHEET_ID else gclient.open(SHEET_NAME)
except SpreadsheetNotFound:
    logging.warning("Spreadsheet not found by ID/name — creating a new one...")
    ss = gclient.create(SHEET_NAME)

def _get_or_create_ws(title: str, headers: List[str]):
    try:
        ws = ss.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=1000, cols=max(20, len(headers)))
        ws.append_row(headers)
    vals = ws.get_all_values()
    if not vals:
        ws.append_row(headers)
    return ws

ws_feedback = _get_or_create_ws(
    "Feedback", ["timestamp", "user_id", "name", "username", "rating", "comment"]
)
ws_users = _get_or_create_ws(
    "Users", ["user_id", "username", "lang", "consent", "tz_offset", "checkin_hour", "paused"]
)
ws_profiles = _get_or_create_ws(
    "Profiles",
    ["user_id", "sex", "age", "goal", "conditions", "meds", "allergies",
     "sleep", "activity", "diet", "notes", "updated_at"],
)
ws_episodes = _get_or_create_ws(
    "Episodes",
    ["episode_id","user_id","topic","started_at","baseline_severity","red_flags",
     "plan_accepted","target","reminder_at","next_checkin_at","status","last_update","notes"],
)
ws_reminders = _get_or_create_ws(
    "Reminders", ["id", "user_id", "text", "when_utc", "created_at", "status"]
)
ws_daily = _get_or_create_ws(
    "DailyCheckins", ["timestamp", "user_id", "mood", "comment"]
)

# ---------------------------
# In-memory session state
# ---------------------------
sessions: dict[int, dict] = {}

# ---------------------------
# i18n
# ---------------------------
SUPPORTED = {"ru", "en", "uk"}

def norm_lang(code: str | None) -> str:
    if not code:
        return "en"
    c = code.split("-")[0].lower()
    return c if c in SUPPORTED else "en"

T = {
    "en": {
        "welcome": "Hi! I’m TendAI — your health & longevity assistant.\nChoose a topic below or just describe what’s bothering you.",
        "menu": ["Pain", "Throat/Cold", "Sleep", "Stress", "Digestion", "Energy", "Nutrition", "Labs", "Habits", "Longevity", "Profile"],
        "help": "I can help with short checkups, a simple 24–48h plan, reminders and daily check-ins.\nCommands:\n/help, /privacy, /pause, /resume, /delete_data, /profile, /checkin_on [HH:MM], /checkin_off, /settz +3",
        "privacy": "TendAI is not a medical service and can’t replace a doctor.\nWe store minimal data (Sheets) to support reminders.\nUse /delete_data to erase your info.",
        "paused_on": "Notifications paused. Use /resume to enable.",
        "paused_off": "Notifications resumed.",
        "deleted": "All your data in TendAI was deleted. You can /start again anytime.",
        "ask_consent": "May I send you a follow-up later to check how you feel? (Change anytime with /pause or /resume.)",
        "yes": "Yes",
        "no": "No",
        "choose_topic": "Choose a topic:",
        "triage_pain_q1": "Where does it hurt?",
        "triage_pain_q1_opts": ["Head", "Throat", "Back", "Belly", "Other"],
        "triage_pain_q2": "What kind of pain?",
        "triage_pain_q2_opts": ["Dull", "Sharp", "Pulsating", "Pressing"],
        "triage_pain_q3": "How long has it lasted?",
        "triage_pain_q3_opts": ["<3h", "3–24h", ">1 day", ">1 week"],
        "triage_pain_q4": "Rate the pain now (0–10):",
        "triage_pain_q5": "Any of these now?",
        "triage_pain_q5_opts": ["High fever", "Vomiting", "Weakness or numbness", "Speech/vision problems", "Trauma", "None"],
        "plan_header": "Your 24–48h plan:",
        "plan_accept": "Will you try this today?",
        "accept_opts": ["✅ Yes", "🔁 Later", "✖️ No"],
        "remind_when": "When shall I check on you?",
        "remind_opts": ["in 4h", "this evening", "tomorrow morning", "no need"],
        "thanks": "Got it 🙌",
        "checkin_ping": "Quick check-in: how is it now (0–10)?",
        "checkin_better": "Nice! Keep it up 💪",
        "checkin_worse": "Sorry to hear. If you have any red flags or pain ≥7/10, consider seeking medical help.",
        "comment_prompt": "Thanks for your rating 🙏\nWant to add a comment? Just type it in, or send /skip to skip.",
        "comment_saved": "Comment saved, thank you! 🙌",
        "skip_ok": "Skipped.",
        "unknown": "I need a bit more information to help. Where exactly does it hurt? How long has it lasted?",
        "lang_switched": "OK, I’ll reply in English next time.",
        "profile_intro": "Let’s set up your profile (~40s). Use buttons or type your answer.",
        "p_step_1": "Step 1/8. Your sex:",
        "p_step_2": "Step 2/8. Age:",
        "p_step_3": "Step 3/8. Main goal:",
        "p_step_4": "Step 4/8. Chronic conditions (if any):",
        "p_step_5": "Step 5/8. Meds/supplements/allergies:",
        "p_step_6": "Step 6/8. Sleep (bed/wake, e.g. 23:30/07:00):",
        "p_step_7": "Step 7/8. Activity:",
        "p_step_8": "Step 8/8. Diet most of the time:",
        "write": "✍️ Write",
        "skip": "⏭️ Skip",
        "saved_profile": "Saved: ",
        "start_where": "Where do you want to start? (symptom/sleep/nutrition/labs/habits/longevity)",
        "daily_gm": "Good morning! Quick daily check-in:",
        "mood_good": "😃 Good",
        "mood_ok": "😐 Okay",
        "mood_bad": "😣 Poor",
        "mood_note": "✍️ Comment",
        "mood_thanks": "Thanks! Wishing you a smooth day 👋",
        "btn_like": "👍",
        "btn_dislike": "👎",
    },
    "ru": {
        "welcome": "Привет! Я TendAI — ассистент здоровья и долголетия.\nВыбери тему ниже или опиши, что беспокоит.",
        "menu": ["Боль", "Горло/простуда", "Сон", "Стресс", "Пищеварение", "Энергия", "Питание", "Анализы", "Привычки", "Долголетие", "Профиль"],
        "help": "Помогаю короткой проверкой, планом на 24–48 ч, напоминаниями и ежедневными чек-инами.\nКоманды:\n/help, /privacy, /pause, /resume, /delete_data, /profile, /checkin_on [ЧЧ:ММ], /checkin_off, /settz +3",
        "privacy": "TendAI не заменяет врача. Храним минимум данных (Sheets) для напоминаний.\nКоманда /delete_data удалит всё.",
        "paused_on": "Напоминания поставлены на паузу. Включить: /resume",
        "paused_off": "Напоминания снова включены.",
        "deleted": "Все ваши данные в TendAI удалены. Можно заново начать через /start.",
        "ask_consent": "Можно прислать напоминание позже, чтобы узнать, как вы? (Меняется /pause и /resume.)",
        "yes": "Да",
        "no": "Нет",
        "choose_topic": "Выберите тему:",
        "triage_pain_q1": "Где болит?",
        "triage_pain_q1_opts": ["Голова", "Горло", "Спина", "Живот", "Другое"],
        "triage_pain_q2": "Какой характер боли?",
        "triage_pain_q2_opts": ["Тупая", "Острая", "Пульсирующая", "Давящая"],
        "triage_pain_q3": "Как долго длится?",
        "triage_pain_q3_opts": ["<3ч", "3–24ч", ">1 дня", ">1 недели"],
        "triage_pain_q4": "Оцените боль (0–10):",
        "triage_pain_q5": "Есть что-то из этого сейчас?",
        "triage_pain_q5_opts": ["Высокая температура", "Рвота", "Слабость/онемение", "Нарушение речи/зрения", "Травма", "Нет"],
        "plan_header": "Ваш план на 24–48 часов:",
        "plan_accept": "Готовы попробовать сегодня?",
        "accept_opts": ["✅ Да", "🔁 Позже", "✖️ Нет"],
        "remind_when": "Когда напомнить и спросить самочувствие?",
        "remind_opts": ["через 4 часа", "вечером", "завтра утром", "не надо"],
        "thanks": "Принято 🙌",
        "checkin_ping": "Коротко: как сейчас по шкале 0–10?",
        "checkin_better": "Отлично! Продолжаем 💪",
        "checkin_worse": "Сочувствую. Если есть «красные флаги» или боль ≥7/10 — лучше обратиться к врачу.",
        "comment_prompt": "Спасибо за оценку 🙏\nХотите добавить комментарий? Просто напишите его, или отправьте /skip, чтобы пропустить.",
        "comment_saved": "Комментарий сохранён, спасибо! 🙌",
        "skip_ok": "Пропущено.",
        "unknown": "Нужно чуть больше деталей. Где болит и сколько длится?",
        "lang_switched": "Ок, дальше отвечаю по-русски.",
        "profile_intro": "Настроим профиль (~40с). Можно нажимать варианты или написать свой ответ.",
        "p_step_1": "Шаг 1/8. Укажи пол:",
        "p_step_2": "Шаг 2/8. Возраст:",
        "p_step_3": "Шаг 3/8. Главная цель:",
        "p_step_4": "Шаг 4/8. Хронические болезни (если есть):",
        "p_step_5": "Шаг 5/8. Лекарства/добавки/аллергии:",
        "p_step_6": "Шаг 6/8. Сон (отбой/подъём, напр. 23:30/07:00):",
        "p_step_7": "Шаг 7/8. Активность:",
        "p_step_8": "Шаг 8/8. Питание чаще всего:",
        "write": "✍️ Написать",
        "skip": "⏭️ Пропустить",
        "saved_profile": "Сохранил: ",
        "start_where": "С чего начнём? (симптом/сон/питание/анализы/привычки/долголетие)",
        "daily_gm": "Доброе утро! Быстрый ежедневный чек-ин:",
        "mood_good": "😃 Хорошо",
        "mood_ok": "😐 Нормально",
        "mood_bad": "😣 Плохо",
        "mood_note": "✍️ Комментарий",
        "mood_thanks": "Спасибо! Хорошего дня 👋",
        "btn_like": "👍",
        "btn_dislike": "👎",
    },
    "uk": {
        "welcome": "Привіт! Я TendAI — асистент здоров’я та довголіття.\nОбери тему нижче або опиши, що турбує.",
        "menu": ["Біль", "Горло/застуда", "Сон", "Стрес", "Травлення", "Енергія", "Харчування", "Аналізи", "Звички", "Довголіття", "Профіль"],
        "help": "Допомагаю короткими перевірками, планом на 24–48 год, нагадуваннями та щоденними чек-інами.\nКоманди:\n/help, /privacy, /pause, /resume, /delete_data, /profile, /checkin_on [ГГ:ХХ], /checkin_off, /settz +2",
        "privacy": "TendAI не замінює лікаря. Зберігаємо мінімум даних (Sheets) для нагадувань.\nКоманда /delete_data видалить усе.",
        "paused_on": "Нагадування призупинені. Увімкнути: /resume",
        "paused_off": "Нагадування знову увімкнені.",
        "deleted": "Усі ваші дані в TendAI видалено. Можна почати знову через /start.",
        "ask_consent": "Можу написати пізніше, щоб дізнатися, як ви? (Можна змінити /pause чи /resume.)",
        "yes": "Так",
        "no": "Ні",
        "choose_topic": "Оберіть тему:",
        "triage_pain_q1": "Де болить?",
        "triage_pain_q1_opts": ["Голова", "Горло", "Спина", "Живіт", "Інше"],
        "triage_pain_q2": "Який характер болю?",
        "triage_pain_q2_opts": ["Тупий", "Гострий", "Пульсуючий", "Тиснучий"],
        "triage_pain_q3": "Як довго триває?",
        "triage_pain_q3_opts": ["<3год", "3–24год", ">1 дня", ">1 тижня"],
        "triage_pain_q4": "Оцініть біль (0–10):",
        "triage_pain_q5": "Є щось із цього зараз?",
        "triage_pain_q5_opts": ["Висока температура", "Блювання", "Слабкість/оніміння", "Проблеми з мовою/зором", "Травма", "Немає"],
        "plan_header": "Ваш план на 24–48 год:",
        "plan_accept": "Готові спробувати сьогодні?",
        "accept_opts": ["✅ Так", "🔁 Пізніше", "✖️ Ні"],
        "remind_when": "Коли нагадати та запитати самопочуття?",
        "remind_opts": ["через 4 год", "увечері", "завтра вранці", "не треба"],
        "thanks": "Прийнято 🙌",
        "checkin_ping": "Коротко: як зараз за шкалою 0–10?",
        "checkin_better": "Чудово! Продовжуємо 💪",
        "checkin_worse": "Шкода. Якщо є «червоні прапорці» або біль ≥7/10 — краще звернутися до лікаря.",
        "comment_prompt": "Дякую за оцінку 🙏\nДодати коментар? Просто напишіть його, або надішліть /skip, щоб пропустити.",
        "comment_saved": "Коментар збережено, дякую! 🙌",
        "skip_ok": "Пропущено.",
        "unknown": "Потрібно трохи більше деталей. Де болить і скільки триває?",
        "lang_switched": "Ок, надалі відповідатиму українською.",
        "profile_intro": "Налаштуймо профіль (~40с). Можна натискати варіанти або написати свій.",
        "p_step_1": "Крок 1/8. Стать:",
        "p_step_2": "Крок 2/8. Вік:",
        "p_step_3": "Крок 3/8. Головна мета:",
        "p_step_4": "Крок 4/8. Хронічні хвороби (якщо є):",
        "p_step_5": "Крок 5/8. Ліки/добавки/алергії:",
        "p_step_6": "Крок 6/8. Сон (відбій/підйом, напр. 23:30/07:00):",
        "p_step_7": "Крок 7/8. Активність:",
        "p_step_8": "Крок 8/8. Харчування переважно:",
        "write": "✍️ Написати",
        "skip": "⏭️ Пропустити",
        "saved_profile": "Зберіг: ",
        "start_where": "З чого почнемо? (симптом/сон/харчування/аналізи/звички/довголіття)",
        "daily_gm": "Доброго ранку! Швидкий щоденний чек-ін:",
        "mood_good": "😃 Добре",
        "mood_ok": "😐 Нормально",
        "mood_bad": "😣 Погано",
        "mood_note": "✍️ Коментар",
        "mood_thanks": "Дякую! Гарного дня 👋",
        "btn_like": "👍",
        "btn_dislike": "👎",
    },
}

def t(lang: str, key: str) -> str:
    return T.get(lang, T["en"]).get(key, T["en"].get(key, key))

# ---------------------------
# Helpers for Sheets
# ---------------------------
def utcnow():
    return datetime.now(timezone.utc)

def iso(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")

def users_get_row_index(user_id: int) -> int | None:
    vals = ws_users.get_all_records()
    for i, row in enumerate(vals, start=2):
        if str(row.get("user_id")) == str(user_id):
            return i
    return None

def users_get(user_id: int) -> dict:
    vals = ws_users.get_all_records()
    for row in vals:
        if str(row.get("user_id")) == str(user_id):
            return row
    return {}

def users_upsert(user_id: int, username: str, lang: str):
    idx = users_get_row_index(user_id)
    base = [str(user_id), username or "", lang, "no", "0", DEFAULT_CHECKIN_LOCAL, "no"]
    if idx:
        ws_users.update(f"A{idx}:G{idx}", [base])
    else:
        ws_users.append_row(base)

def users_set(user_id: int, field: str, value: str):
    idx = users_get_row_index(user_id)
    if not idx:
        return
    headers = ws_users.row_values(1)
    if field in headers:
        col = headers.index(field) + 1
        ws_users.update_cell(idx, col, value)

def profiles_get_row_index(user_id: int) -> int | None:
    vals = ws_profiles.get_all_records()
    for i, row in enumerate(vals, start=2):
        if str(row.get("user_id")) == str(user_id):
            return i
    return None

def profiles_get(user_id: int) -> dict:
    vals = ws_profiles.get_all_records()
    for row in vals:
        if str(row.get("user_id")) == str(user_id):
            return row
    return {}

def profiles_upsert(user_id: int, data: dict):
    idx = profiles_get_row_index(user_id)
    headers = ws_profiles.row_values(1)
    row = profiles_get(user_id) if idx else {}
    row.update({k: ("" if v is None else (", ".join(v) if isinstance(v, list) else str(v))) for k, v in data.items()})
    row["user_id"] = str(user_id)
    row["updated_at"] = iso(utcnow())
    values = [row.get(h, "") for h in headers]
    if idx:
        ws_profiles.update(f"A{idx}:{chr(64+len(headers))}{idx}", [values])
    else:
        ws_profiles.append_row(values)

def episode_create(user_id: int, topic: str, baseline_severity: int, red_flags: str) -> str:
    eid = f"{user_id}-{uuid.uuid4().hex[:8]}"
    now = iso(utcnow())
    ws_episodes.append_row([
        eid, str(user_id), topic, now,
        str(baseline_severity), red_flags, "0", "<=3/10",
        "", "", "open", now, ""
    ])
    return eid

def episode_find_open(user_id: int) -> dict | None:
    vals = ws_episodes.get_all_records()
    for row in vals:
        if str(row.get("user_id")) == str(user_id) and row.get("status") == "open":
            return row
    return None

def episode_set(eid: str, field: str, value: str):
    vals = ws_episodes.get_all_values()
    headers = vals[0]
    if field not in headers:
        return
    col = headers.index(field) + 1
    for i in range(2, len(vals) + 1):
        if ws_episodes.cell(i, 1).value == eid:
            ws_episodes.update_cell(i, col, value)
            ws_episodes.update_cell(i, headers.index("last_update") + 1, iso(utcnow()))
            return

def reminder_add(user_id: int, text: str, when_utc: datetime):
    rid = f"{user_id}-{uuid.uuid4().hex[:6]}"
    ws_reminders.append_row([rid, str(user_id), text, iso(when_utc), iso(utcnow()), "scheduled"])
    return rid

def schedule_from_sheet_on_start(app):
    """Поднимаем незавершённые эпизоды, reminders и ежедневные чекапы."""
    now = utcnow()
    # Episodes check-ins
    for row in ws_episodes.get_all_records():
        if row.get("status") != "open":
            continue
        eid = row.get("episode_id")
        uid = int(row.get("user_id"))
        nca = row.get("next_checkin_at") or ""
        if not nca:
            continue
        try:
            dt_ = datetime.strptime(nca, "%Y-%m-%d %H:%M:%S%z")
        except Exception:
            continue
        delay = max(60, (dt_ - now).total_seconds())
        app.job_queue.run_once(job_checkin_episode, when=delay, data={"user_id": uid, "episode_id": eid})
    # Reminders one-off
    for row in ws_reminders.get_all_records():
        if (row.get("status") or "") != "scheduled":
            continue
        uid = int(row.get("user_id"))
        when = row.get("when_utc")
        rid = row.get("id")
        try:
            dt_ = datetime.strptime(when, "%Y-%m-%d %H:%M:%S%z")
        except Exception:
            continue
        delay = max(60, (dt_ - now).total_seconds())
        app.job_queue.run_once(job_oneoff_reminder, when=delay, data={"user_id": uid, "reminder_id": rid})
    # Daily check-ins
    for u in ws_users.get_all_records():
        if (u.get("paused") or "").lower() == "yes":
            continue
        uid = int(u.get("user_id"))
        tz_off = int(str(u.get("tz_offset") or "0"))
        hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
        schedule_daily_checkin(app, uid, tz_off, hhmm, norm_lang(u.get("lang")))

# ---------------------------
# Daily check-in scheduling
# ---------------------------
def hhmm_to_tuple(hhmm: str) -> tuple[int, int]:
    m = re.search(r'([01]?\d|2[0-3]):([0-5]\d)', hhmm.strip())
    return (int(m.group(1)), int(m.group(2))) if m else (8, 30)

def local_to_utc_hour_min(tz_offset_hours: int, hhmm: str) -> tuple[int, int]:
    h, m = hhmm_to_tuple(hhmm)
    h_utc = (h - tz_offset_hours) % 24
    return h_utc, m

def schedule_daily_checkin(app, user_id: int, tz_offset: int, hhmm_local: str, lang: str):
    for j in app.job_queue.get_jobs_by_name(f"daily_{user_id}"):
        j.schedule_removal()
    h_utc, m_utc = local_to_utc_hour_min(tz_offset, hhmm_local)
    t = dtime(hour=h_utc, minute=m_utc, tzinfo=timezone.utc)
    app.job_queue.run_daily(job_daily_checkin, time=t, name=f"daily_{user_id}",
                            data={"user_id": user_id, "lang": lang})

# ---------------------------
# Scenarios (pain + generic)
# ---------------------------
TOPIC_KEYS = {
    "en": {"Pain": "pain", "Throat/Cold": "throat", "Sleep": "sleep", "Stress": "stress", "Digestion": "digestion", "Energy": "energy",
           "Nutrition": "nutrition", "Labs": "labs", "Habits": "habits", "Longevity": "longevity", "Profile": "profile"},
    "ru": {"Боль": "pain", "Горло/простуда": "throat", "Сон": "sleep", "Стресс": "stress", "Пищеварение": "digestion", "Энергия": "energy",
           "Питание": "nutrition", "Анализы": "labs", "Привычки": "habits", "Долголетие": "longevity", "Профиль": "profile"},
    "uk": {"Біль": "pain", "Горло/застуда": "throat", "Сон": "sleep", "Стрес": "stress", "Травлення": "digestion", "Енергія": "energy",
           "Харчування": "nutrition", "Аналізи": "labs", "Звички": "habits", "Довголіття": "longevity", "Профіль": "profile"},
}

def main_menu(lang: str) -> ReplyKeyboardMarkup:
    lst = T[lang]["menu"]
    rows = [lst[:5], lst[5:]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def numeric_keyboard_0_10(lang: str) -> ReplyKeyboardMarkup:
    row1 = [str(i) for i in range(0, 6)]
    row2 = [str(i) for i in range(6, 11)]
    return ReplyKeyboardMarkup([row1, row2], resize_keyboard=True, one_time_keyboard=True)

def accept_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([T[lang]["accept_opts"]], resize_keyboard=True, one_time_keyboard=True)

def remind_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([T[lang]["remind_opts"]], resize_keyboard=True, one_time_keyboard=True)

# ---------------------------
# Planning helpers
# ---------------------------
def pain_plan(lang: str, red_flags_selected: list[str]) -> list[str]:
    flg = [s for s in red_flags_selected if s and str(s).lower() not in ["none", "нет", "немає"]]
    if flg:
        return {
            "ru": ["⚠️ Есть тревожные признаки. Лучше как можно скорее показаться врачу/в скорую."],
            "uk": ["⚠️ Є тривожні ознаки. Краще якнайшвидше звернутися до лікаря/швидкої."],
            "en": ["⚠️ Red flags present. Please seek urgent medical evaluation."],
        }[lang]
    if lang == "ru":
        return [
            "1) Вода 400–600 мл и 15–20 мин тишины/отдыха.",
            "2) Если нет противопоказаний — ибупрофен 200–400 мг однократно с едой.",
            "3) Проветрить, уменьшить экран на 30–60 мин.",
            "Проверка: к вечеру боль ≤3/10; усиливается — пиши.",
        ]
    if lang == "uk":
        return [
            "1) Вода 400–600 мл і 15–20 хв тиші/відпочинку.",
            "2) Якщо нема протипоказань — ібупрофен 200–400 мг одноразово з їжею.",
            "3) Провітрити, менше екрану 30–60 хв.",
            "Перевірка: до вечора біль ≤3/10; якщо посилюється — напиши.",
        ]
    return [
        "1) Drink 400–600 ml water; rest 15–20 min in a quiet room.",
        "2) If no contraindications — ibuprofen 200–400 mg once with food.",
        "3) Air the room; reduce screen time 30–60 min.",
        "Check: by evening pain ≤3/10; worsening — ping me.",
    ]

# ---------------------------
# Jobs (check-ins)
# ---------------------------
async def job_checkin_episode(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    user_id = data.get("user_id"); episode_id = data.get("episode_id")
    if not user_id or not episode_id:
        return
    u = users_get(user_id)
    if (u.get("paused") or "").lower() == "yes":
        return
    lang = norm_lang(u.get("lang") or "en")
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=t(lang, "checkin_ping"),
            reply_markup=numeric_keyboard_0_10(lang),
        )
        episode_set(episode_id, "next_checkin_at", "")
    except Exception as e:
        logging.error(f"job_checkin_episode send error: {e}")

async def job_oneoff_reminder(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    user_id = data.get("user_id"); rid = data.get("reminder_id")
    try:
        row = users_get(user_id)
        lang = norm_lang(row.get("lang") or "en")
        await context.bot.send_message(chat_id=user_id, text=t(lang, "thanks"))
    except Exception as e:
        logging.error(f"reminder send error: {e}")
    vals = ws_reminders.get_all_values()
    for i in range(2, len(vals)+1):
        if ws_reminders.cell(i, 1).value == rid:
            ws_reminders.update_cell(i, 6, "sent")
            break

async def job_daily_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id"); lang = d.get("lang", "en")
    u = users_get(uid)
    if (u.get("paused") or "").lower() == "yes":
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_good"], callback_data="mood|good"),
         InlineKeyboardButton(T[lang]["mood_ok"],   callback_data="mood|ok"),
         InlineKeyboardButton(T[lang]["mood_bad"],  callback_data="mood|bad")],
        [InlineKeyboardButton(T[lang]["mood_note"], callback_data="mood|note")]
    ])
    try:
        await context.bot.send_message(uid, T[lang]["daily_gm"], reply_markup=kb)
    except Exception as e:
        logging.error(f"daily checkin error: {e}")

# ---------------------------
# LLM Router
# ---------------------------
SYS_ROUTER = """
You are TendAI — a concise, warm, professional health & longevity assistant (not a doctor).
Always answer strictly in {lang}. Keep it short (<=6 lines + up to 4 bullets).
Use user profile if helpful. TRIAGE: ask 1–2 clarifiers first; advise ER only for clear red flags with high confidence.
Return MINIFIED JSON ONLY:
{"intent":"symptom"|"nutrition"|"sleep"|"labs"|"habits"|"longevity"|"other",
 "assistant_reply":string,
 "followups":string[],"needs_more":boolean,"red_flags":boolean,"confidence":0..1}
"""

def llm_router_answer(text: str, lang: str, profile: dict) -> dict:
    if not oai:
        return {"intent":"other","assistant_reply":t(lang,"unknown"),"followups":[],"needs_more":True,"red_flags":False,"confidence":0.3}
    sys = SYS_ROUTER.format(lang=lang) + f"\nUserProfile: {json.dumps(profile, ensure_ascii=False)}"
    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.2,
            max_tokens=420,
            messages=[{"role":"system","content":sys},{"role":"user","content":text}]
        )
        out = resp.choices[0].message.content.strip()
        m = re.search(r"\{.*\}\s*$", out, re.S)
        data = json.loads(m.group(0) if m else out)
        if data.get("red_flags") and float(data.get("confidence",0)) < 0.6:
            data["red_flags"] = False
            data["needs_more"] = True
            data.setdefault("followups", []).append(
                "Где именно/какой характер/сколько длится?" if lang=="ru" else
                ("Де саме/який характер/скільки триває?" if lang=="uk" else "Where exactly/what character/how long?")
            )
        return data
    except Exception as e:
        logging.error(f"router LLM error: {e}")
        return {"intent":"other","assistant_reply":t(lang,"unknown"),"followups":[],"needs_more":True,"red_flags":False,"confidence":0.3}

# ---------------------------
# Profile (guided intake)
# ---------------------------
PROFILE_STEPS = [
    {"key":"sex", "opts":{
        "ru":[("Мужской","male"),("Женский","female"),("Другое","other")],
        "en":[("Male","male"),("Female","female"),("Other","other")],
        "uk":[("Чоловіча","male"),("Жіноча","female"),("Інша","other")],
    }},
    {"key":"age", "opts":{
        "ru":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "en":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "uk":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
    }},
    {"key":"goal", "opts":{
        "ru":[("Похудение","weight"),("Энергия","energy"),("Сон","sleep"),("Долголетие","longevity"),("Сила","strength")],
        "en":[("Weight","weight"),("Energy","energy"),("Sleep","sleep"),("Longevity","longevity"),("Strength","strength")],
        "uk":[("Вага","weight"),("Енергія","energy"),("Сон","sleep"),("Довголіття","longevity"),("Сила","strength")],
    }},
    {"key":"conditions", "opts":{
        "ru":[("Нет","none"),("Гипертония","hypertension"),("Диабет","diabetes"),("Щитовидка","thyroid"),("Другое","other")],
        "en":[("None","none"),("Hypertension","hypertension"),("Diabetes","diabetes"),("Thyroid","thyroid"),("Other","other")],
        "uk":[("Немає","none"),("Гіпертонія","hypertension"),("Діабет","diabetes"),("Щитоподібна","thyroid"),("Інше","other")],
    }},
    {"key":"meds", "opts":{
        "ru":[("Нет","none"),("Магний","magnesium"),("Витамин D","vitd"),("Аллергии есть","allergies"),("Другое","other")],
        "en":[("None","none"),("Magnesium","magnesium"),("Vitamin D","vitd"),("Allergies","allergies"),("Other","other")],
        "uk":[("Немає","none"),("Магній","magnesium"),("Вітамін D","vitd"),("Алергії","allergies"),("Інше","other")],
    }},
    {"key":"sleep", "opts":{
        "ru":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
        "en":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Irregular","irregular")],
        "uk":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
    }},
    {"key":"activity", "opts":{
        "ru":[("<5k шагов","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Спорт регулярно","sport")],
        "en":[("<5k steps","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Regular sport","sport")],
        "uk":[("<5k кроків","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Спорт регулярно","sport")],
    }},
    {"key":"diet", "opts":{
        "ru":[("Сбалансировано","balanced"),("Низкоугл/кето","lowcarb"),("Вегетар/веган","plant"),("Нерегулярно","irregular")],
        "en":[("Balanced","balanced"),("Low-carb/keto","lowcarb"),("Vegetarian/vegan","plant"),("Irregular","irregular")],
        "uk":[("Збалансовано","balanced"),("Маловугл/кето","lowcarb"),("Вегетар/веган","plant"),("Нерегулярно","irregular")],
    }},
]

def build_profile_kb(lang: str, key: str, opts: List[Tuple[str,str]]) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for label, val in opts:
        row.append(InlineKeyboardButton(label, callback_data=f"p|choose|{key}|{val}"))
        if len(row) == 3:
            rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([InlineKeyboardButton(T[lang]["write"], callback_data=f"p|write|{key}"),
                 InlineKeyboardButton(T[lang]["skip"],  callback_data=f"p|skip|{key}")])
    return InlineKeyboardMarkup(rows)

async def start_profile(update: Update, lang: str, uid: int):
    sessions[uid] = {"profile_active": True, "p_step": 0, "p_wait_key": None}
    await update.message.reply_text(T[lang]["profile_intro"])
    step = PROFILE_STEPS[0]
    kb = build_profile_kb(lang, step["key"], step["opts"][lang])
    await update.message.reply_text(T[lang]["p_step_1"], reply_markup=kb)

# >>> FIXED: принимает Message, использует reply_text
async def advance_profile(msg, lang: str, uid: int):
    s = sessions.get(uid, {})
    s["p_step"] += 1
    if s["p_step"] < len(PROFILE_STEPS):
        idx = s["p_step"]
        msg_key = f"p_step_{idx+1}"
        step = PROFILE_STEPS[idx]
        kb = build_profile_kb(lang, step["key"], step["opts"][lang])
        await msg.reply_text(T[lang][msg_key], reply_markup=kb)
        return
    prof = profiles_get(uid)
    summary_parts = []
    for k in ["sex","age","goal","conditions","meds","sleep","activity","diet"]:
        v = prof.get(k) or sessions.get(uid,{}).get(k,"")
        if v:
            summary_parts.append(f"{k}: {v}")
    profiles_upsert(uid, {})
    sessions[uid]["profile_active"] = False
    await msg.reply_text(T[lang]["saved_profile"] + "; ".join(summary_parts))
    await msg.reply_text(T[lang]["start_where"], reply_markup=main_menu(lang))

# ---------------------------
# Commands
# ---------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = norm_lang(getattr(user, "language_code", None))
    users_upsert(user.id, user.username or "", lang)

    await update.message.reply_text(t(lang, "welcome"), reply_markup=main_menu(lang))

    u = users_get(user.id)
    if (u.get("consent") or "").lower() not in {"yes", "no"}:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(t(lang, "yes"), callback_data="consent|yes"),
              InlineKeyboardButton(t(lang, "no"),  callback_data="consent|no")]]
        )
        await update.message.reply_text(t(lang, "ask_consent"), reply_markup=kb)

    tz_off = int(str(u.get("tz_offset") or "0"))
    hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
    schedule_daily_checkin(context.application, user.id, tz_off, hhmm, lang)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await update.message.reply_text(t(lang, "help"))

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await update.message.reply_text(t(lang, "privacy"))

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "yes")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text(t(lang, "paused_on"))

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "no")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text(t(lang, "paused_off"))

async def cmd_delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    idx = users_get_row_index(uid)
    if idx:
        ws_users.delete_rows(idx)
    pidx = profiles_get_row_index(uid)
    if pidx:
        ws_profiles.delete_rows(pidx)
    vals = ws_episodes.get_all_values()
    to_delete = []
    for i in range(2, len(vals) + 1):
        if ws_episodes.cell(i, 2).value == str(uid):
            to_delete.append(i)
    for j, row_i in enumerate(to_delete):
        ws_episodes.delete_rows(row_i - j)
    rvals = ws_reminders.get_all_values()
    to_delete = []
    for i in range(2, len(rvals) + 1):
        if ws_reminders.cell(i, 2).value == str(uid):
            to_delete.append(i)
    for j, row_i in enumerate(to_delete):
        ws_reminders.delete_rows(row_i - j)

    lang = norm_lang(getattr(update.effective_user, "language_code", None))
    await update.message.reply_text(t(lang, "deleted"), reply_markup=ReplyKeyboardRemove())

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None))
    await start_profile(update, lang, uid)

async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    parts = (update.message.text or "").split()
    if len(parts) < 2 or not re.fullmatch(r"[+-]?\d{1,2}", parts[1]):
        await update.message.reply_text({"ru":"Формат: /settz +3","uk":"Формат: /settz +2","en":"Usage: /settz +3"}[lang]); return
    off = int(parts[1])
    users_set(uid, "tz_offset", str(off))
    hhmm = users_get(uid).get("checkin_hour") or DEFAULT_CHECKIN_LOCAL
    schedule_daily_checkin(context.application, uid, off, hhmm, lang)
    await update.message.reply_text({"ru":f"Часовой сдвиг: {off}ч","uk":f"Зсув: {off} год","en":f"Timezone offset: {off}h"}[lang])

async def cmd_checkin_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    parts = (update.message.text or "").split(maxsplit=1)
    hhmm = DEFAULT_CHECKIN_LOCAL
    if len(parts) == 2:
        m = re.search(r'([01]?\d|2[0-3]):([0-5]\d)', parts[1])
        if m: hhmm = m.group(0)
    users_set(uid, "checkin_hour", hhmm)
    tz_off = int(str(users_get(uid).get("tz_offset") or "0"))
    schedule_daily_checkin(context.application, uid, tz_off, hhmm, lang)
    await update.message.reply_text({"ru":f"Ежедневный чек-ин включён ({hhmm}).","uk":f"Щоденний чек-ін увімкнено ({hhmm}).","en":f"Daily check-in enabled ({hhmm})."}[lang])

async def cmd_checkin_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    for j in context.application.job_queue.get_jobs_by_name(f"daily_{uid}"):
        j.schedule_removal()
    await update.message.reply_text({"ru":"Ежедневный чек-ин выключен.","uk":"Щоденний чек-ін вимкнено.","en":"Daily check-in disabled."}[norm_lang(users_get(uid).get("lang") or "en")])

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if sessions.get(uid, {}).get("awaiting_comment"):
        sessions[uid]["awaiting_comment"] = False
        lang = norm_lang(users_get(uid).get("lang") or "en")
        await update.message.reply_text(t(lang, "skip_ok"))

# ---------------------------
# Callback (consent, feedback, profile, daily mood)
# ---------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = (q.data or "")
    uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")

    if data.startswith("consent|"):
        choice = data.split("|", 1)[1]
        users_set(uid, "consent", "yes" if choice == "yes" else "no")
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(t(lang, "thanks"))
        return

    if data in {"feedback_yes", "feedback_no"}:
        rating = "1" if data.endswith("yes") else "0"
        ws_feedback.append_row([iso(utcnow()), str(uid), data, q.from_user.username or "", rating, ""])
        sessions.setdefault(uid, {})["awaiting_comment"] = True
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(t(lang, "comment_prompt"))
        return

    if data.startswith("p|"):
        _, action, key, *rest = data.split("|")
        if action == "choose":
            value = "|".join(rest)
            sessions.setdefault(uid, {})[key] = value
            profiles_upsert(uid, {key: value})
            await advance_profile(q.message, lang, uid)
            return
        if action == "write":
            sessions.setdefault(uid, {})["p_wait_key"] = key
            await q.message.reply_text({"ru":"Напиши коротко свой вариант:","uk":"Напиши коротко свій варіант:","en":"Type your answer:"}[lang])
            return
        if action == "skip":
            profiles_upsert(uid, {key: ""})
            await advance_profile(q.message, lang, uid)
            return

    if data.startswith("mood|"):
        mood = data.split("|",1)[1]
        if mood == "note":
            sessions.setdefault(uid, {})["awaiting_daily_comment"] = True
            await q.message.reply_text({"ru":"Коротко опиши самочувствие:","uk":"Коротко опиши самопочуття:","en":"Write a short note:"}[lang])
            return
        ws_daily.append_row([iso(utcnow()), str(uid), mood, ""])
        await q.message.reply_text(T[lang]["mood_thanks"])
        return

# ---------------------------
# Scenario Flow (pain)
# ---------------------------
def detect_or_choose_topic(lang: str, text: str) -> str | None:
    text_l = text.lower()
    if any(w in text_l for w in ["опрос", "анкета", "опит", "questionnaire", "survey"]):
        return "profile"
    if any(w in text_l for w in ["болит", "боль", "hurt", "pain", "болю"]):
        return "pain"
    if any(w in text_l for w in ["горло", "throat", "простуд", "cold"]):
        return "throat"
    if any(w in text_l for w in ["сон", "sleep"]):
        return "sleep"
    if any(w in text_l for w in ["стресс", "stress"]):
        return "stress"
    if any(w in text_l for w in ["живот", "желуд", "живіт", "стул", "понос", "диар", "digest"]):
        return "digestion"
    if any(w in text_l for w in ["энерг", "енерг", "energy", "fatigue", "слабость"]):
        return "energy"
    if any(w in text_l for w in ["питание","харчування","nutrition"]):
        return "nutrition"
    if any(w in text_l for w in ["анализ","аналіз","labs"]):
        return "labs"
    if any(w in text_l for w in ["привыч","звич","habit"]):
        return "habits"
    if any(w in text_l for w in ["долголет","довголіт","longevity"]):
        return "longevity"
    for label, key in TOPIC_KEYS.get(lang, TOPIC_KEYS["en"]).items():
        if text.strip() == label:
            return key
    return None

async def start_pain_triage(update: Update, lang: str, uid: int):
    sessions[uid] = {"topic": "pain", "step": 1, "answers": {}}
    await update.message.reply_text(
        t(lang, "triage_pain_q1"),
        reply_markup=ReplyKeyboardMarkup([T[lang]["triage_pain_q1_opts"]], resize_keyboard=True, one_time_keyboard=True),
    )

async def continue_pain_triage(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str, uid: int, text: str):
    s = sessions.get(uid, {})
    step = s.get("step", 1)

    if step == 1:
        s["answers"]["loc"] = text
        s["step"] = 2
        await update.message.reply_text(
            t(lang, "triage_pain_q2"),
            reply_markup=ReplyKeyboardMarkup([T[lang]["triage_pain_q2_opts"]], resize_keyboard=True, one_time_keyboard=True),
        ); return

    if step == 2:
        s["answers"]["kind"] = text
        s["step"] = 3
        await update.message.reply_text(
            t(lang, "triage_pain_q3"),
            reply_markup=ReplyKeyboardMarkup([T[lang]["triage_pain_q3_opts"]], resize_keyboard=True, one_time_keyboard=True),
        ); return

    if step == 3:
        s["answers"]["duration"] = text
        s["step"] = 4
        await update.message.reply_text(t(lang, "triage_pain_q4"), reply_markup=numeric_keyboard_0_10(lang))
        return

    if step == 4:
        m = re.search(r'\d+', text)
        if not m:
            await update.message.reply_text(t(lang, "triage_pain_q4"), reply_markup=numeric_keyboard_0_10(lang))
            return
        sev = max(0, min(10, int(m.group(0))))
        s["answers"]["severity"] = sev
        s["step"] = 5
        await update.message.reply_text(
            t(lang, "triage_pain_q5"),
            reply_markup=ReplyKeyboardMarkup([T[lang]["triage_pain_q5_opts"]], resize_keyboard=True, one_time_keyboard=True),
        ); return

    if step == 5:
        red = text
        s["answers"]["red"] = red
        sev = int(s["answers"].get("severity", 5))
        eid = episode_create(uid, "pain", sev, red)
        s["episode_id"] = eid
        plan_lines = pain_plan(lang, [red])
        await update.message.reply_text(f"{t(lang,'plan_header')}\n" + "\n".join(plan_lines))
        await update.message.reply_text(t(lang, "plan_accept"), reply_markup=accept_keyboard(lang))
        s["step"] = 6; return

    if step == 6:
        acc = text.strip()
        accepted = "1" if acc.startswith("✅") else "0"
        episode_set(s["episode_id"], "plan_accepted", accepted)
        await update.message.reply_text(t(lang, "remind_when"), reply_markup=remind_keyboard(lang))
        s["step"] = 7; return

    if step == 7:
        choice = text.strip().lower()
        delay = None
        if choice in {"in 4h", "через 4 часа", "через 4 год"}: delay = timedelta(hours=4)
        elif choice in {"this evening", "вечером", "увечері"}: delay = timedelta(hours=6)
        elif choice in {"tomorrow morning", "завтра утром", "завтра вранці"}: delay = timedelta(hours=16)
        elif choice in {"no need", "не надо", "не треба"}: delay = None

        if delay:
            next_time = utcnow() + delay
            episode_set(s["episode_id"], "next_checkin_at", iso(next_time))
            context.job_queue.run_once(job_checkin_episode, when=delay.total_seconds(),
                                       data={"user_id": uid, "episode_id": s["episode_id"]})
        await update.message.reply_text(t(lang, "thanks"), reply_markup=main_menu(lang))
        sessions.pop(uid, None)
        return

# ---------------------------
# Main text handler
# ---------------------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    text = (update.message.text or "").strip()

    urec = users_get(uid)
    if not urec:
        try:
            lang = norm_lang(detect(text))
        except Exception:
            lang = norm_lang(getattr(user, "language_code", None))
        users_upsert(uid, user.username or "", lang)
    else:
        lang = norm_lang(urec.get("lang") or getattr(user, "language_code", None))

    # комментарий к ежедневному чеку
    if sessions.get(uid, {}).get("awaiting_daily_comment"):
        ws_daily.append_row([iso(utcnow()), str(uid), "note", text])
        sessions[uid]["awaiting_daily_comment"] = False
        await update.message.reply_text(T[lang]["mood_thanks"])
        return

    # комментарий к лайк/дизлайк
    if sessions.get(uid, {}).get("awaiting_comment"):
        ws_feedback.append_row([iso(utcnow()), str(uid), "comment", user.username or "", "", text])
        sessions[uid]["awaiting_comment"] = False
        await update.message.reply_text(t(lang, "comment_saved"))
        return

    # свободный ввод шага профиля
    if sessions.get(uid, {}).get("p_wait_key"):
        key = sessions[uid]["p_wait_key"]
        sessions[uid]["p_wait_key"] = None
        val = text
        if key == "age":
            m = re.search(r'\d{2}', text)
            if m: val = m.group(0)
        profiles_upsert(uid, {key: val})
        sessions[uid][key] = val
        await advance_profile(update.message, lang, uid)
        return

    # активный сценарий боли
    if sessions.get(uid, {}).get("topic") == "pain":
        await continue_pain_triage(update, context, lang, uid, text); return

    topic = detect_or_choose_topic(lang, text)
    if topic == "profile":
        await start_profile(update, lang, uid); return
    if topic == "pain":
        await start_pain_triage(update, lang, uid); return
    if topic in {"throat", "sleep", "stress", "digestion", "energy", "nutrition", "labs", "habits", "longevity"}:
        prof = profiles_get(uid)
        data = llm_router_answer(text, lang, prof)
        reply = data.get("assistant_reply") or t(lang, "unknown")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["btn_like"], callback_data="feedback_yes"),
                                    InlineKeyboardButton(T[lang]["btn_dislike"], callback_data="feedback_no")]])
        await update.message.reply_text(reply, reply_markup=kb)
        for q in (data.get("followups") or [])[:2]:
            await update.message.reply_text(q)
        return

    # общий фолбэк
    prof = profiles_get(uid)
    data = llm_router_answer(text, lang, prof)
    reply = data.get("assistant_reply") or t(lang, "unknown")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["btn_like"], callback_data="feedback_yes"),
                                InlineKeyboardButton(T[lang]["btn_dislike"], callback_data="feedback_no")]])
    await update.message.reply_text(reply, reply_markup=kb)
    for q in (data.get("followups") or [])[:2]:
        await update.message.reply_text(q)

# ---------------------------
# Number replies (0–10)
# ---------------------------
async def on_number_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    text = update.message.text.strip()
    try:
        val = int(text)
        if not (0 <= val <= 10):
            return
    except Exception:
        return

    lang = norm_lang(users_get(uid).get("lang") or getattr(user, "language_code", None))

    # >>> ВАЖНО: если идёт боль-шаг4, передаём в триаж
    if sessions.get(uid, {}).get("topic") == "pain" and sessions[uid].get("step") == 4:
        await continue_pain_triage(update, context, lang, uid, str(val))
        return

    ep = episode_find_open(uid)
    if not ep:
        await update.message.reply_text(t(lang, "thanks")); return
    eid = ep.get("episode_id")
    episode_set(eid, "notes", f"checkin:{val}")

    if val <= 3:
        await update.message.reply_text(t(lang, "checkin_better"), reply_markup=main_menu(lang))
        episode_set(eid, "status", "resolved")
    else:
        await update.message.reply_text(t(lang, "checkin_worse"), reply_markup=main_menu(lang))

# ---------------------------
# App init
# ---------------------------
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    schedule_from_sheet_on_start(app)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("delete_data", cmd_delete_data))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("settz", cmd_settz))
    app.add_handler(CommandHandler("checkin_on", cmd_checkin_on))
    app.add_handler(CommandHandler("checkin_off", cmd_checkin_off))

    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_handler(MessageHandler(filters.Regex(r"^(?:[0-9]|10)$"), on_number_reply))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling()

if __name__ == "__main__":
    main()
