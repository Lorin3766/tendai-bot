# -*- coding: utf-8 -*-
# ==========================================
# TendAI — FULL CODE (Part 1 / 2)
# Base, i18n, storage (Sheets with 429-fix), intake, rules, care
# Часть 2 добавит: коллбеки, автодетект языка на каждом сообщении,
# триаж-флоу, планировщик, джобы, build_app() и main().
# ==========================================

import os, re, json, uuid, logging, random, time
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

# ---------- Google Sheets (robust + memory fallback + 429 cooldown) ----------
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
DEFAULT_WEEKLY_LOCAL   = "10:00"  # Sunday
DEFAULT_QUIET_HOURS    = "22:00-08:00"

AUTO_MAX_PER_DAY = 2  # авто-сообщений/день максимум

# OpenAI client (optional)
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


# --- Localization of profile values & prefix ---
LOCALIZE_MAP = {
    "sex": {
        "ru": {"male":"мужской","female":"женский","other":"другой"},
        "uk": {"male":"чоловіча","female":"жіноча","other":"інша"},
        "en": {"male":"male","female":"female","other":"other"},
        "es": {"male":"hombre","female":"mujer","other":"otro"},
    },
    "goal": {
        "ru": {"energy":"энергия","sleep":"сон","weight":"вес","strength":"сила","longevity":"долголетие"},
        "uk": {"energy":"енергія","sleep":"сон","weight":"вага","strength":"сила","longevity":"довголіття"},
        "en": {"energy":"energy","sleep":"sleep","weight":"weight","strength":"strength","longevity":"longevity"},
        "es": {"energy":"energía","sleep":"sueño","weight":"peso","strength":"fuerza","longevity":"longevidad"},
    },
    "activity": {
        "ru": {"low":"низкая","mid":"средняя","high":"высокая","sport":"спорт"},
        "uk": {"low":"низька","mid":"середня","high":"висока","sport":"спорт"},
        "en": {"low":"low","mid":"medium","high":"high","sport":"sport"},
        "es": {"low":"baja","mid":"media","high":"alta","sport":"deporte"},
    },
    "diet_focus": {
        "ru":{"balanced":"сбаланс.","lowcarb":"низкоугл.","plant":"растит.","irregular":"нерегул."},
        "uk":{"balanced":"збаланс.","lowcarb":"маловугл.","plant":"рослинне","irregular":"нерегул."},
        "en":{"balanced":"balanced","lowcarb":"low-carb","plant":"plant-based","irregular":"irregular"},
        "es":{"balanced":"equilibrada","lowcarb":"baja carb.","plant":"vegetal","irregular":"irregular"},
    }
}

def localize_value(lang: str, field: str, value: str) -> str:
    v = (value or "").strip().lower()
    m = LOCALIZE_MAP.get(field, {}).get(lang, {})
    return m.get(v, value)

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

def personalized_prefix(lang: str, profile: dict) -> str:
    sex_raw = (profile.get("sex") or "")
    goal_raw = (profile.get("goal") or "")
    age_raw  = str(profile.get("age") or "")
    m = re.search(r"\d+", age_raw or "")
    age = m.group(0) if m else ""
    sex  = localize_value(lang, "sex", sex_raw)
    goal = localize_value(lang, "goal", goal_raw)
    if sum(bool(x) for x in (sex, age, goal)) >= 2:
        tpl = (T.get(lang) or T["en"]).get("px", T["en"]["px"])
        return tpl.format(sex=sex or "—", age=age or "—", goal=goal or "—")
    return ""


# ---------------- Helpers ----------------
def utcnow() -> datetime: return datetime.now(timezone.utc)
def iso(dt: Optional[datetime]) -> str:
    return "" if not dt else dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")

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

def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()

def _user_lang(uid:int) -> str:
    u = users_get(uid) if 'users_get' in globals() else {}
    return norm_lang((u or {}).get("lang") or "en")


# -------- Sessions (runtime) ----------
sessions: Dict[int, dict] = {}  # ephemeral state per user


# -------- Sheets (with memory fallback + 429-cooldown) --------
SHEETS_ENABLED = True
ss = None
ws_feedback = ws_users = ws_profiles = ws_episodes = ws_reminders = ws_daily = None
ws_rules = ws_challenges = None

GSPREAD_CLIENT: Optional[gspread.client.Client] = None
SPREADSHEET_ID_FOR_INTAKE: str = ""

# быстрый кэш строк (id → row) и «охладитель» при 429
ROW_CACHE = {"Users": {}, "Profiles": {}, "Episodes": {}, "Reminders": {}}
_SHEETS_COOLDOWN_UNTIL = 0

def _gs_can_read() -> bool:
    return time.time() >= _SHEETS_COOLDOWN_UNTIL

def _gs_tripped(e: Exception):
    """Если словили 429 — ставим «паузу» на минуту, а не отключаем Sheets насовсем."""
    global _SHEETS_COOLDOWN_UNTIL
    if "429" in str(e):
        _SHEETS_COOLDOWN_UNTIL = time.time() + 70  # сек.

def _ws_headers(ws):
    try: return ws.row_values(1) if ws else []
    except Exception as e:
        _gs_tripped(e); return []

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

        # лёгкое создание/проверка шитов без get_all_values()
        def _ensure_ws(title: str, headers: List[str]):
            try:
                ws = ss.worksheet(title)
            except WorksheetNotFound:
                ws = ss.add_worksheet(title=title, rows=4000, cols=max(60, len(headers)))
                ws.update('A1', [headers])
                return ws
            try:
                cur = ws.row_values(1)
            except Exception as e:
                _gs_tripped(e); cur = []
            need = (not cur) or (len(cur) < len(headers)) or any((i >= len(cur)) or (cur[i] != h) for i,h in enumerate(headers))
            if need:
                ws.update('A1', [headers])
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

def _headers(ws): return _ws_headers(ws)

