# -*- coding: utf-8 -*-
# TendAI main.py — обновлено: короткий тон, нерепетирующийся профиль, 3 пресета напоминаний,
# фидбек ≤1/день, персонализация (уточняющий вопрос + 3 конкретные опции), авто-вопросы,
# новая таблица Preferences (память вкусов и пресетов)

import os, re, json, uuid, logging, random
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

# --- SAFE import of optional PRO-intake plugin ---
try:
    from intake_pro import register_intake_pro  # noqa: F401
    HAVE_INTAKE_PRO = True
except Exception:
    HAVE_INTAKE_PRO = False
    def register_intake_pro(app, gclient=None, on_complete_cb=None):
        logging.warning("intake_pro not found — PRO-опрос отключён на этом деплое.")
        async def _fallback_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            await q.message.reply_text("PRO-опрос недоступен на этом деплое. Используйте /profile.")
        app.add_handler(CallbackQueryHandler(_fallback_cb, pattern=r"^intake:"))

from openai import OpenAI

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
        "welcome": "Hi! I’m TendAI — your health & longevity assistant.\nTell me in a few words, I’ll help. Quick 40s intake for better tips.",
        "help": "Short checkups, 24–48h plans, reminders, daily check-ins.\nCommands: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /energy /mood /water /skin /ru /uk /en /es",
        "privacy": "TendAI is not a medical service. Minimal data for reminders. /delete_data to erase.",
        "paused_on": "Notifications paused. Use /resume to enable.",
        "paused_off": "Notifications resumed.",
        "deleted": "All your data was deleted. Use /start to begin again.",
        "ask_consent": "May I send a follow-up later to check how you feel?",
        "yes":"Yes","no":"No",
        "unknown":"Tell me a bit more: where exactly and for how long?",
        "profile_intro":"Quick intake (~40s). Tap a button or type.",
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
        "start_where":"What today — nutrition, sleep or activity?",
        "daily_gm":"Good morning! Quick check-in:",
        "mood_good":"😃 Good","mood_ok":"😐 Okay","mood_bad":"😣 Poor","mood_note":"✍️ Comment",
        "mood_thanks":"Thanks! Have a smooth day 👋",
        "triage_pain_q1":"Where does it hurt?",
        "triage_pain_q1_opts":["Head","Throat","Back","Belly","Other"],
        "triage_pain_q2":"What kind of pain?",
        "triage_pain_q2_opts":["Dull","Sharp","Pulsating","Pressing"],
        "triage_pain_q3":"How long?",
        "triage_pain_q3_opts":["<3h","3–24h",">1 day",">1 week"],
        "triage_pain_q4":"Rate it (0–10):",
        "triage_pain_q5":"Any of these now?",
        "triage_pain_q5_opts":["High fever","Vomiting","Weakness/numbness","Speech/vision problems","Trauma","None"],
        "plan_header":"Your 24–48h plan:",
        "plan_accept":"Try this today?",
        "accept_opts":["✅ Yes","🔁 Later","✖️ No"],
        "remind_when":"When to remind & check you?",
        "remind_opts":["in 4h","this evening","tomorrow morning","no need"],
        "thanks":"Got it 🙌",
        "checkin_ping":"Quick check-in: how is it now (0–10)?",
        "checkin_better":"Nice! Keep it up 💪",
        "checkin_worse":"If red flags or pain ≥7/10 — consider medical help.",
        "act_rem_4h":"⏰ Remind in 4h",
        "act_rem_eve":"⏰ This evening",
        "act_rem_morn":"⏰ Tomorrow morning",
        "act_save_episode":"💾 Save as episode",
        "act_ex_neck":"🧘 5-min neck routine",
        "act_find_lab":"🧪 Find a lab",
        "act_er":"🚑 Emergency info",
        "act_city_prompt":"Type your city/area so I can suggest a lab (text only).",
        "act_saved":"Saved.",
        "er_text":"If symptoms worsen, severe shortness of breath, chest pain, confusion, or persistent high fever — seek urgent care.",
        "px":"{sex_loc}, {age}y; goal — {goal_loc}.",
        "back":"◀ Back",
        "exit":"Exit",
        "ask_fb":"Was this helpful?",
        "fb_thanks":"Thanks for your feedback! ✅",
        "fb_write":"Write a short feedback message:",
        "fb_good":"👍 Like",
        "fb_bad":"👎 Dislike",
        "fb_free":"📝 Feedback",
        "h60_btn": "Health in 60 seconds",
        "h60_intro": "Write briefly what bothers you (e.g., “headache”, “fatigue”, “stomach pain”). I’ll give 3 key tips in 60 seconds.",
        "h60_t1": "Possible causes",
        "h60_t2": "Do now (24–48h)",
        "h60_t3": "When to see a doctor",
        "h60_serious": "Serious to rule out",
        # Youth quick labels
        "energy_title": "Energy for today:",
        "water_prompt": "Drink 300–500 ml of water. Remind in 4 hours?",
        "skin_title": "Skin/Body tip:",
        # Auto-questions
        "auto_q_sleep":"How did you sleep (hours)?",
        "auto_q_breakfast":"What did you have for breakfast?",
        "auto_q_choice":"Tip on activity or nutrition?"
    },
    "ru": {
        "welcome":"Привет! Я TendAI — ассистент здоровья и долголетия.\nРасскажи в двух словах — подскажу. Короткий опрос (~40с) для точности.",
        "help":"Короткие проверки, план на 24–48 ч, напоминания, ежедневные чек-ины.\nКоманды: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +3 /health60 /energy /mood /water /skin /ru /uk /en /es",
        "privacy":"TendAI не заменяет врача. Храним минимум данных для напоминаний. /delete_data — удалить.",
        "paused_on":"Напоминания на паузе. /resume — включить.",
        "paused_off":"Напоминания снова включены.",
        "deleted":"Все данные удалены. /start — начать заново.",
        "ask_consent":"Можно прислать напоминание позже — узнать, как вы?",
        "yes":"Да","no":"Нет",
        "unknown":"Чуть подробнее: где именно и как давно?",
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
        "start_where":"С чего сегодня — питание, сон или активность?",
        "daily_gm":"Доброе утро! Быстрый чек-ин:",
        "mood_good":"😃 Хорошо","mood_ok":"😐 Нормально","mood_bad":"😣 Плохо","mood_note":"✍️ Комментарий",
        "mood_thanks":"Спасибо! Хорошего дня 👋",
        "triage_pain_q1":"Где болит?",
        "triage_pain_q1_opts":["Голова","Горло","Спина","Живот","Другое"],
        "triage_pain_q2":"Какой характер боли?",
        "triage_pain_q2_opts":["Тупая","Острая","Пульсирующая","Давящая"],
        "triage_pain_q3":"Как давно?",
        "triage_pain_q3_opts":["<3ч","3–24ч",">1 дня",">1 недели"],
        "triage_pain_q4":"Оцените (0–10):",
        "triage_pain_q5":"Есть ли что-то из этого сейчас?",
        "triage_pain_q5_opts":["Высокая температура","Рвота","Слабость/онемение","Нарушение речи/зрения","Травма","Нет"],
        "plan_header":"Ваш план на 24–48 часов:",
        "plan_accept":"Попробуете сегодня?",
        "accept_opts":["✅ Да","🔁 Позже","✖️ Нет"],
        "remind_when":"Когда напомнить и спросить самочувствие?",
        "thanks":"Принято 🙌",
        "checkin_ping":"Коротко: как сейчас по шкале 0–10?",
        "checkin_better":"Отлично! Держим курс 💪",
        "checkin_worse":"Если есть «красные флаги» или боль ≥7/10 — лучше обратиться к врачу.",
        "act_rem_4h":"⏰ Через 4 ч",
        "act_rem_eve":"⏰ Вечером",
        "act_rem_morn":"⏰ Утром",
        "act_save_episode":"💾 Сохранить эпизод",
        "act_ex_neck":"🧘 Шея 5 мин",
        "act_find_lab":"🧪 Найти лабораторию",
        "act_er":"🚑 Когда срочно в скорую",
        "act_city_prompt":"Напишите город/район — подскажу лабораторию.",
        "act_saved":"Сохранено.",
        "er_text":"Если нарастает, сильная одышка, боль в груди, спутанность, стойкая высокая температура — как можно скорее к неотложке/скорой.",
        "px":"{sex_loc}, {age}; цель — {goal_loc}.",
        "back":"◀ Назад",
        "exit":"Выйти",
        "ask_fb":"Это было полезно?",
        "fb_thanks":"Спасибо за отзыв! ✅",
        "fb_write":"Напишите короткий отзыв одним сообщением:",
        "fb_good":"👍 Нравится",
        "fb_bad":"👎 Не полезно",
        "fb_free":"📝 Отзыв",
        "h60_btn": "Здоровье за 60 секунд",
        "h60_intro": "Коротко напишите, что беспокоит (например: «болит голова», «усталость», «боль в животе»). Дам 3 ключевых совета.",
        "h60_t1": "Возможные причины",
        "h60_t2": "Что сделать сейчас (24–48 ч)",
        "h60_t3": "Когда обратиться к врачу",
        "h60_serious": "Что серьёзное исключить",
        "energy_title": "Энергия на сегодня:",
        "water_prompt": "Выпей 300–500 мл воды. Напомнить через 4 часа?",
        "skin_title": "Совет для кожи/тела:",
        "auto_q_sleep":"Как спалось (часы)?",
        "auto_q_breakfast":"Что ел на завтрак?",
        "auto_q_choice":"Подсказку — по активности или питанию?"
    },
    "uk": {
        **{
            k: v for k, v in {
                "help": "Короткі перевірки, план на 24–48 год, нагадування, щоденні чек-іни.\nКоманди: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /energy /mood /water /skin /ru /uk /en /es",
                "act_rem_4h": "⏰ Через 4 год",
                "energy_title": "Енергія на сьогодні:",
                "water_prompt": "Випий 300–500 мл води. Нагадати через 4 години?",
                "skin_title": "Догляд за шкірою/тілом:",
                "auto_q_sleep":"Як спалося (години)?",
                "auto_q_breakfast":"Що їв на сніданок?",
                "auto_q_choice":"Підказку — активність чи харчування?"
            }.items()
        }
    }
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
    # сбрасываем отображение префикса — покажется один раз после изменения профиля
    sessions.setdefault(uid, {})["px_shown"] = False
    sessions[uid]["px_sig"] = profile_signature(profiles_get(uid))
    context.user_data[GATE_FLAG_KEY] = True
    render_cb = context.application.bot_data.get("render_menu_cb")
    if callable(render_cb):
        await render_cb(update, context)
    else:
        await context.application.bot.send_message(update.effective_chat.id, "/start")

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
ws_feedback = ws_users = ws_profiles = ws_episodes = ws_reminders = ws_daily = ws_rules = ws_prefs = None

