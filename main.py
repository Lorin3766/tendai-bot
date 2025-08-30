# -*- coding: utf-8 -*-
import os, re, json, uuid, logging
from datetime import datetime, timedelta, timezone, time as dtime
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
# импортируем только регистрацию (кнопку дадим сами через InlineKeyboardButton)
from intake_pro import register_intake_pro

# ---------- Google Sheets (robust + memory fallback) ----------
import gspread
from gspread.exceptions import SpreadsheetNotFound
import gspread.utils as gsu
from oauth2client.service_account import ServiceAccountCredentials


# ---------------- Boot & Config ----------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
DetectorFactory.seed = 0

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# По умолчанию ставим gpt-4o (а не -mini)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

SHEET_NAME = os.getenv("SHEET_NAME", "TendAI Sheets")
SHEET_ID = os.getenv("SHEET_ID", "")
ALLOW_CREATE_SHEET = os.getenv("ALLOW_CREATE_SHEET", "0") == "1"
DEFAULT_CHECKIN_LOCAL = "08:30"

oai: Optional[OpenAI] = None
try:
    if OPENAI_API_KEY:
        os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
    oai = OpenAI()
except Exception as e:
    logging.error(f"OpenAI init error: {e}")
    oai = None


# ---------------- i18n ----------------
SUPPORTED = {"ru", "en", "uk", "es"}  # добавили es

def norm_lang(code: Optional[str]) -> str:
    if not code:
        return "en"
    c = code.split("-")[0].lower()
    return c if c in SUPPORTED else "en"

