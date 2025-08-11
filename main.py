# -*- coding: utf-8 -*-
"""
TendAI — чат-первый ассистент здоровья и долголетия.
— Без кнопок и меню. Весь диалог — текстом, естественно.
— LLM вызывается на КАЖДОЕ сообщение. Жёстко требуем JSON.
— Персональные уточнения (нагрузка/жара/сон/питание/стул/мочеиспускание/стресс/контакты),
  оцифровка состояния (0–10), микро-план из 3 шагов, красные флаги в одну строку,
  закрытие петли (предложение чек-ина).
— Рейтинги/отзывы: пользователь может прислать 👍 или 👎 в любой момент,
  а также написать свободный текст отзыва (через /feedback).
— Эпизоды и чек-ины сохраняются в Google Sheets.
"""

import os, re, json, uuid, logging, hashlib
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# langdetect (по возможности)
try:
    from langdetect import detect, DetectorFactory
    DetectorFactory.seed = 0
except Exception:
    detect = None

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
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

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # при наличии — gpt-5
SHEET_NAME      = os.getenv("SHEET_NAME", "TendAI Feedback")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing")

# OpenAI client
oai = OpenAI(api_key=OPENAI_API_KEY) if (OPENAI_API_KEY and OpenAI) else None
logging.info(f"OPENAI enabled={bool(OPENAI_API_KEY)} client={bool(oai)} model={OPENAI_MODEL}")

# Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not creds_json:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")
credentials = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), scope)
gclient = gspread.authorize(credentials)
ss = gclient.open(SHEET_NAME)

def _get_or_create_ws(title: str, headers: list[str]):
    try:
        ws = ss.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=3000, cols=24)
        ws.append_row(headers)
    if not ws.get_all_values():
        ws.append_row(headers)
    return ws

ws_feedback = _get_or_create_ws("Feedback", ["timestamp","user_id","context","username","rating","comment"])
ws_users    = _get_or_create_ws("Users", ["user_id","username","lang","consent","tz_offset","checkin_hour","paused"])
ws_eps      = _get_or_create_ws("Episodes", [
    "episode_id","user_id","topic","started_at","baseline_severity","red_flags",
    "plan_accepted","target","reminder_at","next_checkin_at","status","last_update","notes"
])

# =========================
# State (RAM)
# =========================
# sessions[user_id] = {
#   "chat_history": [...],
#   "answers": {...},
#   "mode": "chat"|"await_consent"|"await_rating"|"await_plan"|"await_reminder",
#   "episode_id": "...",
#   "awaiting_comment": bool,
#   "feedback_context": str,
#   "last_advice_hash": str,
#   "last_feedback_prompt_ts": float,
# }
sessions: dict[int, dict] = {}

# =========================
# i18n
# =========================
SUPPORTED = {"ru","en","uk","es"}
def norm_lang(code: str | None) -> str:
    if not code: return "en"
    c = code.split("-")[0].lower()
    if c.startswith("ua"): c = "uk"
    return c if c in SUPPORTED else "en"