# -------- Wrappers (429-friendly): Users / Profiles --------
def users_get(uid: int) -> dict:
    if SHEETS_ENABLED and _gs_can_read():
        try:
            row_i = ROW_CACHE["Users"].get(uid)
            if not row_i:
                cell = ws_users.find(str(uid))
                if cell:
                    row_i = cell.row
                    ROW_CACHE["Users"][uid] = row_i
            if row_i:
                vals = ws_users.row_values(row_i)
                hdr = _headers(ws_users)
                return {hdr[i]: (vals[i] if i < len(vals) else "") for i in range(len(hdr))}
        except Exception as e:
            _gs_tripped(e)
            logging.warning(f"users_get -> memory fallback: {e}")
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
            hdr = _headers(ws_users)
            row_i = ROW_CACHE["Users"].get(uid)
            if not row_i and _gs_can_read():
                cell = ws_users.find(str(uid))
                if cell:
                    row_i = cell.row
                    ROW_CACHE["Users"][uid] = row_i
            if row_i:
                cur = users_get(uid)
                merged = {**base, **{k: (cur.get(k) or base[k]) for k in base}}
                ws_users.update(f"A{row_i}:{gsu.rowcol_to_a1(row_i, len(hdr))}",
                                [[merged.get(h, "") for h in hdr]])
                return
            ws_users.append_row([base.get(h, "") for h in hdr])
            try:
                cell = ws_users.find(str(uid))
                if cell: ROW_CACHE["Users"][uid] = cell.row
            except Exception: pass
            return
        except Exception as e:
            _gs_tripped(e)
            logging.warning(f"users_upsert -> memory fallback: {e}")
    MEM_USERS[uid] = {**MEM_USERS.get(uid, {}), **base}

def users_set(uid: int, field: str, value: str):
    if SHEETS_ENABLED:
        try:
            hdr = _headers(ws_users)
            if field not in hdr:
                ws_users.update('A1', [hdr + [field]])
                hdr = _headers(ws_users)
            row_i = ROW_CACHE["Users"].get(uid)
            if not row_i and _gs_can_read():
                cell = ws_users.find(str(uid))
                if cell:
                    row_i = cell.row
                    ROW_CACHE["Users"][uid] = row_i
            if row_i:
                ws_users.update_cell(row_i, hdr.index(field) + 1, value)
                return
        except Exception as e:
            _gs_tripped(e)
            logging.warning(f"users_set -> memory fallback: {e}")
    u = MEM_USERS.setdefault(uid, {}); u[field] = value

def profiles_get(uid: int) -> dict:
    if SHEETS_ENABLED and _gs_can_read():
        try:
            row_i = ROW_CACHE["Profiles"].get(uid)
            if not row_i:
                cell = ws_profiles.find(str(uid))
                if cell:
                    row_i = cell.row
                    ROW_CACHE["Profiles"][uid] = row_i
            if row_i:
                vals = ws_profiles.row_values(row_i)
                hdr = _headers(ws_profiles)
                return {hdr[i]: (vals[i] if i < len(vals) else "") for i in range(len(hdr))}
        except Exception as e:
            _gs_tripped(e)
            logging.warning(f"profiles_get -> memory fallback: {e}")
    return MEM_PROFILES.get(uid, {})

def profiles_upsert(uid: int, patch: dict):
    patch = dict(patch or {}); patch["user_id"] = str(uid); patch["updated_at"] = iso(utcnow())
    if SHEETS_ENABLED:
        try:
            hdr = _headers(ws_profiles)
            for k in patch.keys():
                if k not in hdr:
                    ws_profiles.update('A1', [hdr + [k]])
                    hdr = _headers(ws_profiles)
            row_i = ROW_CACHE["Profiles"].get(uid)
            if not row_i and _gs_can_read():
                cell = ws_profiles.find(str(uid))
                if cell:
                    row_i = cell.row
                    ROW_CACHE["Profiles"][uid] = row_i
            if row_i:
                cur_vals = ws_profiles.row_values(row_i)
                cur = {hdr[i]: (cur_vals[i] if i < len(cur_vals) else "") for i in range(len(hdr))}
                merged = {**cur, **patch}
                ws_profiles.update(f"A{row_i}:{gsu.rowcol_to_a1(row_i, len(hdr))}",
                                   [[merged.get(h, "") for h in hdr]])
                return
            ws_profiles.append_row([patch.get(h, "") for h in hdr])
            try:
                cell = ws_profiles.find(str(uid))
                if cell: ROW_CACHE["Profiles"][uid] = cell.row
            except Exception: pass
            return
        except Exception as e:
            _gs_tripped(e)
            logging.warning(f"profiles_upsert -> memory fallback: {e}")
    MEM_PROFILES[uid] = {**MEM_PROFILES.get(uid, {}), **patch}

# -------- The rest (episodes/reminders/daily/feedback/challenges) — как было --------
def feedback_add(ts: str, uid: int, name: str, username: str, rating: str, comment: str):
    row = {"timestamp":ts, "user_id":str(uid), "name":name, "username":username, "rating":rating, "comment":comment}
    if SHEETS_ENABLED:
        try:
            hdr = _headers(ws_feedback); ws_feedback.append_row([row.get(h, "") for h in hdr]); return
        except Exception as e:
            _gs_tripped(e); logging.warning(f"feedback_add -> memory fallback: {e}")
    MEM_FEEDBACK.append(row)

def daily_add(ts: str, uid: int, mood: str="", comment: str="", energy: Optional[int]=None):
    row = {"timestamp":ts, "user_id":str(uid), "mood":mood, "energy":("" if energy is None else str(energy)), "comment":comment}
    if SHEETS_ENABLED:
        try:
            hdr = _headers(ws_daily); ws_daily.append_row([row.get(h, "") for h in hdr]); return
        except Exception as e:
            _gs_tripped(e); logging.warning(f"daily_add -> memory fallback: {e}")
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
            _gs_tripped(e); logging.warning(f"episode_create -> memory fallback: {e}"); MEM_EPISODES.append(row)
    else: MEM_EPISODES.append(row)
    return eid

