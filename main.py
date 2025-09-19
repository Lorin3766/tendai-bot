# -*- coding: utf-8 -*-
# TendAI main.py — обновлено: лимитер/тихие часы, утро+вечер, Youth-команды,
# безопасные headers для Sheets, Rules (evidence), мягкий фидбек, баннер профиля (1 раз),
# тёплый тон (мысль→вопрос), 3 пресета напоминаний, конкретные варианты,
# АВТО-ПРЕДЛОЖЕНИЕ ОПРОСНИКА С ПЕРВОГО СООБЩЕНИЯ + шаги height_cm/weight_kg/supplements
# + Главное меню, Smart check-in, Habits Quick-log, Micro-plans, Find care, Language switch,
#   контекстные чипы, /menu, и фикс синтаксической ошибки.
# + ДОБАВЛЕНО: полноценный ES, мини Smart-check-in утром/вечером, лог smart, лимит 3.

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
        logging.warning("intake_pro not found — PRO-опрос отключён на этом деплое.")
        async def _fallback_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            await q.message.reply_text(
                "PRO-опрос недоступен на этом деплое. Используйте /profile."
            )
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
        "start_where":"Where do you want to start now? (symptom/sleep/nutrition/labs/habits/longevity) — or tap /menu",
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
        # Youth quick labels
        "energy_title": "Energy for today:",
        "water_prompt": "Drink 300–500 ml of water. Remind in 2 hours?",
        "skin_title": "Skin/Body tip:",
        # Main menu labels
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
        # Chips
        "chips_hb": "Avoid triggers • OTC options • When to see a doctor",
        "chips_neck": "5-min routine • Heat/Ice tips • Red flags",
        # --- i18n доп. ключи ---
        "menu_pick_sym": "Pick a symptom:",
        "menu_miniplans": "Mini-plans:",
        "menu_findcare": "Find care:",
        "menu_quicklog": "Quick log:",
        "menu_reminders": "Reminders:",
        "menu_language": "Language / Idioma:",
        "back_btn": "◀ Back",
        "send_weight_hint": "Send weight in kg, e.g., 72.5",
        "logged_streak": "Logged. Streak: ",
        "choose_language_done": "Done.",
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
        "start_where":"С чего начнём? (симптом/сон/питание/анализы/привычки/долголетие) — или нажми /menu",
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
        "act_city_prompt":"Напишите город/район, чтобы подсказать лабораторию (текстом).",
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
        "h60_intro": "Коротко напишите, что беспокоит (например: «болит голова», «усталость», «боль в животе»). Я дам 3 ключевых совета за 60 секунд.",
        "h60_t1": "Возможные причины",
        "h60_t2": "Что сделать сейчас (24–48 ч)",
        "h60_t3": "Когда обратиться к врачу",
        "h60_serious": "Что серьёзное исключить",
        "energy_title": "Энергия на сегодня:",
        "water_prompt": "Выпей 300–500 мл воды. Напомнить через 2 часа?",
        "skin_title": "Совет для кожи/тела:",
        # Main menu labels
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
        # --- i18n доп. ключи ---
        "menu_pick_sym": "Выберите симптом:",
        "menu_miniplans": "Мини-планы:",
        "menu_findcare": "Куда обратиться:",
        "menu_quicklog": "Быстрый лог:",
        "menu_reminders": "Быстрые напоминания:",
        "menu_language": "Язык / Language:",
        "back_btn": "◀ Назад",
        "send_weight_hint": "Пришлите вес в кг, например 72.5",
        "logged_streak": "Записал. Серия: ",
        "choose_language_done": "Готово.",
    }
}
# Наследуем uk от ru и переопределяем отличия
T["uk"] = {**T["ru"], **{
    "help": "Короткі перевірки, план на 24–48 год, нагадування, щоденні чек-іни.\nКоманди: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /energy /mood /water /skin /ru /uk /en /es /menu",
    "daily_pm":"Вечірній чек-ін: як пройшов день?",
    "act_rem_2h": "⏰ Нагадати через 2 год",
    "energy_title": "Енергія на сьогодні:",
    "water_prompt": "Випий 300–500 мл води. Нагадати через 2 години?",
    "skin_title": "Догляд за шкірою/тілом:",
    "m_menu_title": "Головне меню",
    "m_sym": "🧭 Симптоми",
    "m_h60": "🩺 Здоровʼя за 60 секунд",
    "m_mini": "🔁 Міні-плани",
    "m_care": "🧪 Куди звернутись",
    "m_hab": "📊 Швидкий лог звичок",
    "m_rem": "🗓 Нагадати",
    "m_lang": "🌐 Мова",
    "m_privacy": "🔒 Приватність і як це працює",
    "m_smart": "🧠 Смарт-чек-ін",
    "m_soon": "🏠 Домашні аналізи/ЕКГ — скоро",
    "chips_hb": "Уникати тригери • OTC-варіанти • Коли до лікаря",
    "chips_neck": "Рутина 5 хв • Тепло/лід • Червоні прапори",
    # доп. ключи
    "menu_pick_sym": "Оберіть симптом:",
    "menu_miniplans": "Міні-плани:",
    "menu_findcare": "Куди звернутись:",
    "menu_quicklog": "Швидкий лог:",
    "menu_reminders": "Швидкі нагадування:",
    "menu_language": "Мова / Language:",
    "back_btn": "◀ Назад",
    "send_weight_hint": "Надішліть вагу в кг, напр. 72.5",
    "logged_streak": "Записано. Серія: ",
    "choose_language_done": "Готово.",
}}
# Полноценный испанский (поверх EN, затем переопределения)
T["es"] = {
    **T["en"],
    "welcome": "¡Hola! Soy TendAI — tu asistente de salud y longevidad.\nCuéntame qué te preocupa; te guiaré. Hagamos una breve encuesta de 40s para personalizar los consejos.",
    "help": "Chequeos cortos, planes de 24–48 h, recordatorios, check-ins diarios.\nComandos: /help /privacy /pause /resume /delete_data /profile /checkin_on 08:30 /checkin_off /settz +2 /health60 /energy /mood /water /skin /ru /uk /en /es /menu",
    "privacy": "TendAI no sustituye a un médico. Ofrecemos orientación y autocuidado. Guardamos datos mínimos para recordatorios. /delete_data para borrar.",
    "ask_consent": "¿Puedo enviarte un seguimiento más tarde para saber cómo estás?",
    "yes": "Sí", "no": "No",
    "unknown": "Necesito un poco más de info: ¿dónde exactamente y desde cuándo?",
    "profile_intro": "Encuesta rápida (~40s). Usa botones o escribe tu respuesta.",
    "p_step_1":"Paso 1/10. Sexo:",
    "p_step_2":"Paso 2/10. Edad:",
    "p_step_3":"Paso 3/10. Estatura (cm):",
    "p_step_4":"Paso 4/10. Peso (kg):",
    "p_step_5":"Paso 5/10. Objetivo principal:",
    "p_step_6":"Paso 6/10. Enfermedades crónicas:",
    "p_step_7":"Paso 7/10. Fármacos:",
    "p_step_8":"Paso 8/10. Suplementos:",
    "p_step_9":"Paso 9/10. Sueño (acostar/levantar, p.ej., 23:30/07:00):",
    "p_step_10":"Paso 10/10. Actividad:",
    "write":"✍️ Escribir",
    "skip":"⏭️ Omitir",
    "saved_profile":"Guardado: ",
    "start_where":"¿Por dónde empezamos ahora? (síntoma/sueño/nutrición/análisis/hábitos/longevidad) — o toca /menu",
    "daily_gm":"¡Buenos días! Chequeo rápido:",
    "daily_pm":"Chequeo de la tarde: ¿cómo fue el día?",
    "mood_good":"😃 Bien","mood_ok":"😐 Normal","mood_bad":"😣 Mal","mood_note":"✍️ Comentario",
    "mood_thanks":"¡Gracias! Que tengas un buen día 👋",
    "plan_header":"Tu plan de 24–48 h:",
    "plan_accept":"¿Lo probarás hoy?",
    "remind_when":"¿Cuándo te recuerdo y pregunto cómo sigues?",
    "thanks":"Hecho 🙌",
    "checkin_ping":"Chequeo rápido: ¿cómo está ahora (0–10)?",
    "checkin_better":"¡Genial! Sigue así 💪",
    "checkin_worse":"Si hay señales de alarma o dolor ≥7/10 — mejor busca atención médica.",
    "act_rem_2h":"⏰ Recordar en 2 h",
    "act_rem_4h":"⏰ Recordar en 4 h",
    "act_rem_eve":"⏰ Esta tarde",
    "act_rem_morn":"⏰ Mañana por la mañana",
    "act_save_episode":"💾 Guardar episodio",
    "act_ex_neck":"🧘 Rutina de cuello 5 min",
    "act_find_lab":"🧪 Encontrar laboratorio",
    "act_er":"🚑 Información de emergencia",
    "act_city_prompt":"Escribe tu ciudad/zona para sugerirte un laboratorio (texto).",
    "act_saved":"Guardado.",
    "er_text":"Si empeora, gran falta de aire, dolor torácico, confusión o fiebre alta persistente — acude a urgencias.",
    "px":"Según tu perfil: {sex}, {age} años; objetivo — {goal}.",
    "back":"◀ Atrás",
    "exit":"Salir",
    "ask_fb":"¿Te resultó útil?",
    "fb_thanks":"¡Gracias por tu opinión! ✅",
    "fb_write":"Escribe un breve comentario en un mensaje:",
    "fb_good":"👍 Me gusta",
    "fb_bad":"👎 No útil",
    "fb_free":"📝 Opinión",
    "h60_btn": "Salud en 60 segundos",
    "h60_intro": "Escribe brevemente qué te molesta (p. ej., “dolor de cabeza”, “fatiga”, “dolor de estómago”). Te daré 3 consejos clave en 60 s.",
    "h60_t1": "Causas posibles",
    "h60_t2": "Qué hacer ahora (24–48 h)",
    "h60_t3": "Cuándo ir al médico",
    "h60_serious": "Graves a descartar",
    "m_menu_title": "Menú principal",
    "m_sym": "🧭 Síntomas",
    "m_h60": "🩺 Salud en 60 s",
    "m_mini": "🔁 Mini-planes",
    "m_care": "🧪 Dónde acudir",
    "m_hab": "📊 Registro rápido de hábitos",
    "m_rem": "🗓 Recordarme",
    "m_lang": "🌐 Idioma",
    "m_privacy": "🔒 Privacidad y cómo funciona",
    "m_smart": "🧠 Chequeo inteligente",
    "m_soon": "🏠 Análisis/ECG en casa — pronto",
    "chips_hb": "Evitar desencadenantes • Opciones OTC • Cuándo consultar",
    "chips_neck": "Rutina 5 min • Calor/Hielo • Señales de alarma",
    "menu_pick_sym": "Elige un síntoma:",
    "menu_miniplans": "Mini-planes:",
    "menu_findcare": "Buscar atención:",
    "menu_quicklog": "Registro rápido:",
    "menu_reminders": "Recordatorios:",
    "menu_language": "Idioma / Language:",
    "back_btn": "◀ Atrás",
    "send_weight_hint": "Envía el peso en kg, p. ej., 72.5",
    "logged_streak": "Registrado. Racha: ",
    "choose_language_done": "Listo.",
}

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
    # показать баннер в следующий ответ
    users_set(uid, "profile_banner_shown", "no")
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
ws_feedback = ws_users = ws_profiles = ws_episodes = ws_reminders = ws_daily = ws_rules = ws_habits = None

