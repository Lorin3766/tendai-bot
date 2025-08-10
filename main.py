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
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,  # оставляем на будущее (совместимость)
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
#   "flow": "collect|confirm|redflags|accept_wait|remind_wait|plan",
#   "answers": {"loc","kind","duration","severity","red"},
#   "await_step": int|str,
#   "episode_id": "...",
#   "awaiting_comment": bool,
#   "awaiting_feedback_choice": bool,
#   "awaiting_consent": bool,
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
        "welcome": "Hi! I’m TendAI — your health & longevity assistant.\nChoose a topic below or briefly tell me what’s going on.",
        "menu": ["Pain", "Throat/Cold", "Sleep", "Stress", "Digestion", "Energy"],
        "help": "I help with short checkups, a 24–48h plan, and gentle follow-ups.\nCommands: /help, /privacy, /pause, /resume, /delete_data, /lang, /feedback",
        "privacy": "TendAI is not a medical service and can’t replace a doctor. We store minimal data for reminders. Use /delete_data to erase your info.",
        "paused_on": "Notifications paused. Use /resume to enable.",
        "paused_off": "Notifications resumed.",
        "deleted": "All your data in TendAI was deleted. You can /start again anytime.",
        "ask_consent": "May I check in with you later about how you feel?",
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
        "oos": "Got it. I’m here for health, self-care and habits. Pick a topic below or briefly tell me what’s going on.",
        "fb_prompt": "Would you like to leave quick feedback?",
        "fb_thanks": "Thanks for your feedback! 💛",
        "fb_like": "👍 Useful",
        "fb_dislike": "👎 Didn’t help",
        "fb_write": "✍️ Write a comment",
    },
    "ru": {
        "welcome": "Привет! Я TendAI — ассистент здоровья и долголетия.\nВыбери тему ниже или коротко опиши, что беспокоит.",
        "menu": ["Боль", "Горло/простуда", "Сон", "Стресс", "Пищеварение", "Энергия"],
        "help": "Помогаю короткой проверкой, планом на 24–48 ч и бережными чек-инами.\nКоманды: /help, /privacy, /pause, /resume, /delete_data, /lang, /feedback",
        "privacy": "TendAI не заменяет врача. Храним минимум данных для напоминаний. /delete_data — удалить всё.",
        "paused_on": "Напоминания на паузе. Включить: /resume",
        "paused_off": "Напоминания снова включены.",
        "deleted": "Все ваши данные в TendAI удалены. Можно начать заново через /start.",
        "ask_consent": "Можно я напишу позже, чтобы узнать, как вы себя чувствуете?",
        "yes": "Да", "no": "Нет",
        "choose_topic": "Выберите тему:",
        "open_prompt": "Коротко: где болит, какой характер (острая/тупая и т.п.) и сколько длится?\nПримеры: «Голова, пульсирующая, 3 часа» / «Поясница, колющая при наклоне, 2 дня».",
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
        "oos": "Понял тебя. Я помогаю по здоровью, самочувствию и привычкам. Выбери тему внизу или коротко опиши, что беспокоит.",
        "fb_prompt": "Оставите быстрый отзыв?",
        "fb_thanks": "Спасибо за отзыв! 💛",
        "fb_like": "👍 Полезно",
        "fb_dislike": "👎 Не помогло",
        "fb_write": "✍️ Написать отзыв",
    },
    "uk": {
        "welcome": "Привіт! Я TendAI — асистент здоров’я та довголіття.\nОбери тему нижче або коротко опиши, що турбує.",
        "menu": ["Біль", "Горло/застуда", "Сон", "Стрес", "Травлення", "Енергія"],
        "help": "Допомагаю короткою перевіркою, планом на 24–48 год і чеками.\nКоманди: /help, /privacy, /pause, /resume, /delete_data, /lang, /feedback",
        "privacy": "TendAI не замінює лікаря. Зберігаємо мінімум даних для нагадувань. /delete_data — видалити все.",
        "paused_on": "Нагадування призупинені. Увімкнути: /resume",
        "paused_off": "Нагадування знову увімкнені.",
        "deleted": "Усі ваші дані в TendAI видалено. Можна почати знову через /start.",
        "ask_consent": "Можу написати пізніше, щоб дізнатися, як ви?",
        "yes": "Так", "no": "Ні",
        "choose_topic": "Оберіть тему:",
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
        "plan_accept": "Спробуємо сьогодні?",
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
        "oos": "Зрозумів. Я тут для здоров’я, звичок і самопіклування. Оберіть тему нижче або коротко опишіть ситуацію.",
        "fb_prompt": "Залишити швидкий відгук?",
        "fb_thanks": "Дякуємо за відгук! 💛",
        "fb_like": "👍 Корисно",
        "fb_dislike": "👎 Не допомогло",
        "fb_write": "✍️ Написати відгук",
    },
    "es": {
        "welcome": "¡Hola! Soy TendAI, tu asistente de salud y longevidad.\nElige un tema abajo o cuéntame brevemente qué pasa.",
        "menu": ["Dolor", "Garganta/Resfriado", "Sueño", "Estrés", "Digestión", "Energía"],
        "help": "Te ayudo con chequeos breves, un plan de 24–48 h y seguimientos.\nComandos: /help, /privacy, /pause, /resume, /delete_data, /lang, /feedback",
        "privacy": "TendAI no sustituye a un médico. Guardamos datos mínimos para recordatorios. Usa /delete_data para borrar tus datos.",
        "paused_on": "Recordatorios pausados. Usa /resume para activarlos.",
        "paused_off": "Recordatorios activados de nuevo.",
        "deleted": "Se eliminaron todos tus datos en TendAI. Puedes empezar otra vez con /start.",
        "ask_consent": "¿Puedo escribirte más tarde para saber cómo sigues?",
        "yes": "Sí", "no": "No",
        "choose_topic": "Elige un tema:",
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
        "checkin_worse": "Si hay señales de alarma o dolor ≥7/10, considera atención médica.",
        "comment_prompt": "Escribe tu comentario ahora. O envía /skip para omitir.",
        "comment_saved": "¡Comentario guardado, gracias! 🙌",
        "skip_ok": "Omitido.",
        "unknown": "Necesito un poco más de información. ¿Dónde te duele y desde cuándo?",
        "use_buttons": "Usa los botones abajo ⤵️",
        "lang_set": "Idioma guardado: Español",
        "help_lang": "Usa /lang ru|en|uk|es para cambiar el idioma.",
        "oos": "Entendido. Estoy para temas de salud, hábitos y autocuidado. Elige un tema abajo o cuéntame brevemente.",
        "fb_prompt": "¿Quieres dejar una opinión rápida?",
        "fb_thanks": "¡Gracias por tu opinión! 💛",
        "fb_like": "👍 Útil",
        "fb_dislike": "👎 No ayudó",
        "fb_write": "✍️ Escribir comentario",
    },
}
def t(lang: str, key: str) -> str:
    return T.get(lang, T["en"]).get(key, T["en"].get(key, key))