def episode_set(eid: str, key: str, val: str):
    if SHEETS_ENABLED:
        try:
            # адресный апдейт (ищем эпизод по id)
            cell = ws_episodes.find(eid)
            if cell:
                row_i = cell.row
                hdr = _headers(ws_episodes)
                if key not in hdr:
                    ws_episodes.update('A1', [hdr + [key]]); hdr = _headers(ws_episodes)
                cur = ws_episodes.row_values(row_i)
                merged = {hdr[i]: (cur[i] if i < len(cur) else "") for i in range(len(hdr))}
                merged[key] = val
                ws_episodes.update(f"A{row_i}:{gsu.rowcol_to_a1(row_i,len(hdr))}", [[merged.get(h,"") for h in hdr]])
                return
        except Exception as e:
            _gs_tripped(e); logging.warning(f"episode_set -> memory fallback: {e}")
    for r in MEM_EPISODES:
        if r.get("episode_id")==eid:
            r[key]=val; return

def episode_find_open(uid: int) -> Optional[dict]:
    if SHEETS_ENABLED and _gs_can_read():
        try:
            # дешёвая эвристика: ищем последние N строк и фильтруем; либо храним кеш id→row
            vals = ws_episodes.get_all_records()[-50:]  # редкая операция
            for r in reversed(vals):
                if str(r.get("user_id"))==str(uid) and (r.get("status")=="open"):
                    return r
        except Exception as e:
            _gs_tripped(e); logging.warning(f"episode_find_open fallback: {e}")
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
            _gs_tripped(e); logging.warning(f"reminder_add -> memory fallback: {e}")
    MEM_REMINDERS.append(row); return rid

def challenge_get(uid: int) -> Optional[dict]:
    if SHEETS_ENABLED and _gs_can_read():
        try:
            rows = ws_challenges.get_all_records()[-100:]  # редкая операция
            for r in reversed(rows):
                if r.get("user_id")==str(uid) and r.get("status")!="done":
                    return r
        except Exception as e:
            _gs_tripped(e); logging.warning(f"challenge_get fallback: {e}")
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
            _gs_tripped(e); logging.warning(f"challenge_start -> memory fallback: {e}")
    MEM_CHALLENGES.append(row)


# ---------- Quickbar & Menus ----------
def quickbar_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["quick_h60"], callback_data="menu|h60")],
        [InlineKeyboardButton(T[lang]["quick_er"], callback_data="menu|er"),
         InlineKeyboardButton(T[lang]["quick_lab"], callback_data="menu|lab")],
        [InlineKeyboardButton(T[lang]["quick_rem"], callback_data="menu|rem")]
    ])

async def show_quickbar(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: Optional[int]=None, force: bool=False):
    try:
        if uid and not force:
            s = sessions.setdefault(uid, {})
            if s.get("qb_sent"):
                return
            s["qb_sent"] = True
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
        clar = {
            "ru":"Где именно болит и сколько длится? Есть температура/травма?",
            "uk":"Де саме болить і скільки триває? Є температура/травма?",
            "en":"Where exactly is the pain and for how long? Any fever/trauma?",
            "es":"¿Dónde exactamente y desde cuándo? ¿Fiebre/trauma?",
        }[lang]
        return {"intent":"other","assistant_reply":clar,"followups":[clar],"needs_more":True,"red_flags":False,"confidence":0.3}
    sys = SYS_ROUTER.replace("{lang}", lang) + f"\nUserProfile: {json.dumps(profile, ensure_ascii=False)}"
    try:
        resp = oai.chat_completions.create(
            model=OPENAI_MODEL, temperature=0.25, max_tokens=420,
            response_format={"type":"json_object"},
            messages=[{"role":"system","content":sys},{"role":"user","content":text}]
        )
        content = getattr(resp.choices[0].message, "content", None) or getattr(resp.choices[0], "message", {}).get("content","")
        data = json.loads((content or "{}").strip() or "{}")
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
        resp = oai.chat_completions.create(
            model=OPENAI_MODEL, temperature=0.2, max_tokens=420,
            response_format={"type":"json_object"},
            messages=[{"role":"system","content":sys},{"role":"user","content":json.dumps(user, ensure_ascii=False)}]
        )
        content = getattr(resp.choices[0].message, "content", None) or getattr(resp.choices[0], "message", {}).get("content","")
        data = json.loads((content or "{}").strip() or "{}")
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
    """Очень лёгкая эвристика — считаем упоминания (кофеин/сон/голова/вода)."""
    low = (text or "").lower()
    p = profiles_get(uid) or {}
    obj = _ai_obj(p)
    def inc(k, dv=1): obj[k] = int(obj.get(k, 0)) + dv
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
    return f"{T[lang]['daily_tip_prefix']} {_get_daily_tip(p, lang)}"


# ---------- Auto-message limiter (unified) ----------
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


# ---------- Scheduling API (без джоб; джобы — в Части 2) ----------
def schedule_oneoff_local(app, uid: int, local_after_hours: float, text: str):
    """Разовое напоминание через N часов (локально, с тихими часами). job_oneoff_reminder — в Части 2."""
    now_local = user_local_now(uid)
    when_local = now_local + timedelta(hours=local_after_hours)
    when_local = adjust_out_of_quiet(when_local, _user_quiet_hours(uid))
    when_utc = local_to_utc_dt(uid, when_local)
    rid = reminder_add(uid, text, when_utc)
    if getattr(app, "job_queue", None):
        # сам job_oneoff_reminder определим в части 2
        app.job_queue.run_once(lambda *_: None, when=(when_utc - utcnow()), data={"user_id":uid, "reminder_id":rid})
    return rid


