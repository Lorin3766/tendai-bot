# Create a new, LLM-powered Telegram bot with a professional health & longevity assistant style.
# - No bottom keyboards.
# - Short 40-second intake in one message; free-text parsing via OpenAI.
# - Professional triage, safety red flags, and concise plans.
# - Multilingual heuristic (ru/en/uk/es) with RU default if Cyrillic detected.
# - Uses python-telegram-bot v20+ and openai python client.
# - Includes /start /reset /profile /plan /privacy commands.
# - Robust fallback if the LLM errors.
# The file is saved to /mnt/data/main_pro.py

code = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TendAI Pro (LLM-powered) — Professional Health & Longevity Assistant
Requirements:
  - python-telegram-bot >= 20
  - openai >= 1.0.0
Environment:
  TELEGRAM_TOKEN=<your token>
  OPENAI_API_KEY=<your key>

Design goals:
- Free, professional answers (no bottom keyboards).
- Fast 40-second intake: one compact questionnaire; user replies in free text.
- Parse intake & later messages to a structured profile via LLM JSON-extraction.
- Triage-first with red-flag detection; concise action steps; disclaimers.
- Multilingual (ru/en/uk/es) heuristic detection; answer in user language.
- Memory: per-user in RAM (intent/profile/history). Replace with DB for prod.
"""

import os
import re
import json
import time
import logging
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict, deque

from telegram import Update, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

from openai import OpenAI

# --------------------- CONFIG ---------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN_HERE")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY_HERE")

SESSION_TIMEOUT_SEC = 30 * 60  # 30 minutes of inactivity -> soft resume
MAX_HISTORY = 12              # keep last N user/bot turns
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # cheap & capable; adjust as needed

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("tendai-pro")

# --------------------- GLOBAL STATE ---------------------
client = OpenAI(api_key=OPENAI_API_KEY)

USER: Dict[int, Dict[str, Any]] = defaultdict(dict)

SAFETY_RED_FLAGS = {
    "ru": [
        "сильная боль в груди", "одышка", "кровь в стуле", "кровь в моче", "слабость одной стороны",
        "паралич", "затруднённая речь", "обморок", "травма головы", "температура выше 39", "температура выше 39.5"
    ],
    "en": [
        "severe chest pain", "shortness of breath", "blood in stool", "blood in urine", "weakness on one side",
        "paralysis", "slurred speech", "fainting", "head injury", "fever over 103", "fever above 103"
    ],
    "es": [
        "dolor fuerte en el pecho", "falta de aire", "sangre en heces", "sangre en orina", "debilidad de un lado",
        "parálisis", "habla arrastrada", "desmayo", "lesión en la cabeza", "fiebre de más de 39"
    ],
    "uk": [
        "сильний біль у грудях", "задишка", "кров у калі", "кров у сечі", "слабкість однієї сторони",
        "параліч", "порушення мови", "непритомність", "травма голови", "температура вище 39"
    ],
}

EMERGENCY_MSG = {
    "ru": "⚠️ Это может быть опасно. Срочно обратитесь в отделение неотложной помощи (ER) или позвоните 911.",
    "en": "⚠️ This could be serious. Please go to the ER or call 911 immediately.",
    "es": "⚠️ Podría ser grave. Ve a urgencias o llama al 911 de inmediato.",
    "uk": "⚠️ Це може бути небезпечно. Негайно зверніться до ER або зателефонуйте 911.",
}

PRIVACY_TEXT = {
    "ru": "Я не врач. Я даю общую информацию и не заменяю медицинскую помощь. Не отправляйте личные номера или пароли. Ваши сообщения обрабатываются сервисом OpenAI.",
    "en": "I’m not a doctor. I provide general information and do not replace medical care. Don’t send personal IDs or passwords. Your messages are processed by OpenAI.",
    "es": "No soy médico. Brindo información general y no reemplazo la atención médica. No envíes datos sensibles. Tus mensajes son procesados por OpenAI.",
    "uk": "Я не лікар. Надаю загальну інформацію і не замінюю медичну допомогу. Не надсилайте чутливі дані. Ваші повідомлення обробляє OpenAI.",
}

INTAKE_PROMPT = {
    "ru": (
        "Быстрый опрос (≈40 сек). Ответьте одним сообщением, в свободной форме:\n"
        "1) Возраст и пол.\n"
        "2) Главная цель (похудение, энергия, сон, долголетие и т.п.).\n"
        "3) Основная жалоба/симптом (если есть) и сколько длится.\n"
        "4) Хроника/операции/лекарства.\n"
        "5) Сон: во сколько ложитесь/встаёте.\n"
        "6) Активность: шаги/тренировки.\n"
        "7) Питание: что обычно едите.\n"
        "8) Есть ли «красные флаги» (сильная боль в груди, одышка и т.п.)?\n"
        "После этого дам план из 4–6 пунктов и уточняющие вопросы."
    ),
    "en": (
        "Quick intake (~40s). Reply in one message, free text:\n"
        "1) Age & sex.\n"
        "2) Main goal (weight, energy, sleep, longevity, etc.).\n"
        "3) Main symptom (if any) & how long.\n"
        "4) Conditions/surgeries/meds.\n"
        "5) Sleep: usual bed/wake time.\n"
        "6) Activity: steps/workouts.\n"
        "7) Diet: typical meals.\n"
        "8) Any red flags (severe chest pain, shortness of breath, etc.)?\n"
        "Then I’ll give a 4–6 step plan and follow-ups."
    ),
    "es": (
        "Intake rápido (~40s). Responde en un mensaje, texto libre:\n"
        "1) Edad y sexo.\n"
        "2) Meta principal (peso, energía, sueño, longevidad, etc.).\n"
        "3) Síntoma principal (si hay) y duración.\n"
        "4) Condiciones/cirugías/medicamentos.\n"
        "5) Sueño: hora de dormir/levantarte.\n"
        "6) Actividad: pasos/entrenos.\n"
        "7) Dieta: comidas típicas.\n"
        "8) ¿Alguna bandera roja (dolor fuerte en pecho, falta de aire, etc.)?\n"
        "Luego daré un plan de 4–6 pasos y preguntas."
    ),
    "uk": (
        "Швидкий опитувальник (~40с). Відповідайте одним повідомленням, довільно:\n"
        "1) Вік і стать.\n"
        "2) Головна мета (вага, енергія, сон, довголіття тощо).\n"
        "3) Основний симптом (якщо є) і скільки триває.\n"
        "4) Хроніка/операції/ліки.\n"
        "5) Сон: коли лягаєте/прокидаєтесь.\n"
        "6) Активність: кроки/тренування.\n"
        "7) Харчування: що зазвичай їсте.\n"
        "8) Чи є “червоні прапорці” (сильний біль у грудях, задишка тощо)?\n"
        "Потім надам план з 4–6 кроків і уточнення."
    ),
}

def detect_lang(text: str) -> str:
    t = (text or "").lower()
    if any(ch in t for ch in "іїєґ") or "привіт" in t:
        return "uk"
    if any(ch in t for ch in "ыэёъ") or "здравствуйте" in t or "привет" in t:
        return "ru"
    if any(w in t for w in ["hola", "gracias", "usted", "salud", "dolor", "¿", "¡", "ñ"]):
        return "es"
    return "en"

def now_ts() -> float:
    return time.time()

def ensure_user(uid: int) -> Dict[str, Any]:
    st = USER[uid]
    st.setdefault("lang", None)
    st.setdefault("profile", {})     # structured intake
    st.setdefault("history", deque(maxlen=MAX_HISTORY))  # list of dicts {role, content}
    st.setdefault("last_seen", now_ts())
    st.setdefault("intake_needed", True)
    return st

def update_seen(st: Dict[str, Any]):
    st["last_seen"] = now_ts()

def timed_out(st: Dict[str, Any]) -> bool:
    return now_ts() - st.get("last_seen", 0) > SESSION_TIMEOUT_SEC

def lang_str(st: Dict[str, Any]) -> str:
    return st.get("lang") or "en"

async def send(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    await update.effective_chat.send_message(text, reply_markup=ReplyKeyboardRemove())

# --------------------- LLM HELPERS ---------------------
SYS_PROMPT = """
You are TendAI — a concise, professional health & longevity assistant (not a doctor).
Communicate warmly and clearly. Use the user's language.
Core rules:
- Start by confirming understanding and summarizing key facts.
- Triage first: check red flags; if present, advise ER/911 immediately.
- Provide a 4–6 step, actionable plan (sleep, nutrition, activity, labs, stress, follow-up).
- Keep answers compact: <= 6 lines plus short bullets. Avoid long essays.
- Personalize using stored profile (age/sex/goals/conditions/meds/sleep/activity/diet).
- Never give definitive diagnosis or prescribe Rx; offer options and when to see a clinician.
- When uncertain, propose safe self-care and monitoring windows.
- Tone: calm, non-judgmental, science-informed, realistic.
"""

JSON_EXTRACT_PROMPT = """
Extract a structured user profile from the text. Return ONLY minified JSON with keys:
{"age": int|null, "sex": "male"|"female"|null, "goal": string|null,
 "main_symptom": string|null, "duration": string|null,
 "conditions": string[]|null, "surgeries": string[]|null, "meds": string[]|null,
 "sleep": {"bedtime": string|null, "waketime": string|null}|null,
 "activity": {"steps_per_day": int|null, "workouts": string|null}|null,
 "diet": string|null, "red_flags": bool|null, "language": "ru"|"en"|"es"|"uk"|null}
