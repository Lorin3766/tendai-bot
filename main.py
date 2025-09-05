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
SUPPORTED = {"ru", "en", "uk", "es"}

def norm_lang(code: Optional[str]) -> str:
    if not code:
        return "en"
    c = code.split("-")[0].lower()
    return c if c in SUPPORTED else "en"

T = {
    "en": {
        "welcome": "Hi! I’m TendAI — your health & longevity assistant.\nDescribe what’s bothering you; I’ll guide you. Let’s do a quick 40s intake to tailor advice.",
        "help": "Short checkups, 24–48h plans, reminders, daily check-ins.\nCommands: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /ru /uk /en /es",
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
    },
    "ru": {
        "welcome":"Привет! Я TendAI — ассистент здоровья и долголетия.\nРасскажи, что беспокоит; я подскажу. Сначала короткий опрос (~40с), чтобы советы были точнее.",
        "help":"Короткие проверки, план на 24–48 ч, напоминания, ежедневные чек-ины.\nКоманды: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +3 /health60 /ru /uk /en /es",
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
    },
    "uk": {
        "welcome":"Привіт! Я TendAI — асистент здоров’я та довголіття.\nРозкажи, що турбує; я підкажу. Спершу швидкий опитник (~40с) для точніших порад.",
        "help":"Короткі перевірки, план на 24–48 год, нагадування, щоденні чек-іни.\nКоманди: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /ru /uk /en /es",
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
    },
}
T["es"] = T["en"]

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
    text = (
        "Чтобы советы были точнее, пройдите короткий опрос. Можно пропустить и сделать позже."
        if lang!="en" else
        "To personalize answers, please take a short intake. You can skip and do it later."
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
            await context.application.bot.send_message(q.message.chat_id, "/start")

async def _ipro_save_to_sheets_and_open_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, profile: dict):
    uid = update.effective_user.id
    profiles_upsert(uid, {
        "sex": profile.get("sex") or "",
        "age": profile.get("age") or "",
        "goal": profile.get("goal") or "",
        "conditions": ", ".join(sorted(profile.get("chronic", []))) if isinstance(profile.get("chronic"), set) else (profile.get("chronic") or ""),
        "meds": profile.get("meds") or "",
        "activity": profile.get("hab_activity") or "",
        "sleep": profile.get("hab_sleep") or "",
        "notes": ", ".join(sorted(profile.get("complaints", []))) if isinstance(profile.get("complaints"), set) else (profile.get("complaints") or ""),
    })
    context.user_data[GATE_FLAG_KEY] = True
    render_cb = context.application.bot_data.get("render_menu_cb")
    if callable(render_cb):
        await render_cb(update, context)
    else:
        await context.application.bot.send_message(update.effective_chat.id, "/start")
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

# === keep client/id for intake_pro ===
GSPREAD_CLIENT: Optional[gspread.client.Client] = None
SPREADSHEET_ID_FOR_INTAKE: str = ""

def _headers(ws):
    return ws.row_values(1)

# --------- Memory fallback stores ----------
MEM_USERS: Dict[int, dict] = {}
MEM_PROFILES: Dict[int, dict] = {}
MEM_EPISODES: List[dict] = []
MEM_REMINDERS: List[dict] = []
MEM_FEEDBACK: List[dict] = []
MEM_DAILY: List[dict] = []

# ---- NEW: in-memory row indexes for precise updates (and fewer reads) ----
USERS_ROW: Dict[str, int] = {}     # "user_id" -> row in Users
PROFILES_ROW: Dict[str, int] = {}  # "user_id" -> row in Profiles