# =========================
# Reply-keyboards (bottom)
# =========================
BACK = "⬅️ Назад"
CANCEL = "❌ Отмена"

def _rkm(rows):  # helper
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def main_menu(lang: str) -> ReplyKeyboardMarkup:
    labels = T[lang]["menu"]
    rows = [labels[:3], labels[3:]]
    return _rkm(rows)

def kb_list_with_nav(options: list[str]) -> ReplyKeyboardMarkup:
    per_row = 3 if len(options) >= 6 else 2
    rows = [options[i:i+per_row] for i in range(0, len(options), per_row)]
    rows.append([BACK, CANCEL])
    return _rkm(rows)

def kb_numbers_0_10() -> ReplyKeyboardMarkup:
    nums = [str(i) for i in range(11)]
    rows = [nums[:6], nums[6:], [BACK, CANCEL]]
    return _rkm(rows)

def kb_confirm_bottom(lang: str) -> ReplyKeyboardMarkup:
    return _rkm([
        [t(lang, "confirm_ok")],
        [t(lang, "confirm_change_loc"), t(lang, "confirm_change_kind")],
        [t(lang, "confirm_change_duration"), t(lang, "confirm_change_severity")],
        [BACK, CANCEL]
    ])

def kb_accept_bottom(lang: str) -> ReplyKeyboardMarkup:
    acc = T[lang]["accept_opts"]
    return _rkm([acc, [BACK, CANCEL]])

def kb_remind_bottom(lang: str) -> ReplyKeyboardMarkup:
    opts = T[lang]["remind_opts"]
    per_row = 2
    rows = [opts[i:i+per_row] for i in range(0, len(opts), per_row)]
    rows.append([BACK, CANCEL])
    return _rkm(rows)

