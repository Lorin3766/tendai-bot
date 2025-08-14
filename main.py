#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TendAI — LLM-powered Health & Longevity Assistant (RU/EN/ES/UA)

Исправления:
- Принудительный запуск опросника по ключевым словам.
- Минимальный порог intake (>=2 ключевых поля), иначе просьба дописать с примером.
- Человечное резюме профиля (без JSON наружу).
- Жёсткая фиксация языка ответа LLM (Answer strictly in {lang}).
- Запрет на фразы про «нет доступа».
- Мягкая триаж-логика (уточнения перед ER).
- Чек-ин 60с, напоминания, SQLite-память.

Команды: /start /reset /privacy /profile /plan /fast /pause /resume /settz /checkin_on /checkin_off /remind /reminders /delremind
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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
)

from openai import OpenAI

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN_HERE")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY_HERE")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Kyiv")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
log = logging.getLogger("tendai")

client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------- DB (SQLite) ----------------
DB_PATH = os.getenv("TEND_AI_DB", "tendai.db")

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db(); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, username TEXT, lang TEXT, tz TEXT,
        checkin_enabled INTEGER DEFAULT 0, profile_json TEXT, created_at TEXT, updated_at TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS episodes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER, lang TEXT,
        user_text TEXT, intent TEXT, slots_json TEXT, assistant_reply TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS checkins(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER, lang TEXT, mood TEXT, comment TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS reminders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, tz TEXT, text TEXT,
        hour INTEGER, minute INTEGER, rrule TEXT, enabled INTEGER DEFAULT 1)""")
    conn.commit(); conn.close()
init_db()

def upsert_user(user_id:int, username:str, lang:str, tz:str, checkin_enabled:bool, profile:Dict[str,Any]):
    now = dt.datetime.utcnow().isoformat()
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if cur.fetchone():
        cur.execute("""UPDATE users SET username=?,lang=?,tz=?,checkin_enabled=?,profile_json=?,updated_at=? WHERE user_id=?""",
                    (username, lang, tz, int(checkin_enabled), json.dumps(profile, ensure_ascii=False), now, user_id))
    else:
        cur.execute("""INSERT INTO users(user_id,username,lang,tz,checkin_enabled,profile_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (user_id, username, lang, tz, int(checkin_enabled), json.dumps(profile, ensure_ascii=False), now, now))
    conn.commit(); conn.close()

def load_user_row(user_id:int) -> Optional[sqlite3.Row]:
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    r = cur.fetchone(); conn.close(); return r

def log_episode(user_id:int, lang:str, user_text:str, intent:str, slots:Dict[str,Any], reply:str):
    conn = db(); cur = conn.cursor()
    cur.execute("INSERT INTO episodes(ts,user_id,lang,user_text,intent,slots_json,assistant_reply) VALUES(?,?,?,?,?,?,?)",
                (dt.datetime.utcnow().isoformat(), user_id, lang, user_text, intent, json.dumps(slots, ensure_ascii=False), reply))
    conn.commit(); conn.close()

def add_checkin_row(user_id:int, lang:str, mood:str, comment:str):
    conn = db(); cur = conn.cursor()
    cur.execute("INSERT INTO checkins(ts,user_id,lang,mood,comment) VALUES(?,?,?,?,?)",
                (dt.datetime.utcnow().isoformat(), user_id, lang, mood, comment))
    conn.commit(); conn.close()

def add_reminder(user_id:int, tz:str, text:str, hour:int, minute:int, rrule:str="DAILY") -> int:
    conn = db(); cur = conn.cursor()
    cur.execute("INSERT INTO reminders(user_id,tz,text,hour,minute,rrule,enabled) VALUES(?,?,?,?,?,?,1)",
                (user_id, tz, text, hour, minute, rrule))
    rid = cur.lastrowid
    conn.commit(); conn.close(); return rid

def list_reminders(user_id:int) -> List[sqlite3.Row]:
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT * FROM reminders WHERE user_id=? AND enabled=1 ORDER BY id", (user_id,))
    rows = cur.fetchall(); conn.close(); return rows

