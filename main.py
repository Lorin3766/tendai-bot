#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TendAI — LLM-powered Health & Longevity Assistant
Функции:
- Авто-детект языка (ru/en/es/uk) и ведение диалога на языке пользователя.
- Сразу после /start: быстрый опросник (~40с) одним сообщением (без кнопок).
- LLM-парсинг intake → профиль (SQLite), дальнейший диалог полностью через LLM-роутер:
  интент, слоты, уточняющие вопросы, персональные рекомендации (человечный тон).
- Чек-ин «за 60 секунд» (/fast и/или по расписанию 08:30 в TZ пользователя) —
  кнопки + свободный ввод; ответы сохраняются.
- Напоминания (/remind, /reminders, /delremind, /checkin_on, /checkin_off, /settz).
- Команды: /start /reset /privacy /profile /plan /fast /pause /resume /remind /reminders /delremind /settz /checkin_on /checkin_off
"""

import os
import re
import json
import time
import sqlite3
import logging
import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from collections import defaultdict, deque
from zoneinfo import ZoneInfo

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters
)

# ---------- OpenAI ----------
from openai import OpenAI

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN_HERE")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY_HERE")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Kyiv")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("tendai")

client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------- DB (SQLite) ----------------
DB_PATH = os.getenv("TEND_AI_DB", "tendai.db")

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        lang TEXT,
        tz TEXT,
        checkin_enabled INTEGER DEFAULT 0,
        profile_json TEXT,
        created_at TEXT,
        updated_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS episodes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, user_id INTEGER, lang TEXT,
        user_text TEXT, intent TEXT, slots_json TEXT, assistant_reply TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS checkins(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, user_id INTEGER, lang TEXT, mood TEXT, comment TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS reminders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, tz TEXT, text TEXT,
        hour INTEGER, minute INTEGER, rrule TEXT, enabled INTEGER DEFAULT 1
    )""")
    conn.commit()
    conn.close()

init_db()