def _sheets_init():
    global SHEETS_ENABLED, ss, ws_feedback, ws_users, ws_profiles, ws_episodes, ws_reminders, ws_daily
    global GSPREAD_CLIENT, SPREADSHEET_ID_FOR_INTAKE, USERS_ROW, PROFILES_ROW, MEM_USERS, MEM_PROFILES
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
            vals = ws.get_all_values()
            if not vals:
                ws.append_row(headers)
            return ws

        ws_feedback = _ensure_ws("Feedback", ["timestamp","user_id","name","username","rating","comment"])
        ws_users    = _ensure_ws("Users",    ["user_id","username","lang","consent","tz_offset","checkin_hour","paused"])
        ws_profiles = _ensure_ws("Profiles", ["user_id","sex","age","goal","conditions","meds","allergies",
                                              "sleep","activity","diet","notes","updated_at"])
        ws_episodes = _ensure_ws("Episodes", ["episode_id","user_id","topic","started_at","baseline_severity","red_flags",
                                              "plan_accepted","target","reminder_at","next_checkin_at","status","last_update","notes"])
        ws_reminders= _ensure_ws("Reminders",["id","user_id","text","when_utc","created_at","status"])
        ws_daily    = _ensure_ws("DailyCheckins",["timestamp","user_id","mood","comment"])

        # ---- PRIME CACHES (single read per sheet on boot) ----
        try:
            vals = ws_users.get_all_values()
            hdr = vals[0] if vals else []
            USERS_ROW = {}
            MEM_USERS = {}
            for i in range(1, len(vals)):  # starting at row 2
                row = vals[i]
                rec = {hdr[j]: (row[j] if j < len(row) else "") for j in range(len(hdr))}
                uid = rec.get("user_id") or ""
                if uid:
                    USERS_ROW[uid] = i + 1
                    try:
                        MEM_USERS[int(uid)] = rec
                    except:
                        pass
        except Exception as e:
            logging.warning(f"Users cache prime failed: {e}")

        try:
            vals = ws_profiles.get_all_values()
            hdr = vals[0] if vals else []
            PROFILES_ROW = {}
            MEM_PROFILES = {}
            for i in range(1, len(vals)):
                row = vals[i]
                rec = {hdr[j]: (row[j] if j < len(row) else "") for j in range(len(hdr))}
                uid = rec.get("user_id") or ""
                if uid:
                    PROFILES_ROW[uid] = i + 1
                    try:
                        MEM_PROFILES[int(uid)] = rec
                    except:
                        pass
        except Exception as e:
            logging.warning(f"Profiles cache prime failed: {e}")

        logging.info("Google Sheets connected.")
    except Exception as e:
        SHEETS_ENABLED = False
        logging.error(f"SHEETS disabled (fallback to memory). Reason: {e}")

_sheets_init()

# --------- Sessions ----------
sessions: Dict[int, dict] = {}

# -------- Sheets wrappers (use memory-first, write-through) --------
def users_get(uid: int) -> dict:
    return MEM_USERS.get(uid, {})

def users_upsert(uid: int, username: str, lang: str):
    base = {
        "user_id": str(uid),
        "username": username or "",
        "lang": lang,
        "consent": (MEM_USERS.get(uid, {}).get("consent") or "no"),
        "tz_offset": (MEM_USERS.get(uid, {}).get("tz_offset") or "0"),
        "checkin_hour": (MEM_USERS.get(uid, {}).get("checkin_hour") or DEFAULT_CHECKIN_LOCAL),
        "paused": (MEM_USERS.get(uid, {}).get("paused") or "no")
    }
    MEM_USERS[uid] = base

    if not SHEETS_ENABLED:
        return
    try:
        hdr = _headers(ws_users)
        row_idx = USERS_ROW.get(str(uid))
        values = [[base.get(h, "") for h in hdr]]
        if row_idx:
            rng = f"A{row_idx}:{gsu.rowcol_to_a1(1, len(hdr)).rstrip('1')}{row_idx}"
            ws_users.update(values, range_name=rng)  # NEW order: values first
        else:
            ws_users.append_row([base.get(h, "") for h in hdr])
            # approximate last row; acceptable for append-only
            USERS_ROW[str(uid)] = ws_users.row_count
    except Exception as e:
        logging.warning(f"users_upsert sheet write failed, keeping memory only: {e}")

def users_set(uid: int, field: str, value: str):
    u = MEM_USERS.setdefault(uid, {"user_id": str(uid)})
    u[field] = value

    if not SHEETS_ENABLED:
        return
    try:
        hdr = _headers(ws_users)
        if field not in hdr:
            return
        row_idx = USERS_ROW.get(str(uid))
        if row_idx:
            col = hdr.index(field) + 1
            ws_users.update_cell(row_idx, col, value)
        else:
            users_upsert(uid, u.get("username",""), u.get("lang","en"))
    except Exception as e:
        logging.warning(f"users_set sheet write failed, keeping memory only: {e}")

def profiles_get(uid: int) -> dict:
    return MEM_PROFILES.get(uid, {})

def profiles_upsert(uid: int, data: dict):
    row = MEM_PROFILES.setdefault(uid, {"user_id": str(uid)})
    for k, v in data.items():
        row[k] = "" if v is None else (", ".join(v) if isinstance(v, list) else str(v))
    row["updated_at"] = iso(utcnow())

    if not SHEETS_ENABLED:
        return
    try:
        hdr = _headers(ws_profiles)
        values = [row.get(h, "") for h in hdr]
        end_col = gsu.rowcol_to_a1(1, len(hdr)).rstrip("1")
        idx = PROFILES_ROW.get(str(uid))
        if idx:
            ws_profiles.update([values], range_name=f"A{idx}:{end_col}{idx}")  # NEW order
        else:
            ws_profiles.append_row(values)
            PROFILES_ROW[str(uid)] = ws_profiles.row_count
    except Exception as e:
        logging.warning(f"profiles_upsert sheet write failed, keeping memory only: {e}")

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
            return
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
        for i in range(2, len(vals)+1):
            if ws_reminders.cell(i,1).value == rid:
                ws_reminders.update_cell(i,6,"sent"); return
    else:
        for r in MEM_REMINDERS:
            if r["id"]==rid:
                r["status"]="sent"; return