def disable_reminder(user_id:int, rid:int) -> bool:
    conn = db(); cur = conn.cursor()
    cur.execute("UPDATE reminders SET enabled=0 WHERE id=? AND user_id=?", (rid, user_id))
    conn.commit(); ok = cur.rowcount>0; conn.close(); return ok

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
        "ru":"Привет, я TendAI — ассистент по здоровью и долголетию. Отвечаю коротко и по делу.",
        "en":"Hi, I’m TendAI — your health & longevity assistant. I’ll keep it short and useful.",
        "es":"Hola, soy TendAI — tu asistente de salud y longevidad. Iré al grano.",
        "uk":"Привіт, я TendAI — асистент зі здоров’я та довголіття. Коротко і по суті."
    },
    "privacy": {
        "ru":"Я не врач. Это не медпомощь. Не отправляй чувствительные данные. Ответы генерирует LLM.",
        "en":"I’m not a doctor. This is not medical care. Don’t send sensitive data. Answers are LLM-generated.",
        "es":"No soy médico. Esto no es atención médica. No envíes datos sensibles. Respuestas generadas por LLM.",
        "uk":"Я не лікар. Це не медична допомога. Не надсилай чутливі дані. Відповіді генерує LLM."
    },
    "intake": {
        "ru":("Быстрый опрос (~40с). Ответь ОДНИМ сообщением, свободно:\n"
              "1) Возраст и пол.\n2) Главная цель (вес, энергия, сон, долголетие и т.п.).\n"
              "3) Основной симптом (если есть) и сколько длится.\n4) Хроника/операции/лекарства.\n"
              "5) Сон: когда ложишься/встаёшь.\n6) Активность: шаги/тренировки.\n"
              "7) Питание: как обычно ешь.\n8) Есть ли тревожные признаки (сильная боль в груди, одышка, кровь, обморок и т.п.)?"),
        "en":("Quick intake (~40s). Reply in ONE message, free text:\n"
              "1) Age & sex.\n2) Main goal (weight, energy, sleep, longevity, etc.).\n"
              "3) Main symptom (if any) & duration.\n4) Conditions/surgeries/meds.\n"
              "5) Sleep: usual bed/wake.\n6) Activity: steps/workouts.\n"
              "7) Diet: typical meals.\n8) Any red flags (severe chest pain, shortness of breath, blood, fainting, etc.)?"),
        "es":("Intake rápido (~40s). Responde en UN mensaje:\n"
              "1) Edad y sexo.\n2) Meta principal (peso, energía, sueño, longevidad, etc.).\n"
              "3) Síntoma principal (si hay) & duración.\n4) Condiciones/cirugías/medicamentos.\n"
              "5) Sueño: hora dormir/levantarte.\n6) Actividad: pasos/entrenos.\n"
              "7) Dieta: comidas típicas.\n8) ¿Banderas rojas (dolor fuerte en pecho, falta de aire, sangre, desmayo, etc.)?"),
        "uk":("Швидкий опитувальник (~40с). Відповідай ОДНИМ повідомленням:\n"
              "1) Вік і стать.\n2) Головна мета (вага, енергія, сон, довголіття тощо).\n"
              "3) Основний симптом (якщо є) і тривалість.\n4) Хроніка/операції/ліки.\n"
              "5) Сон: коли лягаєш/прокидаєшся.\n6) Активність: кроки/тренування.\n"
              "7) Харчування: що зазвичай їси.\n8) Червоні прапорці (біль у грудях, задишка, кров, непритомність тощо)?")
    },
    "checkin_hello": {
        "ru":"Доброе утро! Быстрый чек-ин: как самочувствие?",
        "en":"Good morning! Quick check-in: how do you feel today?",
        "es":"¡Buenos días! Chequeo rápido: ¿cómo te sientes hoy?",
        "uk":"Доброго ранку! Швидкий чек-ін: як самопочуття сьогодні?"
    }
}
def tget(key:str, lang:str) -> str:
    return TXT.get(key, {}).get(lang) or TXT.get(key, {}).get("en", "")

