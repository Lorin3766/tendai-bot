import os, json, logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from openai import OpenAI
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ---------- Базовая настройка ----------
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN отсутствует")
if not OPENAI_API_KEY:
    logging.warning("OPENAI_API_KEY не задан — ответы ИИ работать не будут")

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ---------- Google Sheets: безопасная инициализация ----------
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
sheet = None
creds_env = os.getenv("GOOGLE_CREDENTIALS_JSON")
if creds_env:
    try:
        creds_dict = json.loads(creds_env)
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gclient = gspread.authorize(credentials)
        sheet = gclient.open("TendAI Feedback").worksheet("Feedback")
        logging.info("Google Sheets подключены")
    except Exception as e:
        logging.exception(f"Sheets error: {e}")
else:
    logging.info("GOOGLE_CREDENTIALS_JSON не задан — отзывы писать не будем")

def add_feedback(user_id, feedback_text):
    if not sheet:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([ts, str(user_id), feedback_text])

# ---------- Память/шаблоны ----------
user_memory = {}
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

# ---------- Команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет, я TendAI 🤗 Что тебя беспокоит и волнует? Я подскажу, что делать.")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

def feedback_buttons():
    return InlineKeyboardMarkup([[InlineKeyboardButton("👍 Да", callback_data="feedback_yes"),
                                  InlineKeyboardButton("👎 Нет", callback_data="feedback_no")]])

async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
        await q.answer()
        add_feedback(q.from_user.id, q.data)
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("Спасибо за отзыв 🙏")
    except Exception:
        logging.exception("feedback_callback error")

# ---------- Основной хэндлер ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        user_message = (update.message.text or "").strip()
        user_lower = user_message.lower()
        logging.info(f"Message from {user_id}: {user_message!r}")

        # Быстрый режим
        if "#60сек" in user_lower or "/fast" in user_lower:
            for keyword, reply in quick_mode_symptoms.items():
                if keyword in user_lower:
                    await update.message.reply_text(reply, reply_markup=feedback_buttons())
                    return
            await update.message.reply_text("❗ Укажи симптом, например: «#60сек голова» или «/fast stomach».", reply_markup=feedback_buttons())
            return

        # Мини-диалоги
        if "голова" in user_lower or "headache" in user_lower:
            await update.message.reply_text(
                "Где именно болит голова? Лоб, затылок, виски?\n"
                "Какой характер боли: тупая, острая, пульсирующая?\n"
                "Есть ли ещё симптомы — тошнота, светобоязнь?"
            )
            user_memory[user_id] = "головная боль"
            return

        if "горло" in user_lower or "throat" in user_lower:
            await update.message.reply_text(
                "Горло болит при глотании или постоянно?\n"
                "Есть ли температура или кашель?\n"
                "Когда началось?"
            )
            user_memory[user_id] = "боль в горле"
            return

        if "кашель" in user_lower or "cough" in user_lower:
            await update.message.reply_text(
                "Кашель сухой или с мокротой?\n"
                "Давно ли он у вас?\n"
                "Есть ли температура, боль в груди или одышка?"
            )
            user_memory[user_id] = "кашель"
            return

        memory_text = f"(Ты ранее упоминал: {user_memory[user_id]})\n" if user_id in user_memory else ""

        system_prompt = (
            "Ты — заботливый и умный помощник по здоровью и долголетию по имени TendAI.\n"
            "Всегда отвечай на том языке, на котором говорит пользователь.\n"
            "Отвечай по сути, без повторов. Если есть симптом — 1–2 уточняющих вопроса, 2–3 причины,\n"
            "что делать дома, и когда идти к врачу."
        )

        bot_reply = "Мне нужно чуть больше деталей. Что именно беспокоит?"
        if client:
            try:
                response = client.chat.completions.create(
                    model="gpt-4o-mini",  # стабильная модель для chat.completions
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message}
                    ],
                    temperature=0.6,
                    max_tokens=400,
                    timeout=30
                )
                bot_reply = memory_text + (response.choices[0].message.content or "").strip()
            except Exception as e:
                logging.exception("OpenAI error")
                bot_reply = f"Сервис ИИ временно недоступен: {e}"

        await update.message.reply_text(bot_reply, reply_markup=feedback_buttons())

    except Exception:
        logging.exception("handle_message fatal error")

# ---------- Логи старта и очистка webhook ----------
async def on_startup(app):
    try:
        me = await app.bot.get_me()
        logging.info(f"Running as @{me.username}")
        await app.bot.delete_webhook(drop_pending_updates=True)
        logging.info("Webhook cleared")
    except Exception:
        logging.exception("Startup hook failed")

# ---------- Запуск ----------
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(feedback_callback))
    app.run_polling(drop_pending_updates=True)
