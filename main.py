# -*- coding: utf-8 -*-
import os
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from langdetect import detect, DetectorFactory

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

from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ---------------------------
# Boot & Config
# ---------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO)
DetectorFactory.seed = 0  # детерминируем langdetect

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
SHEET_NAME = os.getenv("SHEET_NAME", "TendAI Feedback")

# OpenAI клиент (используем ТОЛЬКО как фолбэк)
oai = None
if OPENAI_API_KEY:
    try:
        oai = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        logging.error(f"OpenAI init error: {e}")
        oai = None

# Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not creds_json:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")
creds_dict = json.loads(creds_json)
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gclient = gspread.authorize(credentials)

# Создаём/открываем книгу и листы
ss = gclient.open(SHEET_NAME)

def _get_or_create_ws(title: str, headers: list[str]):
    try:
        ws = ss.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=1000, cols=20)
        ws.append_row(headers)
    # если пустой — добавим заголовки
    vals = ws.get_all_values()
    if not vals:
        ws.append_row(headers)
    return ws

ws_feedback = _get_or_create_ws(
    "Feedback",
    ["timestamp", "user_id", "name", "username", "rating", "comment"],
)

ws_users = _get_or_create_ws(
    "Users",
    ["user_id", "username", "lang", "consent", "tz_offset", "checkin_hour", "paused"],
)

ws_episodes = _get_or_create_ws(
    "Episodes",
    [
        "episode_id",
        "user_id",
        "topic",
        "started_at",
        "baseline_severity",
        "red_flags",
        "plan_accepted",
        "target",
        "reminder_at",
        "next_checkin_at",
        "status",
        "last_update",
        "notes",
    ],
)

# ---------------------------
# In-memory session state
# ---------------------------
# короткие состояния для сценариев (не долговременная память)
sessions: dict[int, dict] = {}  # user_id -> {...}

# ---------------------------
# i18n
# ---------------------------
SUPPORTED = {"ru", "en", "uk"}  # русский, английский, українська

def norm_lang(code: str | None) -> str:
    if not code:
        return "en"
    c = code.split("-")[0].lower()
    if c in SUPPORTED:
        return c
    return "en"

