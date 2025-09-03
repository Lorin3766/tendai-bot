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

# === PRO-INTAKE (обновлённая интеграция) ===
try:
    from intake_pro import register_intake_pro, intake_entry_button  # <-- новый API
except Exception:
    register_intake_pro = None
    intake_entry_button = None

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
    # ru/uk/es — см. длинный блок i18n как в предыдущей версии (оставляем без изменений)
}
T["ru"] = json.loads(json.dumps(T["en"], ensure_ascii=False))  # заглушка, ниже заменим реальным RU-блоком
T["uk"] = json.loads(json.dumps(T["en"], ensure_ascii=False))
T["es"] = json.loads(json.dumps(T["en"], ensure_ascii=False))
# --- здесь подмените RU/UK/ES полноценными словарями из предыдущей версии (опущено в краткости ответа) ---

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
    # используем intake_pro кнопку, чтобы callback был "ipro:start"
    entry_btn = intake_entry_button("🧩 Пройти опрос (40–60 сек)" if lang!="en" else "🧩 Take the 40–60s intake") \
                if intake_entry_button else \
                InlineKeyboardButton("🧩 Intake", callback_data="ipro:start")
    kb = [
        [entry_btn],
        [InlineKeyboardButton("➡️ Позже — показать меню" if lang!="en" else "➡️ Later — open menu",
                              callback_data="gate:skip")],
    ]
    text = ("Чтобы советы были точнее, пройдите короткий опрос. Можно пропустить и сделать позже."
            if lang!="en" else
            "To personalize answers, please take a short intake. You can skip and do it later.")
    await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(kb))

async def gate_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if (q.data or "") == "gate:skip":
        context.user_data[GATE_FLAG_KEY] = True
        await q.edit_message_text("Ок, открываю меню…" if context.user_data.get("lang","en")!="en" else "OK, opening the menu…")
        await context.bot.send_message(q.message.chat_id, T[_user_lang(q.from_user.id)]["start_where"],
                                       reply_markup=inline_topic_kb(_user_lang(q.from_user.id)))

# === MINI-INTAKE (можно оставить как «с первого сообщения») ===
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

# === сохраним клиент для возможных внешних модулей ===
GSPREAD_CLIENT: Optional[gspread.client.Client] = None

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
    global GSPREAD_CLIENT
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
    last_d = None
    if last_date:
        try:
            last_d = datetime.strptime(last_date, "%Y-%m-%d").date()
        except:
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
    if not getattr(app, "job_queue", None):
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
    if not getattr(app, "job_queue", None):
        logging.warning(f"JobQueue not available – skip daily scheduling for uid={uid}.")
        return
    for j in app.job_queue.get_jobs_by_name(f"daily_{uid}"):
        j.schedule_removal()
    h_utc, m_utc = local_to_utc_hour_min(tz_off, hhmm_local)
    t = dtime(hour=h_utc, minute=m_utc, tzinfo=timezone.utc)
    app.job_queue.run_daily(job_daily_checkin, time=t, name=f"daily_{uid}", data={"user_id":uid,"lang":lang})

def schedule_evening_checkin(app, uid:int, tz_off:int, hhmm_local:str, lang:str):
    if not getattr(app, "job_queue", None):
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
# ---------- UI helpers ----------
def inline_numbers_0_10() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for i in range(0, 11):
        row.append(InlineKeyboardButton(str(i), callback_data=f"score:{i}"))
        if len(row) == 6:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def inline_yesno(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(T[lang]["yes"], callback_data="yn:yes"),
        InlineKeyboardButton(T[lang]["no"],  callback_data="yn:no"),
    ]])

def inline_topic_kb(lang: str) -> InlineKeyboardMarkup:
    # Главное меню (можно расширять)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["h60_btn"], callback_data="topic:health60")],
        [InlineKeyboardButton("💧 " + T[lang]["hydrate_btn"], callback_data="topic:hydrate"),
         InlineKeyboardButton("⚡ " + T[lang]["gm_energy"], callback_data="topic:energy")],
        [InlineKeyboardButton("📝 " + T[lang]["fb_free"], callback_data="fb:free")],
        [InlineKeyboardButton("🧩 Intake", callback_data="ipro:start")]  # дубликат входа в PRO-опрос
    ])

