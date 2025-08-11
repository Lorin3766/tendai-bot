# -*- coding: utf-8 -*-
import os, re, json, uuid, logging
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
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # поставь gpt-5, если доступна
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
        ws = ss.add_worksheet(title=title, rows=2000, cols=20)
        ws.append_row(headers)
    if not ws.get_all_values():
        ws.append_row(headers)
    return ws

ws_feedback = _get_or_create_ws("Feedback", ["timestamp", "user_id", "name", "username", "rating", "comment"])
ws_users    = _get_or_create_ws("Users", ["user_id", "username", "lang", "consent", "tz_offset", "checkin_hour", "paused"])
ws_eps      = _get_or_create_ws("Episodes", [
    "episode_id","user_id","topic","started_at","baseline_severity","red_flags","plan_accepted",
    "target","reminder_at","next_checkin_at","status","last_update","notes"
])

# =========================
# State (RAM)
# =========================
# sessions[user_id] = {
#   "chat_history": [...],
#   "answers": {"intent","loc","kind","duration","severity","red"},
#   "mode": "chat"|"await_rating"|"await_plan"|"await_reminder"|"await_consent",
#   "episode_id": "...",
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
        "welcome":"Привет! Я TendAI — тёплый ассистент по здоровью и долголетию. Расскажи коротко, что происходит — помогу разобраться и предложу аккуратный план.",
        "help":"Команды: /help, /privacy, /pause, /resume, /delete_data, /lang <ru|en|uk|es>, /feedback",
        "privacy":"Я не заменяю врача. Даю мягкие рекомендации и напоминания. Можно удалить данные командой /delete_data.",
        "thanks":"Спасибо, услышал.",
        "consent":"Можно писать тебе позже, чтобы спросить самочувствие?",
        "ok_menu":"Если что — просто напиши словами. Кнопок не будет 🙂",
    },
    "en": {
        "welcome":"Hi! I’m TendAI — a warm health & longevity assistant. Tell me briefly what’s going on and I’ll guide you gently.",
        "help":"Commands: /help, /privacy, /pause, /resume, /delete_data, /lang <ru|en|uk|es>, /feedback",
        "privacy":"I don’t replace a doctor. I offer gentle self-care steps and check-ins. Use /delete_data to erase your data.",
        "thanks":"Thanks, got it.",
        "consent":"May I check in with you later about how you feel?",
        "ok_menu":"Just type in natural language. There are no buttons 🙂",
    },
    "uk": {
        "welcome":"Привіт! Я TendAI — теплий асистент зі здоров’я та довголіття. Коротко опишіть, що відбувається — я допоможу.",
        "help":"Команди: /help, /privacy, /pause, /resume, /delete_data, /lang <ru|en|uk|es>, /feedback",
        "privacy":"Я не замінюю лікаря. Даю м’які поради та нагадування. /delete_data — видалити дані.",
        "thanks":"Дякую, почуло.",
        "consent":"Можу написати пізніше й запитати, як ви?",
        "ok_menu":"Просто пишіть звичайними словами. Кнопок не буде 🙂",
    },
    "es": {
        "welcome":"¡Hola! Soy TendAI — un asistente cálido de salud y longevidad. Cuéntame brevemente y te guiaré con cuidado.",
        "help":"Comandos: /help, /privacy, /pause, /resume, /delete_data, /lang <ru|en|uk|es>, /feedback",
        "privacy":"No sustituyo a un médico. Ofrezco pasos de autocuidado y seguimientos. /delete_data para borrar tus datos.",
        "thanks":"¡Gracias!",
        "consent":"¿Puedo escribirte más tarde para saber cómo sigues?",
        "ok_menu":"Escribe de forma natural. No habrá botones 🙂",
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
    "You are TendAI, a professional yet warm health & longevity coach. "
    "Speak in the user's language, 2–5 sentences, supportive, clear and practical. "
    "No diagnosis, no fear-mongering. When info is missing, ask ONE focused question. "
    "Offer simple home-care steps only when appropriate. Do not show buttons; list choices inline with short phrases. "
    "Return ONLY JSON with keys: "
    "assistant (string), next_action (one of: followup, rate_0_10, confirm_plan, pick_reminder, escalate, none), "
    "slots (object; may include: intent in [pain, throat, sleep, stress, digestion, energy]; "
    "loc in [Head, Throat, Back, Belly, Chest, Other]; kind in [Dull, Sharp, Throbbing, Burning, Pressing]; "
    "duration (string), severity (int 0..10), red (string among [High fever, Vomiting, Weakness/numbness, Speech/vision issues, Trauma, None]), "
    "), plan_steps (array of strings, optional)."
)

