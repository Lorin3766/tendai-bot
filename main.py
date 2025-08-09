# -*- coding: utf-8 -*-
import os
import re
import json
import uuid
import logging
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

# langdetect — используем, но безопасно
try:
    from langdetect import detect, DetectorFactory
    DetectorFactory.seed = 0
except Exception:
    detect = None

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ===== OpenAI (для гибридного парсера/подсказок) =====
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# ===== Google Sheets =====
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# =========================
# Boot & Config
# =========================
load_dotenv()
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
SHEET_NAME = os.getenv("SHEET_NAME", "TendAI Feedback")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing")

# OpenAI client
oai = OpenAI(api_key=OPENAI_API_KEY) if (OPENAI_API_KEY and OpenAI) else None

# Google Sheets init
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not creds_json:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")
creds_dict = json.loads(creds_json)
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gclient = gspread.authorize(credentials)
ss = gclient.open(SHEET_NAME)

def _get_or_create_ws(title: str, headers: list[str]):
    try:
        ws = ss.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=2000, cols=20)
        ws.append_row(headers)
    vals = ws.get_all_values()
    if not vals:
        ws.append_row(headers)
    return ws

ws_feedback = _get_or_create_ws(
    "Feedback", ["timestamp", "user_id", "name", "username", "rating", "comment"]
)
ws_users = _get_or_create_ws(
    "Users", ["user_id", "username", "lang", "consent", "tz_offset", "checkin_hour", "paused"]
)
ws_episodes = _get_or_create_ws(
    "Episodes",
    [
        "episode_id", "user_id", "topic", "started_at",
        "baseline_severity", "red_flags", "plan_accepted",
        "target", "reminder_at", "next_checkin_at",
        "status", "last_update", "notes",
    ],
)

# =========================
# State (RAM)
# =========================
# sessions[user_id] = {
#   "topic": "pain",
#   "flow": "collect"|"confirm"|"redflags"|"zone"|"plan"|"accept_wait"|"remind_wait",
#   "answers": {"loc","kind","duration","severity","red"},
#   "zone": {"name": "head/back/belly/chest/throat", "idx": 1..3, "q": {1:"yes"/"no"/"maybe", ...}},
#   "episode_id": "...",
#   "awaiting_comment": bool,
#   "feedback_context": str,
# }
sessions: dict[int, dict] = {}

# =========================
# i18n
# =========================
SUPPORTED = {"ru", "en", "uk", "es"}
def norm_lang(code: str | None) -> str:
    if not code:
        return "en"
    c = code.split("-")[0].lower()
    if c.startswith("ua"):
        c = "uk"
    return c if c in SUPPORTED else "en"