# ---------------- LANGUAGE ----------------
def detect_lang(update:Update, fallback="en")->str:
    code=(update.effective_user.language_code or "").lower()
    text=(update.message.text or "").lower() if update.message else ""
    if code.startswith("ru"): return "ru"
    if code.startswith("uk"): return "uk"
    if code.startswith("es"): return "es"
    if code.startswith("en"): return "en"
    if any(w in text for w in ["привет","здравствуйте","самочувствие","боль"]): return "ru"
    if any(w in text for w in ["привіт","здоров","болить"]): return "uk"
    if any(w in text for w in ["hola","salud","dolor","¿","¡","ñ"]): return "es"
    if any(w in text for w in ["hello","hi","pain","sleep","diet"]): return "en"
    return fallback

# ---------------- INTAKE CONTROL ----------------
FORCE_INTAKE_PHRASES = [
    "опрос", "опросник", "анкета", "дай опросник", "где опросник",
    "questionnaire", "survey", "give questionnaire", "start intake"
]

def wants_intake(text:str)->bool:
    t=text.lower()
    return any(p in t for p in FORCE_INTAKE_PHRASES)

PRIMARY_KEYS = ["age","sex","goal","main_symptom"]

def intake_min_ok(profile:Dict[str,Any])->bool:
    cnt=0
    for k in PRIMARY_KEYS:
        v=profile.get(k)
        if isinstance(v, dict): 
            # shouldn't happen for these keys
            continue
        if v not in [None,"",[]]:
            cnt+=1
    return cnt>=2  # требуем минимум 2 ключевых поля

def humanize_profile(p:Dict[str,Any], lang:str)->str:
    parts=[]
    # age & sex
    if p.get("sex"): parts.append(("м" if p["sex"]=="male" else "ж") if lang=="ru" else p["sex"])
    if p.get("age"): parts.append(f"{p['age']}")
    if p.get("goal"): parts.append({"ru":"цель — ","en":"goal — ","es":"meta — ","uk":"мета — "}[lang]+str(p["goal"]))
    if p.get("main_symptom"):
        d = ("; "+str(p.get("duration"))) if p.get("duration") else ""
        parts.append({"ru":"симптом — ","en":"symptom — ","es":"síntoma — ","uk":"симптом — "}[lang]+str(p["main_symptom"])+d)
    if p.get("sleep") and any(p["sleep"].get(k) for k in ["bedtime","waketime"]):
        parts.append({"ru":"сон — ","en":"sleep — ","es":"sueño — ","uk":"сон — "}[lang]+f"{p['sleep'].get('bedtime','?')}/{p['sleep'].get('waketime','?')}")
    if p.get("activity") and (p["activity"].get("steps_per_day") or p["activity"].get("workouts")):
        act=[]
        if p["activity"].get("steps_per_day"): act.append(f"{p['activity']['steps_per_day']} steps")
        if p["activity"].get("workouts"): act.append(p["activity"]["workouts"])
        parts.append({"ru":"активность — ","en":"activity — ","es":"actividad — ","uk":"активність — "}[lang]+"; ".join(act))
    if p.get("diet"): parts.append({"ru":"питание — ","en":"diet — ","es":"dieta — ","uk":"харчування — "}[lang]+str(p["diet"]))
    if not parts:
        return {"ru":"Пока у меня только язык общения. Добавь возраст/пол/цель/симптом — и я настрою рекомендации.",
                "en":"I only know your language so far. Add age/sex/goal/symptom to personalize.",
                "es":"Por ahora solo conozco tu idioma. Añade edad/sexo/meta/síntoma para personalizar.",
                "uk":"Поки я знаю лише мову. Додай вік/стать/мету/симптом — і персоналізую відповіді."}[lang]
    return "; ".join(parts)

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