def daily_add(ts, uid, mood, comment):
    if SHEETS_ENABLED:
        ws_daily.append_row([ts,str(uid),mood,comment or ""])
    else:
        MEM_DAILY.append({"timestamp":ts,"user_id":str(uid),"mood":mood,"comment":comment or ""})


# --------- JobQueue helper ----------
def _has_jq_app(app) -> bool:
    return getattr(app, "job_queue", None) is not None

def _has_jq_ctx(context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        return getattr(context.application, "job_queue", None) is not None
    except Exception:
        return False

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

    # use memory cache even when sheets are enabled (avoid extra read)
    src_u = list(MEM_USERS.values())
    for u in src_u:
        if (u.get("paused") or "").lower()=="yes":
            continue
        uid = int(u.get("user_id"))
        tz_off = int(str(u.get("tz_offset") or "0"))
        hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
        schedule_daily_checkin(app, uid, tz_off, hhmm, norm_lang(u.get("lang") or "en"))

def hhmm_tuple(hhmm:str)->Tuple[int,int]:
    m = re.search(r'([01]?\d|2[0-3]):([0-5]\d)', hhmm.strip())
    return (int(m.group(1)), int(m.group(2))) if m else (8,30)

def local_to_utc_hour_min(tz_offset_hours:int, hhmm:str)->Tuple[int,int]:
    h,m = hhmm_tuple(hhmm); return ((h - tz_offset_hours) % 24, m)

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

async def job_daily_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, lang = d.get("user_id"), d.get("lang","en")
    u = users_get(uid)
    if (u.get("paused") or "").lower()=="yes":
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_good"], callback_data="mood|good"),
         InlineKeyboardButton(T[lang]["mood_ok"], callback_data="mood|ok"),
         InlineKeyboardButton(T[lang]["mood_bad"], callback_data="mood|bad")],
        [InlineKeyboardButton(T[lang]["mood_note"], callback_data="mood|note")]
    ])
    try:
        await context.bot.send_message(uid, T[lang]["daily_gm"], reply_markup=kb)
    except Exception as e:
        logging.error(f"daily checkin error: {e}")

# ------------- LLM Router (with personalization) -------------
SYS_ROUTER = (
    "You are TendAI — a concise, warm, professional health & longevity assistant (not a doctor). "
    "Always answer strictly in {lang}. Keep replies short (<=6 lines + up to 4 bullets). "
    "Personalize recommendations using the provided profile (sex/age/goal/conditions). "
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
        "profile": {k: profile.get(k, "") for k in ["sex","age","goal","conditions","meds","sleep","activity","diet"]}
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

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = norm_lang(getattr(user, "language_code", None))
    users_upsert(user.id, user.username or "", lang)
    context.user_data["lang"] = lang
    sessions.setdefault(user.id, {})["last_user_text"] = "/start"

    await update.message.reply_text(T[lang]["welcome"], reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))

    if not profiles_get(user.id) and not context.user_data.get(GATE_FLAG_KEY):
        await gate_show(update, context)

    u = users_get(user.id)
    if (u.get("consent") or "").lower() not in {"yes","no"}:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["yes"], callback_data="consent|yes"),
                                    InlineKeyboardButton(T[lang]["no"], callback_data="consent|no")]])
        await update.message.reply_text(T[lang]["ask_consent"], reply_markup=kb)

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