# === Canonical headers + safe reader (фикс дубликатов хедеров) ===
USERS_HEADERS = ["user_id","username","lang","consent","tz_offset","checkin_hour","paused","quiet_hours","last_sent_utc","sent_today","streak","challenge_id","challenge_day"]
PROFILES_HEADERS = ["user_id","sex","age","goal","conditions","meds","allergies","sleep","activity","diet","notes","updated_at","goals","diet_focus","steps_target","cycle_enabled","cycle_last_date","cycle_avg_len"]
EPISODES_HEADERS = ["episode_id","user_id","topic","started_at","baseline_severity","red_flags","plan_accepted","target","reminder_at","next_checkin_at","status","last_update","notes"]
REMINDERS_HEADERS = ["id","user_id","text","when_utc","created_at","status"]
DAILY_HEADERS = ["timestamp","user_id","mood","comment"]
FEEDBACK_HEADERS = ["timestamp","user_id","name","username","rating","comment"]
RULES_HEADERS = ["rule_id","domain","segment","lang","text","citations"]
# NEW: Preferences — личные вкусы/настройки/ограничители фидбека и автоспрашиваний
PREFERENCES_HEADERS = ["user_id","likes_json","dislikes_json","meal_budget","reminder_preset","last_fb_date","last_auto_q_utc"]

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

# === Сохраняем gspread client и id таблицы для register_intake_pro ===
GSPREAD_CLIENT: Optional[gspread.client.Client] = None
SPREADSHEET_ID_FOR_INTAKE: str = ""

def _sheets_init():
    global SHEETS_ENABLED, ss, ws_feedback, ws_users, ws_profiles, ws_episodes, ws_reminders, ws_daily, ws_rules, ws_prefs
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

        ws_feedback = _ensure_ws("Feedback", FEEDBACK_HEADERS)
        ws_users    = _ensure_ws("Users", USERS_HEADERS)
        ws_profiles = _ensure_ws("Profiles", PROFILES_HEADERS)
        ws_episodes = _ensure_ws("Episodes", EPISODES_HEADERS)
        ws_reminders= _ensure_ws("Reminders", REMINDERS_HEADERS)
        ws_daily    = _ensure_ws("DailyCheckins", DAILY_HEADERS)
        ws_rules    = _ensure_ws("Rules", RULES_HEADERS)
        ws_prefs    = _ensure_ws("Preferences", PREFERENCES_HEADERS)  # NEW
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
MEM_PREFS: Dict[int, dict] = {}

# --------- Sessions ----------
sessions: Dict[int, dict] = {}

# -------- Sheets wrappers --------
def _headers(ws):
    return ws.row_values(1)

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
        "challenge_day": ""
    }
    if SHEETS_ENABLED:
        vals = ws_records(ws_users, USERS_HEADERS)
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                ws_users.update(range_name=f"A{i}:M{i}", values=[[base.get(h,"") for h in USERS_HEADERS]])
                return
        ws_users.append_row([base.get(h,"") for h in USERS_HEADERS])
    else:
        MEM_USERS[uid] = base

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

def prefs_get(uid: int) -> dict:
    if SHEETS_ENABLED:
        for r in ws_records(ws_prefs, PREFERENCES_HEADERS):
            if str(r.get("user_id")) == str(uid):
                return r
        return {}
    return MEM_PREFS.get(uid, {})

def prefs_upsert(uid: int, data: dict):
    if SHEETS_ENABLED:
        hdr = PREFERENCES_HEADERS
        current, idx = None, None
        for i, r in enumerate(ws_records(ws_prefs, PREFERENCES_HEADERS), start=2):
            if str(r.get("user_id")) == str(uid):
                current, idx = r, i
                break
        if not current:
            current = {"user_id": str(uid), "likes_json":"{}", "dislikes_json":"{}", "meal_budget":"", "reminder_preset":"", "last_fb_date":"", "last_auto_q_utc":""}
        for k,v in data.items():
            current[k] = "" if v is None else (json.dumps(v, ensure_ascii=False) if isinstance(v,(dict,list)) else str(v))
        values = [current.get(h,"") for h in hdr]
        end_col = gsu.rowcol_to_a1(1, len(hdr)).rstrip("1")
        if idx:
            ws_prefs.update(range_name=f"A{idx}:{end_col}{idx}", values=[values])
        else:
            ws_prefs.append_row(values)
    else:
        row = MEM_PREFS.setdefault(uid, {"user_id": str(uid),"likes_json":"{}","dislikes_json":"{}","meal_budget":"","reminder_preset":"","last_fb_date":"","last_auto_q_utc":""})
        for k,v in data.items():
            row[k] = "" if v is None else (json.dumps(v, ensure_ascii=False) if isinstance(v,(dict,list)) else str(v))

def prefs_set(uid: int, field: str, value: str):
    if SHEETS_ENABLED:
        vals = ws_records(ws_prefs, PREFERENCES_HEADERS)
        hdr = PREFERENCES_HEADERS
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                if field in hdr:
                    ws_prefs.update_cell(i, hdr.index(field)+1, value)
                return
        # if not exists — upsert
        prefs_upsert(uid, {field: value})
    else:
        row = MEM_PREFS.setdefault(uid, {"user_id": str(uid)})
        row[field] = value

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
        # вечерний + тихие часы
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