SYS_ROUTER_BASE = """
You are TendAI — a concise, warm, professional health & longevity assistant (not a doctor).
Always answer strictly in {lang}. Do not switch languages unless explicitly requested.
Never say you have no access to a questionnaire; if asked, guide the intake yourself.
Keep answers compact (<=6 lines + up to 4 bullets). Use stored profile when helpful.
TRIAGE: Ask 1–2 targeted clarifiers first. Advise ER only for clear red flags with high confidence.
Return MINIFIED JSON ONLY:
{"language":"ru"|"en"|"es"|"uk"|null,
 "intent":"symptom"|"nutrition"|"sleep"|"labs"|"habits"|"longevity"|"other",
 "slots":object,"severity":"low"|"moderate"|"high","red_flags":boolean,"confidence":0..1,
 "assistant_reply":string,"followups":string[],"needs_more":boolean}
"""

def llm(messages:List[Dict[str,str]], model:str=OPENAI_MODEL, temperature:float=0.25, max_tokens:int=480)->str:
    resp = client.chat.completions.create(model=model, temperature=temperature, max_tokens=max_tokens, messages=messages)
    return resp.choices[0].message.content.strip()

def parse_intake(text:str, lang_hint:str)->Dict[str,Any]:
    try:
        out = llm([{"role":"system","content":SYS_INTAKE_PARSER},
                   {"role":"user","content":text}], temperature=0.0, max_tokens=450)
        m=re.search(r"\{.*\}\s*$", out, re.S); out=m.group(0) if m else out
        data=json.loads(out)
        if not data.get("language"): data["language"]=lang_hint
        return data
    except Exception as e:
        log.warning(f"parse_intake failed: {e}")
        return {"language": lang_hint}

def route_and_answer(text:str, lang:str, profile:Dict[str,Any], history:List[Dict[str,str]], fast:bool=False)->Dict[str,Any]:
    sys = SYS_ROUTER_BASE.format(lang=lang) + f"\nFast mode: {str(fast)}. Stored profile: {json.dumps(profile, ensure_ascii=False)}"
    out = llm([{"role":"system","content":sys},{"role":"user","content":text}], temperature=0.2, max_tokens=480)
    try:
        m=re.search(r"\{.*\}\s*$", out, re.S); out=m.group(0) if m else out
        data=json.loads(out)
        # soften premature ER
        if data.get("red_flags") and float(data.get("confidence",0)) < 0.6:
            data["red_flags"]=False; data["needs_more"]=True
            if not data.get("followups"):
                data["followups"]=["Где именно/какой характер/сколько длится?"] if lang=="ru" else ["Where exactly/what character/how long?"]
        return data
    except Exception as e:
        log.warning(f"route parse failed: {e}")
        brief={"ru":"Коротко: опиши где/какая боль и сколько длится. База: фиксированный подъём, 7–10 тыс. шагов, овощи каждый приём, белок 1.2–1.6 г/кг/день.",
               "en":"Briefly: where/what pain and how long. Basics: fixed wake, 7–10k steps, veggies each meal, protein 1.2–1.6 g/kg/day.",
               "es":"Breve: dónde/qué dolor y cuánto dura. Básicos: despertar fijo, 7–10k pasos, verduras cada comida, proteína 1.2–1.6 g/kg/día.",
               "uk":"Коротко: де/який біль і скільки триває. База: фіксоване пробудження, 7–10 тис. кроків, овочі кожен прийом, білок 1.2–1.6 г/кг/день."}.get(lang,"en")
        return {"language":lang,"intent":"other","slots":{},"severity":"low","red_flags":False,"confidence":0.3,
                "assistant_reply":brief,"followups":[], "needs_more":True}

# ---------------- HELPERS ----------------
def ik(text:str, data:str)->InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)

def parse_hhmm(s:str)->Optional[tuple]:
    m=re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', s)
    return (int(m.group(1)), int(m.group(2))) if m else None

# ---------------- SCHEDULING ----------------
def schedule_daily(context:ContextTypes.DEFAULT_TYPE, chat_id:int, tz:str, hour:int, minute:int, name:str, data:dict, callback):
    for j in context.job_queue.get_jobs_by_name(name): j.schedule_removal()
    t=dt.time(hour=hour, minute=minute, tzinfo=ZoneInfo(tz))
    context.job_queue.run_daily(callback=callback, time=t, name=name, data=data)