# ---------- LLM helpers ----------
def _oai_chat(messages: List[Dict[str, str]], temperature=0.4, max_tokens=650) -> str:
    if not oai:
        return "AI is temporarily unavailable. I’ll reply without model."
    try:
        # OpenAI SDK v1
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logging.error(f"OpenAI error: {e}")
        return "I couldn’t generate a smart reply just now."

def llm_general_reply(lang: str, user_text: str, profile: dict) -> str:
    px = personalized_prefix(lang, profile)
    sys = (
        "You are TendAI, a friendly, concise health & longevity assistant. "
        "Be practical, evidence-based, and avoid medical diagnosis. "
        "Prefer 24–48h self-care steps, red flags, and when to seek care. "
        "Use short lists. Keep tone calm and supportive."
    )
    user = f"{px}\nUser: {user_text}".strip()
    out = _oai_chat([
        {"role": "system", "content": sys},
        {"role": "user", "content": user}
    ])
    if not out:
        out = "Let's try simple steps first: hydrate, gentle movement, rest, and monitor symptoms."
    return out

def llm_health60(lang: str, user_text: str, profile: dict) -> str:
    px = personalized_prefix(lang, profile)
    sys = (
        "You are TendAI. Produce a compact 'Health in 60s' reply in the user's language. "
        "Structure with three titled sections using short bullet points:\n"
        "1) Possible causes\n2) Do now (24–48h)\n3) When to see a doctor\n"
        "Add a very brief 'Serious to rule out' only if relevant. No fluff."
    )
    prompt = f"{px}\nTopic: {user_text}\nLanguage: {lang}"
    out = _oai_chat([
        {"role": "system", "content": sys},
        {"role": "user", "content": prompt}
    ], temperature=0.2, max_tokens=500)
    if not out:
        out = (
            f"{T[lang]['h60_t1']}:\n• Stress, dehydration.\n"
            f"{T[lang]['h60_t2']}:\n• Water, rest, light walk.\n• Gentle stretching.\n"
            f"{T[lang]['h60_t3']}:\n• Severe/worsening pain, high fever, red flags."
        )
    return out

# ---------- Daily & evening jobs ----------
async def job_daily_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, lang = d.get("user_id"), d.get("lang", "en")
    if not uid:
        return
    u = users_get(uid)
    if (u.get("paused") or "").lower() == "yes":
        return
    try:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(T[lang]["mood_good"], callback_data="mood:good"),
            InlineKeyboardButton(T[lang]["mood_ok"],   callback_data="mood:ok"),
            InlineKeyboardButton(T[lang]["mood_bad"],  callback_data="mood:bad"),
        ], [
            InlineKeyboardButton(T[lang]["mood_note"], callback_data="mood:note"),
            InlineKeyboardButton(T[lang]["gm_evening_btn"], callback_data="set:rem_eve")
        ]])
        await context.bot.send_message(uid, T[lang]["daily_gm"], reply_markup=kb)
        inc_auto(uid)
    except Exception as e:
        logging.error(f"job_daily_checkin send error: {e}")

async def job_evening_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, lang = d.get("user_id"), d.get("lang", "en")
    if not uid:
        return
    try:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(T[lang]["evening_tip_btn"], callback_data="tip:daily")
        ]])
        await context.bot.send_message(uid, T[lang]["evening_intro"], reply_markup=kb)
    except Exception as e:
        logging.error(f"job_evening_checkin send error: {e}")

# ---------- Mini-intake callbacks ----------
async def mini_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    lang = _user_lang(uid)
    data = (q.data or "")
    parts = data.split("|")
    if len(parts) < 2 or parts[0] != "mini":
        return
    action = parts[1]
    s = sessions.setdefault(uid, {})
    if action == "choose" and len(parts) == 4:
        key, val = parts[2], parts[3]
        mini_handle_choice(uid, key, val)
        await ask_next_mini(context, q.message.chat_id, lang, uid)
    elif action == "skip" and len(parts) == 3:
        # просто двигаем шаг
        s.setdefault("mini_step", 0)
        s["mini_step"] = int(s["mini_step"]) + 1
        await ask_next_mini(context, q.message.chat_id, lang, uid)
    elif action == "write" and len(parts) == 3:
        key = parts[2]
        s["mini_wait_key"] = key
        await q.edit_message_text((T[lang]["write"] + "…"))
    else:
        await q.edit_message_text(T[lang]["unknown"])

