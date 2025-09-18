# -*- coding: utf-8 -*-
# TendAI main.py — минимальные правки без смены архитектуры:
# 1) on_callback + меню/чипы определены ДО build_app()
# 2) зарегистрирован CallbackQueryHandler(on_callback)
# 3) удалены следы Updater; только Application.run_polling()
# 4) render_main_menu — одна актуальная версия
# 5) фикс NameError из-за _build_app_orig

import os, re, json, uuid, logging, random
from datetime import datetime, timedelta, timezone, time as dtime, date
from typing import List, Tuple, Dict, Optional, Any
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

# --- SAFE import of optional PRO-intake plugin ---
try:
    from intake_pro import register_intake_pro  # noqa: F401
    HAVE_INTAKE_PRO = True
except Exception:
    HAVE_INTAKE_PRO = False
    def register_intake_pro(app, gclient=None, on_complete_cb=None):
        logging.warning("intake_pro not found — PRO-опрос отключён.")
        async def _fallback_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            await q.message.reply_text(
                "PRO-опрос недоступен на этом деплое. Используйте /profile."
            )
        app.add_handler(CallbackQueryHandler(_fallback_cb, pattern=r"^intake:"))

# ---------- OpenAI (optional) ----------
from openai import OpenAI
oai: Optional[OpenAI] = None

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
        "help": "Short checkups, 24–48h plans, reminders, daily check-ins.\nCommands: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /energy /mood /water /skin /ru /uk /en /es /menu",
        "privacy": "TendAI is not a medical service and can’t replace a doctor. We provide navigation and self-care tips. Minimal data stored for reminders. /delete_data to erase.",
        "paused_on": "Notifications paused. Use /resume to enable.",
        "paused_off": "Notifications resumed.",
        "deleted": "All your data was deleted. Use /start to begin again.",
        "ask_consent": "May I send you a follow-up to check how you feel later?",
        "yes":"Yes","no":"No",
        "unknown":"I need a bit more info: where exactly and for how long?",
        "profile_intro":"Quick intake (~40s). Use buttons or type your answer.",
        "p_step_1":"Step 1/10. Sex:",
        "p_step_2":"Step 2/10. Age:",
        "p_step_3":"Step 3/10. Height (cm):",
        "p_step_4":"Step 4/10. Weight (kg):",
        "p_step_5":"Step 5/10. Main goal:",
        "p_step_6":"Step 6/10. Chronic conditions:",
        "p_step_7":"Step 7/10. Meds:",
        "p_step_8":"Step 8/10. Supplements:",
        "p_step_9":"Step 9/10. Sleep (bed/wake, e.g., 23:30/07:00):",
        "p_step_10":"Step 10/10. Activity:",
        "write":"✍️ Write",
        "skip":"⏭️ Skip",
        "saved_profile":"Saved: ",
        "start_where":"Where do you want to start now? — or tap /menu",
        "daily_gm":"Good morning! Quick daily check-in:",
        "daily_pm":"Evening check-in: how was your day?",
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
        "act_rem_2h":"⏰ Remind in 2h",
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
        "ask_fb":"Was this helpful?",
        "fb_thanks":"Thanks for your feedback! ✅",
        "fb_write":"Write a short feedback message:",
        "fb_good":"👍 Like",
        "fb_bad":"👎 Dislike",
        "fb_free":"📝 Feedback",
        "h60_btn": "Health in 60 seconds",
        "h60_intro": "Write briefly what bothers you (e.g., “headache”, “fatigue”, “stomach pain”). I’ll give you 3 key tips in 60 seconds.",
        "h60_t1": "Possible causes",
        "h60_t2": "Do now (next 24–48h)",
        "h60_t3": "When to see a doctor",
        "h60_serious": "Serious to rule out",
        "energy_title": "Energy for today:",
        "water_prompt": "Drink 300–500 ml of water. Remind in 2 hours?",
        "skin_title": "Skin/Body tip:",
        "m_menu_title": "Main menu",
        "m_sym": "🧭 Symptoms",
        "m_h60": "🩺 Health in 60 seconds",
        "m_mini": "🔁 Mini-plans",
        "m_care": "🧪 Find care",
        "m_hab": "📊 Habits Quick-log",
        "m_rem": "🗓 Remind me",
        "m_lang": "🌐 Language",
        "m_privacy": "🔒 Privacy & how it works",
        "m_smart": "🧠 Smart check-in",
        "m_soon": "🏠 At-home labs/ECG — coming soon",
        "chips_hb": "Avoid triggers • OTC options • When to see a doctor",
        "chips_neck": "5-min routine • Heat/Ice tips • Red flags",
    },
    "ru": {
        "welcome":"Привет! Я TendAI — ассистент здоровья и долголетия.\nРасскажи, что беспокоит; я подскажу. Сначала короткий опрос (~40с), чтобы советы были точнее.",
        "help":"Короткие проверки, план на 24–48 ч, напоминания, ежедневные чек-ины.\nКоманды: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +3 /health60 /energy /mood /water /skin /ru /uk /en /es /menu",
        "privacy":"TendAI не заменяет врача. Это навигация и советы по самопомощи. Храним минимум данных для напоминаний. /delete_data — удалить.",
        "paused_on":"Напоминания поставлены на паузу. /resume — включить.",
        "paused_off":"Напоминания снова включены.",
        "deleted":"Все данные удалены. /start — начать заново.",
        "ask_consent":"Можно прислать напоминание позже, чтобы узнать, как вы?",
        "yes":"Да","no":"Нет",
        "unknown":"Нужно чуть больше деталей: где именно и сколько длится?",
        "profile_intro":"Быстрый опрос (~40с). Можно нажимать кнопки или писать свой ответ.",
        "p_step_1":"Шаг 1/10. Пол:",
        "p_step_2":"Шаг 2/10. Возраст:",
        "p_step_3":"Шаг 3/10. Рост (см):",
        "p_step_4":"Шаг 4/10. Вес (кг):",
        "p_step_5":"Шаг 5/10. Главная цель:",
        "p_step_6":"Шаг 6/10. Хронические болезни:",
        "p_step_7":"Шаг 7/10. Лекарства:",
        "p_step_8":"Шаг 8/10. Добавки:",
        "p_step_9":"Шаг 9/10. Сон (отбой/подъём, напр. 23:30/07:00):",
        "p_step_10":"Шаг 10/10. Активность:",
        "write":"✍️ Написать",
        "skip":"⏭️ Пропустить",
        "saved_profile":"Сохранил: ",
        "start_where":"С чего начнём? — или нажми /menu",
        "daily_gm":"Доброе утро! Быстрый чек-ин:",
        "daily_pm":"Вечерний чек-ин: как прошёл день?",
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
        "act_rem_2h":"⏰ Напомнить через 2 ч",
        "act_rem_4h":"⏰ Напомнить через 4 ч",
        "act_rem_eve":"⏰ Сегодня вечером",
        "act_rem_morn":"⏰ Завтра утром",
        "act_save_episode":"💾 Сохранить эпизод",
        "act_ex_neck":"🧘 5-мин упражнения для шеи",
        "act_find_lab":"🧪 Найти лабораторию",
        "act_er":"🚑 Когда срочно в скорую",
        "act_city_prompt":"Напишите город/район (текстом), подскажу лаборатории.",
        "act_saved":"Сохранено.",
        "er_text":"Если нарастает, сильная одышка, боль в груди, спутанность, стойкая высокая температура — как можно скорее к неотложке/скорой.",
        "px":"С учётом профиля: {sex}, {age} лет; цель — {goal}.",
        "back":"◀ Назад",
        "exit":"Выйти",
        "ask_fb":"Это было полезно?",
        "fb_thanks":"Спасибо за отзыв! ✅",
        "fb_write":"Напишите короткий отзыв одним сообщением:",
        "fb_good":"👍 Нравится",
        "fb_bad":"👎 Не полезно",
        "fb_free":"📝 Отзыв",
        "h60_btn": "Здоровье за 60 секунд",
        "h60_intro": "Коротко напишите, что беспокоит. Я дам 3 ключевых совета за 60 секунд.",
        "h60_t1": "Возможные причины",
        "h60_t2": "Что сделать сейчас (24–48 ч)",
        "h60_t3": "Когда обратиться к врачу",
        "h60_serious": "Что серьёзное исключить",
        "energy_title": "Энергия на сегодня:",
        "water_prompt": "Выпей 300–500 мл воды. Напомнить через 2 часа?",
        "skin_title": "Совет для кожи/тела:",
        "m_menu_title": "Главное меню",
        "m_sym": "🧭 Симптомы",
        "m_h60": "🩺 Здоровье за 60 секунд",
        "m_mini": "🔁 Мини-планы",
        "m_care": "🧪 Куда обратиться",
        "m_hab": "📊 Быстрый лог привычек",
        "m_rem": "🗓 Напомнить",
        "m_lang": "🌐 Язык",
        "m_privacy": "🔒 Приватность и как это работает",
        "m_smart": "🧠 Смарт-чек-ин",
        "m_soon": "🏠 Домашние анализы/ЭКГ — скоро",
        "chips_hb": "Избегать триггеры • OTC-варианты • Когда к врачу",
        "chips_neck": "Рутина 5 мин • Тепло/лед • Красные флаги",
    }
}
T["uk"] = {**T["ru"], **{
    "help": "Короткі перевірки, план на 24–48 год, нагадування, щоденні чек-іни.\nКоманди: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /energy /mood /water /skin /ru /uk /en /es /menu",
    "daily_pm":"Вечірній чек-ін: як пройшов день?",
    "act_rem_2h": "⏰ Нагадати через 2 год",
    "energy_title": "Енергія на сьогодні:",
    "water_prompt": "Випий 300–500 мл води. Нагадати через 2 години?",
    "skin_title": "Догляд за шкірою/тілом:",
    "m_menu_title": "Головне меню",
    "m_h60": "🩺 Здоровʼя за 60 секунд",
}}
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
    users_set(uid, "profile_banner_shown", "no")
    context.user_data[GATE_FLAG_KEY] = True
    render_cb = context.application.bot_data.get("render_menu_cb")
    if callable(render_cb):
        await render_cb(update, context)
    else:
        await context.application.bot.send_message(update.effective_chat.id, "/start")