T = {
    "en": {
        "welcome": "Hi! I’m TendAI — your health & longevity assistant.\nIf you like, we can personalize in 40–60s, or just open the menu and talk.",
        "help": "Short checkups, 24–48h plans, reminders, daily check-ins.\nCommands: /menu /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /ru /uk /en /es",
        "privacy": "TendAI is not a medical service and can’t replace a doctor. We store minimal data for reminders. /delete_data to erase.",
        "ask_personalize": "To tailor advice better, would you like a short 6-question intake (40–60s)?",
        "start_where":"Where do you want to start? You can pick a topic above or just write a few words.",
        "menu_btn":"📋 Open menu",
        "menu_opened":"Opened the menu.",
        "feedback_q":"Was this helpful?",
        "fb_thanks":"Thanks for your feedback! ✅",
        "fb_write":"Write a short feedback message:",
        "yes":"Yes","no":"No",
        "thanks":"Got it 🙌",
        "unknown":"I need a bit more info to be precise.",
        # Health60
        "h60_btn": "Health in 60 seconds",
        "h60_intro": "Briefly write what bothers you (e.g., “headache”, “fatigue”, “stomach pain”). I’ll give 3 key tips in 60 seconds.",
        "h60_t1": "Possible causes",
        "h60_t2": "Do now (next 24–48h)",
        "h60_t3": "When to see a doctor",
        "h60_serious": "Serious to rule out",
        # Plan & actions
        "plan_header":"Your 24–48h plan:",
        "plan_accept":"Will you try this today?",
        "accept_opts":["✅ Yes","🔁 Later","✖️ No"],
        "remind_when":"When shall I check on you?",
        "act_rem_4h":"⏰ In 4h",
        "act_rem_eve":"⏰ This evening",
        "act_rem_morn":"⏰ Tomorrow morning",
        "px":"Considering your profile: {sex}, {age}y; goal — {goal}.",
        "back":"◀ Back",
        "exit":"Exit",
        "footer_personalize":"🧩 Personalize (6 Qs)",
        "footer_menu":"📋 Menu",
    },
    "ru": {
        "welcome":"Привет! Я TendAI — ассистент здоровья и долголетия.\nМожно пройти персонализацию за 40–60 сек или просто открыть меню и поговорить.",
        "help":"Короткие проверки, план на 24–48 ч, напоминания, ежедневные чек-ины.\nКоманды: /menu /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +3 /health60 /ru /uk /en /es",
        "privacy":"TendAI не заменяет врача. Храним минимум данных для напоминаний. /delete_data — удалить.",
        "ask_personalize":"Чтобы советы не промахивались — хотите короткий опрос из 6 пунктов (40–60 секунд)?",
        "start_where":"С чего начнём? Можно выбрать раздел выше или просто написать пару слов.",
        "menu_btn":"📋 Открыть меню",
        "menu_opened":"Открыл меню.",
        "feedback_q":"Это было полезно?",
        "fb_thanks":"Спасибо за отзыв! ✅",
        "fb_write":"Напишите короткий комментарий:",
        "yes":"Да","no":"Нет",
        "thanks":"Принято 🙌",
        "unknown":"Нужно чуть больше деталей, чтобы подсказать точнее.",
        # Health60
        "h60_btn": "Здоровье за 60 секунд",
        "h60_intro": "Коротко напишите, что беспокоит (например: «болит голова», «усталость», «боль в животе»). Дам 3 ключевых шага за 60 секунд.",
        "h60_t1": "Возможные причины",
        "h60_t2": "Что сделать сейчас (24–48 ч)",
        "h60_t3": "Когда обратиться к врачу",
        "h60_serious": "Что серьёзное исключить",
        # Plan & actions
        "plan_header":"Ваш план на 24–48 часов:",
        "plan_accept":"Готовы попробовать сегодня?",
        "accept_opts":["✅ Да","🔁 Позже","✖️ Нет"],
        "remind_when":"Когда удобно напомнить и спросить самочувствие?",
        "act_rem_4h":"⏰ Через 4 часа",
        "act_rem_eve":"⏰ Сегодня вечером",
        "act_rem_morn":"⏰ Завтра утром",
        "px":"С учётом профиля: {sex}, {age} лет; цель — {goal}.",
        "back":"◀ Назад",
        "exit":"Выйти",
        "footer_personalize":"🧩 Персонализировать (6 вопросов)",
        "footer_menu":"📋 Меню",
    },
    "uk": {
        "welcome":"Привіт! Я TendAI — асистент здоров’я та довголіття.\nМожемо пройти персоналізацію за 40–60 с або просто відкрити меню й поговорити.",
        "help":"Короткі перевірки, план на 24–48 год, нагадування, щоденні чек-іни.\nКоманди: /menu /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /ru /uk /en /es",
        "privacy":"TendAI не замінює лікаря. Зберігаємо мінімум даних для нагадувань. /delete_data — видалити.",
        "ask_personalize":"Щоб поради були точніші — бажаєте короткий опитник із 6 пунктів (40–60 с)?",
        "start_where":"З чого почнемо? Можна обрати розділ вище або просто написати кілька слів.",
        "menu_btn":"📋 Відкрити меню",
        "menu_opened":"Відкрив меню.",
        "feedback_q":"Це було корисно?",
        "fb_thanks":"Дякую за відгук! ✅",
        "fb_write":"Напишіть короткий коментар:",
        "yes":"Так","no":"Ні",
        "thanks":"Прийнято 🙌",
        "unknown":"Потрібно трохи більше деталей, щоб підказати точніше.",
        "h60_btn": "Здоров’я за 60 секунд",
        "h60_intro": "Коротко напишіть, що турбує (напр.: «болить голова», «втома»). Дам 3 кроки за 60 секунд.",
        "h60_t1": "Можливі причини",
        "h60_t2": "Що зробити зараз (24–48 год)",
        "h60_t3": "Коли звернутися до лікаря",
        "h60_serious": "Що серйозне виключити",
        "plan_header":"Ваш план на 24–48 год:",
        "plan_accept":"Готові спробувати сьогодні?",
        "accept_opts":["✅ Так","🔁 Пізніше","✖️ Ні"],
        "remind_when":"Коли зручно нагадати та спитати самопочуття?",
        "act_rem_4h":"⏰ Через 4 год",
        "act_rem_eve":"⏰ Сьогодні ввечері",
        "act_rem_morn":"⏰ Завтра зранку",
        "px":"З урахуванням профілю: {sex}, {age} р.; мета — {goal}.",
        "back":"◀ Назад",
        "exit":"Вийти",
        "footer_personalize":"🧩 Персоналізувати (6 питань)",
        "footer_menu":"📋 Меню",
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

# ===== ONBOARDING GATE (Скрыть меню до опроса, но опрос опционален) =====
GATE_FLAG_KEY = "menu_unlocked"

def _is_menu_unlocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get(GATE_FLAG_KEY):
        return True
    prof = profiles_get(update.effective_user.id) or {}
    return not profile_is_incomplete(prof)

async def gate_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шторка: предложить PRO-опрос (по желанию) или открыть меню."""
    lang = context.user_data.get("lang", "en")
    kb = [
        [InlineKeyboardButton("🧩 Пройти опрос (40–60 сек)" if lang!="en" else "🧩 Take the 40–60s intake", callback_data="intake:start")],
        [InlineKeyboardButton("➡️ Позже — показать меню" if lang!="en" else "➡️ Later — open menu", callback_data="gate:skip")],
    ]
    text = (
        "Чтобы советы были точнее, можно пройти короткий опрос. Можно пропустить и сделать позже."
        if lang!="en" else
        "To personalize answers, you can take a short intake. You can skip and do it later."
    )
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
            await context.application.bot.send_message(q.message.chat_id, "/menu")

# ===== /ONBOARDING GATE =====

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

# === Сохраняем gspread client и id таблицы для register_intake_pro ===
GSPREAD_CLIENT: Optional[gspread.client.Client] = None
SPREADSHEET_ID_FOR_INTAKE: str = ""

def _sheets_init():
    global SHEETS_ENABLED, ss, ws_feedback, ws_users, ws_profiles, ws_episodes, ws_reminders, ws_daily
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
                ws = ss.add_worksheet(title=title, rows=1000, cols=max(20, len(headers)))
                ws.append_row(headers)
            if not ws.get_all_values():
                ws.append_row(headers)
            return ws

        ws_feedback = _ensure_ws("Feedback", ["timestamp","user_id","name","username","rating","comment"])
        ws_users = _ensure_ws("Users", ["user_id","username","lang","consent","tz_offset","checkin_hour","paused",
                                        "last_fb_at"])
        ws_profiles = _ensure_ws("Profiles", ["user_id","sex","age","goal","conditions","meds","allergies",
                                              "sleep","activity","diet","notes","updated_at"])
        ws_episodes = _ensure_ws("Episodes", ["episode_id","user_id","topic","started_at","baseline_severity","red_flags",
                                              "plan_accepted","target","reminder_at","next_checkin_at","status","last_update","notes"])
        ws_reminders= _ensure_ws("Reminders",["id","user_id","text","when_utc","created_at","status"])
        ws_daily = _ensure_ws("DailyCheckins",["timestamp","user_id","mood","comment"])
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
        "paused": "no",
        "last_fb_at": ""
    }

    if SHEETS_ENABLED:
        vals = ws_users.get_all_records()
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                ws_users.update(f"A{i}:H{i}", [[base[k] for k in _headers(ws_users)]])
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
                if field in hdr:
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
# ------------- Personalization helpers -------------
def personalized_prefix(lang: str, profile: dict) -> str:
    sex = profile.get("sex") or ""
    age = profile.get("age") or ""
    goal = profile.get("goal") or ""
    if not (sex or age or goal):
        return ""
    return T[lang]["px"].format(sex=sex or "—", age=age or "—", goal=goal or "—")

# ----- Backfill triage labels if missing (safety) -----
def _ensure_triage_labels():
    defaults = {
        "triage_pain_q1": {"en":"Where does it hurt?","ru":"Где болит?","uk":"Де болить?","es":"¿Dónde duele?"},
        "triage_pain_q2": {"en":"What kind of pain?","ru":"Какой характер боли?","uk":"Який характер болю?","es":"¿Qué tipo de dolor?"},
        "triage_pain_q3": {"en":"How long has it lasted?","ru":"Как долго длится?","uk":"Як довго триває?","es":"¿Cuánto dura?"},
        "triage_pain_q4": {"en":"Rate the pain (0–10):","ru":"Оцените боль (0–10):","uk":"Оцініть біль (0–10):","es":"Valora el dolor (0–10):"},
        "triage_pain_q5": {"en":"Any of these now?","ru":"Есть ли что-то из этого сейчас?","uk":"Є щось із цього зараз?","es":"¿Alguno de estos ahora?"},
        "triage_pain_q1_opts": {
            "en":["Head","Throat","Back","Belly","Other"],
            "ru":["Голова","Горло","Спина","Живот","Другое"],
            "uk":["Голова","Горло","Спина","Живіт","Інше"],
            "es":["Head","Throat","Back","Belly","Other"],
        },
        "triage_pain_q2_opts": {
            "en":["Dull","Sharp","Pulsating","Pressing"],
            "ru":["Тупая","Острая","Пульсирующая","Давящая"],
            "uk":["Тупий","Гострий","Пульсуючий","Тиснучий"],
            "es":["Dull","Sharp","Pulsating","Pressing"],
        },
        "triage_pain_q3_opts": {
            "en":["<3h","3–24h",">1 day",">1 week"],
            "ru":["<3ч","3–24ч",">1 дня",">1 недели"],
            "uk":["<3год","3–24год",">1 дня",">1 тижня"],
            "es":["<3h","3–24h",">1 day",">1 week"],
        },
        "triage_pain_q5_opts": {
            "en":["High fever","Vomiting","Weakness/numbness","Speech/vision problems","Trauma","None"],
            "ru":["Высокая температура","Рвота","Слабость/онемение","Нарушение речи/зрения","Травма","Нет"],
            "uk":["Висока температура","Блювання","Слабкість/оніміння","Проблеми з мовою/зором","Травма","Немає"],
            "es":["High fever","Vomiting","Weakness/numbness","Speech/vision problems","Trauma","None"],
        },
        "daily_gm": {
            "en":"Good morning! Quick daily check-in:",
            "ru":"Доброе утро! Быстрый чек-ин:",
            "uk":"Доброго ранку! Швидкий чек-ін:",
            "es":"¡Buenos días! Chequeo rápido:",
        },
        "mood_good":{"en":"😃 Good","ru":"😃 Хорошо","uk":"😃 Добре","es":"😃 Bien"},
        "mood_ok":{"en":"😐 Okay","ru":"😐 Нормально","uk":"😐 Нормально","es":"😐 Normal"},
        "mood_bad":{"en":"😣 Poor","ru":"😣 Плохо","uk":"😣 Погано","es":"😣 Mal"},
        "mood_note":{"en":"✍️ Comment","ru":"✍️ Комментарий","uk":"✍️ Коментар","es":"✍️ Nota"},
        "checkin_ping":{
            "en":"Quick check-in: how is it now (0–10)?",
            "ru":"Коротко: как сейчас по шкале 0–10?",
            "uk":"Коротко: як зараз за шкалою 0–10?",
            "es":"Revisión rápida: ¿cómo está ahora (0–10)?",
        },
        "act_er":{
            "en":"🚑 Emergency info","ru":"🚑 Когда срочно в скорую","uk":"🚑 Коли терміново в швидку","es":"🚑 Emergencia",
        },
    }
    for lang in T.keys():
        for k,v in defaults.items():
            if k not in T[lang]:
                T[lang][k] = v.get(lang, list(v.values())[0] if isinstance(v,dict) else v)

_ensure_triage_labels()

# ------------- Plans & serious -------------
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
    return core + extra + [T[lang]["act_er"]]

SERIOUS_KWS = {
    "diabetes": ["diabetes","диабет","сахарный","цукров","hba1c","гликированный","глюкоза"],
    "hepatitis": ["hepatitis","гепатит","печень hbs","hcv","alt","ast"],
    "cancer": ["cancer","рак","онко","опухол","tumor","пухлина"],
    "tb": ["tuberculosis","tb","туберкул","туберкульоз"],
}

def detect_serious(text: str) -> Optional[str]:
    low = (text or "").lower()
    for cond, kws in SERIOUS_KWS.items():
        if any(k in low for k in kws):
            return cond
    return None

def serious_plan(lang: str, cond: str, profile: dict) -> List[str]:
    age = int(re.search(r"\d+", str(profile.get("age") or "0")).group(0)) if re.search(r"\d+", str(profile.get("age") or "")) else 0
    if cond=="diabetes":
        base = {
            "ru":[
                "Подозрение на диабет/контроль: анализы — глюкоза натощак, HbA1c, липидограмма, креатинин.",
                "Врач: эндокринолог в течение 1–2 недель.",
                "Срочно: спутанность, сильная жажда/мочеиспускание + рвота, глюкоза > 16 ммоль/л — неотложка.",
            ],
            "uk":[
                "Підозра на діабет/контроль: глюкоза натще, HbA1c, ліпідограма, креатинін.",
                "Лікар: ендокринолог упродовж 1–2 тижнів.",
                "Терміново: сплутаність, сильна спрага/сечовиділення + блювання, глюкоза > 16 ммоль/л — невідкладна.",
            ],
            "en":[
                "Suspected diabetes/control: labs — fasting glucose, HbA1c, lipid panel, creatinine.",
                "Doctor: endocrinologist within 1–2 weeks.",
                "Urgent: confusion, polyuria/polydipsia with vomiting, glucose > 300 mg/dL — emergency.",
            ],
            "es":[
                "Sospecha de diabetes/control: glucosa en ayunas, HbA1c, perfil lipídico, creatinina.",
                "Médico: endocrinólogo en 1–2 semanas.",
                "Urgente: confusión, poliuria/polidipsia con vómitos, glucosa > 300 mg/dL — emergencia.",
            ],
        }[lang]
        if age>=40:
            base += [{"ru":f"Возраст {age}+: рекомендуем скрининг глаз и почек.",
                      "uk":f"Вік {age}+: рекомендовано скринінг очей і нирок.",
                      "en":f"Age {age}+: screen for eyes and kidneys.",
                      "es":f"Edad {age}+: cribado de ojos y riñón."}[lang]]
        return base + [T[lang]["act_er"]]
    if cond=="hepatitis":
        return {
            "ru":[ "Возможен гепатит: ALT/AST, билирубин, HBsAg, анти-HCV.",
                   "Врач: гастроэнтеролог/инфекционист в 1–2 недели.",
                   "Срочно: желтуха, тёмная моча, спутанность — неотложка."],
            "uk":[ "Ймовірний гепатит: ALT/AST, білірубін, HBsAg, anti-HCV.",
                   "Лікар: гастроентеролог/інфекціоніст за 1–2 тижні.",
                   "Терміново: жовтяниця, темна сеча, сплутаність — невідкладна."],
            "en":[ "Possible hepatitis: ALT/AST, bilirubin, HBsAg, anti-HCV.",
                   "Doctor: GI/hepatology or ID in 1–2 weeks.",
                   "Urgent: jaundice, dark urine, confusion — emergency."],
            "es":[ "Posible hepatitis: ALT/AST, bilirrubina, HBsAg, anti-HCV.",
                   "Médico: gastro/hepatología o infecciosas en 1–2 semanas.",
                   "Urgente: ictericia, orina oscura, confusión — emergencia."],
        }[lang] + [T[lang]["act_er"]]
    if cond=="cancer":
        return {
            "ru":[ "Онкотема: консультация онколога как можно скорее (1–2 недели).",
                   "Подготовьте выписки, результаты КТ/МРТ/биопсии при наличии.",
                   "Срочно: кровотечение, нарастающая одышка/боль, резкая слабость — неотложка."],
            "uk":[ "Онкотема: онколог якнайшвидше (1–2 тижні).",
                   "Підготуйте виписки та результати КТ/МРТ/біопсії.",
                   "Терміново: кровотеча, задишка/біль, різка слабкість — невідкладна."],
            "en":[ "Oncology topic: see an oncologist asap (1–2 weeks).",
                   "Prepare records and any CT/MRI/biopsy results.",
                   "Urgent: bleeding, worsening dyspnea/pain, profound weakness — emergency."],
            "es":[ "Oncología: oncólogo lo antes posible (1–2 semanas).",
                   "Prepare informes y TC/RM/biopsia.",
                   "Urgente: sangrado, disnea/dolor en aumento, gran debilidad — emergencia."],
        }[lang] + [T[lang]["act_er"]]
    if cond=="tb":
        return {
            "ru":[ "Подозрение на ТБ: рентген/КТ грудной клетки, мокрота (микроскопия/ПЦР).",
                   "Врач: фтизиатр.",
                   "Срочно: кровохарканье, высокая температура с одышкой — неотложка."],
            "uk":[ "Підозра на ТБ: рентген/КТ грудної клітки, мокротиння (мікроскопія/ПЛР).",
                   "Лікар: фтизіатр.",
                   "Терміново: кровохаркання, висока температура з задишкою — невідкладна."],
            "en":[ "Suspected TB: chest X-ray/CT and sputum tests.",
                   "Doctor: TB specialist.",
                   "Urgent: hemoptysis, high fever with breathlessness — emergency."],
            "es":[ "Sospecha de TB: rayos X/TC y esputo.",
                   "Médico: especialista en TB.",
                   "Urgente: hemoptisis, fiebre alta con disnea — emergencia."],
        }[lang] + [T[lang]["act_er"]]
    return [T[lang]["unknown"]]

# ------------- Inline keyboards -------------
def inline_numbers_0_10() -> InlineKeyboardMarkup:
    rows = []
    row1 = [InlineKeyboardButton(str(n), callback_data=f"num|{n}") for n in range(0, 6)]
    row2 = [InlineKeyboardButton(str(n), callback_data=f"num|{n}") for n in range(6, 11)]
    rows.append(row1)
    rows.append(row2)
    rows.append([InlineKeyboardButton("◀", callback_data="pain|exit")])
    return InlineKeyboardMarkup(rows)

def inline_list(options: List[str], prefix: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for opt in options:
        row.append(InlineKeyboardButton(opt, callback_data=f"{prefix}|{opt}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def inline_accept(lang: str) -> InlineKeyboardMarkup:
    labels = T[lang]["accept_opts"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(labels[0], callback_data="acc|yes"),
         InlineKeyboardButton(labels[1], callback_data="acc|later"),
         InlineKeyboardButton(labels[2], callback_data="acc|no")]
    ])

def inline_remind(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["act_rem_4h"], callback_data="rem|4h")],
        [InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="rem|evening")],
        [InlineKeyboardButton(T[lang]["act_rem_morn"], callback_data="rem|morning")]
    ])

def inline_feedback_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👍", callback_data="fb|up"),
         InlineKeyboardButton("👎",  callback_data="fb|down")],
        [InlineKeyboardButton("📝 " + T[lang]["fb_write"], callback_data="fb|text")]
    ])

def inline_actions(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["act_rem_4h"],  callback_data="act|rem|4h"),
         InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="act|rem|evening"),
         InlineKeyboardButton(T[lang]["act_rem_morn"],callback_data="act|rem|morning")],
        [InlineKeyboardButton(T[lang]["h60_btn"], callback_data="act|h60")],
        [InlineKeyboardButton("🧘 5-min neck", callback_data="act|ex|neck")],
        [InlineKeyboardButton("🧪 Find a lab" if lang=="en" else "🧪 Найти лабораторию" if lang=="ru" else "🧪 Знайти лабораторію" if lang=="uk" else "🧪 Lab", callback_data="act|lab")],
        [InlineKeyboardButton(T[lang]["act_er"], callback_data="act|er")]
    ])

def inline_topic_kb(lang: str) -> InlineKeyboardMarkup:
    label = {"ru":"🧩 Опрос 6 пунктов","uk":"🧩 Опитник (6)","en":"🧩 Intake (6 Qs)","es":"🧩 Intake (6)"}[lang]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🩺 Pain", callback_data="topic|pain"),
         InlineKeyboardButton("😴 Sleep", callback_data="topic|sleep"),
         InlineKeyboardButton("🍎 Nutrition", callback_data="topic|nutrition")],
        [InlineKeyboardButton("🧪 Labs", callback_data="topic|labs"),
         InlineKeyboardButton("🔁 Habits", callback_data="topic|habits"),
         InlineKeyboardButton("🧬 Longevity", callback_data="topic|longevity")],
        [InlineKeyboardButton("👤 Profile", callback_data="topic|profile")],
        [InlineKeyboardButton(label, callback_data="intake:start")]  # ключевое — запускает плагин
    ])

# ------------- Feedback timing -------------
def _maybe_ask_feedback(update_or_msg, lang: str, uid: int):
    """Спрашиваем отзыв не назойливо — не чаще, чем раз в 3 часа и только после полезного ответа/плана."""
    u = users_get(uid)
    last = u.get("last_fb_at") or ""
    ok_to_ask = True
    if last:
        try:
            dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S%z")
            ok_to_ask = (utcnow() - dt) > timedelta(hours=3)
        except:
            ok_to_ask = True
    if ok_to_ask:
        users_set(uid, "last_fb_at", iso(utcnow()))
        try:
            update_or_msg.reply_text(T[lang]["feedback_q"], reply_markup=inline_feedback_kb(lang))
        except Exception:
            pass

# ------------- Intake Pro completion callback -------------
async def _ipro_save_to_sheets_and_open_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, profile: dict):
    uid = update.effective_user.id
    # Сохраняем ключевые поля в Profiles
    profiles_upsert(uid, {
        "sex": profile.get("sex") or "",
        "age": profile.get("age") or "",
        "goal": profile.get("goal") or "",
        "conditions": ", ".join(sorted(profile.get("chronic", []))) if isinstance(profile.get("chronic"), (set,list)) else (profile.get("chronic") or ""),
        "meds": profile.get("meds") or "",
        "activity": profile.get("hab_activity") or "",
        "sleep": profile.get("hab_sleep") or "",
        "diet": profile.get("diet") or "",
        "notes": ", ".join(sorted(profile.get("complaints", []))) if isinstance(profile.get("complaints"), (set,list)) else (profile.get("complaints") or ""),
    })
    # Разблокируем меню
    context.user_data[GATE_FLAG_KEY] = True
    await render_main_menu(update, context)

# ------------- Commands -------------
async def post_init(app):
    me = await app.bot.get_me()
    logging.info(f"BOT READY: @{me.username} (id={me.id})")

async def render_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None) or "en")
    try:
        await update.effective_chat.send_message(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
    except Exception as e:
        logging.error(f"render_main_menu error: {e}")

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None) or "en")
    await update.message.reply_text(T[lang]["menu_opened"], reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = norm_lang(getattr(user, "language_code", None))
    users_upsert(user.id, user.username or "", lang)
    sessions.setdefault(user.id, {})["last_user_text"] = "/start"

    # Приветствие + меню
    await update.message.reply_text(T[lang]["welcome"], reply_markup=ReplyKeyboardRemove())
    await render_main_menu(update, context)

    # Предложить персонализацию (не навязываем)
    prof = profiles_get(user.id)
    if profile_is_incomplete(prof) and not context.user_data.get(GATE_FLAG_KEY):
        await gate_show(update, context)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await update.message.reply_text(T[lang]["help"])

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await update.message.reply_text(T[lang]["privacy"])

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "yes")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text("⏸️ " + ({"ru":"Уведомления на паузе.","uk":"Сповіщення на паузі.","en":"Notifications paused.","es":"Notificaciones en pausa."}[lang]))

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "no")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text("▶️ " + ({"ru":"Уведомления снова включены.","uk":"Сповіщення знову увімкнені.","en":"Notifications resumed.","es":"Notificaciones reanudadas."}[lang]))

async def cmd_delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if SHEETS_ENABLED:
        vals = ws_users.get_all_values()
        for i in range(2, len(vals)+1):
            if ws_users.cell(i,1).value == str(uid):
                ws_users.delete_rows(i); break
    else:
        MEM_USERS.pop(uid, None); MEM_PROFILES.pop(uid, None)
        global MEM_EPISODES, MEM_REMINDERS, MEM_DAILY
        MEM_EPISODES = [r for r in MEM_EPISODES if r["user_id"]!=str(uid)]
        MEM_REMINDERS = [r for r in MEM_REMINDERS if r["user_id"]!=str(uid)]
        MEM_DAILY = [r for r in MEM_DAILY if r["user_id"]!=str(uid)]
    lang = norm_lang(getattr(update.effective_user,"language_code",None) or "en")
    await update.message.reply_text({"ru":"Все данные удалены. /start — начать заново.",
                                     "uk":"Усі дані видалено. /start — почати знову.",
                                     "en":"All data deleted. Use /start to begin again.",
                                     "es":"Datos eliminados. Usa /start para empezar de nuevo."}[lang],
                                    reply_markup=ReplyKeyboardRemove())

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
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None) or "en")
    sessions.setdefault(uid, {})["awaiting_h60"] = True
    await update.message.reply_text(T[lang]["h60_intro"])

async def cmd_intake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None) or "en")
    txt  = {"ru":"🧩 PRO-опрос: 6 ключевых вопросов. Готовы начать?",
            "uk":"🧩 PRO-опитник: 6 ключових питань. Починаємо?",
            "en":"🧩 PRO intake: 6 quick questions. Ready?",
            "es":"🧩 PRO intake: 6 quick questions. Ready?"}[lang]
    start_label = {"ru":"▶️ Начать","uk":"▶️ Почати","en":"▶️ Start","es":"▶️ Start"}[lang]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(start_label, callback_data="intake:start")]])
    await update.message.reply_text(txt, reply_markup=kb)

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # перенаправляем на PRO-интейк
    await cmd_intake(update, context)

# Быстрые смены языка
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

# ------------- Callback handler -------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = (q.data or ""); uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    chat_id = q.message.chat.id

    # GATE
    if data.startswith("gate:"):
        await gate_cb(update, context); return

    # Профиль (переадресуем на PRO)
    if data == "topic|profile":
        # показываем кнопку запуска PRO-опросника
        await q.message.reply_text(T[lang]["ask_personalize"] if "ask_personalize" in T[lang] else "Run intake?", 
                                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ " + ({"ru":"Начать","uk":"Почати","en":"Start","es":"Start"}[lang]), callback_data="intake:start")]]))
        return

    # Daily mood
    if data.startswith("mood|"):
        mood = data.split("|",1)[1]
        if mood=="note":
            sessions.setdefault(uid,{})["awaiting_daily_comment"] = True
            await q.message.reply_text({"ru":"Короткий комментарий:",
                                        "uk":"Короткий коментар:",
                                        "en":"Short note:",
                                        "es":"Nota corta:"}[lang]); return
        daily_add(iso(utcnow()), uid, mood, "")
        await q.message.reply_text(T[lang]["thanks"]); return

    # Темы
    if data.startswith("topic|"):
        topic = data.split("|",1)[1]
        if topic=="pain":
            sessions[uid] = {"topic":"pain","step":1,"answers":{}}
            kb = _kb_for_code(lang, "painloc")
            await q.message.reply_text(T[lang]["triage_pain_q1"], reply_markup=kb); return

        # Остальные темы — через роутер
        last = sessions.get(uid,{}).get("last_user_text","")
        prof = profiles_get(uid)
        prompt = f"topic:{topic}\nlast_user: {last or '—'}"
        data_llm = llm_router_answer(prompt, lang, prof)
        prefix = personalized_prefix(lang, prof)
        reply = ((prefix + "\n") if prefix else "") + (data_llm.get("assistant_reply") or T[lang]["unknown"])
        await q.message.reply_text(reply, reply_markup=inline_actions(lang))
        _maybe_ask_feedback(q.message, lang, uid)
        return

    # Pain triage buttons
    s = sessions.setdefault(uid, {})
    if data == "pain|exit":
        sessions.pop(uid, None)
        await q.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
        return

    if data.startswith("painloc|"):
        s.update({"topic":"pain","step":2,"answers":{"loc": data.split("|",1)[1]}})
        await q.message.reply_text(T[lang]["triage_pain_q2"], reply_markup=_kb_for_code(lang,"painkind")); return

    if data.startswith("painkind|"):
        s.setdefault("answers",{})["kind"] = data.split("|",1)[1]; s["step"]=3
        await q.message.reply_text(T[lang]["triage_pain_q3"], reply_markup=_kb_for_code(lang,"paindur")); return

    if data.startswith("paindur|"):
        s.setdefault("answers",{})["duration"] = data.split("|",1)[1]; s["step"]=4
        await q.message.reply_text(T[lang]["triage_pain_q4"], reply_markup=_kb_for_code(lang,"num")); return

    if data.startswith("num|"):
        if s.get("topic")=="pain" and s.get("step")==4:
            sev = int(data.split("|",1)[1])
            s.setdefault("answers",{})["severity"] = sev; s["step"]=5
            await q.message.reply_text(T[lang]["triage_pain_q5"], reply_markup=_kb_for_code(lang,"painrf")); return

    if data.startswith("painrf|"):
        red = data.split("|",1)[1]
        s.setdefault("answers",{})["red"] = red
        sev = int(s["answers"].get("severity",5))
        eid = episode_create(uid, "pain", sev, red); s["episode_id"] = eid
        plan_lines = pain_plan(lang, [red], profiles_get(uid))
        prefix = personalized_prefix(lang, profiles_get(uid))
        text_plan = (prefix + "\n" if prefix else "") + f"{T[lang]['plan_header']}\n" + "\n".join(plan_lines)
        await q.message.reply_text(text_plan)
        await q.message.reply_text(T[lang]["plan_accept"], reply_markup=inline_accept(lang))
        _maybe_ask_feedback(q.message, lang, uid)
        s["step"] = 6; return

    if data.startswith("acc|"):
        accepted = "1" if data.endswith("|yes") else "0"
        if s.get("episode_id"):
            episode_set(s["episode_id"], "plan_accepted", accepted)
        await q.message.reply_text(T[lang]["remind_when"], reply_markup=inline_remind(lang))
        s["step"] = 7; return

    if data.startswith("rem|"):
        choice = data.split("|",1)[1]
        delay = {"4h":4, "evening":6, "morning":16}.get(choice)
        if delay and s.get("episode_id"):
            next_time = utcnow() + timedelta(hours=delay)
            episode_set(s["episode_id"], "next_checkin_at", iso(next_time))
            if _has_jq_ctx(context):
                context.application.job_queue.run_once(job_checkin_episode, when=delay*3600,
                                                       data={"user_id":uid,"episode_id":s["episode_id"]})
            else:
                logging.warning("JobQueue not available – episode follow-up not scheduled.")
        await q.message.reply_text(T[lang]["thanks"], reply_markup=inline_topic_kb(lang))
        sessions.pop(uid, None); return

    # smart follow-ups (actions)
    if data.startswith("act|"):
        parts = data.split("|")
        kind = parts[1]
        if kind=="h60":
            sessions.setdefault(uid,{})["awaiting_h60"] = True
            await q.message.reply_text(T[lang]["h60_intro"])
            return
        if kind=="rem":
            key = parts[2]
            hours = {"4h":4, "evening":6, "morning":16}.get(key,4)
            when_ = utcnow() + timedelta(hours=hours)
            rid = reminder_add(uid, T[lang]["thanks"], when_)
            if _has_jq_ctx(context):
                context.application.job_queue.run_once(job_oneoff_reminder, when=hours*3600,
                                                       data={"user_id":uid,"reminder_id":rid})
            else:
                logging.warning("JobQueue not available – one-off reminder not scheduled.")
            await q.message.reply_text(T[lang]["thanks"]); return
        if kind=="ex":
            txt = {
                "ru":"🧘 5 минут шея: 1) медленные наклоны вперёд/назад ×5; 2) повороты в стороны ×5; 3) полукруги подбородком ×5; 4) лёгкая растяжка трапеций 2×20 сек.",
                "uk":"🧘 5 хв шия: 1) повільні нахили вперед/назад ×5; 2) повороти в сторони ×5; 3) півкола підборіддям ×5; 4) легка розтяжка трапецій 2×20 с.",
                "en":"🧘 5-min neck: 1) slow flex/extend ×5; 2) rotations left/right ×5; 3) chin semicircles ×5; 4) gentle upper-trap stretch 2×20s.",
                "es":"🧘 Cuello 5 min: 1) flex/ext lenta ×5; 2) giros izq/der ×5; 3) semicírculos mentón ×5; 4) estiramiento trapecio sup. 2×20s."
            }[lang]
            await q.message.reply_text(txt); return
        if kind=="lab":
            sessions.setdefault(uid,{})["awaiting_city"] = True
            await q.message.reply_text({"ru":"Напишите город/район, чтобы подсказать лабораторию (текстом).",
                                        "uk":"Напишіть місто/район, щоб порадити лабораторію (текстом).",
                                        "en":"Type your city/area so I can suggest a lab (text only).",
                                        "es":"Escribe tu ciudad/zona para sugerir laboratorio."}[lang]); return
        if kind=="er":
            await q.message.reply_text({
                "ru":"Если нарастает, сильная одышка, боль в груди, спутанность, стойкая высокая температура — как можно скорее к неотложке/скорой.",
                "uk":"Якщо посилюється, сильна задишка, біль у грудях, сплутаність, тривала висока температура — якнайшвидше до невідкладної/швидкої.",
                "en":"If worsening, severe shortness of breath, chest pain, confusion, or persistent high fever — seek urgent care/emergency.",
                "es":"Si empeora, disnea intensa, dolor torácico, confusión o fiebre alta persistente — acude a urgencias."
            }[lang]); return

    # --- Feedback buttons ---
    if data.startswith("fb|"):
        sub = data.split("|",1)[1]
        if sub == "up":
            feedback_add(iso(utcnow()), uid, "feedback_yes", q.from_user.username, 1, "")
            await q.message.reply_text(T[lang]["fb_thanks"])
            return
        if sub == "down":
            feedback_add(iso(utcnow()), uid, "feedback_no", q.from_user.username, 0, "")
            await q.message.reply_text(T[lang]["fb_thanks"])
            return
        if sub == "text":
            sessions.setdefault(uid,{})["awaiting_free_feedback"] = True
            await q.message.reply_text(T[lang]["fb_write"])
            return

# ------------- Text handler -------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    text = (update.message.text or "").strip()
    logging.info(f"INCOMING uid={uid} text={text[:200]}")

    # первичное сохранение пользователя + приветствие
    urec = users_get(uid)
    if not urec:
        lang_guess = detect_lang_from_text(text, norm_lang(getattr(user, "language_code", None) or "en"))
        users_upsert(uid, user.username or "", lang_guess)
        sessions.setdefault(uid, {})["last_user_text"] = text
        await update.message.reply_text(T[lang_guess]["welcome"], reply_markup=ReplyKeyboardRemove())
        await render_main_menu(update, context)
        # предложить пройти intake (опционально)
        await gate_show(update, context)
        return

    # динамическая смена языка
    saved_lang = norm_lang(urec.get("lang") or getattr(user,"language_code",None) or "en")
    detected_lang = detect_lang_from_text(text, saved_lang)
    if detected_lang != saved_lang:
        users_set(uid,"lang",detected_lang)
    lang = detected_lang
    sessions.setdefault(uid, {})["last_user_text"] = text

    # ежедневный чек-ин — заметка
    if sessions.get(uid, {}).get("awaiting_daily_comment"):
        daily_add(iso(utcnow()), uid, "note", text)
        sessions[uid]["awaiting_daily_comment"] = False
        await update.message.reply_text(T[lang]["thanks"]); return

    # свободный отзыв
    if sessions.get(uid, {}).get("awaiting_free_feedback"):
        sessions[uid]["awaiting_free_feedback"] = False
        feedback_add(iso(utcnow()), uid, "free", user.username, "", text)
        await update.message.reply_text(T[lang]["fb_thanks"]); return

    # «лаборатория» — город
    if sessions.get(uid, {}).get("awaiting_city"):
        sessions[uid]["awaiting_city"] = False
        await update.message.reply_text(T[lang]["thanks"]); return

    # Health60 — ждём симптом
    if sessions.get(uid, {}).get("awaiting_h60"):
        sessions[uid]["awaiting_h60"] = False
        prof = profiles_get(uid)
        prefix = personalized_prefix(lang, prof)
        plan = health60_make_plan(lang, text, prof)
        msg = ((prefix + "\n") if prefix else "") + plan
        await update.message.reply_text(msg, reply_markup=inline_actions(lang))
        _maybe_ask_feedback(update.message, lang, uid)
        return

    # Серьёзные диагнозы по ключевым словам
    sc = detect_serious(text)
    if sc:
        prof = profiles_get(uid)
        prefix = personalized_prefix(lang, prof)
        plan = serious_plan(lang, sc, prof)
        msg = (prefix + "\n" if prefix else "") + "\n".join(plan)
        await update.message.reply_text(msg, reply_markup=inline_actions(lang))
        _maybe_ask_feedback(update.message, lang, uid)
        return

    # Если уже в pain-триаже — продолжаем через LLM/синонимы
    s = sessions.get(uid, {})
    if s.get("topic") == "pain":
        if re.search(r"\b(stop|exit|back|назад|выход|вийти)\b", text.lower()):
            sessions.pop(uid, None)
            await update.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
            return

        data = llm_decide_next_pain_step(text, lang, s)
        if data and isinstance(data, dict):
            s.setdefault("answers", {}).update({k: v for k, v in (data.get("updates") or {}).items() if v not in (None, "")})
            filled = s["answers"]
            if "red" in filled and "severity" in filled:
                sev = int(filled.get("severity", 5))
                red = str(filled.get("red") or "None")
                eid = episode_create(uid, "pain", sev, red); s["episode_id"] = eid
                plan_lines = pain_plan(lang, [red], profiles_get(uid))
                prefix = personalized_prefix(lang, profiles_get(uid))
                text_plan = (prefix + "\n" if prefix else "") + f"{T[lang]['plan_header']}\n" + "\n".join(plan_lines)
                await update.message.reply_text(text_plan)
                await update.message.reply_text(T[lang]["plan_accept"], reply_markup=inline_accept(lang))
                _maybe_ask_feedback(update.message, lang, uid)
                s["step"] = 6
                return

            ask = data.get("ask") or ""
            kb_code = data.get("kb")
            if kb_code and kb_code != "done":
                s["step"] = {"painloc": 1, "painkind": 2, "paindur": 3, "num": 4, "painrf": 5}.get(kb_code, s.get("step", 1))
                await update.message.reply_text(
                    ask or {
                        "painloc": T[lang]["triage_pain_q1"],
                        "painkind": T[lang]["triage_pain_q2"],
                        "paindur": T[lang]["triage_pain_q3"],
                        "num": T[lang]["triage_pain_q4"],
                        "painrf": T[lang]["triage_pain_q5"],
                    }[kb_code],
                    reply_markup=_kb_for_code(lang, kb_code),
                )
                return

        # Fallback по синонимам/шагам
        if s.get("step") == 1:
            label = _match_from_syns(text, lang, PAIN_LOC_SYNS)
            if label:
                s.setdefault("answers", {})["loc"] = label
                s["step"] = 2
                await update.message.reply_text(T[lang]["triage_pain_q2"], reply_markup=_kb_for_code(lang, "painkind"))
                return
            await update.message.reply_text(T[lang]["triage_pain_q1"], reply_markup=_kb_for_code(lang, "painloc"))
            return

        if s.get("step") == 2:
            label = _match_from_syns(text, lang, PAIN_KIND_SYNS)
            if label:
                s.setdefault("answers", {})["kind"] = label
                s["step"] = 3
                await update.message.reply_text(T[lang]["triage_pain_q3"], reply_markup=_kb_for_code(lang, "paindur"))
                return
            await update.message.reply_text(T[lang]["triage_pain_q2"], reply_markup=_kb_for_code(lang, "painkind"))
            return

        if s.get("step") == 3:
            label = _classify_duration(text, lang)
            if label:
                s.setdefault("answers", {})["duration"] = label
                s["step"] = 4
                await update.message.reply_text(T[lang]["triage_pain_q4"], reply_markup=_kb_for_code(lang, "num"))
                return
            await update.message.reply_text(T[lang]["triage_pain_q3"], reply_markup=_kb_for_code(lang, "paindur"))
            return

        if s.get("step") == 4:
            m = re.fullmatch(r"(?:10|[0-9])", text)
            if m:
                sev = int(m.group(0))
                s.setdefault("answers", {})["severity"] = sev
                s["step"] = 5
                await update.message.reply_text(T[lang]["triage_pain_q5"], reply_markup=_kb_for_code(lang, "painrf"))
                return
            await update.message.reply_text(T[lang]["triage_pain_q4"], reply_markup=_kb_for_code(lang, "num"))
            return

        if s.get("step") == 5:
            rf_label = _match_from_syns(text, lang, RED_FLAG_SYNS) or \
                       ("Нет" if lang == "ru" and re.search(r"\bнет\b", text.lower()) else
                        "Немає" if lang == "uk" and re.search(r"\bнема\b", text.lower()) else
                        "None" if lang in ("en", "es") and re.search(r"\bno(ne|)?\b", text.lower()) else None)
            if rf_label:
                s.setdefault("answers", {})["red"] = rf_label
                sev = int(s["answers"].get("severity", 5))
                eid = episode_create(uid, "pain", sev, rf_label); s["episode_id"] = eid
                plan_lines = pain_plan(lang, [rf_label], profiles_get(uid))
                prefix = personalized_prefix(lang, profiles_get(uid))
                text_plan = (prefix + "\n" if prefix else "") + f"{T[lang]['plan_header']}\n" + "\n".join(plan_lines)
                await update.message.reply_text(text_plan)
                await update.message.reply_text(T[lang]["plan_accept"], reply_markup=inline_accept(lang))
                _maybe_ask_feedback(update.message, lang, uid)
                s["step"] = 6
                return
            await update.message.reply_text(T[lang]["triage_pain_q5"], reply_markup=_kb_for_code(lang, "painrf"))
            return

    # Общий ответ — LLM + персонализация
    prof = profiles_get(uid)
    data = llm_router_answer(text, lang, prof)
    prefix = personalized_prefix(lang, prof)
    reply = ((prefix + "\n") if prefix else "") + (data.get("assistant_reply") or T[lang]["unknown"])
    await update.message.reply_text(reply, reply_markup=inline_actions(lang))
    _maybe_ask_feedback(update.message, lang, uid)

# ---------- Main / wiring ----------
GCLIENT = GSPREAD_CLIENT

def build_app() -> "Application":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # сохраним рендер меню, чтобы gate мог дергать
    app.bot_data["render_menu_cb"] = render_main_menu

    # Commands
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("menu",         cmd_menu))
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
    app.add_handler(CommandHandler("intake",       cmd_intake))

    # Quick language toggles
    app.add_handler(CommandHandler("ru", cmd_ru))
    app.add_handler(CommandHandler("en", cmd_en))
    app.add_handler(CommandHandler("uk", cmd_uk))
    app.add_handler(CommandHandler("es", cmd_es))

    # === PRO-опросник (6 пунктов) — регистрируем ПЕРЕД общими колбэками
    try:
        register_intake_pro(app, GCLIENT, on_complete_cb=_ipro_save_to_sheets_and_open_menu)
        logging.info("Intake Pro registered.")
    except Exception as e:
        logging.warning(f"Intake Pro registration failed: {e}")

    # Gate handler
    app.add_handler(CallbackQueryHandler(gate_cb, pattern=r"^gate:"))

    # ВАЖНО: общий колбэк ДОЛЖЕН исключать intake:, чтобы плагин получил свои события
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(?!intake:)"))

    # Текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app


if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        logging.error("TELEGRAM_TOKEN is not set")
        raise SystemExit(1)

    application = build_app()

    # Restore scheduled jobs from Sheets/memory (if any)
    try:
        schedule_from_sheet_on_start(application)
    except Exception as e:
        logging.warning(f"Scheduling restore failed: {e}")

    logging.info("Starting TendAI bot polling…")
    application.run_polling()