# === Canonical headers + safe reader ===
USERS_HEADERS = [
    "user_id","username","lang","consent","tz_offset","checkin_hour","paused",
    "quiet_hours","last_sent_utc","sent_today","streak","challenge_id","challenge_day",
    # мягкий фидбек и баннер профиля
    "last_fb_asked","profile_banner_shown"
]
PROFILES_HEADERS = ["user_id","sex","age","goal","conditions","meds","allergies","sleep","activity","diet","notes","updated_at","goals","diet_focus","steps_target","cycle_enabled","cycle_last_date","cycle_avg_len",
                    # новые поля
                    "height_cm","weight_kg","supplements"]
EPISODES_HEADERS = ["episode_id","user_id","topic","started_at","baseline_severity","red_flags","plan_accepted","target","reminder_at","next_checkin_at","status","last_update","notes"]
REMINDERS_HEADERS = ["id","user_id","text","when_utc","created_at","status"]
DAILY_HEADERS = ["timestamp","user_id","mood","comment"]
FEEDBACK_HEADERS = ["timestamp","user_id","name","username","rating","comment"]
RULES_HEADERS = ["rule_id","domain","segment","lang","text","citations"]
# NEW: habits quick-log (тип/значение/ед./текущий streak по типу)
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

# === Сохраняем gspread client и id таблицы для register_intake_pro ===
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
            # выравниваем заголовки при расхождении
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
def _headers(ws):
    return ws.row_values(1)

def users_get(uid: int) -> dict:
    if SHEETS_ENABLED:
        for r in ws_records(ws_users, USERS_HEADERS):
            if str(r.get("user_id")) == str(uid):
                return r
        return {}
    return MEM_USERS.get(uid, {})

# === PATCH 1: мягкий merge вместо перезаписи ===
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

# --- HABITS LOG ---
def habits_add(uid: int, typ: str, value: Optional[str], unit: Optional[str]) -> int:
    """Append habit row and return current streak (naive: consecutive days incl. today)."""
    ts = iso(utcnow())
    rec = {"timestamp":ts,"user_id":str(uid),"type":typ,"value":value or "1","unit":unit or "", "streak":"0"}
    # write first (streak after)
    if SHEETS_ENABLED:
        ws_habits.append_row([rec.get(h,"") for h in HABITS_HEADERS])
        rows = ws_records(ws_habits, HABITS_HEADERS)
        rows = [r for r in rows if r.get("user_id")==str(uid) and r.get("type")==typ]
    else:
        MEM_HABITS.append(rec)
        rows = [r for r in MEM_HABITS if r.get("user_id")==str(uid) and r.get("type")==typ]
    # compute streak
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
            if streak == 0:
                streak = 1
            else:
                streak += 1
            expected = expected - timedelta(days=1)
        elif d < expected:
            break
        else:
            continue
    # update last row streak
    if rows_sorted:
        last = rows_sorted[0]
        last["streak"] = str(streak)
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
        # вечерний (отдельный текст)
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

# === Вечер: отдельный джоб и планировщик только для вечера ===
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
    # ↑ лимит поднят до 3, чтобы утренний блок помещался
    return sent_today < 3

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
    "Always answer strictly in {lang}. Keep replies short (<=6 lines + up to 4 bullets). "
    "Personalize using the provided profile (sex/age/goal/conditions). "
    "TRIAGE: ask 1–2 clarifiers first; advise ER only for clear red flags. "
    "Return JSON ONLY like: "
    "{\"intent\":\"symptom\"|\"nutrition\"|\"sleep\"|\"labs\"|\"habits\"|\"longevity\"|\"other\","
    "\"assistant_reply\": \"string\", \"followups\": [\"string\"], \"needs_more\": true, "
    "\"red_flags\": false, \"confidence\": 0.0}"
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

# ===== Rules-based подсказки =====
def rules_match(seg: str, prof: dict) -> bool:
    if not seg:
        return True
    for part in seg.split("&"):
        m = re.match(r'(\w+)\s*(>=|<=|=|>|<)\s*([\w\-]+)', part.strip())
        if not m:
            return False
        k, op, v = m.groups()
        pv = (prof.get(k) or prof.get(k.lower()) or "")
        if k in ("age", "steps_target", "cycle_avg_len","height_cm","weight_kg"):
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

# ===== Мини-логика цикла =====
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

async def job_daily_checkin(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    uid, lang = d.get("user_id"), d.get("lang","en")
    u = users_get(uid)
    if (u.get("paused") or "").lower()=="yes":
        return
    # приветствие + настроение (утро)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_good"], callback_data="mood|good"),
         InlineKeyboardButton(T[lang]["mood_ok"], callback_data="mood|ok"),
         InlineKeyboardButton(T[lang]["mood_bad"], callback_data="mood|bad")],
        [InlineKeyboardButton(T[lang]["mood_note"], callback_data="mood|note")]
    ])
    await maybe_send(context, uid, T[lang]["daily_gm"], kb)

    # ДОБАВЛЕНО: мини smart check-in сразу после настроения
    await maybe_send(
        context, uid,
        ("Как вы сейчас?" if lang != "en" else "How are you now?"),
        inline_smart_checkin(lang)
    )

    # 1–2 совета по питанию из Rules
    prof = profiles_get(uid)
    tips = pick_nutrition_tips(lang, prof, limit=2)
    if tips:
        await maybe_send(context, uid, "• " + "\n• ".join(tips))

    # деликатный совет по фазе цикла (если включено)
    phase = cycle_phase_for(uid)
    if phase:
        tip = cycle_tip(lang, phase)
        if tip:
            await maybe_send(context, uid, tip)

# Новый вечерний джоб — другой текст
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

    # ДОБАВЛЕНО: мини smart check-in вечером
    await maybe_send(
        context, uid,
        ("Как вы сейчас?" if lang != "en" else "How are you now?"),
        inline_smart_checkin(lang)
    )

# ===== Serious keywords =====
SERIOUS_KWS = {
    "diabetes":["diabetes","диабет","сахарный","цукров","глюкоза","hba1c","гликированный"],
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

# ===== Персонализированный префикс/баннер (показывать 1 раз) =====
def _ru_age_phrase(age_str: str) -> str:
    try:
        n = int(re.search(r"\d+", age_str).group())
    except Exception:
        return age_str
    last2 = n % 100
    last1 = n % 10
    if 11 <= last2 <= 14:
        word = "лет"
    elif last1 == 1:
        word = "год"
    elif 2 <= last1 <= 4:
        word = "года"
    else:
        word = "лет"
    return f"{n} {word}"

def profile_banner(lang: str, profile: dict) -> str:
    sex = str(profile.get("sex") or "").strip().lower()
    age_raw = str(profile.get("age") or "").strip()
    goal = (profile.get("goal") or profile.get("goals") or "").strip()
    ht = (profile.get("height_cm") or "").strip()
    wt = (profile.get("weight_kg") or "").strip()
    if lang == "ru":
        sex_ru = {"male":"мужчина","female":"женщина","other":"человек"}.get(sex, "человек")
        age_ru = _ru_age_phrase(age_raw or "—")
        goal_ru = {"longevity":"долголетие","energy":"энергия","sleep":"сон","weight":"похудение","strength":"сила"}.get(goal, goal or "—")
        hw = f", {ht}см/{wt}кг" if (ht or wt) else ""
        return f"{sex_ru}, {age_ru}{hw}; цель — {goal_ru}"
    if lang == "uk":
        hw = f", {ht}см/{wt}кг" if (ht or wt) else ""
        return f"{sex or '—'}, {age_raw or '—'}{hw}; ціль — {goal or '—'}"
    if lang == "es":
        hw = f", {ht}cm/{wt}kg" if (ht or wt) else ""
        return f"{sex or '—'}, {age_raw or '—'}{hw}; objetivo — {goal or '—'}"
    # EN — fixed
    hw = f", {ht}cm/{wt}kg" if (ht or wt) else ""
    return f"{sex or '—'}, {age_raw or '—'}{hw}; goal — {goal or '—'}"

def should_show_profile_banner(uid: int) -> bool:
    u = users_get(uid)
    return (u.get("profile_banner_shown") or "no") != "yes"

def apply_warm_tone(text: str, lang: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", (text or "").strip())

def ask_feedback_soft(uid: int, context: ContextTypes.DEFAULT_TYPE, lang: str):
    try:
        u = users_get(uid)
        last = (u.get("last_fb_asked") or "").strip()
        today = (utcnow() + timedelta(hours=int(str(u.get("tz_offset") or "0")))).date().isoformat()
        if last == today:
            return
        kb = inline_feedback_kb(lang)
        context.application.create_task(context.bot.send_message(uid, T[lang]["ask_fb"], reply_markup=kb))
        users_set(uid, "last_fb_asked", today)
    except Exception as e:
        logging.warning(f"ask_feedback_soft error: {e}")

# ===== Планы и кнопки =====
def pain_plan(lang: str, red_flags_selected: List[str], profile: dict) -> List[str]:
    flg = [s for s in red_flags_selected if s and str(s).lower() not in ["none","нет","немає","ninguno","no"]]
    if flg:
        return {"ru":["⚠️ Есть тревожные признаки. Лучше как можно скорее показаться врачу/в скорую."],
                "uk":["⚠️ Є тривожні ознаки. Варто якнайшвидше звернутися до лікаря/швидкої."],
                "en":["⚠️ Red flags present. Please seek urgent medical evaluation."],
                "es":["⚠️ Señales de alarma presentes. Busca evaluación médica urgente."]}[lang]
    age_num = 0
    try:
        age_num = int(re.search(r"\d+", str(profile.get("age") or "")).group(0))
    except Exception:
        age_num = 0
    extra = []
    if age_num >= 60:
        extra.append({"ru":"Вам 60+, будьте осторожны с НПВП; пейте воду и при ухудшении обратитесь к врачу.",
                      "uk":"Вам 60+, обережно з НПЗЗ; пийте воду, за погіршення — до лікаря.",
                      "en":"Age 60+: be careful with NSAIDs; hydrate and seek care if worsening.",
                      "es":"Edad 60+: cuidado con AINEs; hidrátate y busca atención si empeora."}[lang])
    core = {"ru":["1) Вода 400–600 мл и 15–20 мин тишины/отдыха.",
                  "2) Если нет противопоказаний — ибупрофен 200–400 мг однократно с едой.",
                  "3) Проветрить, уменьшить экран на 30–60 мин.","Цель: к вечеру боль ≤3/10."],
            "uk":["1) Вода 400–600 мл і 15–20 хв спокою.",
                  "2) Якщо нема протипоказань — ібупрофен 200–400 мг одноразово з їжею.",
                  "3) Провітрити, менше екрану 30–60 хв.","Мета: до вечора біль ≤3/10."],
            "en":["1) Drink 400–600 ml water; rest 15–20 min.",
                  "2) If no contraindications — ibuprofen 200–400 mg once with food.",
                  "3) Air the room; reduce screen time 30–60 min.","Goal: by evening pain ≤3/10."],
            "es":["1) Bebe 400–600 ml de agua; descansa 15–20 min.",
                  "2) Si no hay contraindicaciones — ibuprofeno 200–400 mg una vez con comida.",
                  "3) Ventila la habitación; reduce pantallas 30–60 min.","Meta: por la tarde dolor ≤3/10."]}[lang]
    return core + extra + [T[lang]["er_text"]]

# ===== Клавиатуры (вкл. главное меню, подменю, чипы) =====
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
        [InlineKeyboardButton("🩺 Pain", callback_data="topic|pain"),
         InlineKeyboardButton("😴 Sleep", callback_data="topic|sleep"),
         InlineKeyboardButton("🍎 Nutrition", callback_data="topic|nutrition")],
        [InlineKeyboardButton("🧪 Labs", callback_data="topic|labs"),
         InlineKeyboardButton("🔁 Habits", callback_data="topic|habits"),
         InlineKeyboardButton("🧬 Longevity", callback_data="topic|longevity")],
        [InlineKeyboardButton("👤 Profile", callback_data="topic|profile")],
        [InlineKeyboardButton(label, callback_data="intake:start")]
    ])

