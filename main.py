# -*- coding: utf-8 -*-
import os, re, json, uuid, logging
from datetime import datetime, timedelta, timezone, time as dtime
from typing import List, Tuple, Dict, Optional

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
from gspread.exceptions import SpreadsheetNotFound
import gspread.utils as gsu
from oauth2client.service_account import ServiceAccountCredentials

# ---------------- Boot & Config ----------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
DetectorFactory.seed = 0

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

SHEET_NAME = os.getenv("SHEET_NAME", "TendAI Sheets")
SHEET_ID = os.getenv("SHEET_ID", "")
ALLOW_CREATE_SHEET = os.getenv("ALLOW_CREATE_SHEET", "0") == "1"
DEFAULT_CHECKIN_LOCAL = "08:30"

oai: Optional[OpenAI] = None
if OPENAI_API_KEY:
    try:
        oai = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        logging.error(f"OpenAI init error: {e}")

# ---------------- Sheets init with fallback ----------------
SHEETS_ENABLED = True
ss = None
ws_feedback = ws_users = ws_profiles = ws_episodes = ws_reminders = ws_daily = None

def _sheets_init():
    global SHEETS_ENABLED, ss, ws_feedback, ws_users, ws_profiles, ws_episodes, ws_reminders, ws_daily
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if not creds_json:
            raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")
        creds = json.loads(creds_json)
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds, scope)
        gclient = gspread.authorize(credentials)

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
                ws = ss.add_worksheet(title=title, rows=1000, cols=max(20, len(headers)))
                ws.append_row(headers)
            if not ws.get_all_values():
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

# --------- i18n ----------
SUPPORTED = {"ru", "en", "uk"}
def norm_lang(code: Optional[str]) -> str:
    if not code: return "en"
    c = code.split("-")[0].lower()
    return c if c in SUPPORTED else "en"

T = {
    "en": {
        "welcome": "Hi! I’m TendAI — your health & longevity assistant.\nDescribe what’s bothering you or tap below.",
        "help": "Short checkups, 24–48h plans, reminders, daily check-ins.\nCommands: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /ru /uk /en",
        "privacy": "TendAI is not a medical service and can’t replace a doctor. We store minimal data for reminders. /delete_data to erase.",
        "paused_on": "Notifications paused. Use /resume to enable.", "paused_off": "Notifications resumed.",
        "deleted": "All your data was deleted. Use /start to begin again.",
        "ask_consent": "May I send you a follow-up to check how you feel later?",
        "yes":"Yes","no":"No",
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
        "comment_prompt":"Thanks 🙏 Add a short comment or /skip.",
        "comment_saved":"Saved, thank you! 🙌",
        "skip_ok":"Skipped.",
        "unknown":"I need a bit more info: where exactly and for how long?",
        "lang_switched":"OK, I’ll reply in English.",
        "profile_intro":"Quick intake (~40s). Use buttons or type your answer.",
        "p_step_1":"Step 1/8. Sex:", "p_step_2":"Step 2/8. Age:",
        "p_step_3":"Step 3/8. Main goal:", "p_step_4":"Step 4/8. Chronic conditions:",
        "p_step_5":"Step 5/8. Meds/supplements/allergies:",
        "p_step_6":"Step 6/8. Sleep (bed/wake, e.g., 23:30/07:00):",
        "p_step_7":"Step 7/8. Activity:", "p_step_8":"Step 8/8. Diet most of the time:",
        "write":"✍️ Write", "skip":"⏭️ Skip", "saved_profile":"Saved: ",
        "start_where":"Where do you want to start now? (symptom/sleep/nutrition/labs/habits/longevity)",
        "daily_gm":"Good morning! Quick daily check-in:",
        "mood_good":"😃 Good","mood_ok":"😐 Okay","mood_bad":"😣 Poor","mood_note":"✍️ Comment",
        "mood_thanks":"Thanks! Have a smooth day 👋",
        "start_intake_now":"Start quick intake now?",
        "start_yes":"Start","start_no":"Later",
        "comment_btn":"📝 Comment","skip_btn":"⏭️ Skip"
    },
    "ru": {
        "welcome":"Привет! Я TendAI — ассистент здоровья и долголетия.\nОпиши, что беспокоит, или выбери ниже.",
        "help":"Короткие проверки, план на 24–48 ч, напоминания, ежедневные чек-ины.\nКоманды: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +3 /ru /uk /en",
        "privacy":"TendAI не заменяет врача. Храним минимум данных для напоминаний. /delete_data — удалить.",
        "paused_on":"Напоминания поставлены на паузу. /resume — включить.", "paused_off":"Напоминания снова включены.",
        "deleted":"Все данные удалены. /start — начать заново.",
        "ask_consent":"Можно прислать напоминание позже, чтобы узнать, как вы?",
        "yes":"Да","no":"Нет",
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
        "comment_prompt":"Спасибо 🙏 Добавьте короткий комментарий или /skip.",
        "comment_saved":"Сохранил, спасибо! 🙌",
        "skip_ok":"Пропустили.",
        "unknown":"Нужно чуть больше деталей: где именно и сколько длится?",
        "lang_switched":"Ок, дальше отвечаю по-русски.",
        "profile_intro":"Быстрый опрос (~40с). Можно нажимать кнопки или писать свой ответ.",
        "p_step_1":"Шаг 1/8. Пол:","p_step_2":"Шаг 2/8. Возраст:",
        "p_step_3":"Шаг 3/8. Главная цель:","p_step_4":"Шаг 4/8. Хронические болезни:",
        "p_step_5":"Шаг 5/8. Лекарства/добавки/аллергии:",
        "p_step_6":"Шаг 6/8. Сон (отбой/подъём, напр. 23:30/07:00):",
        "p_step_7":"Шаг 7/8. Активность:","p_step_8":"Шаг 8/8. Питание чаще всего:",
        "write":"✍️ Написать","skip":"⏭️ Пропустить","saved_profile":"Сохранил: ",
        "start_where":"С чего начнём? (симптом/сон/питание/анализы/привычки/долголетие)",
        "daily_gm":"Доброе утро! Быстрый чек-ин:",
        "mood_good":"😃 Хорошо","mood_ok":"😐 Нормально","mood_bad":"😣 Плохо","mood_note":"✍️ Комментарий",
        "mood_thanks":"Спасибо! Хорошего дня 👋",
        "start_intake_now":"Запустить быстрый опрос сейчас?",
        "start_yes":"Начать","start_no":"Позже",
        "comment_btn":"📝 Комментарий","skip_btn":"⏭️ Пропустить"
    },
    "uk": {
        "welcome":"Привіт! Я TendAI — асистент здоров’я та довголіття.\nОпиши, що турбує, або обери нижче.",
        "help":"Короткі перевірки, план на 24–48 год, нагадування, щоденні чек-іни.\nКоманди: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /ru /uk /en",
        "privacy":"TendAI не замінює лікаря. Зберігаємо мінімум даних для нагадувань. /delete_data — видалити.",
        "paused_on":"Нагадування призупинені. /resume — увімкнути.", "paused_off":"Нагадування знову увімкнені.",
        "deleted":"Усі дані видалено. /start — почати знову.",
        "ask_consent":"Можу надіслати нагадування пізніше, щоб дізнатися, як ви?",
        "yes":"Так","no":"Ні",
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
        "comment_prompt":"Дякую 🙏 Додайте короткий коментар або /skip.",
        "comment_saved":"Збережено, дякую! 🙌",
        "skip_ok":"Пропущено.",
        "unknown":"Потрібно трохи більше: де саме і скільки триває?",
        "lang_switched":"Ок, надалі відповідатиму українською.",
        "profile_intro":"Швидкий опитник (~40с). Можна натискати кнопки або писати свій варіант.",
        "p_step_1":"Крок 1/8. Стать:","p_step_2":"Крок 2/8. Вік:",
        "p_step_3":"Крок 3/8. Головна мета:","p_step_4":"Крок 4/8. Хронічні хвороби:",
        "p_step_5":"Крок 5/8. Ліки/добавки/алергії:",
        "p_step_6":"Крок 6/8. Сон (відбій/підйом, напр. 23:30/07:00):",
        "p_step_7":"Крок 7/8. Активність:","p_step_8":"Крок 8/8. Харчування переважно:",
        "write":"✍️ Написати","skip":"⏭️ Пропустити","saved_profile":"Зберіг: ",
        "start_where":"З чого почнемо? (симптом/сон/харчування/аналізи/звички/довголіття)",
        "daily_gm":"Доброго ранку! Швидкий чек-ін:",
        "mood_good":"😃 Добре","mood_ok":"😐 Нормально","mood_bad":"😣 Погано","mood_note":"✍️ Коментар",
        "mood_thanks":"Дякую! Гарного дня 👋",
        "start_intake_now":"Запустити швидкий опитник зараз?",
        "start_yes":"Почати","start_no":"Пізніше",
        "comment_btn":"📝 Коментар","skip_btn":"⏭️ Пропустити"
    },
}
def t(lang: str, key: str) -> str:
    return T.get(lang, T["en"]).get(key, T["en"].get(key, key))

