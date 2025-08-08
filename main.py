# main.py
import os
import re
import json
import logging
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# --- Optional deps (no-crash) ---
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
except Exception:
    gspread = None
    ServiceAccountCredentials = None

# ============== Config & Setup ==============
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)

client = OpenAI(api_key=OPENAI_API_KEY) if (OPENAI_API_KEY and OpenAI) else None

# ------------- Google Sheets (optional) -------------
sheet = None
if gspread and ServiceAccountCredentials:
    creds_env = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_env:
        try:
            scope = ["https://spreadsheets.google.com/feeds",
                     "https://www.googleapis.com/auth/drive"]
            creds_dict = json.loads(creds_env)
            credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            gclient = gspread.authorize(credentials)
            sheet = gclient.open("TendAI Feedback").worksheet("Feedback")
            logging.info("Google Sheets connected")
        except Exception as e:
            logging.exception(f"Sheets connect error: {e}")
    else:
        logging.info("GOOGLE_CREDENTIALS_JSON not set — feedback rows will be skipped.")

def add_feedback_row(user, name, rating, comment):
    """
    Append full feedback row. Expected header:
    timestamp | user_id | name | username | rating | comment
    """
    if not sheet:
        return
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        username = user.username or ""
        sheet.append_row([ts, str(user.id), name, username, rating, comment or ""])
    except Exception:
        logging.exception("Failed to append feedback row")

# ============== Memory & Templates ==============
user_memory = {}        # user_id -> last simple symptom tag
pending_feedback = {}   # user_id -> {"name": "feedback_yes|feedback_no", "rating": 1|0}

quick_mode_symptoms = {
    "голова": """[Здоровье за 60 секунд]
💡 Возможные причины: стресс, обезвоживание, недосып
🪪 Что делать: выпей воды, отдохни, проветри комнату
🚨 Когда к врачу: если боль внезапная, сильная, с тошнотой или нарушением зрения""",

    "head": """[Quick Health Check]
💡 Possible causes: stress, dehydration, fatigue
🪪 Try: rest, hydration, fresh air
🚨 See a doctor if pain is sudden, severe, or with nausea/vision issues""",

    "живот": """[Здоровье за 60 секунд]
💡 Возможные причины: гастрит, питание, стресс
🪪 Что делать: тёплая вода, покой, исключи еду на 2 часа
🚨 Когда к врачу: если боль резкая, с температурой, рвотой или длится >1 дня""",

    "stomach": """[Quick Health Check]
💡 Possible causes: gastritis, poor diet, stress
🪪 Try: warm water, rest, skip food for 2 hours
🚨 See a doctor if pain is sharp, with fever or vomiting""",

    "слабость": """[Здоровье за 60 секунд]
💡 Возможные причины: усталость, вирус, анемия
🪪 Что делать: отдых, поешь, выпей воды
🚨 Когда к врачу: если слабость длится >2 дней или нарастает""",

    "weakness": """[Quick Health Check]
💡 Possible causes: fatigue, virus, low iron
🪪 Try: rest, eat, hydrate
🚨 Doctor: if weakness lasts >2 days or gets worse"""
}

def feedback_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👍 Да", callback_data="feedback_yes"),
         InlineKeyboardButton("👎 Нет", callback_data="feedback_no")]
    ])

# ====== Light language detection (no paid libs, no crashes) ======
try:
    from langdetect import detect as ld_detect
except Exception:
    ld_detect = None

def detect_lang(text: str, tg_lang: str | None = None) -> str:
    """langdetect if available -> heuristics (UK letters / any Cyrillic) -> Telegram UI lang -> en"""
    if ld_detect:
        try:
            return ld_detect(text)
        except Exception:
            pass
    t = text.lower()
    if any(ch in t for ch in "іїєґ"):
        return "uk"
    if re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", text):
        if sum(t.count(ch) for ch in "іїєґ") >= 2:
            return "uk"
        return "ru"
    if tg_lang:
        if tg_lang.startswith("en"):
            return "en"
        if tg_lang.startswith(("uk", "ru")):
            return tg_lang[:2]
    return "en"