# === Утро+Вечер по локальному времени (simple) ===
def schedule_morning_evening(app, uid:int, tz_off:int, lang:str, morning="08:30", evening="20:30"):
    if not _has_jq_app(app): return
    for name in [f"daily_m_{uid}", f"daily_e_{uid}"]:
        for j in app.job_queue.get_jobs_by_name(name): j.schedule_removal()
    h_m, m_m = hhmm_tuple(morning); h_m = (h_m - tz_off) % 24
    h_e, m_e = hhmm_tuple(evening); h_e = (h_e - tz_off) % 24
    app.job_queue.run_daily(job_daily_checkin, dtime(hour=h_m, minute=m_m, tzinfo=timezone.utc),
                            name=f"daily_m_{uid}", data={"user_id":uid,"lang":lang})
    app.job_queue.run_daily(job_daily_checkin, dtime(hour=h_e, minute=m_e, tzinfo=timezone.utc),
                            name=f"daily_e_{uid}", data={"user_id":uid,"lang":lang})

# ------------- Лимитер авто-сообщений + тихие часы -------------
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

# ------------- Jobs (часть общая; сам job_daily_checkin ниже переопределён) -------------
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

# ------------- Тон/стиль + роутер -------------
SYS_ROUTER = (
    "You are TendAI — a concise, warm assistant (not a doctor). "
    "Answer strictly in {lang}. Be brief (≤3 short lines or up to 3 bullets). "
    "One idea → one question. Use user profile (sex/age/goal/conditions) silently. "
    "TRIAGE: ask 1 clarifier first; ER only for clear red flags. Return JSON ONLY as: "
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
            max_tokens=380,
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

# ===== Rules-based подсказки (доказательная база) =====
def rules_match(seg: str, prof: dict) -> bool:
    if not seg:
        return True
    for part in seg.split("&"):
        m = re.match(r'(\w+)\s*(>=|<=|=|>|<)\s*([\w\-]+)', part.strip())
        if not m:
            return False
        k, op, v = m.groups()
        pv = (prof.get(k) or prof.get(k.lower()) or "")
        if k in ("age", "steps_target", "cycle_avg_len"):
            try:
                pv = int(re.search(r'\d+', str(pv)).group())
                v = int(v)
            except Exception:
                return False
        else:
            pv = str(pv).lower()
            v = str(v).lower()
        if op == "=" and not (pv == v): return False
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
        if (r.get("domain") or "").lower() != "nutrition":
            continue
        if (r.get("lang") or "en") != lang:
            continue
        if rules_match(r.get("segment") or "", prof):
            t = (r.get("text") or "").strip()
            if t:
                tips.append(t)
    random.shuffle(tips)
    return tips[:limit]

# ===== Мини-логика цикла (опционально) =====
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
    if lang not in T: lang = "en"
    base = {
        "menses": {
            "ru":"Фаза менструации: мягче к себе, железо/белок, сон приоритет.",
            "en":"Menses phase: go gentle, prioritize iron/protein and sleep."
        },
        "follicular": {
            "ru":"Фолликулярная фаза: лучше заходят тренировки/новые задачи.",
            "en":"Follicular phase: great for workouts and new tasks."
        },
        "ovulation": {
            "ru":"Овуляция: следи за сном и гидратацией.",
            "en":"Ovulation: watch sleep and hydration."
        },
        "luteal": {
            "ru":"Лютеиновая: магний/прогулка, стабильный сон, меньше кофеина.",
            "en":"Luteal: magnesium/walk, steady sleep, go easy on caffeine."
        }
    }
    return base.get(phase, {}).get(lang, "")

# ===== Префикс профиля: показывать 1 раз при старте/изменении =====
def profile_signature(prof: dict) -> str:
    return "|".join([
        str(prof.get("sex") or "").lower(),
        re.search(r"\d+", str(prof.get("age") or "" )).group() if re.search(r"\d+", str(prof.get("age") or "")) else "",
        str(prof.get("goal") or "").lower()
    ])

def _localize_sex(lang:str, sex_raw:str)->str:
    s = (sex_raw or "").lower()
    if lang=="ru":
        return {"male":"мужчина","female":"женщина"}.get(s, "чел.")
    if lang=="uk":
        return {"male":"чоловік","female":"жінка"}.get(s, "людина")
    return {"male":"male","female":"female"}.get(s, s or "—")

def _localize_goal(lang:str, goal_raw:str)->str:
    g = (goal_raw or "").lower()
    if lang=="ru":
        return {"longevity":"долголетие","weight":"вес","energy":"энергия","sleep":"сон","strength":"сила"}.get(g, g or "—")
    if lang=="uk":
        return {"longevity":"довголіття","weight":"вага","energy":"енергія","sleep":"сон","strength":"сила"}.get(g, g or "—")
    return g or "—"

def maybe_profile_prefix(uid:int, lang:str, prof:dict) -> str:
    """Возвращает локализованный префикс один раз при старте/после изменения профиля."""
    sig = profile_signature(prof)
    s = sessions.setdefault(uid, {})
    if s.get("px_sig") != sig:
        # профиль изменился — разрешаем показать снова
        s["px_shown"] = False
        s["px_sig"] = sig
    if s.get("px_shown"):
        return ""
    # сформируем строку и отметим как показанную
    sex_loc = _localize_sex(lang, prof.get("sex"))
    goal_loc = _localize_goal(lang, prof.get("goal"))
    age_num = re.search(r"\d+", str(prof.get("age") or ""))
    age_str = age_num.group() if age_num else "—"
    prefix = T[lang]["px"].format(sex_loc=sex_loc, age=age_str, goal_loc=goal_loc)
    s["px_shown"] = True
    return prefix

# ===== Клавиатуры (сокращено до 3 пресетов напоминаний, ≤6–7 кнопок) =====
def inline_numbers_0_10() -> InlineKeyboardMarkup:
    rows = []
    row1 = [InlineKeyboardButton(str(n), callback_data=f"num|{n}") for n in range(0, 6)]
    row2 = [InlineKeyboardButton(str(n), callback_data=f"num|{n}") for n in range(6, 11)]
    rows.append(row1); rows.append(row2); rows.append([InlineKeyboardButton("◀", callback_data="pain|exit")])
    return InlineKeyboardMarkup(rows)

def inline_list(options: List[str], prefix: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for opt in options:
        row.append(InlineKeyboardButton(opt, callback_data=f"{prefix}|{opt}"))
        if len(row) == 3: rows.append(row); row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def inline_topic_kb(lang: str) -> InlineKeyboardMarkup:
    label = {"ru":"🧩 Опрос 6 пунктов","uk":"🧩 Опитник (6)","en":"🧩 Intake (6 Qs)","es":"🧩 Intake (6)"}[lang]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍎 Nutrition", callback_data="topic|nutrition"),
         InlineKeyboardButton("😴 Sleep", callback_data="topic|sleep"),
         InlineKeyboardButton("🏃 Activity", callback_data="topic|habits")],
        [InlineKeyboardButton("🧪 Labs", callback_data="topic|labs"),
         InlineKeyboardButton("🧬 Longevity", callback_data="topic|longevity"),
         InlineKeyboardButton("👤 Profile", callback_data="topic|profile")],
        [InlineKeyboardButton(label, callback_data="intake:start")]
    ])

def inline_accept(lang: str) -> InlineKeyboardMarkup:
    labels = T[lang]["accept_opts"]
    return InlineKeyboardMarkup([[InlineKeyboardButton(labels[0], callback_data="acc|yes"),
                                  InlineKeyboardButton(labels[1], callback_data="acc|later"),
                                  InlineKeyboardButton(labels[2], callback_data="acc|no")]])

def inline_remind(lang: str) -> InlineKeyboardMarkup:
    # Только 3 пресета: +4ч, вечером, утром
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["act_rem_4h"], callback_data="rem|4h")],
        [InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="rem|evening")],
        [InlineKeyboardButton(T[lang]["act_rem_morn"], callback_data="rem|morning")]
    ])

def inline_feedback_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["fb_good"], callback_data="fb|up"),
         InlineKeyboardButton(T[lang]["fb_bad"],  callback_data="fb|down")],
        [InlineKeyboardButton(T[lang]["fb_free"], callback_data="fb|text")]
    ])

def inline_actions(lang: str) -> InlineKeyboardMarkup:
    # ≤6 кнопок: 3 напоминания + 3 быстрых
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["act_rem_4h"],  callback_data="act|rem|4h"),
         InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="act|rem|evening"),
         InlineKeyboardButton(T[lang]["act_rem_morn"],callback_data="act|rem|morning")],
        [InlineKeyboardButton(T[lang]["h60_btn"],     callback_data="act|h60"),
         InlineKeyboardButton(T[lang]["act_find_lab"],callback_data="act|lab"),
         InlineKeyboardButton(T[lang]["act_er"],      callback_data="act|er")]
    ])

