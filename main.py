#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TendAI — LLM-powered Health & Longevity Assistant (RU/EN/ES/UK)
Гид-опросник (8 шагов с вариантами + free-text), LLM-диалог с мягким триажем,
SQLite-память, чек-ин 60с и напоминания. Жёсткая фиксация языка.
"""

import os, re, json, time, sqlite3, logging, datetime as dt
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict, deque
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
)

# OpenAI 1.x
from openai import OpenAI

# -------------------- CONFIG --------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN_HERE")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY_HERE")
OPENAI_MODEL  = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_TZ    = os.getenv("DEFAULT_TZ", "Europe/Kyiv")
DB_PATH       = os.getenv("TEND_AI_DB", "tendai.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("tendai")

client = OpenAI(api_key=OPENAI_API_KEY)

# -------------------- DB ------------------------
def db()->sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    c=db().cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
      user_id INTEGER PRIMARY KEY, username TEXT, lang TEXT, tz TEXT,
      checkin_enabled INTEGER DEFAULT 0, profile_json TEXT, created_at TEXT, updated_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS episodes(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER, lang TEXT,
      user_text TEXT, intent TEXT, slots_json TEXT, assistant_reply TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS checkins(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER, lang TEXT, mood TEXT, comment TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS reminders(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, tz TEXT, text TEXT,
      hour INTEGER, minute INTEGER, rrule TEXT, enabled INTEGER DEFAULT 1)""")
    c.connection.commit(); c.connection.close()
init_db()