T = {
    "ru": {
        "welcome":"Привет! Я TendAI — тёплый ассистент по здоровью и долголетию. Расскажи коротко, что происходит — помогу и предложу аккуратный план.",
        "help":"Команды: /help, /privacy, /pause, /resume, /delete_data, /lang <ru|en|uk|es>, /feedback",
        "privacy":"Я не заменяю врача. Даю мягкие рекомендации и чек-ины. Данные можно удалить через /delete_data.",
        "consent":"Можно время от времени спрашивать самочувствие? Напишите «да» или «нет».",
        "thanks":"Спасибо, услышал.",
        "checkin_prompt":"Короткий чек-ин: как сейчас по шкале 0–10? Напишите число.",
        "rate_req":"Оцените, пожалуйста, состояние сейчас одним числом 0–10.",
        "plan_try":"Попробуете сегодня? Напишите: «да», «позже» или «нет».",
        "remind_when":"Когда напомнить: «через 4 часа», «вечером», «завтра утром» или «не надо»?",
        "remind_ok":"Принято 🙌",
        "feedback_hint":"Если было полезно — можете поставить 👍 или 👎, а также написать короткий отзыв в одном сообщении.",
        "deleted":"✅ Данные удалены. /start — начать заново.",
    },
    "en": {
        "welcome":"Hi! I’m TendAI — a warm health & longevity assistant. Tell me briefly what’s going on and I’ll help you with a gentle plan.",
        "help":"Commands: /help, /privacy, /pause, /resume, /delete_data, /lang <ru|en|uk|es>, /feedback",
        "privacy":"I’m not a doctor. I offer gentle self-care and check-ins. You can wipe data via /delete_data.",
        "consent":"May I check in with you from time to time? Please reply “yes” or “no”.",
        "thanks":"Thanks, got it.",
        "checkin_prompt":"Quick check-in: how is it now (0–10)? Please reply with a number.",
        "rate_req":"Please rate your state now 0–10 with a single number.",
        "plan_try":"Will you try this today? Reply: “yes”, “later” or “no”.",
        "remind_when":"When should I check in: “in 4h”, “this evening”, “tomorrow morning” or “no need”?",
        "remind_ok":"Got it 🙌",
        "feedback_hint":"If this helped, feel free to send 👍 or 👎, and you can also write a short comment.",
        "deleted":"✅ Data deleted. /start to begin again.",
    },
    "uk": {
        "welcome":"Привіт! Я TendAI — теплий асистент зі здоров’я та довголіття. Коротко опишіть, що відбувається — підкажу м’який план.",
        "help":"Команди: /help, /privacy, /pause, /resume, /delete_data, /lang <ru|en|uk|es>, /feedback",
        "privacy":"Я не лікар. Пропоную м’які кроки та чек-іни. Дані можна стерти через /delete_data.",
        "consent":"Можу час від часу писати, щоб дізнатись, як ви? Відповідь: «так» або «ні».",
        "thanks":"Дякую, почув.",
        "checkin_prompt":"Короткий чек-ін: як зараз (0–10)? Напишіть число.",
        "rate_req":"Оцініть, будь ласка, 0–10 одним числом.",
        "plan_try":"Спробуєте сьогодні? Відповідь: «так», «пізніше» або «ні».",
        "remind_when":"Коли нагадати: «через 4 год», «увечері», «завтра вранці» чи «не треба»?",
        "remind_ok":"Прийнято 🙌",
        "feedback_hint":"Якщо було корисно — надішліть 👍 або 👎, а також короткий коментар.",
        "deleted":"✅ Дані видалено. /start — почати знову.",
    },
    "es": {
        "welcome":"¡Hola! Soy TendAI — un asistente cálido de salud y longevidad. Cuéntame brevemente y te daré un plan suave.",
        "help":"Comandos: /help, /privacy, /pause, /resume, /delete_data, /lang <ru|en|uk|es>, /feedback",
        "privacy":"No soy médico. Ofrezco autocuidado y seguimientos. Borra tus datos con /delete_data.",
        "consent":"¿Puedo escribirte de vez en cuando para revisar cómo sigues? Responde «sí» o «no».",
        "thanks":"¡Gracias!",
        "checkin_prompt":"Revisión rápida: ¿cómo estás ahora (0–10)? Escribe un número.",
        "rate_req":"Valóralo ahora 0–10 con un solo número.",
        "plan_try":"¿Lo intentas hoy? Responde: «sí», «más tarde» o «no».",
        "remind_when":"¿Cuándo te escribo: «en 4 h», «esta tarde», «mañana por la mañana» o «no hace falta»?",
        "remind_ok":"¡Hecho! 🙌",
        "feedback_hint":"Si te ayudó, envía 👍 o 👎 y, si quieres, un breve comentario.",
        "deleted":"✅ Datos borrados. /start para empezar de nuevo.",
    },
}
def t(lang: str, key: str) -> str:
    return T.get(lang, T["en"]).get(key, T["en"].get(key, key))

# =========================
# Sheets helpers
# =========================
def now_utc(): return datetime.now(timezone.utc)
def iso(dt: datetime | None) -> str:
    return "" if not dt else dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")

def users_row_idx(uid: int) -> int | None:
    for i, row in enumerate(ws_users.get_all_records(), start=2):
        if str(row.get("user_id")) == str(uid): return i
    return None
def users_get(uid: int) -> dict:
    for row in ws_users.get_all_records():
        if str(row.get("user_id")) == str(uid): return row
    return {}
def users_upsert(uid: int, username: str, lang: str):
    idx = users_row_idx(uid)
    row = [str(uid), username or "", lang, "no", "0", "", "no"]
    if idx: ws_users.update(f"A{idx}:G{idx}", [row])
    else:   ws_users.append_row(row)