# ===== Фидбек: не чаще 1 раза в сутки =====
def _today_str(uid:int)->str:
    u=users_get(uid); tz=int(str(u.get("tz_offset") or "0"))
    return (utcnow()+timedelta(hours=tz)).date().isoformat()

def should_ask_feedback(uid:int)->bool:
    p = prefs_get(uid)
    last = (p.get("last_fb_date") or "").strip()
    today = _today_str(uid)
    return last != today

async def maybe_ask_feedback(context_or_msg, uid:int, lang:str):
    if should_ask_feedback(uid):
        # обновим дату в Preferences
        prefs_set(uid, "last_fb_date", _today_str(uid))
        kb = inline_feedback_kb(lang)
        # context_or_msg может быть контекст jobqueue или message; используем универсально
        try:
            if hasattr(context_or_msg, "bot"):
                await context_or_msg.bot.send_message(uid, T[lang]["ask_fb"], reply_markup=kb)
            else:
                await context_or_msg.reply_text(T[lang]["ask_fb"], reply_markup=kb)
        except Exception: pass

# ===== Персонализация: уточняющий вопрос + 3 конкретных варианта =====
def _json_or_empty(s:str)->dict:
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}

def _push_like(uid:int, key:str):
    p = prefs_get(uid); likes = _json_or_empty(p.get("likes_json"))
    likes[key] = int(likes.get(key,0)) + 1
    prefs_upsert(uid, {"likes_json": likes})

def meal_options(lang:str, time_of_day:str, prof:dict)->List[str]:
    plant = (str(prof.get("diet") or "").lower() in {"plant","vegetarian","vegan"})
    # Бюджет/аллергии и др. можно расширять; держим коротко и конкретно
    if plant:
        base = {
            "breakfast": ["Тофу-скрэмбл + овощи", "Греческий йогурт 200 г + ягоды", "Овсянка на воде + арахисовая паста 1 ст. л."],
            "dinner":    ["Чечевичная котлета + салат", "Кускус + нут + овощи", "Тофу запечённый + брокколи"]
        }
    else:
        base = {
            "breakfast": ["Творог 200 г + огурец", "Омлет 2 яйца + овощи", "Сардины 1 банка + салат"],
            "dinner":    ["Курица 120–150 г + салат", "Рыба 120–150 г + овощи", "Говядина постная 120 г + брокколи"]
        }
    if lang=="en":
        base = {
            "breakfast": ["Cottage cheese 200 g + cucumber", "2-egg omelet + veggies", "Sardines (1 can) + salad"],
            "dinner":    ["Chicken 120–150 g + salad", "Fish 120–150 g + veggies", "Lean beef 120 g + broccoli"]
        } if not plant else {
            "breakfast": ["Tofu scramble + veggies", "Greek yogurt 200 g + berries", "Oatmeal + 1 tbsp peanut butter"],
            "dinner":    ["Lentil patty + salad", "Couscous + chickpeas + veggies", "Baked tofu + broccoli"]
        }
    return base.get(time_of_day, [])[:3]

def build_food_options_kb(options:List[str])->InlineKeyboardMarkup:
    rows=[]
    for i,opt in enumerate(options, start=1):
        rows.append([InlineKeyboardButton(f"{i}) {opt}", callback_data=f"food|pick|{i}|{opt}")])
    rows.append([InlineKeyboardButton("◀", callback_data="food|back")])
    return InlineKeyboardMarkup(rows)

def personalize_followup(text:str, lang:str, prof:dict)->Tuple[Optional[str], Optional[InlineKeyboardMarkup], Optional[dict]]:
    """
    Возвращает (вопрос, клавиатура, meta) если удалось распознать намерение.
    meta может содержать { 'kind':'protein', 'time_choices':True } и т.п.
    """
    low = (text or "").lower()
    # ↑ белок / protein
    if any(k in low for k in ["белка","белок","protein"]):
        q = "Окей 👍. Для завтрака или ужина?" if lang=="ru" else ("Ок 👍. Сніданок чи вечеря?" if lang=="uk" else "Okay 👍. For breakfast or dinner?")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🥣 Завтрак" if lang!="en" else "🥣 Breakfast", callback_data="food|time|breakfast"),
             InlineKeyboardButton("🍽️ Ужин"    if lang!="en" else "🍽️ Dinner",    callback_data="food|time|dinner")]
        ])
        return q, kb, {"kind":"protein"}
    # сон
    if any(k in low for k in ["сон","sleep","insomnia","уснуть"]):
        q = "Подсказку — уснуть легче или утренний разгон?" if lang=="ru" else ("Підказку — легше заснути чи ранковий розгін?" if lang=="uk" else "Tip — easier falling asleep or morning boost?")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌙 Заснуть" if lang!="en" else "🌙 Fall asleep", callback_data="sleep|focus|night"),
             InlineKeyboardButton("☀️ Утро"   if lang!="en" else "☀️ Morning",     callback_data="sleep|focus|morning")]
        ])
        return q, kb, {"kind":"sleep"}
    # питание/активность выбор
    if any(k in low for k in ["питани","nutrition","активност","activity"]):
        q = T[lang]["auto_q_choice"]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🍎 Питание" if lang!="en" else "🍎 Nutrition", callback_data="topic|nutrition"),
             InlineKeyboardButton("🏃 Активность" if lang!="en" else "🏃 Activity", callback_data="topic|habits")]
        ])
        return q, kb, {"kind":"choice"}
    return None, None, None

# ===== Переопределяем daily-джобу: приветствие + питание + цикл + авто-вопрос =====
async def job_daily_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, lang = d.get("user_id"), d.get("lang","en")
    u = users_get(uid)
    if (u.get("paused") or "").lower()=="yes":
        return
    # приветствие + настроение (1 короткое)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_good"], callback_data="mood|good"),
         InlineKeyboardButton(T[lang]["mood_ok"], callback_data="mood|ok"),
         InlineKeyboardButton(T[lang]["mood_bad"], callback_data="mood|bad")],
        [InlineKeyboardButton(T[lang]["mood_note"], callback_data="mood|note")]
    ])
    await maybe_send(context, uid, T[lang]["daily_gm"], kb)

    # 1–2 совета по питанию из Rules
    prof = profiles_get(uid)
    tips = pick_nutrition_tips(lang, prof, limit=1)  # короче
    if tips:
        await maybe_send(context, uid, "• " + "\n• ".join(tips))

    # деликатный совет по фазе цикла (если включено)
    phase = cycle_phase_for(uid)
    if phase:
        tip = cycle_tip(lang, phase)
        if tip:
            await maybe_send(context, uid, tip)

    # Авто-вопрос раз в день
    p = prefs_get(uid); last = (p.get("last_auto_q_utc") or "")
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S%z") if last else None
    except: last_dt = None
    do_ask = not last_dt or (utcnow() - last_dt) > timedelta(hours=22)
    if do_ask and can_send(uid):
        q = random.choice([T[lang]["auto_q_sleep"], T[lang]["auto_q_breakfast"], T[lang]["auto_q_choice"]])
        await context.bot.send_message(uid, q)
        prefs_set(uid, "last_auto_q_utc", iso(utcnow()))

# ===== Serious keywords =====
SERIOUS_KWS = {
    "diabetes":["diabetes","диабет","сахарный","цукров","глюкоза","hba1c","гликированный","глюкоза"],
    "hepatitis":["hepatitis","гепатит","печень hbs","hcv","alt","ast"],
    "cancer":["cancer","рак","онко","онколог","опухол","пухлина","tumor"],
    "tb":["tuberculosis","tb","туберкул","туберкульоз"],
}

def detect_serious(text: str) -> Optional[str]:
    low = (text or "").lower()
    for cond, kws in SERIOUS_KWS.items():
        if any(k in low for k in kws):
            return cond
    return None