T = {
    "en": {
        "welcome": "Hi! I’m TendAI — your health & longevity assistant.\nChoose a topic below or just describe what’s bothering you.",
        "menu": ["Pain", "Throat/Cold", "Sleep", "Stress", "Digestion", "Energy"],
        "help": "I can help with short checkups, a simple 24–48h plan, and gentle follow-ups.\nCommands:\n/help, /privacy, /pause, /resume, /delete_data",
        "privacy": "TendAI is not a medical service and can’t replace a doctor.\nWe store minimal data to support reminders.\nUse /delete_data to erase your info.",
        "paused_on": "Notifications paused. Use /resume to enable.",
        "paused_off": "Notifications resumed.",
        "deleted": "All your data in TendAI was deleted. You can /start again anytime.",
        "ask_consent": "May I send you a follow-up later to check how you feel? (You can change with /pause or /resume.)",
        "yes": "Yes",
        "no": "No",
        "choose_topic": "Choose a topic:",
        "triage_pain_q1": "Where does it hurt?",
        "triage_pain_q1_opts": ["Head", "Throat", "Back", "Belly", "Other"],
        "triage_pain_q2": "What kind of pain?",
        "triage_pain_q2_opts": ["Dull", "Sharp", "Pulsating", "Pressing"],
        "triage_pain_q3": "How long has it lasted?",
        "triage_pain_q3_opts": ["<3h", "3–24h", ">1 day", ">1 week"],
        "triage_pain_q4": "Rate the pain now (0–10):",
        "triage_pain_q5": "Any of these now?",
        "triage_pain_q5_opts": ["High fever", "Vomiting", "Weakness or numbness", "Speech/vision problems", "Trauma", "None"],
        "plan_header": "Your 24–48h plan:",
        "plan_accept": "Will you try this today?",
        "accept_opts": ["✅ Yes", "🔁 Later", "✖️ No"],
        "remind_when": "When shall I check on you?",
        "remind_opts": ["in 4h", "this evening", "tomorrow morning", "no need"],
        "thanks": "Got it 🙌",
        "checkin_ping": "Quick check-in: how is it now (0–10)?",
        "checkin_better": "Nice! Keep it up 💪",
        "checkin_worse": "Sorry to hear. If you have any red flags or pain ≥7/10, consider seeking medical help.",
        "comment_prompt": "Thanks for your rating 🙏\nWant to add a comment? Just type it in, or send /skip to skip.",
        "comment_saved": "Comment saved, thank you! 🙌",
        "skip_ok": "Skipped.",
        "unknown": "I need a bit more information to help. Where exactly does it hurt? How long has it lasted?",
        "lang_switched": "OK, I’ll reply in English next time.",
    },
    "ru": {
        "welcome": "Привет! Я TendAI — ассистент здоровья и долголетия.\nВыбери тему ниже или опиши, что беспокоит.",
        "menu": ["Боль", "Горло/простуда", "Сон", "Стресс", "Пищеварение", "Энергия"],
        "help": "Я помогаю короткой проверкой, планом на 24–48 ч и заботливыми чек-инами.\nКоманды:\n/help, /privacy, /pause, /resume, /delete_data",
        "privacy": "TendAI не заменяет врача. Мы храним минимум данных для напоминаний.\nКоманда /delete_data удалит всё.",
        "paused_on": "Напоминания поставлены на паузу. Включить: /resume",
        "paused_off": "Напоминания снова включены.",
        "deleted": "Все ваши данные в TendAI удалены. Можно заново начать через /start.",
        "ask_consent": "Можно прислать напоминание позже, чтобы узнать, как вы? (Можно менять /pause и /resume.)",
        "yes": "Да",
        "no": "Нет",
        "choose_topic": "Выберите тему:",
        "triage_pain_q1": "Где болит?",
        "triage_pain_q1_opts": ["Голова", "Горло", "Спина", "Живот", "Другое"],
        "triage_pain_q2": "Какой характер боли?",
        "triage_pain_q2_opts": ["Тупая", "Острая", "Пульсирующая", "Давящая"],
        "triage_pain_q3": "Как долго длится?",
        "triage_pain_q3_opts": ["<3ч", "3–24ч", ">1 дня", ">1 недели"],
        "triage_pain_q4": "Оцените боль (0–10):",
        "triage_pain_q5": "Есть что-то из этого?",
        "triage_pain_q5_opts": ["Высокая температура", "Рвота", "Слабость/онемение", "Нарушение речи/зрения", "Травма", "Нет"],
        "plan_header": "Ваш план на 24–48 часов:",
        "plan_accept": "Готовы попробовать сегодня?",
        "accept_opts": ["✅ Да", "🔁 Позже", "✖️ Нет"],
        "remind_when": "Когда напомнить и спросить самочувствие?",
        "remind_opts": ["через 4 часа", "вечером", "завтра утром", "не надо"],
        "thanks": "Принято 🙌",
        "checkin_ping": "Коротко: как сейчас по шкале 0–10?",
        "checkin_better": "Отлично! Продолжаем 💪",
        "checkin_worse": "Сочувствую. Если есть «красные флаги» или боль ≥7/10 — лучше обратиться к врачу.",
        "comment_prompt": "Спасибо за оценку 🙏\nХотите добавить комментарий? Просто напишите его, или отправьте /skip, чтобы пропустить.",
        "comment_saved": "Комментарий сохранён, спасибо! 🙌",
        "skip_ok": "Пропустили.",
        "unknown": "Нужно чуть больше деталей. Где болит и сколько длится?",
        "lang_switched": "Ок, дальше отвечаю по-русски.",
    },
    "uk": {
        "welcome": "Привіт! Я TendAI — асистент здоров’я та довголіття.\nОбери тему нижче або опиши, що турбує.",
        "menu": ["Біль", "Горло/застуда", "Сон", "Стрес", "Травлення", "Енергія"],
        "help": "Допомагаю короткою перевіркою, планом на 24–48 год та чеками.\nКоманди:\n/help, /privacy, /pause, /resume, /delete_data",
        "privacy": "TendAI не замінює лікаря. Зберігаємо мінімум даних для нагадувань.\nКоманда /delete_data видалить усе.",
        "paused_on": "Нагадування призупинені. Увімкнути: /resume",
        "paused_off": "Нагадування знову увімкнені.",
        "deleted": "Усі ваші дані в TendAI видалено. Можна почати знову через /start.",
        "ask_consent": "Можу написати пізніше, щоб дізнатися, як ви? (Можна змінити /pause або /resume.)",
        "yes": "Так",
        "no": "Ні",
        "choose_topic": "Оберіть тему:",
        "triage_pain_q1": "Де болить?",
        "triage_pain_q1_opts": ["Голова", "Горло", "Спина", "Живіт", "Інше"],
        "triage_pain_q2": "Який характер болю?",
        "triage_pain_q2_opts": ["Тупий", "Гострий", "Пульсуючий", "Тиснучий"],
        "triage_pain_q3": "Як довго триває?",
        "triage_pain_q3_opts": ["<3год", "3–24год", ">1 дня", ">1 тижня"],
        "triage_pain_q4": "Оцініть біль (0–10):",
        "triage_pain_q5": "Є щось із цього?",
        "triage_pain_q5_opts": ["Висока температура", "Блювання", "Слабкість/оніміння", "Проблеми з мовою/зором", "Травма", "Немає"],
        "plan_header": "Ваш план на 24–48 год:",
        "plan_accept": "Готові спробувати сьогодні?",
        "accept_opts": ["✅ Так", "🔁 Пізніше", "✖️ Ні"],
        "remind_when": "Коли нагадати та спитати самопочуття?",
        "remind_opts": ["через 4 год", "увечері", "завтра вранці", "не треба"],
        "thanks": "Прийнято 🙌",
        "checkin_ping": "Коротко: як зараз за шкалою 0–10?",
        "checkin_better": "Чудово! Продовжуємо 💪",
        "checkin_worse": "Шкода. Якщо є «червоні прапорці» або біль ≥7/10 — краще звернутися до лікаря.",
        "comment_prompt": "Дякую за оцінку 🙏\nДодати коментар? Просто напишіть, або надішліть /skip, щоб пропустити.",
        "comment_saved": "Коментар збережено, дякую! 🙌",
        "skip_ok": "Пропущено.",
        "unknown": "Потрібно трохи більше деталей. Де болить і скільки триває?",
        "lang_switched": "Ок, надалі відповідатиму українською.",
    },
}