def users_set(uid: int, field: str, value: str):
    idx = users_row_idx(uid)
    if not idx: return
    headers = ws_users.row_values(1)
    if field in headers:
        ws_users.update_cell(idx, headers.index(field)+1, value)

def episode_create(uid: int, topic: str, baseline_sev: int, red: str) -> str:
    eid = f"{uid}-{uuid.uuid4().hex[:8]}"
    ws_eps.append_row([eid, str(uid), topic, iso(now_utc()), str(baseline_sev), red,
                       "0","<=3/10","","","open", iso(now_utc()), ""])
    return eid
def episode_find_open(uid: int) -> dict | None:
    for row in ws_eps.get_all_records():
        if str(row.get("user_id")) == str(uid) and row.get("status") == "open":
            return row
    return None
def episode_set(eid: str, field: str, value: str):
    vals = ws_eps.get_all_values(); headers = vals[0]
    if field not in headers: return
    col = headers.index(field)+1
    for i in range(2, len(vals)+1):
        if ws_eps.cell(i,1).value == eid:
            ws_eps.update_cell(i, col, value)
            ws_eps.update_cell(i, headers.index("last_update")+1, iso(now_utc()))
            return

def reschedule_from_sheet(app):
    for row in ws_eps.get_all_records():
        if row.get("status") != "open": continue
        nca = row.get("next_checkin_at") or ""
        if not nca: continue
        try:
            dt = datetime.strptime(nca, "%Y-%m-%d %H:%M:%S%z")
        except Exception:
            continue
        delay = (dt - now_utc()).total_seconds()
        if delay < 60: delay = 60
        app.job_queue.run_once(job_checkin, when=delay, data={"user_id": int(row["user_id"]), "episode_id": row["episode_id"]})

# =========================
# LLM core (chat-first)
# =========================
SYS_PROMPT = (
    "You are TendAI, a professional, warm health & longevity coach. "
    "Speak in the user's language. 2–5 sentences. Natural, supportive, specific. "
    "Never diagnose; no fear. Ask ONE focused follow-up when data is missing. "
    "For weakness/fatigue cases, consider context questions: training/heat, sleep, nutrition/hydration, "
    "bowel/urination, stress, sick contacts. Encourage a 0–10 self-rating or what activities are limited. "
    "Provide a tiny micro-plan (3 concise steps) when appropriate. "
    "Add one-line red flags: high fever, shortness of breath, chest pain, one-sided weakness; advise medical care if present. "
    "Offer to close the loop: propose a check-in later (evening or next morning). "
    "Do NOT show buttons; present choices inline as short phrases. "
    "Return ONLY JSON with keys: "
    "assistant (string), "
    "next_action (one of: followup, rate_0_10, confirm_plan, pick_reminder, escalate, ask_feedback, none), "
    "slots (object; may include: intent in [pain, throat, sleep, stress, digestion, energy]; "
    "loc in [Head, Throat, Back, Belly, Chest, Other]; kind in [Dull, Sharp, Throbbing, Burning, Pressing]; "
    "duration (string), severity (int 0..10), red (string among [High fever, Vomiting, Weakness/numbness, Speech/vision issues, Trauma, None])), "
    "plan_steps (array of strings, optional)."
)

def _force_json(messages, temperature=0.2, max_tokens=500):
    if not oai: return None
    try:
        return oai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type":"json_object"},
            messages=messages,
        )
    except Exception as e:
        logging.warning(f"response_format fallback: {e}")
        return oai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=messages,
        )

def _json_from(raw: str) -> dict:
    raw = (raw or "").strip()
    try:
        m = re.search(r"\{[\s\S]*\}", raw)
        return json.loads(m.group(0)) if m else json.loads(raw)
    except Exception:
        return {}

def llm_chat(uid: int, lang: str, user_text: str) -> dict:
    hist = sessions.setdefault(uid, {}).setdefault("chat_history", [])[-12:]
    messages = [{"role":"system","content":SYS_PROMPT}] + hist + [{"role":"user","content":f"[lang={lang}] {user_text}"}]
    try:
        resp = _force_json(messages)
        content = (resp.choices[0].message.content or "").strip()
        data = _json_from(content)
        if not isinstance(data, dict):
            data = {}
        # save compact natural text into history
        a = data.get("assistant","")
        hist.append({"role":"user","content":user_text[:1000]})
        if a: hist.append({"role":"assistant","content":a[:1000]})
        sessions[uid]["chat_history"] = hist[-14:]
        logging.info(f"LLM ok | next_action={data.get('next_action')}")
        return data
    except Exception as e:
        logging.warning(f"LLM error: {e}")
        return {}