async def job_checkin(context:ContextTypes.DEFAULT_TYPE):
    d=context.job.data or {}; chat_id=d.get("chat_id"); lang=d.get("lang","en")
    kb=[[ik({"ru":"😃 Хорошо","en":"😃 Good","es":"😃 Bien","uk":"😃 Добре"}[lang],"checkin:good"),
         ik({"ru":"😐 Нормально","en":"😐 Okay","es":"😐 Normal","uk":"😐 Нормально"}[lang],"checkin:ok"),
         ik({"ru":"😣 Плохо","en":"😣 Poor","es":"😣 Mal","uk":"😣 Погано"}[lang],"checkin:bad")],
        [ik({"ru":"✍️ Комментарий","en":"✍️ Comment","es":"✍️ Comentario","uk":"✍️ Коментар"}[lang],"checkin:comment")]]
    await context.bot.send_message(chat_id, tget("checkin_hello", lang), reply_markup=InlineKeyboardMarkup(kb))

async def job_reminder(context:ContextTypes.DEFAULT_TYPE):
    d=context.job.data or {}; chat_id=d.get("chat_id"); text=d.get("text","Reminder")
    await context.bot.send_message(chat_id, text)

# ---------------- COMMANDS ----------------
async def cmd_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    st=STATE[uid]; st.lang=detect_lang(update, st.lang); st.intake_needed=True
    # load persist
    row=load_user_row(uid)
    if row:
        st.tz=row["tz"] or DEFAULT_TZ
        st.profile=json.loads(row["profile_json"] or "{}")
        st.lang=row["lang"] or st.lang
        if row["checkin_enabled"]:
            schedule_daily(context, update.effective_chat.id, st.tz, 8, 30, f"checkin_{uid}", {"chat_id":update.effective_chat.id,"lang":st.lang}, job_checkin)
    await update.effective_chat.send_message(tget("greet", st.lang), reply_markup=ReplyKeyboardRemove())
    await update.effective_chat.send_message(tget("privacy", st.lang))
    await update.effective_chat.send_message(TXT["intake"][st.lang])

async def cmd_reset(update:Update, context:ContextTypes.DEFAULT_TYPE):
    STATE[update.effective_user.id]=UserState(lang=detect_lang(update,"en"))
    await update.effective_chat.send_message("Reset. /start", reply_markup=ReplyKeyboardRemove())

async def cmd_privacy(update:Update, context:ContextTypes.DEFAULT_TYPE):
    st=STATE[update.effective_user.id]; await update.effective_chat.send_message(tget("privacy", st.lang))

async def cmd_profile(update:Update, context:ContextTypes.DEFAULT_TYPE):
    st=STATE[update.effective_user.id]
    await update.effective_chat.send_message("Profile (internal):\n"+json.dumps(st.profile or {}, ensure_ascii=False, indent=2))

async def cmd_plan(update:Update, context:ContextTypes.DEFAULT_TYPE):
    st=STATE[update.effective_user.id]
    sys=("You are TendAI. Create a compact 4–6 step plan ONLY from this profile. "
         "Short sentences + bullets; friendly; no diagnosis.")
    try:
        reply = llm([{"role":"system","content":sys},{"role":"user","content":json.dumps(st.profile, ensure_ascii=False)}],
                    temperature=0.2, max_tokens=420)
    except Exception as e:
        log.warning(f"plan failed: {e}")
        reply={"ru":"Не удалось сгенерировать план сейчас.","en":"Couldn’t generate a plan now.",
               "es":"No se pudo generar un plan ahora.","uk":"Не вдалося створити план зараз."}.get(st.lang,"en")
    await update.effective_chat.send_message(reply)