If info is missing put null. No commentary.
"""

def llm_chat(messages: List[Dict[str, str]], model: str = MODEL, temperature: float = 0.3) -> str:
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=messages,
    )
    return resp.choices[0].message.content.strip()

def llm_extract_profile(text: str, lang: str) -> Dict[str, Any]:
    try:
        content = llm_chat([
            {"role": "system", "content": JSON_EXTRACT_PROMPT},
            {"role": "user", "content": text}
        ], model=os.getenv("OPENAI_MODEL_PARSER", MODEL), temperature=0)
        data = json.loads(content)
        # override language if we already detected
        if lang and data.get("language") is None:
            data["language"] = lang
        return data
    except Exception as e:
        logger.warning(f"Profile extract failed: {e}")
        # Fallback: simple regex-based guesses
        data = {
            "age": None, "sex": None, "goal": None, "main_symptom": None, "duration": None,
            "conditions": None, "surgeries": None, "meds": None,
            "sleep": {"bedtime": None, "waketime": None},
            "activity": {"steps_per_day": None, "workouts": None},
            "diet": None, "red_flags": None, "language": lang or "en"
        }
        m = re.search(r'(\d{2})\s*(?:лет|y|years|años)', text.lower())
        if m:
            data["age"] = int(m.group(1))
        if re.search(r'\b(male|man|муж|hombre)\b', text.lower()):
            data["sex"] = "male"
        if re.search(r'\b(female|woman|жен|mujer)\b', text.lower()):
            data["sex"] = "female"
        return data

def build_llm_messages(st: Dict[str, Any], user_text: str) -> List[Dict[str, str]]:
    lang = lang_str(st)
    profile = st.get("profile") or {}
    # Compose context in the user's language
    profile_brief = json.dumps(profile, ensure_ascii=False)
    system = SYS_PROMPT + f"\nUser language hint: {lang}. Stored profile (JSON): {profile_brief}."
    messages = [{"role": "system", "content": system}]
    # add recent history
    for h in list(st["history"]):
        messages.append(h)
    messages.append({"role": "user", "content": user_text})
    return messages

def safety_hit(text: str, lang: str) -> bool:
    flags = SAFETY_RED_FLAGS.get(lang, [])
    t = text.lower()
    for f in flags:
        if f in t:
            return True
    return False

# --------------------- COMMANDS ---------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = ensure_user(update.effective_user.id)
    first_text = (update.message.text or "").strip()
    st["lang"] = detect_lang(first_text) if first_text else (st.get("lang") or "ru")
    lang = lang_str(st)
    update_seen(st)
    st["intake_needed"] = True if not st.get("profile") else False
    greet = {
        "ru": "Привет, я TendAI. Я помогу кратко и по делу.",
        "en": "Hi, I’m TendAI. I’ll keep it short and useful.",
        "es": "Hola, soy TendAI. Iré al grano y útil.",
        "uk": "Привіт, я TendAI. Коротко і по суті."
    }[lang]
    await send(update, context, greet)
    await send(update, context, INTAKE_PROMPT[lang])
    await send(update, context, PRIVACY_TEXT[lang])

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = ensure_user(update.effective_user.id)
    USER[update.effective_user.id] = {}  # wipe
    ensure_user(update.effective_user.id)
    await send(update, context, "Reset. /start")

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = ensure_user(update.effective_user.id)
    await send(update, context, PRIVACY_TEXT[lang_str(st)])

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = ensure_user(update.effective_user.id)
    lang = lang_str(st)
    prof = st.get("profile") or {}
    if not prof:
        await send(update, context, {"ru":"Профиль пуст. Ответьте на опрос.",
                                     "en":"Profile is empty. Please answer the intake.",
                                     "es":"Perfil vacío. Responde el intake.",
                                     "uk":"Профіль порожній. Дайте відповіді на опитування."}[lang])
        return
    await send(update, context, "Profile:\n" + json.dumps(prof, ensure_ascii=False, indent=2))

async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = ensure_user(update.effective_user.id)
    lang = lang_str(st)
    if not st.get("profile"):
        await send(update, context, {"ru":"Сначала заполните короткий опрос.",
                                     "en":"Please complete the quick intake first.",
                                     "es":"Completa primero el intake.",
                                     "uk":"Спочатку заповніть коротке опитування."}[lang])
        return
    # Ask LLM to produce a short plan using profile only
    messages = [{"role":"system","content":SYS_PROMPT + "\nCreate a short plan based on stored profile only."},
                {"role":"user","content":"Generate a concise 4–6 step plan now."}]
    try:
        reply = llm_chat(messages, temperature=0.3)
    except Exception as e:
        reply = {"ru":"Не удалось сгенерировать план. Попробуйте ещё раз.",
                 "en":"Couldn’t generate a plan now. Try again.",
                 "es":"No se pudo generar el plan. Inténtalo de nuevo.",
                 "uk":"Не вдалося створити план. Спробуйте ще."}[lang]
    await send(update, context, reply)

# --------------------- MESSAGE HANDLER ---------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""
    st = ensure_user(user_id)

    # Language detection & timeout resume
    st["lang"] = st.get("lang") or detect_lang(text)
    lang = lang_str(st)
    if timed_out(st):
        st["history"].clear()
        await send(update, context, {
            "ru":"Продолжим с того места? Кратко напомните цель/симптом.",
            "en":"Shall we resume? Briefly remind your goal/symptom.",
            "es":"¿Seguimos? Resume tu objetivo/síntoma.",
            "uk":"Продовжимо? Нагадайте коротко мету/симптом."
        }[lang])
    update_seen(st)

    # Red flags check (hard stop)
    if safety_hit(text, lang):
        await send(update, context, EMERGENCY_MSG[lang])
        return

    # If intake is needed -> try to parse
    if st.get("intake_needed"):
        prof = llm_extract_profile(text, lang)
        # if no age/goal at all, ask to try again
        minimal_ok = prof.get("age") or prof.get("goal") or prof.get("main_symptom")
        st["profile"] = prof if prof else {}
        st["intake_needed"] = False if minimal_ok else True
        if st["intake_needed"]:
            await send(update, context, {
                "ru":"Чуть подробнее, пожалуйста (возраст/пол/цель/симптом/длительность).",
                "en":"Please add age/sex/goal/symptom/duration details.",
                "es":"Agrega edad/sexo/meta/síntoma/duración, por favor.",
                "uk":"Додайте вік/стать/мету/симптом/тривалість."
            }[lang])
            return
        # Confirm summary to user
        summary = json.dumps(prof, ensure_ascii=False, indent=2)
        confirm = {
            "ru":"Принял. Краткое резюме профиля:\n",
            "en":"Got it. Brief profile summary:\n",
            "es":"Entendido. Resumen del perfil:\n",
            "uk":"Прийнято. Короткий підсумок профілю:\n",
        }[lang] + summary
        await send(update, context, confirm)
        # small next prompt
        next_q = {
            "ru":"С чем начнём прямо сейчас? (симптом/сон/питание/активность/анализы)",
            "en":"Where do you want to start right now? (symptom/sleep/nutrition/activity/labs)",
            "es":"¿Por dónde empezamos ahora? (síntoma/sueño/nutrición/actividad/análisis)",
            "uk":"З чого почнемо зараз? (симптом/сон/харчування/активність/аналізи)",
        }[lang]
        await send(update, context, next_q)
        return

    # Normal dialog: build context and call LLM
    try:
        st["history"].append({"role":"user","content": text})
        messages = build_llm_messages(st, text)
        reply = llm_chat(messages, temperature=0.4)
        st["history"].append({"role":"assistant","content": reply})
        await send(update, context, reply)
    except Exception as e:
        logger.warning(f"LLM call failed: {e}")
        # Fallback rule-based minimal reply
        fallback = {
            "ru":"Короткий совет: начните со сна (фиксированное пробуждение, свет утром), шаги 7–10 тыс., овощи каждый приём, белок 1.2–1.6 г/кг/день. Если хотите — напишите главную цель, а я составлю план из 5 шагов.",
            "en":"Quick tip: anchor sleep (fixed wake, morning light), 7–10k steps, veggies each meal, protein 1.2–1.6 g/kg/day. Tell me your main goal and I’ll draft a 5-step plan.",
            "es":"Consejo rápido: ancla el sueño (despertar fijo, luz matutina), 7–10k pasos, verduras en cada comida, proteína 1.2–1.6 g/kg/día. Dime tu objetivo y haré un plan de 5 pasos.",
            "uk":"Швидка порада: зафіксуйте пробудження, ранкове світло, 7–10 тис. кроків, овочі у кожен прийом, білок 1.2–1.6 г/кг/день. Напишіть головну мету — складу план з 5 кроків.",
        }[lang]
        await send(update, context, fallback)

# --------------------- MAIN ---------------------
def main():
    if TELEGRAM_TOKEN == "YOUR_TELEGRAM_TOKEN_HERE":
        logger.warning("Set TELEGRAM_TOKEN env var.")
    if OPENAI_API_KEY == "YOUR_OPENAI_API_KEY_HERE":
        logger.warning("Set OPENAI_API_KEY env var.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("plan", cmd_plan))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("TendAI Pro bot is running (LLM-powered).")
    app.run_polling()

if __name__ == "__main__":
    main()
'''

path = "/mnt/data/main_pro.py"
with open(path, "w", encoding="utf-8") as f:
    f.write(code)

print(f"Saved to {path}")