# =========================
# Simple NLP helpers
# =========================
def parse_rating(s: str):
    m = re.search(r"\b(10|[0-9])\b", s.strip())
    return int(m.group(1)) if m else None

YES = {
    "ru":{"да","ага","ок","хорошо","готов","сделаю"},
    "en":{"yes","ok","sure","ready","will do","yep","yeah"},
    "uk":{"так","ок","гаразд","зроблю","готовий","готова"},
    "es":{"sí","si","ok","vale","listo","lista"},
}
LATER = {
    "ru":{"позже","потом","не сейчас"},
    "en":{"later","not now"},
    "uk":{"пізніше","не зараз"},
    "es":{"más tarde","luego","no ahora"},
}
NO = {
    "ru":{"нет","не","не буду","не хочу"},
    "en":{"no","nope"},
    "uk":{"ні","не буду"},
    "es":{"no"},
}
def is_yes(lang, s): return s.lower() in YES.get(lang,set())
def is_no(lang, s): return s.lower() in NO.get(lang,set())
def is_later(lang, s): return s.lower() in LATER.get(lang,set())

def parse_reminder_code(lang: str, s: str) -> str:
    tl = s.lower()
    if any(k in tl for k in ["4h","4 h","через 4","4 часа","4 год","en 4 h","4 horas"]): return "4h"
    if any(k in tl for k in ["вечер","вечером","evening","esta tarde","увечері","вечір"]): return "evening"
    if any(k in tl for k in ["утро","утром","morning","mañana","завтра утром","завтра вранці"]): return "morning"
    if any(k in tl for k in ["не надо","не нужно","no need","none","no hace falta"]): return "none"
    return ""

# =========================
# Advice de-dupe
# =========================
def _hash_text(s: str) -> str: return hashlib.sha1((s or "").encode("utf-8")).hexdigest()
def send_nodup(uid: int, text: str, send_fn):
    """Не повторять одинаковые советы подряд."""
    if not text: return
    s = sessions.setdefault(uid, {})
    h = _hash_text(text)
    if s.get("last_advice_hash") == h:
        return
    s["last_advice_hash"] = h
    sessions[uid] = s
    return send_fn(text)

# =========================
# Plans (fallback if LLM didn't provide plan_steps)
# =========================
def fallback_plan(lang: str, ans: dict) -> list[str]:
    sev = int(ans.get("severity", 5))
    red = (ans.get("red") or "None").lower()
    urgent = any(w in red for w in ["fever","shortness","breath","одыш","chest","перед","weakness","односторон"]) and sev >= 7
    if urgent:
        return {
            "ru":[ "⚠️ Есть признаки возможной угрозы. Пожалуйста, обратитесь за медицинской помощью." ],
            "en":[ "⚠️ Some answers suggest urgent risks. Please seek medical care as soon as possible." ],
            "uk":[ "⚠️ Є ознаки можливої загрози. Зверніться до лікаря." ],
            "es":[ "⚠️ Posibles signos de urgencia. Busca atención médica lo antes posible." ],
        }[lang]
    base = {
        "ru":[ "1) Вода 400–600 мл и 15–20 минут тишины.", "2) Если нет противопоказаний — ибупрофен 200–400 мг 1 раз с едой.", "3) Пауза от экранов 30–60 мин." ],
        "en":[ "1) 400–600 ml water + 15–20 min quiet rest.", "2) If no contraindications — ibuprofen 200–400 mg once with food.", "3) Screen break 30–60 min." ],
        "uk":[ "1) 400–600 мл води + 15–20 хв тиші.", "2) Якщо немає протипоказань — ібупрофен 200–400 мг 1 раз із їжею.", "3) Перерва від екранів 30–60 хв." ],
        "es":[ "1) 400–600 ml de agua + 15–20 min de descanso.", "2) Si no hay contraindicaciones — ibuprofeno 200–400 mg una vez con comida.", "3) Descanso de pantallas 30–60 min." ],
    }[lang]
    return base