# === Новая команда /intake (PRO-опрос 6 вопросов) ===
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
                "Порог срочно: спутанность, жажда/мочеиспускание + рвота, глюкоза > 16 ммоль/л — неотложка.",
            ],
            "uk":[
                "Підозра на діабет/контроль: аналізи — глюкоза натще, HbA1c, ліпідограма, креатинін.",
                "Лікар: ендокринолог упродовж 1–2 тижнів.",
                "Терміново: сплутаність, сильна спрага/сечовиділення + блювання, глюкоза > 16 ммоль/л — невідкладна.",
            ],
            "en":[
                "Suspected diabetes/control: labs — fasting glucose, HbA1c, lipid panel, creatinine.",
                "Doctor: endocrinologist within 1–2 weeks.",
                "Urgent: confusion, polyuria/polydipsia with vomiting, glucose > 300 mg/dL — emergency.",
            ],
            "es":[
                "Sospecha de diabetes/control: análisis — glucosa en ayunas, HbA1c, perfil lipídico, creatinina.",
                "Médico: endocrinólogo en 1–2 semanas.",
                "Urgente: confusión, poliuria/polidipsia con vómitos, glucosa > 300 mg/dL — emergencia.",
            ],
        }[lang]
        if age>=40:
            base += [{"ru":f"Возраст {age}+: рекомендован скрининг ретинопатии и почек.",
                      "uk":f"Вік {age}+: рекомендований скринінг ретинопатії та нирок.",
                      "en":f"Age {age}+: screen for retinopathy and kidney disease.",
                      "es":f"Edad {age}+: cribado de retinopatía y riñón."}[lang]]
        return base + [T[lang]["er_text"]]
    if cond=="hepatitis":
        return {
            "ru":[ "Возможен гепатит: анализы — ALT/AST, билирубин, HBsAg, анти-HCV.",
                   "Врач: гастроэнтеролог/инфекционист в 1–2 недели.",
                   "Срочно: желтуха, тёмная моча, спутанность — неотложка."],
            "uk":[ "Ймовірний гепатит: аналізи — ALT/AST, білірубін, HBsAg, anti-HCV.",
                   "Лікар: гастроентеролог/інфекціоніст за 1–2 тижні.",
                   "Терміново: жовтяниця, темна сеча, сплутаність — невідкладна."],
            "en":[ "Possible hepatitis: labs — ALT/AST, bilirubin, HBsAg, anti-HCV.",
                   "Doctor: GI/hepatology or ID in 1–2 weeks.",
                   "Urgent: jaundice, dark urine, confusion — emergency."],
            "es":[ "Posible hepatitis: análisis — ALT/AST, bilirrubina, HBsAg, anti-HCV.",
                   "Médico: GI/hepatología o infecciosas en 1–2 semanas.",
                   "Urgente: ictericia, orina oscura, confusión — emergencia."],
        }[lang] + [T[lang]["er_text"]]
    if cond=="cancer":
        return {
            "ru":[ "Тема онкологии: оценка у профильного онколога как можно скорее (1–2 недели).",
                   "Подготовьте выписки, результаты КТ/МРТ/биопсии при наличии.",
                   "Срочно: кровотечение, нарастающая одышка/боль, резкая слабость — неотложка."],
            "uk":[ "Онкотема: консультація онколога якнайшвидше (1–2 тижні).",
                   "Підготуйте виписки, результати КТ/МРТ/біопсії за наявності.",
                   "Терміново: кровотеча, наростаюча задишка/біль, різка слабкість — невідкладна."],
            "en":[ "Oncology topic: see an oncologist asap (1–2 weeks).",
                   "Prepare records and any CT/MRI/biopsy results.",
                   "Urgent: bleeding, worsening dyspnea/pain, profound weakness — emergency."],
            "es":[ "Oncología: valoración por oncólogo lo antes posible (1–2 semanas).",
                   "Prepare informes y resultados de TC/RM/biopsia si hay.",
                   "Urgente: sangrado, disnea/dolor en aumento, gran debilidad — emergencia."],
        }[lang] + [T[lang]["er_text"]]
    if cond=="tb":
        return {
            "ru":[ "Подозрение на туберкулёз: флюорография/рентген, анализ мокроты (микроскопия/ПЦР).",
                   "Врач: фтизиатр.",
                   "Срочно: кровохарканье, высокая температура с одышкой — неотложка."],
            "uk":[ "Підозра на туберкульоз: флюорографія/рентген, аналіз мокротиння (мікроскопія/ПЛР).",
                   "Лікар: фтизіатр.",
                   "Терміново: кровохаркання, висока температура з задишкою — невідкладна."],
            "en":[ "Suspected TB: chest X-ray and sputum tests (microscopy/PCR).",
                   "Doctor: TB specialist.",
                   "Urgent: hemoptysis, high fever with breathlessness — emergency."],
            "es":[ "Sospecha de TB: radiografía de tórax y esputo (microscopía/PCR).",
                   "Médico: especialista en TB.",
                   "Urgente: hemoptisis, fiebre alta con disnea — emergencia."],
        }[lang] + [T[lang]["er_text"]]
    return [T[lang]["unknown"]]