# ---------- Rules (evidence-based) ----------
def rules_lookup(topic: str, segment: str, lang: str) -> Optional[str]:
    try:
        if SHEETS_ENABLED and ws_rules and _gs_can_read():
            rows = ws_rules.get_all_records()[-200:]  # ограничим выборку
            for r in rows:
                if (r.get("enabled","").strip().lower() in {"1","true","yes"}) and \
                   (r.get("topic","").strip().lower() == (topic or "").strip().lower()):
                    seg = (r.get("segment") or "").strip().lower()
                    if not seg or seg == (segment or "").strip().lower():
                        txt = (r.get("advice_text") or "").strip()
                        if not txt: continue
                        m = re.search(r"\[\[\s*"+re.escape(lang)+r"\s*:(.*?)\]\]", txt, re.DOTALL)
                        if m: return m.group(1).strip()
                        return txt
    except Exception as e:
        _gs_tripped(e); logging.warning(f"rules_lookup fallback: {e}")
    return None


# ---------- Gentle one-liners ----------
def tiny_care_tip(lang: str, mood: str, profile: dict) -> str:
    if lang == "ru":
        if mood == "excellent": return "🔥 Отлично! Сохраним ритм — 5 минут движения к кофе?"
        if mood == "ok":        return "🙂 300 мл воды и 1 минуту дыхания 4-6."
        if mood == "tired":     return "😴 Прогулка 5–7 мин + стакан воды."
        if mood == "pain":      return "🤕 Наблюдаем за болью. Если усиливается — «⚡ 60 сек»."
        return "👍 Бережём темп и воду рядом."
    if lang == "uk":
        if mood == "excellent": return "🔥 Круто! 5 хвилин руху до кави?"
        if mood == "ok":        return "🙂 300 мл води і 1 хв дихання 4-6."
        if mood == "tired":     return "😴 Прогулянка 5–7 хв + вода."
        if mood == "pain":      return "🤕 Якщо посилюється — «⚡ 60 c»."
        return "👍 Бережемо темп і воду поруч."
    if mood == "excellent": return "🔥 Nice! 5-min walk before coffee?"
    if mood == "ok":        return "🙂 300 ml water + 1-min 4-6 breathing."
    if mood == "tired":     return "😴 Try 5–7 min walk + water."
    if mood == "pain":      return "🤕 If worsening, tap “⚡ 60s”."
    return "👍 Keep the pace and keep water nearby."


# ---------- Challenge helpers ----------
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
            _gs_tripped(e); logging.warning(f"challenge_tick -> memory fallback: {e}")
            ch["days_done"] = str(done)
            if done >= length: ch["status"] = "done"
    else:
        ch["days_done"] = str(done)
        if done >= length: ch["status"] = "done"
    return f"{T[norm_lang((users_get(uid) or {}).get('lang') or 'en')]['challenge_progress'].format(d=done, len=length)}"


# ---------- Streak helpers ----------
def streak_update(uid: int) -> Optional[str]:
    u = users_get(uid) or {}
    today = datetime.utcnow().date().isoformat()
    last = (u.get("gm_last_date") or "")
    if last == today:
        return None
    try:
        streak = int(u.get("streak") or "0")
        best   = int(u.get("streak_best") or "0")
    except Exception:
        streak, best = 0, 0
    streak = streak + 1 if last else 1
    best = max(best, streak)
    users_set(uid, "streak", str(streak))
    users_set(uid, "streak_best", str(best))
    users_set(uid, "gm_last_date", today)
    if streak in (3, 7, 14, 30):
        lang = norm_lang((u.get("lang") or "en"))
        msg = {
            "ru": f"🔥 {streak} дней подряд! Держим мягкий темп.",
            "uk": f"🔥 {streak} днів поспіль! Тримаємо темп.",
            "en": f"🔥 {streak} days in a row! Keep it gentle.",
            "es": f"🔥 ¡{streak} días seguidos! Suave y constante.",
        }[lang]
        return msg
    return None


# ---------- Cycle helpers ----------
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
        "luteal":"Магний/омега по согласованию с врачом, береги ритм."
    }
    tips_en = {
        "menstruation":"Gentle mode: hydration, sleep, light stretching.",
        "follicular":"Great window for training and focus tasks.",
        "ovulation":"Energy is up — guard sleep and hydration.",
        "luteal":"Consider magnesium/omega (doctor-approved), keep the pace."
    }
    src = tips_ru if lang in {"ru","uk"} else tips_en
    return src.get(phase, src["follicular"])


# ---------- MINI-INTAKE ----------
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

async def start_mini_intake(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: int):
    sessions[uid] = sessions.get(uid, {})
    sessions[uid].update({"mini_active": True, "mini_step": 0, "mini_answers": {}, "qb_sent": False})
    await context.bot.send_message(chat_id, {
        "ru":"🔎 Мини-опрос для персонализации (4–6 кликов).",
        "uk":"🔎 Міні-опитування для персоналізації (4–6 кліків).",
        "en":"🔎 Mini-intake for personalization (4–6 taps).",
        "es":"🔎 Mini-intake para personalización (4–6 toques).",
    }[lang], reply_markup=ReplyKeyboardRemove())
    await ask_next_mini(context, chat_id, lang, uid)