# ---------- Generic callbacks ----------
async def generic_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    lang = _user_lang(uid)
    data = q.data or ""

    # Mood & energy
    if data.startswith("mood:"):
        mood = data.split(":")[1]
        ts = iso(utcnow())
        daily_add(ts, uid, mood=mood, comment="")
        await q.edit_message_text(T[lang]["mood_thanks"])
        return

    if data.startswith("score:"):
        # шкала 0–10 (для чек-ина эпизода)
        try:
            score = int(data.split(":")[1])
        except:
            score = -1
        ts = iso(utcnow())
        daily_add(ts, uid, mood=f"score_{score}", comment="")
        await q.edit_message_text(T[lang]["thanks"])
        return

    # Tips & buttons
    if data == "tip:daily":
        await q.edit_message_text(T[lang]["daily_tip_prefix"] + " Focus on 7–8h sleep, steps, protein, and hydration.")
        return

    if data == "set:rem_eve":
        users_set(uid, "evening_hour", DEFAULT_EVENING_LOCAL)
        schedule_evening_checkin(context.application, uid, _user_tz_off(uid), DEFAULT_EVENING_LOCAL, lang)
        await q.edit_message_text(T[lang]["evening_set"].format(t=DEFAULT_EVENING_LOCAL))
        return

    # Topics
    if data == "topic:health60":
        sessions.setdefault(uid, {})["h60_mode"] = True
        await q.edit_message_text(T[lang]["h60_intro"])
        return

    if data == "topic:hydrate":
        await q.edit_message_text(T[lang]["hydrate_nudge"])
        return

    if data == "topic:energy":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("1", callback_data="energy:1"),
            InlineKeyboardButton("2", callback_data="energy:2"),
            InlineKeyboardButton("3", callback_data="energy:3"),
            InlineKeyboardButton("4", callback_data="energy:4"),
            InlineKeyboardButton("5", callback_data="energy:5"),
        ]])
        await q.edit_message_text(T[lang]["gm_energy_q"], reply_markup=kb)
        return

    if data.startswith("energy:"):
        try:
            val = int(data.split(":")[1])
        except:
            val = None
        daily_add(iso(utcnow()), uid, mood="energy", comment="", energy=val)
        await q.edit_message_text(T[lang]["gm_energy_done"])
        return

    # Feedback
    if data == "fb:free":
        sessions.setdefault(uid, {})["fb_wait"] = True
        await q.edit_message_text(T[lang]["fb_write"])
        return

# ---------- Commands ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # попытка определить язык по имени/био бесполезна — ставим 'en' по умолчанию, поправим далее
    lang = context.user_data.get("lang") or "en"
    users_upsert(user.id, user.username or "", lang)
    update_last_seen(user.id)

    # если профиль пустой — предлагаем шторку
    if not _is_menu_unlocked(update, context):
        await gate_show(update, context)
        # параллельно можем запустить мини-опрос
        await start_mini_intake(context, update.effective_chat.id, lang, user.id)
        return

    await update.message.reply_text(T[lang]["start_where"], reply_markup=inline_topic_kb(lang))

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _user_lang(update.effective_user.id)
    await update.message.reply_text(T[lang]["help"])

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _user_lang(update.effective_user.id)
    await update.message.reply_text(T[lang]["privacy"])