# ===== Персонализированный префикс и планы =====
def pain_plan(lang: str, red_flags_selected: List[str], profile: dict) -> List[str]:
    flg = [s for s in red_flags_selected if s and str(s).lower() not in ["none","нет","немає","ninguno","no"]]
    if flg:
        return {"ru":["⚠️ Есть тревожные признаки. Лучше как можно скорее показаться врачу/в скорую."],
                "uk":["⚠️ Є тривожні ознаки. Варто якнайшвидше звернутися до лікаря/швидкої."],
                "en":["⚠️ Red flags present. Please seek urgent medical evaluation."],
                "es":["⚠️ Señales de alarma presentes. Busca evaluación médica urgente."]}[lang]
    age = int(re.search(r"\d+", str(profile.get("age") or "0")).group(0)) if re.search(r"\d+", str(profile.get("age") or "")) else 0
    extra = []
    if age >= 60:
        extra.append({"ru":"Вам 60+, будьте осторожны с НПВП; пейте воду и при ухудшении обратитесь к врачу.",
                      "uk":"Вам 60+, обережно з НПЗЗ; пийте воду, за погіршення — до лікаря.",
                      "en":"Age 60+: be careful with NSAIDs; hydrate and seek care if worsening.",
                      "es":"Edad 60+: cuidado con AINEs; hidrátate y busca atención si empeora."}[lang])
    core = {"ru":["1) Вода 400–600 мл и 15–20 мин отдыха.",
                  "2) Если нет противопоказаний — ибупрофен 200–400 мг однократно с едой.",
                  "3) Проветрить, меньше экранов 30–60 мин.","Цель: к вечеру боль ≤3/10."],
            "uk":["1) Вода 400–600 мл і 15–20 хв спокою.",
                  "2) Якщо нема протипоказань — ібупрофен 200–400 мг одноразово з їжею.",
                  "3) Провітрити, менше екрану 30–60 хв.","Мета: до вечора біль ≤3/10."],
            "en":["1) Drink 400–600 ml water; rest 15–20 min.",
                  "2) If no contraindications — ibuprofen 200–400 mg once with food.",
                  "3) Air the room; reduce screens 30–60 min.","Goal: by evening pain ≤3/10."],
            "es":["1) Bebe 400–600 ml de agua; descansa 15–20 min.",
                  "2) Si no hay contraindicaciones — ibuprofeno 200–400 mg una vez con comida.",
                  "3) Ventila la habitación; menos pantallas 30–60 min.","Meta: por la tarde dolor ≤3/10."]}[lang]
    return core + extra + [T[lang]["er_text"]]
PREFERENCES_HEADERS = ["user_id","likes_json","dislikes_json","meal_budget","reminder_preset","last_feedback_date"]
ws_prefs = None
MEM_PREFS: Dict[int, dict] = {}

def _ensure_preferences_sheet():
global ws_prefs
if not SHEETS_ENABLED or not ss:
return
try:
ws_prefs_local = ss.worksheet("Preferences")
except gspread.WorksheetNotFound:
ws_prefs_local = ss.add_worksheet(title="Preferences", rows=1000, cols=max(10, len(PREFERENCES_HEADERS)))
ws_prefs_local.append_row(PREFERENCES_HEADERS)
if not ws_prefs_local.get_all_values():
ws_prefs_local.append_row(PREFERENCES_HEADERS)
ws_prefs = ws_prefs_local

try:
_ensure_preferences_sheet()
except Exception as e:
logging.warning(f"Preferences sheet init skipped: {e}")

def prefs_get(uid: int) -> dict:
if SHEETS_ENABLED and ws_prefs:
try:
vals = ws_prefs.get_all_records(expected_headers=PREFERENCES_HEADERS, default_blank="")
except Exception:
vals = []
for r in vals:
if str(r.get("user_id")) == str(uid):
return r
return {}
return MEM_PREFS.get(uid, {})

def _json_load(s: str) -> dict:
try:
return json.loads(s or "{}")
except Exception:
return {}

def prefs_upsert(uid: int, data: dict):
row = {
"user_id": str(uid),
"likes_json": "{}",
"dislikes_json": "{}",
"meal_budget": "",
"reminder_preset": "",
"last_feedback_date": ""
}
row.update(prefs_get(uid))
for k, v in data.items():
row[k] = v if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False)
if SHEETS_ENABLED and ws_prefs:
all_rows = ws_prefs.get_all_values()
hdr = all_rows[0] if all_rows else PREFERENCES_HEADERS
# найти индекс
idx = None
for i in range(2, len(all_rows) + 1):
if ws_prefs.cell(i, 1).value == str(uid):
idx = i
break
vals = [row.get(h, "") for h in PREFERENCES_HEADERS]
end_col = gsu.rowcol_to_a1(1, len(PREFERENCES_HEADERS)).rstrip("1")
if idx:
ws_prefs.update(range_name=f"A{idx}:{end_col}{idx}", values=[vals])
else:
ws_prefs.append_row(vals)
else:
MEM_PREFS[uid] = row

def prefs_inc_like(uid: int, key: str):
p = prefs_get(uid)
likes = _json_load(p.get("likes_json",""))
likes[key] = int(likes.get(key, 0)) + 1
prefs_upsert(uid, {"likes_json": likes})

def prefs_set_reminder(uid: int, preset: str):
prefs_upsert(uid, {"reminder_preset": preset})

def prefs_set_fb_date(uid: int, ymd: str):
prefs_upsert(uid, {"last_feedback_date": ymd})

-------- Локализация пола/цели + однократный префикс профиля ---------

SEX_LABELS = {
"ru": {"male":"мужчина","female":"женщина","other":"другое"},
"uk": {"male":"чоловік","female":"жінка","other":"інше"},
"en": {"male":"male","female":"female","other":"other"},
"es": {"male":"hombre","female":"mujer","other":"otro"},
}
GOAL_LABELS = {
"ru": {"weight":"похудение","energy":"энергия","sleep":"сон","longevity":"долголетие","strength":"сила"},
"uk": {"weight":"вага","energy":"енергія","sleep":"сон","longevity":"довголіття","strength":"сила"},
"en": {"weight":"weight","energy":"energy","sleep":"sleep","longevity":"longevity","strength":"strength"},
"es": {"weight":"peso","energy":"energía","sleep":"sueño","longevity":"longevidad","strength":"fuerza"},
}

def _profile_hash(prof: dict) -> str:
parts = [str(prof.get("sex","")).lower(), str(prof.get("age","")), str(prof.get("goal","")).lower()]
return "|".join(parts)

def profile_short_label(lang: str, prof: dict) -> str:
lang = norm_lang(lang)
sex = (prof.get("sex") or "").lower()
age = str(prof.get("age") or "").strip()
goal = (prof.get("goal") or prof.get("goals") or "").lower()
sex_lbl = SEX_LABELS.get(lang, SEX_LABELS["en"]).get(sex, sex or "")
goal_lbl = GOAL_LABELS.get(lang, GOAL_LABELS["en"]).get(goal, goal or "")
# требование: RU формат «мужчина, 42; цель — долголетие»
if lang == "ru":
age_part = age if age else "—"
return f"{sex_lbl}, {age_part}; цель — {goal_lbl}".strip(", ;")
if lang == "uk":
age_part = age if age else "—"
return f"{sex_lbl}, {age_part}; мета — {goal_lbl}".strip(", ;")
if lang == "es":
age_part = age if age else "—"
return f"{sex_lbl}, {age_part}; objetivo — {goal_lbl}".strip(", ;")
# en
age_part = f"{age}" if age else "—"
return f"{sex_lbl}, {age_part}; goal — {goal_lbl}".strip(", ;")

Переопределяем personalized_prefix: показывает 1 раз при старте или при изменении

def personalized_prefix(lang: str, profile: dict) -> str:
try:
uid = int(profile.get("user_id") or 0)
except Exception:
uid = 0
if not uid:
return ""
s = sessions.setdefault(uid, {})
curr_hash = _profile_hash(profile)
shown_hash = s.get("px_hash","")
if not s.get("px_shown", False) or shown_hash != curr_hash:
s["px_shown"] = True
s["px_hash"] = curr_hash
# короткая форма
return profile_short_label(lang, profile)
return ""

-------- Тон/стиль LLM: «1 мысль → 1 вопрос», теплее и короче ----------