def _force_json(messages, temperature=0.2, max_tokens=450):
    """Try response_format=json_object; fallback if unsupported."""
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
        logging.warning(f"response_format disabled, fallback: {e}")
        return oai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=messages,
        )

def _json_from_text(raw: str) -> dict:
    raw = (raw or "").strip()
    try:
        m = re.search(r"\{[\s\S]*\}", raw)
        return json.loads(m.group(0)) if m else json.loads(raw)
    except Exception:
        return {}

def llm_chat(uid: int, lang: str, user_text: str) -> dict:
    """messages: chat history (short), returns dict"""
    hist = sessions.setdefault(uid, {}).setdefault("chat_history", [])[-12:]
    messages = [{"role":"system","content":SYS_PROMPT}] + hist + [{"role":"user","content":f"[lang={lang}] {user_text}"}]
    try:
        resp = _force_json(messages)
        content = (resp.choices[0].message.content or "").strip()
        data = _json_from_text(content)
        if not isinstance(data, dict):
            data = {}
        # save compact natural assistant text back into history
        assistant_text = data.get("assistant", "")
        hist.append({"role":"user","content":user_text[:1000]})
        if assistant_text:
            hist.append({"role":"assistant","content":assistant_text[:1000]})
        sessions[uid]["chat_history"] = hist[-14:]
        logging.info(f"LLM ok | text='{user_text[:40]}' -> next_action={data.get('next_action')}")
        return data
    except Exception as e:
        logging.warning(f"LLM error: {e}")
        return {}

# =========================
# Parsing helpers (free-text)
# =========================
YES = {"ru":{"да","ага","ок","хорошо","готов","го"},
       "en":{"yes","yep","ok","sure","ready","yeah"},
       "uk":{"так","ок","гаразд","готовий","готова"},
       "es":{"sí","si","ok","vale","listo","lista"}}
LATER = {"ru":{"позже","потом","не сейчас"},
         "en":{"later","not now"},
         "uk":{"пізніше","не зараз"},
         "es":{"más tarde","luego","no ahora"}}
NO = {"ru":{"нет","неа","не буду"},
      "en":{"no","nope"},
      "uk":{"ні","не буду"},
      "es":{"no"}}

def is_yes(lang, s): return s.lower() in YES.get(lang, set())
def is_no(lang, s): return s.lower() in NO.get(lang, set())
def is_later(lang, s): return s.lower() in LATER.get(lang, set())

def parse_rating(s: str):
    m = re.search(r"\b(10|[0-9])\b", s.strip())
    return int(m.group(1)) if m else None

def parse_reminder_code(lang: str, s: str) -> str:
    tl = s.lower()
    if any(k in tl for k in ["4h","4 h","4ч","4 ч","через 4","4 hours","en 4 h","4 horas","через четыре","через чотири"]):
        return "4h"
    if any(k in tl for k in ["вечер","вечером","evening","tarde","увечері","вечір"]):
        return "evening"
    if any(k in tl for k in ["утро","утром","morning","mañana","ранком","вранці"]):
        return "morning"
    if any(k in tl for k in ["не надо","не нужно","без","no need","none","not necessary","no hace falta"]):
        return "none"
    return ""