def inline_accept(lang: str) -> InlineKeyboardMarkup:
    labels = T[lang]["accept_opts"]
    return InlineKeyboardMarkup([[InlineKeyboardButton(labels[0], callback_data="acc|yes"),
                                  InlineKeyboardButton(labels[1], callback_data="acc|later"),
                                  InlineKeyboardButton(labels[2], callback_data="acc|no")]])

def inline_remind(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏰ +4h" if lang=="en" else T[lang]["act_rem_4h"], callback_data="rem|4h"),
         InlineKeyboardButton("⏰ This evening" if lang=="en" else T[lang]["act_rem_eve"], callback_data="rem|evening"),
         InlineKeyboardButton("⏰ Tomorrow morning" if lang=="en" else T[lang]["act_rem_morn"], callback_data="rem|morning")]
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

# === NEW: Main menu & submenus ===
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
        [InlineKeyboardButton("Neck pain 5-min" if lang=="en" else ("Шея 5 мин" if lang=="ru" else ("Шия 5 хв" if lang=="uk" else "Cuello 5 min")), callback_data="mini|neck")],
        [InlineKeyboardButton("Sleep reset (3 nights)" if lang=="en" else ("Сон-ресет (3 ночи)" if lang=="ru" else ("Сон-ресет (3 ночі)" if lang=="uk" else "Reinicio del sueño (3 noches)")), callback_data="mini|sleepreset")],
        [InlineKeyboardButton("Heartburn: 3 steps" if lang=="en" else ("Изжога: 3 шага" if lang=="ru" else ("Печія: 3 кроки" if lang=="uk" else "Acidez: 3 pasos")), callback_data="mini|heartburn")],
        [InlineKeyboardButton("Hydration on hot days" if lang=="en" else ("Гидратация в жару" if lang=="ru" else ("Гідратація в спеку" if lang=="uk" else "Hidratación en calor")), callback_data="mini|hydration")],
        [InlineKeyboardButton(T[lang]["back"], callback_data="menu|root")]
    ])

def inline_findcare_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Labs near me" if lang=="en" else ("Лабы рядом" if lang=="ru" else ("Лаби поруч" if lang=="uk" else "Laboratorios cerca")), callback_data="care|labsnear")],
        [InlineKeyboardButton("Urgent care" if lang=="en" else ("Неотложка" if lang=="ru" else ("Невідкладна" if lang=="uk" else "Urgencias")), callback_data="care|urgent")],
        [InlineKeyboardButton("Free clinics (NJ)" if lang=="en" else ("Бесплатные клиники (NJ)" if lang=="ru" else ("Безкоштовні клініки (NJ)" if lang=="uk" else "Clínicas gratuitas (NJ)")), callback_data="care|free_nj")],
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

def inline_smart_checkin(lang: str) -> InlineKeyboardMarkup:
    lab = {"en":["I’m OK","Pain","Tired","Stressed","Heartburn","Other"],
           "ru":["Я ок","Боль","Устал","Стресс","Изжога","Другое"],
           "uk":["Все ок","Біль","Втома","Стрес","Печія","Інше"],
           "es":["Estoy bien","Dolor","Cansado","Estrés","Acidez","Otro"]}[lang]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lab[0], callback_data="smart|ok"),
         InlineKeyboardButton(lab[1], callback_data="smart|pain")],
        [InlineKeyboardButton(lab[2], callback_data="smart|tired"),
         InlineKeyboardButton(lab[3], callback_data="smart|stress")],
        [InlineKeyboardButton(lab[4], callback_data="smart|hb"),
         InlineKeyboardButton(lab[5], callback_data="smart|other")],
        [InlineKeyboardButton(T[lang]["back"], callback_data="menu|root")]
    ])

# ===== Контекстные чипы =====
def chips_for_text(text: str, lang: str) -> Optional[InlineKeyboardMarkup]:
    low = (text or "").lower()
    hb_kw = any(k in low for k in ["heartburn","burning after meals","изжог","жжёт","жжет","печія","кислота"])
    neck_kw = any(k in low for k in ["neck pain","neck","шея","затылок","ший"])
    if hb_kw:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Avoid triggers" if lang=="en" else "Избегать триггеры", callback_data="chip|hb|triggers")],
            [InlineKeyboardButton("OTC options", callback_data="chip|hb|otc")],
            [InlineKeyboardButton("When to see a doctor" if lang=="en" else "Когда к врачу", callback_data="chip|hb|red")]
        ])
    if neck_kw:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("5-min routine", callback_data="chip|neck|routine")],
            [InlineKeyboardButton("Heat/Ice tips" if lang=="en" else "Тепло/лёд", callback_data="chip|neck|heat")],
            [InlineKeyboardButton("Red flags", callback_data="chip|neck|red")]
        ])
    return None

# ===== Micro-plans =====
def microplan_text(key: str, lang: str) -> str:
    if key=="neck":
        return {"ru":"Шея 5 мин:\n1) Медленные наклоны вперёд/назад ×5\n2) Повороты в стороны ×5\n3) Полукруги подбородком ×5\n4) Растяжка трапеций 2×20с.",
                "uk":"Шия 5 хв:\n1) Нахили вперед/назад ×5\n2) Повороти в сторони ×5\n3) Півкола підборіддям ×5\n4) Розтяжка трапецій 2×20с.",
                "en":"Neck 5-min:\n1) Slow flex/extend ×5\n2) Rotations L/R ×5\n3) Chin semicircles ×5\n4) Upper-trap stretch 2×20s.",
                "es":"Cuello 5 min:\n1) Flex/ext lenta ×5\n2) Giros izq/der ×5\n3) Semicírculos con barbilla ×5\n4) Estiramiento trapecio sup. 2×20s."}[lang]
    if key=="sleepreset":
        return {"ru":"Сон-ресет (3 ночи):\nН1: экран-детокс 60м + отбой фикс.\nН2: 15м вне кровати при пробуждениях.\nН3: свет утром 10–15м, кофе до 14:00.",
                "uk":"Сон-ресет (3 ночі):\nН1: детокс екранів 60 хв + фіксований відбій.\nН2: 15 хв поза ліжком при пробудженнях.\nН3: світло вранці 10–15 хв, кава до 14:00.",
                "en":"Sleep reset (3 nights):\nN1: 60-min screen detox + fixed bedtime.\nN2: 15-min out of bed if awake.\nN3: AM light 10–15m; caffeine by 2pm.",
                "es":"Reinicio del sueño (3 noches):\nN1: 60 min sin pantallas + hora fija.\nN2: 15 min fuera de la cama si despiertas.\nN3: Luz AM 10–15m; café hasta 14:00."}[lang]
    if key=="heartburn":
        return {"ru":"Изжога — 3 шага:\n1) Порции меньше, не ложиться 3ч после еды.\n2) Триггеры: жирное, алкоголь, мята, шоколад, кофе — убрать.\n3) OTC: антацид по инструкции 2–3 дня.",
                "uk":"Печія — 3 кроки:\n1) Менші порції, не лягати 3 год після їжі.\n2) Тригери: жирне, алкоголь, м’ята, шоколад, кава — прибрати.\n3) OTC: антацид за інстр. 2–3 дні.",
                "en":"Heartburn — 3 steps:\n1) Smaller meals; avoid lying 3h after.\n2) Remove triggers: fatty foods, alcohol, mint, chocolate, coffee.\n3) OTC antacid 2–3 days as directed.",
                "es":"Acidez — 3 pasos:\n1) Comidas pequeñas; no recostarse 3h.\n2) Evitar: grasas, alcohol, menta, chocolate, café.\n3) Antiácido OTC 2–3 días según etiqueta."}[lang]
    if key=="hydration":
        return {"ru":"Гидратация в жару:\nВода 200–300 мл каждый час активности; соль/электролиты при длительной жаре; светлая одежда и тень.",
                "uk":"Гідратація в спеку:\nВода 200–300 мл щогодини активності; електроліти за тривалої спеки; світлий одяг і тінь.",
                "en":"Hot-day hydration:\n200–300 ml water each active hour; add electrolytes if prolonged heat; light clothing & shade.",
                "es":"Hidratación en calor:\n200–300 ml de agua por hora activa; electrolitos si el calor es prolongado; ropa clara y sombra."}[lang]
    return ""