# ---------- Anti-duplicate ----------
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
ws_feedback = ws_users = ws_profiles = ws_episodes = ws_reminders = ws_daily = ws_rules = ws_habits = None

USERS_HEADERS = [
    "user_id","username","lang","consent","tz_offset","checkin_hour","paused",
    "quiet_hours","last_sent_utc","sent_today","streak","challenge_id","challenge_day",
    "last_fb_asked","profile_banner_shown"
]
PROFILES_HEADERS = ["user_id","sex","age","goal","conditions","meds","allergies","sleep","activity","diet","notes","updated_at","goals","diet_focus","steps_target","cycle_enabled","cycle_last_date","cycle_avg_len","height_cm","weight_kg","supplements"]
EPISODES_HEADERS = ["episode_id","user_id","topic","started_at","baseline_severity","red_flags","plan_accepted","target","reminder_at","next_checkin_at","status","last_update","notes"]
REMINDERS_HEADERS = ["id","user_id","text","when_utc","created_at","status"]
DAILY_HEADERS = ["timestamp","user_id","mood","comment"]
FEEDBACK_HEADERS = ["timestamp","user_id","name","username","rating","comment"]
RULES_HEADERS = ["rule_id","domain","segment","lang","text","citations"]
HABITS_HEADERS = ["timestamp","user_id","type","value","unit","streak"]

def ws_records(ws, expected_headers):
    try:
        return ws.get_all_records(expected_headers=expected_headers, default_blank="")
    except Exception as e:
        logging.error(f"ws_records fallback ({getattr(ws,'title','?')}): {e}")
        vals = ws.get_all_values()
        if not vals: return []
        body = vals[1:]
        out = []
        for row in body:
            row = (row + [""] * len(expected_headers))[:len(expected_headers)]
            out.append({h: row[i] for i, h in enumerate(expected_headers)})
        return out