# =========================
# Plan builder (простые правила)
# =========================
def build_hypotheses(ans: dict) -> list[str]:
    loc = (ans.get("loc") or "").lower()
    kind = (ans.get("kind") or "").lower()
    lines=[]
    if any(w in loc for w in ["head","голов","cabeza"]):
        if "throbb" in kind or "пульс" in kind: lines.append("Похоже на мигренозный тип.")
        elif "дав" in kind or "press" in kind or "tight" in kind: lines.append("Похоже на напряжение/стресс-головную боль.")
    if any(w in loc for w in ["back","спин","espalda"]):
        if "прострел" in kind or "shoot" in kind: lines.append("Есть признаки корешкового компонента.")
        else: lines.append("Больше похоже на механическую боль в спине.")
    if any(w in loc for w in ["throat","горло","garganta"]):
        lines.append("Часто это вирусная боль в горле.")
    return lines

def make_plan(lang: str, ans: dict) -> list[str]:
    red = (ans.get("red") or "None").lower()
    sev = int(ans.get("severity", 5))
    urgent = any(w in red for w in ["fever","vomit","weak","speech","vision","травм","trauma"]) and sev >= 7
    if urgent:
        return {
            "ru":[ "⚠️ Есть признаки возможной угрозы. Пожалуйста, как можно скорее обратитесь за медицинской помощью." ],
            "en":[ "⚠️ Some answers suggest urgent risks. Please seek medical care as soon as possible." ],
            "uk":[ "⚠️ Є ознаки можливої загрози. Будь ласка, якнайшвидше зверніться по медичну допомогу." ],
            "es":[ "⚠️ Hay señales de posible urgencia. Busca atención médica lo antes posible." ],
        }[lang]

    base = {
        "ru":[
            "1) Вода 400–600 мл и 15–20 минут покоя в тихом месте.",
            "2) Если нет противопоказаний — ибупрофен 200–400 мг 1 раз с едой.",
            "3) Проветрить комнату; уменьшить экраны на 30–60 минут.",
            "Цель: к вечеру интенсивность ≤3/10."
        ],
        "en":[
            "1) Drink 400–600 ml water and rest 15–20 minutes in a quiet place.",
            "2) If no contraindications — ibuprofen 200–400 mg once with food.",
            "3) Air the room; reduce screens for 30–60 minutes.",
            "Target: pain/strain ≤3/10 by evening."
        ],
        "uk":[
            "1) 400–600 мл води та 15–20 хв спокою у тихому місці.",
            "2) Якщо немає протипоказань — ібупрофен 200–400 мг 1 раз із їжею.",
            "3) Провітрити кімнату; менше екранів 30–60 хв.",
            "Мета: до вечора інтенсивність ≤3/10."
        ],
        "es":[
            "1) Bebe 400–600 ml de agua y descansa 15–20 min en un lugar tranquilo.",
            "2) Si no hay contraindicaciones — ibuprofeno 200–400 mg una vez con comida.",
            "3) Ventila la habitación; menos pantallas 30–60 min.",
            "Objetivo: por la tarde ≤3/10."
        ],
    }[lang]

    loc = (ans.get("loc") or "").lower()
    if any(w in loc for w in ["back","спин","espalda"]):
        extra = {"ru":["4) Тёплый компресс 10–15 мин 2–3р/день, мягкая мобилизация/растяжка."],
                 "en":["4) Warm compress 10–15 min 2–3×/day; gentle mobility."],
                 "uk":["4) Теплий компрес 10–15 хв 2–3р/день; м’яка мобілізація."],
                 "es":["4) Compresa tibia 10–15 min 2–3×/día; movilidad suave."]}[lang]
        return base + extra
    if any(w in loc for w in ["throat","горло","garganta"]):
        extra = {"ru":["4) Тёплое питьё; полоскание соли 3–4р/день."],
                 "en":["4) Warm fluids; saline gargles 3–4×/day."],
                 "uk":["4) Теплі напої; полоскання сольовим розчином 3–4р/день."],
                 "es":["4) Líquidos tibios; gárgaras salinas 3–4×/día."]}[lang]
        return base + extra
    return base

