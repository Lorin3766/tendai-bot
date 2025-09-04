# -*- coding: utf-8 -*-
"""
TendAI Bot — Part 1/2: Base & UX
- i18n текстовые ресурсы (расширены)
- гибрид общения: постоянный quickbar UI
- мини-опросник (интро) с кнопками (работает сразу при первом сообщении)
- безопасные врапперы к Google Sheets + память
- помощники (время, тихие часы, префикс персонализации)
- метрики жизни (ядро), анти-дубликаты
- команды-оболочки и минимальный post_init

ВНИМАНИЕ: Поведение быстрых кнопок, Health60, напоминания, утренние/вечерние джобы,
детальная маршрутизация и прочая логика — в Части 2.
"""

import os, re, json, uuid, logging, random
from datetime import datetime, timedelta, timezone, date, time as dtime
from typing import Optional, Dict, List, Tuple, Set

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

# ---------- (Опционально) OpenAI клиент — логика будет в Части 2 ----------
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # чтобы не падало, если пакет не установлен

# ---------- Google Sheets (надёжно + фоллбэк в память) ----------
import gspread
import gspread.utils as gsu
from gspread.exceptions import SpreadsheetNotFound, WorksheetNotFound
from oauth2client.service_account import ServiceAccountCredentials


# ============================= BOOT =============================
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
DetectorFactory.seed = 0

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

SHEET_NAME = os.getenv("SHEET_NAME", "TendAI Sheets")
SHEET_ID = os.getenv("SHEET_ID", "")
ALLOW_CREATE_SHEET = os.getenv("ALLOW_CREATE_SHEET", "0") == "1"

DEFAULT_CHECKIN_LOCAL = os.getenv("DEFAULT_CHECKIN_LOCAL", "08:30")
DEFAULT_EVENING_LOCAL = os.getenv("DEFAULT_EVENING_LOCAL", "20:00")
DEFAULT_QUIET_HOURS = os.getenv("DEFAULT_QUIET_HOURS", "22:00-08:00")

# OpenAI (не обязателен для Части 1)
oai = None
if OPENAI_API_KEY and OpenAI is not None:
    try:
        os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
        oai = OpenAI()
    except Exception as e:
        logging.warning(f"OpenAI init warning: {e}")


# ============================= i18n =============================
SUPPORTED = {"ru", "uk", "en", "es"}

def norm_lang(code: Optional[str]) -> str:
    if not code: return "en"
    c = code.split("-")[0].lower()
    return c if c in SUPPORTED else "en"

T: Dict[str, Dict[str, str]] = {
    "en": {
        # приветствие/справка
        "welcome": "Hi! I’m TendAI — your friendly health & longevity buddy.\nTell me what’s up — or tap a quick action below. I’ll personalize with a 40s intake.",
        "help": "Short check-ins, 24–48h plans, reminders, daily care.\nCommands: /help /privacy /profile /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
        "privacy": "TendAI is not a medical service. We store minimal data for reminders. Use /profile to update, /delete_data to erase.",
        # гибрид-диалог / quickbar
        "quick_title": "Quick actions",
        "qb_h60": "⚡ Health in 60s",
        "qb_er": "🚑 Emergency info",
        "qb_lab": "🧪 Lab",
        "qb_rem": "⏰ Reminder",
        # утренний чек-ин варианты
        "gm_greet": "Good morning! 🌞 How do you feel today?",
        "gm_btn_excellent": "👍 Great",
        "gm_btn_ok": "🙂 Okay",
        "gm_btn_tired": "😐 Tired",
        "gm_btn_pain": "🤕 In pain",
        "gm_btn_skip": "⏭ Skip",
        # эмпатия/соц.доказательства/юмор/метрики
        "thanks": "Got it 🙌",
        "nudge_soft": "If you’re not sure, a glass of water and a short walk help many people.",
        "social_proof": "70% of users your age discovered a sleep trigger within 2 weeks.",
        "metrics_title": "Life stats",
        "metrics_today": "Today is your {n}-th day of life 🎉.",
        "metrics_pct": "You’re ~{p}% to 100 years.",
        "metrics_bar": "Progress: {bar} {p}%",
        # intake
        "intake_intro": "Mini-intake (~40s). Use buttons or write your answer.",
        "write": "✍️ Write",
        "skip": "⏭️ Skip",
        "saved_profile": "Saved: ",
        "start_where": "Where would you like to start? (symptom/sleep/nutrition/habits/longevity)",
    },
    "ru": {
        "welcome": "Привет! Я TendAI — заботливый помощник здоровья и долголетия.\nНапиши, что беспокоит, или нажми быструю кнопку ниже. Для точности сделаем лёгкий опрос (~40с).",
        "help": "Короткие чек-ины, план на 24–48 ч, напоминания, ежедневная забота.\nКоманды: /help /privacy /profile /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
        "privacy": "TendAI не заменяет врача. Храним минимум данных для напоминаний. /profile — обновить, /delete_data — удалить всё.",
        "quick_title": "Быстрые действия",
        "qb_h60": "⚡ Здоровье за 60 сек",
        "qb_er": "🚑 Срочно в скорую",
        "qb_lab": "🧪 Лаборатория",
        "qb_rem": "⏰ Напоминание",
        "gm_greet": "Доброе утро! 🌞 Как сегодня самочувствие?",
        "gm_btn_excellent": "👍 Отлично",
        "gm_btn_ok": "🙂 Нормально",
        "gm_btn_tired": "😐 Устал",
        "gm_btn_pain": "🤕 Болит",
        "gm_btn_skip": "⏭ Пропустить",
        "thanks": "Принято 🙌",
        "nudge_soft": "Если сомневаетесь — стакан воды и короткая прогулка помогают многим.",
        "social_proof": "70% пользователей вашего возраста нашли триггеры сна за 2 недели.",
        "metrics_title": "Метрики жизни",
        "metrics_today": "Сегодня ваш {n}-й день жизни 🎉.",
        "metrics_pct": "Пройдено ~{p}% к 100 годам.",
        "metrics_bar": "Прогресс: {bar} {p}%",
        "intake_intro": "Мини-опрос (~40с). Можно нажимать кнопки или написать свой ответ.",
        "write": "✍️ Написать",
        "skip": "⏭️ Пропустить",
        "saved_profile": "Сохранил: ",
        "start_where": "С чего начнём? (симптом/сон/питание/привычки/долголетие)",
    },
    "uk": {
        "welcome": "Привіт! Я TendAI — турботливий помічник здоров’я та довголіття.\nНапиши, що турбує, або натисни швидку кнопку нижче. Для точності зробимо легке опитування (~40с).",
        "help": "Короткі чек-іни, план на 24–48 год, нагадування, щоденна турбота.\nКоманди: /help /privacy /profile /health60 /mood /energy /hydrate /skintip /cycle /youth /ru /uk /en /es",
        "privacy": "TendAI не замінює лікаря. Зберігаємо мінімум даних. /profile — оновити, /delete_data — видалити.",
        "quick_title": "Швидкі дії",
        "qb_h60": "⚡ Здоров’я за 60 с",
        "qb_er": "🚑 Терміново в швидку",
        "qb_lab": "🧪 Лабораторія",
        "qb_rem": "⏰ Нагадування",
        "gm_greet": "Доброго ранку! 🌞 Як сьогодні самопочуття?",
        "gm_btn_excellent": "👍 Чудово",
        "gm_btn_ok": "🙂 Нормально",
        "gm_btn_tired": "😐 Втома",
        "gm_btn_pain": "🤕 Біль",
        "gm_btn_skip": "⏭ Пропустити",
        "thanks": "Прийнято 🙌",
        "nudge_soft": "Якщо вагаєшся — склянка води й коротка прогулянка допомагають багатьом.",
        "social_proof": "70% користувачів твого віку знаходять тригери сну за 2 тижні.",
        "metrics_title": "Метрики життя",
        "metrics_today": "Сьогодні твій {n}-й день життя 🎉.",
        "metrics_pct": "Пройдено ~{p}% до 100 років.",
        "metrics_bar": "Прогрес: {bar} {p}%",
        "intake_intro": "Міні-опитник (~40с). Можна натискати кнопки або написати свій варіант.",
        "write": "✍️ Написати",
        "skip": "⏭️ Пропустити",
        "saved_profile": "Зберіг: ",
        "start_where": "З чого почнемо? (симптом/сон/харчування/звички/довголіття)",
    },
}
T["es"] = T["en"]  # простая заглушка