T = {
    "en": {
        "welcome": "Hi! I’m TendAI — your health & longevity assistant.\nChoose a topic below or just describe what’s bothering you.",
        "menu": ["Pain", "Throat/Cold", "Sleep", "Stress", "Digestion", "Energy"],
        "help": "I help with short checkups, a 24–48h plan, and gentle follow-ups.\nCommands: /help, /privacy, /pause, /resume, /delete_data, /lang, /feedback",
        "privacy": "TendAI is not a medical service and can’t replace a doctor. We store minimal data for reminders. Use /delete_data to erase your info.",
        "paused_on": "Notifications paused. Use /resume to enable.",
        "paused_off": "Notifications resumed.",
        "deleted": "All your data in TendAI was deleted. You can /start again anytime.",
        "ask_consent": "May I send you a follow-up later to check how you feel? (Change with /pause or /resume.)",
        "yes": "Yes", "no": "No",
        "choose_topic": "Choose a topic:",
        "open_prompt": "Briefly: where is the pain, how does it feel (sharp/dull/etc.), and how long has it lasted?\nExamples: “Head, throbbing, 3 hours” / “Lower back, sharp when bending, 2 days”.",
        "triage_pain_q1": "Where does it hurt?\nChoose below ⤵️",
        "triage_pain_q1_opts": ["Head", "Throat", "Back", "Belly", "Chest", "Other"],
        "triage_pain_q2": "What kind of pain?\nChoose below ⤵️",
        "triage_pain_q2_opts": ["Dull", "Sharp", "Throbbing", "Burning", "Pressing"],
        "triage_pain_q3": "How long has it lasted?\nChoose below ⤵️",
        "triage_pain_q3_opts": ["<3h", "3–24h", ">1 day", ">1 week"],
        "triage_pain_q4": "Rate the pain now (0–10):",
        "triage_pain_q5": "Any of these now?\n(High fever, Vomiting, Weakness/numbness, Speech/vision issues, Trauma, None)",
        "triage_pain_q5_opts": ["High fever", "Vomiting", "Weakness/numbness", "Speech/vision issues", "Trauma", "None"],
        "confirm_title": "Please confirm I got this right:",
        "confirm_loc": "• Where: {loc}",
        "confirm_kind": "• Character: {kind}",
        "confirm_duration": "• Duration: {duration}",
        "confirm_severity": "• Severity: {severity}/10",
        "confirm_ok": "✅ Looks correct",
        "confirm_change_loc": "✏️ Change Where",
        "confirm_change_kind": "✏️ Change Character",
        "confirm_change_duration": "✏️ Change Duration",
        "confirm_change_severity": "✏️ Change Severity",
        "plan_header": "Your 24–48h plan:",
        "plan_accept": "Will you try this today?",
        "accept_opts": ["✅ Yes", "🔁 Later", "✖️ No"],
        "remind_when": "When shall I check on you?",
        "remind_opts": ["in 4h", "this evening", "tomorrow morning", "no need"],
        "thanks": "Got it 🙌",
        "checkin_ping": "Quick check-in: how is it now (0–10)?",
        "checkin_better": "Nice! Keep it up 💪",
        "checkin_worse": "Sorry to hear. If you have red flags or pain ≥7/10, please consider medical help.",
        "comment_prompt": "Write your comment now. Or send /skip to pass.",
        "comment_saved": "Feedback saved, thank you! 🙌",
        "skip_ok": "Skipped.",
        "unknown": "I need a bit more information to help. Where exactly does it hurt? How long has it lasted?",
        "use_buttons": "Please use the buttons below ⤵️",
        "lang_set": "Language set: English",
        "help_lang": "Use /lang ru|en|uk|es to change language.",
        "oos": "This is outside my scope. I focus on health, self-care and longevity. Please choose a topic below.",
        "fb_prompt": "Would you like to leave quick feedback?",
        "fb_thanks": "Thanks for your feedback! 💛",
        "fb_like": "👍 Useful",
        "fb_dislike": "👎 Didn’t help",
        "fb_write": "✍️ Write a comment",
        # Zone questions
        "ans_yes": "Yes",
        "ans_no": "No",
        "ans_maybe": "Not sure",
        "zone_head_q1": "Sudden 'thunderclap' or worst-ever headache?",
        "zone_head_q2": "Any speech/vision trouble or weakness?",
        "zone_head_q3": "Neck stiffness or fever?",
        "zone_back_q1": "Numbness in groin or loss of bladder/bowel control?",
        "zone_back_q2": "Recent trauma, fever, cancer history or steroids?",
        "zone_back_q3": "Does pain shoot below the knee?",
        "zone_belly_q1": "Where exactly: upper/lower, right/left, or center?",
        "zone_belly_q2": "Related to food, fatty meals, or alcohol?",
        "zone_belly_q3": "Vomiting/diarrhea or no gas/stool? Fever or pregnancy?",
        "zone_chest_q1": "Pressure behind sternum >10 min with shortness of breath or cold sweat?",
        "zone_chest_q2": "Worse with deep breath/movement or when pressing the chest?",
        "zone_chest_q3": "Cough or fever?",
        "zone_throat_q1": "Fever or tonsillar exudate (white patches)?",
        "zone_throat_q2": "Cough or recent sick contact?",
        "zone_throat_q3": "Worse on swallowing or at night?",
    },
    "ru": {
        "welcome": "Привет! Я TendAI — ассистент здоровья и долголетия.\nВыбери тему ниже или опиши, что беспокоит.",
        "menu": ["Боль", "Горло/простуда", "Сон", "Стресс", "Пищеварение", "Энергия"],
        "help": "Помогаю короткой проверкой, планом на 24–48 ч и заботливыми чек-инами.\nКоманды: /help, /privacy, /pause, /resume, /delete_data, /lang, /feedback",
        "privacy": "TendAI не заменяет врача. Мы храним минимум данных для напоминаний. /delete_data — удалить всё.",
        "paused_on": "Напоминания поставлены на паузу. Включить: /resume",
        "paused_off": "Напоминания снова включены.",
        "deleted": "Все ваши данные в TendAI удалены. Можно начать снова через /start.",
        "ask_consent": "Можно прислать напоминание позже, чтобы узнать, как вы? (Меняется командами /pause и /resume.)",
        "yes": "Да", "no": "Нет",
        "choose_topic": "Выберите тему:",
        "open_prompt": "Коротко: где болит, как чувствуется (острая/тупая и т.п.) и сколько длится?\nПримеры: «Голова, пульсирующая, 3 часа» / «Поясница, колющая при наклоне, 2 дня».",
        "triage_pain_q1": "Где болит?\nВыберите ниже ⤵️",
        "triage_pain_q1_opts": ["Голова", "Горло", "Спина", "Живот", "Грудь", "Другое"],
        "triage_pain_q2": "Какой характер боли?\nВыберите ниже ⤵️",
        "triage_pain_q2_opts": ["Тупая", "Острая", "Пульсирующая", "Жгучая", "Давящая"],
        "triage_pain_q3": "Как долго длится?\nВыберите ниже ⤵️",
        "triage_pain_q3_opts": ["<3ч", "3–24ч", ">1 дня", ">1 недели"],
        "triage_pain_q4": "Оцените боль (0–10):",
        "triage_pain_q5": "Есть что-то из этого?\n(Высокая температура, Рвота, Слабость/онемение, Проблемы речи/зрения, Травма, Нет)",
        "triage_pain_q5_opts": ["Высокая температура", "Рвота", "Слабость/онемение", "Проблемы речи/зрения", "Травма", "Нет"],
        "confirm_title": "Проверьте, верно ли я понял:",
        "confirm_loc": "• Где: {loc}",
        "confirm_kind": "• Характер: {kind}",
        "confirm_duration": "• Длительность: {duration}",
        "confirm_severity": "• Интенсивность: {severity}/10",
        "confirm_ok": "✅ Всё верно",
        "confirm_change_loc": "✏️ Изменить «Где»",
        "confirm_change_kind": "✏️ Изменить «Характер»",
        "confirm_change_duration": "✏️ Изменить «Длительность»",
        "confirm_change_severity": "✏️ Изменить «Интенсивность»",
        "plan_header": "Ваш план на 24–48 часов:",
        "plan_accept": "Готовы попробовать сегодня?",
        "accept_opts": ["✅ Да", "🔁 Позже", "✖️ Нет"],
        "remind_when": "Когда напомнить и спросить самочувствие?",
        "remind_opts": ["через 4 часа", "вечером", "завтра утром", "не надо"],
        "thanks": "Принято 🙌",
        "checkin_ping": "Коротко: как сейчас по шкале 0–10?",
        "checkin_better": "Отлично! Продолжаем 💪",
        "checkin_worse": "Если появились «красные флаги» или боль ≥7/10 — лучше обратиться к врачу.",
        "comment_prompt": "Напишите комментарий сейчас. Или /skip — пропустить.",
        "comment_saved": "Отзыв сохранён, спасибо! 🙌",
        "skip_ok": "Пропущено.",
        "unknown": "Нужно чуть больше деталей. Где болит и сколько длится?",
        "use_buttons": "Пожалуйста, используйте кнопки ниже ⤵️",
        "lang_set": "Язык сохранён: Русский",
        "help_lang": "Используйте /lang ru|en|uk|es чтобы сменить язык.",
        "oos": "Это вне моей компетенции. Я помогаю с заботой о здоровье и долголетии. Пожалуйста, выберите тему ниже.",
        "fb_prompt": "Хотите оставить быстрый отзыв?",
        "fb_thanks": "Спасибо за отзыв! 💛",
        "fb_like": "👍 Полезно",
        "fb_dislike": "👎 Не помогло",
        "fb_write": "✍️ Написать отзыв",
        "ans_yes": "Да",
        "ans_no": "Нет",
        "ans_maybe": "Не знаю",
        "zone_head_q1": "Внезапная «как удар»/самая сильная?",
        "zone_head_q2": "Проблемы с речью/зрением или слабость?",
        "zone_head_q3": "Ригидность шеи или температура?",
        "zone_back_q1": "Онемение в паху/потеря контроля мочи/стула?",
        "zone_back_q2": "Недавняя травма, лихорадка, онкология, стероиды?",
        "zone_back_q3": "Отдаёт ниже колена?",
        "zone_belly_q1": "Где точнее: верх/низ, право/лево, центр?",
        "zone_belly_q2": "Связь с едой/жирным/алкоголем?",
        "zone_belly_q3": "Рвота/понос или задержка газов/стула? Жар/беременность?",
        "zone_chest_q1": "Давящая за грудиной >10 мин с одышкой/холодным потом?",
        "zone_chest_q2": "Хуже при вдохе/движении или при надавливании?",
        "zone_chest_q3": "Кашель или температура?",
        "zone_throat_q1": "Температура или налёт на миндалинах?",
        "zone_throat_q2": "Кашель или недавний контакт с больными?",
        "zone_throat_q3": "Боль при глотании или ночью хуже?",
    },
    "uk": {
        "welcome": "Привіт! Я TendAI — асистент здоров’я та довголіття.\nОбери тему нижче або опиши, що турбує.",
        "menu": ["Біль", "Горло/застуда", "Сон", "Стрес", "Травлення", "Енергія"],
        "help": "Допомагаю короткою перевіркою, планом на 24–48 год і чеками.\nКоманди: /help, /privacy, /pause, /resume, /delete_data, /lang, /feedback",
        "privacy": "TendAI не замінює лікаря. Ми зберігаємо мінімум даних для нагадувань. /delete_data — видалити все.",
        "paused_on": "Нагадування призупинені. Увімкнути: /resume",
        "paused_off": "Нагадування знову увімкнені.",
        "deleted": "Усі ваші дані в TendAI видалено. Можна почати знову через /start.",
        "ask_consent": "Можу написати пізніше, щоб дізнатися, як ви? (Змінюється /pause або /resume.)",
        "yes": "Так", "no": "Ні",
        "choose_topic": "Оберіть тему:",
        "open_prompt": "Коротко: де болить, який характер (гострий/тупий і т.д.) і скільки триває?\nПриклади: «Голова, пульсівний, 3 год» / «Поперек, гострий при нахилі, 2 дні».",
        "triage_pain_q1": "Де болить?\nВиберіть нижче ⤵️",
        "triage_pain_q2": "Який характер болю?\nВиберіть нижче ⤵️",
        "triage_pain_q3": "Як довго триває?\nВиберіть нижче ⤵️",
        "triage_pain_q3_opts": ["<3год", "3–24год", ">1 дня", ">1 тижня"],
        "triage_pain_q4": "Оцініть біль (0–10):",
        "triage_pain_q5": "Є щось із цього?\n(Висока температура, Блювання, Слабкість/оніміння, Мова/зір, Травма, Немає)",
        "triage_pain_q5_opts": ["Висока температура", "Блювання", "Слабкість/оніміння", "Мова/зір", "Травма", "Немає"],
        "confirm_title": "Підтвердіть, чи правильно я зрозумів:",
        "confirm_loc": "• Де: {loc}",
        "confirm_kind": "• Характер: {kind}",
        "confirm_duration": "• Тривалість: {duration}",
        "confirm_severity": "• Інтенсивність: {severity}/10",
        "confirm_ok": "✅ Все вірно",
        "confirm_change_loc": "✏️ Змінити «Де»",
        "confirm_change_kind": "✏️ Змінити «Характер»",
        "confirm_change_duration": "✏️ Змінити «Тривалість»",
        "confirm_change_severity": "✏️ Змінити «Інтенсивність»",
        "plan_header": "Ваш план на 24–48 год:",
        "plan_accept": "Готові спробувати сьогодні?",
        "accept_opts": ["✅ Так", "🔁 Пізніше", "✖️ Ні"],
        "remind_when": "Коли нагадати та спитати самопочуття?",
        "remind_opts": ["через 4 год", "увечері", "завтра вранці", "не треба"],
        "thanks": "Прийнято 🙌",
        "checkin_ping": "Коротко: як зараз (0–10)?",
        "checkin_better": "Чудово! Продовжуємо 💪",
        "checkin_worse": "Якщо є «червоні прапорці» або біль ≥7/10 — краще звернутися до лікаря.",
        "comment_prompt": "Напишіть коментар зараз. Або /skip — пропустити.",
        "comment_saved": "Відгук збережено, дякуємо! 🙌",
        "skip_ok": "Пропущено.",
        "unknown": "Потрібно трохи більше деталей. Де болить і скільки триває?",
        "use_buttons": "Будь ласка, скористайтесь кнопками нижче ⤵️",
        "lang_set": "Мову змінено: Українська",
        "help_lang": "Використовуйте /lang ru|en|uk|es щоб змінити мову.",
        "oos": "Це поза моєю компетенцією. Я допомагаю із турботою про здоров’я та довголіття. Будь ласка, оберіть тему нижче.",
        "fb_prompt": "Залишити швидкий відгук?",
        "fb_thanks": "Дякуємо за відгук! 💛",
        "fb_like": "👍 Корисно",
        "fb_dislike": "👎 Не допомогло",
        "fb_write": "✍️ Написати відгук",
        "ans_yes": "Так",
        "ans_no": "Ні",
        "ans_maybe": "Не знаю",
        "zone_head_q1": "Раптовий «удар»/найсильніший у житті?",
        "zone_head_q2": "Проблеми з мовою/зором або слабкість?",
        "zone_head_q3": "Ригідність шиї або температура?",
        "zone_back_q1": "Оніміння в паху/втрата контролю сечі/стулу?",
        "zone_back_q2": "Травма, гарячка, онкологія або стероїди?",
        "zone_back_q3": "Віддає нижче коліна?",
        "zone_belly_q1": "Де саме: верх/низ, право/ліво, центр?",
        "zone_belly_q2": "Зв’язок з їжею/жирним/алкоголем?",
        "zone_belly_q3": "Блювання/діарея чи затримка газів/стулу? Жар/вагітність?",
        "zone_chest_q1": "Тиснучий біль за грудиною >10 хв з задишкою/холодним потом?",
        "zone_chest_q2": "Гірше при вдиху/русі чи при натисканні?",
        "zone_chest_q3": "Кашель або температура?",
        "zone_throat_q1": "Температура або наліт на мигдаликах?",
        "zone_throat_q2": "Кашель або недавній контакт із хворими?",
        "zone_throat_q3": "Біль при ковтанні або гірше вночі?",
    },
    "es": {
        "welcome": "¡Hola! Soy TendAI, tu asistente de salud y longevidad.\nElige un tema o describe qué te molesta.",
        "menu": ["Dolor", "Garganta/Resfriado", "Sueño", "Estrés", "Digestión", "Energía"],
        "help": "Te ayudo con chequeos breves, un plan de 24–48 h y seguimientos.\nComandos: /help, /privacy, /pause, /resume, /delete_data, /lang, /feedback",
        "privacy": "TendAI no sustituye a un médico. Guardamos datos mínimos para recordatorios. Usa /delete_data para borrar tus datos.",
        "paused_on": "Recordatorios pausados. Usa /resume para activarlos.",
        "paused_off": "Recordatorios activados de nuevo.",
        "deleted": "Se eliminaron todos tus datos en TendAI. Puedes empezar otra vez con /start.",
        "ask_consent": "¿Puedo escribirte más tarde para saber cómo sigues? (Cámbialo con /pause o /resume.)",
        "yes": "Sí", "no": "No",
        "choose_topic": "Elige un tema:",
        "open_prompt": "Breve: ¿dónde duele, cómo se siente (agudo/sordo, etc.) y desde cuándo?\nEj.: «Cabeza, palpitante, 3 h» / «Lumbar, punzante al agacharme, 2 días».",
        "triage_pain_q1": "¿Dónde te duele?\nElige abajo ⤵️",
        "triage_pain_q2": "¿Qué tipo de dolor?\nElige abajo ⤵️",
        "triage_pain_q3": "¿Desde cuándo lo tienes?\nElige abajo ⤵️",
        "triage_pain_q3_opts": ["<3h", "3–24h", ">1 día", ">1 semana"],
        "triage_pain_q4": "Valora el dolor ahora (0–10):",
        "triage_pain_q5": "¿Alguno de estos ahora?\n(Fiebre alta, Vómitos, Debilidad/entumecimiento, Habla/visión, Trauma, Ninguno)",
        "triage_pain_q5_opts": ["Fiebre alta", "Vómitos", "Debilidad/entumecimiento", "Habla/visión", "Trauma", "Ninguno"],
        "confirm_title": "Confirma si lo entendí bien:",
        "confirm_loc": "• Dónde: {loc}",
        "confirm_kind": "• Tipo: {kind}",
        "confirm_duration": "• Duración: {duration}",
        "confirm_severity": "• Intensidad: {severity}/10",
        "confirm_ok": "✅ Correcto",
        "confirm_change_loc": "✏️ Cambiar «Dónde»",
        "confirm_change_kind": "✏️ Cambiar «Tipo»",
        "confirm_change_duration": "✏️ Cambiar «Duración»",
        "confirm_change_severity": "✏️ Cambiar «Intensidad»",
        "plan_header": "Tu plan para 24–48 h:",
        "plan_accept": "¿Lo intentas hoy?",
        "accept_opts": ["✅ Sí", "🔁 Más tarde", "✖️ No"],
        "remind_when": "¿Cuándo te escribo para revisar?",
        "remind_opts": ["en 4 h", "esta tarde", "mañana por la mañana", "no hace falta"],
        "thanks": "¡Hecho! 🙌",
        "checkin_ping": "Revisión rápida: ¿cómo estás ahora (0–10)?",
        "checkin_better": "¡Bien! Sigue así 💪",
        "checkin_worse": "Lo siento. Si hay señales de alarma o dolor ≥7/10, considera atención médica.",
        "comment_prompt": "Escribe tu comentario ahora. O envía /skip para omitir.",
        "comment_saved": "¡Comentario guardado, gracias! 🙌",
        "skip_ok": "Omitido.",
        "unknown": "Necesito un poco más de información. ¿Dónde te duele y desde cuándo?",
        "use_buttons": "Usa los botones abajo ⤵️",
        "lang_set": "Idioma guardado: Español",
        "help_lang": "Usa /lang ru|en|uk|es para cambiar el idioma.",
        "oos": "Esto está fuera de mi ámbito. Me enfoco en salud, autocuidado y longevidad. Por favor, elige un tema abajo.",
        "fb_prompt": "¿Quieres dejar una opinión rápida?",
        "fb_thanks": "¡Gracias por tu opinión! 💛",
        "fb_like": "👍 Útil",
        "fb_dislike": "👎 No ayudó",
        "fb_write": "✍️ Escribir comentario",
        "ans_yes": "Sí",
        "ans_no": "No",
        "ans_maybe": "No sé",
        "zone_head_q1": "¿De repente, como un trueno, o la peor de tu vida?",
        "zone_head_q2": "¿Problemas de habla/visión o debilidad?",
        "zone_head_q3": "¿Rigidez de cuello o fiebre?",
        "zone_back_q1": "¿Entumecimiento en la ingle o pérdida de control de orina/defecación?",
        "zone_back_q2": "¿Trauma reciente, fiebre, cáncer o esteroides?",
        "zone_back_q3": "¿Irradia por debajo de la rodilla?",
        "zone_belly_q1": "¿Dónde exactamente: arriba/abajo, derecha/izquierda o centro?",
        "zone_belly_q2": "¿Relacionado con comida, grasas o alcohol?",
        "zone_belly_q3": "¿Vómitos/diarrea o sin gases/evacuación? ¿Fiebre/embarazo?",
    },
}
def t(lang: str, key: str) -> str:
    return T.get(lang, T["en"]).get(key, T["en"].get(key, key))