# ------------- Profile (intake — старый 8 шагов опционально) -------------
PROFILE_STEPS = [
    {"key":"sex","opts":{
        "ru":[("Мужской","male"),("Женский","female"),("Другое","other")],
        "en":[("Male","male"),("Female","female"),("Other","other")],
        "uk":[("Чоловіча","male"),("Жіноча","female"),("Інша","other")],
        "es":[("Hombre","male"),("Mujer","female"),("Otro","other")],
    }},
    {"key":"age","opts":{
        "ru":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "en":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "uk":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "es":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
    }},
    {"key":"goal","opts":{
        "ru":[("Похудение","weight"),("Энергия","energy"),("Сон","sleep"),("Долголетие","longevity"),("Сила","strength")],
        "en":[("Weight","weight"),("Energy","energy"),("Sleep","sleep"),("Longevity","longevity"),("Strength","strength")],
        "uk":[("Вага","weight"),("Енергія","energy"),("Сон","sleep"),("Довголіття","longevity"),("Сила","strength")],
        "es":[("Peso","weight"),("Energía","energy"),("Sueño","sleep"),("Longevidad","longevity"),("Fuerza","strength")],
    }},
    {"key":"conditions","opts":{
        "ru":[("Нет","none"),("Гипертония","hypertension"),("Диабет","diabetes"),("Щитовидка","thyroid"),("Другое","other")],
        "en":[("None","none"),("Hypertension","hypertension"),("Diabetes","diabetes"),("Thyroid","thyroid"),("Other","other")],
        "uk":[("Немає","none"),("Гіпертонія","hypertension"),("Діабет","diabetes"),("Щитоподібна","thyroid"),("Інше","other")],
        "es":[("Ninguna","none"),("Hipertensión","hypertension"),("Diabetes","diabetes"),("Tiroides","thyroid"),("Otra","other")],
    }},
    {"key":"meds","opts":{
        "ru":[("Нет","none"),("Магний","magnesium"),("Витамин D","vitd"),("Аллергии есть","allergies"),("Другое","other")],
        "en":[("None","none"),("Magnesium","magnesium"),("Vitamin D","vitd"),("Allergies","allergies"),("Other","other")],
        "uk":[("Немає","none"),("Магній","magnesium"),("Вітамін D","vitd"),("Алергії","allergies"),("Інше","other")],
        "es":[("Ninguno","none"),("Magnesio","magnesium"),("Vitamina D","vitd"),("Alergias","allergies"),("Otro","other")],
    }},
    {"key":"sleep","opts":{
        "ru":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
        "en":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Irregular","irregular")],
        "uk":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
        "es":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Irregular","irregular")],
    }},
    {"key":"activity","opts":{
        "ru":[("<5к шагов","<5k"),("5–8к","5-8k"),("8–12к","8-12k"),("Спорт регулярно","sport")],
        "en":[("<5k steps","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Regular sport","sport")],
        "uk":[("<5к кроків","<5k"),("5–8к","5-8k"),("8–12к","8-12k"),("Спорт регулярно","sport")],
        "es":[("<5k pasos","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Deporte regular","sport")],
    }},
    {"key":"diet","opts":{
        "ru":[("Сбалансировано","balanced"),("Низкоугл/кето","lowcarb"),("Вегетар/веган","plant"),("Нерегулярно","irregular")],
        "en":[("Balanced","balanced"),("Low-carb/keto","lowcarb"),("Vegetarian/vegan","plant"),("Irregular","irregular")],
        "uk":[("Збалансовано","balanced"),("Маловугл/кето","lowcarb"),("Вегетар/веган","plant"),("Нерегулярно","irregular")],
        "es":[("Equilibrada","balanced"),("Baja en carb/keto","lowcarb"),("Vegetariana/vegana","plant"),("Irregular","irregular")],
    }},
]

def build_profile_kb(lang:str, key:str, opts:List[Tuple[str,str]])->InlineKeyboardMarkup:
    rows=[]; row=[]
    for label,val in opts:
        row.append(InlineKeyboardButton(label, callback_data=f"p|choose|{key}|{val}"))
        if len(row)==3:
            rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([InlineKeyboardButton(T[lang]["write"], callback_data=f"p|write|{key}"),
                 InlineKeyboardButton(T[lang]["skip"], callback_data=f"p|skip|{key}")])
    return InlineKeyboardMarkup(rows)