# ----------------- Helpers -----------------
def utcnow(): return datetime.now(timezone.utc)
def iso(dt: Optional[datetime]) -> str:
    return "" if not dt else dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")

def detect_lang_from_text(text: str, fallback: str) -> str:
    s = (text or "").strip()
    if not s: return fallback
    try:
        return norm_lang(detect(s))
    except Exception:
        if re.search(r"[а-яёіїєґ]", s.lower()):
            return "uk" if re.search(r"[іїєґ]", s.lower()) else "ru"
        return fallback

def profile_is_incomplete(profile_row: dict) -> bool:
    keys = ["sex","age","goal"]
    return sum(1 for k in keys if str(profile_row.get(k) or "").strip()) < 2

def msg_norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (s or "").lower())).strip()

async def send_unique(bot, chat_id: int, text: str, uid: int, reply_markup=None):
    """Не повторять одну мысль трижды подряд."""
    s = sessions.setdefault(uid, {})
    buf = s.setdefault("last_msgs", [])
    norm = msg_norm(text)
    if norm and (norm in buf[-3:]):  # защита от повторов
        logging.info("skip duplicate message")
        return
    msg = await bot.send_message(chat_id, text, reply_markup=reply_markup)
    buf.append(norm)
    if len(buf) > 12: buf[:] = buf[-12:]
    return msg

# -------- Sheets wrappers (fallback to memory) --------
def _headers(ws): return ws.row_values(1)

def users_get(uid: int) -> dict:
    if SHEETS_ENABLED:
        for r in ws_users.get_all_records():
            if str(r.get("user_id")) == str(uid): return r
        return {}
    return MEM_USERS.get(uid, {})

def users_upsert(uid: int, username: str, lang: str):
    base = {"user_id": str(uid), "username": username or "", "lang": lang,
            "consent": "no", "tz_offset":"0", "checkin_hour": DEFAULT_CHECKIN_LOCAL, "paused":"no"}
    if SHEETS_ENABLED:
        existing = users_get(uid) or {}
        merged = {**base, **existing}  # не затираем существующие поля
        hdr = _headers(ws_users)
        row_values = [merged.get(h, "") for h in hdr]
        vals = ws_users.get_all_records()
        for i, r in enumerate(vals, start=2):
            if str(r.get("user_id")) == str(uid):
                end_cell = gsu.rowcol_to_a1(i, len(hdr))   # напр. 'G2'
                end_col = re.sub(r'\d+', '', end_cell)     # 'G'
                ws_users.update(f"A{i}:{end_col}{i}", [row_values])
                return
        ws_users.append_row(row_values)
    else:
        MEM_USERS[uid] = {**base, **MEM_USERS.get(uid, {})}

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
            if str(r.get("user_id")) == str(uid): return r
        return {}
    return MEM_PROFILES.get(uid, {})

def profiles_upsert(uid: int, data: dict):
    if SHEETS_ENABLED:
        hdr = _headers(ws_profiles)
        current, idx = None, None
        for i, r in enumerate(ws_profiles.get_all_records(), start=2):
            if str(r.get("user_id")) == str(uid):
                current, idx = r, i; break
        if not current: current = {"user_id": str(uid)}
        for k,v in data.items():
            current[k] = "" if v is None else (", ".join(v) if isinstance(v,list) else str(v))
        current["updated_at"] = iso(utcnow())
        values = [current.get(h,"") for h in hdr]
        end_cell = gsu.rowcol_to_a1(1, len(hdr)).replace("1","")
        if idx: ws_profiles.update(f"A{idx}:{end_cell}{idx}", [values])
        else:   ws_profiles.append_row(values)
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
            if r.get("user_id")==str(uid) and r.get("status")=="open": return r
        return None
    for r in MEM_EPISODES:
        if r["user_id"]==str(uid) and r["status"]=="open": return r
    return None