def t(lang: str, key: str) -> str:
    return T.get(lang, T["en"]).get(key, T["en"].get(key, key))

# ---------------------------
# Sheets helpers
# ---------------------------
def utcnow():
    return datetime.now(timezone.utc)

def iso(dt: datetime | None) -> str:
    if not dt:
        return ""
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
    if idx:
        ws_users.update(f"A{idx}:G{idx}", [[str(user_id), username or "", lang, "no", "0", "", "no"]])
    else:
        ws_users.append_row([str(user_id), username or "", lang, "no", "0", "", "no"])

def users_set(user_id: int, field: str, value: str):
    idx = users_get_row_index(user_id)
    if not idx:
        return
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
    if field not in headers:
        return
    col = headers.index(field) + 1
    for i in range(2, len(vals) + 1):
        if ws_episodes.cell(i, 1).value == eid:
            ws_episodes.update_cell(i, col, value)
            ws_episodes.update_cell(i, headers.index("last_update") + 1, iso(utcnow()))
            return

def schedule_from_sheet_on_start(app):
    """При запуске перечитываем незавершённые эпизоды и восстанавливаем чек-ины."""
    vals = ws_episodes.get_all_records()
    now = utcnow()
    for row in vals:
        if row.get("status") != "open":
            continue
        eid = row.get("episode_id")
        uid = int(row.get("user_id"))
        nca = row.get("next_checkin_at") or ""
        if not nca:
            continue
        try:
            # формат: "YYYY-mm-dd HH:MM:SS+0000"
            dt = datetime.strptime(nca, "%Y-%m-%d %H:%M:%S%z")
        except Exception:
            continue
        delay = (dt - now).total_seconds()
        if delay < 60:
            delay = 60  # если просрочено — напомним через минуту
        app.job_queue.run_once(job_checkin, when=delay, data={"user_id": uid, "episode_id": eid})