def kb_yes_no(lang: str) -> ReplyKeyboardMarkup:
    return _rkm([[t(lang,"yes"), t(lang,"no")], [CANCEL]])

def kb_feedback_bottom(lang: str) -> ReplyKeyboardMarkup:
    return _rkm([[t(lang,"fb_like"), t(lang,"fb_dislike")], [t(lang,"fb_write")], [CANCEL]])

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

def map_redflag_text(lang: str, text: str) -> str | None:
    tl = (text or "").strip().lower()
    none_words = {"ru": {"нет","ничего","none","не надо"},
                  "en": {"none","no"},
                  "uk": {"нема","ні","немає","none"},
                  "es": {"ninguno","ninguna","no","none"}}
    if tl in none_words.get(lang, set()):
        return "None"
    opts = T[lang]["triage_pain_q5_opts"]
    for o in opts:
        if o.lower() in tl:
            return o
    return None

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
# Topic detection
# =========================
TOPIC_KEYS = {
    "en": {"Pain": "pain", "Throat/Cold": "throat", "Sleep": "sleep", "Stress": "stress", "Digestion": "digestion", "Energy": "energy"},
    "ru": {"Боль": "pain", "Горло/простуда": "throat", "Сон": "sleep", "Стресс": "stress", "Пищеварение": "digestion", "Энергия": "energy"},
    "uk": {"Біль": "pain", "Горло/застуда": "throat", "Сон": "sleep", "Стрес": "stress", "Травлення": "digestion", "Енергія": "energy"},
    "es": {"Dolor": "pain", "Garganta/Resfriado": "throat", "Sueño": "sleep", "Estrés": "stress", "Digestión": "digestion", "Energía": "energy"},
}

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
# Plans / Hypotheses (простые правила)
# =========================
def build_hypotheses(lang: str, ans: dict, zone: dict) -> list[tuple[str, float, str]]:
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
        if "throbb" in kind or "пульс" in kind:
            add("Migraine-like", 0.7 + 0.05*(sev>=6), "Throbbing + moderate/severe")
        if "press" in kind or "tight" in kind or "дав" in kind:
            add("Tension-type", 0.6, "Pressing/tight character")
        if "3–24" in duration or ">1 day" in duration or ">1 дня" in duration:
            add("Tension/sinus", 0.4, "Lasts many hours")
        if zq.get(1) == "yes":
            add("Urgent risk pattern", 1.0, "Sudden worst-ever → escalate")
        if zq.get(2) == "yes":
            add("Neurologic red flag", 1.0, "Speech/vision/weakness → escalate")
        if zq.get(3) == "yes":
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

    H.sort(key=lambda x: x[1], reverse=True)
    return H[:3]

def pain_plan(lang: str, ans: dict, zone: dict, hypotheses: list[tuple[str,float,str]]) -> list[str]:
    red = (ans.get("red") or "").lower()
    urgent = any(s in red for s in ["fever", "vomit", "weakness", "speech", "vision", "травм", "trauma"]) and (ans.get("severity", 0) >= 7)
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
            reply_markup=kb_numbers_0_10(),
        )
        # пометим режим чек-ина для on_text
        ss = sessions.setdefault(uid, {})
        ss["await_step"] = "checkin"
        ss["episode_id"] = eid
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
        s = sessions.setdefault(user.id, {})
        s["awaiting_consent"] = True
        await update.message.reply_text(t(lang, "ask_consent"), reply_markup=kb_yes_no(lang))

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang, "help"), reply_markup=main_menu(lang))

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang, "privacy"), reply_markup=main_menu(lang))

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "yes")
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang, "paused_on"), reply_markup=main_menu(lang))

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "no")
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang, "paused_off"), reply_markup=main_menu(lang))

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
        await update.message.reply_text(t(lang, "help_lang"), reply_markup=main_menu(lang))
        return
    candidate = norm_lang(context.args[0])
    if candidate not in SUPPORTED:
        cur = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
        await update.message.reply_text(t(cur, "help_lang"), reply_markup=main_menu(cur))
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
        await update.message.reply_text(t(lang, "skip_ok"), reply_markup=main_menu(lang))
    else:
        lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
        await update.message.reply_text(t(lang, "use_buttons"), reply_markup=main_menu(lang))