def mini_handle_choice(uid: int, key: str, value: str):
    s = sessions.setdefault(uid, {"mini_active": True, "mini_step": 0, "mini_answers": {}})
    s["mini_answers"][key] = value
    s["mini_step"] = int(s.get("mini_step", 0)) + 1

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
        profiles_upsert(uid, {"ai_profile": json.dumps({"v":1,"habits":answers.get("habits","")}, ensure_ascii=False)})
        await context.bot.send_message(chat_id, T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
        await show_quickbar(context, chat_id, lang, uid)
        return
    key = MINI_KEYS[step_idx]
    label = MINI_STEPS[key]["label"][lang]
    await context.bot.send_message(chat_id, label, reply_markup=build_mini_kb(lang, key))
# ---------- Small helpers ----------
def _user_lang(uid: int) -> str:
    try:
        return norm_lang((users_get(uid) or {}).get("lang") or "en")
    except Exception:
        return "en"

def _parse_tz(s: str) -> Optional[int]:
    m = re.match(r"^\s*([+-]?\d{1,2})\s*$", s or "")
    if not m: return None
    try:
        off = int(m.group(1))
        if -12 <= off <= 14: return off
        return None
    except Exception:
        return None

def _parse_quiet(s: str) -> Optional[str]:
    if re.match(r"^\s*([01]?\d|2[0-3]):[0-5]\d-([01]?\d|2[0-3]):[0-5]\d\s*$", s or ""):
        return s.strip()
    return None

# ---------- Language commands ----------
async def _set_lang(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str):
    uid = update.effective_user.id
    users_upsert(uid, update.effective_user.username, lang)
    users_set(uid, "lang", lang)
    await update.message.reply_text(T[lang]["thanks"])
    await show_quickbar(context, update.effective_chat.id, lang, uid, force=True)

async def cmd_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):  await _set_lang(update, context, "ru")
async def cmd_en(update: Update, context: ContextTypes.DEFAULT_TYPE):  await _set_lang(update, context, "en")
async def cmd_uk(update: Update, context: ContextTypes.DEFAULT_TYPE):  await _set_lang(update, context, "uk")
async def cmd_es(update: Update, context: ContextTypes.DEFAULT_TYPE):  await _set_lang(update, context, "es")

# ---------- Core commands ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    guess = norm_lang(getattr(update.effective_user, "language_code", "en"))
    users_upsert(uid, update.effective_user.username, guess)
    lang = _user_lang(uid)
    users_set(uid, "last_seen", iso(utcnow()))
    await update.message.reply_text(T[lang]["welcome"])
    # Quickbar and optional intake
    prof = profiles_get(uid) or {}
    if profile_is_incomplete(prof):
        await start_mini_intake(context, update.effective_chat.id, lang, uid)
    else:
        await show_quickbar(context, update.effective_chat.id, lang, uid, force=True)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    await update.message.reply_text(T[lang]["help"])
    await show_quickbar(context, update.effective_chat.id, lang, uid)

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    await update.message.reply_text(T[lang]["privacy"])

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    users_set(uid, "paused", "yes")
    await update.message.reply_text(T[lang]["paused_on"])

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    users_set(uid, "paused", "no")
    await update.message.reply_text(T[lang]["paused_off"])

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    # memory stores – wipe
    MEM_USERS.pop(uid, None); MEM_PROFILES.pop(uid, None)
    # (Sheets удалять не будем — мягкая политика)
    await update.message.reply_text(T[lang]["deleted"])

def _fmt_profile_brief(lang: str, p: dict) -> str:
    pref = personalized_prefix(lang, p) or ""
    items = []
    sv = lambda f: localize_value(lang, f, (p.get(f) or ""))
    if p.get("diet_focus"): items.append(sv("diet_focus"))
    if p.get("activity"):   items.append(sv("activity"))
    if p.get("steps_target"): items.append(str(p.get("steps_target")))
    if p.get("habits"): items.append(str(p.get("habits")))
    brief = ", ".join([x for x in items if str(x).strip()])
    return (pref + ("\n" + brief if brief else "")).strip()

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    p = profiles_get(uid) or {}
    if not p: 
        await update.message.reply_text({"ru":"Профиль пока пуст.","uk":"Профіль порожній.","en":"Profile is empty.","es":"Perfil vacío."}[lang])
    else:
        await update.message.reply_text(_fmt_profile_brief(lang, p))

# ---------- Time & scheduling commands ----------
async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    if not context.args:
        await update.message.reply_text({"ru":"Пример: /settz +3","uk":"Приклад: /settz +3","en":"Example: /settz +3","es":"Ej.: /settz +3"}[lang]); return
    off = _parse_tz(context.args[0])
    if off is None:
        await update.message.reply_text({"ru":"Введите смещение, например +3 или -5.","uk":"Введіть зсув, напр. +3 або -5.","en":"Enter offset like +3 or -5.","es":"Introduce offset como +3 o -5."}[lang]); return
    users_set(uid, "tz_offset", str(off))
    await update.message.reply_text({"ru":f"Часовой сдвиг сохранён: {off}","uk":f"Зсув часу збережено: {off}","en":f"Time offset saved: {off}","es":f"Offset guardado: {off}"}[lang])

async def cmd_checkin_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    hhmm = context.args[0] if context.args else DEFAULT_CHECKIN_LOCAL
    if hhmm_tuple(hhmm) is None: hhmm = DEFAULT_CHECKIN_LOCAL
    users_set(uid, "checkin_hour", hhmm)
    schedule_daily_checkin(context.application, uid, _user_tz_off(uid), hhmm, lang)
    await update.message.reply_text({"ru":f"Утренний чек-ин в {hhmm}.","uk":f"Ранковий чек-ін о {hhmm}.","en":f"Morning check-in at {hhmm}.","es":f"Check-in matutino a las {hhmm}."}[lang])

async def cmd_checkin_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    _remove_jobs(context.application, f"gm_{uid}")
    await update.message.reply_text({"ru":"Утренний чек-ин отключён.","uk":"Ранковий чек-ін вимкнено.","en":"Morning check-in disabled.","es":"Check-in matutino desactivado."}[_user_lang(uid)])

async def cmd_evening_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    hhmm = context.args[0] if context.args else DEFAULT_EVENING_LOCAL
    users_set(uid, "evening_hour", hhmm)
    schedule_evening_checkin(context.application, uid, _user_tz_off(uid), hhmm, lang)
    await update.message.reply_text(T[lang]["evening_set"].format(t=hhmm))

async def cmd_evening_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    _remove_jobs(context.application, f"eve_{uid}")
    await update.message.reply_text(T[_user_lang(uid)]["evening_off"])