# ------------- Text handler -------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    text = (update.message.text or "").strip()
    logging.info(f"INCOMING uid={uid} text={text[:200]}")

    # первичное сохранение пользователя + приветствие
    urec = users_get(uid)
    if not urec:
        lang_guess = detect_lang_from_text(text, norm_lang(getattr(user, "language_code", None)))
        users_upsert(uid, user.username or "", lang_guess)
        sessions.setdefault(uid, {})["last_user_text"] = text
        await update.message.reply_text(T[lang_guess]["welcome"], reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text(T[lang_guess]["start_where"], reply_markup=inline_topic_kb(lang_guess))
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang_guess]["yes"], callback_data="consent|yes"),
                                    InlineKeyboardButton(T[lang_guess]["no"], callback_data="consent|no")]])
        await update.message.reply_text(T[lang_guess]["ask_consent"], reply_markup=kb)
        if _has_jq_ctx(context):
            schedule_daily_checkin(context.application, uid, 0, DEFAULT_CHECKIN_LOCAL, lang_guess)
        # Вместо автозапуска профиля — мягкая «шторка»
        await gate_show(update, context)
        return

    # динамическая смена языка
    saved_lang = norm_lang(urec.get("lang") or getattr(user,"language_code",None))
    detected_lang = detect_lang_from_text(text, saved_lang)
    if detected_lang != saved_lang:
        users_set(uid,"lang",detected_lang)
    lang = detected_lang
    sessions.setdefault(uid, {})["last_user_text"] = text

    # серьёзные диагнозы
    sc = detect_serious(text)
    if sc:
        sessions.setdefault(uid,{})["mode"] = "serious"
        sessions[uid]["serious_condition"] = sc
        prof = profiles_get(uid)
        prefix = personalized_prefix(lang, prof)
        plan = serious_plan(lang, sc, prof)
        msg = (prefix + "\n" if prefix else "") + "\n".join(plan)
        await update.message.reply_text(msg, reply_markup=inline_actions(lang))
        try:
            await update.message.reply_text(T[lang]["ask_fb"], reply_markup=inline_feedback_kb(lang))
        except Exception:
            pass
        return

    # ежедневный чек-ин — заметка
    if sessions.get(uid, {}).get("awaiting_daily_comment"):
        daily_add(iso(utcnow()), uid, "note", text)
        sessions[uid]["awaiting_daily_comment"] = False
        await update.message.reply_text(T[lang]["mood_thanks"]); return

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
        try:
            await update.message.reply_text(T[lang]["ask_fb"], reply_markup=inline_feedback_kb(lang))
        except Exception:
            pass
        return

    # свободный ответ для intake (старый 8-шаг. профиль)
    if sessions.get(uid, {}).get("p_wait_key"):
        key = sessions[uid]["p_wait_key"]; sessions[uid]["p_wait_key"] = None
        val = text
        if key=="age":
            m = re.search(r'\d{2}', text)
            if m: val = m.group(0)
        profiles_upsert(uid,{key:val}); sessions[uid][key]=val
        await advance_profile_ctx(context, update.effective_chat.id, lang, uid); return

    # ===== LLM-оркестратор внутри pain-триажа =====
    s = sessions.get(uid, {})
    if s.get("topic") == "pain":
        if re.search(r"\b(stop|exit|back|назад|выход|выйти)\b", text.lower()):
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
                try:
                    await update.message.reply_text(T[lang]["ask_fb"], reply_markup=inline_feedback_kb(lang))
                except Exception:
                    pass
                s["step"] = 6
                return

            ask = data.get("ask") or ""
            kb_code = data.get("kb")
            if kb_code and kb_code != "done":
                s["step"] = {"painloc": 1, "painkind": 2, "paindur": 3, "num": 4, "painrf": 5}.get(kb_code, s.get("step", 1))
                await send_unique(
                    update.message,
                    uid,
                    ask or {
                        "painloc": T[lang]["triage_pain_q1"],
                        "painkind": T[lang]["triage_pain_q2"],
                        "paindur": T[lang]["triage_pain_q3"],
                        "num": T[lang]["triage_pain_q4"],
                        "painrf": T[lang]["triage_pain_q5"],
                    }[kb_code],
                    reply_markup=_kb_for_code(lang, kb_code),
                    force=True,
                )
                return

        # Fallback по синонимам
        if s.get("step") == 1:
            label = _match_from_syns(text, lang, PAIN_LOC_SYNS)
            if label:
                s.setdefault("answers", {})["loc"] = label
                s["step"] = 2
                await send_unique(update.message, uid, T[lang]["triage_pain_q2"], reply_markup=_kb_for_code(lang, "painkind"))
                return
            await send_unique(update.message, uid, T[lang]["triage_pain_q1"], reply_markup=_kb_for_code(lang, "painloc"))
            return

        if s.get("step") == 2:
            label = _match_from_syns(text, lang, PAIN_KIND_SYNS)
            if label:
                s.setdefault("answers", {})["kind"] = label
                s["step"] = 3
                await send_unique(update.message, uid, T[lang]["triage_pain_q3"], reply_markup=_kb_for_code(lang, "paindur"))
                return
            await send_unique(update.message, uid, T[lang]["triage_pain_q2"], reply_markup=_kb_for_code(lang, "painkind"))
            return

        if s.get("step") == 3:
            label = _classify_duration(text, lang)
            if label:
                s.setdefault("answers", {})["duration"] = label
                s["step"] = 4
                await update.message.reply_text(T[lang]["triage_pain_q4"], reply_markup=_kb_for_code(lang, "num"))
                return
            await send_unique(update.message, uid, T[lang]["triage_pain_q3"], reply_markup=_kb_for_code(lang, "paindur"))
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
                try:
                    await update.message.reply_text(T[lang]["ask_fb"], reply_markup=inline_feedback_kb(lang))
                except Exception:
                    pass
                s["step"] = 6
                return
            await send_unique(update.message, uid, T[lang]["triage_pain_q5"], reply_markup=_kb_for_code(lang, "painrf"))
            return

    # ---- Общий фолбек — LLM + персонализация ----
    prof = profiles_get(uid)
    data = llm_router_answer(text, lang, prof)
    prefix = personalized_prefix(lang, prof)
    reply = ((prefix + "\n") if prefix else "") + (data.get("assistant_reply") or T[lang]["unknown"])
    await update.message.reply_text(reply, reply_markup=inline_actions(lang))
    try:
        await update.message.reply_text(T[lang]["ask_fb"], reply_markup=inline_feedback_kb(lang))
    except Exception:
        pass
    for one in (data.get("followups") or [])[:2]:
        await send_unique(update.message, uid, one, force=True)
    return