SYS_ROUTER = (
"You are TendAI — warm, simple, supportive health assistant (not a doctor). "
"Answer strictly in {lang}. Be concise. Prefer: one short thought → one short question. "
"Use friendly tone, avoid medical jargon. Personalize with profile (sex/age/goal/conditions). "
"If giving tips, list up to 3 concrete, ready-to-do options for today. "
"TRIAGE: ask 1–2 clarifiers; ER only for clear red flags. "
"Return JSON ONLY: "
"{"intent":"symptom"|"nutrition"|"sleep"|"labs"|"habits"|"longevity"|"other","
""assistant_reply":"string","followups":["string"],"needs_more":true,"
""red_flags":false,"confidence":0.0}"
)

-------- Ограничение кнопок и пресеты напоминаний (3 шт) ----------

def inline_remind(lang: str) -> InlineKeyboardMarkup:
# оставляем только 3 пресета: +4 ч, вечером, утром
return InlineKeyboardMarkup([
[InlineKeyboardButton(T[lang]["act_rem_4h"], callback_data="rem|4h")],
[InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="rem|evening")],
[InlineKeyboardButton(T[lang]["act_rem_morn"],callback_data="rem|morning")]
])

def inline_actions(lang: str) -> InlineKeyboardMarkup:
# максимум ~6–7 кнопок на экран, короткие подписи
return InlineKeyboardMarkup([
[InlineKeyboardButton(T[lang]["h60_btn"], callback_data="act|h60")],
[InlineKeyboardButton(T[lang]["act_rem_4h"], callback_data="act|rem|4h"),
InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="act|rem|evening"),
InlineKeyboardButton(T[lang]["act_rem_morn"],callback_data="act|rem|morning")],
[InlineKeyboardButton(T[lang]["act_ex_neck"], callback_data="act|ex|neck")],
[InlineKeyboardButton(T[lang]["act_find_lab"], callback_data="act|lab")],
[InlineKeyboardButton(T[lang]["act_er"], callback_data="act|er")]
])

-------- Фидбек: не чаще 1 раза в день ----------

def _today_local_str(uid: int) -> str:
u = users_get(uid)
tz_off = int(str(u.get("tz_offset") or "0"))
return (utcnow() + timedelta(hours=tz_off)).strftime("%Y-%m-%d")

def maybe_ask_feedback(context: ContextTypes.DEFAULT_TYPE, uid: int, lang: str):
# при ключевых блоках (например, сформирован план/варианты) — спросить не чаще 1/день
today = _today_local_str(uid)
p = prefs_get(uid)
last = str(p.get("last_feedback_date") or "")
if last == today:
return
# отправляем и запоминаем день
async def _send():
try:
await context.bot.send_message(uid, T[lang]["ask_fb"], reply_markup=inline_feedback_kb(lang))
prefs_set_fb_date(uid, today)
except Exception as e:
logging.error(f"maybe_ask_feedback send error: {e}")
return _send()

-------- Кулдаун автоспрашиваний (30 минут) ----------

def set_auto_cooldown(uid: int, minutes: int = 30):
sessions.setdefault(uid, {})["next_auto_ask_utc"] = utcnow() + timedelta(minutes=minutes)

def in_auto_cooldown(uid: int) -> bool:
next_ = sessions.setdefault(uid, {}).get("next_auto_ask_utc")
return bool(next_ and utcnow() < next_)

Переопределяем maybe_send: учитываем кулдаун

async def maybe_send(context: ContextTypes.DEFAULT_TYPE, uid: int, text: str, kb=None):
if in_auto_cooldown(uid):
return
if can_send(uid):
try:
await context.bot.send_message(uid, text, reply_markup=kb)
mark_sent(uid)
except Exception as e:
logging.error(f"send fail: {e}")

-------- Автовопросы: мягкие, одно предложение + вопрос ----------

AUTO_QUESTIONS = {
"ru": [
"Как спалось сегодня (часы)?",
"Что было на завтрак?",
"Дать короткий совет — активность или питание?"
],
"en": [
"How did you sleep today (hours)?",
"What did you have for breakfast?",
"Want a quick tip — activity or nutrition?"
],
"uk": [
"Як спалося сьогодні (години)?",
"Що було на сніданок?",
"Дати коротку пораду — активність чи харчування?"
],
"es": [
"¿Cómo dormiste hoy (horas)?",
"¿Qué desayunaste?",
"¿Te doy un tip rápido — actividad o nutrición?"
]
}

async def maybe_auto_question(context: ContextTypes.DEFAULT_TYPE, uid: int, lang: str):
if in_auto_cooldown(uid):
return
qs = AUTO_QUESTIONS.get(lang, AUTO_QUESTIONS["en"])
q = random.choice(qs)
await maybe_send(context, uid, q)
set_auto_cooldown(uid, 30)

-------- Генератор «коротких конкретных вариантов» по еде ----------

def meal_options(lang: str, time_of_day: str = "any", flags: Optional[set] = None) -> List[Tuple[str,str]]:
# возвращаем список (label, key)
flags = flags or set()
RU = [
("Творог 200 г + огурец", "cottage"),
("Омлет 2 яйца + овощи", "omelet"),
("Сардины 1 банка + салат", "sardines"),
("Кефир 250 мл + ягоды", "kefir"),
("Греческий йогурт 200 г + орехи", "yogurt"),
]
EN = [
("Cottage cheese 200g + cucumber", "cottage"),
("2-egg omelet + veggies", "omelet"),
("Sardines (1 can) + salad", "sardines"),
("Kefir 250ml + berries", "kefir"),
("Greek yogurt 200g + nuts", "yogurt"),
]
UK = [
("Творог 200 г + огірок", "cottage"),
("Омлет 2 яйця + овочі", "omelet"),
("Сардини 1 банка + салат", "sardines"),
("Кефір 250 мл + ягоди", "kefir"),
("Грецький йогурт 200 г + горіхи", "yogurt"),
]
ES = [
("Requesón 200g + pepino", "cottage"),
("Tortilla 2 huevos + verduras", "omelet"),
("Sardinas (1 lata) + ensalada", "sardines"),
("Kéfir 250ml + frutos rojos", "kefir"),
("Yogur griego 200g + nueces", "yogurt"),
]
base = {"ru": RU, "en": EN, "uk": UK, "es": ES}.get(lang, EN)
# простая фильтрация по времени
if time_of_day == "breakfast":
base = [x for x in base if x[1] in {"cottage","omelet","yogurt","kefir"}]
if "fish_free" in flags:
base = [x for x in base if x[1] != "sardines"]
random.shuffle(base)
return base[:3]

def meal_kb(opts: List[Tuple[str,str]]) -> InlineKeyboardMarkup:
rows = [[InlineKeyboardButton(lbl, callback_data=f"food|pick|{key}")] for (lbl,key) in opts]
rows.append([InlineKeyboardButton("◀", callback_data="food|exit")])
return InlineKeyboardMarkup(rows)

-------- Персонализирующий уточняющий вопрос (мысль → вопрос) ----------

def personalize_followup(lang: str, text: str) -> Optional[Tuple[str, str]]:
low = text.lower()
if any(k in low for k in ["белка","белок","protein","протеїн"]):
return ({"ru":"Окей 👍. Для завтрака или ужина?",
"uk":"Окей 👍. Для сніданку чи вечері?",
"en":"Got it 👍. For breakfast or dinner?",
"es":"Vale 👍. ¿Para desayuno o cena?"}[lang], "food|time")
if any(k in low for k in ["сон","sleep"]):
return ({"ru":"Хочешь совет про засыпание или про утро?",
"uk":"Порада про засинання чи про ранок?",
"en":"Want a tip for falling asleep or morning routine?",
"es":"¿Un tip para conciliar el sueño o la mañana?"}[lang], "sleep|focus")
return None

-------- Job: утро/вечер + авто-вопросы (переопределяем ещё раз) ----------

async def job_daily_checkin(context: ContextTypes.DEFAULT_TYPE):
d = context.job.data or {}
uid, lang = d.get("user_id"), d.get("lang","en")
u = users_get(uid)
if (u.get("paused") or "").lower()=="yes":
return
# настроение — мягко
kb = InlineKeyboardMarkup([
[InlineKeyboardButton(T[lang]["mood_good"], callback_data="mood|good"),
InlineKeyboardButton(T[lang]["mood_ok"], callback_data="mood|ok"),
InlineKeyboardButton(T[lang]["mood_bad"], callback_data="mood|bad")],
[InlineKeyboardButton(T[lang]["mood_note"], callback_data="mood|note")]
])
await maybe_send(context, uid, T[lang]["daily_gm"], kb)