def upsert_user(user_id:int, username:str, lang:str, tz:str, checkin_enabled:bool, profile:Dict[str,Any]):
    now=dt.datetime.utcnow().isoformat()
    conn=db(); cur=conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if cur.fetchone():
        cur.execute("""UPDATE users SET username=?,lang=?,tz=?,checkin_enabled=?,profile_json=?,updated_at=? WHERE user_id=?""",
                    (username, lang, tz, int(checkin_enabled), json.dumps(profile, ensure_ascii=False), now, user_id))
    else:
        cur.execute("""INSERT INTO users(user_id,username,lang,tz,checkin_enabled,profile_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (user_id, username, lang, tz, int(checkin_enabled), json.dumps(profile, ensure_ascii=False), now, now))
    conn.commit(); conn.close()

def load_user_row(user_id:int)->Optional[sqlite3.Row]:
    conn=db(); cur=conn.cursor(); cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    r=cur.fetchone(); conn.close(); return r

def log_episode(user_id:int, lang:str, user_text:str, intent:str, slots:Dict[str,Any], reply:str):
    conn=db(); cur=conn.cursor()
    cur.execute("INSERT INTO episodes(ts,user_id,lang,user_text,intent,slots_json,assistant_reply) VALUES(?,?,?,?,?,?,?)",
                (dt.datetime.utcnow().isoformat(), user_id, lang, user_text, intent, json.dumps(slots, ensure_ascii=False), reply))
    conn.commit(); conn.close()

def add_checkin_row(user_id:int, lang:str, mood:str, comment:str):
    conn=db(); cur=conn.cursor()
    cur.execute("INSERT INTO checkins(ts,user_id,lang,mood,comment) VALUES(?,?,?,?,?)",
                (dt.datetime.utcnow().isoformat(), user_id, lang, mood, comment))
    conn.commit(); conn.close()

def add_reminder(user_id:int, tz:str, text:str, hour:int, minute:int, rrule:str="DAILY")->int:
    conn=db(); cur=conn.cursor()
    cur.execute("INSERT INTO reminders(user_id,tz,text,hour,minute,rrule,enabled) VALUES(?,?,?,?,?,?,1)",
                (user_id, tz, text, hour, minute, rrule))
    rid=cur.lastrowid; conn.commit(); conn.close(); return rid

def list_reminders(user_id:int)->List[sqlite3.Row]:
    conn=db(); cur=conn.cursor(); cur.execute("SELECT * FROM reminders WHERE user_id=? AND enabled=1 ORDER BY id",(user_id,))
    rows=cur.fetchall(); conn.close(); return rows

def disable_reminder(user_id:int, rid:int)->bool:
    conn=db(); cur=conn.cursor(); cur.execute("UPDATE reminders SET enabled=0 WHERE id=? AND user_id=?",(rid,user_id))
    conn.commit(); ok=cur.rowcount>0; conn.close(); return ok

# -------------------- STATE ---------------------
@dataclass
class UserState:
    lang: str = "en"
    tz: str = DEFAULT_TZ
    paused: bool = False
    profile: Dict[str,Any] = field(default_factory=dict)
    history: deque = field(default_factory=lambda: deque(maxlen=14))
    fast_mode: bool = False

    # Guided intake
    intake_active: bool = False
    intake_step: int = 0
    intake_wait_key: Optional[str] = None   # ждём свободный ввод для ключа

STATE: Dict[int, UserState] = defaultdict(UserState)

# -------------------- TEXTS ---------------------
def tget(mapper:Dict[str,Dict[str,str]], key:str, lang:str)->str:
    return mapper.get(key,{}).get(lang) or mapper.get(key,{}).get("en","")

TXT = {
    "greet":{
        "ru":"Привет, я TendAI — ассистент по здоровью и долголетию. Отвечаю кратко и по делу.",
        "en":"Hi, I’m TendAI — your health & longevity assistant. I’ll keep it short and useful.",
        "es":"Hola, soy TendAI — tu asistente de salud y longevidad. Iré al grano.",
        "uk":"Привіт, я TendAI — асистент зі здоров’я та довголіття. Коротко і по суті."
    },
    "privacy":{
        "ru":"Я не врач. Это не медпомощь. Не отправляй чувствительные данные. Ответы генерирует LLM.",
        "en":"I’m not a doctor. This isn’t medical care. Don’t send sensitive data. Responses are LLM-generated.",
        "es":"No soy médico. Esto no es atención médica. No envíes datos sensibles. Respuestas generadas por LLM.",
        "uk":"Я не лікар. Це не медична допомога. Не надсилай чутливі дані. Відповіді генерує LLM."
    },
    "intake_intro":{
        "ru":"Давай пройдём короткий опрос (~40с). Можно нажимать варианты или написать свой ответ.",
        "en":"Let’s do a quick intake (~40s). Use buttons or type your own answer.",
        "es":"Hagamos un breve intake (~40s). Usa botones o escribe tu respuesta.",
        "uk":"Зробімо коротке опитування (~40с). Користуйся кнопками або напиши свій варіант."
    },
    "btn_write":{"ru":"✍️ Написать","en":"✍️ Write","es":"✍️ Escribir","uk":"✍️ Написати"},
    "btn_skip":{"ru":"⏭️ Пропустить","en":"⏭️ Skip","es":"⏭️ Omitir","uk":"⏭️ Пропустити"},
    "btn_comment":{"ru":"✍️ Комментарий","en":"✍️ Comment","es":"✍️ Comentario","uk":"✍️ Коментар"},
    "checkin_hello":{
        "ru":"Доброе утро! Быстрый чек-ин: как самочувствие?",
        "en":"Good morning! Quick check-in: how do you feel today?",
        "es":"¡Buenos días! Chequeo rápido: ¿cómo te sientes hoy?",
        "uk":"Доброго ранку! Швидкий чек-ін: як самопочуття сьогодні?"
    },
    "after_summary_head":{
        "ru":"Сохранил основные данные: ",
        "en":"Saved your basics: ",
        "es":"He guardado tus datos básicos: ",
        "uk":"Зберіг основні дані: "
    },
    "after_summary_next":{
        "ru":"С чего начнём сейчас? (симптом/сон/питание/анализы/привычки/довголетие)",
        "en":"Where do you want to start now? (symptom/sleep/nutrition/labs/habits/longevity)",
        "es":"¿Por dónde empezamos ahora? (síntoma/sueño/nutrición/análisis/hábitos/longevidad)",
        "uk":"З чого почнемо зараз? (симптом/сон/харчування/аналізи/звички/довголіття)"
    },
    "need_more_for_intake":{
        "ru":"Чтобы персонализировать рекомендации, добавь 2–3 пункта: возраст/пол/цель/симптом+длительность. Пример: «м, 34; цель — энергия; симптом — тупая боль в пояснице 2 дня».",
        "en":"To personalize, add 2–3 items: age/sex/goal/symptom+duration. Example: “m, 34; goal—energy; symptom—dull low-back pain 2 days”.",
        "es":"Para personalizar, añade 2–3 datos: edad/sexo/meta/síntoma+duración. Ej.: “h, 34; meta—energía; síntoma—dolor lumbar 2 días”.",
        "uk":"Щоб персоналізувати, додай 2–3 пункти: вік/стать/мета/симптом+тривалість. Приклад: «ч, 34; мета — енергія; симптом — тупий біль у попереку 2 дні»."
    }
}

# -------------------- LANGUAGE -------------------
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

# -------------------- INTAKE (guided) -----------
# Формат шага: (key, question_by_lang, options_by_lang[list of (label,value)])
INTAKE_STEPS: List[Dict[str,Any]] = [
    {
        "key":"sex",
        "q":{
            "ru":"Шаг 1/8. Укажи пол:",
            "en":"Step 1/8. Your sex:",
            "es":"Paso 1/8. Tu sexo:",
            "uk":"Крок 1/8. Твоя стать:"
        },
        "opts":{
            "ru":[("Мужской","male"),("Женский","female"),("Другое","other")],
            "en":[("Male","male"),("Female","female"),("Other","other")],
            "es":[("Hombre","male"),("Mujer","female"),("Otro","other")],
            "uk":[("Чоловіча","male"),("Жіноча","female"),("Інша","other")],
        }
    },
    {
        "key":"age",
        "q":{
            "ru":"Шаг 2/8. Возраст:",
            "en":"Step 2/8. Age:",
            "es":"Paso 2/8. Edad:",
            "uk":"Крок 2/8. Вік:"
        },
        "opts":{
            "ru":[("18–25","20"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
            "en":[("18–25","20"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
            "es":[("18–25","20"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
            "uk":[("18–25","20"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        }
    },
    {
        "key":"goal",
        "q":{
            "ru":"Шаг 3/8. Главная цель:",
            "en":"Step 3/8. Main goal:",
            "es":"Paso 3/8. Meta principal:",
            "uk":"Крок 3/8. Головна мета:"
        },
        "opts":{
            "ru":[("Похудение","weight"),("Энергия","energy"),("Сон","sleep"),("Долголетие","longevity"),("Сила/фитнес","strength")],
            "en":[("Weight","weight"),("Energy","energy"),("Sleep","sleep"),("Longevity","longevity"),("Strength/Fitness","strength")],
            "es":[("Peso","weight"),("Energía","energy"),("Sueño","sleep"),("Longevidad","longevity"),("Fuerza/Fitness","strength")],
            "uk":[("Вага","weight"),("Енергія","energy"),("Сон","sleep"),("Довголіття","longevity"),("Сила/Фітнес","strength")],
        }
    },
    {
        "key":"conditions",
        "q":{
            "ru":"Шаг 4/8. Хронические болезни (если есть):",
            "en":"Step 4/8. Chronic conditions (if any):",
            "es":"Paso 4/8. Condiciones crónicas (si hay):",
            "uk":"Крок 4/8. Хронічні захворювання (якщо є):"
        },
        "opts":{
            "ru":[("Нет","none"),("Гипертония","hypertension"),("Диабет","diabetes"),("Щитовидка","thyroid"),("Другое","other")],
            "en":[("None","none"),("Hypertension","hypertension"),("Diabetes","diabetes"),("Thyroid","thyroid"),("Other","other")],
            "es":[("Ninguna","none"),("Hipertensión","hypertension"),("Diabetes","diabetes"),("Tiroides","thyroid"),("Otra","other")],
            "uk":[("Немає","none"),("Гіпертонія","hypertension"),("Діабет","diabetes"),("Щитоподібна","thyroid"),("Інше","other")],
        }
    },
    {
        "key":"meds",
        "q":{
            "ru":"Шаг 5/8. Лекарства/добавки/аллергии (если есть):",
            "en":"Step 5/8. Meds/supplements/allergies (if any):",
            "es":"Paso 5/8. Medicamentos/suplementos/alergias (si hay):",
            "uk":"Крок 5/8. Ліки/добавки/алергії (якщо є):"
        },
        "opts":{
            "ru":[("Нет","none"),("Магний","magnesium"),("Витамин D","vitd"),("Аллергии есть","allergies"),("Другое","other")],
            "en":[("None","none"),("Magnesium","magnesium"),("Vitamin D","vitd"),("Allergies","allergies"),("Other","other")],
            "es":[("Ninguno","none"),("Magnesio","magnesium"),("Vitamina D","vitd"),("Alergias","allergies"),("Otro","other")],
            "uk":[("Немає","none"),("Магній","magnesium"),("Вітамін D","vitd"),("Алергії","allergies"),("Інше","other")],
        }
    },
    {
        "key":"sleep",
        "q":{
            "ru":"Шаг 6/8. Сон: когда ложишься/встаёшь? (пример 23:30/07:00)",
            "en":"Step 6/8. Sleep: usual bed/wake? (e.g., 23:30/07:00)",
            "es":"Paso 6/8. Sueño: hora de dormir/levantarte? (ej., 23:30/07:00)",
            "uk":"Крок 6/8. Сон: коли лягаєш/прокидаєшся? (прик., 23:30/07:00)"
        },
        "opts":{
            "ru":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
            "en":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Irregular","irregular")],
            "es":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Irregular","irregular")],
            "uk":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
        }
    },
    {
        "key":"activity",
        "q":{
            "ru":"Шаг 7/8. Активность:",
            "en":"Step 7/8. Activity:",
            "es":"Paso 7/8. Actividad:",
            "uk":"Крок 7/8. Активність:"
        },
        "opts":{
            "ru":[("<5k шагов","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Спорт регулярно","sport")],
            "en":[("<5k steps","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Regular training","sport")],
            "es":[("<5k pasos","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Entreno regular","sport")],
            "uk":[("<5k кроків","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Спорт регулярно","sport")],
        }
    },
    {
        "key":"diet",
        "q":{
            "ru":"Шаг 8/8. Питание чаще всего:",
            "en":"Step 8/8. Diet most of the time:",
            "es":"Paso 8/8. Dieta la mayor parte del tiempo:",
            "uk":"Крок 8/8. Харчування здебільшого:"
        },
        "opts":{
            "ru":[("Сбалансировано","balanced"),("Низкоугл/кето","lowcarb"),("Вегетариан/веган","plant"),("Нерегулярно","irregular")],
            "en":[("Balanced","balanced"),("Low-carb/keto","lowcarb"),("Vegetarian/vegan","plant"),("Irregular","irregular")],
            "es":[("Equilibrada","balanced"),("Baja en carbos/keto","lowcarb"),("Vegetariana/vegana","plant"),("Irregular","irregular")],
            "uk":[("Збалансовано","balanced"),("Маловугл/кето","lowcarb"),("Вегетаріан/веган","plant"),("Нерегулярно","irregular")],
        }
    },
]

WRITE_CMD = {"ru":"✍️ Написать","en":"✍️ Write","es":"✍️ Escribir","uk":"✍️ Написати"}
SKIP_CMD  = {"ru":"⏭️ Пропустить","en":"⏭️ Skip","es":"⏭️ Omitir","uk":"⏭️ Пропустити"}

PRIMARY_KEYS = ["age","sex","goal","main_symptom"]  # для минимума (последний ключ пользователь может сообщить позже)

FORCE_INTAKE_PHRASES = [
    "опрос", "опросник", "анкета", "дай опросник", "где опросник",
    "questionnaire", "survey", "give questionnaire", "start intake"
]

def build_intake_kb(lang:str, key:str, opts:List[Tuple[str,str]])->InlineKeyboardMarkup:
    rows=[]
    row=[]
    for label, value in opts:
        row.append(InlineKeyboardButton(label, callback_data=f"intake:choose:{key}:{value}"))
        if len(row)==3:
            rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([InlineKeyboardButton(WRITE_CMD[lang], callback_data=f"intake:write:{key}"),
                 InlineKeyboardButton(SKIP_CMD[lang],  callback_data=f"intake:skip:{key}")])
    return InlineKeyboardMarkup(rows)

def wants_intake(text:str)->bool:
    t=text.lower()
    return any(p in t for p in FORCE_INTAKE_PHRASES)

def intake_min_ok(profile:Dict[str,Any])->bool:
    cnt=0
    for k in PRIMARY_KEYS:
        v=profile.get(k)
        if v not in [None,"",[],{}]:
            cnt+=1
    return cnt>=2

def humanize_profile(p:Dict[str,Any], lang:str)->str:
    parts=[]
    if p.get("sex"): parts.append({"ru":{"male":"м","female":"ж"}.get(p["sex"],"другое"),
                                   "en":p["sex"],"es":p["sex"],"uk":{"male":"ч","female":"ж"}.get(p["sex"],"інша")}[lang])
    if p.get("age"): parts.append(f"{p['age']}")
    if p.get("goal"): parts.append({"ru":"цель — ","en":"goal — ","es":"meta — ","uk":"мета — "}[lang]+str(p["goal"]))
    if p.get("main_symptom"): parts.append({"ru":"симптом — ","en":"symptom — ","es":"síntoma — ","uk":"симптом — "}[lang]+str(p["main_symptom"]))
    if p.get("conditions") and p["conditions"]!="none": parts.append({"ru":"хроника — ","en":"conditions — ","es":"condiciones — ","uk":"хронічні — "}[lang]+str(p["conditions"]))
    if p.get("meds") and p["meds"]!="none": parts.append({"ru":"лек/добавки — ","en":"meds/supps — ","es":"meds/suplementos — ","uk":"ліки/добавки — "}[lang]+str(p["meds"]))
    if p.get("sleep"): parts.append({"ru":"сон — ","en":"sleep — ","es":"sueño — ","uk":"сон — "}[lang]+str(p["sleep"]))
    if p.get("activity"): parts.append({"ru":"активность — ","en":"activity — ","es":"actividad — ","uk":"активність — "}[lang]+str(p["activity"]))
    if p.get("diet"): parts.append({"ru":"питание — ","en":"diet — ","es":"dieta — ","uk":"харчування — "}[lang]+str(p["diet"]))
    if not parts:
        return {"ru":"пока знаю только язык. Добавь возраст/пол/цель/симптом — и персонализирую.",
                "en":"for now I only know your language. Add age/sex/goal/symptom to personalize.",
                "es":"por ahora solo conozco tu idioma. Añade edad/sexo/meta/síntoma para personalizar.",
                "uk":"поки знаю лише мову. Додай вік/стать/мету/симптом — і персоналізую."}[lang]
    return "; ".join(parts)

# -------------------- LLM PROMPTS ---------------
SYS_ROUTER_BASE = """
You are TendAI — a concise, warm, professional health & longevity assistant (not a doctor).
Always answer strictly in {lang}. Do not switch languages unless explicitly asked.
Never claim lack of access to forms; if user asks for a questionnaire, guide them briefly.
Keep answers compact (<=6 lines + up to 4 bullets). Use stored profile when relevant.
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

def route_and_answer(text:str, lang:str, profile:Dict[str,Any], fast:bool=False)->Dict[str,Any]:
    sys = SYS_ROUTER_BASE.format(lang=lang) + f"\nFast mode: {str(fast)}. Stored profile: {json.dumps(profile, ensure_ascii=False)}"
    out = llm([{"role":"system","content":sys},{"role":"user","content":text}], temperature=0.2, max_tokens=480)
    try:
        m=re.search(r"\{.*\}\s*$", out, re.S); out=m.group(0) if m else out
        data=json.loads(out)
        if data.get("red_flags") and float(data.get("confidence",0))<0.6:
            data["red_flags"]=False; data["needs_more"]=True
            if not data.get("followups"):
                data["followups"]=["Где именно/какой характер/сколько длится?"] if lang=="ru" else ["Where exactly/what character/how long?"]
        return data
    except Exception as e:
        log.warning(f"route parse failed: {e}")
        brief={"ru":"Коротко: опиши где/какая боль и сколько длится. База: фиксированный подъём, 7–10 тыс. шагов, овощи каждый приём, белок 1.2–1.6 г/кг/д.",
               "en":"Briefly: where/what pain and how long. Basics: fixed wake, 7–10k steps, veggies each meal, protein 1.2–1.6 g/kg/d.",
               "es":"Breve: dónde/qué dolor y cuánto dura. Básicos: despertar fijo, 7–10k pasos, verduras en cada comida, proteína 1.2–1.6 g/kg/d.",
               "uk":"Коротко: де/який біль і скільки триває. База: фіксоване пробудження, 7–10 тис. кроків, овочі кожного прийому, білок 1.2–1.6 г/кг/д."}.get(lang,"en")
        return {"language":lang,"intent":"other","slots":{},"severity":"low","red_flags":False,"confidence":0.3,
                "assistant_reply":brief,"followups":[], "needs_more":True}

# -------------------- SCHEDULING -----------------
def schedule_daily(context:ContextTypes.DEFAULT_TYPE, chat_id:int, tz:str, hour:int, minute:int, name:str, data:dict, callback):
    for j in context.job_queue.get_jobs_by_name(name): j.schedule_removal()
    t=dt.time(hour=hour, minute=minute, tzinfo=ZoneInfo(tz))
    context.job_queue.run_daily(callback=callback, time=t, name=name, data=data)

async def job_checkin(context:ContextTypes.DEFAULT_TYPE):
    d=context.job.data or {}; chat_id=d.get("chat_id"); lang=d.get("lang","en")
    kb=[[InlineKeyboardButton({"ru":"😃 Хорошо","en":"😃 Good","es":"😃 Bien","uk":"😃 Добре"}[lang],"checkin:good"),
         InlineKeyboardButton({"ru":"😐 Нормально","en":"😐 Okay","es":"😐 Normal","uk":"😐 Нормально"}[lang],"checkin:ok"),
         InlineKeyboardButton({"ru":"😣 Плохо","en":"😣 Poor","es":"😣 Mal","uk":"😣 Погано"}[lang],"checkin:bad")],
        [InlineKeyboardButton(TXT["btn_comment"][lang], "checkin:comment")]]
    await context.bot.send_message(chat_id, TXT["checkin_hello"][lang], reply_markup=InlineKeyboardMarkup(kb))

async def job_reminder(context:ContextTypes.DEFAULT_TYPE):
    d=context.job.data or {}; chat_id=d.get("chat_id"); text=d.get("text","Reminder")
    await context.bot.send_message(chat_id, text)

# -------------------- HELPERS -------------------
def ik(text:str, data:str)->InlineKeyboardButton: return InlineKeyboardButton(text, callback_data=data)
def parse_hhmm(s:str)->Optional[tuple]:
    m=re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', s)
    return (int(m.group(1)), int(m.group(2))) if m else None

def send_intake_step(update:Update, st:UserState):
    step = INTAKE_STEPS[st.intake_step]
    q = step["q"][st.lang]
    kb = build_intake_kb(st.lang, step["key"], step["opts"][st.lang])
    return update.effective_chat.send_message(q, reply_markup=kb)

# -------------------- COMMANDS ------------------
async def cmd_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    st=STATE[uid]; st.lang=detect_lang(update, st.lang)
    # load persistent
    row=load_user_row(uid)
    if row:
        st.tz=row["tz"] or DEFAULT_TZ
        st.profile=json.loads(row["profile_json"] or "{}")
        st.lang=row["lang"] or st.lang
        if row["checkin_enabled"]:
            schedule_daily(context, update.effective_chat.id, st.tz, 8, 30, f"checkin_{uid}",
                           {"chat_id":update.effective_chat.id,"lang":st.lang}, job_checkin)
    await update.effective_chat.send_message(TXT["greet"][st.lang], reply_markup=ReplyKeyboardRemove())
    await update.effective_chat.send_message(TXT["privacy"][st.lang])
    # Guided intake starts immediately
    st.intake_active=True; st.intake_step=0; st.intake_wait_key=None
    await update.effective_chat.send_message(TXT["intake_intro"][st.lang])
    send_intake_step(update, st)

async def cmd_reset(update:Update, context:ContextTypes.DEFAULT_TYPE):
    STATE[update.effective_user.id]=UserState(lang=detect_lang(update,"en"))
    await update.effective_chat.send_message("Reset. /start", reply_markup=ReplyKeyboardRemove())

async def cmd_privacy(update:Update, context:ContextTypes.DEFAULT_TYPE):
    st=STATE[update.effective_user.id]; await update.effective_chat.send_message(TXT["privacy"][st.lang])

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
    msg={"ru":"Чек-ин 60с. Выбери: Хорошо/Нормально/Плохо — или напиши комментарий. Если есть симптом — где/какой/сколько длится?",
         "en":"60-second check-in. Choose Good/Okay/Poor or add a note. If any symptom — where/what/how long?",
         "es":"Chequeo de 60s. Elige Bien/Normal/Mal o añade nota. Si hay síntoma — dónde/qué/cuánto tiempo?",
         "uk":"Чек-ін 60с. Обери Добре/Нормально/Погано або додай нотатку. Якщо є симптом — де/який/скільки?"}[st.lang]
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
                                                  "es":"Indica zona horaria, p.ej., Europe/Madrid","uk":"Вкажи часовий пояс, напр., Europe/Kyiv"}[st.lang]); return
    tz=args[1]
    try:
        ZoneInfo(tz); st.tz=tz
        row=load_user_row(update.effective_user.id)
        upsert_user(update.effective_user.id, update.effective_user.username or "", st.lang, st.tz,
                    bool(row["checkin_enabled"] if row else False), st.profile)
        await update.effective_chat.send_message({"ru":f"Таймзона: {tz}","en":f"Timezone set: {tz}",
                                                  "es":f"Zona horaria: {tz}","uk":f"Часовий пояс: {tz}"}[st.lang])
    except Exception:
        await update.effective_chat.send_message({"ru":"Неверная таймзона.","en":"Invalid timezone.","es":"Zona horaria inválida.","uk":"Неправильний часовий пояс."}[st.lang])