async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = norm_lang(users_get(uid).get("lang") or getattr(update.effective_user,"language_code",None))
    s = sessions.setdefault(uid, {})
    s["awaiting_feedback_choice"] = True
    s["feedback_context"] = "general"
    await update.message.reply_text(t(lang, "fb_prompt"), reply_markup=kb_feedback_bottom(lang))

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
# FLOW HELPERS
# =========================
def next_missing_step(ans: dict) -> int:
    if "loc" not in ans: return 1
    if "kind" not in ans: return 2
    if "duration" not in ans: return 3
    if "severity" not in ans: return 4
    if "red" not in ans: return 5
    return 0

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

async def send_step_question_bottom(message, lang: str, s: dict, step: int):
    s["await_step"] = step
    if step == 1:
        await message.reply_text(t(lang, "triage_pain_q1"), reply_markup=kb_list_with_nav(T[lang]["triage_pain_q1_opts"]))
    elif step == 2:
        await message.reply_text(t(lang, "triage_pain_q2"), reply_markup=kb_list_with_nav(T[lang]["triage_pain_q2_opts"]))
    elif step == 3:
        await message.reply_text(t(lang, "triage_pain_q3"), reply_markup=kb_list_with_nav(T[lang]["triage_pain_q3_opts"]))
    elif step == 4:
        await message.reply_text(t(lang, "triage_pain_q4"), reply_markup=kb_numbers_0_10())
    elif step == 5:
        await message.reply_text(t(lang, "triage_pain_q5"), reply_markup=kb_list_with_nav(T[lang]["triage_pain_q5_opts"]))

async def start_pain_triage(update: Update, lang: str, uid: int):
    sessions[uid] = {"topic": "pain", "flow": "collect", "answers": {}}
    await send_step_question_bottom(update.message, lang, sessions[uid], 1)

async def proceed_to_confirm(message, lang: str, uid: int):
    s = sessions.setdefault(uid, {})
    ans = s.setdefault("answers", {})
    text = render_confirm(lang, ans)
    s["flow"] = "confirm"
    s["await_step"] = 0
    sessions[uid] = s
    await message.reply_text(text, reply_markup=kb_confirm_bottom(lang))

async def continue_collect(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str, uid: int, text_input: str):
    # оставляем гибридный парсер на будущее, если начнем без кнопок
    s = sessions.setdefault(uid, {"topic": "pain", "flow": "collect", "answers": {}})
    ans = s.setdefault("answers", {})
    llm_data = parse_with_llm(text_input, lang)
    if llm_data and llm_data.get("confidence", 0) >= 0.5:
        ans.update(normalize_llm_slots(llm_data, lang))
    slots = extract_slots(text_input, lang)
    for k, v in slots.items():
        ans.setdefault(k, v)
    if not ans:
        await update.message.reply_text(t(lang, "open_prompt"))
        return
    sessions[uid] = s
    await proceed_to_confirm(update.message, lang, uid)