SYS_PROMPT = {
    "en": ("You are TendAI, a caring, concise health assistant. Reply in the user's language (English here). "
           "If a symptom appears: ask 1–2 clarifying questions, give 2–3 possible causes, simple home care, "
           "and when to see a doctor. Be calm and clear."),
    "ru": ("Ты — заботливый и понятный помощник по здоровью TendAI. Отвечай на языке пользователя "
           "(здесь — русский). Если есть симптом: 1–2 уточняющих вопроса, 2–3 возможные причины, "
           "что сделать дома и когда идти к врачу. Спокойно и по делу."),
    "uk": ("Ти — турботливий і зрозумілий помічник зі здоров'я TendAI. Відповідай мовою користувача "
           "(тут — українською). Якщо є симптом: 1–2 уточнювальні запитання, 2–3 можливі причини, "
           "що зробити вдома і коли до лікаря. Спокійно і по суті."),
}

FALLBACK = {
    "en": "I need a bit more information to help. Where exactly does it hurt? How long has it lasted?",
    "ru": "Мне нужно немного больше информации, чтобы помочь. Где именно болит и как давно это началось?",
    "uk": "Мені потрібно трохи більше інформації, щоб допомогти. Де саме болить і відколи це триває?",
}

# ============== Lifecycle / Commands ==============
async def on_startup(app):
    me = await app.bot.get_me()
    logging.info(f"Running as @{me.username}")
    # чистим вебхук на всякий случай, чтобы polling не конфликтовал
    await app.bot.delete_webhook(drop_pending_updates=True)
    logging.info("Webhook cleared")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Как я могу помочь тебе сегодня? Есть какие-то вопросы о здоровье?",
        reply_markup=feedback_buttons()
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def skip_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in pending_feedback:
        data = pending_feedback.pop(uid)
        add_feedback_row(update.effective_user, data["name"], data["rating"], "")
        await update.message.reply_text("Окей, без комментария. Спасибо! 🙏")
    else:
        await update.message.reply_text("Сейчас нечего пропускать 🙂")

# ============== Feedback (buttons) ==============
async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
        await q.answer()
        user = q.from_user
        choice = q.data  # feedback_yes | feedback_no
        rating = 1 if choice == "feedback_yes" else 0

        pending_feedback[user.id] = {"name": choice, "rating": rating}

        # убираем кнопки под исходным сообщением (если возможно)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        await q.message.reply_text(
            "Спасибо за оценку 🙏\n"
            "Хочешь добавить комментарий? Просто напиши сообщение в ответ.\n"
            "Или отправь /skip, чтобы пропустить."
        )
    except Exception:
        logging.exception("feedback_callback error")