# ---------------------------
# Scenarios (pain + generic)
# ---------------------------
TOPIC_KEYS = {
    "en": {"Pain": "pain", "Throat/Cold": "throat", "Sleep": "sleep", "Stress": "stress", "Digestion": "digestion", "Energy": "energy"},
    "ru": {"Боль": "pain", "Горло/простуда": "throat", "Сон": "sleep", "Стресс": "stress", "Пищеварение": "digestion", "Энергия": "energy"},
    "uk": {"Біль": "pain", "Горло/застуда": "throat", "Сон": "sleep", "Стрес": "stress", "Травлення": "digestion", "Енергія": "energy"},
}

def main_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([T[lang]["menu"]], resize_keyboard=True)

def numeric_keyboard_0_10(lang: str) -> ReplyKeyboardMarkup:
    row1 = [str(i) for i in range(0, 6)]
    row2 = [str(i) for i in range(6, 11)]
    return ReplyKeyboardMarkup([row1, row2], resize_keyboard=True, one_time_keyboard=True)

def accept_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([T[lang]["accept_opts"]], resize_keyboard=True, one_time_keyboard=True)

def remind_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([T[lang]["remind_opts"]], resize_keyboard=True, one_time_keyboard=True)

# ---------------------------
# Planning helpers
# ---------------------------
def pain_plan(lang: str, red_flags_selected: list[str]) -> list[str]:
    # простой план для боли без красных флагов
    if lang == "ru":
        lines = [
            "1) Вода 400–600 мл, 15–20 мин отдыха в тихой комнате.",
            "2) Если нет противопоказаний — ибупрофен 200–400 мг однократно с едой.",
            "3) Проветрить комнату и уменьшить экран на 30–60 мин.",
            "Следить: к вечеру боль ≤3/10.",
            "К врачу: внезапная «самая сильная» боль, после травмы, с рвотой/нарушением речи/зрения/онемением — срочно.",
        ]
    elif lang == "uk":
        lines = [
            "1) Вода 400–600 мл, 15–20 хв відпочинку в тихій кімнаті.",
            "2) Якщо немає протипоказань — ібупрофен 200–400 мг одноразово з їжею.",
            "3) Провітрити кімнату та зменшити екран на 30–60 хв.",
            "Стежити: до вечора біль ≤3/10.",
            "До лікаря: раптовий «найсильніший» біль, після травми, з блюванням/порушенням мови/зору/онімінням — негайно.",
        ]
    else:
        lines = [
            "1) Drink 400–600 ml water and rest 15–20 minutes in a quiet room.",
            "2) If no contraindications — ibuprofen 200–400 mg once with food.",
            "3) Air the room and reduce screen time 30–60 minutes.",
            "Monitor: by evening pain ≤3/10.",
            "See a doctor: sudden “worst ever” pain, after trauma, with vomiting/speech/vision issues/numbness — urgently.",
        ]
    if any(s for s in red_flags_selected if s and s.lower() not in ["none", "нет", "немає"]):
        # Если есть флаги — более строгий посыл
        if lang == "ru":
            lines = ["⚠️ Обнаружены тревожные признаки. Лучше как можно скорее оцениться у врача / скорой помощи."]
        elif lang == "uk":
            lines = ["⚠️ Є тривожні ознаки. Краще якнайшвидше оцінитися у лікаря / швидкої."]
        else:
            lines = ["⚠️ Red flags present. Please consider urgent medical evaluation."]
    return lines