def episode_set(eid: str, field: str, value: str):
    if SHEETS_ENABLED:
        vals = ws_episodes.get_all_values(); hdr = vals[0]
        if field not in hdr: return
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
    if SHEETS_ENABLED: ws_reminders.append_row([rec[k] for k in _headers(ws_reminders)])
    else: MEM_REMINDERS.append(rec)
    return rid

def reminders_all_records():
    if SHEETS_ENABLED: return ws_reminders.get_all_records()
    return MEM_REMINDERS.copy()

def reminders_mark_sent(rid: str):
    if SHEETS_ENABLED:
        vals = ws_reminders.get_all_values()
        for i in range(2, len(vals)+1):
            if ws_reminders.cell(i,1).value == rid:
                ws_reminders.update_cell(i,6,"sent"); return
    else:
        for r in MEM_REMINDERS:
            if r["id"]==rid: r["status"]="sent"; return

def daily_add(ts, uid, mood, comment):
    if SHEETS_ENABLED: ws_daily.append_row([ts,str(uid),mood,comment or ""])
    else: MEM_DAILY.append({"timestamp":ts,"user_id":str(uid),"mood":mood,"comment":comment or ""})

# --------- Scheduling (restore on start) ---------
def schedule_from_sheet_on_start(app):
    now = utcnow()
    src = ws_episodes.get_all_records() if SHEETS_ENABLED else MEM_EPISODES
    for r in src:
        if r.get("status")!="open": continue
        eid = r.get("episode_id"); uid = int(r.get("user_id"))
        nca = r.get("next_checkin_at") or ""
        if not nca: continue
        try: dt_ = datetime.strptime(nca, "%Y-%m-%d %H:%M:%S%z")
        except: continue
        delay = max(60, (dt_-now).total_seconds())
        app.job_queue.run_once(job_checkin_episode, when=delay, data={"user_id":uid,"episode_id":eid})

    for r in reminders_all_records():
        if (r.get("status") or "")!="scheduled": continue
        uid = int(r.get("user_id")); rid=r.get("id")
        try: dt_ = datetime.strptime(r.get("when_utc"), "%Y-%m-%d %H:%M:%S%z")
        except: continue
        delay = max(60,(dt_-now).total_seconds())
        app.job_queue.run_once(job_oneoff_reminder, when=delay, data={"user_id":uid,"reminder_id":rid})

    src_u = ws_users.get_all_records() if SHEETS_ENABLED else list(MEM_USERS.values())
    for u in src_u:
        if (u.get("paused") or "").lower()=="yes": continue
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
    for j in app.job_queue.get_jobs_by_name(f"daily_{uid}"): j.schedule_removal()
    h_utc, m_utc = local_to_utc_hour_min(tz_off, hhmm_local)
    t = dtime(hour=h_utc, minute=m_utc, tzinfo=timezone.utc)
    app.job_queue.run_daily(job_daily_checkin, time=t, name=f"daily_{uid}", data={"user_id":uid,"lang":lang})

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
        await send_unique(context.bot, uid, t(lang,"checkin_ping"), uid, reply_markup=kb)
        episode_set(eid, "next_checkin_at", "")
    except Exception as e:
        logging.error(f"job_checkin_episode send error: {e}")

async def job_oneoff_reminder(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, rid = d.get("user_id"), d.get("reminder_id")
    text = t(norm_lang(users_get(uid).get("lang") or "en"), "thanks")
    for r in reminders_all_records():
        if r.get("id")==rid: text = r.get("text") or text; break
    try:
        await send_unique(context.bot, uid, text, uid)
    except Exception as e:
        logging.error(f"reminder send error: {e}")
    reminders_mark_sent(rid)

async def job_daily_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, lang = d.get("user_id"), d.get("lang","en")
    u = users_get(uid)
    if (u.get("paused") or "").lower()=="yes": return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_good"], callback_data="mood|good"),
         InlineKeyboardButton(T[lang]["mood_ok"],   callback_data="mood|ok"),
         InlineKeyboardButton(T[lang]["mood_bad"],  callback_data="mood|bad")],
        [InlineKeyboardButton(T[lang]["mood_note"], callback_data="mood|note")]
    ])
    try:
        await send_unique(context.bot, uid, T[lang]["daily_gm"], uid, reply_markup=kb)
    except Exception as e:
        logging.error(f"daily checkin error: {e}")

# ------------- LLM Router -------------
SYS_ROUTER = """
You are TendAI — a concise, warm, professional health & longevity assistant (not a doctor).
Always answer strictly in {lang}. Keep replies short (<=6 lines + up to 4 bullets).
Use the user profile if provided. TRIAGE: ask 1–2 clarifiers first; advise ER only for clear red flags with high confidence.
Return MINIFIED JSON ONLY:
{{"intent":"symptom"|"nutrition"|"sleep"|"labs"|"habits"|"longevity"|"other",
  "assistant_reply": string,
  "followups": string[],
  "needs_more": boolean,
  "red_flags": boolean,
  "confidence": 0.0}}
"""
def llm_router_answer(text: str, lang: str, profile: dict) -> dict:
    if not oai:
        return {"intent":"other","assistant_reply":t(lang,"unknown"),"followups":[],"needs_more":True,"red_flags":False,"confidence":0.3}
    sys = SYS_ROUTER.format(lang=lang) + f"\nUserProfile: {json.dumps(profile, ensure_ascii=False)}"
    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL, temperature=0.25, max_tokens=420,
            messages=[{"role":"system","content":sys},{"role":"user","content":text}]
        )
        out = resp.choices[0].message.content.strip()
        m = re.search(r"\{.*\}\s*$", out, re.S)
        data = json.loads(m.group(0) if m else out)
        if data.get("red_flags") and float(data.get("confidence",0)) < 0.6:
            data["red_flags"] = False; data["needs_more"] = True
            data.setdefault("followups", []).append(
                "Где именно/какой характер/сколько длится?" if lang=="ru" else
                ("Де саме/який характер/скільки триває?" if lang=="uk" else "Where exactly/what character/how long?")
            )
        return data
    except Exception as e:
        logging.error(f"router LLM error: {e}")
        return {"intent":"other","assistant_reply":t(lang,"unknown"),"followups":[],"needs_more":True,"red_flags":False,"confidence":0.3}