# =========================
# Callback (совместимость, не используется в новом потоке)
# =========================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Оставлено для обратной совместимости, если старые сообщения с inline-кнопками ещё есть в чате.
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Обновление: используйте кнопки внизу 👇")

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

    s = sessions.get(uid, {})

    # Согласие на напоминания (нижние кнопки)
    if s.get("awaiting_consent"):
        if text == CANCEL:
            s["awaiting_consent"] = False
            await update.message.reply_text(t(lang, "thanks"), reply_markup=main_menu(lang))
            return
        if text in {t(lang,"yes"), t(lang,"no")}:
            users_set(uid, "consent", "yes" if text == t(lang,"yes") else "no")
            s["awaiting_consent"] = False
            await update.message.reply_text(t(lang, "thanks"), reply_markup=main_menu(lang))
            return
        await update.message.reply_text(t(lang, "use_buttons"))
        return

    # Фидбек (нижние кнопки)
    if s.get("awaiting_feedback_choice"):
        if text == CANCEL:
            s["awaiting_feedback_choice"] = False
            await update.message.reply_text(t(lang, "thanks"), reply_markup=main_menu(lang))
            return
        if text == t(lang,"fb_like"):
            ws_feedback.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), str(uid), s.get("feedback_context","general"), user.username or "", "1", ""])
            s["awaiting_feedback_choice"] = False
            await update.message.reply_text(t(lang, "fb_thanks"), reply_markup=main_menu(lang))
            return
        if text == t(lang,"fb_dislike"):
            ws_feedback.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), str(uid), s.get("feedback_context","general"), user.username or "", "0", ""])
            s["awaiting_feedback_choice"] = False
            await update.message.reply_text(t(lang, "fb_thanks"), reply_markup=main_menu(lang))
            return
        if text == t(lang,"fb_write"):
            s["awaiting_feedback_choice"] = False
            s["awaiting_comment"] = True
            await update.message.reply_text(t(lang, "comment_prompt"))
            return
        await update.message.reply_text(t(lang, "use_buttons")); return

    # ждём комментарий?
    if s.get("awaiting_comment") and not text.startswith("/"):
        name = s.get("feedback_context") or "general"
        ws_feedback.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), str(uid), f"comment:{name}", user.username or "", "", text])
        s["awaiting_comment"] = False
        s["feedback_context"] = ""
        sessions[uid] = s
        await update.message.reply_text(t(lang, "comment_saved"), reply_markup=main_menu(lang))
        return

    # простые приветы → меню
    if text.lower() in {"hi","hello","hey","hola","привет","здравствуйте","привіт","вітаю","buenas"}:
        await update.message.reply_text(t(lang, "welcome"), reply_markup=main_menu(lang))
        return

    # оффтоп?
    if not is_care_related(lang, text):
        await update.message.reply_text(t(lang, "oos"), reply_markup=main_menu(lang))
        return

    # ===== Нижние кнопки шагов триажа =====
    if s.get("topic") == "pain" and s.get("await_step"):
        step = s["await_step"]
        ans = s.setdefault("answers", {})

        # Навигация
        if text == CANCEL:
            sessions.pop(uid, None)
            await update.message.reply_text(t(lang, "thanks"), reply_markup=main_menu(lang))
            return
        if text == BACK:
            prev = max(1, step - 1) if isinstance(step, int) else 1
            await send_step_question_bottom(update.message, lang, s, prev)
            return

        # Шаг 1 — где болит (принимаем и свободный ввод)
        if step == 1:
            if text in T[lang]["triage_pain_q1_opts"]:
                ans["loc"] = text
            else:
                slots = extract_slots(text, lang)
                if slots.get("loc"):
                    ans["loc"] = slots["loc"]
                else:
                    await update.message.reply_text(t(lang, "use_buttons")); return
            s["await_step"] = 2
            await send_step_question_bottom(update.message, lang, s, 2); return

        # Шаг 2 — характер боли
        if step == 2:
            if text in T[lang]["triage_pain_q2_opts"]:
                ans["kind"] = text
            else:
                slots = extract_slots(text, lang)
                if slots.get("kind"):
                    ans["kind"] = slots["kind"]
                else:
                    await update.message.reply_text(t(lang, "use_buttons")); return
            s["await_step"] = 3
            await send_step_question_bottom(update.message, lang, s, 3); return

        # Шаг 3 — длительность
        if step == 3:
            if text in T[lang]["triage_pain_q3_opts"]:
                ans["duration"] = text
            else:
                dur = _match_duration(text, lang)
                if dur:
                    ans["duration"] = dur
                else:
                    await update.message.reply_text(t(lang, "use_buttons")); return
            s["await_step"] = 4
            await send_step_question_bottom(update.message, lang, s, 4); return

        # Шаг 4 — интенсивность 0–10
        if step == 4:
            sev = _match_severity(text) if not text.isdigit() else int(text)
            if isinstance(sev, int) and 0 <= sev <= 10:
                ans["severity"] = sev
                s["await_step"] = 0
                s["flow"] = "confirm"
                await update.message.reply_text(render_confirm(lang, ans), reply_markup=kb_confirm_bottom(lang))
                return
            await update.message.reply_text(t(lang, "use_buttons")); return

        # Шаг 5 — красные флаги
        if step == 5:
            rf = map_redflag_text(lang, text)
            if rf:
                ans["red"] = rf
                s["await_step"] = 0
                # зона опустим; сразу план
                zname = "general"
                s["zone"] = {"name": zname, "idx": 1, "q": {}}
                hyps = build_hypotheses(lang, ans, s.get("zone", {}))
                eid = s.get("episode_id") or episode_create(uid, "pain", int(ans.get("severity",5)), ans.get("red","None"))
                s["episode_id"] = eid
                plan_lines = pain_plan(lang, ans, s.get("zone", {}), hyps)
                await update.message.reply_text(f"{t(lang,'plan_header')}\n" + "\n".join(plan_lines))
                await update.message.reply_text(t(lang, "plan_accept"), reply_markup=kb_accept_bottom(lang))
                s["flow"] = "accept_wait"
                return
            await update.message.reply_text(t(lang, "use_buttons")); return

        # чек-ин — ловим здесь же
        if step == "checkin":
            if text.isdigit() and 0 <= int(text) <= 10:
                val = int(text)
                ep = episode_find_open(uid)
                if ep:
                    eid = ep.get("episode_id")
                    episode_set(eid, "notes", f"checkin:{val}")
                    if val <= 3:
                        episode_set(eid, "status", "resolved")
                        await update.message.reply_text(t(lang, "checkin_better"), reply_markup=main_menu(lang))
                    else:
                        await update.message.reply_text(t(lang, "checkin_worse"), reply_markup=main_menu(lang))
                s["await_step"] = 0
                return
            await update.message.reply_text(t(lang, "use_buttons")); return

    # Подтверждение (нижние кнопки)
    if s.get("topic") == "pain" and s.get("flow") == "confirm":
        ans = s.setdefault("answers", {})
        if text == t(lang, "confirm_ok"):
            s["flow"] = "redflags"
            await send_step_question_bottom(update.message, lang, s, 5)
            return
        map_change = {
            t(lang,"confirm_change_loc"): 1,
            t(lang,"confirm_change_kind"): 2,
            t(lang,"confirm_change_duration"): 3,
            t(lang,"confirm_change_severity"): 4,
        }
        if text in map_change:
            await send_step_question_bottom(update.message, lang, s, map_change[text])
            return
        if text in (BACK, CANCEL):
            await update.message.reply_text(t(lang, "use_buttons"), reply_markup=main_menu(lang))
            return

    # Принятие плана (нижние кнопки)
    if s.get("topic") == "pain" and s.get("flow") == "accept_wait":
        acc = T[lang]["accept_opts"]
        if text in acc:
            choice = text
            eid = s.get("episode_id")
            if choice == acc[0]:
                episode_set(eid, "plan_accepted", "1")
            elif choice == acc[1]:
                episode_set(eid, "plan_accepted", "later")
            else:
                episode_set(eid, "plan_accepted", "0")
            s["flow"] = "remind_wait"
            await update.message.reply_text(t(lang, "remind_when"), reply_markup=kb_remind_bottom(lang))
            return
        await update.message.reply_text(t(lang, "use_buttons")); return

    # Настройка напоминания (нижние кнопки)
    if s.get("topic") == "pain" and s.get("flow") == "remind_wait":
        opts = T[lang]["remind_opts"]
        if text in opts:
            code_map = {
                opts[0]: "4h",
                opts[1]: "evening",
                opts[2]: "morning",
                opts[3]: "none",
            }
            code = code_map[text]
            eid = s.get("episode_id")
            urec = users_get(uid)
            tz_off = 0
            try: tz_off = int(urec.get("tz_offset") or "0")
            except: tz_off = 0
            now_utc = utcnow(); user_now = now_utc + timedelta(hours=tz_off)
            if code == "4h": target_user = user_now + timedelta(hours=4)
            elif code == "evening":
                target_user = user_now.replace(hour=19, minute=0, second=0, microsecond=0)
                if target_user < user_now: target_user += timedelta(days=1)
            elif code == "morning":
                target_user = user_now.replace(hour=9, minute=0, second=0, microsecond=0)
                if target_user < user_now: target_user += timedelta(days=1)
            else:
                target_user = None
            if target_user:
                target_utc = target_user - timedelta(hours=tz_off)
                episode_set(eid, "next_checkin_at", iso(target_utc))
                delay = max(60, (target_utc - now_utc).total_seconds())
                context.job_queue.run_once(job_checkin, when=delay, data={"user_id": uid, "episode_id": eid})
            await update.message.reply_text(t(lang, "thanks"), reply_markup=main_menu(lang))
            s["flow"] = "plan"
            return
        await update.message.reply_text(t(lang, "use_buttons")); return

    # Новая тема → триаж
    topic = detect_or_choose_topic(lang, text) or "pain"
    if topic in {"pain","throat","sleep","stress","digestion","energy"}:
        await start_pain_triage(update, lang, uid)
        return

    # Фолбэк
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

    # Совместимость с инлайн-колбэками из старых сообщений
    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