# =========================
# Jobs (check-ins)
# =========================
async def job_checkin(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    uid = data.get("user_id"); eid = data.get("episode_id")
    if not uid or not eid: return
    u = users_get(uid)
    if (u.get("paused") or "").lower() == "yes": return
    lang = u.get("lang") or "en"
    try:
        await context.bot.send_message(uid, t(lang,"checkin_prompt"))
        s = sessions.setdefault(uid, {})
        s["mode"] = "await_rating"
        s["episode_id"] = eid
        episode_set(eid, "next_checkin_at", "")
    except Exception as e:
        logging.error(f"job_checkin send error: {e}")

# =========================
# Commands
# =========================
async def on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    reschedule_from_sheet(app)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    lang = users_get(uid).get("lang")
    if not lang:
        txt = (update.message.text or "").strip() if update.message else ""
        cand = None
        if detect:
            try: cand = detect(txt) if txt else None
            except Exception: cand = None
        lang = norm_lang(cand or getattr(user,"language_code",None))
        users_upsert(uid, user.username or "", lang)

    await update.message.reply_text(t(lang,"welcome"))
    u = users_get(uid)
    s = sessions.setdefault(uid, {"mode":"chat","answers":{}, "chat_history":[]})
    if (u.get("consent") or "").lower() not in {"yes","no"}:
        s["mode"]="await_consent"
        await update.message.reply_text(t(lang,"consent"))

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang,"help"))

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang,"privacy"))

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid,"paused","yes"); await update.message.reply_text("⏸️ Paused.")

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid,"paused","no"); await update.message.reply_text("▶️ Resumed.")

async def cmd_delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    idx = users_row_idx(uid)
    if idx: ws_users.delete_rows(idx)
    vals = ws_eps.get_all_values(); to_del=[]
    for i in range(2, len(vals)+1):
        if ws_eps.cell(i,2).value == str(uid): to_del.append(i)
    for j, row_i in enumerate(to_del):
        ws_eps.delete_rows(row_i - j)
    await update.message.reply_text(t(norm_lang(getattr(update.effective_user,"language_code",None)),"deleted"))

async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /lang ru|en|uk|es"); return
    cand = norm_lang(context.args[0])
    if cand not in SUPPORTED:
        await update.message.reply_text("Usage: /lang ru|en|uk|es"); return
    users_set(uid,"lang",cand); await update.message.reply_text("✅ Language set.")

async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions.setdefault(uid,{})["awaiting_comment"]=True
    sessions[uid]["feedback_context"]="manual"
    await update.message.reply_text("Напишите короткий отзыв одним сообщением. Можно также просто отправить 👍 или 👎.")

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = sessions.setdefault(uid,{})
    s["awaiting_comment"]=False
    await update.message.reply_text("Ок, пропустили.")

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Используйте обычные ответы текстом (без кнопок).")

# =========================
# Text handler (chat-first)
# =========================
THUMBS_UP = {"👍","👍🏻","👍🏼","👍🏽","👍🏾","👍🏿"}
THUMBS_DOWN = {"👎","👎🏻","👎🏼","👎🏽","👎🏾","👎🏿"}