# ---- Serious conditions (hepatitis/diabetes/cancer etc.) ----
SERIOUS_MAP = {
    "hepat": "hepatitis", "гепат": "hepatitis",
    "diabet": "diabetes", "диабет": "diabetes", "цукр": "diabetes",
    "onc": "cancer", "рак": "cancer", "tumor": "cancer", "опухол": "cancer"
}
def detect_serious(text: str) -> Optional[str]:
    tl = text.lower()
    for k,v in SERIOUS_MAP.items():
        if k in tl: return v
    return None

def serious_reply(lang: str, condition: str, prof: dict) -> Tuple[str, List[str]]:
    age = prof.get("age") or ""
    sex = prof.get("sex") or ""
    intro = {
        "ru": f"Понял. Учитываю профиль (возраст {age or 'не указан'}, пол {sex or '—'}).",
        "uk": f"Зрозумів. Враховую профіль (вік {age or 'не вказано'}, стать {sex or '—'}).",
        "en": f"Got it. Considering your profile (age {age or 'n/a'}, sex {sex or '—'})."
    }[lang]
    if condition=="hepatitis":
        body = {
            "ru": "По гепатиту важно: последние анализы (АЛТ/АСТ, билирубин, HBsAg/anti-HCV), симптомы, алкоголь/лекарства. Пока — режим щадящий, вода, исключить алкоголь/гепатотоксичные препараты.",
            "uk": "Щодо гепатиту важливо: останні аналізи (АЛТ/АСТ, білірубін, HBsAg/anti-HCV), симптоми, алкоголь/ліки. Поки що — щадний режим, вода, виключити алкоголь та гепатотоксичні препарати.",
            "en": "For hepatitis: key items are latest labs (ALT/AST, bilirubin, HBsAg/anti-HCV), symptoms, alcohol/meds. Meanwhile: gentle regimen, hydration, avoid alcohol/hepatotoxic meds."
        }[lang]
        follow = {
            "ru": ["Есть ли результаты анализов? (АЛТ/АСТ/билирубин)", "Были ли желтушность кожи/глаз, тёмная моча, сильная слабость?"],
            "uk": ["Є результати аналізів? (АЛТ/АСТ/білірубін)", "Були жовтушність шкіри/очей, темна сеча, сильна слабкість?"],
            "en": ["Do you have lab results? (ALT/AST/bilirubin)", "Any jaundice, dark urine, marked fatigue?"]
        }[lang]
    elif condition=="diabetes":
        body = {
            "ru": "Для диабета уточним: тип, текущие сахара/НbA1c, лекарства, эпизоды гипо. Ближайшая цель: стабильный сахар, шаги после еды, вода.",
            "uk": "Для діабету: тип, поточні цукри/HbA1c, ліки, епізоди гіпо. Мета: стабільний цукор, кроки після їжі, вода.",
            "en": "For diabetes: type, current glucose/HbA1c, meds, hypoglycemia episodes. Near goal: steady glucose, post-meal walking, hydration."
        }[lang]
        follow = {
            "ru": ["Какой тип? Последний HbA1c?", "Какие препараты принимаете? Были гипо-эпизоды?"],
            "uk": ["Який тип? Останній HbA1c?", "Які препарати приймаєте? Були епізоди гіпо?"],
            "en": ["Which type? Latest HbA1c?", "What meds are you on? Any hypo episodes?"]
        }[lang]
    else:  # cancer
        body = {
            "ru": "По онкологии лучше опираться на выписку: стадия, гистология, текущая схема лечения. Я помогу с питанием, сном, побочками, восстановлением.",
            "uk": "По онкології важлива виписка: стадія, гістологія, поточна схема лікування. Допоможу з харчуванням, сном, побічками, відновленням.",
            "en": "For cancer, anchor on the discharge note: stage, histology, current regimen. I can help with nutrition, sleep, side-effects, recovery."
        }[lang]
        follow = {
            "ru": ["Есть ли выписка/стадия/лечение?", "Что беспокоит сейчас сильнее всего? Боль/тошнота/сон/аппетит?"],
            "uk": ["Є виписка/стадія/лікування?", "Що турбує зараз найбільше? Біль/нудота/сон/апетит?"],
            "en": ["Do you have discharge/stage/therapy plan?", "What troubles you most now? Pain/nausea/sleep/appetite?"]
        }[lang]
    return f"{intro}\n{body}", follow

# --------- Inline keyboards ---------
def inline_topic_kb(lang:str) -> InlineKeyboardMarkup:
    items = [
        ("Pain","pain"),("Throat/Cold","throat"),("Sleep","sleep"),("Stress","stress"),
        ("Digestion","digestion"),("Energy","energy"),
        ("Nutrition","nutrition"),("Labs","labs"),("Habits","habits"),
        ("Longevity","longevity"),("Profile","profile")
    ]
    by_lang = {
        "ru":["Боль","Горло/простуда","Сон","Стресс","Пищеварение","Энергия","Питание","Анализы","Привычки","Долголетие","Профиль"],
        "uk":["Біль","Горло/застуда","Сон","Стрес","Травлення","Енергія","Харчування","Аналізи","Звички","Довголіття","Профіль"],
        "en":[x[0] for x in items]
    }[lang]
    keys = [x[1] for x in items]
    rows=[]; row=[]
    for label,key in zip(by_lang, keys):
        row.append(InlineKeyboardButton(label, callback_data=f"topic|{key}"))
        if len(row)==3: rows.append(row); row=[]
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def inline_list(opts: List[str], prefix:str) -> InlineKeyboardMarkup:
    rows=[]; row=[]
    for i, label in enumerate(opts,1):
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}|{label}"))
        if len(row)==3: rows.append(row); row=[]
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def inline_numbers_0_10() -> InlineKeyboardMarkup:
    nums = [str(i) for i in range(0,11)]
    rows = [
        [InlineKeyboardButton(n, callback_data=f"num|{n}") for n in nums[:6]],
        [InlineKeyboardButton(n, callback_data=f"num|{n}") for n in nums[6:]]
    ]
    return InlineKeyboardMarkup(rows)

def inline_accept(lang:str) -> InlineKeyboardMarkup:
    labels = T[lang]["accept_opts"]
    return InlineKeyboardMarkup([[InlineKeyboardButton(labels[0],callback_data="acc|yes"),
                                  InlineKeyboardButton(labels[1],callback_data="acc|later"),
                                  InlineKeyboardButton(labels[2],callback_data="acc|no")]])