async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /en /ru /uk /es
    uid = update.effective_user.id
    cmd = (update.message.text or "").strip().lstrip("/").lower()
    if cmd in SUPPORTED:
        users_set(uid, "lang", cmd)
        context.user_data["lang"] = cmd
        await update.message.reply_text("OK", reply_markup=inline_topic_kb(cmd))
    else:
        await update.message.reply_text("Language not supported.")

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    prof = profiles_get(uid) or {}
    # компактный вывод
    keys_show = ["sex","age","goal","diet_focus","steps_target","conditions","meds","allergies","sleep","activity"]
    lines = [f"{k}: {prof.get(k,'')}" for k in keys_show if prof.get(k)]
    txt = (T[lang]["saved_profile"] + "\n" + "\n".join(lines)) if lines else "No profile yet."
    # кнопка входа в PRO-интейк
    if intake_entry_button:
        kb = InlineKeyboardMarkup([[intake_entry_button("🧩 Update intake")]])
    else:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🧩 Intake", callback_data="ipro:start")]])
    await update.message.reply_text(txt, reply_markup=kb)

async def cmd_checkin_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    hhmm = DEFAULT_CHECKIN_LOCAL
    parts = (update.message.text or "").split()
    if len(parts) >= 2 and re.match(r"^\d{1,2}:\d{2}$", parts[1]):
        hhmm = parts[1]
    users_set(uid, "checkin_hour", hhmm)
    schedule_daily_checkin(context.application, uid, _user_tz_off(uid), hhmm, lang)
    await update.message.reply_text(f"Daily check-in set to {hhmm} (local).")

async def cmd_checkin_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    for j in context.application.job_queue.get_jobs_by_name(f"daily_{uid}"):
        j.schedule_removal()
    users_set(uid, "checkin_hour", "")
    await update.message.reply_text("Daily check-in disabled.")

async def cmd_evening_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    hhmm = DEFAULT_EVENING_LOCAL
    parts = (update.message.text or "").split()
    if len(parts) >= 2 and re.match(r"^\d{1,2}:\d{2}$", parts[1]):
        hhmm = parts[1]
    users_set(uid, "evening_hour", hhmm)
    schedule_evening_checkin(context.application, uid, _user_tz_off(uid), hhmm, lang)
    await update.message.reply_text(T[lang]["evening_set"].format(t=hhmm))

async def cmd_evening_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    for j in context.application.job_queue.get_jobs_by_name(f"evening_{uid}"):
        j.schedule_removal()
    users_set(uid, "evening_hour", "")
    await update.message.reply_text(T[_user_lang(uid)]["evening_off"])

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "yes")
    await update.message.reply_text(T[_user_lang(uid)]["paused_on"])

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "no")
    await update.message.reply_text(T[_user_lang(uid)]["paused_off"])

async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    parts = (update.message.text or "").split()
    try:
        off = int(parts[1])
    except Exception:
        await update.message.reply_text("Use: /settz +2 or /settz -5")
        return
    users_set(uid, "tz_offset", str(off))
    # перескедулим
    u = users_get(uid)
    lang = _user_lang(uid)
    hhmm = u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL
    if hhmm:
        schedule_daily_checkin(context.application, uid, off, hhmm, lang)
    eh = u.get("evening_hour") or ""
    if eh:
        schedule_evening_checkin(context.application, uid, off, eh, lang)
    await update.message.reply_text(f"Timezone offset set to {off} hours.")

async def cmd_health60(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = _user_lang(uid)
    sessions.setdefault(uid, {})["h60_mode"] = True
    await update.message.reply_text(T[lang]["h60_intro"])

# ---------- Text handler ----------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    lang = detect_lang_from_text(text, _user_lang(uid))
    context.user_data["lang"] = lang
    users_set(uid, "lang", lang)
    update_last_seen(uid)

    s = sessions.setdefault(uid, {})

    # 1) Ожидание свободного текста для mini-intake
    if s.get("mini_wait_key"):
        key = s.pop("mini_wait_key")
        s.setdefault("mini_answers", {})[key] = text
        s["mini_step"] = int(s.get("mini_step", 0)) + 1
        await ask_next_mini(context, update.effective_chat.id, lang, uid)
        return

    # 2) Ожидание свободного текста для feedback
    if s.get("fb_wait"):
        s.pop("fb_wait", None)
        feedback_add(iso(utcnow()), uid, update.effective_user.full_name, update.effective_user.username, "free", text)
        await update.message.reply_text(T[lang]["fb_thanks"])
        return

    # 3) Health60 режим
    if s.get("h60_mode"):
        s["h60_mode"] = False
        prof = profiles_get(uid) or {}
        reply = llm_health60(lang, text, prof)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(T[lang]["fb_good"], callback_data="fb:good"),
            InlineKeyboardButton(T[lang]["fb_bad"],  callback_data="fb:bad"),
            InlineKeyboardButton(T[lang]["fb_free"], callback_data="fb:free"),
        ]])
        await update.message.reply_text(reply, reply_markup=kb)
        return

    # 4) Обычный LLM ответ
    if not is_duplicate_question(uid, text):
        prof = profiles_get(uid) or {}
        answer = llm_general_reply(lang, text, prof)
        await update.message.reply_text(answer)
    else:
        await update.message.reply_text("↻ Already asked. Please rephrase or add details.")