async def cmd_fast(update:Update, context:ContextTypes.DEFAULT_TYPE):
    st=STATE[update.effective_user.id]; st.fast_mode=True
    msg={"ru":"Чек-ин 60с. Можно выбрать: Хорошо/Нормально/Плохо, и дописать свой комментарий. Если есть симптом — где/какой/сколько длится?",
         "en":"60s check-in. Pick: Good/Okay/Poor and add a note. If any symptom — where/what/how long?",
         "es":"Chequeo 60s. Elige: Bien/Normal/Mal y agrega nota. Si hay síntoma — dónde/qué/cuánto tiempo?",
         "uk":"Чек-ін 60с. Обери: Добре/Нормально/Погано і додай нотатку. Якщо є симптом — де/який/скільки?"}[st.lang]
    await update.effective_chat.send_message(msg)

async def cmd_pause(update:Update, context:ContextTypes.DEFAULT_TYPE):
    st=STATE[update.effective_user.id]; st.paused=True
    await update.effective_chat.send_message({"ru":"Пауза. /resume чтобы вернуть.","en":"Paused. /resume to enable.",
                                              "es":"En pausa. /resume para activar.","uk":"Пауза. /resume щоб увімкнути."}[st.lang])

async def cmd_resume(update:Update, context:ContextTypes.DEFAULT_TYPE):
    st=STATE[update.effective_user.id]; st.paused=False
    await update.effective_chat.send_message({"ru":"Возобновил работу.","en":"Resumed.","es":"Reanudado.","uk":"Відновлено."}[st.lang])

async def cmd_settz(update:Update, context:ContextTypes.DEFAULT_TYPE):
    st=STATE[update.effective_user.id]; args=(update.message.text or "").split()
    if len(args)<2:
        await update.effective_chat.send_message({"ru":"Укажи таймзону, напр. Europe/Kyiv","en":"Provide timezone, e.g., Europe/Kyiv",
                                                  "es":"Indica zona horaria, p.ej. Europe/Madrid","uk":"Вкажи часовий пояс, напр. Europe/Kyiv"}[st.lang]); return
    tz=args[1]
    try:
        ZoneInfo(tz); st.tz=tz
        row=load_user_row(update.effective_user.id)
        upsert_user(update.effective_user.id, update.effective_user.username or "", st.lang, st.tz, bool(row["checkin_enabled"] if row else False), st.profile)
        await update.effective_chat.send_message({"ru":f"Таймзона: {tz}","en":f"Timezone set: {tz}","es":f"Zona horaria: {tz}","uk":f"Часовий пояс: {tz}"}[st.lang])
    except Exception:
        await update.effective_chat.send_message({"ru":"Неверная таймзона.","en":"Invalid timezone.","es":"Zona horaria inválida.","uk":"Неправильний часовий пояс."}[st.lang])

async def cmd_checkin_on(update:Update, context:ContextTypes.DEFAULT_TYPE):
    st=STATE[update.effective_user.id]
    schedule_daily(context, update.effective_chat.id, st.tz, 8, 30, f"checkin_{update.effective_user.id}", {"chat_id":update.effective_chat.id,"lang":st.lang}, job_checkin)
    upsert_user(update.effective_user.id, update.effective_user.username or "", st.lang, st.tz, True, st.profile)
    await update.effective_chat.send_message({"ru":"Утренний чек-ин включен (08:30).","en":"Morning check-in enabled (08:30).",
                                              "es":"Check-in matutino activado (08:30).","uk":"Ранковий чек-ін увімкнено (08:30)."}[st.lang])

async def cmd_checkin_off(update:Update, context:ContextTypes.DEFAULT_TYPE):
    for j in context.job_queue.get_jobs_by_name(f"checkin_{update.effective_user.id}"): j.schedule_removal()
    st=STATE[update.effective_user.id]
    upsert_user(update.effective_user.id, update.effective_user.username or "", st.lang, st.tz, False, st.profile)
    await update.effective_chat.send_message({"ru":"Чек-ин отключен.","en":"Check-in disabled.","es":"Check-in desactivado.","uk":"Чек-ін вимкнено."}[st.lang])