GSPREAD_CLIENT: Optional[gspread.client.Client] = None
SPREADSHEET_ID_FOR_INTAKE: str = ""

def _sheets_init():
    global SHEETS_ENABLED, ss, ws_feedback, ws_users, ws_profiles, ws_episodes, ws_reminders, ws_daily, ws_rules, ws_habits
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
            vals = ws.get_all_values()
            if not vals:
                ws.append_row(headers)
            else:
                head = vals[0]
                if len(head) < len(headers):
                    pad = headers[len(head):]
                    ws.update(range_name=f"{gsu.rowcol_to_a1(1,len(head)+1)}:{gsu.rowcol_to_a1(1,len(headers))}",
                              values=[pad])
            return ws

        ws_feedback = _ensure_ws("Feedback", FEEDBACK_HEADERS)
        ws_users    = _ensure_ws("Users", USERS_HEADERS)
        ws_profiles = _ensure_ws("Profiles", PROFILES_HEADERS)
        ws_episodes = _ensure_ws("Episodes", EPISODES_HEADERS)
        ws_reminders= _ensure_ws("Reminders", REMINDERS_HEADERS)
        ws_daily    = _ensure_ws("DailyCheckins", DAILY_HEADERS)
        ws_rules    = _ensure_ws("Rules", RULES_HEADERS)
        ws_habits   = _ensure_ws("HabitsLog", HABITS_HEADERS)
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
MEM_RULES: List[dict] = []
MEM_HABITS: List[dict] = []

# --------- Sessions ----------
sessions: Dict[int, dict] = {}

# -------- Sheets wrappers --------
def users_get(uid: int) -> dict:
    if SHEETS_ENABLED:
        for r in ws_records(ws_users, USERS_HEADERS):
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
        "quiet_hours": "22:00-08:00",
        "last_sent_utc": "",
        "sent_today": "0",
        "streak": "0",
        "challenge_id": "",
        "challenge_day": "",
        "last_fb_asked": "",
        "profile_banner_shown": "no"
    }
    if SHEETS_ENABLED:
        vals = ws_records(ws_users, USERS_HEADERS)
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                merged = {h: r.get(h, "") for h in USERS_HEADERS}
                merged["user_id"] = str(uid)
                if username: merged["username"] = username
                if lang:     merged["lang"] = lang
                end_col = gsu.rowcol_to_a1(1, len(USERS_HEADERS)).rstrip("1")
                ws_users.update(
                    range_name=f"A{i}:{end_col}{i}",
                    values=[[merged.get(h, "") for h in USERS_HEADERS]]
                )
                return
        ws_users.append_row([base.get(h,"") for h in USERS_HEADERS])
    else:
        prev = MEM_USERS.get(uid, {})
        merged = {**base, **prev}
        if username: merged["username"] = username
        if lang:     merged["lang"] = lang
        MEM_USERS[uid] = merged

def users_set(uid: int, field: str, value: str):
    if SHEETS_ENABLED:
        vals = ws_records(ws_users, USERS_HEADERS)
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                hdr = USERS_HEADERS
                if field in hdr:
                    ws_users.update_cell(i, hdr.index(field)+1, value)
                return
    else:
        u = MEM_USERS.setdefault(uid, {})
        u[field] = value

def profiles_get(uid: int) -> dict:
    if SHEETS_ENABLED:
        for r in ws_records(ws_profiles, PROFILES_HEADERS):
            if str(r.get("user_id")) == str(uid):
                return r
        return {}
    return MEM_PROFILES.get(uid, {})

def profiles_upsert(uid: int, data: dict):
    if SHEETS_ENABLED:
        hdr = PROFILES_HEADERS
        current, idx = None, None
        for i, r in enumerate(ws_records(ws_profiles, PROFILES_HEADERS), start=2):
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
            ws_profiles.update(range_name=f"A{idx}:{end_col}{idx}", values=[values])
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
        ws_episodes.append_row([rec.get(h,"") for h in EPISODES_HEADERS])
    else:
        MEM_EPISODES.append(rec)
    return eid

def episode_find_open(uid: int) -> Optional[dict]:
    if SHEETS_ENABLED:
        for r in ws_records(ws_episodes, EPISODES_HEADERS):
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
        ws_reminders.append_row([rec.get(h,"") for h in REMINDERS_HEADERS])
    else:
        MEM_REMINDERS.append(rec)
    return rid

def reminders_all_records():
    if SHEETS_ENABLED:
        return ws_records(ws_reminders, REMINDERS_HEADERS)
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

# --- HABITS LOG (streak) ---
def habits_add(uid: int, typ: str, value: Optional[str], unit: Optional[str]) -> int:
    ts = iso(utcnow())
    rec = {"timestamp":ts,"user_id":str(uid),"type":typ,"value":value or "1","unit":unit or "", "streak":"0"}
    if SHEETS_ENABLED:
        ws_habits.append_row([rec.get(h,"") for h in HABITS_HEADERS])
        rows = ws_records(ws_habits, HABITS_HEADERS)
        rows = [r for r in rows if r.get("user_id")==str(uid) and r.get("type")==typ]
    else:
        MEM_HABITS.append(rec)
        rows = [r for r in MEM_HABITS if r.get("user_id")==str(uid) and r.get("type")==typ]
    def _to_date(r):
        try:
            dt = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S%z").astimezone(timezone.utc).date()
            return dt
        except Exception:
            return utcnow().date()
    rows_sorted = sorted(rows, key=lambda r: r["timestamp"], reverse=True)
    today = (utcnow()).date()
    streak = 0
    expected = today
    for r in rows_sorted:
        d = _to_date(r)
        if d == expected:
            streak = streak + 1 if streak else 1
            expected = expected - timedelta(days=1)
        elif d < expected:
            break
    if rows_sorted:
        rows_sorted[0]["streak"] = str(streak)
    return streak

# --------- JobQueue helper ----------
def _has_jq_app(app) -> bool:
    return getattr(app, "job_queue", None) is not None