# краткие советы по питанию из Rules
prof = profiles_get(uid)
tips = pick_nutrition_tips(lang, prof, limit=2)
if tips:
    await maybe_send(context, uid, "• " + "\n• ".join(tips))

# совет по циклу (если включён)
phase = cycle_phase_for(uid)
if phase:
    tip = cycle_tip(lang, phase)
    if tip:
        await maybe_send(context, uid, tip)

# один авто-вопрос
await maybe_auto_question(context, uid, lang)


|python

-------- OnText: короче/теплее, 1 мысль → 1 вопрос, 3 варианта ----------

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
user = update.effective_user; uid = user.id
text = (update.message.text or "").strip()
logging.info(f"INCOMING uid={uid} text={text[:200]}")

urec = users_get(uid)
if not urec:
    lang_guess = detect_lang_from_text(text, norm_lang(getattr(user, "language_code", None)))
    users_upsert(uid, user.username or "", lang_guess)
    sessions.setdefault(uid, {})["last_user_text"] = text
    await update.message.reply_text(T[lang_guess]["welcome"], reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text({"ru":"Питание, сон или активность?",
                                     "uk":"Харчування, сон чи активність?",
                                     "en":"Nutrition, sleep or activity?",
                                     "es":"Nutrición, sueño o actividad?"}[lang_guess],
                                    reply_markup=inline_topic_kb(lang_guess))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang_guess]["yes"], callback_data="consent|yes"),
                                InlineKeyboardButton(T[lang_guess]["no"],  callback_data="consent|no")]])
    await update.message.reply_text(T[lang_guess]["ask_consent"], reply_markup=kb)
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, uid, 0, DEFAULT_CHECKIN_LOCAL, lang_guess)
        schedule_morning_evening(context.application, uid, 0, lang_guess)
    await gate_show(update, context)
    return

saved_lang = norm_lang(urec.get("lang") or getattr(user,"language_code",None))
detected_lang = detect_lang_from_text(text, saved_lang)
if detected_lang != saved_lang:
    users_set(uid,"lang",detected_lang)
lang = detected_lang
sessions.setdefault(uid, {})["last_user_text"] = text

# быстрые режимы
if sessions.get(uid, {}).get("awaiting_daily_comment"):
    daily_add(iso(utcnow()), uid, "note", text)
    sessions[uid]["awaiting_daily_comment"] = False
    set_auto_cooldown(uid, 30)
    await update.message.reply_text(T[lang]["mood_thanks"]); return

if sessions.get(uid, {}).get("awaiting_free_feedback"):
    sessions[uid]["awaiting_free_feedback"] = False
    feedback_add(iso(utcnow()), uid, "free", user.username, "", text)
    await update.message.reply_text(T[lang]["fb_thanks"]); return

if sessions.get(uid, {}).get("awaiting_city"):
    sessions[uid]["awaiting_city"] = False
    set_auto_cooldown(uid, 30)
    await update.message.reply_text(T[lang]["thanks"]); return

# Health60 быстрый режим
if sessions.get(uid, {}).get("awaiting_h60"):
    sessions[uid]["awaiting_h60"] = False
    prof = profiles_get(uid)
    px_once = personalized_prefix(lang, {**prof, "user_id": str(uid)})
    plan = health60_make_plan(lang, text, prof) if 'health60_make_plan' in globals() else T[lang]["unknown"]
    msg_head = (px_once + "\n") if px_once else ""
    await update.message.reply_text(msg_head + str(plan), reply_markup=inline_actions(lang))
    await maybe_ask_feedback(context, uid, lang)
    set_auto_cooldown(uid, 30)
    return

# профиль (ручной ввод шага)
if sessions.get(uid, {}).get("p_wait_key"):
    key = sessions[uid]["p_wait_key"]; sessions[uid]["p_wait_key"] = None
    val = text
    if key=="age":
        m = re.search(r'\d{2,3}', text)
        if m: val = m.group(0)
    profiles_upsert(uid,{key:val}); sessions[uid][key]=val
    await advance_profile_ctx(context, update.effective_chat.id, lang, uid); return

# серьёзные ключевые слова
sc = detect_serious(text)
if sc:
    sessions.setdefault(uid,{})["mode"] = "serious"
    sessions[uid]["serious_condition"] = sc
    prof = profiles_get(uid)
    px_once = personalized_prefix(lang, {**prof, "user_id": str(uid)})
    plan = pain_plan(lang, [], prof)
    msg = (px_once + "\n" if px_once else "") + "\n".join(plan)
    await update.message.reply_text(msg, reply_markup=inline_actions(lang))
    await maybe_ask_feedback(context, uid, lang)
    set_auto_cooldown(uid, 30)
    return

# «мысль → вопрос» персонализатор (питание/сон)
tfu = personalize_followup(lang, text)
if tfu:
    q, code = tfu
    sessions.setdefault(uid,{})["route"] = code
    set_auto_cooldown(uid, 30)
    # питание: предложить выбор времени
    if code == "food|time":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton({"ru":"Завтрак","uk":"Сніданок","en":"Breakfast","es":"Desayuno"}[lang], callback_data="food|time|breakfast"),
             InlineKeyboardButton({"ru":"Ужин","uk":"Вечеря","en":"Dinner","es":"Cena"}[lang], callback_data="food|time|dinner")],
            [InlineKeyboardButton(T[lang]["back"], callback_data="food|exit")]
        ])
        await update.message.reply_text(q, reply_markup=kb); return
    # сон: фокус
    if code == "sleep|focus":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton({"ru":"Засыпание","uk":"Засинання","en":"Falling asleep","es":"Conciliar el sueño"}[lang], callback_data="sleep|focus|night"),
             InlineKeyboardButton({"ru":"Утро","uk":"Ранок","en":"Morning","es":"Mañana"}[lang], callback_data="sleep|focus|morning")],
            [InlineKeyboardButton(T[lang]["back"], callback_data="sleep|exit")]
        ])
        await update.message.reply_text(q, reply_markup=kb); return

# стандартный роутер LLM → короткий ответ + 1 вопрос
prof = profiles_get(uid)
data = llm_router_answer(text, lang, prof)
px_once = personalized_prefix(lang, {**prof, "user_id": str(uid)})
head = (px_once + "\n") if px_once else ""
reply = head + (data.get("assistant_reply") or T[lang]["unknown"])
await update.message.reply_text(reply, reply_markup=inline_actions(lang))
# добавим один уточняющий вопрос из followups, если есть
fups = (data.get("followups") or [])
if fups:
    await send_unique(update.message, uid, fups[0], force=True)
await maybe_ask_feedback(context, uid, lang)
set_auto_cooldown(uid, 30)
return

-------- Callbacks: питание/сон/фидбек/напоминания ----------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
q = update.callback_query; await q.answer()
data = (q.data or ""); uid = q.from_user.id
lang = norm_lang(users_get(uid).get("lang") or "en")
chat_id = q.message.chat.id

if data.startswith("gate:"):
    await gate_cb(update, context); return

# профиль — как было в основе
if data.startswith("p|"):
    _, action, key, *rest = data.split("|")
    s = sessions.setdefault(uid, {"profile_active": True, "p_step": 0})
    if action == "choose":
        value = "|".join(rest) if rest else ""
        s[key] = value; profiles_upsert(uid, {key: value})
        # сбросим показ префикса на случай изменения
        s["px_shown"] = False
        await advance_profile_ctx(context, chat_id, lang, uid); return
    if action == "write":
        s["p_wait_key"] = key
        await q.message.reply_text({"ru":"Напишите короткий ответ:","uk":"Напишіть коротко:",
                                    "en":"Type your answer:","es":"Escribe tu respuesta:"}[lang]); return
    if action == "skip":
        profiles_upsert(uid, {key: ""})
        await advance_profile_ctx(context, chat_id, lang, uid); return

if data.startswith("consent|"):
    users_set(uid, "consent", "yes" if data.endswith("|yes") else "no")
    try: await q.edit_message_reply_markup(reply_markup=None)
    except: pass
    await q.message.reply_text(T[lang]["thanks"]); return

