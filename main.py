# -*- coding: utf-8 -*-
import os
import re
import json
import uuid
import logging
from time import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

# langdetect — по возможности
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
    CallbackQueryHandler,  # для совместимости со старыми сообщениями
    ContextTypes,
    filters,
)

# ===== OpenAI =====
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
#   "topic": "pain" | ...,
#   "flow": "chat"|"confirm"|"accept_wait"|"remind_wait"|"plan",
#   "answers": {"loc","kind","duration","severity","red"},
#   "await_step": int|str,  # "checkin"
#   "episode_id": "...",
#   "chat_history": [{"role":"user/assistant","content":"..."}],
#   "awaiting_comment": bool,
#   "awaiting_feedback_choice": bool,
#   "awaiting_consent": bool,
#   "feedback_context": str,
#   "last_send": {key: ts},
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

# ===== Reply-keyboards (bottom) =====
BACK = "⬅️ Назад"
CANCEL = "❌ Отмена"

def _rkm(rows):
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def main_menu(lang: str) -> ReplyKeyboardMarkup:
    labels = T[lang]["menu"]
    rows = [labels[:3], labels[3:]]
    return _rkm(rows)

def kb_numbers_0_10() -> ReplyKeyboardMarkup:
    nums = [str(i) for i in range(11)]
    rows = [nums[:6], nums[6:], [CANCEL]]
    return _rkm(rows)

def kb_accept_bottom(lang: str) -> ReplyKeyboardMarkup:
    acc = T[lang]["accept_opts"]
    return _rkm([acc, [CANCEL]])

def kb_remind_bottom(lang: str) -> ReplyKeyboardMarkup:
    opts = T[lang]["remind_opts"]
    per_row = 2
    rows = [opts[i:i+per_row] for i in range(0, len(opts), per_row)]
    rows.append([CANCEL])
    return _rkm(rows)

def kb_yes_no(lang: str) -> ReplyKeyboardMarkup:
    return _rkm([[t(lang,"yes"), t(lang,"no")], [CANCEL]])

def kb_feedback_bottom(lang: str) -> ReplyKeyboardMarkup:
    return _rkm([[t(lang,"fb_like"), t(lang,"fb_dislike")], [t(lang,"fb_write")], [CANCEL]])

# =========================
# NLP helpers
# =========================
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
def _match_duration(text: str, lang: str) -> str | None:
    m = re.search(DUR_PATTERNS.get(lang, ""), (text or "").lower())
    if not m: return None
    return f"{m.group(1)} {m.group(2)}"
def _match_severity(text: str) -> int | None:
    tl = (text or "").lower()
    for pat in SEVERITY_PATTERNS:
        m = re.search(pat, tl)
        if m:
            try:
                val = int(m.group(1))
                if 0 <= val <= 10: return val
            except Exception:
                pass
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
# Helpers: send-once (анти-дубль)
# =========================
def send_once(uid: int, key: str, now_ts: float, cooldown: float = 8.0) -> bool:
    s = sessions.setdefault(uid, {})
    last = s.get("last_send", {})
    ts = last.get(key, 0.0)
    if now_ts - ts >= cooldown:
        last[key] = now_ts
        s["last_send"] = last
        sessions[uid] = s
        return True
    return False