def _has_jq_ctx(context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        return getattr(context.application, "job_queue", None) is not None
    except Exception:
        return False

# --------- Scheduling (restore) ---------
def schedule_from_sheet_on_start(app):
    if not _has_jq_app(app):
        logging.warning("JobQueue not available – skip scheduling on start.")
        return
    now = utcnow()
    src = ws_records(ws_episodes, EPISODES_HEADERS) if SHEETS_ENABLED else MEM_EPISODES
    for r in src:
        if r.get("status")!="open": continue
        eid = r.get("episode_id"); uid = int(r.get("user_id"))
        nca = r.get("next_checkin_at") or ""
        if not nca: continue
        try:
            dt_ = datetime.strptime(nca, "%Y-%m-%d %H:%M:%S%z")
        except:
            continue
        delay = max(60, (dt_-now).total_seconds())
        app.job_queue.run_once(job_checkin_episode, when=delay, data={"user_id":uid,"episode_id":eid})
    for r in reminders_all_records():
        if (r.get("status") or "")!="scheduled": continue
        uid = int(r.get("user_id")); rid=r.get("id")
        try:
            dt_ = datetime.strptime(r.get("when_utc"), "%Y-%m-%d %H:%M:%S%z")
        except:
            continue
        delay = max(60,(dt_-now).total_seconds())
        app.job_queue.run_once(job_oneoff_reminder, when=delay, data={"user_id":uid,"reminder_id":rid})
    src_u = ws_records(ws_users, USERS_HEADERS) if SHEETS_ENABLED else list(MEM_USERS.values())
    for u in src_u:
        if (u.get("paused") or "").lower()=="yes": continue
        uid = int(u.get("user_id"))
        tz_off = int(str(u.get("tz_offset") or "0"))
        hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
        schedule_daily_checkin(app, uid, tz_off, hhmm, norm_lang(u.get("lang") or "en"))
        schedule_morning_evening(app, uid, tz_off, norm_lang(u.get("lang") or "en"))

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

def schedule_morning_evening(app, uid:int, tz_off:int, lang:str, evening="20:30"):
    if not _has_jq_app(app): return
    for j in app.job_queue.get_jobs_by_name(f"daily_e_{uid}"):
        j.schedule_removal()
    h_e, m_e = hhmm_tuple(evening); h_e = (h_e - tz_off) % 24
    app.job_queue.run_daily(
        job_evening_checkin,
        dtime(hour=h_e, minute=m_e, tzinfo=timezone.utc),
        name=f"daily_e_{uid}",
        data={"user_id":uid,"lang":lang}
    )

# ------------- Quiet hours / limiter -------------
def _in_quiet(uid: int, now_utc: datetime) -> bool:
    u = users_get(uid)
    q = (u.get("quiet_hours") or "").strip()
    if not q: return False
    m = re.match(r'(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})', q)
    if not m: return False
    tz_off = int(str(u.get("tz_offset") or "0"))
    local = now_utc + timedelta(hours=tz_off)
    start = local.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    end   = local.replace(hour=int(m.group(3)), minute=int(m.group(4)), second=0, microsecond=0)
    if end <= start:
        return local >= start or local <= end
    return start <= local <= end

def can_send(uid: int) -> bool:
    u = users_get(uid)
    if (u.get("paused") or "").lower() == "yes": return False
    if _in_quiet(uid, utcnow()): return False
    sent_today = int(str(u.get("sent_today") or "0"))
    return sent_today < 2

def mark_sent(uid: int):
    u = users_get(uid)
    tz_off = int(str(u.get("tz_offset") or "0"))
    last = u.get("last_sent_utc") or ""
    today_local = (utcnow() + timedelta(hours=tz_off)).date()
    last_local  = None
    if last:
        try:
            last_local = (datetime.strptime(last, "%Y-%m-%d %H:%M:%S%z").astimezone(timezone.utc) + timedelta(hours=tz_off)).date()
        except:
            last_local = None
    sent = 0 if (not last_local or last_local != today_local) else int(str(u.get("sent_today") or "0"))
    users_set(uid, "sent_today", str(sent + 1))
    users_set(uid, "last_sent_utc", iso(utcnow()))

async def maybe_send(context: ContextTypes.DEFAULT_TYPE, uid: int, text: str, kb=None):
    if can_send(uid):
        try:
            await context.bot.send_message(uid, text, reply_markup=kb)
            mark_sent(uid)
        except Exception as e:
            logging.error(f"send fail: {e}")

# ------------- Jobs -------------
async def job_checkin_episode(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, eid = d.get("user_id"), d.get("episode_id")
    if not uid or not eid: return
    u = users_get(uid)
    if (u.get("paused") or "").lower()=="yes": return
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

# ===== LLM Router =====
SYS_ROUTER = (
    "You are TendAI — a concise, warm, professional health & longevity assistant (not a doctor). "
    "Always answer strictly in {lang}. Keep replies short. "
    "TRIAGE: ask 1–2 clarifiers first; advise ER only for clear red flags. "
    "Return JSON ONLY like: "
    "{\"intent\":\"symptom\"|\"nutrition\"|\"sleep\"|\"labs\"|\"habits\"|\"longevity\"|\"other\","
    "\"assistant_reply\":\"string\",\"followups\":[\"string\"],\"needs_more\":true,"
    "\"red_flags\":false,\"confidence\":0.0}"
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
            messages=[{"role":"system","content":sys},{"role":"user","content":text}]
        )
        out = resp.choices[0].message.content.strip()
        data = json.loads(out)
        if "followups" not in data or data["followups"] is None:
            data["followups"] = []
        return data
    except Exception as e:
        logging.error(f"router LLM error: {e}")
        return {"intent":"other","assistant_reply":T[lang]["unknown"],"followups":[],"needs_more":True,"red_flags":False,"confidence":0.3}

# ===== Rules & tips =====
def rules_match(seg: str, prof: dict) -> bool:
    if not seg:
        return True
    for part in seg.split("&"):
        m = re.match(r'(\w+)\s*(>=|<=|=|>|<)\s*([\w\-]+)', part.strip())
        if not m: return False
        k, op, v = m.groups()
        pv = (prof.get(k) or prof.get(k.lower()) or "")
        if k in ("age","steps_target","cycle_avg_len","height_cm","weight_kg"):
            try: pv = int(re.search(r'\d+', str(pv)).group()); v = int(v)
            except Exception: return False
        else:
            pv = str(pv).lower(); v = str(v).lower()
        if op == "="  and not (pv == v): return False
        if op == ">=" and not (pv >= v): return False
        if op == "<=" and not (pv <= v): return False
        if op == ">"  and not (pv >  v): return False
        if op == "<"  and not (pv <  v): return False
    return True

def _read_rules():
    if SHEETS_ENABLED:
        return ws_records(ws_rules, RULES_HEADERS)
    return MEM_RULES

def pick_nutrition_tips(lang: str, prof: dict, limit: int = 2) -> List[str]:
    tips = []
    for r in _read_rules():
        if (r.get("domain") or "").lower() != "nutrition": continue
        if (r.get("lang") or "en") != lang: continue
        if rules_match(r.get("segment") or "", prof):
            t = (r.get("text") or "").strip()
            if t: tips.append(t)
    random.shuffle(tips)
    return tips[:limit]

# ===== Cycle helpers =====
def cycle_phase_for(uid: int) -> Optional[str]:
    prof = profiles_get(uid)
    if str(prof.get("cycle_enabled") or "").lower() not in {"1","yes","true"}:
        return None
    try:
        last = datetime.strptime(str(prof.get("cycle_last_date")), "%Y-%m-%d").date()
        avg  = int(str(prof.get("cycle_avg_len") or "28"))
    except Exception:
        return None
    day = ((utcnow().date() - last).days % max(avg, 21)) + 1
    if 1 <= day <= 5:   return "menses"
    if 6 <= day <= 13:  return "follicular"
    if 14 <= day <= 15: return "ovulation"
    return "luteal"

def cycle_tip(lang: str, phase: str) -> str:
    base = {
        "menses":   {"ru":"Фаза менструации: мягче к себе, железо/белок, сон приоритет.","en":"Menses phase: go gentle, prioritize iron/protein and sleep."},
        "follicular":{"ru":"Фолликулярная фаза: лучше заходят тренировки/новые задачи.","en":"Follicular phase: great for workouts and new tasks."},
        "ovulation": {"ru":"Овуляция: следи за сном и гидратацией.","en":"Ovulation: watch sleep and hydration."},
        "luteal":    {"ru":"Лютеиновая: магний/прогулка, стабильный сон, меньше кофеина.","en":"Luteal: magnesium/walk, steady sleep, go easy on caffeine."}
    }
    return base.get(phase, {}).get(lang, "")

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
    await maybe_send(context, uid, T[lang]["daily_gm"], kb)
    prof = profiles_get(uid)
    tips = pick_nutrition_tips(lang, prof, limit=2)
    if tips:
        await maybe_send(context, uid, "• " + "\n• ".join(tips))
    phase = cycle_phase_for(uid)
    if phase:
        tip = cycle_tip(lang, phase)
        if tip:
            await maybe_send(context, uid, tip)

async def job_evening_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, lang = d.get("user_id"), d.get("lang","en")
    u = users_get(uid)
    if (u.get("paused") or "").lower()=="yes":
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_good"], callback_data="mood|good"),
         InlineKeyboardButton(T[lang]["mood_ok"],   callback_data="mood|ok"),
         InlineKeyboardButton(T[lang]["mood_bad"],  callback_data="mood|bad")],
        [InlineKeyboardButton(T[lang]["mood_note"], callback_data="mood|note")]
    ])
    await maybe_send(context, uid, T[lang]["daily_pm"], kb)