# ============================= CALLBACK KEYS =============================
CB_MENU_H60 = "menu|h60"
CB_MENU_ER  = "menu|er"
CB_MENU_LAB = "menu|lab"
CB_MENU_REM = "menu|rem"

# Мини-опросник
CB_MINI_CHOOSE = "mini|choose|{key}|{val}"
CB_MINI_WRITE  = "mini|write|{key}"
CB_MINI_SKIP   = "mini|skip|{key}"

# Утренний чек-ин (кнопки появятся в Части 2)
CB_GM_MOOD_PREFIX = "gm|mood|"
CB_GM_SKIP = "gm|skip"


# ============================= HELPERS =============================
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: Optional[datetime]) -> str:
    return "" if not dt else dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")

def detect_lang_from_text(text: str, fallback: str) -> str:
    s = (text or "").strip()
    if not s: return fallback
    low = s.lower()
    if re.search(r"[а-яёіїєґ]", low):
        # восточнославянские буквы — быстрое эвристическое ветвление
        return "uk" if re.search(r"[іїєґ]", low) else "ru"
    try:
        return norm_lang(detect(s))
    except Exception:
        return fallback

# анти-дубликат
from difflib import SequenceMatcher
def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()

sessions: Dict[int, dict] = {}
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


# ============================= SHEETS + MEMORY =============================
SHEETS_ENABLED = True
ss = None
ws_users = ws_profiles = ws_daily = ws_feedback = ws_episodes = ws_challenges = ws_reminders = None

MEM_USERS: Dict[int, dict] = {}
MEM_PROFILES: Dict[int, dict] = {}
MEM_DAILY: List[dict] = []
MEM_FEEDBACK: List[dict] = []
MEM_EPISODES: List[dict] = []
MEM_CHALLENGES: List[dict] = []
MEM_REMINDERS: List[dict] = []

def _ws_headers(ws) -> List[str]:
    try:
        return ws.row_values(1) or []
    except Exception:
        return []