def upsert_user(user_id: int, username: str, lang: str, tz: str, checkin_enabled: bool, profile: Dict[str,Any]):
    conn = db()
    cur = conn.cursor()
    now = dt.datetime.utcnow().isoformat()
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if cur.fetchone():
        cur.execute("""UPDATE users SET username=?, lang=?, tz=?, checkin_enabled=?, profile_json=?, updated_at=?
                       WHERE user_id=?""",
                    (username, lang, tz, int(checkin_enabled), json.dumps(profile, ensure_ascii=False), now, user_id))
    else:
        cur.execute("""INSERT INTO users(user_id, username, lang, tz, checkin_enabled, profile_json, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (user_id, username, lang, tz, int(checkin_enabled), json.dumps(profile, ensure_ascii=False), now, now))
    conn.commit()
    conn.close()

def load_user_row(user_id: int) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def log_episode(user_id: int, lang: str, user_text: str, intent: str, slots: Dict[str,Any], reply: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT INTO episodes(ts,user_id,lang,user_text,intent,slots_json,assistant_reply) VALUES(?,?,?,?,?,?,?)",
                (dt.datetime.utcnow().isoformat(), user_id, lang, user_text, intent, json.dumps(slots, ensure_ascii=False), reply))
    conn.commit()
    conn.close()

def add_checkin_row(user_id: int, lang: str, mood: str, comment: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT INTO checkins(ts,user_id,lang,mood,comment) VALUES(?,?,?,?,?)",
                (dt.datetime.utcnow().isoformat(), user_id, lang, mood, comment))
    conn.commit()
    conn.close()

def add_reminder(user_id: int, tz: str, text: str, hour: int, minute: int, rrule: str="DAILY") -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT INTO reminders(user_id,tz,text,hour,minute,rrule,enabled) VALUES(?,?,?,?,?,?,1)",
                (user_id, tz, text, hour, minute, rrule))
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid

def list_reminders(user_id: int) -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM reminders WHERE user_id=? AND enabled=1 ORDER BY id", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def disable_reminder(user_id: int, rid: int) -> bool:
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE reminders SET enabled=0 WHERE id=? AND user_id=?", (rid, user_id))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok

# ---------------- STATE ----------------
@dataclass
class UserState:
    lang: str = "en"
    tz: str = DEFAULT_TZ
    paused: bool = False
    intake_needed: bool = True
    profile: Dict[str, Any] = field(default_factory=dict)
    history: deque = field(default_factory=lambda: deque(maxlen=14))
    fast_mode: bool = False
    awaiting_checkin_comment: bool = False
    last_seen: float = field(default_factory=time.time)

STATE: Dict[int, UserState] = defaultdict(UserState)

# ---------------- TEXTS ----------------
TXT = {
    "greet": {
        "ru": "Привет, я TendAI — ассистент по здоровью и долголетию. Отвечаю коротко и по делу.",
        "en": "Hi, I’m TendAI — your health & longevity assistant. I’ll keep it short and useful.",
        "es": "Hola, soy TendAI — tu asistente de salud y longevidad. Iré al grano.",
        "uk": "Привіт, я TendAI — асистент зі здоров’я та довголіття. Коротко і по суті."
    },
    "privacy": {
        "ru": "Я не врач. Это не медицинская помощь. Не отправляй чувствительные данные. Ответы генерирует LLM.",
        "en": "I’m not a doctor. This is not medical care. Don’t send sensitive data. Answers are LLM-generated.",
        "es": "No soy médico. Esto no es atención médica. No envíes datos sensibles. Respuestas generadas por LLM.",
        "uk": "Я не лікар. Це не медична допомога. Не надсилай чутливі дані. Відповіді генерує LLM."
    },
    "intake": {
        "ru": (
            "Быстрый опрос (~40с). Ответь ОДНИМ сообщением, свободно:\n"
            "1) Возраст и пол.\n"
            "2) Главная цель (вес, энергия, сон, долголетие и т.п.).\n"
            "3) Основной симптом (если есть) и сколько длится.\n"
            "4) Хроника/операции/лекарства.\n"
            "5) Сон: во сколько ложишься/встаёшь.\n"
            "6) Активность: шаги/тренировки.\n"
            "7) Питание: как обычно ешь.\n"
            "8) Есть ли тревожные признаки (сильная боль в груди, одышка, кровь, обморок и т.п.)?"
        ),
        "en": (
            "Quick intake (~40s). Reply in ONE message, free text:\n"
            "1) Age & sex.\n"
            "2) Main goal (weight, energy, sleep, longevity, etc.).\n"
            "3) Main symptom (if any) and duration.\n"
            "4) Conditions/surgeries/meds.\n"
            "5) Sleep: usual bed/wake time.\n"
            "6) Activity: steps/workouts.\n"
            "7) Diet: typical meals.\n"
            "8) Any red flags (severe chest pain, shortness of breath, blood, fainting, etc.)?"
        ),
        "es": (
            "Intake rápido (~40s). Responde en UN solo mensaje:\n"
            "1) Edad y sexo.\n"
            "2) Meta principal (peso, energía, sueño, longevidad, etc.).\n"
            "3) Síntoma principal (si hay) y duración.\n"
            "4) Condiciones/cirugías/medicamentos.\n"
            "5) Sueño: hora de dormir/levantarte.\n"
            "6) Actividad: pasos/entrenos.\n"
            "7) Dieta: comidas típicas.\n"
            "8) ¿Bandera roja (dolor fuerte en pecho, falta de aire, sangre, desmayo, etc.)?"
        ),
        "uk": (
            "Швидкий опитувальник (~40с). Відповідай ОДНИМ повідомленням:\n"
            "1) Вік і стать.\n"
            "2) Головна мета (вага, енергія, сон, довголіття тощо).\n"
            "3) Основний симптом (якщо є) і тривалість.\n"
            "4) Хроніка/операції/ліки.\n"
            "5) Сон: коли лягаєш/прокидаєшся.\n"
            "6) Активність: кроки/тренування.\n"
            "7) Харчування: що зазвичай їси.\n"
            "8) Червоні прапорці (сильний біль у грудях, задишка, кров, непритомність тощо)?"
        ),
    },
    "checkin_hello": {
        "ru":"Доброе утро! Быстрый чек-ин: как самочувствие?",
        "en":"Good morning! Quick check-in: how do you feel today?",
        "es":"¡Buenos días! Chequeo rápido: ¿cómo te sientes hoy?",
        "uk":"Доброго ранку! Швидкий чек-ін: як самопочуття сьогодні?"
    }
}

def tget(key: str, lang: str) -> str:
    return TXT.get(key, {}).get(lang) or TXT.get(key, {}).get("en", "")

# ---------------- LANGUAGE ----------------
def detect_lang(update: Update, fallback="en") -> str:
    code = (update.effective_user.language_code or "").lower()
    text = (update.message.text or "").lower() if update.message else ""
    if code.startswith("ru"): return "ru"
    if code.startswith("uk"): return "uk"
    if code.startswith("es"): return "es"
    if code.startswith("en"): return "en"
    if any(w in text for w in ["привет","здравствуйте","самочувствие","боль"]): return "ru"
    if any(w in text for w in ["привіт","здоров","болить"]): return "uk"
    if any(w in text for w in ["hola","salud","dolor","¿","¡","ñ"]): return "es"
    if any(w in text for w in ["hello","hi","pain","sleep","diet"]): return "en"
    return fallback

# ---------------- LLM PROMPTS ----------------
SYS_INTAKE_PARSER = """
Extract user health profile from the text. Return MINIFIED JSON ONLY:
{"age":int|null,"sex":"male"|"female"|null,"goal":string|null,
 "main_symptom":string|null,"duration":string|null,
 "conditions":string[]|null,"surgeries":string[]|null,"meds":string[]|null,
 "sleep":{"bedtime":string|null,"waketime":string|null}|null,
 "activity":{"steps_per_day":int|null,"workouts":string|null}|null,
 "diet":string|null,"red_flags":bool|null,"language":"ru"|"en"|"es"|"uk"|null}
No prose.
"""

SYS_ROUTER = """
You are TendAI — a concise, human, professional health & longevity assistant (not a doctor).
Speak the user's language; keep answers compact (<=6 lines + up to 4 bullets).
Use the stored profile when relevant. No diagnosis or prescriptions.
TRIAGE RULE:
- FIRST ask 1–2 targeted clarifying questions (location, character, duration, intensity, context).
- ONLY advise ER if clear red flags are present; if uncertain, ask first and give safe steps.
Return MINIFIED JSON ONLY:
{"language":"ru"|"en"|"es"|"uk"|null,
 "intent":"symptom"|"nutrition"|"sleep"|"labs"|"habits"|"longevity"|"other",
 "slots":object,
 "severity":"low"|"moderate"|"high",
 "red_flags":boolean,
 "confidence":0..1,
 "assistant_reply":string,
 "followups":string[],      # <=2 short questions
 "needs_more":boolean}
Style: warm, respectful, science-informed, practical.
"""

def llm(messages: List[Dict[str,str]], model: str=OPENAI_MODEL, temperature: float=0.25, max_tokens: int=480) -> str:
    resp = client.chat.completions.create(model=model, temperature=temperature, max_tokens=max_tokens, messages=messages)
    return resp.choices[0].message.content.strip()

def parse_intake(text: str, lang_hint: str) -> Dict[str,Any]:
    try:
        out = llm([
            {"role":"system","content":SYS_INTAKE_PARSER},
            {"role":"user","content":text}
        ], temperature=0.0, max_tokens=450)
        m = re.search(r"\{.*\}\s*$", out, re.S)
        if m: out = m.group(0)
        data = json.loads(out)
        if not data.get("language"): data["language"] = lang_hint
        return data
    except Exception as e:
        log.warning(f"parse_intake failed: {e}")
        return {"language": lang_hint}

def route_and_answer(text: str, lang: str, profile: Dict[str,Any], history: List[Dict[str,str]], fast: bool=False) -> Dict[str,Any]:
    sys = SYS_ROUTER + f"\nUser language hint: {lang}. Fast mode: {str(fast)}. Stored profile: {json.dumps(profile, ensure_ascii=False)}"
    out = llm([
        {"role":"system","content":sys},
        {"role":"user","content":text}
    ], temperature=0.2, max_tokens=480)
    try:
        m = re.search(r"\{.*\}\s*$", out, re.S)
        if m: out = m.group(0)
        data = json.loads(out)
        # soften premature ER
        if data.get("red_flags") and float(data.get("confidence", 0)) < 0.6:
            data["red_flags"] = False
            data["needs_more"] = True
            if not data.get("followups"):
                data["followups"] = ["Where exactly, what kind of pain, and how long?"] if lang=="en" else \
                    ["Где именно, какой характер боли и сколько длится?"]
        return data
    except Exception as e:
        log.warning(f"route parse failed: {e}")
        brief = {
            "ru":"Коротко: опиши где/какая боль и сколько длится. База: фиксированный подъём, 7–10 тыс. шагов, овощи каждый приём, белок 1.2–1.6 г/кг/день.",
            "en":"Briefly: tell me where/what pain and for how long. Basics: fixed wake, 7–10k steps, veggies each meal, protein 1.2–1.6 g/kg/day.",
            "es":"Breve: dónde/qué dolor y cuánto dura. Básicos: despertar fijo, 7–10k pasos, verduras cada comida, proteína 1.2–1.6 g/kg/día.",
            "uk":"Коротко: де/який біль і скільки триває. База: фіксоване пробудження, 7–10 тис. кроків, овочі кожен прийом, білок 1.2–1.6 г/кг/день."
        }.get(lang, "en")
        return {"language":lang,"intent":"other","slots":{},"severity":"low","red_flags":False,"confidence":0.3,
                "assistant_reply":brief,"followups":[], "needs_more":True}

# ---------------- HELPERS ----------------
def ik(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)

def parse_hhmm(s: str) -> Optional[tuple]:
    m = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', s)
    if not m: return None
    return int(m.group(1)), int(m.group(2))

# ---------------- SCHEDULING ----------------
def schedule_daily(context: ContextTypes.DEFAULT_TYPE, chat_id: int, tz: str, hour: int, minute: int, name: str, data: dict, callback):
    # clear old with same name
    for j in context.job_queue.get_jobs_by_name(name):
        j.schedule_removal()
    t = dt.time(hour=hour, minute=minute, tzinfo=ZoneInfo(tz))
    context.job_queue.run_daily(callback=callback, time=t, name=name, data=data)

async def job_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    chat_id = d.get("chat_id")
    lang = d.get("lang", "en")
    kb = [[ik({"ru":"😃 Хорошо","en":"😃 Good","es":"😃 Bien","uk":"😃 Добре"}[lang], "checkin:good"),
           ik({"ru":"😐 Нормально","en":"😐 Okay","es":"😐 Normal","uk":"😐 Нормально"}[lang], "checkin:ok"),
           ik({"ru":"😣 Плохо","en":"😣 Poor","es":"😣 Mal","uk":"😣 Погано"}[lang], "checkin:bad")],
          [ik({"ru":"✍️ Комментарий","en":"✍️ Comment","es":"✍️ Comentario","uk":"✍️ Коментар"}[lang], "checkin:comment")]]
    await context.bot.send_message(chat_id, tget("checkin_hello", lang), reply_markup=InlineKeyboardMarkup(kb))

async def job_reminder(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    chat_id = d.get("chat_id")
    text = d.get("text", "Reminder")
    await context.bot.send_message(chat_id, text)

# ---------------- COMMANDS ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = STATE[uid]
    st.lang = detect_lang(update, st.lang)
    # load persisted user (tz, checkin, profile)
    row = load_user_row(uid)
    if row:
        st.tz = row["tz"] or DEFAULT_TZ
        st.profile = json.loads(row["profile_json"] or "{}")
        st.lang = row["lang"] or st.lang
        if row["checkin_enabled"]:
            # reschedule checkin
            schedule_daily(context, update.effective_chat.id, st.tz, 8, 30, f"checkin_{uid}", {"chat_id": update.effective_chat.id, "lang": st.lang}, job_checkin)
    await update.effective_chat.send_message(tget("greet", st.lang), reply_markup=ReplyKeyboardRemove())
    await update.effective_chat.send_message(tget("privacy", st.lang))
    # immediate intake
    await update.effective_chat.send_message(TXT["intake"][st.lang])
    st.intake_needed = True

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STATE[update.effective_user.id] = UserState(lang=detect_lang(update, "en"))
    await update.effective_chat.send_message("Reset. /start", reply_markup=ReplyKeyboardRemove())

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]
    await update.effective_chat.send_message(tget("privacy", st.lang))

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]
    await update.effective_chat.send_message("Profile:\n" + json.dumps(st.profile or {}, ensure_ascii=False, indent=2))

async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]
    sys = ("You are TendAI. Create a compact 4–6 step plan ONLY from this profile. "
           "Short sentences + bullets; no diagnosis; friendly tone.")
    try:
        reply = llm([{"role":"system","content":sys},
                     {"role":"user","content":json.dumps(st.profile, ensure_ascii=False)}],
                    temperature=0.2, max_tokens=420)
    except Exception as e:
        log.warning(f"plan failed: {e}")
        reply = {"ru":"Не удалось сгенерировать план сейчас.",
                 "en":"Couldn’t generate a plan now.",
                 "es":"No se pudo generar un plan ahora.",
                 "uk":"Не вдалося створити план зараз."}.get(st.lang,"en")
    await update.effective_chat.send_message(reply)

async def cmd_fast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]
    st.fast_mode = True
    msg = {"ru":"Чек-ин «за 60 секунд». Выберите вариант или ответьте текстом:\n— Хорошо / Нормально / Плохо\n— Как спал? Хорошо / Прерывисто / Не выспался\n— Есть боль? Где/какая/сколько длится?",
           "en":"60-second check-in. Pick an option or type freely:\n— Good / Okay / Poor\n— Sleep: Good / Fragmented / Poor\n— Any pain? Where/what/how long?",
           "es":"Chequeo de 60s. Elige opción o escribe:\n— Bien / Normal / Mal\n— Sueño: Bien / Fragmentado / Mal\n— ¿Dolor? Dónde/qué/cuánto tiempo?",
           "uk":"Чек-ін 60с. Оберіть варіант або напишіть:\n— Добре / Нормально / Погано\n— Сон: Добре / Переривчастий / Погано\n— Біль? Де/який/скільки?"}[st.lang]
    await update.effective_chat.send_message(msg)

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]; st.paused = True
    await update.effective_chat.send_message({"ru":"Пауза. Я не буду писать первым. /resume — вернуть.",
                                              "en":"Paused. I won’t message first. /resume to enable.",
                                              "es":"En pausa. No iniciaré mensajes. /resume para activar.",
                                              "uk":"Пауза. Я не писатиму першим. /resume щоб увімкнути."}[st.lang])

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]; st.paused = False
    await update.effective_chat.send_message({"ru":"Возобновил работу.","en":"Resumed.","es":"Reanudado.","uk":"Відновлено."}[st.lang])

async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]
    args = (update.message.text or "").split()
    if len(args) < 2:
        await update.effective_chat.send_message({"ru":"Укажи таймзону, напр. Europe/Kyiv",
                                                  "en":"Provide timezone, e.g., Europe/Kyiv",
                                                  "es":"Indica timezone, p.ej., Europe/Madrid",
                                                  "uk":"Вкажи таймзону, напр., Europe/Kyiv"}[st.lang])
        return
    tz = args[1]
    try:
        ZoneInfo(tz)
        st.tz = tz
        row = load_user_row(update.effective_user.id)
        upsert_user(update.effective_user.id, update.effective_user.username or "", st.lang, st.tz,
                    bool(row["checkin_enabled"] if row else False), STATE[update.effective_user.id].profile)
        await update.effective_chat.send_message({"ru":f"Ок, таймзона: {tz}", "en":f"Timezone set: {tz}",
                                                  "es":f"Zona horaria: {tz}", "uk":f"Часовий пояс: {tz}"}[st.lang])
    except Exception:
        await update.effective_chat.send_message({"ru":"Неверная таймзона.","en":"Invalid timezone.","es":"Zona horaria inválida.","uk":"Неправильний часовий пояс."}[st.lang])

async def cmd_checkin_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]
    st.lang = st.lang or detect_lang(update, "en")
    # persist flag and schedule 08:30
    schedule_daily(context, update.effective_chat.id, st.tz, 8, 30, f"checkin_{update.effective_user.id}", {"chat_id": update.effective_chat.id, "lang": st.lang}, job_checkin)
    upsert_user(update.effective_user.id, update.effective_user.username or "", st.lang, st.tz, True, st.profile)
    await update.effective_chat.send_message({"ru":"Утренний чек-ин включен (08:30).","en":"Morning check-in enabled (08:30).",
                                              "es":"Check-in matutino activado (08:30).","uk":"Ранковий чек-ін увімкнено (08:30)."}[st.lang])

async def cmd_checkin_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]
    for j in context.job_queue.get_jobs_by_name(f"checkin_{update.effective_user.id}"): j.schedule_removal()
    upsert_user(update.effective_user.id, update.effective_user.username or "", st.lang, st.tz, False, st.profile)
    await update.effective_chat.send_message({"ru":"Чек-ин отключен.","en":"Check-in disabled.","es":"Check-in desactivado.","uk":"Чек-ін вимкнено."}[st.lang])

async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /remind <text> at HH:MM [daily] """
    st = STATE[update.effective_user.id]
    raw = (update.message.text or "")
    m_time = parse_hhmm(raw)
    if not m_time:
        await update.effective_chat.send_message({"ru":"Формат: /remind выпить воду at 16:00",
                                                  "en":"Format: /remind drink water at 16:00",
                                                  "es":"Formato: /remind beber agua at 16:00",
                                                  "uk":"Формат: /remind випити воду at 16:00"}[st.lang])
        return
    hour, minute = m_time
    text = re.sub(r'/remind', '', raw, flags=re.I).strip()
    text = re.sub(r'\bat\s*[0-2]?\d:[0-5]\d.*', '', text).strip() or {"ru":"Напоминание","en":"Reminder","es":"Recordatorio","uk":"Нагадування"}[st.lang]
    rid = add_reminder(update.effective_user.id, st.tz, text, hour, minute, "DAILY")
    schedule_daily(context, update.effective_chat.id, st.tz, hour, minute, f"rem_{rid}", {"chat_id": update.effective_chat.id, "text": text}, job_reminder)
    await update.effective_chat.send_message({"ru":f"Ок, буду напоминать ежедневно в {hour:02d}:{minute:02d} — «{text}». (id {rid})",
                                              "en":f"Got it. I’ll remind you daily at {hour:02d}:{minute:02d}: “{text}”. (id {rid})",
                                              "es":f"Hecho. Te recordaré cada día a las {hour:02d}:{minute:02d}: “{text}”. (id {rid})",
                                              "uk":f"Гаразд. Нагадуватиму щодня о {hour:02d}:{minute:02d}: «{text}». (id {rid})"}[st.lang])

async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]
    rows = list_reminders(update.effective_user.id)
    if not rows:
        await update.effective_chat.send_message({"ru":"Активных напоминаний нет.","en":"No active reminders.","es":"No hay recordatorios activos.","uk":"Активних нагадувань немає."}[st.lang])
        return
    lines = []
    for r in rows:
        lines.append(f"{r['id']}: {r['text']} — {r['hour']:02d}:{r['minute']:02d} ({r['rrule']})")
    await update.effective_chat.send_message("\n".join(lines))