# ===== Menus / Chips UI =====
def inline_numbers_0_10() -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(str(n), callback_data=f"num|{n}") for n in range(0, 6)]
    row2 = [InlineKeyboardButton(str(n), callback_data=f"num|{n}") for n in range(6, 11)]
    rows = [row1, row2, [InlineKeyboardButton("◀", callback_data="pain|exit")]]
    return InlineKeyboardMarkup(rows)

def inline_list(options: List[str], prefix: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for opt in options:
        row.append(InlineKeyboardButton(opt, callback_data=f"{prefix}|{opt}"))
        if len(row) == 3: rows.append(row); row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def inline_main_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["m_h60"], callback_data="menu|h60")],
        [InlineKeyboardButton(T[lang]["m_sym"], callback_data="menu|sym")],
        [InlineKeyboardButton(T[lang]["m_mini"], callback_data="menu|mini")],
        [InlineKeyboardButton(T[lang]["m_care"], callback_data="menu|care")],
        [InlineKeyboardButton(T[lang]["m_hab"], callback_data="menu|hab")],
        [InlineKeyboardButton(T[lang]["m_rem"], callback_data="menu|rem")],
        [InlineKeyboardButton(T[lang]["m_lang"], callback_data="menu|lang")],
        [InlineKeyboardButton(T[lang]["m_privacy"], callback_data="menu|privacy")],
        [InlineKeyboardButton(T[lang]["m_smart"], callback_data="menu|smart")],
        [InlineKeyboardButton(T[lang]["m_soon"], callback_data="menu|coming")]
    ])

def inline_symptoms_menu(lang: str) -> InlineKeyboardMarkup:
    labels = {"en":["Headache","Heartburn","Fatigue","Other"],
              "ru":["Головная боль","Изжога","Усталость","Другое"],
              "uk":["Головний біль","Печія","Втома","Інше"],
              "es":["Dolor de cabeza","Acidez","Fatiga","Otro"]}[lang]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(labels[0], callback_data="sym|headache"),
         InlineKeyboardButton(labels[1], callback_data="sym|heartburn")],
        [InlineKeyboardButton(labels[2], callback_data="sym|fatigue"),
         InlineKeyboardButton(labels[3], callback_data="sym|other")],
        [InlineKeyboardButton(T[lang]["back"], callback_data="menu|root")]
    ])