def _ws_ensure_columns(ws, desired_headers: List[str]):
    """
    Надёжно создаём шапку и расширяем сетку под новые колонки.
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
            # аккуратно добавляем справа
            for h in missing:
                ws.update_cell(1, len(current) + 1, h)
                current.append(h)
    except Exception as e:
        logging.warning(f"ensure columns failed for {getattr(ws, 'title', '?')}: {e}")

def _sheets_init():
    global SHEETS_ENABLED, ss
    global ws_users, ws_profiles, ws_daily, ws_feedback, ws_episodes, ws_challenges, ws_reminders
    try:
        creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if not creds_json:
            raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = json.loads(creds_json)
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds, scope)
        gclient = gspread.authorize(credentials)

        try:
            ss_ = gclient.open_by_key(SHEET_ID) if SHEET_ID else gclient.open(SHEET_NAME)
        except SpreadsheetNotFound:
            if ALLOW_CREATE_SHEET:
                ss_ = gclient.create(SHEET_NAME)
            else:
                raise
        ss = ss_

        def _ensure_ws(title: str, headers: List[str]):
            try:
                ws = ss.worksheet(title)
            except WorksheetNotFound:
                ws = ss.add_worksheet(title=title, rows=4000, cols=max(60, len(headers)))
                ws.append_row(headers)
            if not _ws_headers(ws):
                ws.append_row(headers)
            _ws_ensure_columns(ws, headers)
            return ws

        # Users & Profiles — добавлены новые столбцы согласно ТЗ
        ws_users = _ensure_ws("Users", [
            "user_id","username","lang","tz_offset","paused","last_seen",
            "checkin_hour","evening_hour","last_auto_date","last_auto_count"
        ])
        ws_profiles = _ensure_ws("Profiles", [
            "user_id","sex","age","goal","goals",
            "conditions","surgeries","meds","allergies",
            "sleep","activity","diet","diet_focus","steps_target","habits",
            "ai_profile","quiet_hours","streak","streak_best","gm_last_date","birth_date",
            "city","notes","updated_at"
        ])
        ws_daily = _ensure_ws("DailyCheckins", ["timestamp","user_id","mood","energy","comment"])
        ws_feedback = _ensure_ws("Feedback", ["timestamp","user_id","name","username","rating","comment"])
        ws_episodes = _ensure_ws("Episodes", ["episode_id","user_id","topic","started_at","status","severity","notes"])
        ws_challenges = _ensure_ws("Challenges", ["user_id","challenge_id","name","start_date","length_days","days_done","status"])
        ws_reminders = _ensure_ws("Reminders", ["id","user_id","text","when_utc","created_at","status"])

        logging.info("Google Sheets connected.")
    except Exception as e:
        SHEETS_ENABLED = False
        logging.error(f"SHEETS disabled (fallback to memory). Reason: {e}")

_sheets_init()

def _headers(ws):  # короткий хелпер
    return _ws_headers(ws)

# ---- Users ----
def users_get(uid: int) -> dict:
    if SHEETS_ENABLED and ws_users:
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
        "tz_offset": "0",
        "paused": "no",
        "last_seen": iso(utcnow()),
        "checkin_hour": DEFAULT_CHECKIN_LOCAL,
        "evening_hour": "",
        "last_auto_date": "",
        "last_auto_count": "0",
    }
    if SHEETS_ENABLED and ws_users:
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
    if SHEETS_ENABLED and ws_users:
        hdr = _headers(ws_users)
        if field not in hdr:
            _ws_ensure_columns(ws_users, hdr + [field])
            hdr = _headers(ws_users)
        vals = ws_users.get_all_records()
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                ws_users.update_cell(i, hdr.index(field) + 1, value)
                return
        # если записи не было — создадим через upsert
        users_upsert(uid, "", "en")
        users_set(uid, field, value)
    else:
        u = MEM_USERS.setdefault(uid, {})
        u[field] = value

# ---- Profiles ----
def profiles_get(uid: int) -> dict:
    if SHEETS_ENABLED and ws_profiles:
        for r in ws_profiles.get_all_records():
            if str(r.get("user_id")) == str(uid):
                return r
        return {}
    return MEM_PROFILES.get(uid, {})

def profiles_upsert(uid: int, patch: Dict[str, str]):
    patch = {**patch, "updated_at": iso(utcnow())}
    if SHEETS_ENABLED and ws_profiles:
        vals = ws_profiles.get_all_records()
        hdr = _headers(ws_profiles)
        # расширим шапку, если в patch есть новые поля
        new_headers = list(hdr)
        for k in patch.keys():
            if k not in new_headers:
                new_headers.append(k)
        if new_headers != hdr:
            _ws_ensure_columns(ws_profiles, new_headers)
            hdr = _headers(ws_profiles)
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                merged = {**r, **patch}
                end_col = gsu.rowcol_to_a1(1, len(hdr)).rstrip("1")
                ws_profiles.update(f"A{i}:{end_col}{i}", [[merged.get(h, "") for h in hdr]])
                return
        # если нет — аппендим новую
        row = {"user_id": str(uid), **patch}
        ws_profiles.append_row([row.get(h, "") for h in hdr])
    else:
        base = MEM_PROFILES.get(uid, {})
        base.update(patch)
        MEM_PROFILES[uid] = base

# ---- Logs helpers ----
def daily_add(ts: str, uid: int, mood: str = "", comment: str = "", energy: Optional[int] = None):
    rec = {"timestamp": ts, "user_id": str(uid), "mood": mood, "energy": "" if energy is None else str(energy), "comment": comment}
    if SHEETS_ENABLED and ws_daily:
        hdr = _headers(ws_daily)
        ws_daily.append_row([rec.get(h, "") for h in hdr])
    else:
        MEM_DAILY.append(rec)

def feedback_add(ts: str, uid: int, name: str, username: str, rating: str, comment: str):
    rec = {"timestamp": ts, "user_id": str(uid), "name": name, "username": username, "rating": rating, "comment": comment}
    if SHEETS_ENABLED and ws_feedback:
        hdr = _headers(ws_feedback)
        ws_feedback.append_row([rec.get(h, "") for h in hdr])
    else:
        MEM_FEEDBACK.append(rec)


# ============================= PERSONALIZATION & TIME =============================
def profile_is_incomplete(p: dict) -> bool:
    needed = ["sex", "age", "goal"]
    return sum(1 for k in needed if str(p.get(k) or "").strip()) < 2

def personalized_prefix(lang: str, profile: dict) -> str:
    sex = (profile.get("sex") or "").strip()
    goal = (profile.get("goal") or "").strip()
    age_raw = str(profile.get("age") or "")
    m = re.search(r"\d+", age_raw)
    age = m.group(0) if m else ""
    if sum(bool(x) for x in (sex, age, goal)) >= 2:
        tpl_map = {
            "en": "Considering your profile: {sex}, {age}y; goal — {goal}.",
            "ru": "С учётом профиля: {sex}, {age} лет; цель — {goal}.",
            "uk": "З урахуванням профілю: {sex}, {age} р.; мета — {goal}.",
            "es": "Perfil: {sex}, {age}y; objetivo — {goal}.",
        }
        tpl = tpl_map.get(lang, tpl_map["en"])
        return tpl.format(sex=sex or "—", age=age or "—", goal=goal or "—")
    return ""

def _user_lang(uid: int, fallback: str = "en") -> str:
    return norm_lang(users_get(uid).get("lang") or fallback)

def _user_tz_off(uid: int) -> int:
    try:
        return int(str(users_get(uid).get("tz_offset") or "0"))
    except Exception:
        return 0

def user_local_now(uid: int) -> datetime:
    off = _user_tz_off(uid)
    return utcnow() + timedelta(hours=off)

def hhmm_tuple(hhmm: str) -> Tuple[int, int]:
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", hhmm or "")
    if not m: return (8, 30)
    return (int(m.group(1)), int(m.group(2)))

def local_to_utc_dt(uid: int, dt_local: datetime) -> datetime:
    return dt_local - timedelta(hours=_user_tz_off(uid))

def adjust_out_of_quiet(when_local: datetime, quiet: str) -> datetime:
    """
    Если попадает в «тихие часы» (HH:MM-HH:MM) — сдвигаем на ближайшее разрешённое время.
    """
    q = quiet or DEFAULT_QUIET_HOURS
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-3]):([0-5]\d)\s*$", q)
    if not m: return when_local
    sh, sm, eh, em = map(int, [m.group(1), m.group(2), m.group(3), m.group(4)])
    start = when_local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = when_local.replace(hour=eh, minute=em, second=0, microsecond=0)
    # «тихие часы» могут переходить через полночь
    in_quiet = (start <= when_local < end) if start < end else not (end <= when_local < start)
    if not in_quiet:
        return when_local
    # сдвинем на конец тихого окна
    target = end if start < end else end + timedelta(days=1)
    return target


# ============================= LIFE METRICS =============================
def life_metrics(profile: dict) -> Tuple[int, int]:
    """
    Возвращает (дней_прожито, проценты_к_100_годам).
    Если нет birth_date — оцениваем по age*365.
    """
    bd = (profile.get("birth_date") or "").strip()
    days = 0
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", bd):
            y, m, d = map(int, bd.split("-"))
            born = date(y, m, d)
            days = (date.today() - born).days
    except Exception:
        days = 0
    if days <= 0:
        try:
            age = int(re.findall(r"\d+", str(profile.get("age") or ""))[0])
        except Exception:
            age = 25
        days = max(1, age * 365)
    pct = min(100, int(round(days / 36500 * 100)))
    return days, pct

def progress_bar(percent: int, width: int = 12) -> str:
    p = max(0, min(100, int(percent)))
    blocks = int(round(p / 100 * width))
    return "█" * blocks + "░" * (width - blocks)


# ============================= MINI INTAKE =============================
MINI_KEYS = ["sex","age","goal","conditions","meds","activity","diet_focus","steps_target","habits","birth_date"]
MINI_FREE_KEYS: Set[str] = {"meds","birth_date"}

MINI_STEPS = {
    "sex": {
        "ru":[("Мужской","male"),("Женский","female"),("Другое","other")],
        "en":[("Male","male"),("Female","female"),("Other","other")],
        "uk":[("Чоловіча","male"),("Жіноча","female"),("Інша","other")],
        "label":{"ru":"Пол:","en":"Sex:","uk":"Стать:"}
    },
    "age": {
        "ru":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "en":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "uk":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "label":{"ru":"Возраст:","en":"Age:","uk":"Вік:"}
    },
    "goal": {
        "ru":[("Энергия","energy"),("Сон","sleep"),("Похудение","weight"),("Сила","strength"),("Долголетие","longevity")],
        "en":[("Energy","energy"),("Sleep","sleep"),("Weight","weight"),("Strength","strength"),("Longevity","longevity")],
        "uk":[("Енергія","energy"),("Сон","sleep"),("Вага","weight"),("Сила","strength"),("Довголіття","longevity")],
        "label":{"ru":"Главная цель:","en":"Main goal:","uk":"Головна мета:"}
    },
    "conditions": {
        "ru":[("Нет","none"),("Сердечно-сосуд.","cvd"),("ЩЖ/эндокр.","endocrine"),("ЖКТ","gi"),("Аллергия","allergy"),("Другое","other")],
        "en":[("None","none"),("Cardio/vascular","cvd"),("Thyroid/endocr.","endocrine"),("GI","gi"),("Allergy","allergy"),("Other","other")],
        "uk":[("Немає","none"),("Серцево-суд.","cvd"),("ЩЗ/ендокр.","endocrine"),("ШКТ","gi"),("Алергія","allergy"),("Інше","other")],
        "label":{"ru":"Хронические состояния:","en":"Chronic conditions:","uk":"Хронічні стани:"}
    },
    "meds": {
        "ru": [], "en": [], "uk": [],
        "label":{"ru":"Лекарства/добавки/аллергии (коротко):","en":"Meds/supplements/allergies (short):","uk":"Ліки/добавки/алергії (коротко):"}
    },
    "activity": {
        "ru":[("Мало","low"),("Умеренно","mid"),("Спорт","high")],
        "en":[("Low","low"),("Moderate","mid"),("Sport","high")],
        "uk":[("Мало","low"),("Помірно","mid"),("Спорт","high")],
        "label":{"ru":"Активность:","en":"Activity:","uk":"Активність:"}
    },
    "diet_focus": {
        "ru":[("Сбаланс.","balanced"),("Низкоугл.","lowcarb"),("Растит.","plant"),("Нерегул.","irregular")],
        "en":[("Balanced","balanced"),("Low-carb","lowcarb"),("Plant-based","plant"),("Irregular","irregular")],
        "uk":[("Збаланс.","balanced"),("Маловугл.","lowcarb"),("Рослинне","plant"),("Нерегул.","irregular")],
        "label":{"ru":"Питание чаще всего:","en":"Diet mostly:","uk":"Харчування:"}
    },
    "steps_target": {
        "ru":[("<5к","5000"),("5–8к","8000"),("8–12к","12000"),("Спорт","15000")],
        "en":[("<5k","5000"),("5–8k","8000"),("8–12k","12000"),("Sport","15000")],
        "uk":[("<5к","5000"),("5–8к","8000"),("8–12к","12000"),("Спорт","15000")],
        "label":{"ru":"Шаги/активность:","en":"Steps/activity:","uk":"Кроки/активність:"}
    },
    "habits": {
        "ru":[("Не курю","no_smoke"),("Курю","smoke"),("Алкоголь редко","alc_low"),("Алкоголь часто","alc_high"),("Кофеин 0–1","caf_low"),("Кофеин 2–3","caf_mid"),("Кофеин 4+","caf_high")],
        "en":[("No smoking","no_smoke"),("Smoking","smoke"),("Alcohol rare","alc_low"),("Alcohol often","alc_high"),("Caffeine 0–1","caf_low"),("Caffeine 2–3","caf_mid"),("Caffeine 4+","caf_high")],
        "uk":[("Не курю","no_smoke"),("Курю","smoke"),("Алкоголь рідко","alc_low"),("Алкоголь часто","alc_high"),("Кофеїн 0–1","caf_low"),("Кофеїн 2–3","caf_mid"),("Кофеїн 4+","caf_high")],
        "label":{"ru":"Привычки:","en":"Habits:","uk":"Звички:"}
    },
    "birth_date": {
        "ru": [], "en": [], "uk": [],
        "label":{"ru":"Дата рождения (ГГГГ-ММ-ДД) — по желанию:","en":"Birth date (YYYY-MM-DD) — optional:","uk":"Дата народження (РРРР-ММ-ДД) — опційно:"}
    }
}

def build_mini_kb(lang: str, key: str) -> InlineKeyboardMarkup:
    opts = MINI_STEPS[key].get(lang, [])
    rows, row = [], []
    for label, val in opts:
        row.append(InlineKeyboardButton(label, callback_data=f"mini|choose|{key}|{val}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row: rows.append(row)
    if key in MINI_FREE_KEYS or not opts:
        rows.append([InlineKeyboardButton(T[lang]["write"], callback_data=f"mini|write|{key}")])
    rows.append([InlineKeyboardButton(T[lang]["skip"], callback_data=f"mini|skip|{key}")])
    return InlineKeyboardMarkup(rows)

async def start_mini_intake(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: int):
    sessions[uid] = {"mini_active": True, "mini_step": 0, "mini_answers": {}}
    await context.bot.send_message(chat_id, T[lang]["intake_intro"], reply_markup=ReplyKeyboardRemove())
    await ask_next_mini(context, chat_id, lang, uid)

async def ask_next_mini(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: int):
    s = sessions.get(uid, {})
    step_idx = s.get("mini_step", 0)
    if step_idx >= len(MINI_KEYS):
        answers = s.get("mini_answers", {})
        # сохраняем в профиль
        prof_patch = {
            "sex": answers.get("sex",""),
            "age": answers.get("age",""),
            "goal": answers.get("goal",""),
            "conditions": answers.get("conditions",""),
            "meds": answers.get("meds",""),
            "activity": answers.get("activity",""),
            "diet_focus": answers.get("diet_focus",""),
            "steps_target": answers.get("steps_target",""),
            "habits": answers.get("habits",""),
            "birth_date": answers.get("birth_date",""),
        }
        profiles_upsert(uid, prof_patch)
        sessions[uid]["mini_active"] = False
        # покажем меню + quickbar
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


# ============================= QUICKBAR (UI) =============================
def quickbar_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["qb_h60"], callback_data=CB_MENU_H60)],
        [InlineKeyboardButton(T[lang]["qb_er"],  callback_data=CB_MENU_ER),
         InlineKeyboardButton(T[lang]["qb_lab"], callback_data=CB_MENU_LAB)],
        [InlineKeyboardButton(T[lang]["qb_rem"], callback_data=CB_MENU_REM)]
    ])

async def show_quickbar(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str):
    try:
        await context.bot.send_message(chat_id, T[lang]["quick_title"], reply_markup=quickbar_kb(lang))
    except Exception as e:
        logging.warning(f"show_quickbar failed: {e}")

def inline_topic_kb(lang: str) -> InlineKeyboardMarkup:
    # компактное меню разделов (пока без поведения; логика — в Части 2)
    rows = [
        [InlineKeyboardButton("⚡", callback_data=CB_MENU_H60),
         InlineKeyboardButton("🚑", callback_data=CB_MENU_ER),
         InlineKeyboardButton("🧪", callback_data=CB_MENU_LAB),
         InlineKeyboardButton("⏰", callback_data=CB_MENU_REM)]
    ]
    return InlineKeyboardMarkup(rows)


# ============================= STATE HELPERS =============================
def update_last_seen(uid: int):
    users_set(uid, "last_seen", iso(utcnow()))

# ============================= COMMANDS (оболочки) =============================
async def post_init(app):
    me = await app.bot.get_me()
    logging.info(f"BOT READY: @{me.username} (id={me.id})")
    # В Части 2 будет: schedule_from_sheet_on_start(app)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = norm_lang(getattr(user, "language_code", None))
    users_upsert(user.id, user.username or "", lang)
    context.user_data["lang"] = lang
    update_last_seen(user.id)

    await update.message.reply_text(T[lang]["welcome"], reply_markup=ReplyKeyboardRemove())

    prof = profiles_get(user.id)
    if profile_is_incomplete(prof):
        await start_mini_intake(context, update.effective_chat.id, lang, user.id)
    else:
        await update.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
        await show_quickbar(context, update.effective_chat.id, lang)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _user_lang(update.effective_user.id)
    await update.message.reply_text(T[lang]["help"])

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _user_lang(update.effective_user.id)
    await update.message.reply_text(T[lang]["privacy"])

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    # запускаем intake по кнопке/команде
    await start_mini_intake(context, update.effective_chat.id, lang, uid)

# Языки
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

# Оболочки разделов — только UI (поведение добавим в Части 2)
async def cmd_health60(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _user_lang(update.effective_user.id)
    await update.message.reply_text("✍️ Напишите коротко, что болит/беспокоит — и я дам 3 шага за 60 секунд.")
    await show_quickbar(context, update.effective_chat.id, lang)

async def cmd_mood(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _user_lang(update.effective_user.id)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(T[lang]["gm_btn_excellent"], callback_data=CB_GM_MOOD_PREFIX+"great"),
        InlineKeyboardButton(T[lang]["gm_btn_ok"],        callback_data=CB_GM_MOOD_PREFIX+"ok"),
        InlineKeyboardButton(T[lang]["gm_btn_tired"],     callback_data=CB_GM_MOOD_PREFIX+"tired"),
        InlineKeyboardButton(T[lang]["gm_btn_pain"],      callback_data=CB_GM_MOOD_PREFIX+"pain"),
        InlineKeyboardButton(T[lang]["gm_btn_skip"],      callback_data=CB_GM_SKIP),
    ]])
    await update.message.reply_text(T[lang]["gm_greet"], reply_markup=kb)
    await show_quickbar(context, update.effective_chat.id, lang)

async def cmd_energy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _user_lang(update.effective_user.id)
    await update.message.reply_text("⚡ Энергия: нажмите одну из кнопок в утреннем чек-ине (в Части 2).")
    await show_quickbar(context, update.effective_chat.id, lang)

async def cmd_hydrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _user_lang(update.effective_user.id)
    await update.message.reply_text("💧 Напоминание пить воду включу в Части 2. Пока просто: выпейте стакан воды 🙂")
    await show_quickbar(context, update.effective_chat.id, lang)

async def cmd_skintip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _user_lang(update.effective_user.id)
    await update.message.reply_text("🧴 Индивидуальные подсказки по коже подключу в Части 2.")
    await show_quickbar(context, update.effective_chat.id, lang)

async def cmd_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _user_lang(update.effective_user.id)
    await update.message.reply_text("🩸 Отслеживание цикла включу в Части 2 (нужно 2 шага вопросами).")
    await show_quickbar(context, update.effective_chat.id, lang)

async def cmd_youth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _user_lang(update.effective_user.id)
    await update.message.reply_text("🎒 Youth-пакет (челленджи, streak) подключу в Части 2.")
    await show_quickbar(context, update.effective_chat.id, lang)


# ============================= MESSAGE HANDLER =============================
async def msg_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = users_get(uid)
    lang = norm_lang(u.get("lang") or getattr(update.effective_user, "language_code", None) or "en")
    users_upsert(uid, update.effective_user.username or "", lang)  # создадим запись при первом сообщении
    update_last_seen(uid)
    text = (update.message.text or "").strip()

    # Если профиль пуст — сразу запускаем мини-опросник (ТЗ)
    prof = profiles_get(uid)
    s = sessions.setdefault(uid, {})
    if profile_is_incomplete(prof) and not s.get("mini_active"):
        await start_mini_intake(context, update.effective_chat.id, lang, uid)
        return

    # Захват свободного текста для шага мини-опросника (write)
    if s.get("mini_active") and s.get("mini_wait_key"):
        key = s["mini_wait_key"]
        s["mini_wait_key"] = None
        s["mini_answers"][key] = text
        s["mini_step"] = int(s.get("mini_step", 0)) + 1
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return

    # обычный текст пока просто подтверждаем и показываем quickbar
    await send_unique(update.message, uid, T[lang]["nudge_soft"])
    await show_quickbar(context, update.effective_chat.id, lang)


# ============================= CALLBACKS (Часть 1: только MINI) =============================
async def cb_mini_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    lang = _user_lang(uid)
    data = q.data or ""

    if not data.startswith("mini|"):
        # Остальные callback-и подключу в Части 2
        return

    # mini|choose|{key}|{val}
    if data.startswith("mini|choose|"):
        _, _, key, val = data.split("|", 3)
        mini_handle_choice(uid, key, val)
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return

    # mini|write|{key} — ждём следующий текст
    if data.startswith("mini|write|"):
        key = data.split("|", 2)[2]
        s = sessions.setdefault(uid, {"mini_active": True, "mini_step": 0, "mini_answers": {}})
        s["mini_wait_key"] = key
        try:
            await q.edit_message_text(T[lang]["write"] + "…")
        except Exception:
            pass
        return

    # mini|skip|{key}
    if data.startswith("mini|skip|"):
        s = sessions.setdefault(uid, {"mini_active": True, "mini_step": 0, "mini_answers": {}})
        s["mini_step"] = int(s.get("mini_step", 0)) + 1
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return


# ============================= MAIN =============================
def main():
    if not TELEGRAM_TOKEN:
        logging.error("TELEGRAM_TOKEN is not set"); return

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # команды-оболочки
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("health60", cmd_health60))
    app.add_handler(CommandHandler("mood", cmd_mood))
    app.add_handler(CommandHandler("energy", cmd_energy))
    app.add_handler(CommandHandler("hydrate", cmd_hydrate))
    app.add_handler(CommandHandler("skintip", cmd_skintip))
    app.add_handler(CommandHandler("cycle", cmd_cycle))
    app.add_handler(CommandHandler("youth", cmd_youth))

    app.add_handler(CommandHandler("ru", cmd_lang_ru))
    app.add_handler(CommandHandler("uk", cmd_lang_uk))
    app.add_handler(CommandHandler("en", cmd_lang_en))
    app.add_handler(CommandHandler("es", cmd_lang_es))

    # callbacks: в Части 1 только mini-интейк
    app.add_handler(CallbackQueryHandler(cb_mini_only), group=1)

    # текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_text), group=2)

    logging.info("Starting polling…")
    app.run_polling()

if __name__ == "__main__":
    main()
# ---------- Безопасные доопределения отсутствующих/заглушечных функций ----------

def _user_lang(uid: int) -> str:
    try:
        return norm_lang((users_get(uid) or {}).get("lang") or "en")
    except Exception:
        return "en"

def _user_tz_off(uid: int) -> int:
    try:
        return int(str((users_get(uid) or {}).get("tz_offset") or "0"))
    except Exception:
        return 0

def hhmm_tuple(hhmm: str) -> Tuple[int, int]:
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", str(hhmm or ""))
    if not m:
        return (8, 30)
    return (int(m.group(1)), int(m.group(2)))

def user_local_now(uid: int) -> datetime:
    return utcnow() + timedelta(hours=_user_tz_off(uid))

def utc_to_local_dt(uid: int, dt_utc: datetime) -> datetime:
    return (dt_utc or utcnow()) + timedelta(hours=_user_tz_off(uid))

def local_to_utc_dt(uid: int, dt_local: datetime) -> datetime:
    return (dt_local or user_local_now(uid)) - timedelta(hours=_user_tz_off(uid))

def adjust_out_of_quiet(when_local: datetime, quiet_hours: str) -> datetime:
    """
    Если время попадает в «тихие часы» (формат HH:MM-HH:MM), переносим на конец тихого интервала.
    """
    q = (quiet_hours or "").strip()
    m = re.match(r"^\s*([01]?\d:[0-5]\d)\s*-\s*([01]?\d:[0-5]\d)\s*$", q)
    if not m:
        return when_local
    start_h, start_m = hhmm_tuple(m.group(1))
    end_h, end_m   = hhmm_tuple(m.group(2))

    wd = when_local
    start_dt = wd.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end_dt   = wd.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

    if end_dt <= start_dt:
        # тихий интервал "перелетает" через полночь
        in_quiet = (wd >= start_dt) or (wd < end_dt)
        if in_quiet:
            # переносим на конец интервала в ближайшем будущем
            if wd >= start_dt:
                return end_dt + timedelta(days=1)
            else:
                return end_dt
        return wd
    else:
        # обычный интервал
        if start_dt <= wd < end_dt:
            return end_dt
        return wd

def update_last_seen(uid: int):
    users_set(uid, "last_seen", iso(utcnow()))


# ---------- Блок «Память» (профили/дневник/фидбек/эпизоды/челленджи/напоминания) ----------

def _ensure_ws_row_by_uid(ws, uid: int) -> int:
    """Возвращает номер строки (>=2) для user_id; создаёт строку при необходимости."""
    hdr = _headers(ws)
    vals = ws.get_all_records()
    for i, r in enumerate(vals, start=2):
        if str(r.get("user_id")) == str(uid):
            return i
    # добавить пустую строку с user_id
    row = [""] * len(hdr)
    try:
        row[hdr.index("user_id")] = str(uid)
    except ValueError:
        _ws_ensure_columns(ws, hdr + ["user_id"])
        hdr = _headers(ws)
        row = [""] * len(hdr)
        row[hdr.index("user_id")] = str(uid)
    ws.append_row(row)
    return ws.row_count  # последняя добавленная

def profiles_upsert(uid: int, patch: Dict[str, str]):
    if SHEETS_ENABLED and ws_profiles:
        hdr = _headers(ws_profiles)
        need = set(patch.keys()) - set(hdr)
        if need:
            _ws_ensure_columns(ws_profiles, hdr + list(need))
            hdr = _headers(ws_profiles)
        row_i = _ensure_ws_row_by_uid(ws_profiles, uid)
        row_vals = ws_profiles.row_values(row_i) + [""] * (len(hdr) - len(ws_profiles.row_values(row_i)))
        # собрать как dict -> list
        cur = {h: (row_vals[i] if i < len(row_vals) else "") for i, h in enumerate(hdr)}
        cur.update(patch)
        cur["user_id"] = str(uid)
        cur["updated_at"] = iso(utcnow())
        ws_profiles.update(f"A{row_i}:{gsu.rowcol_to_a1(row_i, len(hdr))}", [[cur.get(h, "") for h in hdr]])
    else:
        prof = MEM_PROFILES.setdefault(uid, {})
        prof.update(patch)
        prof["updated_at"] = iso(utcnow())

def feedback_add(ts: str, uid: int, name: str, username: str, rating: str, comment: str):
    if SHEETS_ENABLED and ws_feedback:
        ws_feedback.append_row([ts, str(uid), name or "", username or "", rating or "", comment or ""])
    else:
        MEM_FEEDBACK.append({"timestamp": ts, "user_id": str(uid), "name": name, "username": username,
                             "rating": rating, "comment": comment})

def daily_add(ts: str, uid: int, mood: str = "", comment: str = "", energy: Optional[int] = None):
    if SHEETS_ENABLED and ws_daily:
        ws_daily.append_row([ts, str(uid), mood or "", str(energy or ""), comment or ""])
    else:
        MEM_DAILY.append({"timestamp": ts, "user_id": str(uid), "mood": mood, "energy": energy, "comment": comment})

def episode_create(uid: int, topic: str, severity: int = 5, red: str = "") -> str:
    eid = str(uuid.uuid4())
    row = {
        "episode_id": eid, "user_id": str(uid), "topic": topic or "unspecified",
        "started_at": iso(utcnow()), "baseline_severity": str(severity),
        "red_flags": red or "", "plan_accepted": "", "target": "", "reminder_at": "",
        "next_checkin_at": "", "status": "open", "last_update": iso(utcnow()), "notes": ""
    }
    if SHEETS_ENABLED and ws_episodes:
        hdr = _headers(ws_episodes)
        _ws_ensure_columns(ws_episodes, hdr + list(row.keys()))
        ws_episodes.append_row([row.get(h, "") for h in _headers(ws_episodes)])
    else:
        MEM_EPISODES.append(row)
    return eid

def episode_find_open(uid: int) -> Optional[dict]:
    if SHEETS_ENABLED and ws_episodes:
        recs = ws_episodes.get_all_records()
        opens = [r for r in recs if str(r.get("user_id")) == str(uid) and (r.get("status") or "") == "open"]
        return opens[-1] if opens else None
    else:
        opens = [r for r in MEM_EPISODES if r["user_id"] == str(uid) and r.get("status") == "open"]
        return opens[-1] if opens else None

def episode_set(eid: str, field: str, value: str):
    if SHEETS_ENABLED and ws_episodes:
        vals = ws_episodes.get_all_records()
        hdr = _headers(ws_episodes)
        if field not in hdr:
            _ws_ensure_columns(ws_episodes, hdr + [field])
            hdr = _headers(ws_episodes)
        for i, r in enumerate(vals, start=2):
            if str(r.get("episode_id")) == str(eid):
                ws_episodes.update_cell(i, hdr.index(field) + 1, value)
                ws_episodes.update_cell(i, hdr.index("last_update") + 1, iso(utcnow()))
                return
    else:
        for r in MEM_EPISODES:
            if r["episode_id"] == eid:
                r[field] = value
                r["last_update"] = iso(utcnow())
                return

def reminder_add(uid: int, text: str, when_utc: datetime) -> str:
    rid = str(uuid.uuid4())
    row = {"id": rid, "user_id": str(uid), "text": text, "when_utc": iso(when_utc), "created_at": iso(utcnow()), "status": "scheduled"}
    if SHEETS_ENABLED and ws_reminders:
        hdr = _headers(ws_reminders)
        _ws_ensure_columns(ws_reminders, hdr + list(row.keys()))
        ws_reminders.append_row([row.get(h, "") for h in _headers(ws_reminders)])
    else:
        MEM_REMINDERS.append(row)
    return rid

def reminder_mark_sent(reminder_id: str):
    if SHEETS_ENABLED and ws_reminders:
        vals = ws_reminders.get_all_records()
        hdr = _headers(ws_reminders)
        for i, r in enumerate(vals, start=2):
            if str(r.get("id")) == str(reminder_id):
                ws_reminders.update_cell(i, hdr.index("status") + 1, "sent")
                return
    else:
        for r in MEM_REMINDERS:
            if r["id"] == reminder_id:
                r["status"] = "sent"
                return

def challenge_get(uid: int) -> Optional[dict]:
    if SHEETS_ENABLED and ws_challenges:
        for r in ws_challenges.get_all_records():
            if str(r.get("user_id")) == str(uid) and (r.get("status") or "") == "open":
                return r
        return None
    else:
        for r in MEM_CHALLENGES:
            if r.get("user_id") == str(uid) and r.get("status") == "open":
                return r
        return None

def challenge_start(uid: int, name: str = "hydrate7", length_days: int = 7):
    row = {"user_id": str(uid), "challenge_id": str(uuid.uuid4()), "name": name, "start_date": date.today().isoformat(),
           "length_days": str(length_days), "days_done": "0", "status": "open"}
    if SHEETS_ENABLED and ws_challenges:
        hdr = _headers(ws_challenges)
        _ws_ensure_columns(ws_challenges, hdr + list(row.keys()))
        ws_challenges.append_row([row.get(h, "") for h in _headers(ws_challenges)])
    else:
        MEM_CHALLENGES.append(row)

def challenge_inc(uid: int, name: str = "hydrate7"):
    if SHEETS_ENABLED and ws_challenges:
        vals = ws_challenges.get_all_records()
        hdr = _headers(ws_challenges)
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid) and (r.get("name") or "") == name and (r.get("status") or "") == "open":
                d = max(0, int(str(r.get("days_done") or "0"))) + 1
                ws_challenges.update_cell(i, hdr.index("days_done") + 1, str(d))
                if d >= int(str(r.get("length_days") or "7")):
                    ws_challenges.update_cell(i, hdr.index("status") + 1, "done")
                return d
        return 0
    else:
        for r in MEM_CHALLENGES:
            if r.get("user_id") == str(uid) and r.get("name") == name and r.get("status") == "open":
                r["days_done"] = str(int(r.get("days_done", "0")) + 1)
                if int(r["days_done"]) >= int(r.get("length_days", "7")):
                    r["status"] = "done"
                return int(r["days_done"])
        return 0


# ---------- Метрики жизни и подсказки ----------

def progress_bar(percent: float, width: int = 12) -> str:
    pct = max(0.0, min(1.0, percent))
    filled = int(round(pct * width))
    return "█" * filled + "░" * (width - filled)

def life_metrics(profile: dict) -> Tuple[int, float]:
    # по дате рождения точнее; иначе оценочно по age
    days = 0
    if profile:
        bd = (profile.get("birth_date") or "").strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", bd):
            try:
                y, m, d = [int(x) for x in bd.split("-")]
                born = datetime(y, m, d, tzinfo=timezone.utc)
                days = max(0, (utcnow() - born).days)
            except Exception:
                days = 0
        if not days:
            try:
                a = int(re.findall(r"\d+", str(profile.get("age") or ""))[0])
                days = max(0, a * 365)
            except Exception:
                days = 0
    pct = min(1.0, days / 36500.0) if days else 0.0
    return (days, pct)

def _get_skin_tip(lang: str, sex: str, age: int) -> str:
    tips = {
        "ru": [
            "SPF каждое утро, даже зимой.",
            "Мягкое умывание, без пересушивания.",
            "Старайтесь 7–8 ч сна — кожа благодарит.",
            "Вода и белок — базис для восстановления."
        ],
        "uk": [
            "SPF щоранку, навіть узимку.",
            "Ніжне вмивання без пересушення.",
            "7–8 годин сну — найкращий б'юті-хак.",
            "Вода та білок — основа відновлення."
        ],
        "en": [
            "Daily SPF, even in winter.",
            "Gentle cleanse, avoid over-drying.",
            "Aim for 7–8h sleep — skin loves it.",
            "Hydration + protein support repair."
        ],
        "es": [
            "SPF diario, incluso en invierno.",
            "Limpieza suave sin resecar.",
            "Duerme 7–8h: tu piel lo nota.",
            "Hidratación y proteína para reparar."
        ],
    }
    return random.choice(tips.get(lang, tips["en"]))

def _get_daily_tip(profile: dict, lang: str) -> str:
    pool = {
        "ru": [
            "2–3 коротких прогулки днём улучшают сон.",
            "Стакан воды перед экраном — маленькая победа.",
            "5 минут растяжки шеи снимут напряжение.",
            "Замените один сладкий перекус на орехи/йогурт."
        ],
        "uk": [
            "2–3 короткі прогулянки вдень покращують сон.",
            "Склянка води перед екраном — маленька перемога.",
            "5 хвилин розтяжки шиї знімуть напруження.",
            "Замініть солодкий перекус на горіхи/йогурт."
        ],
        "en": [
            "Two short walks today can boost tonight’s sleep.",
            "A glass of water now is a tiny win.",
            "Five minutes of neck mobility eases tension.",
            "Swap one sweet snack for nuts/yogurt."
        ],
        "es": [
            "Dos paseos breves hoy mejoran el sueño.",
            "Un vaso de agua ahora es una mini victoria.",
            "Cinco minutos de cuello alivian tensión.",
            "Cambia un dulce por frutos secos/yogur."
        ],
    }
    return random.choice(pool.get(_user_lang(int(profile.get("user_id") or 0)), pool["en"]))


# ---------- Quickbar и меню разделов ----------

def quickbar_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ " + (T[lang]["h60_btn"] if "h60_btn" in T[lang] else "Health in 60s"), callback_data="menu|h60"),
            InlineKeyboardButton("🚑 " + ("Срочно" if lang=="ru" else "ER"), callback_data="menu|er"),
        ],
        [
            InlineKeyboardButton("🧪 " + ("Лаборатория" if lang!="en" else "Lab"), callback_data="menu|lab"),
            InlineKeyboardButton("⏰ " + (T[lang]["quick_rem"] if "quick_rem" in T[lang] else "Reminder"), callback_data="menu|rem"),
        ]
    ])

def inline_topic_kb(lang: str) -> InlineKeyboardMarkup:
    return quickbar_kb(lang)

async def show_quickbar(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str):
    try:
        await context.bot.send_message(chat_id, T[lang]["quick_title"], reply_markup=quickbar_kb(lang))
    except Exception as e:
        logging.warning(f"show_quickbar: {e}")


# ---------- Планировщики и джобы ----------

def _has_jq_ctx(context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        return bool(getattr(context, "application", None) and context.application.job_queue)
    except Exception:
        return False

def _mk_first_run(hh: int, mm: int, tz_off: int) -> float:
    now_utc = utcnow()
    target_local = (now_utc + timedelta(hours=tz_off)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target_local <= (now_utc + timedelta(hours=tz_off)):
        target_local += timedelta(days=1)
    first_utc = target_local - timedelta(hours=tz_off)
    return max(1.0, (first_utc - now_utc).total_seconds())

def schedule_daily_checkin(app, uid: int, tz_off: int, hhmm: str, lang: str):
    for j in app.job_queue.get_jobs_by_name(f"daily_{uid}"):
        j.schedule_removal()
    hh, mm = hhmm_tuple(hhmm)
    app.job_queue.run_repeating(job_daily_checkin, interval=86400, first=_mk_first_run(hh, mm, tz_off),
                                name=f"daily_{uid}", data={"user_id": uid, "lang": lang})

def schedule_evening_checkin(app, uid: int, tz_off: int, hhmm: str, lang: str):
    for j in app.job_queue.get_jobs_by_name(f"evening_{uid}"):
        j.schedule_removal()
    hh, mm = hhmm_tuple(hhmm)
    app.job_queue.run_repeating(job_evening_checkin, interval=86400, first=_mk_first_run(hh, mm, tz_off),
                                name=f"evening_{uid}", data={"user_id": uid, "lang": lang})

def schedule_from_sheet_on_start(app):
    if not SHEETS_ENABLED or not ws_users:
        return
    try:
        for r in ws_users.get_all_records():
            uid = int(str(r.get("user_id") or "0")) if str(r.get("user_id") or "").isdigit() else None
            if not uid:
                continue
            lang = norm_lang(r.get("lang") or "en")
            tz  = int(str(r.get("tz_offset") or "0"))
            ch  = r.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL
            schedule_daily_checkin(app, uid, tz, ch, lang)
            eh  = (r.get("evening_hour") or "").strip()
            if eh:
                schedule_evening_checkin(app, uid, tz, eh, lang)
    except Exception as e:
        logging.warning(f"schedule_from_sheet_on_start: {e}")

async def job_oneoff_reminder(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id"); rid = d.get("reminder_id")
    if not uid or not rid:
        return
    try:
        # найдём текст
        text = "⏰"
        if SHEETS_ENABLED and ws_reminders:
            vals = ws_reminders.get_all_records()
            for r in vals:
                if str(r.get("id")) == str(rid):
                    text = (r.get("text") or "⏰")
                    break
        else:
            for r in MEM_REMINDERS:
                if r["id"] == rid:
                    text = r.get("text") or "⏰"
                    break
        await context.bot.send_message(uid, text)
        reminder_mark_sent(rid)
    except Exception as e:
        logging.error(f"job_oneoff_reminder: {e}")

# Переопределяем job_daily_checkin, чтобы добавить кнопки 👍/🙂/😐/🤕/⏭
async def job_daily_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid = d.get("user_id"); lang = norm_lang((d.get("lang") or "en"))
    if not uid:
        return
    u = users_get(uid)
    if (u.get("paused") or "").lower() == "yes":
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👍", callback_data="gm|mood|great"),
         InlineKeyboardButton("🙂", callback_data="gm|mood|ok"),
         InlineKeyboardButton("😐", callback_data="gm|mood|tired"),
         InlineKeyboardButton("🤕", callback_data="gm|mood|pain")],
        [InlineKeyboardButton("⏭", callback_data="gm|skip")],
    ])
    try:
        # Пробросим метрики жизни время от времени
        prof = profiles_get(uid) or {}
        dl, pct = life_metrics(prof)
        maybe_metrics = ""
        if random.random() < 0.2 and dl:
            bar = progress_bar(pct)
            if prof.get("birth_date"):
                maybe_metrics = f"\n{('Сегодня твой' if lang!='en' else 'Today is your')} {dl}-й день жизни. {bar} {int(pct*100)}%."
            else:
                maybe_metrics = f"\n{('Оценочно, ' if lang!='en' else 'Approx., ')}{dl} дней. {bar} {int(pct*100)}%."
        await context.bot.send_message(uid, (T[lang]["daily_gm"] + maybe_metrics), reply_markup=kb)
    except Exception as e:
        logging.error(f"job_daily_checkin send error: {e}")


# ---------- Персонализационные утилиты ----------

def _short_coach_reply(lang: str, mood: str, prof: dict) -> str:
    packs = {
        "ru": {
            "great": ["Круто! Закрепим — 10 минут прогулки 🌿", "Отличный старт. Стакан воды? 💧"],
            "ok":    ["Неплохо. 3 глубоких вдоха — и вперёд.", "Маленький шаг: 5 минут шеи 🧘"],
            "tired": ["Понимаю. Попробуй 200–300 мл воды и 5 мин отдыха.", "Минутка на окно/воздух — помогает."],
            "pain":  ["Сочувствую. Оцени по 0–10 и скажи, где именно? Если ≥7/10 — лучше к врачу.", "Может помочь короткая пауза, вода и тёплый шарф (если шея/горло)."]
        },
        "en": {
            "great": ["Nice! Lock it in with a 10-min walk 🌿", "Strong start. Glass of water? 💧"],
            "ok":    ["Not bad. 3 deep breaths — go.", "Tiny win: 5-min neck reset 🧘"],
            "tired": ["Got it. Try 200–300 ml water + 5-min break.", "Crack a window, short fresh-air reset."],
            "pain":  ["Sorry about that. Rate 0–10 & where? If ≥7/10 — consider care.", "Brief rest + water; scarf/heat if neck/throat."]
        }
    }
    return random.choice(packs.get(lang, packs["en"]).get(mood, ["👍"]))


def _ai_profile_autoupdate(uid: int):
    """
    Простейший автоапдейт профиля по накопленным данным.
    Здесь оставляем лёгкий набросок, чтобы не усложнять.
    """
    prof = profiles_get(uid) or {}
    streak = int(str(prof.get("streak") or "0") or 0)
    tips = []
    if streak >= 3:
        tips.append("good_streak")
    ai = {"hints": tips, "ts": iso(utcnow())}
    profiles_upsert(uid, {"ai_profile": json.dumps(ai, ensure_ascii=False)})


def _touch_streak(uid: int, acted: bool):
    """
    acted=True — был ответ, увеличиваем стрик;
    acted=False — skip, ничего не меняем.
    """
    prof = profiles_get(uid) or {}
    today = (user_local_now(uid)).date().isoformat()
    last = (prof.get("gm_last_date") or "")
    streak = int(str(prof.get("streak") or "0") or 0)
    best   = int(str(prof.get("streak_best") or "0") or 0)
    if acted:
        if last == today:
            pass  # уже считали
        else:
            # если вчера — продолжаем, иначе с 1
            try:
                y_date = (user_local_now(uid) - timedelta(days=1)).date().isoformat()
                if last == y_date:
                    streak += 1
                else:
                    streak = 1
            except Exception:
                streak = 1
        best = max(best, streak)
        profiles_upsert(uid, {"streak": str(streak), "streak_best": str(best), "gm_last_date": today})
    else:
        # skip — просто отметим дату, не меняя streak
        if last != today:
            profiles_upsert(uid, {"gm_last_date": today})


# ---------- Переопределённый cb_handler с полной логикой гибридных кнопок ----------

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
        await show_quickbar(context, uid, lang)
        return

    # -------- MINI-INTAKE ----------
    if data.startswith("mini|"):
        _, action, key, *rest = data.split("|")
        if action == "choose":
            value = rest[0] if rest else ""
            mini_handle_choice(uid, key, value)
        elif action == "skip":
            s = sessions.setdefault(uid, {"mini_active": True, "mini_step": 0, "mini_answers": {}})
            s["mini_step"] = int(s.get("mini_step", 0)) + 1
        elif action == "write":
            s = sessions.setdefault(uid, {"mini_active": True, "mini_step": 0, "mini_answers": {}})
            s["mini_wait_key"] = key
            await q.edit_message_text(T[lang]["write"] + "…")
            return
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return

    # -------- Гибрид-меню ----------
    if data.startswith("menu|"):
        act = data.split("|",1)[1]
        if act == "h60":
            sessions.setdefault(uid, {})["awaiting_h60_text"] = True
            await context.bot.send_message(uid, T[lang]["h60_intro"])
        elif act == "er":
            await context.bot.send_message(uid, T[lang]["er_text"])
        elif act == "lab":
            sessions.setdefault(uid, {})["await_lab_city"] = True
            await context.bot.send_message(uid, T[lang]["act_city_prompt"])
        elif act == "rem":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(T[lang]["act_rem_4h"],  callback_data="rem|4h")],
                [InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="rem|eve")],
                [InlineKeyboardButton(T[lang]["act_rem_morn"],callback_data="rem|morn")]
            ])
            await context.bot.send_message(uid, T[lang]["remind_when"], reply_markup=kb)
        elif act == "energy":
            row = [InlineKeyboardButton(str(i), callback_data=f"energy|rate|{i}") for i in range(1,6)]
            await context.bot.send_message(uid, T[lang]["gm_energy_q"], reply_markup=InlineKeyboardMarkup([row]))
        elif act == "hydrate":
            tip = T[lang]["hydrate_nudge"]
            daily_add(iso(utcnow()), uid, mood="", comment=tip, energy=None)
            # прогресс челленджа воды (если запущен)
            d = challenge_get(uid)
            if d and (d.get("name") == "hydrate7") and (d.get("status") == "open"):
                done = challenge_inc(uid)
                await context.bot.send_message(uid, T[lang]["challenge_progress"].format(d=done, len=d.get("length_days","7")))
            await context.bot.send_message(uid, tip)
        elif act == "skintip":
            prof = profiles_get(uid) or {}
            age = int(re.search(r"\d+", str(prof.get("age") or "0")).group(0)) if re.search(r"\d+", str(prof.get("age") or "")) else 0
            sex = (prof.get("sex") or "").lower()
            tip = _get_skin_tip(lang, sex, age)
            await context.bot.send_message(uid, f"{T[lang]['daily_tip_prefix']} {tip}")
        await show_quickbar(context, uid, lang)
        return

    # -------- Универсальные напоминания из меню --------
    if data.startswith("rem|"):
        opt = data.split("|",1)[1]
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
        await show_quickbar(context, uid, lang)
        return

    # -------- Daily GM ----------
    if data.startswith("gm|"):
        _, sub, *rest = data.split("|")
        if sub == "mood":
            mood = rest[0] if rest else "ok"
            daily_add(iso(utcnow()), uid, mood=mood, comment="")
            _touch_streak(uid, acted=True)
            # короткий коуч-ответ
            prof = profiles_get(uid) or {}
            msg = _short_coach_reply(lang, mood, prof)
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(uid, msg)
            await show_quickbar(context, uid, lang)
        elif sub == "skip":
            _touch_streak(uid, acted=False)
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(uid, T[lang]["thanks"])
            await show_quickbar(context, uid, lang)
        return

    # -------- Энергия ----------
    if data.startswith("energy|rate|"):
        try:
            val = int(data.split("|")[-1])
        except:
            val = None
        daily_add(iso(utcnow()), uid, mood="", comment="energy", energy=val)
        _touch_streak(uid, acted=True)
        await q.edit_message_reply_markup(None)
        await context.bot.send_message(uid, T[lang]["gm_energy_done"])
        await show_quickbar(context, uid, lang)
        return

    # -------- Youth / Cycle / Tips ----------
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
        await show_quickbar(context, uid, lang)
        return

    # -------- Cycle consent ----------
    if data.startswith("cycle|consent|"):
        val = data.split("|")[-1]
        if val == "yes":
            sessions.setdefault(uid, {})["await_cycle_last"] = True
            await context.bot.send_message(uid, T[lang]["cycle_ask_last"])
        else:
            await context.bot.send_message(uid, T[lang]["thanks"])
        await show_quickbar(context, uid, lang)
        return

    # -------- Numeric check-in on episode ----------
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
        await show_quickbar(context, uid, lang)
        return

    # -------- Feedback ----------
    if data.startswith("fb|"):
        kind = data.split("|")[1]
        if kind in {"good","bad"}:
            feedback_add(iso(utcnow()), uid, name="", username=users_get(uid).get("username",""), rating=("1" if kind=="bad" else "5"), comment="")
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(uid, T[lang]["fb_thanks"])
        elif kind == "free":
            sessions.setdefault(uid, {})["await_fb_msg"] = True
            await q.edit_message_text(T[lang]["fb_write"])
        await show_quickbar(context, uid, lang)
        return


# ---------- Переопределённая обработка текста: добавлены lab-город и сжатие ответов ----------

def _trim_to_5_lines(text: str) -> str:
    lines = (text or "").strip().splitlines()
    # оставим не более 5 строк
    lines = [l.strip() for l in lines if l.strip()]
    if len(lines) > 5:
        lines = lines[:5]
    return "\n".join(lines)

# Переписываем _process_health60, чтобы сразу логировать эпизод
async def _process_health60(uid: int, lang: str, text: str, msg_obj):
    prof = profiles_get(uid) or {}
    prefix = personalized_prefix(lang, prof)
    plan = health60_make_plan(lang, text, prof)
    final = _trim_to_5_lines((prefix + "\n" if prefix else "") + plan)

    # логируем эпизод для счётчика повторов темы
    topic_guess = "headache" if re.search(r"голов|head", text.lower()) else ("belly" if re.search(r"живот|stomach|belly", text.lower()) else "symptom")
    eid = episode_create(uid, topic=topic_guess, severity=5, red="")
    sessions.setdefault(uid, {})["last_eid"] = eid

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
    _ai_profile_autoupdate(uid)

# Переопределяем msg_text, добавив обработку await_lab_city
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
        # birth_date нормализуем
        if key == "birth_date":
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
                # оставим как есть, но лучше уведомить позже
                pass
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return

    # Lab city
    if s.get("await_lab_city"):
        s["await_lab_city"] = False
        profiles_upsert(uid, {"city": text})
        await update.message.reply_text(T[lang]["act_saved"])
        await show_quickbar(context, uid, lang)
        return

    # GM note
    if s.get("await_gm_note"):
        daily_add(iso(utcnow()), uid, mood="", comment=text, energy=None)
        s["await_gm_note"] = False
        await update.message.reply_text(T[lang]["mood_thanks"])
        await show_quickbar(context, uid, lang)
        return

    # Quiet hours
    if s.get("await_quiet"):
        qh = text if re.match(r"^\s*([01]?\d|2[0-3]):[0-5]\d-([01]?\d|2[0-3]):[0-5]\d\s*$", text) else DEFAULT_QUIET_HOURS
        profiles_upsert(uid, {"quiet_hours": qh})
        s["await_quiet"] = False
        await update.message.reply_text(T[lang]["quiet_saved"].format(qh=qh))
        await show_quickbar(context, uid, lang)
        return

    # Cycle flow
    if s.get("await_cycle_last"):
        if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            profiles_upsert(uid, {"cycle_last_date": text})
            s["await_cycle_last"] = False
            s["await_cycle_len"]  = True
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
        await show_quickbar(context, uid, lang)
        return

    # Feedback free-form
    if s.get("await_fb_msg"):
        feedback_add(iso(utcnow()), uid, name="", username=users_get(uid).get("username",""), rating="0", comment=text[:800])
        s["await_fb_msg"] = False
        await update.message.reply_text(T[lang]["fb_thanks"])
        await show_quickbar(context, uid, lang)
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
        await show_quickbar(context, uid, lang)
        return

    # Router default (concise assistant mode)
    prof = profiles_get(uid) or {}
    prefix = personalized_prefix(lang, prof)
    route = llm_router_answer(text, lang, prof)
    reply = route.get("assistant_reply") or T[lang]["unknown"]
    followups = route.get("followups") or []
    lines = [_trim_to_5_lines(reply)]
    if followups:
        short_f = [f for f in followups if f.strip()][:4]
        if short_f:
            lines.append("")
            lines.append("— " + "\n— ".join(short_f))
    final = (prefix + "\n" if prefix else "") + "\n".join([l for l in lines if l]).strip()
    await send_unique(update.message, uid, final)
    await show_quickbar(context, uid, lang)