async def cmd_checkin_on(update:Update, context:ContextTypes.DEFAULT_TYPE):
    st=STATE[update.effective_user.id]
    schedule_daily(context, update.effective_chat.id, st.tz, 8, 30, f"checkin_{update.effective_user.id}",
                   {"chat_id":update.effective_chat.id,"lang":st.lang}, job_checkin)
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
    schedule_daily(context, update.effective_chat.id, st.tz, hour, minute, f"rem_{rid}",
                   {"chat_id":update.effective_chat.id,"text":text}, job_reminder)
    await update.effective_chat.send_message({"ru":f"Ок, ежедневно в {hour:02d}:{minute:02d}: «{text}». (id {rid})",
                                              "en":f"Daily at {hour:02d}:{minute:02d}: “{text}”. (id {rid})",
                                              "es":f"Cada día a las {hour:02d}:{minute:02d}: “{text}”. (id {rid})",
                                              "uk":f"Щодня о {hour:02d}:{minute:02d}: «{text}». (id {rid})"}[st.lang])

async def cmd_reminders(update:Update, context:ContextTypes.DEFAULT_TYPE):
    st=STATE[update.effective_user.id]; rows=list_reminders(update.effective_user.id)
    if not rows:
        await update.effective_chat.send_message({"ru":"Активных напоминаний нет.","en":"No active reminders.",
                                                  "es":"No hay recordatorios activos.","uk":"Активних нагадувань немає."}[st.lang]); return
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