# ===== Chips text =====
def chip_text(domain: str, kind: str, lang: str) -> str:
    if domain=="hb":
        if kind=="triggers":
            return {"ru":"Изжога — триггеры: жирное, острое, шоколад, кофе, цитрусы, мята, алкоголь. Последний приём пищи за 3 ч до сна.",
                    "uk":"Печія — тригери: жирне, гостре, шоколад, кава, цитрусові, м’ята, алкоголь. Останній прийом за 3 год до сну.",
                    "en":"Heartburn triggers: fatty/spicy foods, chocolate, coffee, citrus, mint, alcohol. Last meal ≥3h before bed.",
                    "es":"Desencadenantes: grasa/picante, chocolate, café, cítricos, menta, alcohol. Última comida ≥3h antes de dormir."}[lang]
        if kind=="otc":
            return {"ru":"OTC варианты при изжоге: антацид (альгиновая кислота/карбонаты), кратко 2–3 дня. Если часто повторяется — обсудить с врачом.",
                    "uk":"OTC варіанти: антацид (альгінати/карбонати) на 2–3 дні. Якщо часто — до лікаря.",
                    "en":"OTC: antacid (alginates/carbonates) for 2–3 days. If frequent — discuss with a clinician.",
                    "es":"OTC: antiácido (alginatos/carbonatos) 2–3 días. Si es frecuente, consulta médica."}[lang]
        if kind=="red":
            return {"ru":"Когда к врачу при изжоге: дисфагия, рвота кровью, чёрный стул, потеря веса, ночные боли, >2–3 нед несмотря на меры.",
                    "uk":"Коли до лікаря: дисфагія, блювання кровʼю, чорний стілець, втрата ваги, нічний біль, >2–3 тиж попри заходи.",
                    "en":"See a doctor if: trouble swallowing, vomiting blood, black stools, weight loss, nocturnal pain, >2–3 weeks despite measures.",
                    "es":"Acude al médico si: disfagia, vómito con sangre, heces negras, pérdida de peso, dolor nocturno, >2–3 semanas pese a medidas."}[lang]
    if domain=="neck":
        if kind=="routine":
            return microplan_text("neck", lang)
        if kind=="heat":
            return {"ru":"Шея: первые 48 ч лучше холод 10–15 мин ×2–3/д; затем тепло для расслабления; лёгкая растяжка без боли.",
                    "uk":"Шия: перші 48 год — холод 10–15 хв ×2–3/д; далі тепло; легка розтяжка без болю.",
                    "en":"Neck: first 48h prefer ice 10–15 min ×2–3/day, then heat for relaxation; gentle stretch without pain.",
                    "es":"Cuello: primeras 48h hielo 10–15 min ×2–3/día, luego calor; estiramientos suaves sin dolor."}[lang]
        if kind=="red":
            return {"ru":"Красные флаги: слабость рук, онемение, травма, лихорадка, боль >7/10, быстро прогрессирует — к врачу/неотложке.",
                    "uk":"Червоні прапори: слабкість рук, оніміння, травма, гарячка, біль >7/10, прогресує — до лікаря/невідкладної.",
                    "en":"Red flags: arm weakness/numbness, trauma, fever, pain >7/10, rapid progression — seek care.",
                    "es":"Banderas rojas: debilidad/entumecimiento en brazos, trauma, fiebre, dolor >7/10, progresión rápida — atención médica."}[lang]
    return ""

# ===== Find care links =====
def care_links(kind: str, lang: str, city_hint: Optional[str]=None) -> str:
    if kind=="labsnear":
        q = "labs near me" if lang=="en" else ("лаборатории рядом" if lang=="ru" else ("лабораторії поруч" if lang=="uk" else "laboratorios cerca"))
        return f"🔗 Google Maps: https://www.google.com/maps/search/{q.replace(' ','+')}"
    if kind=="urgent":
        q = "urgent care near me" if lang=="en" else ("неотложка рядом" if lang=="ru" else ("невідкладна поруч" if lang=="uk" else "urgencias cerca"))
        return f"🔗 Google Maps: https://www.google.com/maps/search/{q.replace(' ','+')}"
    if kind=="free_nj":
        return "🔗 Free clinics NJ: https://www.google.com/maps/search/free+clinic+New+Jersey"
    return ""

# ===== Youth-пакет: команды =====
async def cmd_energy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    tips = {
      "en": [
        "1) 10-min brisk walk now (raise pulse).",
        "2) 300–500 ml water + light protein.",
        "3) 20-min screen detox to refresh focus."
      ],
      "ru": [
        "1) Быстрая ходьба 10 мин (пульс чуть выше обычного).",
        "2) 300–500 мл воды + лёгкий белок.",
        "3) 20 мин без экрана — разгрузка внимания."
      ],
      "uk": [
        "1) Швидка ходьба 10 хв (пульс трохи вище).",
        "2) 300–500 мл води + легкий білок.",
        "3) 20 хв без екрана — перезавантаження уваги."
      ],
      "es": [
        "1) Camina rápido 10 min.",
        "2) 300–500 ml de agua + proteína ligera.",
        "3) 20 min sin pantallas."
      ]
    }[lang]
    await update.message.reply_text(T[lang]["energy_title"] + "\n" + "\n".join(tips), reply_markup=inline_actions(lang))

async def cmd_water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏰ +4h" if lang=="en" else T[lang]["act_rem_4h"], callback_data="act|rem|4h")]])
    await update.message.reply_text(T[lang]["water_prompt"], reply_markup=kb)

async def cmd_mood(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T[lang]["mood_good"], callback_data="mood|good"),
         InlineKeyboardButton(T[lang]["mood_ok"],   callback_data="mood|ok"),
         InlineKeyboardButton(T[lang]["mood_bad"],  callback_data="mood|bad")],
        [InlineKeyboardButton(T[lang]["mood_note"], callback_data="mood|note")]
    ])
    await update.message.reply_text(T[lang]["daily_gm"], reply_markup=kb)

async def cmd_skin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    tip = {
        "ru":"Умывание 2×/день тёплой водой, SPF утром, 1% ниацинамид вечером.",
        "en":"Wash face 2×/day with lukewarm water, SPF in the morning, 1% niacinamide at night.",
        "uk":"Вмивання 2×/день теплою водою, SPF вранці, 1% ніацинамід ввечері.",
        "es":"Lava el rostro 2×/día con agua tibia, SPF por la mañana, 1% niacinamida por la noche."
    }[lang]
    await update.message.reply_text(T[lang]["skin_title"] + "\n" + tip, reply_markup=inline_actions(lang))

# ===== Pain triage вспомогательные =====
def _kb_for_code(lang: str, code: str):
    if code == "painloc":
        kb = inline_list(T[lang]["triage_pain_q1_opts"], "painloc")
    elif code == "painkind":
        kb = inline_list(T[lang]["triage_pain_q2_opts"], "painkind")
    elif code == "paindur":
        kb = inline_list(T[lang]["triage_pain_q3_opts"], "paindur")
    elif code == "num":
        kb = inline_numbers_0_10()
    elif code == "painrf":
        kb = inline_list(T[lang]["triage_pain_q5_opts"], "painrf")
    else:
        kb = None
    if kb:
        rows = kb.inline_keyboard + [[InlineKeyboardButton(T[lang]["back"], callback_data="pain|exit")]]
        return InlineKeyboardMarkup(rows)
    return None

# ====== Health60 =====
async def cmd_health60(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None))
    sessions.setdefault(uid, {})["awaiting_h60"] = True
    await update.message.reply_text(T[lang]["h60_intro"])

# ===== /intake кнопка =====
async def cmd_intake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None) or "en")
    txt  = {"ru":"🧩 PRO-опрос: 6 ключевых вопросов. Готовы начать?",
            "uk":"🧩 PRO-опитник: 6 ключових питань. Починаємо?",
            "en":"🧩 PRO intake: 6 quick questions. Ready?",
            "es":"🧩 PRO intake: 6 preguntas rápidas. ¿Listo?"}[lang]
    start_label = {"ru":"▶️ Начать","uk":"▶️ Почати","en":"▶️ Start","es":"▶️ Empezar"}[lang]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(start_label, callback_data="intake:start")]])
    await update.message.reply_text(txt, reply_markup=kb)