# =========================
# Jobs (check-in)
# =========================
async def job_checkin(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    uid = data.get("user_id"); eid = data.get("episode_id")
    if not uid or not eid: return
    u = users_get(uid)
    if (u.get("paused") or "").lower() == "yes": return
    lang = u.get("lang") or "en"
    try:
        await context.bot.send_message(uid, {
            "ru":"Небольшой чек-ин: как сейчас по шкале 0–10? Просто напишите число.",
            "en":"Quick check-in: how is it now (0–10)? Please reply with a number.",
            "uk":"Короткий чек-ін: як зараз (0–10)? Напишіть число.",
            "es":"Revisión rápida: ¿cómo estás ahora (0–10)? Responde con un número.",
        }[lang])
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
        logging.info("Webhook cleared")
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
    await update.message.reply_text(t(lang,"ok_menu"))
    u = users_get(uid)
    if (u.get("consent") or "").lower() not in {"yes","no"}:
        s = sessions.setdefault(uid, {})
        s["mode"] = "await_consent"
        await update.message.reply_text(t(lang,"consent") + " (Напишите: да/нет)")
    else:
        sessions.setdefault(uid, {}).setdefault("mode","chat")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang,"help"))

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = norm_lang(users_get(update.effective_user.id).get("lang") or getattr(update.effective_user,"language_code",None))
    await update.message.reply_text(t(lang,"privacy"))

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid,"paused","yes")
    await update.message.reply_text("⏸️ Paused.")

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; users_set(uid,"paused","no")
    await update.message.reply_text("▶️ Resumed.")

async def cmd_delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    idx = users_row_idx(uid)
    if idx: ws_users.delete_rows(idx)
    vals = ws_eps.get_all_values(); to_del=[]
    for i in range(2, len(vals)+1):
        if ws_eps.cell(i,2).value == str(uid): to_del.append(i)
    for j, row_i in enumerate(to_del):
        ws_eps.delete_rows(row_i - j)
    await update.message.reply_text("✅ Deleted. /start to begin again.")

async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /lang ru|en|uk|es"); return
    cand = norm_lang(context.args[0])
    if cand not in SUPPORTED:
        await update.message.reply_text("Usage: /lang ru|en|uk|es"); return
    users_set(uid,"lang",cand)
    await update.message.reply_text("✅ Language set.")

async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text("Напишите краткий отзыв одним сообщением. (или /skip) ")
    s = sessions.setdefault(uid, {}); s["awaiting_comment"]=True; s["feedback_context"]="general"

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = sessions.setdefault(uid, {})
    s["awaiting_comment"]=False
    await update.message.reply_text("Ок, пропустили.")

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Используйте обычные ответы текстом.")