# =========================
# NLP — синонимы и парсинг
# =========================
LOC_SYNS = {
    "ru": {
        "Head": ["голова","голове","висок","виски","лоб","затылок","темя","темечко"],
        "Throat": ["горло","горле","гланды","миндалины"],
        "Back": ["спина","поясница","позвоночник","лопатка","лопатке"],
        "Belly": ["живот","желудок","кишки","кишечник","животе","желудке"],
        "Chest": ["грудь","груди","грудине","грудной"],
    },
    "en": {
        "Head": ["head","temple","forehead","occiput","back of head"],
        "Throat": ["throat","tonsil","pharynx","sore throat"],
        "Back": ["back","lower back","spine","shoulder blade","scapula"],
        "Belly": ["belly","stomach","abdomen","tummy","gastric"],
        "Chest": ["chest","sternum"],
    },
    "uk": {
        "Head": ["голова","скроня","скроні","потилиця","лоб","тім’я","голові"],
        "Throat": ["горло","мигдалики","глотка"],
        "Back": ["спина","поперек","хребет","лопатка","лопатці"],
        "Belly": ["живіт","шлунок","кишки","кишечник","животі","шлунку"],
        "Chest": ["груди","груднина"],
    },
    "es": {
        "Head": ["cabeza","sien","frente","nuca"],
        "Throat": ["garganta","amígdala","amígdalas","faringe"],
        "Back": ["espalda","lumbago","lumbar","columna","omóplato"],
        "Belly": ["vientre","estómago","abdomen","barriga","panza"],
        "Chest": ["pecho","esternón"],
    },
}