# ===== Profile (10 шагов) =====
PROFILE_STEPS = [
    {"key":"sex","opts":{"ru":[("Мужчина","male"),("Женщина","female"),("Другое","other")],
                         "en":[("Male","male"),("Female","female"),("Other","other")],
                         "uk":[("Чоловіча","male"),("Жіноча","female"),("Інша","other")],
                         "es":[("Hombre","male"),("Mujer","female"),("Otro","other")]}},
    {"key":"age","opts":{"ru":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
                         "en":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
                         "uk":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
                         "es":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")]}},
    {"key":"height_cm","opts":{"ru":[("160","160"),("170","170"),("180","180")],
                               "en":[("160","160"),("170","170"),("180","180")],
                               "uk":[("160","160"),("170","170"),("180","180")],
                               "es":[("160","160"),("170","170"),("180","180")]}},
    {"key":"weight_kg","opts":{"ru":[("60","60"),("75","75"),("90","90")],
                               "en":[("60","60"),("75","75"),("90","90")],
                               "uk":[("60","60"),("75","75"),("90","90")],
                               "es":[("60","60"),("75","75"),("90","90")]}},
    {"key":"goal","opts":{"ru":[("Похудение","weight"),("Энергия","energy"),("Сон","sleep"),("Долголетие","longevity"),("Сила","strength")],
                          "en":[("Weight","weight"),("Energy","energy"),("Sleep","sleep"),("Longevity","longevity"),("Strength","strength")],
                          "uk":[("Вага","weight"),("Енергія","energy"),("Сон","sleep"),("Довголіття","longevity"),("Сила","strength")],
                          "es":[("Peso","weight"),("Energía","energy"),("Sueño","sleep"),("Longevidad","longevity"),("Fuerza","strength")]}},
    {"key":"conditions","opts":{"ru":[("Нет","none"),("Гипертония","hypertension"),("Диабет","diabetes"),("Щитовидка","thyroid"),("Другое","other")],
                               "en":[("None","none"),("Hypertension","hypertension"),("Diabetes","diabetes"),("Thyroid","thyroid"),("Other","other")],
                               "uk":[("Немає","none"),("Гіпертонія","hypertension"),("Діабет","diabetes"),("Щитоподібна","thyroid"),("Інше","other")],
                               "es":[("Ninguna","none"),("Hipertensión","hypertension"),("Diabetes","diabetes"),("Tiroides","thyroid"),("Otra","other")]}},
    {"key":"meds","opts":{"ru":[("Нет","none"),("Магний","magnesium"),("Витамин D","vitd"),("Аллергии есть","allergies"),("Другое","other")],
                          "en":[("None","none"),("Magnesium","magnesium"),("Vitamin D","vitd"),("Allergies","allergies"),("Other","other")],
                          "uk":[("Немає","none"),("Магній","magnesium"),("Вітамін D","vitd"),("Алергії","allergies"),("Інше","other")],
                          "es":[("Ninguno","none"),("Magnesio","magnesium"),("Vitamina D","vitd"),("Alergias","allergies"),("Otro","other")]}},
    {"key":"supplements","opts":{"ru":[("Нет","none"),("Омега-3","omega3"),("Креатин","creatine"),("Протеин","protein"),("Другое","other")],
                                "en":[("None","none"),("Omega-3","omega3"),("Creatine","creatine"),("Protein","protein"),("Other","other")],
                                "uk":[("Немає","none"),("Омега-3","omega3"),("Креатин","creatine"),("Протеїн","protein"),("Інше","other")],
                                "es":[("Ninguno","none"),("Omega-3","omega3"),("Creatina","creatine"),("Proteína","protein"),("Otro","other")]}},
    {"key":"sleep","opts":{"ru":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
                           "en":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Irregular","irregular")],
                           "uk":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
                           "es":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Irregular","irregular")]}},
    {"key":"activity","opts":{"ru":[("<5к шагов","<5k"),("5–8к","5-8k"),("8–12к","8-12k"),("Спорт регулярно","sport")],
                             "en":[("<5k steps","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Regular sport","sport")],
                             "uk":[("<5к кроків","<5k"),("5–8к","5-8k"),("8–12к","8-12k"),("Спорт регулярно","sport")],
                             "es":[("<5k pasos","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Deporte regular","sport")]}}
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

async def start_profile_ctx(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: int):
    sessions[uid] = {"profile_active": True, "p_step": 0, "p_wait_key": None}
    await context.bot.send_message(chat_id, T[lang]["profile_intro"], reply_markup=ReplyKeyboardRemove())
    step = PROFILE_STEPS[0]
    kb = build_profile_kb(lang, step["key"], step["opts"][lang])
    await context.bot.send_message(chat_id, T[lang]["p_step_1"], reply_markup=kb)

async def advance_profile_ctx(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str, uid: int):
    s = sessions.get(uid, {})
    s["p_step"] += 1
    if s["p_step"] < len(PROFILE_STEPS):
        idx = s["p_step"]; step = PROFILE_STEPS[idx]
        kb = build_profile_kb(lang, step["key"], step["opts"][lang])
        key_to_idx = {"sex":1,"age":2,"height_cm":3,"weight_kg":4,"goal":5,"conditions":6,"meds":7,"supplements":8,"sleep":9,"activity":10}
        num = key_to_idx.get(step["key"], idx+1)
        await context.bot.send_message(chat_id, T[lang][f"p_step_{num}"], reply_markup=kb)
        return
    # финал
    prof = profiles_get(uid); summary=[]
    for k in ["sex","age","height_cm","weight_kg","goal","conditions","meds","supplements","sleep","activity","diet"]:
        v = prof.get(k) or sessions.get(uid,{}).get(k,"")
        if v: summary.append(f"{k}: {v}")
    profiles_upsert(uid, {})
    sessions[uid]["profile_active"] = False
    users_set(uid, "profile_banner_shown", "no")
    await context.bot.send_message(chat_id, T[lang]["saved_profile"] + "; ".join(summary))
    await context.bot.send_message(chat_id, T[lang]["start_where"], reply_markup=inline_topic_kb(lang))

# ===== РЕНДЕР ГЛАВНОГО МЕНЮ =====
async def render_main_menu(update_or_cb: Update, context: ContextTypes.DEFAULT_TYPE):
    if update_or_cb.callback_query:
        chat_id = update_or_cb.callback_query.message.chat.id
        uid = update_or_cb.callback_query.from_user.id
    else:
        chat_id = update_or_cb.effective_chat.id
        uid = update_or_cb.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update_or_cb.effective_user, "language_code", None) or "en")
    await context.bot.send_message(chat_id, f"{T[lang]['m_menu_title']}", reply_markup=inline_main_menu(lang))

# ===== Основной текстовый обработчик =====
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    text = (update.message.text or "").strip()
    logging.info(f"INCOMING uid={uid} text={text[:200]}")
    urec = users_get(uid)

    # новый пользователь: сохраняем, приветствие, меню, согласие и GATE (опрос)
    if not urec:
        lang_guess = detect_lang_from_text(text, norm_lang(getattr(user, "language_code", None)))
        users_upsert(uid, user.username or "", lang_guess)
        sessions.setdefault(uid, {})["last_user_text"] = text
        await update.message.reply_text(T[lang_guess]["welcome"], reply_markup=ReplyKeyboardRemove())
        # NEW: сразу главное меню
        await update.message.reply_text(T[lang_guess]["m_menu_title"], reply_markup=inline_main_menu(lang_guess))
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang_guess]["yes"], callback_data="consent|yes"),
                                    InlineKeyboardButton(T[lang_guess]["no"],  callback_data="consent|no")]])
        await update.message.reply_text(T[lang_guess]["ask_consent"], reply_markup=kb)
        if _has_jq_ctx(context):
            schedule_daily_checkin(context.application, uid, 0, DEFAULT_CHECKIN_LOCAL, lang_guess)
            schedule_morning_evening(context.application, uid, 0, lang_guess)
        context.user_data["lang"] = lang_guess
        await gate_show(update, context)
        return

    saved_lang = norm_lang(urec.get("lang") or getattr(user,"language_code",None))
    detected_lang = detect_lang_from_text(text, saved_lang)
    if detected_lang != saved_lang:
        users_set(uid,"lang",detected_lang)
    lang = detected_lang
    sessions.setdefault(uid, {})["last_user_text"] = text

    sc = detect_serious(text)
    if sc:
        sessions.setdefault(uid,{})["mode"] = "serious"
        sessions[uid]["serious_condition"] = sc
        prof = profiles_get(uid)
        plan = pain_plan(lang, [], prof)
        await update.message.reply_text("\n".join(plan), reply_markup=inline_actions(lang))
        ask_feedback_soft(uid, context, lang)
        return

    if sessions.get(uid, {}).get("awaiting_daily_comment"):
        daily_add(iso(utcnow()), uid, "note", text)
        sessions[uid]["awaiting_daily_comment"] = False
        await update.message.reply_text(T[lang]["mood_thanks"]); return

    if sessions.get(uid, {}).get("awaiting_free_feedback"):
        sessions[uid]["awaiting_free_feedback"] = False
        feedback_add(iso(utcnow()), uid, "free", user.username, "", text)
        await update.message.reply_text(T[lang]["fb_thanks"]); return

    if sessions.get(uid, {}).get("awaiting_city"):
        sessions[uid]["awaiting_city"] = False
        await update.message.reply_text(T[lang]["thanks"]); return

    # HABITS: ожидание веса
    if sessions.get(uid, {}).get("awaiting_weight"):
        m = re.search(r'\d{1,3}(?:[.,]\d{1,1})?', text.replace(",", "."))
        sessions[uid]["awaiting_weight"] = False
        if m:
            val = m.group(0)
            st = habits_add(uid, "weight", val, "kg")
            await update.message.reply_text(("Logged weight: " if lang=="en" else ("Вес записан: " if lang=="ru" else ("Вага записана: " if lang=="uk" else "Peso registrado: ")) ) + f"{val} kg\nStreak: {st}", reply_markup=inline_main_menu(lang))
        else:
            await update.message.reply_text("Please send a number like 72.5" if lang=="en" else ("Пришлите число, например 72.5" if lang=="ru" else ("Надішліть число, напр. 72.5" if lang=="uk" else "Envía un número como 72.5")), reply_markup=inline_main_menu(lang))
        return

    if sessions.get(uid, {}).get("awaiting_h60"):
        sessions[uid]["awaiting_h60"] = False
        prof = profiles_get(uid)
        low = text.lower()
        if any(word in low for word in ["белок","protein","больше белка","↑белок"]):
            if lang=="ru":
                msg = "Под тебя подойдёт сегодня:\n• Творог 200 г + огурец\n• Омлет 2 яйца + овощи\n• Сардины 1 банка + салат\nВыбери вариант — подстрою дальше."
            elif lang=="uk":
                msg = "На сьогодні підійде:\n• Сир 200 г + огірок\n• Омлет 2 яйця + овочі\n• Сардини 1 банка + салат\nОбери варіант — підлаштую далі."
            elif lang=="es":
                msg = "Para hoy te sirve:\n• Requesón 200 g + pepino\n• Tortilla 2 huevos + verduras\n• Sardinas (1 lata) + ensalada\nElige una opción — ajusto lo siguiente."
            else:
                msg = "Good picks for today:\n• Cottage cheese 200 g + cucumber\n• 2-egg omelet + veggies\n• Sardines (1 can) + salad\nPick one — I’ll tailor next."
            await update.message.reply_text(msg, reply_markup=inline_actions(lang))
        else:
            await update.message.reply_text(T[lang]["unknown"], reply_markup=inline_actions(lang))
        # Показ чипов по контексту
        chips = chips_for_text(text, lang)
        if chips:
            await update.message.reply_text(T[lang]["chips_hb"] if "hb" in str(chips.inline_keyboard[0][0].callback_data) else T[lang]["chips_neck"], reply_markup=chips)
        ask_feedback_soft(uid, context, lang)
        return

    if sessions.get(uid, {}).get("p_wait_key"):
        key = sessions[uid]["p_wait_key"]; sessions[uid]["p_wait_key"] = None
        val = text
        if key in {"age","height_cm","weight_kg"}:
            m = re.search(r'\d{1,3}', text)
            if m: val = m.group(0)
        profiles_upsert(uid,{key:val}); sessions[uid][key]=val
        users_set(uid, "profile_banner_shown", "no")
        await advance_profile_ctx(context, update.effective_chat.id, lang, uid); return

    s = sessions.get(uid, {})
    if s.get("topic") == "pain":
        if re.search(r"\b(stop|exit|back|назад|выход|выйти)\b", text.lower()):
            sessions.pop(uid, None)
            await update.message.reply_text(T[lang]["m_menu_title"], reply_markup=inline_main_menu(lang))
            return
        if s.get("step") == 1:
            await send_unique(update.message, uid, T[lang]["triage_pain_q2"], reply_markup=_kb_for_code(lang, "painkind")); return
        if s.get("step") == 2:
            await send_unique(update.message, uid, T[lang]["triage_pain_q3"], reply_markup=_kb_for_code(lang, "paindur")); return
        if s.get("step") == 3:
            await update.message.reply_text(T[lang]["triage_pain_q4"], reply_markup=_kb_for_code(lang, "num")); return
        if s.get("step") == 4:
            m = re.fullmatch(r"(?:10|[0-9])", text)
            if m:
                sev = int(m.group(0)); s.setdefault("answers", {})["severity"] = sev; s["step"] = 5
                await update.message.reply_text(T[lang]["triage_pain_q5"], reply_markup=_kb_for_code(lang, "painrf")); return
            await update.message.reply_text(T[lang]["triage_pain_q4"], reply_markup=_kb_for_code(lang, "num")); return

    if should_show_profile_banner(uid):
        prof = profiles_get(uid)
        banner = profile_banner(lang, prof)
        if banner.strip().strip("—"):
            await update.message.reply_text(banner)
        users_set(uid, "profile_banner_shown", "yes")

    prof = profiles_get(uid)
    data = llm_router_answer(text, lang, prof)

    msg = apply_warm_tone(data.get("assistant_reply") or T[lang]["unknown"], lang)
    await update.message.reply_text(msg, reply_markup=inline_actions(lang))
    # Контекстные чипы
    chips = chips_for_text(text, lang)
    if chips:
        await update.message.reply_text(T[lang]["chips_hb"] if "hb" in str(chips.inline_keyboard[0][0].callback_data) else T[lang]["chips_neck"], reply_markup=chips)
    ask_feedback_soft(uid, context, lang)
    for one in (data.get("followups") or [])[:2]:
        await send_unique(update.message, uid, apply_warm_tone(one, lang), force=True)
    return