def inline_remind(lang:str) -> InlineKeyboardMarkup:
    labs = T[lang]["remind_opts"]; keys = ["4h","evening","morning","none"]
    rows=[[InlineKeyboardButton(labs[i], callback_data=f"rem|{keys[i]}") for i in range(4)]]
    return InlineKeyboardMarkup(rows)

def inline_feedback(lang:str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["comment_btn"], callback_data="fb|comment"),
                                  InlineKeyboardButton(T[lang]["skip_btn"], callback_data="fb|skip")]])

# ------------- Plans -------------
def pain_plan(lang: str, red_flags_selected: List[str]) -> List[str]:
    flg = [s for s in red_flags_selected if s and str(s).lower() not in ["none","нет","немає"]]
    if flg:
        return {
            "ru":["⚠️ Есть тревожные признаки. Лучше как можно скорее показаться врачу/в скорую."],
            "uk":["⚠️ Є тривожні ознаки. Варто якнайшвидше звернутися до лікаря/швидкої."],
            "en":["⚠️ Red flags present. Please seek urgent medical evaluation."]
        }[lang]
    if lang=="ru":
        return ["1) Вода 400–600 мл и 15–20 мин тишины/отдыха.",
                "2) Если нет противопоказаний — ибупрофен 200–400 мг однократно с едой.",
                "3) Проветрить, уменьшить экран на 30–60 мин.",
                "Цель: к вечеру боль ≤3/10. Если нарастает — напишите."]
    if lang=="uk":
        return ["1) Вода 400–600 мл і 15–20 хв спокою.",
                "2) Якщо нема протипоказань — ібупрофен 200–400 мг одноразово з їжею.",
                "3) Провітрити, менше екрану 30–60 хв.",
                "Мета: до вечора біль ≤3/10. Якщо посилюється — напишіть."]
    return ["1) Drink 400–600 ml water; rest 15–20 min.",
            "2) If no contraindications — ibuprofen 200–400 mg once with food.",
            "3) Reduce screen time 30–60 min; air the room.",
            "Goal: by evening pain ≤3/10. Worsening — ping me."]

# ------------- Profile (intake) -------------
PROFILE_STEPS = [
    {"key":"sex","opts":{
        "ru":[("Мужской","male"),("Женский","female"),("Другое","other")],
        "en":[("Male","male"),("Female","female"),("Other","other")],
        "uk":[("Чоловіча","male"),("Жіноча","female"),("Інша","other")],
    }},
    {"key":"age","opts":{
        "ru":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "en":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "uk":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
    }},
    {"key":"goal","opts":{
        "ru":[("Похудение","weight"),("Энергия","energy"),("Сон","sleep"),("Долголетие","longevity"),("Сила","strength")],
        "en":[("Weight","weight"),("Energy","energy"),("Sleep","sleep"),("Longevity","longevity"),("Strength","strength")],
        "uk":[("Вага","weight"),("Енергія","energy"),("Сон","sleep"),("Довголіття","longevity"),("Сила","strength")],
    }},
    {"key":"conditions","opts":{
        "ru":[("Нет","none"),("Гипертония","hypertension"),("Диабет","diabetes"),("Щитовидка","thyroid"),("Другое","other")],
        "en":[("None","none"),("Hypertension","hypertension"),("Diabetes","diabetes"),("Thyroid","thyroid"),("Other","other")],
        "uk":[("Немає","none"),("Гіпертонія","hypertension"),("Діабет","diabetes"),("Щитоподібна","thyroid"),("Інше","other")],
    }},
    {"key":"meds","opts":{
        "ru":[("Нет","none"),("Магний","magnesium"),("Витамин D","vitd"),("Аллергии есть","allergies"),("Другое","other")],
        "en":[("None","none"),("Magnesium","magnesium"),("Vitamin D","vitd"),("Allergies","allergies"),("Other","other")],
        "uk":[("Немає","none"),("Магній","magnesium"),("Вітамін D","vitd"),("Алергії","allergies"),("Інше","other")],
    }},
    {"key":"sleep","opts":{
        "ru":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
        "en":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Irregular","irregular")],
        "uk":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
    }},
    {"key":"activity","opts":{
        "ru":[("<5к шагов","<5k"),("5–8к","5-8k"),("8–12к","8-12k"),("Спорт регулярно","sport")],
        "en":[("<5k steps","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Regular sport","sport")],
        "uk":[("<5к кроків","<5k"),("5–8к","5-8k"),("8–12к","8-12k"),("Спорт регулярно","sport")],
    }},
    {"key":"diet","opts":{
        "ru":[("Сбалансировано","balanced"),("Низкоугл/кето","lowcarb"),("Вегетар/веган","plant"),("Нерегулярно","irregular")],
        "en":[("Balanced","balanced"),("Low-carb/keto","lowcarb"),("Vegetarian/vegan","plant"),("Irregular","irregular")],
        "uk":[("Збалансовано","balanced"),("Маловугл/кето","lowcarb"),("Вегетар/веган","plant"),("Нерегулярно","irregular")],
    }},
]
def build_profile_kb(lang:str, key:str, opts:List[Tuple[str,str]])->InlineKeyboardMarkup:
    rows=[]; row=[]
    for label,val in opts:
        row.append(InlineKeyboardButton(label, callback_data=f"p|choose|{key}|{val}"))
        if len(row)==3: rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([InlineKeyboardButton(T[lang]["write"], callback_data=f"p|write|{key}"),
                 InlineKeyboardButton(T[lang]["skip"],  callback_data=f"p|skip|{key}")])
    return InlineKeyboardMarkup(rows)

async def start_profile(update: Update, lang: str, uid: int):
    sessions[uid] = {**sessions.get(uid, {}), "profile_active": True, "p_step": 0, "p_wait_key": None}
    await send_unique(update.get_bot(), update.effective_chat.id, T[lang]["profile_intro"], uid, reply_markup=ReplyKeyboardRemove())
    step = PROFILE_STEPS[0]
    kb = build_profile_kb(lang, step["key"], step["opts"][lang])
    await send_unique(update.get_bot(), update.effective_chat.id, T[lang]["p_step_1"], uid, reply_markup=kb)