KIND_SYNS = {
    "ru": {
        "Dull": ["тупая","тупой","ноющая","ноет","ломит"],
        "Sharp": ["острая","острый","резкая","режущая","колющая","прострел"],
        "Throbbing": ["пульсирующая","пульсирует","стучит"],
        "Burning": ["жгучая","жжение","жжёт","жжет"],
        "Pressing": ["давящая","давит","сжимает","жмёт"],
    },
    "en": {
        "Dull": ["dull","aching","ache","sore"],
        "Sharp": ["sharp","stabbing","cutting","knife","shooting","acute"],
        "Throbbing": ["throbbing","pulsating","pounding"],
        "Burning": ["burning","burn","scalding"],
        "Pressing": ["pressing","tight","pressure","squeezing"],
    },
    "uk": {
        "Dull": ["тупий","ниючий","ниє","ломить"],
        "Sharp": ["гострий","різкий","колючий","ніж","простріл"],
        "Throbbing": ["пульсівний","стукає","тремтить"],
        "Burning": ["пекучий","печіння"],
        "Pressing": ["тиснучий","тисне","стискає","давить"],
    },
    "es": {
        "Dull": ["sordo","sorda"],
        "Sharp": ["agudo","aguda","punzante","cortante"],
        "Throbbing": ["palpitante","pulsátil","latente"],
        "Burning": ["ardor","ardiente","quemazón"],
        "Pressing": ["opresivo","opresión","aprieta"],
    },
}

DUR_PATTERNS = {
    "ru": r"(\d+)\s*(мин|минут|час|часа|часов|сут|дн|дней|нед|недел)",
    "en": r"(\d+)\s*(min|mins|minute|minutes|hour|hours|day|days|week|weeks)",
    "uk": r"(\d+)\s*(хв|хвилин|год|годин|дн|днів|тижд|тижнів)",
    "es": r"(\d+)\s*(min|minutos|minuto|hora|horas|día|días|semana|semanas)",
}

SEVERITY_PATTERNS = [
    r"\b([0-9]|10)\s*/\s*10\b",
    r"\bна\s*([0-9]|10)\b",
    r"\b([0-9]|10)\s*из\s*10\b",
    r"\b([0-9]|10)\b",
]

def _match_from_map(text: str, mapping: dict[str, list[str]]) -> str | None:
    tl = text.lower()
    for canon, syns in mapping.items():
        for s in syns:
            if s in tl:
                return canon
    return None

def _match_duration(text: str, lang: str) -> str | None:
    m = re.search(DUR_PATTERNS.get(lang, ""), text.lower())
    if not m: return None
    num, unit = m.group(1), m.group(2)
    return f"{num} {unit}"

def _match_severity(text: str) -> int | None:
    tl = text.lower()
    for pat in SEVERITY_PATTERNS:
        m = re.search(pat, tl)
        if m:
            try:
                val = int(m.group(1))
                if 0 <= val <= 10:
                    return val
            except Exception:
                pass
    return None

def extract_slots(text: str, lang: str) -> dict:
    slots = {}
    if not text: return slots
    loc = _match_from_map(text, LOC_SYNS.get(lang, {}))
    if loc: slots["loc"] = loc
    kind = _match_from_map(text, KIND_SYNS.get(lang, {}))
    if kind: slots["kind"] = kind
    dur = _match_duration(text, lang)
    if dur: slots["duration"] = dur
    sev = _match_severity(text)
    if sev is not None: slots["severity"] = sev
    return slots

# =========================
# Sheets helpers
# =========================
def utcnow(): return datetime.now(timezone.utc)
def iso(dt: datetime | None) -> str:
    if not dt: return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")

def users_get_row_index(user_id: int) -> int | None:
    vals = ws_users.get_all_records()
    for i, row in enumerate(vals, start=2):
        if str(row.get("user_id")) == str(user_id):
            return i
    return None

def users_get(user_id: int) -> dict:
    vals = ws_users.get_all_records()
    for row in vals:
        if str(row.get("user_id")) == str(user_id):
            return row
    return {}

def users_upsert(user_id: int, username: str, lang: str):
    idx = users_get_row_index(user_id)
    row = [str(user_id), username or "", lang, "no", "0", "", "no"]
    if idx:
        ws_users.update(f"A{idx}:G{idx}", [row])
    else:
        ws_users.append_row(row)

def users_set(user_id: int, field: str, value: str):
    idx = users_get_row_index(user_id)
    if not idx: return
    headers = ws_users.row_values(1)
    if field in headers:
        col = headers.index(field) + 1
        ws_users.update_cell(idx, col, value)

def episode_create(user_id: int, topic: str, baseline_severity: int, red_flags: str) -> str:
    eid = f"{user_id}-{uuid.uuid4().hex[:8]}"
    now = iso(utcnow())
    ws_episodes.append_row([
        eid, str(user_id), topic, now,
        str(baseline_severity), red_flags, "0", "<=3/10",
        "", "", "open", now, ""
    ])
    return eid

def episode_find_open(user_id: int) -> dict | None:
    vals = ws_episodes.get_all_records()
    for row in vals:
        if str(row.get("user_id")) == str(user_id) and row.get("status") == "open":
            return row
    return None

def episode_set(eid: str, field: str, value: str):
    vals = ws_episodes.get_all_values()
    headers = vals[0]
    if field not in headers: return
    col = headers.index(field) + 1
    for i in range(2, len(vals) + 1):
        if ws_episodes.cell(i, 1).value == eid:
            ws_episodes.update_cell(i, col, value)
            ws_episodes.update_cell(i, headers.index("last_update") + 1, iso(utcnow()))
            return

def schedule_from_sheet_on_start(app):
    vals = ws_episodes.get_all_records()
    now = utcnow()
    for row in vals:
        if row.get("status") != "open": continue
        eid = row.get("episode_id")
        uid = int(row.get("user_id"))
        nca = row.get("next_checkin_at") or ""
        if not nca: continue
        try:
            dt = datetime.strptime(nca, "%Y-%m-%d %H:%M:%S%z")
        except Exception:
            continue
        delay = (dt - now).total_seconds()
        if delay < 60: delay = 60
        app.job_queue.run_once(job_checkin, when=delay, data={"user_id": uid, "episode_id": eid})

# =========================
# UI helpers
# =========================
TOPIC_KEYS = {
    "en": {"Pain": "pain", "Throat/Cold": "throat", "Sleep": "sleep", "Stress": "stress", "Digestion": "digestion", "Energy": "energy"},
    "ru": {"Боль": "pain", "Горло/простуда": "throat", "Сон": "sleep", "Стресс": "stress", "Пищеварение": "digestion", "Энергия": "energy"},
    "uk": {"Біль": "pain", "Горло/застуда": "throat", "Сон": "sleep", "Стрес": "stress", "Травлення": "digestion", "Енергія": "energy"},
    "es": {"Dolor": "pain", "Garganta/Resfriado": "throat", "Sueño": "sleep", "Estrés": "stress", "Digestión": "digestion", "Energía": "energy"},
}

def main_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([T[lang]["menu"]], resize_keyboard=True)

def inline_kb_for_step(lang: str, step: int):
    if step == 1:
        labels = T[lang]["triage_pain_q1_opts"]
    elif step == 2:
        labels = T[lang]["triage_pain_q2_opts"]
    elif step == 3:
        labels = T[lang]["triage_pain_q3_opts"]
    elif step == 5:
        labels = T[lang]["triage_pain_q5_opts"]
    else:
        return None
    per_row = 3 if len(labels) >= 6 else 2
    rows = []
    for i in range(0, len(labels), per_row):
        row = [
            InlineKeyboardButton(text=labels[j], callback_data=f"pain|s|{step}|{j}")
            for j in range(i, min(i + per_row, len(labels)))
        ]
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def inline_kb_numbers_0_10(prefix: str) -> InlineKeyboardMarkup:
    nums = [str(i) for i in range(0, 11)]
    rows, row = [], []
    for i, n in enumerate(nums, start=1):
        row.append(InlineKeyboardButton(n, callback_data=f"{prefix}|{n}"))
        if i % 6 == 0:
            rows.append(row); row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def inline_kb_accept_with_feedback(lang: str, eid: str) -> InlineKeyboardMarkup:
    acc = T[lang]["accept_opts"]
    rows = [
        [
            InlineKeyboardButton(acc[0], callback_data=f"plan|accept|yes|{eid}"),
            InlineKeyboardButton(acc[1], callback_data=f"plan|accept|later|{eid}"),
            InlineKeyboardButton(acc[2], callback_data=f"plan|accept|no|{eid}"),
        ],
        [
            InlineKeyboardButton(t(lang,"fb_like"), callback_data=f"fb|like|plan|{eid}"),
            InlineKeyboardButton(t(lang,"fb_dislike"), callback_data=f"fb|dislike|plan|{eid}"),
            InlineKeyboardButton(t(lang,"fb_write"), callback_data=f"fb|write|plan|{eid}"),
        ]
    ]
    return InlineKeyboardMarkup(rows)