# ===== Callback handler (см. Часть 2) =====
# on_callback будет определён во второй части файла.

# ---------- Build & run ----------
async def post_init(app):
    me = await app.bot.get_me()
    logging.info(f"BOT READY: @{me.username} (id={me.id})")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = norm_lang(getattr(user, "language_code", None))
    users_upsert(user.id, user.username or "", lang)
    context.user_data["lang"] = lang
    sessions.setdefault(user.id, {})["last_user_text"] = "/start"
    await update.message.reply_text(T[lang]["welcome"], reply_markup=ReplyKeyboardRemove())
    prof = profiles_get(user.id)
    if prof and should_show_profile_banner(user.id):
        await update.message.reply_text(profile_banner(lang, prof))
        users_set(user.id, "profile_banner_shown", "yes")
    # NEW: главное меню
    await update.message.reply_text(T[lang]["m_menu_title"], reply_markup=inline_main_menu(lang))
    if not profiles_get(user.id) and not context.user_data.get(GATE_FLAG_KEY):
        await gate_show(update, context)
    u = users_get(user.id)
    if (u.get("consent") or "").lower() not in {"yes","no"}:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(T[lang]["yes"], callback_data="consent|yes"),
                                    InlineKeyboardButton(T[lang]["no"],  callback_data="consent|no")]])
        await update.message.reply_text(T[lang]["ask_consent"], reply_markup=kb)
    tz_off = int(str(u.get("tz_offset") or "0"))
    hhmm = (u.get("checkin_hour") or DEFAULT_CHECKIN_LOCAL)
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, user.id, tz_off, hhmm, lang)  # утро
        schedule_morning_evening(context.application, user.id, tz_off, lang)     # вечер
    else:
        logging.warning("JobQueue not available on /start – daily check-ins not scheduled.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await update.message.reply_text(T[lang]["help"])

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await update.message.reply_text(T[lang]["privacy"])

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await render_main_menu(update, context)

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "yes")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text(T[lang]["paused_on"])

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid, "paused", "no")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text(T[lang]["paused_off"])

# *** ОБНОВЛЁННЫЙ /delete_data: чистим все листы и снимаем джобы
async def cmd_delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if SHEETS_ENABLED:
        def _delete_where(ws, col_name, value):
            vals = ws.get_all_values()
            if not vals: return
            hdr = vals[0]
            try:
                col = hdr.index(col_name) + 1
            except ValueError:
                return
            rows = []
            for i in range(2, len(vals) + 1):
                try:
                    if ws.cell(i, col).value == str(value):
                        rows.append(i)
                except Exception:
                    continue
            for i in reversed(rows):
                ws.delete_rows(i)

        _delete_where(ws_users,    "user_id", uid)
        _delete_where(ws_profiles, "user_id", uid)
        _delete_where(ws_episodes, "user_id", uid)
        _delete_where(ws_reminders,"user_id", uid)
        _delete_where(ws_daily,    "user_id", uid)
        _delete_where(ws_feedback, "user_id", uid)
        _delete_where(ws_habits,   "user_id", uid)
    else:
        MEM_USERS.pop(uid, None)
        MEM_PROFILES.pop(uid, None)
        global MEM_EPISODES, MEM_REMINDERS, MEM_DAILY, MEM_FEEDBACK, MEM_HABITS
        MEM_EPISODES  = [r for r in MEM_EPISODES  if r["user_id"] != str(uid)]
        MEM_REMINDERS = [r for r in MEM_REMINDERS if r["user_id"] != str(uid)]
        MEM_DAILY     = [r for r in MEM_DAILY     if r["user_id"] != str(uid)]
        MEM_FEEDBACK  = [r for r in MEM_FEEDBACK  if r["user_id"] != str(uid)]
        MEM_HABITS    = [r for r in MEM_HABITS    if r["user_id"] != str(uid)]

    # снимаем расписанные ежедневные задачи
    if _has_jq_ctx(context):
        for name in [f"daily_{uid}", f"daily_e_{uid}"]:
            for j in context.application.job_queue.get_jobs_by_name(name):
                j.schedule_removal()

    lang = norm_lang(getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(T[lang]["deleted"], reply_markup=ReplyKeyboardRemove())

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user, "language_code", None))
    await start_profile_ctx(context, update.effective_chat.id, lang, uid)

# *** ОБНОВЛЁННЫЙ /settz: клиппинг диапазона −12…+14
async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")
    parts = (update.message.text or "").split()
    if len(parts)<2 or not re.fullmatch(r"[+-]?\d{1,2}", parts[1]):
        await update.message.reply_text({"ru":"Формат: /settz +3","uk":"Формат: /settz +2",
                                         "en":"Usage: /settz +3","es":"Uso: /settz +3"}[lang]); return
    off = int(parts[1])
    off = max(-12, min(14, off))  # клиппим смещение
    users_set(uid, "tz_offset", str(off))
    hhmm = users_get(uid).get("checkin_hour") or DEFAULT_CHECKIN_LOCAL
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, uid, off, hhmm, lang)  # утро
        schedule_morning_evening(context.application, uid, off, lang)      # вечер
    await update.message.reply_text({"ru":f"Сдвиг часового пояса: {off}ч",
                                     "uk":f"Зсув: {off} год",
                                     "en":f"Timezone offset: {off}h",
                                     "es":f"Desfase horario: {off}h"}[lang])

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
    if _has_jq_ctx(context):
        schedule_daily_checkin(context.application, uid, tz_off, hhmm, lang)  # утро
        schedule_morning_evening(context.application, uid, tz_off, lang)      # вечер
    else:
        logging.warning("JobQueue not available – daily check-in not scheduled.")
    await update.message.reply_text({"ru":f"Ежедневный чек-ин включён ({hhmm}).",
                                     "uk":f"Щоденний чек-ін увімкнено ({hhmm}).",
                                     "en":f"Daily check-in enabled ({hhmm}).",
                                     "es":f"Check-in diario activado ({hhmm})."}[lang])

async def cmd_checkin_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if _has_jq_ctx(context):
        for name in [f"daily_{uid}", f"daily_e_{uid}"]:
            for j in context.application.job_queue.get_jobs_by_name(name):
                j.schedule_removal()
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text({"ru":"Ежедневный чек-ин выключен.",
                                     "uk":"Щоденний чек-ін вимкнено.",
                                     "en":"Daily check-in disabled.",
                                     "es":"Check-in diario desactivado."}[lang])

def build_app() -> "Application":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    # безопасно подключаем PRO-опросник
    try:
        register_intake_pro(app, GSPREAD_CLIENT, on_complete_cb=_ipro_save_to_sheets_and_open_menu)
        logging.info("Intake Pro registered.")
    except Exception as e:
        logging.warning(f"Intake Pro registration failed: {e}")
    # Commands
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("privacy",      cmd_privacy))
    app.add_handler(CommandHandler("menu",         cmd_menu))   # NEW
    app.add_handler(CommandHandler("pause",        cmd_pause))
    app.add_handler(CommandHandler("resume",       cmd_resume))
    app.add_handler(CommandHandler("delete_data",  cmd_delete_data))
    app.add_handler(CommandHandler("profile",      cmd_profile))
    app.add_handler(CommandHandler("settz",        cmd_settz))
    app.add_handler(CommandHandler("checkin_on",   cmd_checkin_on))
    app.add_handler(CommandHandler("checkin_off",  cmd_checkin_off))
    app.add_handler(CommandHandler("health60",     cmd_health60))
    app.add_handler(CommandHandler("intake",       cmd_intake))
    # Youth
    app.add_handler(CommandHandler("energy",       cmd_energy))
    app.add_handler(CommandHandler("mood",         cmd_mood))
    app.add_handler(CommandHandler("water",        cmd_water))
    app.add_handler(CommandHandler("skin",         cmd_skin))
    # Lang toggles
    app.add_handler(CommandHandler("ru", lambda u,c: users_set(u.effective_user.id,"lang","ru") or u.message.reply_text("Ок, дальше отвечаю по-русски.")))
    app.add_handler(CommandHandler("en", lambda u,c: users_set(u.effective_user.id,"lang","en")  or u.message.reply_text("OK, I’ll reply in English.")))
    app.add_handler(CommandHandler("uk", lambda u,c: users_set(u.effective_user.id,"lang","uk")  or u.message.reply_text("Ок, надалі відповідатиму українською.")))
    app.add_handler(CommandHandler("es", lambda u,c: users_set(u.effective_user.id,"lang","es")  or u.message.reply_text("De acuerdo, responderé en español.")))
    # Gate & callbacks
    app.add_handler(CallbackQueryHandler(gate_cb, pattern=r"^gate:"))
    # Основной callback-роутер будет добавлен ниже (после определения on_callback в Части 2)
    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # проброс рендера меню для GATE
    app.bot_data["render_menu_cb"] = render_main_menu
    return app