# ---------- Quick Commands ----------
async def cmd_health60(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    sessions.setdefault(uid, {})["awaiting_h60_text"] = True
    await update.message.reply_text(T[lang]["h60_intro"])

async def cmd_hydrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    await update.message.reply_text(T[lang]["hydrate_nudge"])
    tip = _challenge_tick(uid)
    if tip: await update.message.reply_text(tip)

async def cmd_skintip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    p = profiles_get(uid) or {}
    try: age = int(re.search(r"\d+", str(p.get("age","") or "0")).group(0))
    except: age = 30
    await update.message.reply_text(_get_skin_tip(lang, p.get("sex",""), age))

async def cmd_energy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(str(i), callback_data=f"energy|{i}") for i in range(1,6)]])
    await update.message.reply_text(T[lang]["gm_energy_q"], reply_markup=kb)

async def cmd_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["yes"], callback_data="cycle|consent|yes"),
                                InlineKeyboardButton(T[lang]["no"],  callback_data="cycle|consent|no")]])
    await update.message.reply_text(T[lang]["cycle_consent"], reply_markup=kb)

async def cmd_youth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    if not challenge_get(uid):
        challenge_start(uid, "water7", 7)
        await update.message.reply_text(T[lang]["challenge_started"])
    await update.message.reply_text(T[lang]["challenge_progress"].format(d=int(challenge_get(uid).get("days_done","0")), len=int(challenge_get(uid).get("length_days","7"))))

# ---------- Quiet hours via callback ----------
async def cmd_quiet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lang = _user_lang(uid)
    sessions.setdefault(uid, {})["await_quiet"] = True
    await update.message.reply_text(T[lang]["ask_quiet"])

# ---------- Override jobs to respect pause flag ----------
async def job_gm(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}; uid = d.get("user_id"); 
    if (users_get(uid) or {}).get("paused") == "yes": return
    lang = _user_lang(uid)
    if not _auto_allowed(uid): return
    local_now = user_local_now(uid)
    if adjust_out_of_quiet(local_now, _user_quiet_hours(uid)) != local_now: return
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
    d = context.job.data or {}; uid = d.get("user_id")
    if (users_get(uid) or {}).get("paused") == "yes": return
    lang = _user_lang(uid)
    if not _auto_allowed(uid): return
    local_now = user_local_now(uid)
    if adjust_out_of_quiet(local_now, _user_quiet_hours(uid)) != local_now: return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["evening_tip_btn"], callback_data="eve|tip")],
        [InlineKeyboardButton(T[lang]["hydrate_btn"],     callback_data="water|nudge")]
    ])
    try:
        await context.bot.send_message(uid, T[lang]["evening_intro"], reply_markup=kb)
        _auto_inc(uid)
    except Exception as e:
        logging.warning(f"job_evening send failed: {e}")