# -------------------- CALLBACKS ------------------
async def on_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; st=STATE[uid]
    data=update.callback_query.data or ""; await update.callback_query.answer()

    # Guided intake actions
    if data.startswith("intake:"):
        _, action, key, *rest = data.split(":")
        if action=="choose":
            value=":".join(rest)
            st.profile[key]=value
            # простая обработка множественных (conditions/meds можно перезаписать строкой/значением)
            await advance_intake(update, st, context)
            return
        if action=="write":
            st.intake_wait_key=key
            await update.effective_chat.send_message({
                "ru":"Напиши ваш вариант коротко:",
                "en":"Type your answer in a few words:",
                "es":"Escribe tu respuesta en pocas palabras:",
                "uk":"Напиши коротко свою відповідь:"}[st.lang])
            return
        if action=="skip":
            st.profile.setdefault(key, "")
            await advance_intake(update, st, context)
            return

    # Check-in quick buttons
    if data in ["checkin:good","checkin:ok","checkin:bad"]:
        add_checkin_row(uid, st.lang, data.split(":")[1], "")
        await update.effective_chat.send_message({"ru":"Спасибо! Хорошего дня 👋","en":"Thanks! Have a great day 👋",
                                                  "es":"¡Gracias! Buen día 👋","uk":"Дякую! Гарного дня 👋"}[st.lang])