def inline_kb_remind(lang: str, eid: str) -> InlineKeyboardMarkup:
    opts = T[lang]["remind_opts"]
    rows = [
        [
            InlineKeyboardButton(opts[0], callback_data=f"plan|remind|4h|{eid}"),
            InlineKeyboardButton(opts[1], callback_data=f"plan|remind|evening|{eid}"),
        ],
        [
            InlineKeyboardButton(opts[2], callback_data=f"plan|remind|morning|{eid}"),
            InlineKeyboardButton(opts[3], callback_data=f"plan|remind|none|{eid}"),
        ]
    ]
    return InlineKeyboardMarkup(rows)

def inline_kb_confirm(lang: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(t(lang,"confirm_ok"), callback_data="confirm|ok")],
        [
            InlineKeyboardButton(t(lang,"confirm_change_loc"), callback_data="confirm|change|loc"),
            InlineKeyboardButton(t(lang,"confirm_change_kind"), callback_data="confirm|change|kind"),
        ],
        [
            InlineKeyboardButton(t(lang,"confirm_change_duration"), callback_data="confirm|change|duration"),
            InlineKeyboardButton(t(lang,"confirm_change_severity"), callback_data="confirm|change|severity"),
        ],
    ]
    return InlineKeyboardMarkup(rows)

def inline_kb_zone(lang: str, zone_key: str, idx: int) -> InlineKeyboardMarkup:
    y = t(lang, "ans_yes"); n = t(lang, "ans_no"); m = t(lang, "ans_maybe")
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(y, callback_data=f"zone|{zone_key}|{idx}|yes"),
            InlineKeyboardButton(n, callback_data=f"zone|{zone_key}|{idx}|no"),
            InlineKeyboardButton(m, callback_data=f"zone|{zone_key}|{idx}|maybe"),
        ]
    ])

async def send_step_question(message, lang: str, step: int):
    key = {1: "triage_pain_q1", 2: "triage_pain_q2", 3: "triage_pain_q3", 4: "triage_pain_q4", 5: "triage_pain_q5"}[step]
    if step in {1, 2, 3, 5}:
        await message.reply_text(t(lang, key), reply_markup=inline_kb_for_step(lang, step))
    elif step == 4:
        await message.reply_text(t(lang, key), reply_markup=inline_kb_numbers_0_10("pain|sev"))

# =========================
# Plans / Hypotheses (простые правила)
# =========================
def build_hypotheses(lang: str, ans: dict, zone: dict) -> list[tuple[str, float, str]]:
    """
    Возвращает список гипотез (name, score, because)
    Простые эвристики по зоне.
    """
    loc = (ans.get("loc") or "").lower()
    kind = (ans.get("kind") or "").lower()
    duration = (ans.get("duration") or "").lower()
    sev = int(ans.get("severity", 5))
    zq = zone.get("q", {}) if zone else {}
    H = []

    def add(name, score, because):
        H.append((name, float(score), because))

    # Head
    if "head" in loc or "голова" in loc or "cabeza" in loc:
        # migraine
        if "throbb" in kind or "пульс" in kind:
            add("Migraine-like", 0.7 + 0.05*(sev>=6), "Throbbing + moderate/severe")
        # tension
        if "press" in kind or "tight" in kind or "дав" in kind:
            add("Tension-type", 0.6, "Pressing/tight character")
        # sinus
        if "3–24" in duration or ">1 day" in duration or ">1 дня" in duration:
            add("Tension/sinus", 0.4, "Lasts many hours")

        # zone answers
        if zq.get(1) == "yes":  # thunderclap
            add("Urgent risk pattern", 1.0, "Sudden worst-ever → escalate")
        if zq.get(2) == "yes":  # neuro
            add("Neurologic red flag", 1.0, "Speech/vision/weakness → escalate")
        if zq.get(3) == "yes":  # neck stiffness/fever
            add("Infection/meningeal concern", 0.9, "Neck stiffness/fever")

    # Back
    if "back" in loc or "спина" in loc or "espalda" in loc:
        if "shoot" in kind or "прострел" in kind or zq.get(3) == "yes":
            add("Radicular pain (sciatica-like)", 0.7, "Shooting below knee/‘прострел’")
        else:
            add("Mechanical low back pain", 0.6, "Typical pattern without red flags")
        if zq.get(1) == "yes" or zq.get(2) == "yes":
            add("Serious back red flag", 0.95, "Perineal numbness/retention or trauma/fever/cancer")

    # Belly
    if "belly" in loc or "живот" in loc or "abdomen" in loc or "vientre" in loc or "stomach" in loc:
        if "vomit" in (ans.get("red","") or "").lower():
            add("Gastroenteritis-like", 0.6, "Nausea/vomiting")
        add("Dyspepsia/gastritis-like", 0.5, "Common benign causes if no red flags")

    # Chest
    if "chest" in loc or "груд" in loc or "pecho" in loc:
        if zq.get(1) == "yes":
            add("Possible cardiac pattern", 1.0, "Pressure >10min + dyspnea/sweat")
        elif zq.get(2) == "yes":
            add("Pleuritic/musculoskeletal", 0.7, "Worse with breathing/movement/press")
        elif zq.get(3) == "yes":
            add("Respiratory infection", 0.6, "Cough/fever")

    # Throat
    if "throat" in loc or "горло" in loc or "garganta" in loc:
        if zq.get(1) == "yes" and zq.get(2) == "no":
            add("Probable bacterial pharyngitis", 0.6, "Fever + exudate, no cough")
        else:
            add("Viral sore throat", 0.6, "Common viral pattern")

    # Normalize & sort
    H.sort(key=lambda x: x[1], reverse=True)
    return H[:3]

def pain_plan(lang: str, ans: dict, zone: dict, hypotheses: list[tuple[str,float,str]]) -> list[str]:
    red = (ans.get("red") or "").lower()
    # срочная эскалация при явных триггерах
    urgent = any(s in red for s in ["fever", "vomit", "weakness", "speech", "vision", "травм", "trauma"]) and (ans.get("severity", 0) >= 7)
    # также по гипотезам
    for name, score, because in hypotheses:
        if "Urgent" in name or "cardiac" in name or "Neurologic" in name or "red flag" in name:
            urgent = True
    if urgent:
        return {
            "ru": ["⚠️ Есть признаки возможной угрозы. Пожалуйста, как можно скорее обратитесь за медицинской помощью."],
            "uk": ["⚠️ Є ознаки можливої загрози. Будь ласка, якнайшвидше зверніться по медичну допомогу."],
            "en": ["⚠️ Some answers suggest urgent risks. Please seek medical care as soon as possible."],
            "es": ["⚠️ Hay señales de posible urgencia. Por favor busca atención médica lo antes posible."],
        }[lang]

    base = {
        "ru": [
            "1) Вода 400–600 мл и 15–20 минут покоя в тихом месте.",
            "2) Если нет противопоказаний — ибупрофен 200–400 мг 1 раз с едой.",
            "3) Проветрить комнату, снизить экраны на 30–60 минут.",
            "Цель: к вечеру боль ≤3/10."
        ],
        "uk": [
            "1) 400–600 мл води та 15–20 хв відпочинку у тихому місці.",
            "2) Якщо немає протипоказань — ібупрофен 200–400 мг 1 раз із їжею.",
            "3) Провітрити кімнату, зменшити екрани на 30–60 хв.",
            "Мета: до вечора біль ≤3/10."
        ],
        "en": [
            "1) Drink 400–600 ml of water and rest 15–20 minutes in a quiet place.",
            "2) If no contraindications — ibuprofen 200–400 mg once with food.",
            "3) Air the room; reduce screen time for 30–60 minutes.",
            "Target: by evening pain ≤3/10."
        ],
        "es": [
            "1) Bebe 400–600 ml de agua y descansa 15–20 min en un lugar tranquilo.",
            "2) Si no hay contraindicaciones — ibuprofeno 200–400 mg una vez con comida.",
            "3) Ventila la habitación; reduce pantallas 30–60 min.",
            "Objetivo: por la tarde dolor ≤3/10."
        ],
    }
    # Лёгкая персонализация: если спина → добавить тепло/растяжка; горло → тёплые напитки/полоскание
    loc = (ans.get("loc") or "").lower()
    if "back" in loc or "спина" in loc or "espalda" in loc:
        extra = {
            "ru": ["4) Тёплый компресс 10–15 мин 2–3 раза/день, мягкая мобилизация/растяжка."],
            "uk": ["4) Теплий компрес 10–15 хв 2–3 р/день, м’яка мобілізація/розтяжка."],
            "en": ["4) Warm compress 10–15 min 2–3×/day, gentle mobility/stretching."],
            "es": ["4) Compresa tibia 10–15 min 2–3×/día, movilidad/estiramientos suaves."],
        }[lang]
        return base[lang] + extra
    if "throat" in loc or "горло" in loc or "garganta" in loc:
        extra = {
            "ru": ["4) Тёплое питьё, полоскания солевым раствором 3–4 раза/день."],
            "uk": ["4) Теплі напої, полоскання сольовим розчином 3–4 р/день."],
            "en": ["4) Warm fluids; saline gargles 3–4×/day."],
            "es": ["4) Líquidos tibios; gárgaras salinas 3–4×/día."],
        }[lang]
        return base[lang] + extra
    return base[lang]