# ---------- Callback handler (extend) ----------
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q: return
    await q.answer()
    uid = q.from_user.id
    lang = _lang_for_cb(uid)
    data = (q.data or "")
    parts = data.split("|")
    kind = parts[0]

    # MINI-INTAKE
    if kind == "mini":
        action = parts[1]; key = parts[2]
        if action == "choose":
            val = parts[3]; mini_handle_choice(uid, key, val)
            await ask_next_mini(context, q.message.chat_id, lang, uid); return
        if action == "write":
            s = sessions.setdefault(uid, {}); s["mini_wait_key"] = key
            await q.message.reply_text(T[lang]["write"]); return
        if action == "skip":
            mini_handle_choice(uid, key, ""); await ask_next_mini(context, q.message.chat_id, lang, uid); return

    # MENU shortcuts
    if kind == "menu":
        sub = parts[1]
        if sub == "h60":
            sessions.setdefault(uid, {})["awaiting_h60_text"] = True
            await q.message.reply_text(T[lang]["h60_intro"]); return
        if sub == "rem":
            # quick 4h reminder
            rid = schedule_oneoff_local(context.application, uid, 4, {"ru":"Проверить самочувствие","uk":"Перевірити самопочуття","en":"Check how I feel","es":"Revisar cómo me siento"}[lang])
            await q.message.reply_text({"ru":"Напомню через ~4 часа.","uk":"Нагадаю за ~4 години.","en":"I’ll remind you in ~4 hours.","es":"Te recordaré en ~4 horas."}[lang]); return
        if sub == "lab":
            await q.message.reply_text({"ru":"🧪 Лаборатория: пока короткие рекомендации. Добавим позже.","uk":"🧪 Лабораторія: короткі поради. Додамо пізніше.","en":"🧪 Labs: short guidance for now. More later.","es":"🧪 Laboratorio: guía corta por ahora. Más tarde."}[lang]); return
        if sub == "er":
            await q.message.reply_text({"ru":"🚑 Если сильная боль в груди, слабость одной стороны, потеря сознания — срочно в скорую.","uk":"🚑 Сильний біль у грудях, слабкість однієї сторони, втрата свідомості — викликайте швидку.","en":"🚑 Severe chest pain, one-sided weakness, fainting — call emergency.","es":"🚑 Dolor torácico intenso, debilidad unilateral, desmayo — emergencias."}[lang]); return

    # GM mood/skip/note
    if kind == "gm":
        sub = parts[1]
        if sub == "mood":
            mood = parts[2]
            daily_add(iso(utcnow()), uid, mood=mood, comment="")
            tip = tiny_care_tip(lang, mood, profiles_get(uid) or {})
            await q.message.reply_text(tip)
            streak_msg = streak_update(uid)
            if streak_msg: await q.message.reply_text(streak_msg)
            # Quick actions after mood
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(T[lang]["act_rem_4h"],  callback_data="rem|4h"),
                 InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="rem|eve"),
                 InlineKeyboardButton(T[lang]["act_rem_morn"],callback_data="rem|morn")],
                [InlineKeyboardButton(T[lang]["act_ex_neck"], callback_data="yt|neck"),
                 InlineKeyboardButton(T[lang]["act_save_episode"], callback_data=f"ep|save|{episode_create(uid,'general',severity=5)}")]
            ])
            await q.message.reply_text(T[lang]["mood_thanks"], reply_markup=kb)
            return
        if sub == "note":
            sessions.setdefault(uid, {})["await_gm_note"] = True
            await q.message.reply_text(T[lang]["fb_write"]); return
        if sub == "skip":
            daily_add(iso(utcnow()), uid, mood="skip")
            await q.message.reply_text(T[lang]["thanks"]); return

    # Energy quick scale 1..5
    if kind == "energy":
        try:
            e = int(parts[1]); e = max(1, min(5, e))
        except: e = 3
        daily_add(iso(utcnow()), uid, energy=e)
        await q.message.reply_text(T[lang]["gm_energy_done"]); return

    # Evening
    if kind == "eve" and parts[1] == "tip":
        p = profiles_get(uid) or {}
        await q.message.reply_text(build_care_nudge(uid, lang)); return

    # Water nudge
    if kind == "water" and parts[1] == "nudge":
        await q.message.reply_text(T[lang]["hydrate_nudge"])
        tip = _challenge_tick(uid)
        if tip: await q.message.reply_text(tip)
        return

    # Reminders quick
    if kind == "rem":
        when = parts[1]
        if when == "4h":
            schedule_oneoff_local(context.application, uid, 4, {"ru":"Проверка самочувствия","uk":"Перевірити самопочуття","en":"Check-in on symptoms","es":"Revisar síntomas"}[lang])
        elif when == "eve":
            # schedule ~18:30 local
            h, m = hhmm_tuple(DEFAULT_EVENING_LOCAL)
            now = user_local_now(uid)
            target = now.replace(hour=h, minute=m) + timedelta(minutes=5)
            delay = max(0.5, (target - now).total_seconds()/3600.0)
            schedule_oneoff_local(context.application, uid, delay, {"ru":"Лёгкая вечерняя проверка","uk":"Легка вечірня перевірка","en":"Gentle evening check","es":"Revisión vespertina suave"}[lang])
        elif when == "morn":
            h, m = hhmm_tuple(DEFAULT_CHECKIN_LOCAL)
            now = user_local_now(uid).replace(hour=h, minute=m) + timedelta(days=1)
            delay = max(0.5, (now - user_local_now(uid)).total_seconds()/3600.0)
            schedule_oneoff_local(context.application, uid, delay, {"ru":"Утренний чек-ин","uk":"Ранковий чек-ін","en":"Morning check-in","es":"Check-in matutino"}[lang])
        await q.message.reply_text(T[lang]["thanks"]); return

    # Cycle tracking
    if kind == "cycle":
        sub = parts[1]
        if sub == "consent":
            if parts[2] == "yes":
                sessions.setdefault(uid, {})["await_cycle_last"] = True
                await q.message.reply_text(T[lang]["cycle_ask_last"]); return
            else:
                profiles_upsert(uid, {"cycle_enabled":"no"})
                await q.message.reply_text(T[lang]["thanks"]); return

    # Quiet hours (ask/save via text – handled in on_text)
    if kind == "quiet":
        if parts[1] == "ask":
            sessions.setdefault(uid, {})["await_quiet"] = True
            await q.message.reply_text(T[lang]["ask_quiet"]); return

    # Episode save handled in Part 1 override too
    if kind == "ep" and parts[1] == "save":
        eid = parts[2]
        try: episode_set(eid, "status", "closed")
        except: pass
        await q.message.reply_text({"ru":"Эпизод сохранён ✅","uk":"Епізод збережено ✅","en":"Episode saved ✅","es":"Episodio guardado ✅"}[lang]); return

    # YouTube neck routine
    if kind == "yt" and parts[1] == "neck":
        txt = {
            "ru":"🧘 3–5 минут: мягкие круги плечами, наклоны головы, «подбор двойного подбородка», лёгкая растяжка трапеций. Без боли.",
            "uk":"🧘 3–5 хв: м’які кола плечима, нахили голови, «підбір підборіддя», легка розтяжка трапецій. Без болю.",
            "en":"🧘 3–5 min: shoulder rolls, gentle neck tilts, chin tucks, light upper-trap stretch. Pain-free.",
            "es":"🧘 3–5 min: círculos de hombros, inclinaciones de cuello, retracciones de mentón, estiramiento trapecio. Sin dolor.",
        }[lang]
        await q.message.reply_text(txt); return