async def cmd_delremind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]
    args = (update.message.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await update.effective_chat.send_message({"ru":"Формат: /delremind <id>","en":"Usage: /delremind <id>",
                                                  "es":"Uso: /delremind <id>","uk":"Формат: /delremind <id>"}[st.lang])
        return
    rid = int(args[1])
    ok = disable_reminder(update.effective_user.id, rid)
    for j in context.job_queue.get_jobs_by_name(f"rem_{rid}"): j.schedule_removal()
    await update.effective_chat.send_message({"ru":("Удалил." if ok else "Не найдено."),
                                              "en":("Deleted." if ok else "Not found."),
                                              "es":("Eliminado." if ok else "No encontrado."),
                                              "uk":("Видалено." if ok else "Не знайдено.")}[st.lang])

# ---------------- CALLBACKS (check-in) ----------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = STATE[uid]
    data = update.callback_query.data or ""
    await update.callback_query.answer()
    if data in ["checkin:good","checkin:ok","checkin:bad"]:
        mood = data.split(":")[1]
        add_checkin_row(uid, st.lang, mood, "")
        await update.effective_chat.send_message({"ru":"Спасибо! Хорошего дня 👋",
                                                  "en":"Thanks! Have a great day 👋",
                                                  "es":"¡Gracias! Buen día 👋",
                                                  "uk":"Дякую! Гарного дня 👋"}[st.lang])
        return
    if data == "checkin:comment":
        st.awaiting_checkin_comment = True
        await update.effective_chat.send_message({"ru":"Коротко опиши самочувствие:",
                                                  "en":"Write a short note about how you feel:",
                                                  "es":"Escribe una nota breve sobre cómo te sientes:",
                                                  "uk":"Коротко опиши самопочуття:"}[st.lang])

# ---------------- MESSAGE HANDLER ----------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = STATE[uid]
    st.lang = st.lang or detect_lang(update, "en")
    text = update.message.text or ""
    st.last_seen = time.time()

    # check-in comment path
    if st.awaiting_checkin_comment:
        st.awaiting_checkin_comment = False
        add_checkin_row(uid, st.lang, "", text)
        await update.effective_chat.send_message({"ru":"Записал. Береги себя.","en":"Noted. Take care.",
                                                  "es":"Anotado. Cuídate.","uk":"Занотував. Бережи себе."}[st.lang])
        return

    # intake first
    if st.intake_needed:
        parsed = parse_intake(text, st.lang)
        # store profile
        for k,v in parsed.items():
            if v in [None,"",[]]: continue
            # merge shallowly
            if isinstance(v, dict):
                st.profile.setdefault(k, {}).update({ik:iv for ik,iv in v.items() if iv not in [None,"",[]]})
            else:
                st.profile[k] = v
        st.intake_needed = False
        # persist user
        upsert_user(uid, update.effective_user.username or "", parsed.get("language", st.lang), st.tz, False, st.profile)
        st.lang = parsed.get("language", st.lang)
        summary = json.dumps(st.profile, ensure_ascii=False, indent=2)
        head = {"ru":"Принял. Резюме профиля:\n","en":"Got it. Profile summary:\n",
                "es":"Entendido. Resumen del perfil:\n","uk":"Прийнято. Підсумок профілю:\n"}[st.lang]
        await update.effective_chat.send_message(head + summary)
        nextq = {"ru":"С чего начнём сейчас? (симптом/сон/питание/анализы/привычки/долголетие)",
                 "en":"Where do you want to start now? (symptom/sleep/nutrition/labs/habits/longevity)",
                 "es":"¿Por dónde empezamos ahora? (síntoma/sueño/nutrición/análisis/hábitos/longevidad)",
                 "uk":"З чого почнемо зараз? (симптом/сон/харчування/аналізи/звички/довголіття)"}[st.lang]
        await update.effective_chat.send_message(nextq)
        return

    # paused?
    if st.paused:
        await update.effective_chat.send_message({"ru":"Пауза включена. /resume чтобы вернуть.",
                                                  "en":"Paused. Use /resume to enable.",
                                                  "es":"En pausa. /resume para activar.",
                                                  "uk":"Пауза. /resume щоб увімкнути."}[st.lang])
        return

    # LLM dialog
    st.history.append({"role":"user","content": text})
    data = route_and_answer(text, st.lang, st.profile, list(st.history), fast=st.fast_mode)
    if isinstance(data.get("language"), str):
        st.lang = data["language"]
    reply = (data.get("assistant_reply") or "").strip()
    if not reply:
        reply = {"ru":"Сформулируй, пожалуйста, цель или вопрос одним предложением.",
                 "en":"Please state your goal or question in one sentence.",
                 "es":"Indica tu objetivo o pregunta en una frase.",
                 "uk":"Сформулюй мету або питання одним реченням."}[st.lang]
    st.fast_mode = False
    st.history.append({"role":"assistant","content": reply})
    await update.effective_chat.send_message(reply, reply_markup=ReplyKeyboardRemove())

    # log
    log_episode(uid, st.lang, text, data.get("intent",""), data.get("slots",{}), reply)

    # follow-ups
    if data.get("needs_more") and data.get("followups"):
        for q in data["followups"][:2]:
            await update.effective_chat.send_message(q)

# ---------------- MAIN ----------------
def main():
    if TELEGRAM_TOKEN == "YOUR_TELEGRAM_TOKEN_HERE":
        log.warning("Set TELEGRAM_TOKEN")
    if OPENAI_API_KEY == "YOUR_OPENAI_API_KEY_HERE":
        log.warning("Set OPENAI_API_KEY")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("fast", cmd_fast))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("settz", cmd_settz))
    app.add_handler(CommandHandler("checkin_on", cmd_checkin_on))
    app.add_handler(CommandHandler("checkin_off", cmd_checkin_off))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("delremind", cmd_delremind))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("TendAI bot running…")
    app.run_polling()

if __name__ == "__main__":
    main()