async def advance_profile(msg, lang: str, uid: int):
    s = sessions.get(uid, {})
    s["p_step"] += 1
    if s["p_step"] < len(PROFILE_STEPS):
        idx = s["p_step"]; step = PROFILE_STEPS[idx]
        kb = build_profile_kb(lang, step["key"], step["opts"][lang])
        await send_unique(msg.get_bot(), msg.chat_id, T[lang][f"p_step_{idx+1}"], uid, reply_markup=kb)
        return
    prof = profiles_get(uid); summary=[]
    for k in ["sex","age","goal","conditions","meds","sleep","activity","diet"]:
        v = prof.get(k) or sessions.get(uid,{}).get(k,"")
        if v: summary.append(f"{k}: {v}")
    profiles_upsert(uid, {})
    sessions[uid]["profile_active"] = False
    await send_unique(msg.get_bot(), msg.chat_id, T[lang]["saved_profile"] + "; ".join(summary), uid)
    await send_unique(msg.get_bot(), msg.chat_id, T[lang]["start_where"], uid, reply_markup=inline_topic_kb(lang))

# ------------- Commands -------------
async def post_init(app):
    me = await app.bot.get_me()
    logging.info(f"BOT READY: @{me.username} (id={me.id})")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = norm_lang(getattr(user, "language_code", None))
    users_upsert(user.id, user.username or "", lang)
    await send_unique(context.bot, update.effective_chat.id, t(lang,"welcome"), user.id, reply_markup=ReplyKeyboardRemove())
    await send_unique(context.bot, update.effective_chat.id, T[lang]["start_intake_now"],
        user.id,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["start_yes"], callback_data="startintake|yes"),
                                            InlineKeyboardButton(T[lang]["start_no"],  callback_data="startintake|no")]]))
    u = users_get(user.id)
    if (u.get("consent") or "").lower() not in {"yes","no"}:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(t(lang,"yes"), callback_data="consent|yes"),
                                    InlineKeyboardButton(t(lang,"no"),  callback_data="consent|no")]])
        await send_unique(context.bot, update.effective_chat.id, t(lang,"ask_consent"), user.id, reply_markup=kb)
    tz_off = int(str(u.get("tz_offset") or "0"))
    hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
    schedule_daily_checkin(context.application, user.id, tz_off, hhmm, lang)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await send_unique(context.bot, update.effective_chat.id, t(lang,"help"), update.effective_user.id)

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await send_unique(context.bot, update.effective_chat.id, t(lang,"privacy"), update.effective_user.id)

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "yes")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await send_unique(context.bot, update.effective_chat.id, t(lang,"paused_on"), uid)

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "no")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await send_unique(context.bot, update.effective_chat.id, t(lang,"paused_off"), uid)

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
    await send_unique(context.bot, update.effective_chat.id, t(lang,"deleted"), uid, reply_markup=ReplyKeyboardRemove())

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None))
    await start_profile(update, lang, uid)

async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    parts = (update.message.text or "").split()
    if len(parts)<2 or not re.fullmatch(r"[+-]?\d{1,2}", parts[1]):
        await send_unique(context.bot, update.effective_chat.id, {"ru":"Формат: /settz +3","uk":"Формат: /settz +2","en":"Usage: /settz +3"}[lang], uid); return
    off = int(parts[1]); users_set(uid,"tz_offset",str(off))
    hhmm = users_get(uid).get("checkin_hour") or DEFAULT_CHECKIN_LOCAL
    schedule_daily_checkin(context.application, uid, off, hhmm, lang)
    await send_unique(context.bot, update.effective_chat.id, {"ru":f"Сдвиг часового пояса: {off}ч","uk":f"Зсув: {off} год","en":f"Timezone offset: {off}h"}[lang], uid)

async def cmd_checkin_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    parts = (update.message.text or "").split(maxsplit=1)
    hhmm = DEFAULT_CHECKIN_LOCAL
    if len(parts)==2:
        m = re.search(r'([01]?\d|2[0-3]):([0-5]\d)', parts[1])
        if m: hhmm = m.group(0)
    users_set(uid,"checkin_hour",hhmm)
    tz_off = int(str(users_get(uid).get("tz_offset") or "0"))
    schedule_daily_checkin(context.application, uid, tz_off, hhmm, lang)
    await send_unique(context.bot, update.effective_chat.id, {"ru":f"Ежедневный чек-ин включён ({hhmm}).","uk":f"Щоденний чек-ін увімкнено ({hhmm}).","en":f"Daily check-in enabled ({hhmm})."}[lang], uid)

async def cmd_checkin_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    for j in context.application.job_queue.get_jobs_by_name(f"daily_{uid}"): j.schedule_removal()
    await send_unique(context.bot, update.effective_chat.id, {"ru":"Ежедневный чек-ин выключен.","uk":"Щоденний чек-ін вимкнено.","en":"Daily check-in disabled."}
                                    [norm_lang(users_get(uid).get("lang") or "en")], uid)

# быстрые команды смены языка
async def cmd_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "ru")
    await send_unique(context.bot, update.effective_chat.id, t("ru","lang_switched"), update.effective_user.id)
async def cmd_en(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "en")
    await send_unique(context.bot, update.effective_chat.id, t("en","lang_switched"), update.effective_user.id)
async def cmd_uk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_set(update.effective_user.id, "lang", "uk")
    await send_unique(context.bot, update.effective_chat.id, t("uk","lang_switched"), update.effective_user.id)