if data.startswith("mood|"):
    mood = data.split("|",1)[1]
    if mood=="note":
        sessions.setdefault(uid,{})["awaiting_daily_comment"] = True
        await q.message.reply_text({"ru":"Короткий комментарий:","uk":"Короткий коментар:",
                                    "en":"Short note:","es":"Nota corta:"}[lang]); return
    daily_add(iso(utcnow()), uid, mood, ""); await q.message.reply_text(T[lang]["mood_thanks"]); return

# быстрый выбор темы из меню
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
    px_once = personalized_prefix(lang, {**prof, "user_id": str(uid)})
    reply = ((px_once + "\n") if px_once else "") + (data_llm.get("assistant_reply") or T[lang]["unknown"])
    await q.message.reply_text(reply, reply_markup=inline_actions(lang))
    fups = (data_llm.get("followups") or [])
    if fups:
        await send_unique(q.message, uid, fups[0], force=True)
    await maybe_ask_feedback(context, uid, lang)
    return

# pain triage — как в основе
s = sessions.setdefault(uid, {})
if data == "pain|exit":
    sessions.pop(uid, None)
    await q.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang)); return
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
    px_once = personalized_prefix(lang, {**profiles_get(uid), "user_id": str(uid)})
    text_plan = (px_once + "\n" if px_once else "") + f"{T[lang]['plan_header']}\n" + "\n".join(plan_lines)
    await q.message.reply_text(text_plan)
    await q.message.reply_text(T[lang]["plan_accept"], reply_markup=inline_accept(lang))
    await maybe_ask_feedback(context, uid, lang)
    s["step"] = 6; return
if data.startswith("acc|"):
    accepted = "1" if data.endswith("|yes") else "0"
    if s.get("episode_id"): episode_set(s["episode_id"], "plan_accepted", accepted)
    await q.message.reply_text(T[lang]["remind_when"], reply_markup=inline_remind(lang))
    s["step"] = 7; return
if data.startswith("rem|"):
    choice = data.split("|",1)[1]
    hours = {"4h":4, "evening":6, "morning":16}.get(choice)
    if hours and s.get("episode_id"):
        next_time = utcnow() + timedelta(hours=hours)
        episode_set(s["episode_id"], "next_checkin_at", iso(next_time))
        if _has_jq_ctx(context):
            context.application.job_queue.run_once(job_checkin_episode, when=hours*3600,
                                                   data={"user_id":uid,"episode_id":s["episode_id"]})
    await q.message.reply_text(T[lang]["thanks"], reply_markup=inline_topic_kb(lang))
    sessions.pop(uid, None); return

# Питание: выбор времени/опций/выбора
if data.startswith("food|exit"):
    sessions.pop(uid, None)
    await q.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang)); return

if data.startswith("food|time|"):
    tod = data.split("|",2)[2]  # breakfast/dinner
    sessions.setdefault(uid,{})["meal_tod"] = tod
    # флаги из профиля (упрощённо)
    prof = profiles_get(uid)
    flags = set()
    if "рыба" in (prof.get("diet","")+prof.get("diet_focus","")) and "не ем" in prof.get("diet",""):
        flags.add("fish_free")
    opts = meal_options(lang, time_of_day=tod, flags=flags)
    kb = meal_kb(opts)
    head = {"ru":"Под тебя подойдёт на сегодня:",
            "uk":"Підійде на сьогодні:",
            "en":"Good picks for today:",
            "es":"Opciones para hoy:"}[lang]
    await q.message.reply_text(head + "\n" + "\n".join([f"• {o[0]}" for o in opts]), reply_markup=kb)
    await maybe_ask_feedback(context, uid, lang)
    return

if data.startswith("food|pick|"):
    key = data.split("|",2)[2]
    prefs_inc_like(uid, key)
    # запишем эпизод-лог
    try:
        _ = episode_create(uid, f"food:{key}", 0, "none")
    except Exception:
        pass
    done = {"ru":"Принято. Запомню вкус и подстрою дальше. Поставить напоминание на вечер?",
            "uk":"Прийнято. Запам'ятаю смак і підлаштую далі. Поставити нагадування на вечір?",
            "en":"Nice. I’ll remember this and adjust next time. Set an evening reminder?",
            "es":"Hecho. Lo recordaré y ajustaré. ¿Ponemos recordatorio para la tarde?"}[lang]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="act|rem|evening")],
        [InlineKeyboardButton(T[lang]["back"], callback_data="food|exit")]
    ])
    await q.message.reply_text(done, reply_markup=kb)
    return

# Сон: два компактных фокуса
if data.startswith("sleep|exit"):
    sessions.pop(uid, None)
    await q.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang)); return

if data.startswith("sleep|focus|"):
    which = data.split("|",2)[2]  # night/morning
    if which == "night":
        tips = {
            "ru":["90 мин без экрана","тёплый душ 5–7 мин","проветрить/прохладная"],
            "uk":["90 хв без екрана","теплий душ 5–7 хв","провітрити/прохолодно"],
            "en":["90 min no screens","warm shower 5–7 min","cool, aired room"],
            "es":["90 min sin pantallas","ducha tibia 5–7 min","habitación fresca"]
        }[lang]
    else:
        tips = {
            "ru":["вода 300–500 мл","10 мин быстрая ходьба","свет/окно 5 мин"],
            "uk":["вода 300–500 мл","10 хв швидка ходьба","світло/вікно 5 хв"],
            "en":["300–500 ml water","10-min brisk walk","bright light 5 min"],
            "es":["300–500 ml de agua","camina 10 min","luz brillante 5 min"]
        }[lang]
    txt = "• " + "\n• ".join(tips) + "\n" + {
        "ru":"Напомнить вечером?",
        "uk":"Нагадати ввечері?",
        "en":"Remind in the evening?",
        "es":"¿Recordar por la tarde?"
    }[lang]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["act_rem_eve"], callback_data="act|rem|evening")],
        [InlineKeyboardButton(T[lang]["back"], callback_data="sleep|exit")]
    ])
    await q.message.reply_text(txt, reply_markup=kb)
    await maybe_ask_feedback(context, uid, lang)
    return

# Быстрые действия
if data.startswith("act|"):
    parts = data.split("|"); kind = parts[1]
    if kind=="h60":
        sessions.setdefault(uid,{})["awaiting_h60"] = True
        await q.message.reply_text(T[lang]["h60_intro"]); return
    if kind=="rem":
        key = parts[2]; hours = {"4h":4, "evening":6, "morning":16}.get(key,4)
        when_ = utcnow() + timedelta(hours=hours)
        rid = reminder_add(uid, T[lang]["thanks"], when_)
        if _has_jq_ctx(context):
            context.application.job_queue.run_once(job_oneoff_reminder, when=hours*3600,
                                                   data={"user_id":uid,"reminder_id":rid})
        await q.message.reply_text(T[lang]["thanks"]); return
    if kind=="ex":
        txt = {"ru":"🧘 5 минут шея: наклоны вперёд/назад ×5; повороты ×5; полукруги ×5; лёгкая растяжка 2×20с.",
               "uk":"🧘 5 хв шия: нахили вперед/назад ×5; повороти ×5; півкола ×5; легка розтяжка 2×20с.",
               "en":"🧘 Neck 5 min: flex/extend ×5; rotations ×5; chin semicircles ×5; gentle stretch 2×20s.",
               "es":"🧘 Cuello 5 min: flex/ext ×5; giros ×5; semicírculos ×5; estiramiento 2×20s."}[lang]
        await q.message.reply_text(txt); return
    if kind=="lab":
        sessions.setdefault(uid,{})["awaiting_city"] = True
        await q.message.reply_text(T[lang]["act_city_prompt"]); return
    if kind=="er":
        await q.message.reply_text(T[lang]["er_text"]); return

if data.startswith("fb|"):
    sub = data.split("|",1)[1]
    if sub == "up":
        feedback_add(iso(utcnow()), uid, "feedback_yes", q.from_user.username, 1, "")
        await q.message.reply_text(T[lang]["fb_thanks"]); return
    if sub == "down":
        feedback_add(iso(utcnow()), uid, "feedback_no",  q.from_user.username, 0, "")
        await q.message.reply_text(T[lang]["fb_thanks"]); return
    if sub == "text":
        sessions.setdefault(uid,{})["awaiting_free_feedback"] = True
        await q.message.reply_text(T[lang]["fb_write"]); return