# =========================
# Jobs (check-ins)
# =========================
async def job_checkin(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    uid = data.get("user_id")
    eid = data.get("episode_id")
    if not uid or not eid: return
    u = users_get(uid)
    if (u.get("paused") or "").lower() == "yes": return
    lang = u.get("lang") or "en"
    try:
        await context.bot.send_message(
            chat_id=uid,
            text=t(lang, "checkin_ping"),
            reply_markup=inline_kb_numbers_0_10("checkin|sev"),
        )
        episode_set(eid, "next_checkin_at", "")
    except Exception as e:
        logging.error(f"job_checkin send error: {e}")

# =========================
# Commands
# =========================
async def on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logging.info("Webhook cleared")
    except Exception:
        pass
    schedule_from_sheet_on_start(app)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = users_get(user.id).get("lang")
    if not lang:
        txt = (update.message.text or "").strip() if update.message else ""
        cand = None
        if detect:
            try:
                cand = detect(txt) if txt else None
            except Exception:
                cand = None
        lang = norm_lang(cand or getattr(user, "language_code", None))
        users_upsert(user.id, user.username or "", lang)
    await update.message.reply_text(t(lang, "welcome"), reply_markup=main_menu(lang))
    u = users_get(user.id)
    if (u.get("consent") or "").lower() not in {"yes","no"}:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(t(lang,"yes"),callback_data="consent|yes"),
                                    InlineKeyboardButton(t(lang,"no"),callback_data="consent|no")]])
        await update.message.reply_text(t(lang, "ask_consent"), reply_markup=kb)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang, "help"))

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang, "privacy"))

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "yes")
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang, "paused_on"))

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "no")
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang, "paused_off"))

async def cmd_delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    idx = users_get_row_index(uid)
    if idx:
        ws_users.delete_rows(idx)
    vals = ws_episodes.get_all_values()
    to_delete = []
    for i in range(2, len(vals)+1):
        if ws_episodes.cell(i,2).value == str(uid):
            to_delete.append(i)
    for j, row_i in enumerate(to_delete):
        ws_episodes.delete_rows(row_i - j)
    lang = norm_lang(getattr(update.effective_user, "language_code", None))
    await update.message.reply_text(t(lang, "deleted"), reply_markup=ReplyKeyboardRemove())

async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
        await update.message.reply_text(t(lang, "help_lang"))
        return
    candidate = norm_lang(context.args[0])
    if candidate not in SUPPORTED:
        cur = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
        await update.message.reply_text(t(cur, "help_lang"))
        return
    users_set(uid, "lang", candidate)
    await update.message.reply_text(t(candidate, "lang_set"), reply_markup=main_menu(candidate))

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = sessions.get(uid, {})
    if s.get("awaiting_comment"):
        s["awaiting_comment"] = False
        s["feedback_context"] = ""
        lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
        await update.message.reply_text(t(lang, "skip_ok"))
    else:
        lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
        await update.message.reply_text(t(lang, "use_buttons"))

async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(
        t(lang, "fb_prompt"),
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(t(lang,"fb_like"), callback_data="fb|like|general|"),
                InlineKeyboardButton(t(lang,"fb_dislike"), callback_data="fb|dislike|general|"),
                InlineKeyboardButton(t(lang,"fb_write"), callback_data="fb|write|general|"),
            ]
        ])
    )

# =========================
# Auto language switch & scope filter
# =========================
GREET_WORDS = {
    "en": {"hi", "hello", "hey"},
    "ru": {"привет", "здравствуйте", "хай"},
    "uk": {"привіт", "вітаю"},
    "es": {"hola", "buenas"},
}

def maybe_autoswitch_lang(uid: int, text: str, cur_lang: str) -> str:
    if not text or text.startswith("/"):
        return cur_lang
    tl = text.strip().lower()
    for lang_code, words in GREET_WORDS.items():
        if tl in words:
            if lang_code != cur_lang:
                users_set(uid, "lang", lang_code)
            return lang_code
    has_lat = bool(re.search(r"[A-Za-z]", text))
    has_cyr = bool(re.search(r"[А-Яа-яЁёІіЇїЄє]", text))
    if has_lat and not has_cyr and cur_lang != "en":
        users_set(uid, "lang", "en")
        return "en"
    if detect:
        try:
            cand = norm_lang(detect(text))
            if cand in SUPPORTED and cand != cur_lang and len(tl) >= 2:
                users_set(uid, "lang", cand)
                return cand
        except Exception:
            pass
    return cur_lang

CARE_KEYWORDS = {
    "en": {
        "pain","headache","throat","cough","cold","fever","back","belly","stomach","chest",
        "sleep","insomnia","stress","anxiety","energy","fatigue","digestion","diarrhea","constipation",
        "nausea","vomit","symptom","medicine","ibuprofen","health","wellness"
    },
    "ru": {
        "боль","болит","голова","головная","горло","кашель","простуда","температура","жар",
        "спина","живот","желудок","грудь","сон","бессонница","стресс","тревога","энергия","слабость",
        "пищеварение","диарея","понос","запор","тошнота","рвота","симптом","здоровье","ибупрофен"
    },
    "uk": {
        "біль","болить","голова","горло","кашель","застуда","температура","жар","спина","живіт","шлунок",
        "груди","сон","безсоння","стрес","тривога","енергія","слабкість","травлення","діарея","запор",
        "нудота","блювання","симптом","здоров'я","ібупрофен"
    },
    "es": {
        "dolor","cabeza","garganta","tos","resfriado","fiebre","espalda","vientre","estómago","pecho",
        "sueño","insomnio","estrés","ansiedad","energía","cansancio","digestión","diarrea","estreñimiento",
        "náusea","vómito","síntoma","salud","ibuprofeno"
    },
}
def is_care_related(lang: str, text: str) -> bool:
    tl = (text or "").lower()
    words = CARE_KEYWORDS.get(lang, CARE_KEYWORDS["en"])
    if tl in GREET_WORDS.get(lang, set()) or tl in {"hi","hello","hola","привет","привіт"}:
        return True
    return any(w in tl for w in words)

# =========================
# LLM hybrid parser (JSON → слоты)
# =========================
def parse_with_llm(text: str, lang_hint: str) -> dict:
    if not oai or not text:
        return {}
    sys = (
        "You are a triage extractor for a health self-care assistant. "
        "Extract fields from user's text. Return ONLY a compact JSON object with keys: "
        "intent, loc, kind, duration, severity, red_flags, lang, confidence. "
        "Allowed values: intent in [pain, throat, sleep, stress, digestion, energy]; "
        "loc in [Head, Throat, Back, Belly, Chest, Other]; "
        "kind in [Dull, Sharp, Throbbing, Burning, Pressing]; "
        "duration in [\"<3h\",\"3–24h\",\">1 day\",\">1 week\"]; "
        "severity integer 0..10; red_flags subset of "
        "[\"High fever\",\"Vomiting\",\"Weakness/numbness\",\"Speech/vision issues\",\"Trauma\"]. "
        "lang in [ru,en,uk,es]. confidence 0..1. "
        "If unknown, use nulls. Respond with JSON only."
    )
    try:
        resp = oai.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.0,
            max_tokens=200,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": f"User text (lang hint {lang_hint}): {text}"},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group(0)) if m else json.loads(raw)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:
        logging.warning(f"LLM parse failed: {e}")
        return {}