def inline_miniplans_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Neck pain 5-min" if lang=="en" else "Шея 5 мин", callback_data="mini|neck")],
        [InlineKeyboardButton("Sleep reset (3 nights)" if lang=="en" else "Сон-ресет (3 ночи)", callback_data="mini|sleepreset")],
        [InlineKeyboardButton("Heartburn: 3 steps" if lang=="en" else "Изжога: 3 шага", callback_data="mini|heartburn")],
        [InlineKeyboardButton("Hydration on hot days" if lang=="en" else "Гидратация в жару", callback_data="mini|hydration")],
        [InlineKeyboardButton(T[lang]["back"], callback_data="menu|root")]
    ])

def inline_findcare_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Labs near me" if lang=="en" else "Лабы рядом", callback_data="care|labsnear")],
        [InlineKeyboardButton("Urgent care" if lang=="en" else "Неотложка", callback_data="care|urgent")],
        [InlineKeyboardButton("Free clinics (NJ)" if lang=="en" else "Бесплатные клиники (NJ)", callback_data="care|free_nj")],
        [InlineKeyboardButton(T[lang]["back"], callback_data="menu|root")]
    ])

def inline_habits_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💧 Water",   callback_data="hab|water"),
         InlineKeyboardButton("🚶 Steps",   callback_data="hab|steps")],
        [InlineKeyboardButton("😴 Sleep",   callback_data="hab|sleep"),
         InlineKeyboardButton("🧠 Stress",  callback_data="hab|stress")],
        [InlineKeyboardButton("⚖️ Weight",  callback_data="hab|weight")],
        [InlineKeyboardButton(T[lang]["back"], callback_data="menu|root")]
    ])

def inline_lang_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("EN", callback_data="lang|en"),
         InlineKeyboardButton("RU", callback_data="lang|ru"),
         InlineKeyboardButton("UK", callback_data="lang|uk"),
         InlineKeyboardButton("ES", callback_data="lang|es")],
        [InlineKeyboardButton(T[lang]["back"], callback_data="menu|root")]
    ])

def inline_feedback_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["fb_good"], callback_data="fb|up"),
         InlineKeyboardButton(T[lang]["fb_bad"],  callback_data="fb|down")],
        [InlineKeyboardButton(T[lang]["fb_free"], callback_data="fb|text")]
    ])

def inline_actions(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏰ +4h" if lang=="en" else T[lang]["act_rem_4h"],  callback_data="act|rem|4h"),
         InlineKeyboardButton("⏰ This evening" if lang=="en" else T[lang]["act_rem_eve"],  callback_data="act|rem|evening"),
         InlineKeyboardButton("⏰ Tomorrow morning" if lang=="en" else T[lang]["act_rem_morn"], callback_data="act|rem|morning")],
        [InlineKeyboardButton(T[lang]["h60_btn"], callback_data="act|h60")],
        [InlineKeyboardButton(T[lang]["act_ex_neck"], callback_data="act|ex|neck")],
        [InlineKeyboardButton(T[lang]["act_find_lab"], callback_data="act|lab")],
        [InlineKeyboardButton(T[lang]["act_er"], callback_data="act|er")]
    ])

# ===== Chips text =====
def microplan_text(key: str, lang: str) -> str:
    if key=="neck":
        return {"ru":"Шея 5 мин:\n1) Медленные наклоны ×5\n2) Повороты ×5\n3) Полукруги подбородком ×5\n4) Растяжка трапеций 2×20с.",
                "en":"Neck 5-min:\n1) Slow flex/extend ×5\n2) Rotations L/R ×5\n3) Chin semicircles ×5\n4) Upper-trap stretch 2×20s."}[lang]
    if key=="sleepreset":
        return {"ru":"Сон-ресет (3 ночи):\nН1: 60м без экрана + фикс. отбой.\nН2: 15м вне кровати при пробуждениях.\nН3: Утренний свет 10–15м; кофе до 14:00.",
                "en":"Sleep reset (3 nights):\nN1: 60-min screen detox + fixed bedtime.\nN2: 15-min out of bed if awake.\nN3: AM light 10–15m; caffeine by 2pm."}[lang]
    if key=="heartburn":
        return {"ru":"Изжога — 3 шага:\n1) Меньше порции; не ложиться 3ч.\n2) Убрать триггеры: жирное/алк./мята/шоколад/кофе.\n3) Антацид 2–3 дня по инструкции.",
                "en":"Heartburn — 3 steps:\n1) Smaller meals; no lying 3h.\n2) Remove triggers: fatty foods, alcohol, mint, chocolate, coffee.\n3) OTC antacid 2–3 days as directed."}[lang]
    if key=="hydration":
        return {"ru":"Гидратация в жару: 200–300 мл воды каждый активный час; электролиты при длительной жаре; светлая одежда и тень.",
                "en":"Hot-day hydration: 200–300 ml water each active hour; add electrolytes for prolonged heat; light clothing & shade."}[lang]
    return ""

def chip_text(domain: str, kind: str, lang: str) -> str:
    if domain=="hb":
        if kind=="triggers":
            return {"ru":"Триггеры изжоги: жирное, острое, шоколад, кофе, цитрусы, мята, алкоголь. Последний приём пищи ≥3ч до сна.",
                    "en":"Heartburn triggers: fatty/spicy foods, chocolate, coffee, citrus, mint, alcohol. Last meal ≥3h before bed."}[lang]
        if kind=="otc":
            return {"ru":"OTC: антацид (алгинаты/карбонаты) 2–3 дня. Если часто — обсудить с врачом.",
                    "en":"OTC: antacid (alginates/carbonates) for 2–3 days. If frequent — discuss with a clinician."}[lang]
        if kind=="red":
            return {"ru":"Когда к врачу при изжоге: дисфагия, кровь в рвоте, чёрный стул, потеря веса, ночные боли, >2–3 нед несмотря на меры.",
                    "en":"See a doctor if: trouble swallowing, vomiting blood, black stools, weight loss, nocturnal pain, >2–3 weeks despite measures."}[lang]
    if domain=="neck":
        if kind=="routine": return microplan_text("neck", lang)
        if kind=="heat":
            return {"ru":"Шея: первые 48 ч лёд 10–15 мин ×2–3/д; затем тепло; мягкая растяжка без боли.",
                    "en":"Neck: first 48h prefer ice 10–15 min ×2–3/day, then heat; gentle stretch without pain."}[lang]
        if kind=="red":
            return {"ru":"Красные флаги: слабость/онемение рук, травма, лихорадка, боль >7/10, быстрая прогрессия — обратиться к врачу.",
                    "en":"Red flags: arm weakness/numbness, trauma, fever, pain >7/10, rapid progression — seek care."}[lang]
    return ""