async def advance_intake(update:Update, st:UserState, context:ContextTypes.DEFAULT_TYPE):
    st.intake_step += 1
    if st.intake_step < len(INTAKE_STEPS):
        send_intake_step(update, st)
        return
    # Intake finished — minimal threshold?
    if not intake_min_ok(st.profile):
        await update.effective_chat.send_message(TXT["need_more_for_intake"][st.lang])
        # позволяем дописать свободно (без перезапуска шагов)
        st.intake_wait_key="free_fill"
        return
    st.intake_active=False; st.intake_wait_key=None
    # persist
    upsert_user(update.effective_user.id, update.effective_user.username or "", st.lang, st.tz, False, st.profile)
    # human summary
    summary = humanize_profile(st.profile, st.lang)
    await update.effective_chat.send_message(TXT["after_summary_head"][st.lang] + summary)
    await update.effective_chat.send_message(TXT["after_summary_next"][st.lang])

# -------------------- MESSAGE HANDLER -----------
async def handle_text(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    st=STATE[uid]
    st.lang=st.lang or detect_lang(update,"en")
    text=(update.message.text or "").strip()

    # forced intake by keywords
    if wants_intake(text):
        st.intake_active=True; st.intake_step=0; st.intake_wait_key=None
        await update.effective_chat.send_message(TXT["intake_intro"][st.lang])
        send_intake_step(update, st)
        return

    # if waiting free text for a specific key
    if st.intake_wait_key:
        key=st.intake_wait_key
        st.intake_wait_key=None
        # lightweight normalization
        if key=="age":
            m=re.search(r'\d{2}', text); st.profile[key]=int(m.group(0)) if m else text
        else:
            st.profile[key]=text
        if key=="free_fill":
            # дополнили минимум — завершаем intake
            if not intake_min_ok(st.profile):
                await update.effective_chat.send_message(TXT["need_more_for_intake"][st.lang])
                st.intake_wait_key="free_fill"
            else:
                st.intake_active=False
                upsert_user(uid, update.effective_user.username or "", st.lang, st.tz, False, st.profile)
                summary=humanize_profile(st.profile, st.lang)
                await update.effective_chat.send_message(TXT["after_summary_head"][st.lang]+summary)
                await update.effective_chat.send_message(TXT["after_summary_next"][st.lang])
        else:
            # продолжаем шаги
            await advance_intake(update, st, context)
        return

    # during guided intake flow (button path)
    if st.intake_active:
        # если пользователь решил отвечать текстом вместо кнопок для текущего шага
        step = INTAKE_STEPS[st.intake_step]
        # записываем как свободный ответ
        st.profile[step["key"]] = text
        await advance_intake(update, st, context)
        return

    # paused?
    if st.paused:
        await update.effective_chat.send_message({"ru":"Пауза включена. /resume чтобы вернуть.",
                                                  "en":"Paused. Use /resume to enable.",
                                                  "es":"En pausa. /resume para activar.",
                                                  "uk":"Пауза. /resume щоб увімкнути."}[st.lang])
        return

    # LLM dialog
    data = route_and_answer(text, st.lang, st.profile, fast=st.fast_mode)
    if isinstance(data.get("language"), str): st.lang=data["language"]
    reply=(data.get("assistant_reply") or "").strip()
    if not reply:
        reply={"ru":"Сформулируй, пожалуйста, цель/вопрос одним предложением.",
               "en":"Please state your goal or question in one sentence.",
               "es":"Indica tu objetivo o pregunta en una frase.",
               "uk":"Сформулюй мету або питання одним реченням."}[st.lang]
    st.fast_mode=False
    await update.effective_chat.send_message(reply, reply_markup=ReplyKeyboardRemove())
    log_episode(uid, st.lang, text, data.get("intent",""), data.get("slots",{}), reply)
    if data.get("needs_more") and data.get("followups"):
        for q in data["followups"][:2]:
            await update.effective_chat.send_message(q)

# -------------------- MAIN ----------------------
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

    logging.info("TendAI bot running…")
    app.run_polling()

if __name__ == "__main__":
    main()