# ---------------------------
# Jobs (check-ins)
# ---------------------------
async def job_checkin(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    user_id = data.get("user_id")
    episode_id = data.get("episode_id")
    if not user_id or not episode_id:
        return
    # проверка: пользователь не на паузе?
    u = users_get(user_id)
    if (u.get("paused") or "").lower() == "yes":
        return
    lang = u.get("lang") or "en"
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=t(lang, "checkin_ping"),
            reply_markup=numeric_keyboard_0_10(lang),
        )
        # пометим next_checkin_at пустым — ждём ответа
        episode_set(episode_id, "next_checkin_at", "")
    except Exception as e:
        logging.error(f"job_checkin send error: {e}")

# ---------------------------
# Command Handlers
# ---------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # язык: из user.language_code или из текста
    lang = norm_lang(getattr(user, "language_code", None))
    users_upsert(user.id, user.username or "", lang)

    await update.message.reply_text(
        t(lang, "welcome"),
        reply_markup=main_menu(lang),
    )

    # спросим согласие на напоминания (если ещё не задано)
    u = users_get(user.id)
    if (u.get("consent") or "").lower() not in {"yes", "no"}:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(t(lang, "yes"), callback_data="consent|yes"),
              InlineKeyboardButton(t(lang, "no"), callback_data="consent|no")]]
        )
        await update.message.reply_text(t(lang, "ask_consent"), reply_markup=kb)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await update.message.reply_text(t(lang, "help"))

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or "en")
    await update.message.reply_text(t(lang, "privacy"))

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "yes")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text(t(lang, "paused_on"))

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users_set(uid, "paused", "no")
    lang = norm_lang(users_get(uid).get("lang") or "en")
    await update.message.reply_text(t(lang, "paused_off"))

async def cmd_delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # вычищаем Users
    idx = users_get_row_index(uid)
    if idx:
        ws_users.delete_rows(idx)
    # вычищаем Episodes
    vals = ws_episodes.get_all_values()
    to_delete = []
    for i in range(2, len(vals) + 1):
        if ws_episodes.cell(i, 2).value == str(uid):
            to_delete.append(i)
    for j, row_i in enumerate(to_delete):
        ws_episodes.delete_rows(row_i - j)
    # фидбек не трогаем (анонимная метрика)
    lang = norm_lang(getattr(update.effective_user, "language_code", None))
    await update.message.reply_text(t(lang, "deleted"), reply_markup=ReplyKeyboardRemove())

# ---------------------------
# Callback (consent, feedback thumbs)
# ---------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = (q.data or "")
    uid = q.from_user.id
    lang = norm_lang(users_get(uid).get("lang") or "en")

    if data.startswith("consent|"):
        choice = data.split("|", 1)[1]
        users_set(uid, "consent", "yes" if choice == "yes" else "no")
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(t(lang, "thanks"))

    elif data in {"feedback_yes", "feedback_no"}:
        # записать лайк/дизлайк и спросить комментарий (опционально)
        rating = "1" if data.endswith("yes") else "0"
        ws_feedback.append_row([iso(utcnow()), str(uid), data, q.from_user.username or "", rating, ""])
        sessions.setdefault(uid, {})["awaiting_comment"] = True
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(t(lang, "comment_prompt"))

# ---------------------------
# Scenario Flow
# ---------------------------
def detect_or_choose_topic(lang: str, text: str) -> str | None:
    text_l = text.lower()
    # триггеры
    if any(w in text_l for w in ["болит", "боль", "hurt", "pain", "болю"]):
        return "pain"
    if any(w in text_l for w in ["горло", "throat", "простуд", "cold"]):
        return "throat"
    if any(w in text_l for w in ["сон", "sleep"]):
        return "sleep"
    if any(w in text_l for w in ["стресс", "stress"]):
        return "stress"
    if any(w in text_l for w in ["живот", "желуд", "живіт", "стул", "понос", "диар", "digest"]):
        return "digestion"
    if any(w in text_l for w in ["энерг", "енерг", "energy", "fatigue", "слабость"]):
        return "energy"
    # кнопки меню
    for label, key in TOPIC_KEYS.get(lang, TOPIC_KEYS["en"]).items():
        if text.strip() == label:
            return key
    return None