# ============== Main handler ==============
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        user_id = user.id
        text = (update.message.text or "").strip()
        low = text.lower()
        tg_lang = getattr(user, "language_code", None)
        lang_code = detect_lang(text, tg_lang)

        logging.info(f"Message from {user_id} ({tg_lang}/{lang_code}): {text!r}")

        # 1) Ожидаем комментарий к отзыву
        if user_id in pending_feedback and not text.startswith("/"):
            data = pending_feedback.pop(user_id)
            add_feedback_row(user, data["name"], data["rating"], text)
            done_msg = {
                "en": "Comment saved, thank you! 🙌",
                "ru": "Комментарий сохранён, спасибо! 🙌",
                "uk": "Коментар збережено, дякуємо! 🙌",
            }.get(lang_code, "Comment saved, thank you! 🙌")
            await update.message.reply_text(done_msg)
            return

        # 2) Быстрый режим
        if "#60сек" in low or "/fast" in low:
            for k, reply in quick_mode_symptoms.items():
                if k in low:
                    await update.message.reply_text(reply, reply_markup=feedback_buttons())
                    return
            msg = {
                "en": "❗ Specify a symptom: “/fast stomach” or “#60sec head”.",
                "ru": "❗ Укажи симптом: «#60сек голова» или «/fast stomach».",
                "uk": "❗ Вкажи симптом: «#60сек голова» або «/fast stomach».",
            }.get(lang_code, "❗ Specify a symptom: “/fast stomach” or “#60sec head”.")
            await update.message.reply_text(msg, reply_markup=feedback_buttons())
            return

        # 3) Простые мини-диалоги (ру/ук/англ)
        if "голова" in low or "headache" in low:
            msg = {
                "en": "Where exactly is the headache (forehead, back, temples)? What type (dull, sharp, pulsating)? Any nausea or light sensitivity?",
                "ru": "Где именно болит голова: лоб, затылок, виски? Какой характер: тупая, острая, пульсирующая? Есть ли тошнота/светобоязнь?",
                "uk": "Де саме болить голова: лоб, потилиця, скроні? Який характер: тупий, гострий, пульсівний? Є нудота/світлобоязнь?",
            }.get(lang_code)
            await update.message.reply_text(msg)
            user_memory[user_id] = {
                "en": "headache", "ru": "головная боль", "uk": "головний біль"
            }.get(lang_code, "headache")
            return

        if "горло" in low or "throat" in low:
            msg = {
                "en": "Does it hurt when swallowing or constantly? Any fever or cough? When did it start?",
                "ru": "Болит при глотании или постоянно? Есть температура или кашель? Когда началось?",
                "uk": "Болить під час ковтання чи постійно? Є температура або кашель? Коли почалося?",
            }.get(lang_code)
            await update.message.reply_text(msg)
            user_memory[user_id] = {"en":"sore throat","ru":"боль в горле","uk":"біль у горлі"}.get(lang_code, "sore throat")
            return

        if "кашель" in low or "cough" in low:
            msg = {
                "en": "Is the cough dry or productive? How long? Any fever, chest pain or shortness of breath?",
                "ru": "Кашель сухой или с мокротой? Давно? Есть ли температура, боль в груди или одышка?",
                "uk": "Кашель сухий чи з мокротою? Давно? Є температура, біль у грудях або задишка?",
            }.get(lang_code)
            await update.message.reply_text(msg)
            user_memory[user_id] = {"en":"cough","ru":"кашель","uk":"кашель"}.get(lang_code, "cough")
            return

        # 4) Ответ ИИ (или фоллбек)
        reply_text = FALLBACK.get(lang_code, FALLBACK["en"])
        if client:
            try:
                system_prompt = SYS_PROMPT.get(lang_code, SYS_PROMPT["en"])
                resp = client.chat_completions.create(  # fallback if older SDK; try new:
                    model="gpt-4o-mini",
                    messages=[{"role":"system","content":system_prompt},
                              {"role":"user","content":text}],
                    temperature=0.6,
                    max_tokens=400
                )
                # Some SDKs use client.chat.completions.create; try both:
                if not hasattr(resp, "choices"):
                    resp = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role":"system","content":system_prompt},
                                  {"role":"user","content":text}],
                        temperature=0.6,
                        max_tokens=400
                    )
                reply_text = (resp.choices[0].message.content or "").strip()
                if user_id in user_memory:
                    prefix_map = {
                        "en": f"(You previously mentioned: {user_memory[user_id]})\n",
                        "ru": f"(Ранее упоминал: {user_memory[user_id]})\n",
                        "uk": f"(Раніше згадував: {user_memory[user_id]})\n",
                    }
                    reply_text = prefix_map.get(lang_code, "") + reply_text
            except Exception as e:
                logging.exception("OpenAI error")
                reply_text = f"{FALLBACK.get(lang_code, FALLBACK['en'])}\n\n(LLM error: {e})"

        await update.message.reply_text(reply_text, reply_markup=feedback_buttons())

    except Exception:
        logging.exception("handle_message fatal error")

# ============== Runner ==============
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("skip", skip_comment))
    app.add_handler(CallbackQueryHandler(feedback_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)