# ------------- Callback handler -------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = (q.data or ""); uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")

    if data.startswith("startintake|"):
        if data.endswith("|yes"):
            try: await q.delete_message()
            except: pass
            # запустить intake
            fake_update = Update(update.update_id, message=q.message)
            fake_update.effective_user = q.from_user
            fake_update.effective_chat = q.message.chat
            await start_profile(fake_update, lang, uid)
        else:
            await send_unique(context.bot, q.message.chat_id, t(lang,"start_where"), uid, reply_markup=inline_topic_kb(lang))
        return

    if data.startswith("consent|"):
        users_set(uid, "consent", "yes" if data.endswith("|yes") else "no")
        try: await q.edit_message_reply_markup(reply_markup=None)
        except: pass
        await send_unique(context.bot, q.message.chat_id, t(lang,"thanks"), uid); return

    if data.startswith("fb|"):
        if data.endswith("|comment"):
            sessions.setdefault(uid, {})["awaiting_comment"] = True
            await send_unique(context.bot, q.message.chat_id, t(lang,"comment_prompt"), uid)
        else:
            await send_unique(context.bot, q.message.chat_id, t(lang,"skip_ok"), uid)
        try: await q.edit_message_reply_markup(reply_markup=None)
        except: pass
        return

    if data.startswith("topic|"):
        topic = data.split("|",1)[1]
        if topic=="profile":
            fake = Update(update.update_id, message=q.message); fake.effective_user=q.from_user; fake.effective_chat=q.message.chat
            await start_profile(fake, lang, uid); return
        if topic=="pain":
            fake = Update(update.update_id, message=q.message); fake.effective_user=q.from_user; fake.effective_chat=q.message.chat
            await start_pain_triage(fake, lang, uid); return
        prof = profiles_get(uid)
        data_llm = llm_router_answer(q.message.text or "", lang, prof)
        reply = data_llm.get("assistant_reply") or t(lang,"unknown")
        await send_unique(context.bot, q.message.chat_id, reply, uid, reply_markup=inline_feedback(lang))
        for one in (data_llm.get("followups") or [])[:2]:
            await send_unique(context.bot, q.message.chat_id, one, uid)
        await send_unique(context.bot, q.message.chat_id, t(lang,"remind_when"), uid, reply_markup=inline_remind(lang))
        return

    # intake callbacks
    if data.startswith("p|"):
        _, action, key, *rest = data.split("|")
        if action=="choose":
            value = "|".join(rest)
            sessions.setdefault(uid,{})[key]=value; profiles_upsert(uid,{key:value})
            await advance_profile(q.message, lang, uid); return
        if action=="write":
            sessions.setdefault(uid,{})["p_wait_key"] = key
            await send_unique(context.bot, q.message.chat_id, {"ru":"Напишите короткий ответ:","uk":"Напишіть коротко:","en":"Type your answer:"}[lang], uid); return
        if action=="skip":
            profiles_upsert(uid,{key:""}); await advance_profile(q.message, lang, uid); return

    # daily moods
    if data.startswith("mood|"):
        mood = data.split("|",1)[1]
        if mood=="note":
            sessions.setdefault(uid,{})["awaiting_daily_comment"] = True
            await send_unique(context.bot, q.message.chat_id, {"ru":"Короткий комментарий:","uk":"Короткий коментар:","en":"Short note:"}[lang], uid); return
        daily_add(iso(utcnow()), uid, mood, ""); await send_unique(context.bot, q.message.chat_id, T[lang]["mood_thanks"], uid); return

    # pain triage inline options
    if data.startswith(("painloc|", "painkind|", "paindur|", "painrf|")):
        label = data.split("|", 1)[1]
        fake_update = Update(update.update_id, message=q.message)
        fake_update.effective_user = q.from_user
        fake_update.effective_chat = q.message.chat
        fake_update.message.text = label
        await continue_pain_triage(fake_update, context, lang, uid, label)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except:
            pass
        return

    if data.startswith("num|"):
        num = data.split("|",1)[1]
        fake_update = Update(update.update_id, message=q.message)
        fake_update.effective_user = q.from_user
        fake_update.effective_chat = q.message.chat
        fake_update.message.text = num
        await on_number_reply(fake_update, context); return

    if data.startswith("acc|"):
        s = sessions.get(uid, {})
        accepted = "1" if data.endswith("|yes") else "0"
        if s.get("episode_id"): episode_set(s["episode_id"], "plan_accepted", accepted)
        await send_unique(context.bot, q.message.chat_id, t(lang,"remind_when"), uid, reply_markup=inline_remind(lang))
        s["step"] = 7; return

    if data.startswith("rem|"):
        s = sessions.get(uid, {})
        choice = data.split("|",1)[1]
        delay = {"4h":4, "evening":6, "morning":16}.get(choice)
        if delay and s.get("episode_id"):
            next_time = utcnow() + timedelta(hours=delay)
            episode_set(s["episode_id"], "next_checkin_at", iso(next_time))
            context.job_queue.run_once(job_checkin_episode, when=delay*3600,
                                       data={"user_id":uid,"episode_id":s["episode_id"]})
        await send_unique(context.bot, q.message.chat_id, t(lang,"thanks"), uid, reply_markup=inline_topic_kb(lang))
        sessions.pop(uid, None); return

# ------------- Topic detection -------------
def detect_or_choose_topic(lang: str, text: str) -> Optional[str]:
    tx = text.lower()
    if any(w in tx for w in ["опрос","анкета","опит","questionnaire","survey"]): return "profile"
    if any(w in tx for w in ["болит","боль","hurt","pain","болю"]): return "pain"
    if any(w in tx for w in ["горло","throat","простуд","cold"]): return "throat"
    if any(w in tx for w in ["сон","sleep"]): return "sleep"
    if any(w in tx for w in ["стресс","stress"]): return "stress"
    if any(w in tx for w in ["живот","желуд","живіт","стул","понос","диар","digest"]): return "digestion"
    if any(w in tx for w in ["энерг","енерг","energy","fatigue","слабость"]): return "energy"
    if any(w in tx for w in ["питание","харчування","nutrition"]): return "nutrition"
    if any(w in tx for w in ["анализ","аналіз","labs"]): return "labs"
    if any(w in tx for w in ["привыч","звич","habit"]): return "habits"
    if any(w in tx for w in ["долголет","довголіт","longevity"]): return "longevity"
    return None

# ------------- Pain triage -------------
async def start_pain_triage(update: Update, lang: str, uid: int):
    sessions[uid] = {"topic":"pain","step":1,"answers":{}}
    kb = inline_list(T[lang]["triage_pain_q1_opts"], "painloc")
    await send_unique(update.get_bot(), update.effective_chat.id, t(lang,"triage_pain_q1"), uid, reply_markup=kb)

async def continue_pain_triage(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str, uid: int, text: str):
    s = sessions.get(uid, {}); step = s.get("step",1)

    if step == 1:
        s["answers"]["loc"] = text; s["step"] = 2
        await send_unique(update.get_bot(), update.effective_chat.id, t(lang,"triage_pain_q2"), uid,
                          reply_markup=inline_list(T[lang]["triage_pain_q2_opts"], "painkind")); return

    if step == 2:
        s["answers"]["kind"] = text; s["step"] = 3
        await send_unique(update.get_bot(), update.effective_chat.id, t(lang,"triage_pain_q3"), uid,
                          reply_markup=inline_list(T[lang]["triage_pain_q3_opts"], "paindur")); return

    if step == 3:
        s["answers"]["duration"] = text; s["step"] = 4
        await send_unique(update.get_bot(), update.effective_chat.id, t(lang,"triage_pain_q4"), uid, reply_markup=inline_numbers_0_10()); return

    if step == 4:
        m = re.search(r'\d+', text)
        if not m:
            await send_unique(update.get_bot(), update.effective_chat.id, t(lang,"triage_pain_q4"), uid, reply_markup=inline_numbers_0_10()); return
        sev = max(0,min(10,int(m.group(0))))
        s["answers"]["severity"] = sev; s["step"] = 5
        await send_unique(update.get_bot(), update.effective_chat.id, t(lang,"triage_pain_q5"), uid,
                          reply_markup=inline_list(T[lang]["triage_pain_q5_opts"], "painrf")); return

    if step == 5:
        red = text; s["answers"]["red"] = red
        eid = episode_create(uid, "pain", int(s["answers"].get("severity",5)), red)
        s["episode_id"] = eid
        plan_lines = pain_plan(lang, [red])
        await send_unique(update.get_bot(), update.effective_chat.id, f"{t(lang,'plan_header')}\n" + "\n".join(plan_lines), uid)
        await send_unique(update.get_bot(), update.effective_chat.id, t(lang,"plan_accept"), uid, reply_markup=inline_accept(lang))
        s["step"] = 6; return