# =========================
# ======== PART 2 =========
# =========================

# ---------- Helpers: time parsing for reminders ----------
def _clip_to_quiet_or_next(uid: int, target_local: datetime) -> datetime:
    """If target falls in quiet hours — move to the end of quiet interval. Returns UTC datetime."""
    u = users_get(uid)
    tz_off = int(str(u.get("tz_offset") or "0"))
    q = (u.get("quiet_hours") or "").strip()
    if not q:
        return (target_local - timedelta(hours=tz_off)).astimezone(timezone.utc)
    m = re.match(r'(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})', q)
    if not m:
        return (target_local - timedelta(hours=tz_off)).astimezone(timezone.utc)
    q_start = target_local.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    q_end   = target_local.replace(hour=int(m.group(3)), minute=int(m.group(4)), second=0, microsecond=0)
    in_quiet = False
    if q_end <= q_start:
        in_quiet = target_local >= q_start or target_local <= q_end
    else:
        in_quiet = q_start <= target_local <= q_end
    if in_quiet:
        # move to quiet end today if not past, else tomorrow quiet_end
        if q_end <= target_local and q_end > q_start:
            target_local = q_end + timedelta(days=1)
        elif q_end <= target_local and q_end <= q_start:
            target_local = q_end  # already next day semantics for overnight
        else:
            target_local = q_end
    return (target_local - timedelta(hours=tz_off)).astimezone(timezone.utc)

def parse_when(uid: int, choice: str) -> datetime:
    """choice: '4h' | 'evening' | 'morning' -> UTC datetime with tz_offset + quiet hours respected."""
    u = users_get(uid)
    tz_off = int(str(u.get("tz_offset") or "0"))
    now_local = (utcnow() + timedelta(hours=tz_off)).replace(tzinfo=None)
    choice = (choice or "").lower()
    if choice == "4h":
        target = now_local + timedelta(hours=4)
        return _clip_to_quiet_or_next(uid, target)
    if choice == "evening":
        h, m = hhmm_tuple("20:30")
        t = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if t <= now_local:
            t = t + timedelta(days=1)
        return _clip_to_quiet_or_next(uid, t)
    if choice == "morning":
        hhmm = users_get(uid).get("checkin_hour") or DEFAULT_CHECKIN_LOCAL
        h, m = hhmm_tuple(hhmm)
        t = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if t <= now_local:
            t = t + timedelta(days=1)
        return _clip_to_quiet_or_next(uid, t)
    # fallback +4h
    return _clip_to_quiet_or_next(uid, now_local + timedelta(hours=4))

# ---------- Episode helpers ----------
def _episode_schedule_checkin(app, uid: int, eid: str, when_utc: datetime):
    """Set next_checkin_at in sheet/memory and schedule job."""
    episode_set(eid, "next_checkin_at", iso(when_utc))
    if _has_jq_app(app):
        # min 60 sec delay (already enforced in schedule_from_sheet_on_start)
        delay = max(60, (when_utc - utcnow()).total_seconds())
        app.job_queue.run_once(job_checkin_episode, when=delay, data={"user_id":uid,"episode_id":eid})

# ---------- Pain triage rendering ----------
def _pain_state(uid: int) -> dict:
    s = sessions.setdefault(uid, {})
    s.setdefault("topic", "pain")
    s.setdefault("step", 1)
    s.setdefault("answers", {})
    return s

async def _pain_start(chat_id: int, uid: int, lang: str, bot):
    s = _pain_state(uid)
    s["step"] = 1
    s["answers"] = {}
    await bot.send_message(chat_id, T[lang]["triage_pain_q1"], reply_markup=_kb_for_code(lang, "painloc"))

async def _pain_next(chat_id: int, uid: int, lang: str, bot):
    s = _pain_state(uid)
    step = s.get("step", 1)
    if step == 1:
        await bot.send_message(chat_id, T[lang]["triage_pain_q2"], reply_markup=_kb_for_code(lang, "painkind"))
    elif step == 2:
        await bot.send_message(chat_id, T[lang]["triage_pain_q3"], reply_markup=_kb_for_code(lang, "paindur"))
    elif step == 3:
        await bot.send_message(chat_id, T[lang]["triage_pain_q4"], reply_markup=_kb_for_code(lang, "num"))
    elif step == 4:
        await bot.send_message(chat_id, T[lang]["triage_pain_q5"], reply_markup=_kb_for_code(lang, "painrf"))
    else:
        # finalize
        prof = profiles_get(uid)
        plan = pain_plan(lang, s["answers"].get("red_flags", []), prof)
        kb = inline_accept(lang)
        await bot.send_message(chat_id, T[lang]["plan_header"] + "\n" + "\n".join(plan), reply_markup=kb)

# ---------- Profile helpers ----------
async def _profile_choose(update, context, uid: int, lang: str, key: str, val: str):
    profiles_upsert(uid, {key: val})
    sessions.setdefault(uid, {})[key] = val
    users_set(uid, "profile_banner_shown", "no")
    await advance_profile_ctx(context, update.effective_chat.id, lang, uid)

async def _profile_write(update, context, uid: int, lang: str, key: str):
    sessions.setdefault(uid, {})["p_wait_key"] = key
    prompts = {
        "sex":{"ru":"Напишите пол (male/female/other).","uk":"Напишіть стать (male/female/other).","en":"Type sex (male/female/other).","es":"Escribe sexo (male/female/other)."},
        "age":{"ru":"Напишите возраст числом.","uk":"Напишіть вік числом.","en":"Type age (number).","es":"Escribe edad (número)."},
        "height_cm":{"ru":"Рост в см (например 175).","uk":"Зріст у см (наприклад 175).","en":"Height in cm (e.g. 175).","es":"Altura en cm (ej. 175)."},
        "weight_kg":{"ru":"Вес в кг (например 72).","uk":"Вага у кг (наприклад 72).","en":"Weight in kg (e.g. 72).","es":"Peso en kg (ej. 72)."},
        "goal":{"ru":"Главная цель (weight/energy/sleep/longevity/strength).","uk":"Головна ціль (weight/energy/sleep/longevity/strength).","en":"Main goal (weight/energy/sleep/longevity/strength).","es":"Objetivo (weight/energy/sleep/longevity/strength)."},
        "conditions":{"ru":"Хронические болезни (списком).","uk":"Хронічні хвороби (списком).","en":"Chronic conditions (list).","es":"Condiciones crónicas (lista)."},
        "meds":{"ru":"Лекарства (списком).","uk":"Ліки (списком).","en":"Meds (list).","es":"Medicamentos (lista)."},
        "supplements":{"ru":"Добавки (списком).","uk":"Добавки (списком).","en":"Supplements (list).","es":"Suplementos (lista)."},
        "sleep":{"ru":"Сон: формат 23:30/07:00 или irregular.","uk":"Сон: формат 23:30/07:00 або irregular.","en":"Sleep: 23:30/07:00 or irregular.","es":"Sueño: 23:30/07:00 o irregular."},
        "activity":{"ru":"Активность: <5k / 5-8k / 8-12k / sport.","uk":"Активність: <5k / 5-8k / 8-12k / sport.","en":"Activity: <5k / 5-8k / 8-12k / sport.","es":"Actividad: <5k / 5-8k / 8-12k / sport."}
    }
    await update.callback_query.message.reply_text(prompts.get(key, {}).get(lang, "Type value:"))

async def _profile_skip(update, context, uid: int, lang: str, key: str):
    await advance_profile_ctx(context, update.effective_chat.id, lang, uid)

# ---------- Quick actions ----------
async def _schedule_oneoff(app, uid: int, rid: str, when_utc: datetime):
    if _has_jq_app(app):
        delay = max(60, (when_utc - utcnow()).total_seconds())
        app.job_queue.run_once(job_oneoff_reminder, when=delay, data={"user_id":uid,"reminder_id":rid})