async def start_pain_triage(update: Update, lang: str, uid: int):
    sessions[uid] = {"topic": "pain", "step": 1, "answers": {}}
    await update.message.reply_text(
        t(lang, "triage_pain_q1"),
        reply_markup=ReplyKeyboardMarkup([T[lang]["triage_pain_q1_opts"]], resize_keyboard=True, one_time_keyboard=True),
    )

async def continue_pain_triage(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str, uid: int, text: str):
    s = sessions.get(uid, {})
    step = s.get("step", 1)

    if step == 1:
        s["answers"]["loc"] = text
        s["step"] = 2
        await update.message.reply_text(
            t(lang, "triage_pain_q2"),
            reply_markup=ReplyKeyboardMarkup([T[lang]["triage_pain_q2_opts"]], resize_keyboard=True, one_time_keyboard=True),
        )
        return

    if step == 2:
        s["answers"]["kind"] = text
        s["step"] = 3
        await update.message.reply_text(
            t(lang, "triage_pain_q3"),
            reply_markup=ReplyKeyboardMarkup([T[lang]["triage_pain_q3_opts"]], resize_keyboard=True, one_time_keyboard=True),
        )
        return

    if step == 3:
        s["answers"]["duration"] = text
        s["step"] = 4
        await update.message.reply_text(
            t(lang, "triage_pain_q4"),
            reply_markup=numeric_keyboard_0_10(lang),
        )
        return

    if step == 4:
        try:
            sev = int(text)
        except Exception:
            await update.message.reply_text(t(lang, "triage_pain_q4"), reply_markup=numeric_keyboard_0_10(lang))
            return
        s["answers"]["severity"] = sev
        s["step"] = 5
        await update.message.reply_text(
            t(lang, "triage_pain_q5"),
            reply_markup=ReplyKeyboardMarkup([T[lang]["triage_pain_q5_opts"]], resize_keyboard=True, one_time_keyboard=True),
        )
        return

    if step == 5:
        red = text
        s["answers"]["red"] = red

        # создаём эпизод, выдаём план
        sev = int(s["answers"].get("severity", 5))
        eid = episode_create(uid, "pain", sev, red)
        s["episode_id"] = eid

        plan_lines = pain_plan(lang, [red])
        await update.message.reply_text(f"{t(lang,'plan_header')}\n" + "\n".join(plan_lines))
        await update.message.reply_text(t(lang, "plan_accept"), reply_markup=accept_keyboard(lang))
        s["step"] = 6
        return

    if step == 6:
        acc = text.strip()
        accepted = "1" if acc.startswith("✅") else "0"
        episode_set(s["episode_id"], "plan_accepted", accepted)
        await update.message.reply_text(t(lang, "remind_when"), reply_markup=remind_keyboard(lang))
        s["step"] = 7
        return

    if step == 7:
        choice = text.strip().lower()
        delay = None
        if choice in {"in 4h", "через 4 часа", "через 4 год"}:
            delay = timedelta(hours=4)
        elif choice in {"this evening", "вечером", "увечері"}:
            delay = timedelta(hours=6)
        elif choice in {"tomorrow morning", "завтра утром", "завтра вранці"}:
            delay = timedelta(hours=16)
        elif choice in {"no need", "не надо", "не треба"}:
            delay = None

        if delay:
            next_time = utcnow() + delay
            episode_set(s["episode_id"], "next_checkin_at", iso(next_time))
            context.job_queue.run_once(
                job_checkin, when=delay.total_seconds(),
                data={"user_id": uid, "episode_id": s["episode_id"]}
            )
        await update.message.reply_text(t(lang, "thanks"), reply_markup=main_menu(lang))
        # завершаем сценарий
        sessions.pop(uid, None)
        return