# =========================
# Text (chat-first)
# =========================
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

    # ожидание согласия
    if s.get("mode") == "await_consent":
        low = text.lower()
        if is_yes(lang, low):
            users_set(uid,"consent","yes"); s["mode"]="chat"
            await update.message.reply_text(t(lang,"thanks")); return
        if is_no(lang, low):
            users_set(uid,"consent","no"); s["mode"]="chat"
            await update.message.reply_text(t(lang,"thanks")); return
        await update.message.reply_text("Пожалуйста, напишите «да» или «нет»."); return

    # ожидание отзыва
    if s.get("awaiting_comment") and not text.startswith("/"):
        ws_feedback.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), str(uid), "comment:general", user.username or "", "", text])
        s["awaiting_comment"]=False
        await update.message.reply_text("Спасибо за отзыв!"); return

    # чек-ин: оценка 0–10
    if s.get("mode") == "await_rating":
        rating = parse_rating(text)
        if rating is None or not (0 <= rating <= 10):
            await update.message.reply_text("Напишите число 0–10 одним сообщением."); return
        ep = episode_find_open(uid)
        if ep:
            eid = ep["episode_id"]
            episode_set(eid,"notes",f"checkin:{rating}")
            if rating <= 3:
                episode_set(eid,"status","resolved")
                await update.message.reply_text({"ru":"Хорошо! Держим курс 💪","en":"Nice! Keep it up 💪","uk":"Чудово! Так тримати 💪","es":"¡Bien! Sigue así 💪"}[lang])
            else:
                await update.message.reply_text({"ru":"Сожалею, что всё ещё неприятно. Если появились красные флаги — лучше обратиться к врачу.","en":"Sorry it still feels uncomfortable. If any red flags appear, please consider medical care.","uk":"Шкода, що ще неприємно. Якщо з’являться «червоні прапорці», зверніться до лікаря.","es":"Lamento que siga molesto. Si aparecen señales de alarma, busca atención médica."}[lang])
        s["mode"]="chat"; return

    # ожидание подтверждения плана
    if s.get("mode") == "await_plan":
        low = text.lower()
        eid = s.get("episode_id")
        if is_yes(lang, low):
            if eid: episode_set(eid,"plan_accepted","1")
            s["mode"]="await_reminder"
            await update.message.reply_text({
                "ru":"Когда напомнить? Можно написать: «через 4 часа», «вечером», «завтра утром» или «не надо».",
                "en":"When should I check in? You can reply: “in 4h”, “this evening”, “tomorrow morning” or “no need”.",
                "uk":"Коли нагадати? Напишіть: «через 4 год», «увечері», «завтра вранці» або «не треба».",
                "es":"¿Cuándo te escribo? Di: «en 4 h», «esta tarde», «mañana por la mañana» o «no hace falta».",
            }[lang]); return
        if is_later(lang, low):
            if eid: episode_set(eid,"plan_accepted","later")
            s["mode"]="await_reminder"
            await update.message.reply_text({
                "ru":"Ок. Когда напомнить: «через 4 часа», «вечером», «завтра утром» или «не надо»?",
                "en":"Ok. When should I check in: “in 4h”, “this evening”, “tomorrow morning” or “no need”?",
                "uk":"Гаразд. Коли нагадати: «через 4 год», «увечері», «завтра вранці» чи «не треба»?",
                "es":"De acuerdo. ¿Cuándo te escribo: «en 4 h», «esta tarde», «mañana por la mañana» o «no hace falta»?",
            }[lang]); return
        if is_no(lang, low):
            if eid: episode_set(eid,"plan_accepted","0")
            s["mode"]="chat"
            await update.message.reply_text({"ru":"Хорошо, без планов. Я рядом, если захочешь продолжить.","en":"Alright, no plan. I’m here if you want to continue.","uk":"Добре, без плану. Я поруч, якщо захочете продовжити.","es":"De acuerdo, sin plan. Estoy aquí si quieres continuar."}[lang]); return
        await update.message.reply_text({"ru":"Ответьте: «да», «позже» или «нет».","en":"Please reply: “yes”, “later” or “no”.","uk":"Відповідайте: «так», «пізніше» або «ні».","es":"Responde: «sí», «más tarde» o «no»."}[lang]); return

    # ожидание выбора напоминания
    if s.get("mode") == "await_reminder":
        code = parse_reminder_code(lang, text)
        if not code:
            await update.message.reply_text({"ru":"Пожалуйста, напишите: «через 4 часа», «вечером», «завтра утром» или «не надо».","en":"Please write: “in 4h”, “this evening”, “tomorrow morning” or “no need”.","uk":"Напишіть: «через 4 год», «увечері», «завтра вранці» або «не треба».","es":"Escribe: «en 4 h», «esta tarde», «mañana por la mañana» o «no hace falta»."}[lang]); return

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
            episode_set(eid, "next_checkin_at", iso(target_utc))
            delay = max(60, (target_utc - nowu).total_seconds())
            context.job_queue.run_once(job_checkin, when=delay, data={"user_id": uid, "episode_id": eid})
        await update.message.reply_text(t(lang,"thanks"))
        s["mode"]="chat"; return

    # ===== CHAT-FIRST =====
    data = llm_chat(uid, lang, text)
    if not data:
        # мягкий фолбэк, без шаблонов
        if lang=="ru":
            await update.message.reply_text("Понимаю. Расскажите, где именно и как давно это началось? Если можете — оцените по шкале 0–10.")
        elif lang=="uk":
            await update.message.reply_text("Розумію. Де саме і відколи це почалось? Якщо можете — оцініть 0–10.")
        elif lang=="es":
            await update.message.reply_text("Entiendo. ¿Dónde exactamente y desde cuándo empezó? Si puedes, valora 0–10.")
        else:
            await update.message.reply_text("I hear you. Where exactly is it and since when? If you can, rate it 0–10.")
        return

    assistant = data.get("assistant") or ""
    if assistant:
        await update.message.reply_text(assistant)

    # обновим слоты
    ans = s.setdefault("answers", {})
    for k in ["intent","loc","kind","duration","severity","red"]:
        v = (data.get("slots") or {}).get(k)
        if v not in (None,""): ans[k]=v

    # если пришли готовые шаги плана — покажем
    plan_steps = data.get("plan_steps") or []
    if plan_steps:
        await update.message.reply_text(t(lang,"thanks"))
        await update.message.reply_text("\n".join(plan_steps))

    # режим в зависимости от next_action
    na = data.get("next_action") or "followup"
    if na == "rate_0_10":
        s["mode"]="await_rating"
        await update.message.reply_text({"ru":"Оцените сейчас по шкале 0–10 одним числом.","en":"Please rate it now 0–10 with a single number.","uk":"Оцініть зараз 0–10 одним числом.","es":"Valóralo ahora 0–10 con un número."}[lang])
        return

    if na == "confirm_plan":
        # если нет эпизода — создадим
        eid = s.get("episode_id")
        if not eid:
            eid = episode_create(uid, ans.get("intent","pain"), int(ans.get("severity",5) or 5), ans.get("red","None") or "None")
            s["episode_id"]=eid
        # если нет явных шагов — соберём базовый план
        if not plan_steps:
            hyps = build_hypotheses(ans)
            steps = make_plan(lang, ans)
            if hyps:
                await update.message.reply_text("Гипотезы: " + " ".join(f"• {h}" for h in hyps))
            await update.message.reply_text("\n".join(steps))
        s["mode"]="await_plan"
        await update.message.reply_text({"ru":"Попробуете сегодня? Напишите: «да», «позже» или «нет».","en":"Will you try this today? Please reply: “yes”, “later” or “no”.","uk":"Спробуєте сьогодні? Напишіть: «так», «пізніше» або «ні».","es":"¿Lo intentas hoy? Responde: «sí», «más tarde» o «no»."}[lang])
        return

    if na == "pick_reminder":
        s["mode"]="await_reminder"
        await update.message.reply_text({"ru":"Когда напомнить: «через 4 часа», «вечером», «завтра утром» или «не надо»?","en":"When should I check in: “in 4h”, “this evening”, “tomorrow morning” or “no need”?","uk":"Коли нагадати: «через 4 год», «увечері», «завтра вранці» чи «не треба»?","es":"¿Cuándo te escribo: «en 4 h», «esta tarde», «mañana por la mañana» o «no hace falta»?"}[lang])
        return

    if na == "escalate":
        await update.message.reply_text({
            "ru":"⚠️ Часть ответов тянет на «красные флаги». Лучше связаться с врачом.",
            "en":"⚠️ Some answers look concerning. Please consider contacting a clinician.",
            "uk":"⚠️ Деякі відповіді тривожні. Краще звернутися до лікаря.",
            "es":"⚠️ Algunas respuestas son preocupantes. Considera contactar a un médico.",
        }[lang])
        return

    # иначе остаёмся в свободном диалоге
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

    app.add_handler(CallbackQueryHandler(on_callback))  # совместимость

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