# =========================
# GPT-5 CHAT ROUTER
# =========================
def llm_chat_reply(uid: int, lang: str, user_text: str) -> dict:
    """
    Возвращает:
    {
      "assistant": "текст ответа для пользователя",
      "slots": {"intent","loc","kind","duration","severity","red"},
      "plan_ready": bool,
      "escalate": bool
    }
    """
    if not oai or not user_text:
        return {}

    # компактная история (последние 8 сообщений)
    s = sessions.setdefault(uid, {})
    hist = s.setdefault("chat_history", [])[-8:]
    sys = (
        "You are TendAI, a warm health & self-care assistant. "
        "Speak briefly (max 4 sentences), supportive, no diagnoses. "
        "If you detect urgent red flags, advise medical help immediately. "
        "Your task: both talk naturally AND extract triage fields. "
        "Answer in user's language. "
        "Return ONLY a JSON object with keys: "
        "assistant (string), plan_ready (bool), escalate (bool), "
        "slots (object with optional keys: intent in [pain, throat, sleep, stress, digestion, energy]; "
        "loc in [Head, Throat, Back, Belly, Chest, Other]; "
        "kind in [Dull, Sharp, Throbbing, Burning, Pressing]; "
        "duration one of [\"<3h\",\"3–24h\",\">1 day\",\">1 week\"] or a human string; "
        "severity int 0..10; "
        "red one of [\"High fever\",\"Vomiting\",\"Weakness/numbness\",\"Speech/vision issues\",\"Trauma\",\"None\"])."
    )
    msgs = [{"role": "system", "content": sys}]
    for m in hist:
        msgs.append(m)
    msgs.append({"role": "user", "content": f"[lang={lang}] {user_text}"})

    try:
        resp = oai.chat.completions.create(
            model="gpt-5",
            temperature=0.2,
            max_tokens=400,
            messages=msgs,
        )
        raw = (resp.choices[0].message.content or "").strip()
        j = None
        try:
            m = re.search(r"\{[\s\S]*\}", raw)
            j = json.loads(m.group(0)) if m else json.loads(raw)
        except Exception:
            j = {"assistant": raw, "plan_ready": False, "escalate": False, "slots": {}}
        # сохраняем в историю
        hist.append({"role": "user", "content": user_text})
        hist.append({"role": "assistant", "content": j.get("assistant","")})
        s["chat_history"] = hist[-10:]
        sessions[uid] = s
        return j
    except Exception as e:
        logging.warning(f"LLM chat failed: {e}")
        return {}

# =========================
# Hypotheses & Plans
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

    if "back" in loc or "спина" in loc or "espalda" in loc:
        if "shoot" in kind or "прострел" in kind or zq.get(3) == "yes":
            add("Radicular pain (sciatica-like)", 0.7, "Shooting below knee/‘прострел’")
        else:
            add("Mechanical low back pain", 0.6, "Typical pattern without red flags")
        if zq.get(1) == "yes" or zq.get(2) == "yes":
            add("Serious back red flag", 0.95, "Perineal numbness/retention or trauma/fever/cancer")

    if "belly" in loc or "живот" in loc or "abdomen" in loc or "vientre" in loc or "stomach" in loc:
        if "vomit" in (ans.get("red","") or "").lower():
            add("Gastroenteritis-like", 0.6, "Nausea/vomiting")
        add("Dyspepsia/gastritis-like", 0.5, "Common benign causes if no red flags")

    if "chest" in loc or "груд" in loc or "pecho" in loc:
        if zq.get(1) == "yes":
            add("Possible cardiac pattern", 1.0, "Pressure >10min + dyspnea/sweat")
        elif zq.get(2) == "yes":
            add("Pleuritic/musculoskeletal", 0.7, "Worse with breathing/movement/press")
        elif zq.get(3) == "yes":
            add("Respiratory infection", 0.6, "Cough/fever")

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
    "en": {"pain","headache","throat","cough","cold","fever","back","belly","stomach","chest",
           "sleep","insomnia","stress","anxiety","energy","fatigue","digestion","diarrhea","constipation",
           "nausea","vomit","symptom","medicine","ibuprofen","health","wellness"},
    "ru": {"боль","болит","голова","горло","кашель","простуда","температура","жар","спина","живот","желудок","грудь",
           "сон","бессонница","стресс","тревога","энергия","слабость","пищеварение","диарея","понос","запор",
           "тошнота","рвота","симптом","здоровье","ибупрофен"},
    "uk": {"біль","болить","голова","горло","кашель","застуда","температура","жар","спина","живіт","шлунок","груди",
           "сон","безсоння","стрес","тривога","енергія","слабкість","травлення","діарея","запор","нудота",
           "блювання","симптом","здоров'я","ібупрофен"},
    "es": {"dolor","cabeza","garganta","tos","resfriado","fiebre","espalda","vientre","estómago","pecho","sueño",
           "insomnio","estrés","ansiedad","energía","cansancio","digestión","diarrea","estreñimiento","náusea",
           "vómito","síntoma","salud","ibuprofeno"},
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