# ---------------------------
# Main text handler
# ---------------------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    text = (update.message.text or "").strip()

    # язык: при первом сообщении пытаемся детектить
    urec = users_get(uid)
    if not urec:
        # на самый первый инпут
        try:
            lang = norm_lang(detect(text))
        except Exception:
            lang = norm_lang(getattr(user, "language_code", None))
        users_upsert(uid, user.username or "", lang)
    else:
        lang = norm_lang(urec.get("lang") or getattr(user, "language_code", None))

    # 1) Если ждём комментарий к фидбеку
    if sessions.get(uid, {}).get("awaiting_comment"):
        ws_feedback.append_row([iso(utcnow()), str(uid), "comment", user.username or "", "", text])
        sessions[uid]["awaiting_comment"] = False
        await update.message.reply_text(t(lang, "comment_saved"))
        return

    # 2) Если в активном сценарии pain
    if sessions.get(uid, {}).get("topic") == "pain":
        await continue_pain_triage(update, context, lang, uid, text)
        return

    # 3) Детект темы и запуск сценария
    topic = detect_or_choose_topic(lang, text)
    if topic == "pain":
        await start_pain_triage(update, lang, uid)
        return
    elif topic in {"throat", "sleep", "stress", "digestion", "energy"}:
        # пока используем pain-триаж как универсальный (можно позже разветвить)
        await start_pain_triage(update, lang, uid)
        return

    # 4) Фолбэк: короткий ответ с LLM (если ключ задан), иначе стандартный
    if oai:
        try:
            prompt = (
                "You are TendAI, a warm, concise health & longevity assistant. "
                "Ask 1–2 clarifying questions, list 2–3 possible causes, "
                "1–3 simple at-home steps, and when to seek care. "
                "Reply in the user's language. Keep it short."
            )
            resp = oai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ],
                temperature=0.3,
                max_tokens=300,
            )
            answer = resp.choices[0].message.content.strip()
            await update.message.reply_text(answer, reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("👍", callback_data="feedback_yes"),
                  InlineKeyboardButton("👎", callback_data="feedback_no")]]
            ))
            return
        except Exception as e:
            logging.error(f"OpenAI error: {e}")

    # без LLM — дефолт
    await update.message.reply_text(t(lang, "unknown"), reply_markup=main_menu(lang))

# ---------------------------
# Check-in reply (numbers)
# ---------------------------
async def on_number_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Если пользователь отправил число 0–10 — считаем что это ответ на чек-ин."""
    user = update.effective_user
    uid = user.id
    text = update.message.text.strip()
    try:
        val = int(text)
        if not (0 <= val <= 10):
            return
    except Exception:
        return

    lang = norm_lang(users_get(uid).get("lang") or getattr(user, "language_code", None))
    ep = episode_find_open(uid)
    if not ep:
        await update.message.reply_text(t(lang, "thanks"))
        return
    eid = ep.get("episode_id")
    # сохраним как заметку
    episode_set(eid, "notes", f"checkin:{val}")

    if val <= 3:
        await update.message.reply_text(t(lang, "checkin_better"), reply_markup=main_menu(lang))
        # можно закрыть эпизод
        episode_set(eid, "status", "resolved")
    else:
        await update.message.reply_text(t(lang, "checkin_worse"), reply_markup=main_menu(lang))

# ---------------------------
# Skip comment
# ---------------------------
async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if sessions.get(uid, {}).get("awaiting_comment"):
        sessions[uid]["awaiting_comment"] = False
        lang = norm_lang(users_get(uid).get("lang") or "en")
        await update.message.reply_text(t(lang, "skip_ok"))
    else:
        pass

# ---------------------------
# App init
# ---------------------------
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # восстановим запланированные чек-ины
    schedule_from_sheet_on_start(app)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("delete_data", cmd_delete_data))
    app.add_handler(CommandHandler("skip", cmd_skip))

    app.add_handler(CallbackQueryHandler(on_callback))

    # сначала ловим "числа" как ответы на чек-ин
    app.add_handler(MessageHandler(filters.Regex(r"^(?:[0-9]|10)$"), on_number_reply))
    # затем всё остальное текстовое
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling()

if __name__ == "__main__":
    main()