# ---------- Main callback router ----------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    urec = users_get(uid) or {}
    lang = norm_lang(urec.get("lang") or getattr(q.from_user, "language_code", None) or "en")
    data = (q.data or "").strip()
    try:
        await q.answer()
    except Exception:
        pass

    # 1) Consent
    if data.startswith("consent|"):
        choice = data.split("|", 1)[1]
        users_set(uid, "consent", "yes" if choice == "yes" else "no")
        await q.message.reply_text(T[lang]["thanks"])
        return

    # 2) Gate intake start/skip
    if data == "intake:start":
        # If PRO plugin present — it handles its own flow (pattern "^intake:")
        if HAVE_INTAKE_PRO:
            # Hand off to intake_pro handlers via callback; here show lightweight confirm
            await q.message.reply_text({"ru":"Открываю PRO-опрос…","uk":"Відкриваю PRO-опитник…","en":"Opening PRO intake…","es":"Abriendo intake PRO…"}[lang])
        else:
            await start_profile_ctx(context, q.message.chat_id, lang, uid)
        return

    # 3) Menu navigation
    if data.startswith("menu|"):
        kind = data.split("|")[1]
        if kind == "root":
            await q.message.reply_text(T[lang]["m_menu_title"], reply_markup=inline_main_menu(lang))
        elif kind == "sym":
            await q.message.reply_text("➕", reply_markup=inline_symptoms_menu(lang))
        elif kind == "mini":
            await q.message.reply_text("Mini-plans", reply_markup=inline_miniplans_menu(lang))
        elif kind == "care":
            await q.message.reply_text("Find care", reply_markup=inline_findcare_menu(lang))
        elif kind == "hab":
            await q.message.reply_text("Habits Quick-log", reply_markup=inline_habits_menu(lang))
        elif kind == "rem":
            await q.message.reply_text(T[lang]["remind_when"], reply_markup=inline_remind(lang))
        elif kind == "lang":
            await q.message.reply_text(T[lang]["m_lang"], reply_markup=inline_lang_menu(lang))
        elif kind == "privacy":
            await q.message.reply_text(T[lang]["privacy"])
        elif kind == "smart":
            await q.message.reply_text("Smart check-in", reply_markup=inline_smart_checkin(lang))
        elif kind == "coming":
            await q.message.reply_text(T[lang]["m_soon"])
        return

    # 4) Symptoms shortcuts
    if data.startswith("sym|"):
        key = data.split("|")[1]
        if key == "headache":
            sessions.setdefault(uid, {})["topic"] = "pain"
            await _pain_start(q.message.chat_id, uid, lang, context.bot)
        elif key == "heartburn":
            await q.message.reply_text(chip_text("hb", "triggers", lang), reply_markup=inline_actions(lang))
        elif key == "fatigue":
            await q.message.reply_text(T[lang]["energy_title"] + "\n• 10–15 min walk\n• Water 300–500 ml\n• Early light", reply_markup=inline_actions(lang))
        elif key == "other":
            await q.message.reply_text(T[lang]["h60_intro"])
        return

    # 5) Pain triage path
    if data.startswith("pain|"):
        # exit/back
        if data.endswith("|exit") or data == "pain|exit":
            sessions.pop(uid, None)
            await q.message.reply_text(T[lang]["m_menu_title"], reply_markup=inline_main_menu(lang))
            return

    if data.startswith(("painloc|","painkind|","paindur|","num|","painrf|")):
        s = _pain_state(uid)
        if data.startswith("painloc|"):
            s["answers"]["loc"] = data.split("|",1)[1]
            s["step"] = 2
        elif data.startswith("painkind|"):
            s["answers"]["kind"] = data.split("|",1)[1]
            s["step"] = 3
        elif data.startswith("paindur|"):
            s["answers"]["dur"] = data.split("|",1)[1]
            s["step"] = 4
        elif data.startswith("num|"):
            try:
                sev = int(data.split("|",1)[1])
            except Exception:
                sev = 5
            s["answers"]["severity"] = sev
            s["step"] = 5
        elif data.startswith("painrf|"):
            rf = s["answers"].setdefault("red_flags", [])
            rf.append(data.split("|",1)[1])
            # allow multiple taps; show finalize button
            s["step"] = 6
        await _pain_next(q.message.chat_id, uid, lang, context.bot)
        return

    # accept plan or schedule reminders from plan screen
    if data.startswith("acc|"):
        choice = data.split("|",1)[1]
        s = sessions.get(uid, {})
        sev = int(s.get("answers", {}).get("severity", 5))
        eid = episode_create(uid, "pain", sev, ",".join(s.get("answers", {}).get("red_flags", [])))
        if choice == "yes":
            kb = inline_remind(lang)
            await q.message.reply_text(T[lang]["plan_accept"], reply_markup=kb)
        elif choice == "later":
            await q.message.reply_text(T[lang]["thanks"], reply_markup=inline_actions(lang))
        else:
            await q.message.reply_text(T[lang]["thanks"])
        sessions.setdefault(uid, {})["episode_id"] = eid
        return

    if data.startswith("rem|"):
        choice = data.split("|",1)[1]
        when_utc = parse_when(uid, choice)
        rid = reminder_add(uid, T[lang]["thanks"], when_utc)
        await q.message.reply_text(T[lang]["thanks"])
        await _schedule_oneoff(context.application, uid, rid, when_utc)
        # Link to episode check-in if exists
        eid = sessions.get(uid, {}).get("episode_id")
        if eid:
            # episode check-in in same slot
            _episode_schedule_checkin(context.application, uid, eid, when_utc)
        return

    # 6) Smart check-in
    if data.startswith("smart|"):
        tag = data.split("|",1)[1]
        daily_add(iso(utcnow()), uid, tag, "")
        # quick tailored nudge
        nudges = {
            "ok":{"ru":"👍 Хайпово!","uk":"👍 Клас!","en":"👍 Nice!","es":"👍 ¡Bien!"},
            "pain":{"ru":"Попробуем 400–600 мл воды и 10 мин тишины?","uk":"Спробуємо 400–600 мл води і 10 хв тиші?","en":"Try 400–600 ml water + 10 min quiet?","es":"¿Probamos 400–600 ml de agua + 10 min en calma?"},
            "tired":{"ru":"Короткая прогулка 10–12 мин помогает взбодриться.","uk":"Коротка прогулянка 10–12 хв підбадьорить.","en":"A brisk 10–12 min walk boosts energy.","es":"Una caminata de 10–12 min anima."},
            "stress":{"ru":"На 3 мин — длинный выдох и плечи вниз.","uk":"На 3 хв — довгий видих і плечі вниз.","en":"For 3 min — long exhale, relax shoulders.","es":"Durante 3 min — exhala largo, hombros abajo."},
            "hb":{"ru":"Изжога? Избегай лежать 3ч после еды, мелкие порции.","uk":"Печія? Не лягай 3 год після їжі, малі порції.","en":"Heartburn? Avoid lying 3h after meals, smaller portions.","es":"¿Acidez? Evita tumbarte 3h tras comer; porciones pequeñas."},
            "other":{"ru":"Опиши в 1 фразе — подскажу.","uk":"Опиши в 1 фразі — підкажу.","en":"Describe in 1 line — I’ll help.","es":"Describe en una línea — te ayudo."}
        }
        await q.message.reply_text(nudges.get(tag, {}).get(lang, T[lang]["thanks"]), reply_markup=inline_actions(lang))
        return

    # 7) Habits quick-log
    if data.startswith("hab|"):
        typ = data.split("|",1)[1]
        if typ == "water":
            st = habits_add(uid, "water", "300-500", "ml")
            await q.message.reply_text(("Logged water. Streak: " if lang=="en" else "Вода записана. Стик: ") + str(st), reply_markup=inline_actions(lang))
        elif typ == "steps":
            st = habits_add(uid, "steps", "done", "")
            await q.message.reply_text(("Steps logged. Streak: " if lang=="en" else "Шаги записаны. Стик: ") + str(st), reply_markup=inline_actions(lang))
        elif typ == "sleep":
            st = habits_add(uid, "sleep", "logged", "")
            await q.message.reply_text(("Sleep logged. Streak: " if lang=="en" else "Сон записан. Стик: ") + str(st), reply_markup=inline_actions(lang))
        elif typ == "stress":
            st = habits_add(uid, "stress", "breath", "")
            await q.message.reply_text(("Stress check logged. Streak: " if lang=="en" else "Стресс-лог записан. Стик: ") + str(st), reply_markup=inline_actions(lang))
        elif typ == "weight":
            sessions.setdefault(uid, {})["awaiting_weight"] = True
            await q.message.reply_text("Send weight in kg (e.g., 72.5)" if lang=="en" else "Пришлите вес в кг (например 72.5)")
        return

    # 8) Micro-plans
    if data.startswith("mini|"):
        key = data.split("|",1)[1]
        await q.message.reply_text(microplan_text(key, lang), reply_markup=inline_actions(lang))
        return

    # 9) Care links
    if data.startswith("care|"):
        key = data.split("|",1)[1]
        if key == "labsnear":
            sessions.setdefault(uid, {})["awaiting_city"] = True
            await q.message.reply_text(T[lang]["act_city_prompt"])
            await q.message.reply_text(care_links("labsnear", lang))
        else:
            await q.message.reply_text(care_links(key, lang))
        return

    # 10) Language switch
    if data.startswith("lang|"):
        new = data.split("|",1)[1]
        if new in SUPPORTED:
            users_set(uid, "lang", new)
            await q.message.reply_text({"ru":"Готово.","uk":"Готово.","en":"Done.","es":"Listo."}[new])
            await q.message.reply_text(T[new]["m_menu_title"], reply_markup=inline_main_menu(new))
        return

    # 11) Feedback
    if data.startswith("fb|"):
        kind = data.split("|",1)[1]
        if kind == "up":
            feedback_add(iso(utcnow()), uid, "inline", q.from_user.username, "up", "")
            await q.message.reply_text(T[lang]["fb_thanks"])
        elif kind == "down":
            feedback_add(iso(utcnow()), uid, "inline", q.from_user.username, "down", "")
            await q.message.reply_text(T[lang]["fb_thanks"])
        elif kind == "text":
            sessions.setdefault(uid, {})["awaiting_free_feedback"] = True
            await q.message.reply_text(T[lang]["fb_write"])
        return

    # 12) Context chips
    if data.startswith("chip|"):
        _, dom, kind = data.split("|", 2)
        await q.message.reply_text(chip_text(dom, kind, lang), reply_markup=inline_actions(lang))
        return

    # 13) Actions
    if data.startswith("act|"):
        parts = data.split("|")
        if parts[1] == "rem":
            choice = parts[2]
            when_utc = parse_when(uid, choice)
            rid = reminder_add(uid, T[lang]["thanks"], when_utc)
            await q.message.reply_text(T[lang]["thanks"])
            await _schedule_oneoff(context.application, uid, rid, when_utc)
            # chain with current episode if any
            eid = sessions.get(uid, {}).get("episode_id")
            if eid:
                _episode_schedule_checkin(context.application, uid, eid, when_utc)
        elif parts[1] == "h60":
            sessions.setdefault(uid, {})["awaiting_h60"] = True
            await q.message.reply_text(T[lang]["h60_intro"])
        elif parts[1] == "ex" and parts[2] == "neck":
            await q.message.reply_text(microplan_text("neck", lang))
        elif parts[1] == "lab":
            sessions.setdefault(uid, {})["awaiting_city"] = True
            await q.message.reply_text(T[lang]["act_city_prompt"])
            await q.message.reply_text(care_links("labsnear", lang))
        elif parts[1] == "er":
            await q.message.reply_text(T[lang]["er_text"])
        return

    # 14) Profile step engine (p|choose|key|val / p|write|key / p|skip|key)
    if data.startswith("p|"):
        parts = data.split("|")
        action = parts[1]
        key = parts[2] if len(parts) > 2 else ""
        if action == "choose" and len(parts) >= 4:
            val = parts[3]
            await _profile_choose(update, context, uid, lang, key, val)
        elif action == "write":
            await _profile_write(update, context, uid, lang, key)
        elif action == "skip":
            await _profile_skip(update, context, uid, lang, key)
        return

    # Unknown fallback
    await q.message.reply_text(T[lang]["unknown"])

# ---------- Safe hook to attach the main callback router ----------
def _attach_main_callbacks(app):
    try:
        # keep gate_cb first; main router handles everything else
        app.add_handler(CallbackQueryHandler(on_callback), group=1)
    except Exception as e:
        logging.warning(f"attach callbacks failed: {e}")

# If this module is executed as a whole file, build_app() is defined in Part 1.
# We won't modify build_app; but if the global 'application' exists, attach now.
try:
    if 'application' in globals() and getattr(application, 'add_handler', None):
        _attach_main_callbacks(application)
except Exception as _e:
    logging.debug(f"Deferred callback attach: {_e}")