# ---------- Text handler (autodetect + flows) ----------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    uid = update.effective_user.id
    raw = (update.message.text or "").strip()

    # 1) soft language update
    lang = _update_msg_lang(uid, raw)

    # 2) guard: awaiting special inputs
    s = sessions.setdefault(uid, {})

    # mini-intake free-write
    if s.get("mini_wait_key"):
        key = s.pop("mini_wait_key")
        mini_handle_choice(uid, key, raw)
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return

    # gm comment
    if s.get("await_gm_note"):
        s["await_gm_note"] = False
        daily_add(iso(utcnow()), uid, comment=raw)
        await update.message.reply_text(T[lang]["thanks"])
        await show_quickbar(context, update.effective_chat.id, lang, uid)
        return

    # cycle last date
    if s.get("await_cycle_last"):
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
            profiles_upsert(uid, {"cycle_enabled":"yes","cycle_last_date":raw})
            s["await_cycle_last"] = False
            s["await_cycle_len"] = True
            await update.message.reply_text(T[lang]["cycle_ask_len"])
        else:
            await update.message.reply_text({"ru":"Формат ГГГГ-ММ-ДД.","uk":"Формат РРРР-ММ-ДД.","en":"Use YYYY-MM-DD.","es":"Formato AAAA-MM-DD."}[lang])
        return
    if s.get("await_cycle_len"):
        try:
            n = int(re.sub(r"\D+","", raw)); n = max(24, min(40, n))
            profiles_upsert(uid, {"cycle_avg_len":str(n)})
            s["await_cycle_len"] = False
            await update.message.reply_text(T[lang]["cycle_saved"])
        except Exception:
            await update.message.reply_text({"ru":"Введите число (24–40).","uk":"Вкажіть число (24–40).","en":"Enter a number (24–40).","es":"Introduce un número (24–40)."}[lang])
        return

    # quiet hours
    if s.get("await_quiet"):
        q = _parse_quiet(raw)
        if q:
            profiles_upsert(uid, {"quiet_hours": q})
            s["await_quiet"] = False
            await update.message.reply_text(T[lang]["quiet_saved"].format(qh=q))
        else:
            await update.message.reply_text({"ru":"Формат ЧЧ:ММ-ЧЧ:ММ","uk":"Формат ГГ:ХХ-ГГ:ХХ","en":"Format HH:MM-HH:MM","es":"Formato HH:MM-HH:MM"}[lang])
        return

    # 3) learn habits from free text
    try: learn_from_text(uid, raw)
    except Exception: pass

    # 4) Health60 awaited symptom text
    if s.get("awaiting_h60_text"):
        s["awaiting_h60_text"] = False
        plan = health60_make_plan(lang, raw, profiles_get(uid) or {})
        pref = personalized_prefix(lang, profiles_get(uid) or {})
        msg = (pref + ("\n\n" if pref else "") + plan).strip()
        await update.message.reply_text(msg)
        # feedback buttons
        fbkb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["fb_good"], callback_data="fb|good"),
                                      InlineKeyboardButton(T[lang]["fb_bad"],  callback_data="fb|bad")],
                                     [InlineKeyboardButton(T[lang]["fb_free"], callback_data="fb|free")]])
        await update.message.reply_text(T[lang]["ask_fb"], reply_markup=fbkb)
        await show_quickbar(context, update.effective_chat.id, lang, uid)
        return

    # 5) Lightweight triage entry from symptom-like messages
    low = raw.lower()
    symptomish = any(k in low for k in [
        "болит","боль","тошно","рвет","рвота","кашель","температур","мигрень","голова","головная",
        "pain","ache","nausea","vomit","cough","fever","headache","migraine","dolor","fiebre","náusea"
    ])
    if symptomish and not s.get("tri",{}).get("step"):
        await _tri_start(update.effective_chat.id, uid, lang, context.bot)
        if profile_is_incomplete(profiles_get(uid) or {}):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                {"ru":"🧩 Заполнить профиль (40с)","uk":"🧩 Заповнити профіль (40с)","en":"🧩 Fill profile (40s)","es":"🧩 Completar perfil (40s)"}[lang],
                callback_data="intake:start")]])
            await update.message.reply_text({"ru":"Чтобы советы были точнее — можно пройти мини-опрос.",
                                             "uk":"Щоб поради були точніші — можна пройти міні-опитник.",
                                             "en":"For sharper tips, you can take a quick mini-intake.",
                                             "es":"Para consejos más precisos — haz el mini-intake."}[lang], reply_markup=kb)
        return

    # 6) Router (LLM/offline) fallback for general questions
    data = llm_router_answer(raw, lang, profiles_get(uid) or {})
    reply = (personalized_prefix(lang, profiles_get(uid) or "") + ("\n\n" if personalized_prefix(lang, profiles_get(uid) or "") else "") + data.get("assistant_reply","")).strip()
    await update.message.reply_text(reply)
    await show_quickbar(context, update.effective_chat.id, lang, uid)

# ---------- Feedback (via callback) ----------
async def cb_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q: return
    await q.answer()
    uid = q.from_user.id; lang = _lang_for_cb(uid)
    data = (q.data or "")
    parts = data.split("|")
    if parts[0] != "fb": return
    if parts[1] in {"good","bad"}:
        feedback_add(iso(utcnow()), uid, q.from_user.full_name, q.from_user.username or "", parts[1], "")
        await q.message.reply_text(T[lang]["fb_thanks"])
    elif parts[1] == "free":
        sessions.setdefault(uid, {})["await_free_fb"] = True
        await q.message.reply_text(T[lang]["fb_write"])

async def on_text_feedback_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    uid = update.effective_user.id
    if sessions.setdefault(uid, {}).get("await_free_fb"):
        lang = _user_lang(uid)
        sessions[uid]["await_free_fb"] = False
        feedback_add(iso(utcnow()), uid, update.effective_user.full_name, update.effective_user.username or "", "free", (update.message.text or "").strip())
        await update.message.reply_text(T[lang]["fb_thanks"])

# ---------- Handlers wiring ----------
def build_app() -> Application:
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("privacy",  cmd_privacy))
    app.add_handler(CommandHandler("pause",    cmd_pause))
    app.add_handler(CommandHandler("resume",   cmd_resume))
    app.add_handler(CommandHandler("delete_data", cmd_delete))
    app.add_handler(CommandHandler("profile",  cmd_profile))

    app.add_handler(CommandHandler("settz",    cmd_settz))
    app.add_handler(CommandHandler("checkin_on",  cmd_checkin_on))
    app.add_handler(CommandHandler("checkin_off", cmd_checkin_off))
    app.add_handler(CommandHandler("evening_on",  cmd_evening_on))
    app.add_handler(CommandHandler("evening_off", cmd_evening_off))
    app.add_handler(CommandHandler("quiet",    cmd_quiet))

    app.add_handler(CommandHandler("health60", cmd_health60))
    app.add_handler(CommandHandler("hydrate",  cmd_hydrate))
    app.add_handler(CommandHandler("skintip",  cmd_skintip))
    app.add_handler(CommandHandler("energy",   cmd_energy))
    app.add_handler(CommandHandler("cycle",    cmd_cycle))
    app.add_handler(CommandHandler("youth",    cmd_youth))

    app.add_handler(CommandHandler("ru", cmd_ru))
    app.add_handler(CommandHandler("en", cmd_en))
    app.add_handler(CommandHandler("uk", cmd_uk))
    app.add_handler(CommandHandler("es", cmd_es))

    # Callbacks
    app.add_handler(CallbackQueryHandler(cb_feedback, pattern=r"^fb\|"))
    app.add_handler(CallbackQueryHandler(cb_handler))  # catch-all extended (mini/menu/gm/tri/etc.)

    # Text handlers — order matters
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_feedback_free))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app

# ---------- Entry ----------
def main():
    app = build_app()
    logging.info("==> Running 'python main.py'")
    app.run_polling()
