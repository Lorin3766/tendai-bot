#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TendAI MVP bot core (rules-first, low-cost)
Implements:
1) Intent router (KW-based) for: assessment, longevity, nutrition, sleep, activity, labs, symptom, other
2) Flexible parsing: extracts numbers from text; gentle 0–10 prompt only when relevant
3) Slots + non-linear answers (symptom: where, character, duration, intensity)
4) Fallback with 1 clarifying Q + 3 quick topics; anti-loop (no >2 same msgs)
5) Dialogue memory (user_state): intent, slots, lang, last 3 user msgs; 30-min timeout with soft resume
6) Auto language detect (ru/en/uk/es), fixed per session; templates per language
7) Short templates (≤3 lines + 1 clarifying Q)
8) Safety red flags → ER/911 advice immediately
9) Logging & telemetry (stdout + CSV at /mnt/data/telemetry.csv)
10) Mini-tests (run with RUN_TESTS=1)
"""
import os
import re
import csv
import time
import logging
from datetime import datetime, timedelta
from collections import deque, defaultdict
from typing import Dict, Any, Tuple, Optional

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
)

# -------------------------- CONFIG --------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN_HERE")
SESSION_TIMEOUT_SEC = 30 * 60  # 30 minutes
MAX_LAST_USER_MSGS = 3
MAX_LAST_BOT_MSGS = 2
TELEMETRY_CSV = os.getenv("TELEMETRY_CSV", "/mnt/data/telemetry.csv")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tendai-bot")

# In-memory user states: user_id -> state dict
USER_STATES: Dict[int, Dict[str, Any]] = defaultdict(dict)

# -------------- HELPERS: LANGUAGE & INTENT ------------------
def detect_lang(text: str) -> str:
    """Heuristic language detection for ru/en/uk/es."""
    t = text.strip().lower()
    if not t:
        return "en"
    uk_chars = set("іїєґ")
    ru_chars = set("ыэёъ")
    es_markers = ["¿", "¡", "ñ", "á", "é", "í", "ó", "ú"]
    if any(ch in t for ch in uk_chars) or "привіт" in t or "будь ласка" in t:
        return "uk"
    if any(ch in t for ch in ru_chars) or "здравствуйте" in t or "привет" in t:
        return "ru"
    if any(ch in t for ch in es_markers) or any(w in t for w in ["hola", "gracias", "salud", "dolor", "días", "semanas", "horas"]):
        return "es"
    # Default guess
    return "en"

INTENTS = ["assessment", "longevity", "nutrition", "sleep", "activity", "labs", "symptom", "other"]

KW = {
    "ru": {
        "assessment": ["оценка", "самочувств", "насколько", "шкала"],
        "longevity": ["долголет", "прожить дольше", "здоровое старение"],
        "nutrition": ["питание", "еда", "рацион", "калори", "белок", "жир", "углевод"],
        "sleep": ["сон", "спать", "засып", "просып", "режим сна"],
        "activity": ["активн", "шаг", "трениров", "спорт", "упражнен"],
        "labs": ["анализ", "лаборатор", "кровь", "биомаркер", "тест"],
        "symptom": ["болит", "боль", "симптом", "тупая", "острая", "жгуч", "пульсир", "голова", "спина", "груд", "живот", "горло", "сустав", "ног", "рук", "плеч"],
    },
    "en": {
        "assessment": ["rate", "rating", "scale", "how are you", "how do you feel"],
        "longevity": ["longevity", "live longer", "healthspan", "anti-aging"],
        "nutrition": ["diet", "nutrition", "calorie", "protein", "fat", "carb", "meal"],
        "sleep": ["sleep", "bedtime", "wake", "insomnia", "nap"],
        "activity": ["steps", "exercise", "workout", "gym", "activity"],
        "labs": ["labs", "bloodwork", "biomarker", "test"],
        "symptom": ["pain", "ache", "hurts", "headache", "back", "chest", "stomach", "throat", "joint", "leg", "arm", "shoulder", "dull", "sharp", "burning", "throbbing"],
    },
    "es": {
        "assessment": ["evaluación", "escala", "cómo te sientes", "califica"],
        "longevity": ["longevidad", "vivir más", "anti envejecimiento", "salud a largo plazo"],
        "nutrition": ["nutrición", "dieta", "calorías", "proteína", "grasa", "carbo"],
        "sleep": ["sueño", "dormir", "hora de dormir", "insomnio", "siesta"],
        "activity": ["pasos", "ejercicio", "entrenamiento", "actividad"],
        "labs": ["análisis", "sangre", "biomarcador", "prueba"],
        "symptom": ["dolor", "me duele", "cabeza", "espalda", "pecho", "estómago", "garganta", "articulación", "pierna", "brazo", "hombro", "sordo", "agudo", "ardor", "pulsátil"],
    },
    "uk": {
        "assessment": ["оцінка", "самопочуття", "наскільки", "шкала"],
        "longevity": ["довголіт", "жити довше", "здорове старіння"],
        "nutrition": ["харчуван", "дієта", "каллор", "білок", "жир", "вуглевод"],
        "sleep": ["сон", "спати", "засин", "прокид", "режим сну"],
        "activity": ["активн", "крок", "тренув", "спорт", "вправ"],
        "labs": ["аналіз", "лаборатор", "кров", "біомаркер", "тест"],
        "symptom": ["болить", "біль", "симптом", "тупий", "гострий", "пекучий", "пульсуючий", "голова", "спина", "груди", "живіт", "горло", "суглоб", "нога", "рука", "плече"],
    },
}

SAFETY_FLAGS = {
    "ru": ["сильная боль в груди", "одышка", "слабость одной стороны", "кровь в стуле", "кровь в моче", "температура", "травма головы", "обморок"],
    "en": ["severe chest pain", "shortness of breath", "weakness on one side", "blood in stool", "blood in urine", "fever", "head injury", "fainting"],
    "es": ["dolor fuerte en el pecho", "falta de aire", "debilidad de un lado", "sangre en heces", "sangre en orina", "fiebre", "lesión en la cabeza", "desmayo"],
    "uk": ["сильний біль у грудях", "задишка", "слабкість однієї сторони", "кров у калі", "кров у сечі", "лихоманка", "травма голови", "непритомн"],
}

EMERGENCY_LINES = {
    "ru": "⚠️ Это может быть опасно. Немедленно обратитесь в неотложную помощь (ER) или позвоните 911.",
    "en": "⚠️ This could be serious. Please go to the ER or call 911 immediately.",
    "es": "⚠️ Podría ser grave. Ve a urgencias o llama al 911 de inmediato.",
    "uk": "⚠️ Це може бути небезпечно. Негайно зверніться до невідкладної допомоги (ER) або зателефонуйте 911.",
}

QUICK_TOPICS = {
    "ru": ["Долголетие", "Питание", "Сон"],
    "en": ["Longevity", "Nutrition", "Sleep"],
    "es": ["Longevidad", "Nutrición", "Sueño"],
    "uk": ["Довголіття", "Харчування", "Сон"],
}

# -------------------------- STATE ---------------------------
def get_state(user_id: int) -> Dict[str, Any]:
    st = USER_STATES[user_id]
    if "initialized" not in st:
        st.update({
            "initialized": True,
            "lang": None,
            "intent": None,
            "slots": {},
            "last_user_msgs": deque(maxlen=MAX_LAST_USER_MSGS),
            "last_bot_msgs": deque(maxlen=MAX_LAST_BOT_MSGS),
            "last_seen": time.time(),
            "fell_back": False,
            "timed_out": False,
        })
    return st

def update_seen(st: Dict[str, Any]):
    st["last_seen"] = time.time()

def timed_out(st: Dict[str, Any]) -> bool:
    return (time.time() - st.get("last_seen", 0)) > SESSION_TIMEOUT_SEC

# --------------------- TELEMETRY LOGGING --------------------
def log_telemetry(user_id: int, intent: str, slots: Dict[str, Any], fell_back: bool):
    try:
        exists = os.path.exists(TELEMETRY_CSV)
        with open(TELEMETRY_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["ts_iso", "user_id", "intent", "filled_slots", "fallback_used"])
            w.writerow([datetime.utcnow().isoformat(), user_id, intent, ";".join(f"{k}={v}" for k,v in slots.items()), int(fell_back)])
    except Exception as e:
        logger.warning(f"Telemetry write failed: {e}")

# ------------------------ UTILITIES -------------------------
def extract_numbers(text: str):
    nums = re.findall(r"\d+", text)
    # quick mapping for Russian words like "семёрку" -> 7
    t = text.lower()
    word_map = {
        "семерку": 7, "семёрку": 7, "семь": 7,
        "восьмерку": 8, "восьмёрку": 8, "восемь": 8,
        "девятку": 9, "девять": 9,
    }
    for w, n in word_map.items():
        if w in t:
            nums.append(str(n))
    return [int(n) for n in nums]

def contains_any(text: str, keywords):
    t = text.lower()
    return any(k in t for k in keywords)

def choose_intent(text: str, lang: str) -> Tuple[str, float]:
    """Score-based rule intent classification. Returns (intent, score)."""
    t = text.lower()
    best_intent = "other"
    best_score = 0
    for intent in INTENTS:
        if intent == "other":
            continue
        score = 0
        for kw in KW[lang][intent]:
            if kw in t:
                score += 1
        if score > best_score:
            best_score = score
            best_intent = intent
    return best_intent, float(best_score)

def safety_check(text: str, lang: str) -> bool:
    return contains_any(text, SAFETY_FLAGS.get(lang, []))

def anti_loop_text(st: Dict[str, Any], text: str) -> str:
    """Avoid sending the same text >2 times; append a soft variation if needed."""
    last = st["last_bot_msgs"]
    if last and text.strip() == (last[-1] or "").strip():
        # If already sent once, vary slightly
        text = text + " ▸"
    return text

def record_bot_text(st: Dict[str, Any], text: str):
    st["last_bot_msgs"].append(text)

# ------------------------ TEMPLATES -------------------------
TEMPLATES = {
    "ru": {
        "greet": "Привет! Я TendAI. Что вас беспокоит или интересует сейчас?",
        "resume": "Продолжим с того места, где остановились?",
        "fallback_q": "Я правильно понимаю тему? Выберите одно из вариантов или напишите подробнее.",
        "ask_scale": "Кстати, по шкале 0–10 как сейчас?",
        "assessment": [
            "Понял. Коротко: есть ли сейчас боль, слабость, головокружение?",
            "Как спалось прошлой ночью и была ли энергия утром?",
            "Если удобно, оцените самочувствие 0–10 и что влияет больше всего?"
        ],
        "longevity": [
            "Вы про долголетие. С какой области начнём — вес/питание, сон, активность или анализы?",
            "Краткий совет: маленькие постоянные шаги > резких перемен.",
            "Готов дать мини-план. Что приоритетно прямо сейчас?"
        ],
        "nutrition": [
            "Про питание. Цель — снизить вес, держать вес или набрать мышечную массу?",
            "Быстрый ориентир: белок 1.2–1.6 г/кг/день, овощи каждый приём пищи.",
            "Есть ли ограничения/предпочтения в еде?"
        ],
        "sleep": [
            "Про сон. Во сколько обычно ложитесь и во сколько хотите просыпаться?",
            "Совет: фиксированное время подъёма и свет утром стабилизируют ритм.",
            "Нужен мини-план сна?"
        ],
        "activity": [
            "Про активность. Сколько шагов/минут умеренной нагрузки сейчас в день?",
            "Правило: чуть-чуть чаще и чуть-чуть дольше каждую неделю.",
            "Есть ли боли/ограничения?"
        ],
        "labs": [
            "Про анализы. Хотите базовый набор для здоровья и долголетия?",
            "Часто включают: липидный профиль, HbA1c, глюкоза натощак, ферритин, ТТГ, витамин D.",
            "Когда сдавали последнее обследование?"
        ],
        "symptom": [
            "Давайте уточним симптом. Где именно? (например: голова, спина, живот)",
            "Какой характер боли: тупая/острая/жгучая/пульсирующая? И сколько длится?",
            "Если удобно, по шкале 0–10 какая интенсивность сейчас?"
        ],
        "plan_sleep_ready": "Мини-план сна: за 2–3 ч до сна — тише и темнее; подъём в одно время; свет утром; кофе до 14:00. Продолжать?",
        "er": EMERGENCY_LINES["ru"],
    },
    "en": {
        "greet": "Hi, I’m TendAI. What’s bothering or interesting you right now?",
        "resume": "Shall we continue from where we left off?",
        "fallback_q": "Am I getting the topic right? Pick an option or type more details.",
        "ask_scale": "By the way, on a 0–10 scale, how is it now?",
        "assessment": [
            "Got it. Briefly: any pain, weakness, or dizziness now?",
            "How did you sleep last night, and energy this morning?",
            "If you can, rate 0–10 and what affects it most?"
        ],
        "longevity": [
            "Longevity — shall we start with weight/nutrition, sleep, activity, or labs?",
            "Quick tip: tiny consistent steps beat sudden changes.",
            "I can draft a mini-plan. What’s priority right now?"
        ],
        "nutrition": [
            "Nutrition. Is the goal weight loss, maintenance, or muscle gain?",
            "Rule of thumb: protein 1.2–1.6 g/kg/day, veggies each meal.",
            "Any dietary restrictions or preferences?"
        ],
        "sleep": [
            "Sleep. What time do you usually go to bed and want to wake up?",
            "Tip: fixed wake time + morning light stabilize rhythm.",
            "Want a quick sleep plan?"
        ],
        "activity": [
            "Activity. How many steps/minutes of moderate exercise per day now?",
            "Rule: a bit more often and a bit longer each week.",
            "Any pain or limitations?"
        ],
        "labs": [
            "Labs. Looking for a basic health & longevity panel?",
            "Often includes: lipid panel, HbA1c, fasting glucose, ferritin, TSH, vitamin D.",
            "When was your last checkup?"
        ],
        "symptom": [
            "Let’s detail the symptom. Where exactly? (e.g., head, back, abdomen)",
            "What’s the character: dull/sharp/burning/throbbing? How long has it lasted?",
            "If it helps, on a 0–10 scale, how intense is it now?"
        ],
        "plan_sleep_ready": "Sleep mini-plan: wind down 2–3h before bed; fixed wake time; morning light; caffeine before 2pm. Continue?",
        "er": EMERGENCY_LINES["en"],
    },
    "es": {
        "greet": "Hola, soy TendAI. ¿Qué te preocupa o interesa ahora mismo?",
        "resume": "¿Seguimos donde lo dejamos?",
        "fallback_q": "¿Es este el tema? Elige una opción o escribe más detalles.",
        "ask_scale": "Por cierto, en una escala 0–10, ¿cómo está ahora?",
        "assessment": [
            "Entendido. Breve: ¿tienes dolor, debilidad o mareo ahora?",
            "¿Cómo dormiste anoche y la energía esta mañana?",
            "Si puedes, califica 0–10 y ¿qué lo afecta más?"
        ],
        "longevity": [
            "Longevidad: ¿empezamos con peso/nutrición, sueño, actividad o análisis?",
            "Consejo: pasos pequeños y constantes superan los cambios bruscos.",
            "Puedo sugerir un mini plan. ¿Qué es prioridad ahora?"
        ],
        "nutrition": [
            "Nutrición. ¿Objetivo: bajar peso, mantener o ganar músculo?",
            "Guía: proteína 1.2–1.6 g/kg/día, verduras en cada comida.",
            "¿Alguna restricción o preferencia alimentaria?"
        ],
        "sleep": [
            "Sueño. ¿A qué hora te acuestas y a qué hora quieres despertarte?",
            "Tip: hora fija de despertar y luz por la mañana.",
            "¿Quieres un mini plan de sueño?"
        ],
        "activity": [
            "Actividad. ¿Cuántos pasos/minutos de ejercicio moderado por día?",
            "Regla: un poco más a menudo y un poco más largo cada semana.",
            "¿Algún dolor o limitación?"
        ],
        "labs": [
            "Análisis. ¿Buscas un panel básico de salud y longevidad?",
            "Suele incluir: perfil lipídico, HbA1c, glucosa en ayunas, ferritina, TSH, vitamina D.",
            "¿Cuándo fue tu último chequeo?"
        ],
        "symptom": [
            "Detallamos el síntoma. ¿Dónde exactamente? (p.ej., cabeza, espalda, abdomen)",
            "Carácter: sordo/agudo/ardor/pulsátil. ¿Y cuánto tiempo lleva?",
            "Si ayuda, en 0–10, ¿qué intensidad tiene ahora?"
        ],
        "plan_sleep_ready": "Mini plan: relajarse 2–3h antes; despertar fijo; luz por la mañana; cafeína antes de las 14:00. ¿Seguimos?",
        "er": EMERGENCY_LINES["es"],
    },
    "uk": {
        "greet": "Привіт, я TendAI. Що турбує або цікавить зараз?",
        "resume": "Продовжимо з того місця, де зупинилися?",
        "fallback_q": "Я правильно розумію тему? Оберіть варіант або напишіть деталі.",
        "ask_scale": "До речі, за шкалою 0–10 як зараз?",
        "assessment": [
            "Зрозумів. Коротко: є зараз біль, слабкість або запаморочення?",
            "Як спалося вночі і енергія зранку?",
            "Якщо зручно, оцініть 0–10 і що впливає найбільше?"
        ],
        "longevity": [
            "Довголіття. Почнемо з ваги/харчування, сну, активності чи аналізів?",
            "Порада: маленькі сталі кроки кращі за різкі зміни.",
            "Можу дати міні‑план. Що в пріоритеті?"
        ],
        "nutrition": [
            "Про харчування. Мета — схуднення, утримання ваги чи набір м’язів?",
            "Орієнтир: білок 1.2–1.6 г/кг/день, овочі кожного прийому.",
            "Є обмеження або вподобання в їжі?"
        ],
        "sleep": [
            "Про сон. Коли зазвичай лягаєте і коли хочете прокидатися?",
            "Порада: фіксований час підйому і світло зранку стабілізують ритм.",
            "Потрібен міні‑план сну?"
        ],
        "activity": [
            "Про активність. Скільки кроків/хвилин помірного навантаження на день?",
            "Правило: трохи частіше і трохи довше щотижня.",
            "Є болі або обмеження?"
        ],
        "labs": [
            "Про аналізи. Потрібен базовий набір для здоров’я й довголіття?",
            "Часто включає: ліпідний профіль, HbA1c, глюкоза натще, феритин, ТТГ, вітамін D.",
            "Коли було останнє обстеження?"
        ],
        "symptom": [
            "Уточнимо симптом. Де саме? (наприклад: голова, спина, живіт)",
            "Який характер болю: тупий/гострий/пекучий/пульсуючий? І скільки триває?",
            "Якщо зручно, за шкалою 0–10 яка інтенсивність зараз?"
        ],
        "plan_sleep_ready": "Міні‑план сну: за 2–3 год до сну — тиша/темрява; підйом у той самий час; світло зранку; кава до 14:00. Продовжити?",
        "er": EMERGENCY_LINES["uk"],
    },
}

# ------------------------ SLOT LOGIC ------------------------
BODY_PARTS = {
    "ru": ["голова", "спина", "груд", "живот", "горло", "плеч", "рук", "ног", "сустав"],
    "en": ["head", "back", "chest", "abdomen", "stomach", "throat", "shoulder", "arm", "leg", "joint"],
    "es": ["cabeza", "espalda", "pecho", "abdomen", "estómago", "garganta", "hombro", "brazo", "pierna", "articulación"],
    "uk": ["голова", "спина", "груди", "живіт", "горло", "плече", "рука", "нога", "суглоб"],
}

PAIN_CHAR = {
    "ru": ["тупая", "острая", "жгучая", "пульсирующая"],
    "en": ["dull", "sharp", "burning", "throbbing"],
    "es": ["sordo", "agudo", "ardor", "pulsátil"],
    "uk": ["тупий", "гострий", "пекучий", "пульсуючий"],
}

TIME_UNITS = {
    "ru": {"час": "hours", "часа": "hours", "часов": "hours", "день": "days", "дня": "days", "дней": "days", "недел": "weeks"},
    "en": {"hour": "hours", "hours": "hours", "day": "days", "days": "days", "week": "weeks", "weeks": "weeks"},
    "es": {"hora": "hours", "horas": "hours", "día": "days", "días": "days", "semana": "weeks", "semanas": "weeks"},
    "uk": {"год": "hours", "години": "hours", "годин": "hours", "день": "days", "днів": "days", "тижд": "weeks"},
}

def parse_duration(text: str, lang: str) -> Optional[str]:
    t = text.lower()
    nums = extract_numbers(t)
    if not nums:
        return None
    for unit_src, unit_std in TIME_UNITS[lang].items():
        if unit_src in t:
            return f"{nums[0]} {unit_std}"
    # If number but no unit, assume hours if context suggests recent
    return f"{nums[0]} hours"

def fill_symptom_slots(st: Dict[str, Any], text: str, lang: str):
    slots = st.setdefault("slots", {})
    t = text.lower()

    # where
    if "where" not in slots:
        for part in BODY_PARTS[lang]:
            if part in t:
                slots["where"] = part
                break

    # character
    if "character" not in slots:
        for c in PAIN_CHAR[lang]:
            if c in t:
                slots["character"] = c
                break

    # duration
    if "duration" not in slots:
        dur = parse_duration(t, lang)
        if dur:
            slots["duration"] = dur

    # intensity
    if "intensity" not in slots:
        nums = extract_numbers(t)
        if nums:
            # Take first number 0-10 as intensity if plausible
            cand = nums[0]
            if 0 <= cand <= 10:
                slots["intensity"] = cand

def symptom_next_question(st: Dict[str, Any], lang: str) -> str:
    slots = st.get("slots", {})
    if "where" not in slots:
        return TEMPLATES[lang]["symptom"][0]
    if "character" not in slots:
        return TEMPLATES[lang]["symptom"][1]
    if "duration" not in slots:
        return TEMPLATES[lang]["symptom"][1]  # duration is mentioned together
    if "intensity" not in slots:
        return TEMPLATES[lang]["symptom"][2]
    return ""  # All filled

# ---------------------- REPLY BUILDERS ----------------------
def make_quick_keyboard(lang: str):
    buttons = [KeyboardButton(x) for x in QUICK_TOPICS[lang]]
    return ReplyKeyboardMarkup([buttons], resize_keyboard=True, one_time_keyboard=True)

def build_fallback(lang: str) -> Tuple[str, ReplyKeyboardMarkup]:
    return (TEMPLATES[lang]["fallback_q"], make_quick_keyboard(lang))

def ready_recommendation_symptom(st: Dict[str, Any], lang: str) -> str:
    s = st.get("slots", {})
    where = s.get("where", "?")
    character = s.get("character", "?")
    duration = s.get("duration", "?")
    intensity = s.get("intensity", "?")
    if lang == "ru":
        tip = f"Итог: {where}, {character}, {duration}, интенсивность {intensity}/10.\n" \
              f"Совет: отдых, тёплый компресс 15–20 мин, НПВП при отсутствии противопоказаний, мониторинг. " \
              f"Если усиливается или есть новые симптомы — обратитесь к врачу."
    elif lang == "es":
        tip = f"Resumen: {where}, {character}, {duration}, intensidad {intensity}/10.\n" \
              f"Consejo: reposo, compresa tibia 15–20 min, AINE si procede, observar. " \
              f"Si empeora o aparecen señales nuevas, consulta médica."
    elif lang == "uk":
        tip = f"Підсумок: {where}, {character}, {duration}, інтенсивність {intensity}/10.\n" \
              f"Порада: відпочинок, теплий компрес 15–20 хв, НПЗЗ за потреби, спостерігати. " \
              f"Якщо посилюється або з’являються нові ознаки — зверніться до лікаря."
    else:
        tip = f"Summary: {where}, {character}, {duration}, intensity {intensity}/10.\n" \
              f"Tip: rest, warm compress 15–20 min, NSAID if appropriate, monitor. " \
              f"If it worsens or new signs appear, see a clinician."
    return tip

def build_intent_reply(intent: str, lang: str, st: Dict[str, Any], text: str) -> Tuple[str, Optional[ReplyKeyboardMarkup]]:
    kb = None
    # Safety: never ask 0–10 unless assessment OR symptom intensity slot prompting
    if intent == "assessment":
        nums = extract_numbers(text)
        lines = TEMPLATES[lang]["assessment"]
        if nums:
            # Extract a rating from any number 0-10
            rating = None
            for n in nums:
                if 0 <= n <= 10:
                    rating = n
                    break
            if rating is not None:
                if lang == "ru":
                    msg = f"Принял оценку {rating}/10. Что сейчас больше всего мешает?"
                elif lang == "es":
                    msg = f"Anoto {rating}/10. ¿Qué influye más ahora?"
                elif lang == "uk":
                    msg = f"Прийняв {rating}/10. Що найбільше заважає зараз?"
                else:
                    msg = f"Got {rating}/10. What affects it the most right now?"
                return msg, kb
        # No numbers — gentle prompt included later
        msg = lines[0] + "\n" + lines[1] + "\n" + TEMPLATES[lang]["ask_scale"]
        return msg, kb

    if intent == "longevity":
        lines = TEMPLATES[lang]["longevity"]
        msg = lines[0] + "\n" + lines[1] + "\n" + lines[2]
        kb = make_quick_keyboard(lang)
        return msg, kb

    if intent == "nutrition":
        lines = TEMPLATES[lang]["nutrition"]
        msg = lines[0] + "\n" + lines[1] + "\n" + lines[2]
        return msg, kb

    if intent == "sleep":
        lines = TEMPLATES[lang]["sleep"]
        msg = lines[0] + "\n" + lines[1] + "\n" + lines[2]
        return msg, kb

    if intent == "activity":
        lines = TEMPLATES[lang]["activity"]
        msg = lines[0] + "\n" + lines[1] + "\n" + lines[2]
        return msg, kb

    if intent == "labs":
        lines = TEMPLATES[lang]["labs"]
        msg = lines[0] + "\n" + lines[1] + "\n" + lines[2]
        return msg, kb

    if intent == "symptom":
        fill_symptom_slots(st, text, lang)
        next_q = symptom_next_question(st, lang)
        if next_q:
            return next_q, kb
        # All slots filled — give recommendation
        return ready_recommendation_symptom(st, lang), kb

    # other
    fb_txt, kb = build_fallback(lang)
    return fb_txt, kb

# ---------------------- CORE HANDLER ------------------------
async def safe_send(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, kb=None):
    st = get_state(update.effective_user.id)
    text = anti_loop_text(st, text)
    await update.effective_chat.send_message(text, reply_markup=kb if kb else ReplyKeyboardRemove())
    record_bot_text(st, text)

def set_lang_if_needed(st: Dict[str, Any], text: str):
    if not st.get("lang"):
        st["lang"] = detect_lang(text)

def soft_timeout_reset_if_needed(st: Dict[str, Any]) -> bool:
    if timed_out(st):
        st["timed_out"] = True
        st["intent"] = None
        st["slots"] = {}
        return True
    return False

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_user.id)
    st["lang"] = st.get("lang") or "en"
    await safe_send(update, context, TEMPLATES[st["lang"]]["greet"])

async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_user.id)
    lang = st.get("lang") or "en"
    txt = {
        "ru": "Я коротко уточняю тему, задаю 1–2 вопроса и даю мини‑совет. Команды: /start /help /reset.",
        "en": "I ask 1–2 clarifying questions and give a mini-tip. Commands: /start /help /reset.",
        "es": "Hago 1–2 preguntas y doy un mini consejo. Comandos: /start /help /reset.",
        "uk": "Ставлю 1–2 запитання і даю міні‑пораду. Команди: /start /help /reset.",
    }[lang]
    await safe_send(update, context, txt)

async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_user.id)
    st.update({"intent": None, "slots": {}, "last_bot_msgs": deque(maxlen=MAX_LAST_BOT_MSGS)})
    lang = st.get("lang") or "en"
    await safe_send(update, context, TEMPLATES[lang]["greet"])

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""
    st = get_state(user_id)
    set_lang_if_needed(st, text)
    lang = st["lang"]
    update_seen(st)

    # Timeout soft resume
    if soft_timeout_reset_if_needed(st):
        await safe_send(update, context, TEMPLATES[lang]["resume"])
        return

    # Track last user messages
    st["last_user_msgs"].append(text)

    # Safety first
    if safety_check(text, lang):
        await safe_send(update, context, TEMPLATES[lang]["er"])
        log_telemetry(user_id, "safety", st.get("slots", {}), fell_back=False)
        return

    # Route intent if none or fallback
    intent_score = 0.0
    if not st.get("intent") or st.get("fell_back"):
        intent, intent_score = choose_intent(text, lang)
        # If low confidence => fallback
        if intent == "other" or intent_score < 1.0:
            st["fell_back"] = True
            msg, kb = build_fallback(lang)
            await safe_send(update, context, msg, kb)
            log_telemetry(user_id, "fallback", st.get("slots", {}), fell_back=True)
            return
        st["intent"] = intent
        st["fell_back"] = False

    # If user tapped a quick topic during fallback, map it to intent
    quick_map = {
        QUICK_TOPICS[lang][0].lower(): "longevity",
        QUICK_TOPICS[lang][1].lower(): "nutrition",
        QUICK_TOPICS[lang][2].lower(): "sleep",
    }
    tl = text.lower().strip()
    if st.get("fell_back") or st.get("intent") == "other":
        if tl in quick_map:
            st["intent"] = quick_map[tl]
            st["fell_back"] = False

    # Build reply for current intent
    msg, kb = build_intent_reply(st["intent"], lang, st, text)

    await safe_send(update, context, msg, kb)
    log_telemetry(user_id, st.get("intent") or "other", st.get("slots", {}), fell_back=st.get("fell_back", False))

# ------------------------ TESTS -----------------------------
def _simulate(texts, lang_hint=None):
    """A tiny offline simulator of core routing/slots for tests (no Telegram)."""
    st = {
        "lang": lang_hint or detect_lang(texts[0] if texts else ""),
        "intent": None,
        "slots": {},
        "last_bot_msgs": deque(maxlen=MAX_LAST_BOT_MSGS),
        "last_user_msgs": deque(maxlen=MAX_LAST_USER_MSGS),
        "last_seen": time.time(),
        "fell_back": False,
    }
    outs = []
    for t in texts:
        # Safety
        if safety_check(t, st["lang"]):
            outs.append(TEMPLATES[st["lang"]]["er"])
            continue
        # Intent
        if not st["intent"] or st["fell_back"]:
            intent, score = choose_intent(t, st["lang"])
            if intent == "other" or score < 1.0:
                st["fell_back"] = True
                outs.append(TEMPLATES[st["lang"]]["fallback_q"])
                continue
            st["intent"] = intent
            st["fell_back"] = False
        # Build message
        msg, _ = build_intent_reply(st["intent"], st["lang"], st, t)
        outs.append(msg)
    return outs, st

def run_tests():
    ok = True
    # 1) “Хочу обсудить своё долголетие” → уточнение долголетия, без просьбы 0–10
    outs, st = _simulate(["Хочу обсудить своё долголетие"], lang_hint="ru")
    ok &= ("долголет" in "".join(outs).lower()) and ("0–10" not in "".join(outs))

    # 2) “Голова болит уже 2 дня, тупая” → собирает слоты, задаёт недостающее/совет
    outs2, st2 = _simulate(["Голова болит уже 2 дня, тупая", "8"], lang_hint="ru")
    joined2 = " | ".join(outs2).lower()
    ok &= (("тупая" in joined2) or ("интенсивность" in joined2))

    # 3) “Поставь план сна” → задаёт время
    outs3, st3 = _simulate(["Поставь план сна"], lang_hint="ru")
    ok &= ("во сколько обычно ложитесь" in " ".join(outs3))

    # 4) “Оценка: где-то на семёрку” → извлекает 7 и идёт дальше
    outs4, st4 = _simulate(["Оценка: где-то на семёрку"], lang_hint="ru")
    ok &= any("7/10" in o for o in outs4)

    # 5) На испанском/английском бот отвечает на том же языке
    outs5, st5 = _simulate(["Hola, quiero hablar de longevidad"], lang_hint=None)
    ok &= ("Longevidad" in "".join(outs5)) or ("longevidad" in "".join(outs5).lower())

    return ok

# ------------------------- MAIN -----------------------------
def main():
    if os.getenv("RUN_TESTS", "0") == "1":
        passed = run_tests()
        print("TESTS PASSED" if passed else "TESTS FAILED")
        return

    if TELEGRAM_TOKEN == "YOUR_TELEGRAM_TOKEN_HERE":
        logger.warning("Please set TELEGRAM_TOKEN env var.")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CommandHandler("reset", handle_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("TendAI bot is starting (rules-first)...")
    app.run_polling()

if __name__ == "__main__":
    main()