def normalize_llm_slots(data: dict, lang: str) -> dict:
    slots = {}
    if not data: return slots
    if data.get("loc") in {"Head","Throat","Back","Belly","Chest","Other"}:
        slots["loc"] = data["loc"]
    if data.get("kind") in {"Dull","Sharp","Throbbing","Burning","Pressing"}:
        slots["kind"] = data["kind"]
    if data.get("duration") in {"<3h","3–24h",">1 day",">1 week"}:
        slots["duration"] = data["duration"]
    sev = data.get("severity")
    if isinstance(sev, int) and 0 <= sev <= 10:
        slots["severity"] = sev
    r = data.get("red_flags") or []
    if isinstance(r, list) and r:
        allowed = {"High fever","Vomiting","Weakness/numbness","Speech/vision issues","Trauma","None"}
        slots["red"] = next((x for x in r if x in allowed), None) or "None"
    return slots

# =========================
# Topic detection
# =========================
def detect_or_choose_topic(lang: str, text: str) -> str | None:
    tl = text.lower().strip()
    if any(w in tl for w in ["болит","боль","hurt","pain","dolor","болю"]): return "pain"
    if any(w in tl for w in ["горло","throat","garganta","простуд","cold"]): return "throat"
    if any(w in tl for w in ["сон","sleep","sueñ"]): return "sleep"
    if any(w in tl for w in ["стресс","stress","estrés"]): return "stress"
    if any(w in tl for w in ["живот","желуд","живіт","стул","понос","диар","digest","estómago","barriga","abdomen"]): return "digestion"
    if any(w in tl for w in ["энерг","енерг","energy","fatigue","слабость","energía","cansancio"]): return "energy"
    for label, key in TOPIC_KEYS.get(lang, TOPIC_KEYS["en"]).items():
        if text.strip() == label: return key
    return None

# =========================
# FLOW HELPERS
# =========================
def next_missing_step(ans: dict) -> int:
    if "loc" not in ans: return 1
    if "kind" not in ans: return 2
    if "duration" not in ans: return 3
    if "severity" not in ans: return 4
    if "red" not in ans: return 5
    return 0  # all present

def render_confirm(lang: str, ans: dict) -> str:
    def val(k, default="—"):
        v = ans.get(k)
        return str(v) if v not in [None, ""] else default
    parts = [
        t(lang, "confirm_title"),
        t(lang, "confirm_loc").format(loc=val("loc","—")),
        t(lang, "confirm_kind").format(kind=val("kind","—")),
        t(lang, "confirm_duration").format(duration=val("duration","—")),
        t(lang, "confirm_severity").format(severity=val("severity","—")),
    ]
    return "\n".join(parts)

def zone_key_from_loc(ans_loc: str) -> str:
    if not ans_loc: return "general"
    tl = ans_loc.lower()
    if "head" in tl or "голов" in tl or "cabeza" in tl: return "head"
    if "back" in tl or "спина" in tl or "espalda" in tl or "пояс" in tl: return "back"
    if "belly" in tl or "жив" in tl or "abdomen" in tl or "stomach" in tl or "vientre" in tl: return "belly"
    if "chest" in tl or "груд" in tl or "pecho" in tl: return "chest"
    if "throat" in tl or "горло" in tl or "garganta" in tl: return "throat"
    return "general"

def zone_question_text(lang: str, zone_key: str, idx: int) -> str:
    key = f"zone_{zone_key}_q{idx}"
    return t(lang, key)

# =========================
# Callback handler
# =========================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(q.from_user,"language_code",None))
    s = sessions.setdefault(uid, {})

    if data.startswith("consent|"):
        users_set(uid, "consent", "yes" if data.endswith("|yes") else "no")
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await q.message.reply_text(t(lang, "thanks"))
        return

    # Feedback
    if data.startswith("fb|"):
        _, action, context_name, eid = (data.split("|") + ["","","",""])[:4]
        name = context_name or "general"
        rating = ""
        comment = ""
        if action == "like":
            rating = "1"
            ws_feedback.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), str(uid), name, q.from_user.username or "", rating, comment])
            await q.message.reply_text(t(lang, "fb_thanks"))
        elif action == "dislike":
            rating = "0"
            ws_feedback.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), str(uid), name, q.from_user.username or "", rating, comment])
            await q.message.reply_text(t(lang, "fb_thanks"))
        elif action == "write":
            s["awaiting_comment"] = True
            s["feedback_context"] = name
            await q.message.reply_text(t(lang, "comment_prompt"))
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        sessions[uid] = s
        return

    # Confirm flow
    if data.startswith("confirm|"):
        parts = data.split("|")
        if parts[1] == "ok":
            # move to red flags
            s["flow"] = "redflags"
            sessions[uid] = s
            await q.message.reply_text(t(lang, "triage_pain_q5"), reply_markup=inline_kb_for_step(lang, 5))
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        elif parts[1] == "change":
            field = parts[2] if len(parts) > 2 else ""
            # ask specific step
            if field == "loc":
                await q.message.reply_text(t(lang,"triage_pain_q1"), reply_markup=inline_kb_for_step(lang,1))
            elif field == "kind":
                await q.message.reply_text(t(lang,"triage_pain_q2"), reply_markup=inline_kb_for_step(lang,2))
            elif field == "duration":
                await q.message.reply_text(t(lang,"triage_pain_q3"), reply_markup=inline_kb_for_step(lang,3))
            elif field == "severity":
                await q.message.reply_text(t(lang,"triage_pain_q4"), reply_markup=inline_kb_numbers_0_10("pain|sev"))
            return

    # Pain inline selections (loc/kind/duration/red)
    if data.startswith("pain|s|"):
        try:
            _, _, step_str, idx_str = data.split("|")
            step = int(step_str)
            idx = int(idx_str)
        except Exception:
            return
        ans = s.setdefault("answers", {})
        labels_map = {
            1: T[lang]["triage_pain_q1_opts"],
            2: T[lang]["triage_pain_q2_opts"],
            3: T[lang]["triage_pain_q3_opts"],
            5: T[lang]["triage_pain_q5_opts"],
        }
        labels = labels_map.get(step, [])
        if idx < 0 or idx >= len(labels):
            return
        label = labels[idx]

        if step == 1:
            ans["loc"] = label
            # after picking loc during confirm-change → re-render confirm
            if s.get("flow") in {"confirm","collect"}:
                text = render_confirm(lang, ans)
                await q.message.reply_text(text, reply_markup=inline_kb_confirm(lang))
        elif step == 2:
            ans["kind"] = label
            if s.get("flow") in {"confirm","collect"}:
                text = render_confirm(lang, ans)
                await q.message.reply_text(text, reply_markup=inline_kb_confirm(lang))
        elif step == 3:
            ans["duration"] = label
            if s.get("flow") in {"confirm","collect"}:
                text = render_confirm(lang, ans)
                await q.message.reply_text(text, reply_markup=inline_kb_confirm(lang))
        elif step == 5:
            ans["red"] = label
            # after red flags → zone or escalate
            s["flow"] = "zone"
            # If red flag present and not "None" → still proceed to zone but plan may escalate later
            zname = zone_key_from_loc(ans.get("loc",""))
            s["zone"] = {"name": zname, "idx": 1, "q": {}}
            # ask zone q1 if exists, else skip to plan
            if zname != "general":
                txt = zone_question_text(lang, zname, 1)
                await q.message.reply_text(txt, reply_markup=inline_kb_zone(lang, zname, 1))
            else:
                # no specific zone → go plan directly
                hyps = build_hypotheses(lang, ans, s.get("zone", {}))
                eid = s.get("episode_id")
                if not eid:
                    eid = episode_create(uid, "pain", int(ans.get("severity",5)), ans.get("red","None"))
                    s["episode_id"] = eid
                plan_lines = pain_plan(lang, ans, s.get("zone", {}), hyps)
                await q.message.reply_text(f"{t(lang,'plan_header')}\n" + "\n".join(plan_lines))
                await q.message.reply_text(t(lang,"plan_accept"), reply_markup=inline_kb_accept_with_feedback(lang, eid))
                s["flow"] = "accept_wait"

        sessions[uid] = s
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if data.startswith("pain|sev|"):
        n = int(data.split("|")[-1])
        ans = s.setdefault("answers", {})
        ans["severity"] = n
        sessions[uid] = s
        try:
            await q.edit_message_text(f"{t(lang, 'triage_pain_q4')}\n• {n} ✅")
        except Exception:
            pass
        # If we are in confirm/collect → re-render confirm; else if we were asking sev during flow, continue
        if s.get("flow") in {"confirm","collect"}:
            text = render_confirm(lang, ans)
            await q.message.reply_text(text, reply_markup=inline_kb_confirm(lang))
        else:
            await send_step_question(q.message, lang, 5)
        return

    if data.startswith("checkin|sev|"):
        try:
            val = int(data.split("|")[-1])
        except Exception:
            return
        ep = episode_find_open(uid)
        if not ep:
            await q.message.reply_text(t(lang, "thanks"), reply_markup=main_menu(lang))
            return
        eid = ep.get("episode_id")
        episode_set(eid, "notes", f"checkin:{val}")
        if val <= 3:
            await q.message.reply_text(t(lang, "checkin_better"), reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(lang,"fb_like"), callback_data="fb|like|checkin|"),
                 InlineKeyboardButton(t(lang,"fb_dislike"), callback_data="fb|dislike|checkin|"),
                 InlineKeyboardButton(t(lang,"fb_write"), callback_data="fb|write|checkin|"),]
            ]))
            episode_set(eid, "status", "resolved")
        else:
            await q.message.reply_text(t(lang, "checkin_worse"), reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(lang,"fb_like"), callback_data="fb|like|checkin|"),
                 InlineKeyboardButton(t(lang,"fb_dislike"), callback_data="fb|dislike|checkin|"),
                 InlineKeyboardButton(t(lang,"fb_write"), callback_data="fb|write|checkin|"),]
            ]))
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    # Zone questions
    if data.startswith("zone|"):
        _, zname, idx_str, answ = data.split("|")
        idx = int(idx_str)
        zone = s.setdefault("zone", {"name": zname, "idx": 1, "q": {}})
        zone["name"] = zname
        zone["q"][idx] = answ
        sessions[uid] = s
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        if idx < 3:
            nxt = idx + 1
            txt = zone_question_text(lang, zname, nxt)
            await q.message.reply_text(txt, reply_markup=inline_kb_zone(lang, zname, nxt))
            zone["idx"] = nxt
            sessions[uid] = s
            return
        else:
            # compute hypotheses + plan
            ans = s.setdefault("answers", {})
            hyps = build_hypotheses(lang, ans, zone)
            # show quick hypotheses with because
            if hyps:
                lines = []
                for name, score, because in hyps:
                    lines.append(f"• {name} ({int(score*100)}%) — {because}")
                await q.message.reply_text("\n".join(lines))
            eid = s.get("episode_id")
            if not eid:
                eid = episode_create(uid, "pain", int(ans.get("severity",5)), ans.get("red","None"))
                s["episode_id"] = eid
            plan_lines = pain_plan(lang, ans, zone, hyps)
            await q.message.reply_text(f"{t(lang,'plan_header')}\n" + "\n".join(plan_lines))
            await q.message.reply_text(t(lang,"plan_accept"), reply_markup=inline_kb_accept_with_feedback(lang, eid))
            s["flow"] = "accept_wait"
            sessions[uid] = s
            return

    # Plan acceptance & remind
    if data.startswith("plan|accept|"):
        _, _, choice, eid = data.split("|")
        if choice == "yes":
            episode_set(eid, "plan_accepted", "1")
            s["flow"] = "remind_wait"
            await q.message.reply_text(t(lang, "remind_when"), reply_markup=inline_kb_remind(lang, eid))
        elif choice == "later":
            episode_set(eid, "plan_accepted", "later")
            s["flow"] = "remind_wait"
            await q.message.reply_text(t(lang, "remind_when"), reply_markup=inline_kb_remind(lang, eid))
        else:
            episode_set(eid, "plan_accepted", "0")
            s["flow"] = "plan"
            await q.message.reply_text(t(lang, "thanks"), reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(lang,"fb_like"), callback_data="fb|like|plan|"+eid),
                 InlineKeyboardButton(t(lang,"fb_dislike"), callback_data="fb|dislike|plan|"+eid),
                 InlineKeyboardButton(t(lang,"fb_write"), callback_data="fb|write|plan|"+eid)]
            ]))
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        sessions[uid] = s
        return

    if data.startswith("plan|remind|"):
        _, _, code, eid = data.split("|")
        urec = users_get(uid)
        tz_off = 0
        try:
            tz_off = int(urec.get("tz_offset") or "0")
        except Exception:
            tz_off = 0
        now_utc = utcnow()
        user_now = now_utc + timedelta(hours=tz_off)
        if code == "4h":
            target_user = user_now + timedelta(hours=4)
        elif code == "evening":
            target_user = user_now.replace(hour=19, minute=0, second=0, microsecond=0)
            if target_user < user_now:
                target_user = target_user + timedelta(days=1)
        elif code == "morning":
            target_user = user_now.replace(hour=9, minute=0, second=0, microsecond=0)
            if target_user < user_now:
                target_user = target_user + timedelta(days=1)
        else:  # none
            target_user = None

        if target_user:
            target_utc = target_user - timedelta(hours=tz_off)
            episode_set(eid, "next_checkin_at", iso(target_utc))
            delay = (target_utc - now_utc).total_seconds()
            if delay < 60: delay = 60
            context.job_queue.run_once(job_checkin, when=delay, data={"user_id": uid, "episode_id": eid})
        await q.message.reply_text(t(lang, "thanks"), reply_markup=main_menu(lang))
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        s["flow"] = "plan"
        sessions[uid] = s
        return

