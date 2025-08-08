import os
import json
import logging
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

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN отсутствует в переменных окружения")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY отсутствует в переменных окружения")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)

client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- Google Sheets ----------
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_env = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not creds_env:
    logging.warning("GOOGLE_CREDENTIALS_JSON не задан — кнопки-отзывы будут работать без записи в таблицу")
    sheet = None
else:
    try:
        creds_dict = json.loads(creds_env)
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client_sheet = gspread.authorize(credentials)
        sheet = client_sheet.open("TendAI Feedback").worksheet("Feedback")
        logging.info("Google Sheets подключены")
    except Exception as e:
        logging.exception(f"Не удалось подключиться к Google Sheets: {e}")
        sheet = None

def add_feedback(user_id, feedback_text):
    if not sheet:
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([timestamp, str(user_id), feedback_text])

# ---------- Память ----------
user_memory = {}
message_counter = {}

# ---------- Быстрые шаблоны ----------
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

# ---------- Кнопки фидбека ----------
def feedback_buttons():
    return InlineKeyboardMarkup([[InlineKeyboardButton("👍 Да", callback_data="feedback_yes"),
                                  InlineKeyboardButton("👎 Нет", callback_data="feedback_no")]])

async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    feedback = query.data
    try:
        add_feedback(user_id, feedback)
    except Exception:
        logging.exception("Ошибка при сохранении отзыва")
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Спасибо за отзыв 🙏")

# ---------- Основной обработчик ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        user_message = (update.message.text or "").strip()
        user_lower = user_message.lower()
        message_counter[user_id] = message_counter.get(user_id, 0) + 1
        logging.info(f"Message from {user_id}: {user_message!r}")

        # Быстрый режим
        if "#60сек" in user_lower or "/fast" in user_lower:
            for keyword, reply in quick_mode_symptoms.items():
                if keyword in user_lower:
                    await update.message.reply_text(reply, reply_markup=feedback_buttons())
                    return
            await update.message.reply_text("❗ Укажи симптом: «#60сек голова» или «/fast stomach».", reply_markup=feedback_buttons())
            return

        # Мини-диалоги по симптомам
        if "голова" in user_lower or "headache" in user_lower:
            await update.message.reply_text(
                "Где именно болит голова — лоб, затылок, виски?\n"
                "Какой характер боли: тупая, острая, пульсирующая?\n"
                "Есть ли тошнота или светобоязнь?"
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

        memory_text = ""
        if user_id in user_memory:
            memory_text = f"(Ты ранее упоминал: {user_memory[user_id]})\n"

        system_prompt = (
            "Ты — заботливый и умный помощник по здоровью и долголетию по имени TendAI.\n"
            "Всегда отвечай на том языке, на котором говорит пользователь.\n"
            "Отвечай по сути, без повторов. При симптоме — 1–2 уточняющих вопроса, 2–3 возможные причины,\n"
            "что делать дома, и когда идти к врачу. Если боли нет — не нагнетай."
        )

        # ВАЖНО: используем доступную модель и ловим ошибки
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
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
            logging.exception("Ошибка OpenAI")
            bot_reply = f"Сервис ИИ временно недоступен: {e}. Попробуй ещё раз позже."

        await update.message.reply_text(bot_reply, reply_markup=feedback_buttons())
    except Exception:
        logging.exception("Критическая ошибка в handle_message")

# ---------- Глобальный обработчик ошибок ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Unhandled error", exc_info=context.error)

# ---------- Удаляем webhook и запускаем polling ----------
async def on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        info = await app.bot.get_webhook_info()
        logging.info(f"Webhook cleared. Current webhook: {info.url!r}")
    except Exception:
        logging.exception("Не удалось удалить webhook")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(feedback_callback))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