# =========================
# Callback (совместимость)
# =========================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    s = sessions.setdefault(uid, {"flow": "chat", "answers": {}, "chat_history": []})

    # Согласие на напоминания
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

    # Фидбек
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
        await update.message.reply_text(t(lang, "comment_saved"), reply_markup=main_menu(lang))
        return

    # чек-ин режим
    if s.get("await_step") == "checkin":
        if text == CANCEL:
            s["await_step"] = 0
            await update.message.reply_text(t(lang, "thanks"), reply_markup=main_menu(lang))
            return
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

    # принятие плана
    if s.get("flow") == "accept_wait":
        acc = T[lang]["accept_opts"]
        if text in acc or text == CANCEL:
            eid = s.get("episode_id")
            if text == acc[0]:
                episode_set(eid, "plan_accepted", "1")
            elif text == acc[1]:
                episode_set(eid, "plan_accepted", "later")
            elif text == acc[2]:
                episode_set(eid, "plan_accepted", "0")
            s["flow"] = "remind_wait"
            await update.message.reply_text(t(lang, "remind_when"), reply_markup=kb_remind_bottom(lang))
            return
        await update.message.reply_text(t(lang, "use_buttons")); return

    # настройка напоминания
    if s.get("flow") == "remind_wait":
        opts = T[lang]["remind_opts"]
        if text in opts or text == CANCEL:
            code_map = {
                opts[0]: "4h",
                opts[1]: "evening",
                opts[2]: "morning",
                opts[3]: "none",
            }
            code = code_map.get(text, "none")
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

    # ===== CHAT-FIRST на GPT-5 =====
    # если совсем оффтоп — один мягкий ответ и меню
    if not is_care_related(lang, text):
        if send_once(uid, key="oos", now_ts=time()):
            await update.message.reply_text(t(lang, "oos"), reply_markup=main_menu(lang))
        return

    # обращаемся к модели
    j = llm_chat_reply(uid, lang, text)
    if not j:
        # фолбэк: краткая просьба уточнить
        await update.message.reply_text(t(lang, "unknown"), reply_markup=main_menu(lang))
        return

    assistant_text = j.get("assistant") or ""
    slots = j.get("slots") or {}
    plan_ready = bool(j.get("plan_ready"))
    escalate = bool(j.get("escalate"))

    # отправляем человеческий ответ
    if assistant_text:
        await update.message.reply_text(assistant_text)

    # обновляем ответы
    ans = s.setdefault("answers", {})
    for k in ["intent","loc","kind","duration","severity","red"]:
        v = slots.get(k)
        if v is not None and v != "":
            ans[k] = v
    sessions[uid] = s

    # если эскалация — завершаем
    if escalate:
        # короткая подсказка уже есть в assistant_text; просто вернём меню
        await update.message.reply_text(t(lang, "thanks"), reply_markup=main_menu(lang))
        return

    # если готовы к плану (по мнению модели) или у нас есть все поля
    have_all = all(k in ans for k in ["loc","kind","duration","severity","red"])
    if plan_ready or have_all:
        # создаём эпизод при необходимости
        eid = s.get("episode_id")
        if not eid:
            eid = episode_create(uid, ans.get("intent","pain"), int(ans.get("severity",5)), ans.get("red","None"))
            s["episode_id"] = eid
        # гипотезы + план
        hyps = build_hypotheses(lang, ans, s.get("zone", {}))
        plan_lines = pain_plan(lang, ans, s.get("zone", {}), hyps)
        await update.message.reply_text(f"{t(lang,'plan_header')}\n" + "\n".join(plan_lines))
        await update.message.reply_text(t(lang, "plan_accept"), reply_markup=kb_accept_bottom(lang))
        s["flow"] = "accept_wait"
        return

    # иначе — остаёмся в беседе (модель уже задала следующий вопрос)
    s["flow"] = "chat"
    sessions[uid] = s

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

    app.add_handler(CallbackQueryHandler(on_callback))  # совместимость

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