# =========================
# Triaging: start/collect/confirm
# =========================
async def start_pain_triage(update: Update, lang: str, uid: int):
    sessions[uid] = {"topic": "pain", "flow": "collect", "answers": {}}
    await update.message.reply_text(t(lang, "open_prompt"))

async def proceed_to_confirm(message, lang: str, uid: int):
    s = sessions.setdefault(uid, {})
    ans = s.setdefault("answers", {})
    text = render_confirm(lang, ans)
    s["flow"] = "confirm"
    sessions[uid] = s
    await message.reply_text(text, reply_markup=inline_kb_confirm(lang))

async def continue_collect(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str, uid: int, text_input: str):
    s = sessions.setdefault(uid, {"topic": "pain", "flow": "collect", "answers": {}})
    ans = s.setdefault("answers", {})

    # LLM-гибрид
    llm_data = parse_with_llm(text_input, lang)
    if llm_data and llm_data.get("confidence", 0) >= 0.5:
        ans.update(normalize_llm_slots(llm_data, lang))

    # Правила
    slots = extract_slots(text_input, lang)
    for k, v in slots.items():
        ans.setdefault(k, v)

    # Если совсем пусто — попросим примеры ещё раз
    if not ans:
        await update.message.reply_text(t(lang, "open_prompt"))
        return

    sessions[uid] = s
    await proceed_to_confirm(update.message, lang, uid)

# =========================
# Text handlers
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    text = (update.message.text or "").strip()

    # язык и автосвитч
    urec = users_get(uid)
    if not urec:
        cand = None
        if detect:
            try:
                cand = detect(text) if text else None
            except Exception:
                cand = None
        lang = norm_lang(cand or getattr(user,"language_code",None))
        users_upsert(uid, user.username or "", lang)
    else:
        lang = norm_lang(urec.get("lang") or getattr(user,"language_code",None))
        lang = maybe_autoswitch_lang(uid, text, lang)

    # ждём комментарий?
    s = sessions.get(uid, {})
    if s.get("awaiting_comment") and not text.startswith("/"):
        name = s.get("feedback_context") or "general"
        ws_feedback.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), str(uid), f"comment:{name}", user.username or "", "", text])
        s["awaiting_comment"] = False
        s["feedback_context"] = ""
        sessions[uid] = s
        await update.message.reply_text(t(lang, "comment_saved"))
        return

    # простые приветы → меню
    if text.lower() in {"hi","hello","hey","hola","привет","здравствуйте","привіт","вітаю","buenas"}:
        await update.message.reply_text(t(lang, "welcome"), reply_markup=main_menu(lang))
        return

    # оффтоп?
    if not is_care_related(lang, text):
        await update.message.reply_text(t(lang, "oos"), reply_markup=main_menu(lang))
        return

    # ACTIVE FLOW
    if s and s.get("topic") == "pain":
        flow = s.get("flow") or "collect"
        if flow == "collect":
            await continue_collect(update, context, lang, uid, text)
            return
        elif flow in {"confirm"}:
            # Если человек пишет текст вместо кнопок — пытаемся обновить слоты и снова показать confirm
            await continue_collect(update, context, lang, uid, text)
            return
        elif flow in {"redflags","zone","accept_wait","remind_wait"}:
            await update.message.reply_text(t(lang, "use_buttons"))
            return

    # новая тема → триаж
    topic = detect_or_choose_topic(lang, text) or "pain"
    if topic in {"pain","throat","sleep","stress","digestion","energy"}:
        await start_pain_triage(update, lang, uid)
        # сразу попытаемся понять из текста, если он не пустой
        if text:
            await continue_collect(update, context, lang, uid, text)
        return

    # фолбэк
    await update.message.reply_text(t(lang, "unknown"), reply_markup=main_menu(lang))

# =========================
# Runner
# =========================
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_startup).build()

    schedule_from_sheet_on_start(app)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("delete_data", cmd_delete_data))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("feedback", cmd_feedback))

    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