# ===== CONTINUE IN PART 2 =====
# ======== PART 2 / CONT ========

# --- вспомогательные мини-утилиты, не дублируют часть 1 ---
def _mk_reminder_utc(uid: int, kind: str) -> Optional[datetime]:
    """Return UTC datetime for +4h / evening / morning using user's tz."""
    u = users_get(uid)
    tz_off = int(str(u.get("tz_offset") or "0"))
    now_utc = utcnow()
    local = now_utc + timedelta(hours=tz_off)
    if kind == "4h":
        target_local = local + timedelta(hours=4)
    elif kind == "evening":
        target_local = local.replace(hour=20, minute=30, second=0, microsecond=0)
        if target_local <= local:
            target_local += timedelta(days=1)
    elif kind == "morning":
        target_local = local.replace(hour=8, minute=30, second=0, microsecond=0)
        if target_local <= local:
            target_local += timedelta(days=1)
    else:
        return None
    return target_local - timedelta(hours=tz_off)

async def _schedule_oneoff(context: ContextTypes.DEFAULT_TYPE, uid: int, when_utc: datetime, text: str):
    rid = reminder_add(uid, text, when_utc)
    if _has_jq_ctx(context):
        delay = max(60, (when_utc - utcnow()).total_seconds())
        context.application.job_queue.run_once(job_oneoff_reminder, when=delay, data={"user_id":uid,"reminder_id":rid})