# ---------- Feedback tiny callbacks via text buttons ----------
async def fb_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    lang = _user_lang(uid)
    data = q.data or ""
    if data == "fb:good":
        feedback_add(iso(utcnow()), uid, q.from_user.full_name, q.from_user.username, "good", "")
        await q.edit_message_text(T[lang]["fb_thanks"])
    elif data == "fb:bad":
        feedback_add(iso(utcnow()), uid, q.from_user.full_name, q.from_user.username, "bad", "")
        await q.edit_message_text(T[lang]["fb_thanks"])

# ---------- Intake PRO integration ----------
def _safe_dict(x):
    return x if isinstance(x, dict) else {}

async def _ipro_on_complete(uid: int, answers: Dict[str, Any], update: Optional[Update]=None, context: Optional[ContextTypes.DEFAULT_TYPE]=None):
    """Колбэк, который вызывает intake_pro после успешного завершения опроса."""
    try:
        profiles_upsert(uid, _safe_dict(answers))
    except Exception as e:
        logging.error(f"ipro save error: {e}")
    # Разблокируем меню
    if context:
        context.user_data[GATE_FLAG_KEY] = True
        lang = _user_lang(uid)
        try:
            chat_id = update.effective_chat.id if update else uid
            await context.bot.send_message(chat_id, T[lang]["start_where"], reply_markup=inline_topic_kb(lang))
        except Exception as e:
            logging.error(f"ipro follow-up send error: {e}")

# ---------- Main ----------
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # === Подключаем PRO-опросник раньше общих хэндлеров (перехват текста) ===
    if register_intake_pro:
        try:
            register_intake_pro(
                app=app,
                gclient=GSPREAD_CLIENT,
                ws_profiles=ws_profiles if SHEETS_ENABLED else None,
                on_complete_cb=_ipro_on_complete,
            )
            logging.info("intake_pro registered.")
        except Exception as e:
            logging.error(f"register_intake_pro failed: {e}")

    # --- Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("profile", cmd_profile))

    app.add_handler(CommandHandler(["en","ru","uk","es"], cmd_lang))
    app.add_handler(CommandHandler("checkin_on", cmd_checkin_on))
    app.add_handler(CommandHandler("checkin_off", cmd_checkin_off))
    app.add_handler(CommandHandler("evening_on", cmd_evening_on))
    app.add_handler(CommandHandler("evening_off", cmd_evening_off))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("settz", cmd_settz))
    app.add_handler(CommandHandler("health60", cmd_health60))

    # --- Callback queries
    app.add_handler(CallbackQueryHandler(gate_cb,      pattern=r"^gate:"))
    app.add_handler(CallbackQueryHandler(mini_cb,      pattern=r"^mini\|"))
    app.add_handler(CallbackQueryHandler(fb_cb,        pattern=r"^fb:(good|bad|free)$"))
    app.add_handler(CallbackQueryHandler(generic_cb))  # прочие кнопки

    # --- Text (последний — после intake_pro)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=10)

    # --- Восстановление задач и запуск
    schedule_from_sheet_on_start(app)
    logging.info("Bot is starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

# --- Entry point ---
if __name__ == "__main__":
    main()
