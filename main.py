#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TendAI — LLM-powered Health & Longevity Assistant (RU/EN/ES/UA)

Функции:
- Авто-язык (telegram language_code + первые слова), хранится в user_state['lang'].
- Приветствие + предложение короткого опросника (6–8 вопросов) — инлайн-кнопки (без reply-клавиатур).
- Интенты и слоты через LLM (symptom / nutrition / sleep / labs / habits / other), red-flags → ER/911.
- Короткие профессиональные ответы (<=6 строк + пункты), экономия токенов. /fast — режим «Здоровье за 60 секунд».
- Ежедневные мини-чекапы в 08:30 локального времени пользователя (если согласился), сохранение в Sheets.
- Сбор отзыва после завершения диалога (👍/👎/✍️).
- Команды: /start /pause /resume /delete_data /privacy /profile /plan /fast.
- Логи взаимодействий в лист Episodes; профиль в Users; чекапы в Checkins.

Важное:
- Код без reply-клавиатур (только инлайн-кнопки).
- Память состояния — в RAM (для продакшна вынести в Redis/DB).
"""

import os
import re
import json
import logging
import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict, deque
from zoneinfo import ZoneInfo

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters
)

# ---- OpenAI (LLM) ----
from openai import OpenAI

# ---- Google Sheets ----
import gspread
from google.oauth2.service_account import Credentials

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN_HERE")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY_HERE")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
GSHEET_ID = os.getenv("GSHEET_ID", "")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "America/New_York")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("tendai-llm")

client = OpenAI(api_key=OPENAI_API_KEY)

# --------------- SHEETS -----------------
def _load_sa():
    cred_src = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
    if not cred_src:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")
    if cred_src.strip().startswith("{"):
        data = json.loads(cred_src)
        creds = Credentials.from_service_account_info(
            data, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
    else:
        creds = Credentials.from_service_account_file(
            cred_src, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
    return creds

def open_sheets():
    try:
        gc = gspread.authorize(_load_sa())
        sh = gc.open_by_key(GSHEET_ID)
        def get_or_create(ws_name, headers):
            try:
                ws = sh.worksheet(ws_name)
            except gspread.WorksheetNotFound:
                ws = sh.add_worksheet(ws_name, rows=1000, cols=20)
                ws.append_row(headers)
            return ws
        users_ws = get_or_create("Users", [
            "ts_iso","user_id","username","lang","tz","checkin_enabled",
            "sex","age","chronic","surgeries","smoking","supplements","allergies","main_concern"
        ])
        episodes_ws = get_or_create("Episodes", [
            "ts_iso","user_id","lang","user_text","intent","slots_json","assistant_reply","feedback","comment"
        ])
        checkins_ws = get_or_create("Checkins", [
            "ts_iso","user_id","lang","mood","comment"
        ])
        return users_ws, episodes_ws, checkins_ws
    except Exception as e:
        log.warning(f"Sheets open failed: {e}")
        return None, None, None

USERS_WS, EPISODES_WS, CHECKINS_WS = open_sheets()

def append_users_row(row: List[Any]):
    try:
        if USERS_WS: USERS_WS.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        log.warning(f"append_users_row failed: {e}")

def append_episode(row: List[Any]):
    try:
        if EPISODES_WS: EPISODES_WS.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        log.warning(f"append_episode failed: {e}")

def append_checkin(row: List[Any]):
    try:
        if CHECKINS_WS: CHECKINS_WS.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        log.warning(f"append_checkin failed: {e}")

def delete_user_everywhere(user_id: int):
    """Удаление строк по user_id в Users/Episodes/Checkins."""
    try:
        for ws in [USERS_WS, EPISODES_WS, CHECKINS_WS]:
            if not ws: continue
            records = ws.get_all_records()
            rows_to_delete = [i+2 for i,rec in enumerate(records) if str(rec.get("user_id")) == str(user_id)]
            # удаляем снизу вверх, чтобы индексы не сдвигались
            for r in reversed(rows_to_delete):
                ws.delete_rows(r)
    except Exception as e:
        log.warning(f"delete_user_everywhere failed: {e}")

# --------------- TRANSLATIONS ----------------
translations = {
    "greeting": {
        "ru": "Привет, я TendAI — ассистент по здоровью и долголетию. Я отвечаю коротко и по делу.",
        "en": "Hi, I’m TendAI — your health & longevity assistant. I keep answers short and useful.",
        "es": "Hola, soy TendAI — tu asistente de salud y longevidad. Respondo breve y útil.",
        "uk": "Привіт, я TendAI — асистент зі здоров’я та довголіття. Відповідаю коротко і по суті."
    },
    "ask_intake": {
        "ru": "Хочешь пройти короткий опрос (6–8 вопросов), чтобы я лучше тебя понял?",
        "en": "Would you like a short intake (6–8 quick questions) so I can personalize better?",
        "es": "¿Quieres un intake corto (6–8 preguntas) para personalizar mejor?",
        "uk": "Бажаєш коротке опитування (6–8 питань), щоб я краще тебе зрозумів?"
    },
    "btn_yes": {"ru":"✅ Да","en":"✅ Yes","es":"✅ Sí","uk":"✅ Так"},
    "btn_no":  {"ru":"❌ Нет","en":"❌ No","es":"❌ No","uk":"❌ Ні"},
    "privacy": {
        "ru": "Я не врач. Даю общую информацию и не заменяю медицинскую помощь. Не отправляй чувствительные данные. Сообщения обрабатываются OpenAI.",
        "en": "I’m not a doctor. I provide general info and don’t replace medical care. Don’t send sensitive data. Messages are processed by OpenAI.",
        "es": "No soy médico. Brindo información general y no reemplazo la atención médica. No envíes datos sensibles. Los mensajes son procesados por OpenAI.",
        "uk": "Я не лікар. Надаю загальну інформацію і не замінюю медичну допомогу. Не надсилай чутливі дані. Повідомлення обробляє OpenAI."
    },
    "intake_q": {  # порядок вопросов
        "ru": [
            "Пол (м/ж)?",
            "Возраст?",
            "Есть ли хронические болезни?",
            "Были ли операции?",
            "Куришь ли?",
            "Принимаешь ли добавки?",
            "Есть аллергии?",
            "Что сейчас больше всего волнует?"
        ],
        "en": [
            "Sex (m/f)?",
            "Age?",
            "Any chronic conditions?",
            "Any surgeries?",
            "Do you smoke?",
            "Do you take supplements?",
            "Any allergies?",
            "What worries you most right now?"
        ],
        "es": [
            "Sexo (m/f)?",
            "Edad?",
            "¿Condiciones crónicas?",
            "¿Cirugías?",
            "¿Fumas?",
            "¿Tomas suplementos?",
            "¿Alergias?",
            "¿Qué te preocupa más ahora?"
        ],
        "uk": [
            "Стать (ч/ж)?",
            "Вік?",
            "Хронічні захворювання?",
            "Операції?",
            "Куриш?",
            "Приймаєш добавки?",
            "Алергії?",
            "Що найбільше турбує зараз?"
        ],
    },
    "want_checkins": {
        "ru": "Включить утренние мини-чекапы в 08:30? (спрошу «как самочувствие»)",
        "en": "Enable morning mini check-ins at 08:30? (I’ll ask how you feel)",
        "es": "¿Activar mini check-ins matutinos a las 08:30? (preguntaré cómo te sientes)",
        "uk": "Увімкнути ранкові міні-чекіни о 08:30? (запитаю як самопочуття)"
    },
    "btn_enable": {"ru":"✅ Включить","en":"✅ Enable","es":"✅ Activar","uk":"✅ Увімкнути"},
    "btn_skip": {"ru":"⏭️ Пропустить","en":"⏭️ Skip","es":"⏭️ Omitir","uk":"⏭️ Пропустити"},
    "checkin_prompt": {
        "ru": "Доброе утро! Как самочувствие сегодня?",
        "en": "Good morning! How do you feel today?",
        "es": "¡Buenos días! ¿Cómo te sientes hoy?",
        "uk": "Доброго ранку! Як самопочуття сьогодні?"
    },
    "moods": {
        "ru": ["😃 Хорошо","😐 Нормально","😣 Плохо"],
        "en": ["😃 Good","😐 Okay","😣 Poor"],
        "es": ["😃 Bien","😐 Normal","😣 Mal"],
        "uk": ["😃 Добре","😐 Нормально","😣 Погано"]
    },
    "feedback_q": {
        "ru":"Был ли я полезен?",
        "en":"Was I helpful?",
        "es":"¿Fui útil?",
        "uk":"Чи був я корисним?"
    },
    "btn_up": {"ru":"👍","en":"👍","es":"👍","uk":"👍"},
    "btn_down": {"ru":"👎","en":"👎","es":"👎","uk":"👎"},
    "btn_comment": {"ru":"✍️ Комментарий","en":"✍️ Comment","es":"✍️ Comentario","uk":"✍️ Коментар"},
    "paused": {
        "ru":"Пауза включена. Я не буду писать первым. /resume чтобы вернуть.",
        "en":"Paused. I won’t message first. Use /resume to enable.",
        "es":"En pausa. No iniciaré mensajes. /resume para activar.",
        "uk":"Пауза. Я не писатиму першим. /resume щоб увімкнути."
    },
    "resumed": {
        "ru":"Возобновил работу.",
        "en":"Resumed.",
        "es":"Reanudado.",
        "uk":"Відновлено."
    },
    "deleted": {
        "ru":"Данные удалены.",
        "en":"Data deleted.",
        "es":"Datos eliminados.",
        "uk":"Дані видалено."
    },
    "fast_hint": {
        "ru":"Режим «Здоровье за 60 секунд». Дай симптом/цель — отвечу сверхкоротко.",
        "en":"Fast mode: give me a symptom/goal — I’ll reply ultra-briefly.",
        "es":"Modo rápido: dime un síntoma/meta y respondo ultra breve.",
        "uk":"Швидкий режим: напиши симптом/мету — відповім дуже коротко."
    }
}

def get_text(key: str, lang: str) -> str:
    table = translations.get(key, {})
    return table.get(lang) or table.get("en") or ""

# --------------- STATE ----------------
@dataclass
class UserState:
    lang: str = "en"
    tz: str = DEFAULT_TZ
    paused: bool = False
    intake_in_progress: bool = False
    intake_index: int = 0
    intake_answers: List[str] = field(default_factory=list)
    checkin_enabled: bool = False
    checkin_job_id: Optional[str] = None
    history: deque = field(default_factory=lambda: deque(maxlen=12))
    last_feedback_prompt: Optional[dt.datetime] = None
    profile: Dict[str, Any] = field(default_factory=dict)
    fast_mode: bool = False  # разовый режим для текущего сообщения

STATE: Dict[int, UserState] = defaultdict(UserState)

# --------------- LANGUAGE DETECTION ---------------
def detect_lang(update: Update, fallback="en") -> str:
    code = (update.effective_user.language_code or "").lower()
    t = (update.message.text or "").lower() if update.message else ""
    if any(x in code for x in ["ru","ru-","ru_"]) or any(w in t for w in ["привет","здравствуйте","самочувствие","боль"]):
        return "ru"
    if any(x in code for x in ["uk","uk-","uk_"]) or any(w in t for w in ["привіт","здоров'я","болить"]):
        return "uk"
    if any(x in code for x in ["es","es-","es_"]) or any(w in t for w in ["hola","salud","dolor","¿","¡","ñ"]):
        return "es"
    if any(w in t for w in ["hello","hi","how are you","pain","sleep","diet"]):
        return "en"
    return fallback

# --------------- LLM PROMPTS ---------------
SYS_CORE = """You are TendAI — a concise, professional health & longevity assistant.
Speak in the user's language. Keep answers compact (<=6 lines + short bullets).
Prioritize safety triage (red flags). Do not diagnose; give safe, actionable steps.
Topics: symptom triage, sleep, nutrition, labs, habits, longevity.
If user invokes 'fast mode', reply in max 4 lines + 3 bullets.
"""

JSON_ROUTER = """
Task: Analyze the user's message and produce a JSON ONLY (no prose).
Return fields:
- language: "ru"|"en"|"es"|"uk"|null
- intent: "symptom"|"nutrition"|"sleep"|"labs"|"habits"|"other"
- slots: for symptom include {where, character, duration, intensity?}; for others include useful keys
- red_flags: boolean
- assistant_reply: string  # compact professional message for the user
- followups: string[]      # up to 2 short clarifying questions (optional)
- needs_more: boolean      # true if followups are needed to proceed
- ask_feedback: boolean    # true if it's a good moment to ask for feedback
Constraints:
- Keep assistant_reply short and practical.
- If red_flags=true, assistant_reply must begin with an ER/911 recommendation.
- Never output anything but minified JSON.
"""

def llm_route_reply(user_lang: str, profile: Dict[str, Any], history: List[Dict[str,str]], user_text: str, fast: bool=False) -> Dict[str, Any]:
    """LLM делает: intent+slots+короткий ответ+followups в JSON."""
    sys = SYS_CORE + f"\nUser language hint: {user_lang}. Fast mode: {str(fast)}.\nStored profile (JSON): {json.dumps(profile, ensure_ascii=False)}"
    messages = [{"role":"system","content":sys},
                {"role":"system","content":JSON_ROUTER},
                {"role":"user","content":user_text}]
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.2,
            max_tokens=400,
            messages=messages
        )
        content = resp.choices[0].message.content.strip()
        # попытка выделить JSON
        m = re.search(r"\{.*\}\s*$", content, re.S)
        if m: content = m.group(0)
        data = json.loads(content)
        return data
    except Exception as e:
        log.warning(f"LLM route failed: {e}")
        # fallback: минимальный ответ
        return {
            "language": user_lang, "intent":"other", "slots": {},
            "red_flags": False,
            "assistant_reply": {
                "ru":"Короткий совет: фиксируйте подъём, утренний свет, 7–10 тыс. шагов, белок 1.2–1.6 г/кг/день. Напишите цель/симптом — уточню план.",
                "en":"Quick tip: fixed wake, morning light, 7–10k steps, protein 1.2–1.6 g/kg/day. Tell me your goal/symptom for a tailored plan.",
                "es":"Consejo: despertar fijo, luz matutina, 7–10k pasos, proteína 1.2–1.6 g/kg/día. Dime tu objetivo/síntoma.",
                "uk":"Порада: фіксоване пробудження, ранкове світло, 7–10 тис. кроків, білок 1.2–1.6 г/кг/день. Напишіть мету/симптом."
            }.get(user_lang,"en"),
            "followups": [], "needs_more": False, "ask_feedback": True
        }

# --------------- UI HELPERS ---------------
def ik_btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)

def send_inline(update: Update, text: str, buttons: List[List[InlineKeyboardButton]]):
    return update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(buttons))

async def send_msg(update: Update, text: str):
    await update.effective_chat.send_message(text, reply_markup=ReplyKeyboardRemove())

# --------------- CHECKIN SCHEDULE ---------------
def schedule_checkin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, tz: str):
    # удаляем старую задачу
    job_name = f"checkin_{chat_id}"
    old = context.job_queue.get_jobs_by_name(job_name)
    for j in old:
        j.schedule_removal()
    # 08:30 локального времени
    t = dt.time(hour=8, minute=30, tzinfo=ZoneInfo(tz))
    context.job_queue.run_daily(callback=job_checkin, time=t, name=job_name, data={"chat_id": chat_id, "lang": lang})

async def job_checkin(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    lang = data.get("lang", "en")
    moods = translations["moods"][lang]
    kb = [[ik_btn(moods[0], "checkin:mood:good"),
           ik_btn(moods[1], "checkin:mood:ok"),
           ik_btn(moods[2], "checkin:mood:bad")],
          [ik_btn(translations["btn_comment"][lang], "checkin:comment")]]
    await context.bot.send_message(chat_id, get_text("checkin_prompt", lang), reply_markup=InlineKeyboardMarkup(kb))

# --------------- COMMANDS ---------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = STATE[uid]
    st.lang = detect_lang(update, st.lang)
    st.tz = st.tz or DEFAULT_TZ
    st.paused = False
    st.fast_mode = False

    await send_msg(update, get_text("greeting", st.lang))
    await send_msg(update, get_text("privacy", st.lang))

    kb = [[ik_btn(get_text("btn_yes", st.lang), "intake:yes"),
           ik_btn(get_text("btn_no", st.lang), "intake:no")]]
    await send_inline(update, get_text("ask_intake", st.lang), kb)

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = STATE[uid].lang or detect_lang(update, "en")
    await send_msg(update, get_text("privacy", lang))

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]
    st.paused = True
    await send_msg(update, get_text("paused", st.lang))

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]
    st.paused = False
    await send_msg(update, get_text("resumed", st.lang))

async def cmd_delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    delete_user_everywhere(uid)
    STATE[uid] = UserState(lang=detect_lang(update, "en"))
    await send_msg(update, get_text("deleted", STATE[uid].lang))

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]
    await send_msg(update, "Profile:\n" + json.dumps(st.profile or {}, ensure_ascii=False, indent=2))

async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]
    # попросим LLM составить мини-план только из профиля
    sys = SYS_CORE + "\nCreate a compact 4–6 step plan based ONLY on stored profile."
    msgs = [{"role":"system","content":sys},
            {"role":"user","content":"Generate the plan now."}]
    try:
        resp = client.chat.completions.create(model=OPENAI_MODEL, temperature=0.2, max_tokens=350, messages=msgs)
        await send_msg(update, resp.choices[0].message.content.strip())
    except Exception as e:
        await send_msg(update, {
            "ru":"Не удалось сгенерировать план сейчас.",
            "en":"Couldn’t generate a plan now.",
            "es":"No se pudo generar un plan ahora.",
            "uk":"Не вдалося створити план зараз."
        }.get(st.lang,"en"))

async def cmd_fast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = STATE[update.effective_user.id]
    st.fast_mode = True
    await send_msg(update, get_text("fast_hint", st.lang))

# --------------- INTAKE FLOW (INLINE) ---------------
def intake_questions(lang: str) -> List[str]:
    return translations["intake_q"][lang]

def intake_key_for_index(idx: int) -> str:
    keys = ["sex","age","chronic","surgeries","smoking","supplements","allergies","main_concern"]
    return keys[idx] if 0 <= idx < len(keys) else f"q{idx}"

async def start_intake(update: Update, st: UserState):
    st.intake_in_progress = True
    st.intake_index = 0
    st.intake_answers = []
    await send_msg(update, intake_questions(st.lang)[0])

async def proceed_intake_answer(update: Update, st: UserState, text: str):
    qs = intake_questions(st.lang)
    st.intake_answers.append(text.strip())
    st.intake_index += 1
    if st.intake_index < len(qs):
        await send_msg(update, qs[st.intake_index])
        return
    # intake завершен
    st.intake_in_progress = False
    # собираем профиль
    prof = {intake_key_for_index(i): st.intake_answers[i] for i in range(len(st.intake_answers))}
    st.profile.update(prof)
    # в Sheets -> Users
    append_users_row([
        dt.datetime.utcnow().isoformat(), update.effective_user.id,
        (update.effective_user.username or ""), st.lang, st.tz, str(st.checkin_enabled),
        prof.get("sex",""), prof.get("age",""), prof.get("chronic",""), prof.get("surgeries",""),
        prof.get("smoking",""), prof.get("supplements",""), prof.get("allergies",""), prof.get("main_concern","")
    ])
    # предложить включить чекапы
    kb = [[ik_btn(get_text("btn_enable", st.lang), "checkin:enable"),
           ik_btn(get_text("btn_skip", st.lang), "checkin:skip")]]
    await send_inline(update, get_text("want_checkins", st.lang), kb)

# --------------- CALLBACKS ---------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = STATE[uid]
    data = update.callback_query.data or ""
    await update.callback_query.answer()

    if data == "intake:yes":
        await start_intake(update, st)
        return
    if data == "intake:no":
        await send_msg(update, {"ru":"Ок, можно начать с любого вопроса.",
                                "en":"Okay, ask me anything to start.",
                                "es":"Vale, empecemos por lo que quieras.",
                                "uk":"Гаразд, можемо почати з будь-чого."}[st.lang])
        return

    if data == "checkin:enable":
        st.checkin_enabled = True
        schedule_checkin(context, update.effective_chat.id, st.lang, st.tz)
        await send_msg(update, {"ru":"Утренние чекапы включены на 08:30.",
                                "en":"Morning check-ins enabled at 08:30.",
                                "es":"Check-ins matutinos activados a las 08:30.",
                                "uk":"Ранкові чекіни увімкнено на 08:30."}[st.lang])
        return
    if data == "checkin:skip":
        st.checkin_enabled = False
        await send_msg(update, {"ru":"Хорошо, без чекапов.",
                                "en":"Alright, no check-ins.",
                                "es":"De acuerdo, sin check-ins.",
                                "uk":"Добре, без чекінів."}[st.lang])
        return

    if data.startswith("checkin:mood:"):
        mood = data.split(":")[-1]
        mood_map = {"good":"good","ok":"ok","bad":"bad"}
        append_checkin([dt.datetime.utcnow().isoformat(), uid, st.lang, mood_map.get(mood,""), ""])
        await send_msg(update, {"ru":"Спасибо! Хорошего дня 👋",
                                "en":"Thanks! Have a great day 👋",
                                "es":"¡Gracias! Buen día 👋",
                                "uk":"Дякую! Гарного дня 👋"}[st.lang])
        return

    if data == "checkin:comment":
        st.history.append({"role":"assistant","content":"__awaiting_checkin_comment__"})
        await send_msg(update, {"ru":"Напиши короткий комментарий о самочувствии.",
                                "en":"Write a short comment about how you feel.",
                                "es":"Escribe un breve comentario sobre cómo te sientes.",
                                "uk":"Напиши короткий коментар про самопочуття."}[st.lang])
        return

    if data == "fb:up":
        append_episode([dt.datetime.utcnow().isoformat(), uid, st.lang, "", "", "", "", "up", ""])
        await send_msg(update, {"ru":"Спасибо за оценку!","en":"Thank you!","es":"¡Gracias!","uk":"Дякую!"}[st.lang])
        return
    if data == "fb:down":
        append_episode([dt.datetime.utcnow().isoformat(), uid, st.lang, "", "", "", "", "down", ""])
        await send_msg(update, {"ru":"Принял. Постараюсь быть полезнее.","en":"Got it. I’ll do better.","es":"Entendido. Mejoraré.","uk":"Зрозуміло. Постараюся краще."}[st.lang])
        return
    if data == "fb:comment":
        st.history.append({"role":"assistant","content":"__awaiting_feedback_comment__"})
        await send_msg(update, {"ru":"Оставь короткий комментарий 🙏",
                                "en":"Leave a short comment 🙏",
                                "es":"Deja un comentario breve 🙏",
                                "uk":"Залиш короткий коментар 🙏"}[st.lang])
        return

# --------------- MESSAGE HANDLER ---------------
def should_ask_feedback(st: UserState) -> bool:
    if st.last_feedback_prompt is None:
        return True
    return (dt.datetime.utcnow() - st.last_feedback_prompt) > dt.timedelta(minutes=15)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = STATE[uid]
    st.lang = st.lang or detect_lang(update, "en")
    text = update.message.text or ""

    # special flows waiting for comments
    if st.history and st.history[-1].get("content") in ["__awaiting_feedback_comment__", "__awaiting_checkin_comment__"]:
        marker = st.history[-1]["content"]
        st.history.pop()
        if marker == "__awaiting_feedback_comment__":
            append_episode([dt.datetime.utcnow().isoformat(), uid, st.lang, "", "", "", "", "", text])
            await send_msg(update, {"ru":"Спасибо!","en":"Thanks!","es":"¡Gracias!","uk":"Дякую!"}[st.lang])
        else:
            append_checkin([dt.datetime.utcnow().isoformat(), uid, st.lang, "", text])
            await send_msg(update, {"ru":"Записал. Берегите себя.","en":"Noted. Take care.","es":"Anotado. Cuídate.","uk":"Занотував. Бережіть себе."}[st.lang])
        return

    # intake in progress?
    if st.intake_in_progress:
        await proceed_intake_answer(update, st, text)
        return

    # paused?
    if st.paused:
        await send_msg(update, get_text("paused", st.lang))
        return

    # fast mode one-shot?
    fast = False
    if st.fast_mode or any(w in text.lower() for w in ["быстро","60 сек","60 секунд","fast"]):
        fast = True
        st.fast_mode = False

    # LLM routing + reply
    st.history.append({"role":"user","content": text})
    data = llm_route_reply(st.lang, st.profile, list(st.history), text, fast=fast)

    # update lang if LLM guessed better
    if isinstance(data.get("language"), str):
        st.lang = data["language"]

    reply = (data.get("assistant_reply") or "").strip()
    if not reply:
        reply = {"ru":"Сформулируй, пожалуйста, цель/вопрос одним предложением.",
                 "en":"Please state your goal or question in one sentence.",
                 "es":"Por favor, di tu objetivo o pregunta en una frase.",
                 "uk":"Будь ласка, сформулюй мету або питання одним реченням."}[st.lang]

    await send_msg(update, reply)

    # log in Episodes
    append_episode([
        dt.datetime.utcnow().isoformat(), uid, st.lang, text,
        data.get("intent",""), json.dumps(data.get("slots",{}), ensure_ascii=False),
        reply, "", ""
    ])

    # ask followups if needed
    if data.get("needs_more") and data.get("followups"):
        for q in data["followups"][:2]:
            await send_msg(update, q)

    # feedback prompt (if it's a good moment and not intake/checkin)
    if data.get("ask_feedback") and should_ask_feedback(st):
        st.last_feedback_prompt = dt.datetime.utcnow()
        kb = [[ik_btn(get_text("btn_up", st.lang), "fb:up"),
               ik_btn(get_text("btn_down", st.lang), "fb:down"),
               ik_btn(get_text("btn_comment", st.lang), "fb:comment")]]
        await send_inline(update, get_text("feedback_q", st.lang), kb)

# --------------- MAIN ----------------
def main():
    if TELEGRAM_TOKEN == "YOUR_TELEGRAM_TOKEN_HERE":
        log.warning("Please set TELEGRAM_TOKEN")
    if OPENAI_API_KEY == "YOUR_OPENAI_API_KEY_HERE":
        log.warning("Please set OPENAI_API_KEY")
    if not GSHEET_ID:
        log.warning("GSHEET_ID is empty — data logging to Sheets will be skipped if not set properly.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("delete_data", cmd_delete_data))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("fast", cmd_fast))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("TendAI LLM bot starting…")
    app.run_polling()

if __name__ == "__main__":
    main()