# ------------- Text handler -------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    text = (update.message.text or "").strip()
    logging.info(f"INCOMING uid={uid} text={text[:200]}")

    saved_lang = norm_lang(users_get(uid).get("lang") or getattr(user,"language_code",None))
    detected_lang = detect_lang_from_text(text, saved_lang)
    if detected_lang != saved_lang: users_set(uid,"lang",detected_lang)
    lang = detected_lang

    # ежедневный чек-ин — заметка
    if sessions.get(uid, {}).get("awaiting_daily_comment"):
        daily_add(iso(utcnow()), uid, "note", text)
        sessions[uid]["awaiting_daily_comment"] = False
        await send_unique(context.bot, update.effective_chat.id, T[lang]["mood_thanks"], uid); return

    # фидбек-коммент
    if sessions.get(uid, {}).get("awaiting_comment"):
        feedback_add(iso(utcnow()), uid, "comment", user.username or "", "", text)
        sessions[uid]["awaiting_comment"] = False
        await send_unique(context.bot, update.effective_chat.id, t(lang,"comment_saved"), uid); return

    # свободный ответ для intake
    if sessions.get(uid, {}).get("p_wait_key"):
        key = sessions[uid]["p_wait_key"]; sessions[uid]["p_wait_key"] = None
        val = text
        if key=="age":
            m = re.search(r'\d{2}', text)
            if m: val = m.group(0)
        profiles_upsert(uid,{key:val}); sessions[uid][key]=val
        await advance_profile(update.message, lang, uid); return

    # если профиль пуст — запускаем intake сразу
    prof = profiles_get(uid)
    if not sessions.get(uid,{}).get("profile_active") and profile_is_incomplete(prof):
        await start_profile(update, lang, uid); return

    # активный триаж боли
    if sessions.get(uid,{}).get("topic") == "pain":
        await continue_pain_triage(update, context, lang, uid, text); return

    # серьёзные диагнозы
    serious = detect_serious(text)
    if serious:
        reply, follow = serious_reply(lang, serious, prof)
        await send_unique(context.bot, update.effective_chat.id, reply, uid, reply_markup=inline_feedback(lang))
        for f in follow:
            await send_unique(context.bot, update.effective_chat.id, f, uid)
        await send_unique(context.bot, update.effective_chat.id, t(lang,"remind_when"), uid, reply_markup=inline_remind(lang))
        return

    topic = detect_or_choose_topic(lang, text)
    if topic == "profile":
        await start_profile(update, lang, uid); return
    if topic == "pain":
        await start_pain_triage(update, lang, uid); return
    if topic in {"throat","sleep","stress","digestion","energy","nutrition","labs","habits","longevity"}:
        data = llm_router_answer(text, lang, prof)
        reply = data.get("assistant_reply") or t(lang,"unknown")
        await send_unique(context.bot, update.effective_chat.id, reply, uid, reply_markup=inline_feedback(lang))
        for one in (data.get("followups") or [])[:2]:
            await send_unique(context.bot, update.effective_chat.id, one, uid)
        await send_unique(context.bot, update.effective_chat.id, t(lang,"remind_when"), uid, reply_markup=inline_remind(lang))
        return

    # общий фолбэк
    data = llm_router_answer(text, lang, prof)
    reply = data.get("assistant_reply") or t(lang,"unknown")
    await send_unique(context.bot, update.effective_chat.id, reply, uid, reply_markup=inline_feedback(lang))
    for one in (data.get("followups") or [])[:2]:
        await send_unique(context.bot, update.effective_chat.id, one, uid)
    await send_unique(context.bot, update.effective_chat.id, t(lang,"remind_when"), uid, reply_markup=inline_remind(lang))

# ------------- Number replies (0–10 typed) -------------
async def on_number_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    text = update.message.text.strip()
    try:
        val = int(text)
        if not (0 <= val <= 10): return
    except Exception:
        return
    lang = norm_lang(users_get(uid).get("lang") or getattr(user,"language_code",None))

    if sessions.get(uid,{}).get("topic")=="pain" and sessions[uid].get("step")==4:
        await continue_pain_triage(update, context, lang, uid, str(val)); return

    ep = episode_find_open(uid)
    if not ep:
        await send_unique(context.bot, update.effective_chat.id, t(lang,"thanks"), uid); return
    eid = ep.get("episode_id"); episode_set(eid,"notes",f"checkin:{val}")

    if val <= 3:
        await send_unique(context.bot, update.effective_chat.id, t(lang,"checkin_better"), uid, reply_markup=inline_topic_kb(lang))
        episode_set(eid,"status","resolved")
    else:
        await send_unique(context.bot, update.effective_chat.id, t(lang,"checkin_worse"), uid, reply_markup=inline_topic_kb(lang))

# ------------- App init -------------
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    schedule_from_sheet_on_start(app)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("delete_data", cmd_delete_data))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("settz", cmd_settz))
    app.add_handler(CommandHandler("checkin_on", cmd_checkin_on))
    app.add_handler(CommandHandler("checkin_off", cmd_checkin_off))
    app.add_handler(CommandHandler("ru", cmd_ru))
    app.add_handler(CommandHandler("en", cmd_en))
    app.add_handler(CommandHandler("uk", cmd_uk))

    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_handler(MessageHandler(filters.Regex(r"^(?:[0-9]|10)$"), on_number_reply))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logging.info(f"SHEETS_ENABLED={SHEETS_ENABLED}")
    app.run_polling()

if __name__ == "__main__":
    main()