def _feedback_prompt_needed(uid: int, interval_sec=180.0) -> bool:
    import time
    s = sessions.setdefault(uid,{})
    last = s.get("last_feedback_prompt_ts", 0.0)
    now = time.time()
    if now - last > interval_sec:
        s["last_feedback_prompt_ts"] = now
        sessions[uid] = s
        return True
    return False

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    text = (update.message.text or "").strip()

    # язык
    urec = users_get(uid)
    if not urec:
        cand=None
        if detect:
            try: cand = detect(text) if text else None
            except Exception: cand=None
        lang = norm_lang(cand or getattr(user,"language_code",None))
        users_upsert(uid, user.username or "", lang)
    else:
        lang = norm_lang(urec.get("lang") or getattr(user,"language_code",None))

    s = sessions.setdefault(uid, {"mode":"chat","answers":{}, "chat_history":[]})

    # ========= Отзывы =========
    if text in THUMBS_UP:
        ws_feedback.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), str(uid), "thumb", user.username or "", "1", ""])
        await update.message.reply_text("Спасибо за 👍")
        return
    if text in THUMBS_DOWN:
        ws_feedback.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), str(uid), "thumb", user.username or "", "0", ""])
        await update.message.reply_text("Спасибо за 👎 — постараюсь быть полезнее.")
        return
    if s.get("awaiting_comment") and not text.startswith("/"):
        ws_feedback.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), str(uid), s.get("feedback_context",""), user.username or "", "", text])
        s["awaiting_comment"]=False
        await update.message.reply_text("Отзыв сохранён 🙌")
        return

    # ========= Согласие =========
    if s.get("mode") == "await_consent":
        low = text.lower()
        if is_yes(lang, low):
            users_set(uid,"consent","yes"); s["mode"]="chat"
            await update.message.reply_text(t(lang,"thanks")); return
        if is_no(lang, low):
            users_set(uid,"consent","no"); s["mode"]="chat"
            await update.message.reply_text(t(lang,"thanks")); return
        await update.message.reply_text("Пожалуйста, напишите «да» или «нет»."); return

    # ========= Чек-ин (0–10) =========
    if s.get("mode") == "await_rating":
        rating = parse_rating(text)
        if rating is None or not (0 <= rating <= 10):
            await update.message.reply_text(t(lang,"rate_req")); return
        ep = episode_find_open(uid)
        if ep:
            eid = ep["episode_id"]
            episode_set(eid,"notes",f"checkin:{rating}")
            if rating <= 3:
                episode_set(eid,"status","resolved")
                await update.message.reply_text({"ru":"Отлично! Рад за прогресс 💪","en":"Great! Love the progress 💪","uk":"Чудово! Гарний прогрес 💪","es":"¡Genial! Buen progreso 💪"}[lang])
            else:
                await update.message.reply_text({"ru":"Понимаю. Если появятся красные флаги — лучше обратиться к врачу.","en":"I hear you. If red flags appear, please consider medical help.","uk":"Розумію. Якщо з’являться «червоні прапорці», зверніться до лікаря.","es":"Entiendo. Si aparecen señales de alarma, consulta a un médico."}[lang])
        s["mode"]="chat"
        if _feedback_prompt_needed(uid):
            await update.message.reply_text(t(lang,"feedback_hint"))
        return

    # ========= Подтверждение плана =========
    if s.get("mode") == "await_plan":
        low = text.lower(); eid = s.get("episode_id")
        if is_yes(lang, low):
            if eid: episode_set(eid,"plan_accepted","1")
            s["mode"]="await_reminder"
            await update.message.reply_text(t(lang,"remind_when")); return
        if is_later(lang, low):
            if eid: episode_set(eid,"plan_accepted","later")
            s["mode"]="await_reminder"
            await update.message.reply_text(t(lang,"remind_when")); return
        if is_no(lang, low):
            if eid: episode_set(eid,"plan_accepted","0")
            s["mode"]="chat"
            await update.message.reply_text({"ru":"Хорошо, без плана. Можем просто отслеживать самочувствие.","en":"Alright, no plan. We can just track how you feel.","uk":"Добре, без плану. Можемо просто відстежувати самопочуття.","es":"De acuerdo, sin plan. Podemos solo revisar cómo sigues."}[lang])
            if _feedback_prompt_needed(uid):
                await update.message.reply_text(t(lang,"feedback_hint"))
            return
        await update.message.reply_text({"ru":"Ответьте «да», «позже» или «нет».","en":"Please reply “yes”, “later” or “no”.","uk":"Відповідайте «так», «пізніше» або «ні».","es":"Responde «sí», «más tarde» o «no»."}[lang])
        return

    # ========= Выбор напоминания =========
    if s.get("mode") == "await_reminder":
        code = parse_reminder_code(lang, text)
        if not code:
            await update.message.reply_text(t(lang,"remind_when")); return
        urec = users_get(uid); tz_off = 0
        try: tz_off = int(urec.get("tz_offset") or "0")
        except Exception: tz_off = 0
        nowu = now_utc(); user_now = nowu + timedelta(hours=tz_off)
        if code == "4h":
            target_user = user_now + timedelta(hours=4)
        elif code == "evening":
            target_user = user_now.replace(hour=19, minute=0, second=0, microsecond=0)
            if target_user < user_now: target_user += timedelta(days=1)
        elif code == "morning":
            target_user = user_now.replace(hour=9, minute=0, second=0, microsecond=0)
            if target_user < user_now: target_user += timedelta(days=1)
        else:
            target_user = None

        eid = s.get("episode_id")
        if target_user and eid:
            target_utc = target_user - timedelta(hours=tz_off)
            episode_set(eid,"next_checkin_at", iso(target_utc))
            delay = max(60, (target_utc - nowu).total_seconds())
            context.job_queue.run_once(job_checkin, when=delay, data={"user_id": uid, "episode_id": eid})
        await update.message.reply_text(t(lang,"remind_ok"))
        s["mode"]="chat"
        if _feedback_prompt_needed(uid):
            await update.message.reply_text(t(lang,"feedback_hint"))
        return

    # ========= CHAT-FIRST (LLM) =========
    data = llm_chat(uid, lang, text)
    if not data:
        # мягкий фолбэк без шаблонов
        if lang=="ru":
            await update.message.reply_text("Понимаю. Где именно ощущаете и как давно началось? Если можно — оцените по шкале 0–10.")
        elif lang=="uk":
            await update.message.reply_text("Розумію. Де саме і відколи це почалось? Якщо можете — оцініть 0–10.")
        elif lang=="es":
            await update.message.reply_text("Entiendo. ¿Dónde exactamente y desde cuándo empezó? Si puedes, valora 0–10.")
        else:
            await update.message.reply_text("I hear you. Where exactly is it and since when? If you can, rate it 0–10.")
        return

    assistant = data.get("assistant") or ""
    if assistant:
        await send_nodup(uid, assistant, update.message.reply_text)

    # сохранить слоты
    ans = s.setdefault("answers", {})
    for k in ["intent","loc","kind","duration","severity","red"]:
        v = (data.get("slots") or {}).get(k)
        if v not in (None,""): ans[k]=v

    # если модель уже вернула шаги плана — покажем
    plan_steps = data.get("plan_steps") or []
    if plan_steps:
        await send_nodup(uid, "\n".join(plan_steps), update.message.reply_text)

    na = data.get("next_action") or "followup"

    if na == "rate_0_10":
        s["mode"]="await_rating"
        await update.message.reply_text(t(lang,"rate_req"))
        return

    if na == "confirm_plan":
        # если нет эпизода — создадим
        eid = s.get("episode_id")
        if not eid:
            eid = episode_create(uid, ans.get("intent","pain"), int(ans.get("severity",5) or 5), ans.get("red","None") or "None")
            s["episode_id"]=eid
        # если модель не дала план — подстрахуемся
        if not plan_steps:
            await send_nodup(uid, "\n".join(fallback_plan(lang, ans)), update.message.reply_text)
        s["mode"]="await_plan"
        await update.message.reply_text(t(lang,"plan_try"))
        return

    if na == "pick_reminder":
        s["mode"]="await_reminder"
        await update.message.reply_text(t(lang,"remind_when"))
        return

    if na == "escalate":
        esc = {
            "ru":"⚠️ Некоторым ответам лучше уделить внимание очно. Если есть высокая температура, одышка, боль в груди или односторонняя слабость — обратитесь к врачу.",
            "en":"⚠️ Some answers are concerning. If high fever, shortness of breath, chest pain or one-sided weakness — seek medical care.",
            "uk":"⚠️ Деякі відповіді тривожні. Якщо висока температура, задишка, біль у грудях або однобічна слабкість — зверніться до лікаря.",
            "es":"⚠️ Algunas respuestas son preocupantes. Si hay fiebre alta, falta de aire, dolor en el pecho o debilidad de un lado — busca atención médica.",
        }[lang]
        await send_nodup(uid, esc, update.message.reply_text)
        if _feedback_prompt_needed(uid):
            await update.message.reply_text(t(lang,"feedback_hint"))
        return

    if na == "ask_feedback" and _feedback_prompt_needed(uid):
        await update.message.reply_text(t(lang,"feedback_hint"))
        return

    # иначе — просто продолжаем разговор
    s["mode"]="chat"
    sessions[uid]=s

# =========================
# Runner
# =========================
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_startup).build()

    reschedule_from_sheet(app)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("delete_data", cmd_delete_data))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("feedback", cmd_feedback))
    app.add_handler(CommandHandler("skip", cmd_skip))

    app.add_handler(CallbackQueryHandler(on_callback))  # на случай старых инлайн-кликов

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