async def cmd_remind(update:Update, context:ContextTypes.DEFAULT_TYPE):
    st=STATE[update.effective_user.id]; raw=(update.message.text or "")
    tm=parse_hhmm(raw)
    if not tm:
        await update.effective_chat.send_message({"ru":"Формат: /remind выпить воду at 16:00",
                                                  "en":"Format: /remind drink water at 16:00",
                                                  "es":"Formato: /remind beber agua at 16:00",
                                                  "uk":"Формат: /remind випити воду at 16:00"}[st.lang]); return
    hour,minute=tm
    text=re.sub(r'/remind','',raw,flags=re.I).strip()
    text=re.sub(r'\bat\s*[0-2]?\d:[0-5]\d.*','',text).strip() or {"ru":"Напоминание","en":"Reminder","es":"Recordatorio","uk":"Нагадування"}[st.lang]
    rid=add_reminder(update.effective_user.id, st.tz, text, hour, minute, "DAILY")
    schedule_daily(context, update.effective_chat.id, st.tz, hour, minute, f"rem_{rid}", {"chat_id":update.effective_chat.id,"text":text}, job_reminder)
    await update.effective_chat.send_message({"ru":f"Ок, ежедневно в {hour:02d}:{minute:02d}: «{text}». (id {rid})",
                                              "en":f"Daily at {hour:02d}:{minute:02d}: “{text}”. (id {rid})",
                                              "es":f"Cada día a las {hour:02d}:{minute:02d}: “{text}”. (id {rid})",
                                              "uk":f"Щодня о {hour:02d}:{minute:02d}: «{text}». (id {rid})"}[st.lang])

async def cmd_reminders(update:Update, context:ContextTypes.DEFAULT_TYPE):
    st=STATE[update.effective_user.id]; rows=list_reminders(update.effective_user.id)
    if not rows:
        await update.effective_chat.send_message({"ru":"Активных напоминаний нет.","en":"No active reminders.","es":"No hay recordatorios activos.","uk":"Активних нагадувань немає."}[st.lang]); return
    lines=[f"{r['id']}: {r['text']} — {r['hour']:02d}:{r['minute']:02d} ({r['rrule']})" for r in rows]
    await update.effective_chat.send_message("\n".join(lines))

async def cmd_delremind(update:Update, context:ContextTypes.DEFAULT_TYPE):
    st=STATE[update.effective_user.id]; args=(update.message.text or "").split()
    if len(args)<2 or not args[1].isdigit():
        await update.effective_chat.send_message({"ru":"Формат: /delremind <id>","en":"Usage: /delremind <id>",
                                                  "es":"Uso: /delremind <id>","uk":"Формат: /delremind <id>"}[st.lang]); return
    rid=int(args[1]); ok=disable_reminder(update.effective_user.id, rid)
    for j in context.job_queue.get_jobs_by_name(f"rem_{rid}"): j.schedule_removal()
    await update.effective_chat.send_message({"ru":("Удалил." if ok else "Не найдено."),"en":("Deleted." if ok else "Not found."),
                                              "es":("Eliminado." if ok else "No encontrado."),"uk":("Видалено." if ok else "Не знайдено.")}[st.lang])

# ---------------- CALLBACKS (check-in) ----------------
async def on_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; st=STATE[uid]
    data=update.callback_query.data or ""; await update.callback_query.answer()
    if data in ["checkin:good","checkin:ok","checkin:bad"]:
        add_checkin_row(uid, st.lang, data.split(":")[1], "")
        await update.effective_chat.send_message({"ru":"Спасибо! Хорошего дня 👋","en":"Thanks! Have a great day 👋",
                                                  "es":"¡Gracias! Buen día 👋","uk":"Дякую! Гарного дня 👋"}[st.lang]); return
    if data=="checkin:comment":
        st.awaiting_checkin_comment=True
        await update.effective_chat.send_message({"ru":"Коротко опиши самочувствие:","en":"Write a short note about how you feel:",
                                                  "es":"Escribe una nota breve sobre cómo te sientes:","uk":"Коротко опиши самопочуття:"}[st.lang])