# ---------- Inline keyboards ----------
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
        [InlineKeyboardButton(label, callback_data="intake:start")]  # ключевое
    ])

def inline_accept(lang: str) -> InlineKeyboardMarkup:
    labels = T[lang]["accept_opts"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(labels[0], callback_data="acc|yes"),
         InlineKeyboardButton(labels[1], callback_data="acc|later"),
         InlineKeyboardButton(labels[2], callback_data="acc|no")]
    ])

def inline_remind(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["act_rem_4h"], callback_data="act|rem|4h")],
        [InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="act|rem|evening")],
        [InlineKeyboardButton(T[lang]["act_rem_morn"], callback_data="act|rem|morning")]
    ])

def inline_feedback_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["fb_good"], callback_data="fb|up"),
         InlineKeyboardButton(T[lang]["fb_bad"],  callback_data="fb|down")],
        [InlineKeyboardButton(T[lang]["fb_free"], callback_data="fb|text")]
    ])

def inline_actions(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["act_rem_4h"],  callback_data="act|rem|4h"),
         InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="act|rem|evening"),
         InlineKeyboardButton(T[lang]["act_rem_morn"],callback_data="act|rem|morning")],
        [InlineKeyboardButton(T[lang]["act_save_episode"], callback_data="act|save")],
        [InlineKeyboardButton(T[lang]["h60_btn"], callback_data="act|h60")],
        [InlineKeyboardButton(T[lang]["act_ex_neck"], callback_data="act|ex|neck")],
        [InlineKeyboardButton(T[lang]["act_find_lab"], callback_data="act|lab")],
        [InlineKeyboardButton(T[lang]["act_er"], callback_data="act|er")]
    ])