# ---------- основной обработчик callback’ов ----------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    uid  = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await q.answer()

    # ===== GATE =====
    if data.startswith("gate:"):
        await gate_cb(update, context)
        return

    # ===== Intake (PRO) кнопка-стартер =====
    if data == "intake:start":
        # регистрируется плагином register_intake_pro(); просто передаём управление
        # если плагина нет — в fallback из части 1 уже добавлен хендлер на ^intake:
        # здесь сообщать ничего не нужно
        return

    # ===== Главное меню =====
    if data == "menu|root":
        try: await q.edit_message_text(T[lang]["m_menu_title"], reply_markup=inline_main_menu(lang))
        except: await q.message.reply_text(T[lang]["m_menu_title"], reply_markup=inline_main_menu(lang))
        return

    if data == "menu|h60":
        await cmd_health60(update, context); return

    if data == "menu|sym":
        try: await q.edit_message_text("Symptoms:", reply_markup=inline_symptoms_menu(lang))
        except: await q.message.reply_text("Symptoms:", reply_markup=inline_symptoms_menu(lang))
        return

    if data == "menu|mini":
        try: await q.edit_message_text("Mini-plans:", reply_markup=inline_miniplans_menu(lang))
        except: await q.message.reply_text("Mini-plans:", reply_markup=inline_miniplans_menu(lang))
        return

    if data == "menu|care":
        try: await q.edit_message_text("Find care:", reply_markup=inline_findcare_menu(lang))
        except: await q.message.reply_text("Find care:", reply_markup=inline_findcare_menu(lang))
        return

    if data == "menu|hab":
        try: await q.edit_message_text("Habits Quick-log:", reply_markup=inline_habits_menu(lang))
        except: await q.message.reply_text("Habits Quick-log:", reply_markup=inline_habits_menu(lang))
        return

    if data == "menu|rem":
        try: await q.edit_message_text(T[lang]["remind_when"], reply_markup=inline_remind(lang))
        except: await q.message.reply_text(T[lang]["remind_when"], reply_markup=inline_remind(lang))
        return

    if data == "menu|lang":
        try: await q.edit_message_text("Language:", reply_markup=inline_lang_menu(lang))
        except: await q.message.reply_text("Language:", reply_markup=inline_lang_menu(lang))
        return

    if data == "menu|privacy":
        await q.message.reply_text(T[lang]["privacy"]); return

    if data == "menu|smart":
        try: await q.edit_message_text("Smart check-in:", reply_markup=inline_smart_checkin(lang))
        except: await q.message.reply_text("Smart check-in:", reply_markup=inline_smart_checkin(lang))
        return

    if data == "menu|coming":
        await q.message.reply_text({"ru":"Скоро!","uk":"Скоро!","en":"Coming soon!","es":"¡Pronto!"}[lang]); return

    # ===== Симптомы =====
    if data.startswith("sym|"):
        key = data.split("|",1)[1]
        if key == "headache":
            sessions.setdefault(uid,{})["topic"]="pain"; sessions[uid]["step"]=1
            await q.message.reply_text(T[lang]["triage_pain_q1"], reply_markup=inline_list(T[lang]["triage_pain_q1_opts"], "painloc")); return
        if key == "heartburn":
            await q.message.reply_text(microplan_text("heartburn", lang), reply_markup=chips_hb_kb(lang)); return
        if key == "fatigue":
            await cmd_energy(update, context); return
        if key == "other":
            await q.message.reply_text(T[lang]["unknown"]); return

    # ===== Мини-планы прямые =====
    if data.startswith("mini|"):
        key = data.split("|",1)[1]
        await q.message.reply_text(microplan_text(key, lang) or "…", reply_markup=inline_actions(lang))
        return

    # ===== Find care =====
    if data.startswith("care|"):
        kind = data.split("|",1)[1]
        await q.message.reply_text(care_links(kind, lang))
        if kind == "labsnear":
            sessions.setdefault(uid,{})["awaiting_city"] = True
            await q.message.reply_text(T[lang]["act_city_prompt"])
        return

    # ===== Habits Quick-log =====
    if data == "hab|water":
        habits_log_add(uid, "water", "250")
        await q.message.reply_text("💧 +250 ml"); return
    if data == "hab|steps":
        habits_log_add(uid, "steps", "1000")
        await q.message.reply_text("🚶 +1000"); return
    if data == "hab|sleep":
        habits_log_add(uid, "sleep", "1")
        await q.message.reply_text("😴 logged"); return
    if data == "hab|stress":
        habits_log_add(uid, "stress_down", "1")
        await q.message.reply_text("🧠 calm logged"); return
    if data == "hab|weight":
        sessions.setdefault(uid,{})["awaiting_weight"] = True
        await q.message.reply_text("Введите вес (кг), например 72.5" if lang!="en" else "Send weight in kg, e.g., 72.5")
        return

    # ===== Напоминания =====
    if data.startswith("rem|"):
        kind = data.split("|",1)[1]
        when_utc = _mk_reminder_utc(uid, kind)
        if when_utc:
            await _schedule_oneoff(context, uid, when_utc, T[lang]["thanks"])
            await q.message.reply_text(T[lang]["thanks"])
        return

    # ===== Быстрые действия (панель действий) =====
    if data.startswith("act|"):
        parts = data.split("|")
        if len(parts) >= 3 and parts[1] == "rem":
            kind = parts[2]
            when_utc = _mk_reminder_utc(uid, kind)
            if when_utc:
                await _schedule_oneoff(context, uid, when_utc, T[lang]["thanks"])
                await q.message.reply_text(T[lang]["thanks"])
            return
        if parts[1] == "h60":
            await cmd_health60(update, context); return
        if parts[1] == "ex" and parts[2] == "neck":
            await q.message.reply_text(microplan_text("neck", lang)); return
        if parts[1] == "lab":
            await q.message.reply_text(care_links("labsnear", lang))
            sessions.setdefault(uid,{})["awaiting_city"] = True
            await q.message.reply_text(T[lang]["act_city_prompt"])
            return
        if parts[1] == "er":
            await q.message.reply_text(T[lang]["er_text"]); return

    # ===== Переключение языка =====
    if data.startswith("lang|"):
        new_lang = data.split("|",1)[1]
        if new_lang in SUPPORTED:
            users_set(uid, "lang", new_lang)
            lang = new_lang
            await q.message.reply_text({"ru":"Готово.","uk":"Готово.","en":"Done.","es":"Listo."}[lang])
            await q.message.reply_text(T[lang]["m_menu_title"], reply_markup=inline_main_menu(lang))
        return

    # ===== Согласие на follow-ups =====
    if data.startswith("consent|"):
        users_set(uid, "consent", "yes" if data.endswith("yes") else "no")
        await q.message.reply_text(T[lang]["thanks"]); return

    # ===== Триаж боли: пошаговые кнопки =====
    if data.startswith(("painloc|","painkind|","paindur|","num|","painrf|","pain|")):
        s = sessions.setdefault(uid, {"topic":"pain","step":1,"answers":{}})
        if data.startswith("pain|exit"):
            sessions.pop(uid, None)
            await q.message.reply_text(T[lang]["m_menu_title"], reply_markup=inline_main_menu(lang)); return
        if data.startswith("painloc|"):
            s["answers"]["loc"] = data.split("|",1)[1]; s["step"]=2
            await q.message.reply_text(T[lang]["triage_pain_q2"], reply_markup=inline_list(T[lang]["triage_pain_q2_opts"], "painkind")); return
        if data.startswith("painkind|"):
            s["answers"]["kind"] = data.split("|",1)[1]; s["step"]=3
            await q.message.reply_text(T[lang]["triage_pain_q3"], reply_markup=inline_list(T[lang]["triage_pain_q3_opts"], "paindur")); return
        if data.startswith("paindur|"):
            s["answers"]["dur"] = data.split("|",1)[1]; s["step"]=4
            await q.message.reply_text(T[lang]["triage_pain_q4"], reply_markup=inline_numbers_0_10()); return
        if data.startswith("num|"):
            try: s["answers"]["severity"] = int(data.split("|",1)[1])
            except: s["answers"]["severity"] = 5
            s["step"]=5
            await q.message.reply_text(T[lang]["triage_pain_q5"], reply_markup=inline_list(T[lang]["triage_pain_q5_opts"], "painrf")); return
        if data.startswith("painrf|"):
            rf = data.split("|",1)[1]
            s["answers"]["rf"] = rf
            # сформировать план
            prof = profiles_get(uid)
            plan_lines = pain_plan(lang, [rf], prof)
            await q.message.reply_text("\n".join(plan_lines), reply_markup=inline_accept(lang))
            return

    # ===== Подтверждение плана (accept) =====
    if data.startswith("acc|"):
        kind = data.split("|",1)[1]
        if kind == "yes":
            # напомнить вечером как дефолт
            when_utc = _mk_reminder_utc(uid, "evening")
            if when_utc:
                await _schedule_oneoff(context, uid, when_utc, T[lang]["checkin_ping"])
        await q.message.reply_text(T[lang]["remind_when"], reply_markup=inline_remind(lang))
        return

    # ===== Чипы (контекстные) =====
    if data.startswith("chip|"):
        _, domain, kind = data.split("|",2)
        await q.message.reply_text(chip_text(domain, kind, lang) or "…")
        return

    # fallback — показать меню
    await q.message.reply_text(T[lang]["m_menu_title"], reply_markup=inline_main_menu(lang))

# ====== ВАЖНО: зарегистрировать общий обработчик callback-кнопок ======
# (Если эта строка попадёт ниже run_polling, она не выполнится.
#  Поэтому убедись, что Часть 2 вставлена ВЫШЕ блока `if __name__ == "__main__": ... run_polling()`.)
try:
    # если приложение уже создано и доступно как `application` — добавим хендлер здесь
    if "application" in globals() and getattr(globals()["application"], "add_handler", None):
        globals()["application"].add_handler(CallbackQueryHandler(on_callback), group=1)
        # сохранить ссылку на актуальный рендер меню (если требуется из других мест)
        globals()["application"].bot_data["render_menu_cb"] = render_main_menu
except Exception as _e:
    logging.warning(f"Deferred callback registration skipped: {_e}")