# ---------------- MESSAGE HANDLER ----------------
async def handle_text(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; st=STATE[uid]
    st.lang=st.lang or detect_lang(update,"en")
    text=update.message.text or ""
    st.last_seen=time.time()

    # forced intake on keywords
    if wants_intake(text):
        st.intake_needed=True
        await update.effective_chat.send_message(TXT["intake"][st.lang])
        return

    # check-in comment
    if st.awaiting_checkin_comment:
        st.awaiting_checkin_comment=False
        add_checkin_row(uid, st.lang, "", text)
        await update.effective_chat.send_message({"ru":"Записал. Береги себя.","en":"Noted. Take care.",
                                                  "es":"Anotado. Cuídate.","uk":"Занотував. Бережи себе."}[st.lang])
        return

    # intake first
    if st.intake_needed:
        parsed=parse_intake(text, st.lang)
        # merge shallow
        for k,v in parsed.items():
            if v in [None,"",[]]: continue
            if isinstance(v,dict):
                st.profile.setdefault(k,{}).update({ik:iv for ik,iv in v.items() if iv not in [None,"",[]]})
            else:
                st.profile[k]=v
        st.lang=parsed.get("language", st.lang)
        # check minimal threshold
        if not intake_min_ok(st.profile):
            # ask for missing with example
            ask={
                "ru":"Чтобы я помог по делу, добавь 2–3 пункта: возраст/пол/цель/симптом+длительность. Пример: «м, 34; цель — энергия; симптом — тупая боль в пояснице 2 дня».",
                "en":"To personalize, add 2–3 items: age/sex/goal/symptom+duration. Example: “m, 34; goal—energy; symptom—dull low-back pain 2 days”.",
                "es":"Para personalizar, añade 2–3 datos: edad/sexo/meta/síntoma+duración. Ej.: “h, 34; meta—energía; síntoma—dolor lumbar 2 días”.",
                "uk":"Щоб персоналізувати, додай 2–3 пункти: вік/стать/мета/симптом+тривалість. Приклад: «ч, 34; мета — енергія; симптом — тупий біль у попереку 2 дні»."
            }[st.lang]
            await update.effective_chat.send_message(ask)
            return
        # intake complete
        st.intake_needed=False
        upsert_user(uid, update.effective_user.username or "", st.lang, st.tz, False, st.profile)
        # human summary
        summary=humanize_profile(st.profile, st.lang)
        head={"ru":"Записал основные данные: ","en":"Saved your basics: ","es":"He guardado tus datos básicos: ","uk":"Зберіг основні дані: "}[st.lang]
        await update.effective_chat.send_message(head + summary)
        nextq={"ru":"С чего начнём сейчас? (симптом/сон/питание/анализы/привычки/долголетие)",
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
    data=route_and_answer(text, st.lang, st.profile, list(st.history), fast=st.fast_mode)
    if isinstance(data.get("language"), str): st.lang=data["language"]
    reply=(data.get("assistant_reply") or "").strip()
    if not reply:
        reply={"ru":"Сформулируй, пожалуйста, цель/вопрос одним предложением.",
               "en":"Please state your goal or question in one sentence.",
               "es":"Indica tu objetivo o pregunta en una frase.",
               "uk":"Сформулюй мету або питання одним реченням."}[st.lang]
    st.fast_mode=False; st.history.append({"role":"assistant","content": reply})
    await update.effective_chat.send_message(reply, reply_markup=ReplyKeyboardRemove())
    log_episode(uid, st.lang, text, data.get("intent",""), data.get("slots",{}), reply)

    # follow-ups
    if data.get("needs_more") and data.get("followups"):
        for q in data["followups"][:2]:
            await update.effective_chat.send_message(q)

# ---------------- MAIN ----------------
def main():
    if TELEGRAM_TOKEN=="YOUR_TELEGRAM_TOKEN_HERE": log.warning("Set TELEGRAM_TOKEN")
    if OPENAI_API_KEY=="YOUR_OPENAI_API_KEY_HERE": log.warning("Set OPENAI_API_KEY")
    app=ApplicationBuilder().token(TELEGRAM_TOKEN).build()

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