# ------------- Callback handler -------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = (q.data or ""); uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    chat_id = q.message.chat.id

    # Gate
    if data.startswith("gate:"):
        await gate_cb(update, context); return

    # Профиль (интейк 8 шагов — опционально)
    if data.startswith("p|"):
        _, action, key, *rest = data.split("|")
        s = sessions.setdefault(uid, {"profile_active": True, "p_step": 0})
        if action == "choose":
            value = "|".join(rest) if rest else ""
            s[key] = value
            profiles_upsert(uid, {key: value})
            await advance_profile_ctx(context, chat_id, lang, uid); return
        if action == "write":
            s["p_wait_key"] = key
            await q.message.reply_text({"ru":"Напишите короткий ответ:",
                                        "uk":"Напишіть коротко:",
                                        "en":"Type your answer:",
                                        "es":"Escribe tu respuesta:"}[lang]); return
        if action == "skip":
            profiles_upsert(uid, {key: ""})
            await advance_profile_ctx(context, chat_id, lang, uid); return

    # Согласие
    if data.startswith("consent|"):
        users_set(uid, "consent", "yes" if data.endswith("|yes") else "no")
        try: await q.edit_message_reply_markup(reply_markup=None)
        except: pass
        await q.message.reply_text(T[lang]["thanks"]); return

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
        await q.message.reply_text(T[lang]["mood_thanks"]); return

    # Темы
    if data.startswith("topic|"):
        topic = data.split("|",1)[1]
        if topic=="profile":
            await start_profile_ctx(context, chat_id, lang, uid); return
        if topic=="pain":
            sessions[uid] = {"topic":"pain","step":1,"answers":{}}
            kb = _kb_for_code(lang, "painloc")
            await q.message.reply_text(T[lang]["triage_pain_q1"], reply_markup=kb); return

        last = sessions.get(uid,{}).get("last_user_text","")
        prof = profiles_get(uid)
        prompt = f"topic:{topic}\nlast_user: {last or '—'}"
        data_llm = llm_router_answer(prompt, lang, prof)
        prefix = personalized_prefix(lang, prof)
        reply = ((prefix + "\n") if prefix else "") + (data_llm.get("assistant_reply") or T[lang]["unknown"])
        await q.message.reply_text(reply, reply_markup=inline_actions(lang))
        try:
            await q.message.reply_text(T[lang]["ask_fb"], reply_markup=inline_feedback_kb(lang))
        except Exception:
            pass
        for one in (data_llm.get("followups") or [])[:2]:
            await send_unique(q.message, uid, one, force=True)
        return

    # Pain triage buttons
    s = sessions.setdefault(uid, {})
    if data == "pain|exit":
        sessions.pop(uid, None)
        await q.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
        return

    if data.startswith("painloc|"):
        s.update({"topic":"pain","step":2,"answers":{"loc": data.split("|",1)[1]}})
        await send_unique(q.message, uid, T[lang]["triage_pain_q2"], reply_markup=_kb_for_code(lang,"painkind")); return

    if data.startswith("painkind|"):
        s.setdefault("answers",{})["kind"] = data.split("|",1)[1]; s["step"]=3
        await send_unique(q.message, uid, T[lang]["triage_pain_q3"], reply_markup=_kb_for_code(lang,"paindur")); return

    if data.startswith("paindur|"):
        s.setdefault("answers",{})["duration"] = data.split("|",1)[1]; s["step"]=4
        await send_unique(q.message, uid, T[lang]["triage_pain_q4"], reply_markup=_kb_for_code(lang,"num")); return

    if data.startswith("num|"):
        if s.get("topic")=="pain" and s.get("step")==4:
            sev = int(data.split("|",1)[1])
            s.setdefault("answers",{})["severity"] = sev; s["step"]=5
            await send_unique(q.message, uid, T[lang]["triage_pain_q5"], reply_markup=_kb_for_code(lang,"painrf")); return

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
        try:
            await q.message.reply_text(T[lang]["ask_fb"], reply_markup=inline_feedback_kb(lang))
        except Exception:
            pass
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
        if kind=="save":
            episode_create(uid, "general", 0, "")
            await q.message.reply_text(T[lang]["act_saved"]); return
        if kind=="ex":
            txt = {
                "ru":"🧘 5 минут шея: 1) медленные наклоны вперёд/назад ×5; 2) повороты в стороны ×5; 3) полукруги подбородком ×5; 4) лёгкая растяжка трапеций 2×20 сек.",
                "uk":"🧘 5 хв шия: 1) повільні нахили вперед/назад ×5; 2) повороти в сторони ×5; 3) півкола підборіддям ×5; 4) легка розтяжка трапецій 2×20 с.",
                "en":"🧘 5-min neck: 1) slow flex/extend ×5; 2) rotations left/right ×5; 3) chin semicircles ×5; 4) gentle upper-trap stretch 2×20s.",
                "es":"🧘 Cuello 5 min: 1) flex/extensión lenta ×5; 2) giros izq/der ×5; 3) semicírculos con la barbilla ×5; 4) estiramiento trapecio sup. 2×20s."
            }[lang]
            await q.message.reply_text(txt); return
        if kind=="lab":
            sessions.setdefault(uid,{})["awaiting_city"] = True
            await q.message.reply_text(T[lang]["act_city_prompt"]); return
        if kind=="er":
            await q.message.reply_text(T[lang]["er_text"]); return

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


# ---------- Main / wiring ----------
# алиас, чтобы build_app соответствовал ожидаемому имени GCLIENT
GCLIENT = GSPREAD_CLIENT

def build_app() -> "Application":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Подключаем PRO-опросник (6 пунктов) — сначала
    try:
        register_intake_pro(app, GCLIENT, on_complete_cb=_ipro_save_to_sheets_and_open_menu)
        logging.info("Intake Pro registered.")
    except Exception as e:
        logging.warning(f"Intake Pro registration failed: {e}")

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
    app.add_handler(CommandHandler("intake",       cmd_intake))

    # Quick language toggles
    app.add_handler(CommandHandler("ru", cmd_ru))
    app.add_handler(CommandHandler("en", cmd_en))
    app.add_handler(CommandHandler("uk", cmd_uk))
    app.add_handler(CommandHandler("es", cmd_es))

    # Обработчик «шторки» (если используешь gate)
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
