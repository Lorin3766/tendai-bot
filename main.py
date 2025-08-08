import os
import json
import logging
from dotenv import load_dotenv
from langdetect import detect, LangDetectException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from openai import OpenAI
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Загружаем .env
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY or not GOOGLE_CREDENTIALS_JSON:
    raise ValueError("❌ Ошибка: TELEGRAM_TOKEN, OPENAI_API_KEY или GOOGLE_CREDENTIALS_JSON не заданы в .env")

# Подключение OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# Подключение Google Sheets
try:
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client_sheet = gspread.authorize(credentials)
    sheet = client_sheet.open("TendAI Feedback").worksheet("Feedback")
except Exception as e:
    logging.error(f"❌ Не удалось подключиться к Google Sheets: {e}")
    sheet = None

# Память и счётчики
user_memory = {}
message_counter = {}

quick_mode_symptoms = {
    "голова": "Возможные причины: стресс, обезвоживание, усталость.\nЧто делать: отдохните, выпейте воды, при необходимости примите обезболивающее.\nК врачу: если боль резкая или длительная.",
    "спина": "Возможные причины: напряжение мышц, неправильная осанка.\nЧто делать: лёгкая разминка, избегайте нагрузок, тёплый компресс.\nК врачу: при онемении или сильной боли.",
    "живот": "Возможные причины: переедание, спазм, инфекция.\nЧто делать: отдых, тёплая вода, лёгкая диета.\nК врачу: при резкой боли, температуре, рвоте."
}

# Добавление отзыва
def add_feedback(user_id, feedback_text):
    if sheet:
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sheet.append_row([timestamp, str(user_id), feedback_text])
        except Exception as e:
            logging.error(f"Ошибка при сохранении отзыва: {e}")

# Кнопки фидбека
def feedback_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👍 Да", callback_data="feedback_yes"),
         InlineKeyboardButton("👎 Нет", callback_data="feedback_no")]
    ])

# /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет, я TendAI 🤗 Что тебя беспокоит и волнует? Я подскажу, что делать.")

# Обработка фидбека
async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    feedback = query.data
    add_feedback(user_id, feedback)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Спасибо за отзыв 🙏")

# Главная обработка сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return  # Игнорируем не-текстовые сообщения

    user_id = update.effective_user.id
    user_message = update.message.text.strip()
    message_counter[user_id] = message_counter.get(user_id, 0) + 1

    # Определяем язык
    try:
        lang = detect(user_message)
    except LangDetectException:
        lang = "unknown"

    # Режим #60сек
    if "#60сек" in user_message.lower():
        for symptom, advice in quick_mode_symptoms.items():
            if symptom in user_message.lower():
                await update.message.reply_text(advice, reply_markup=feedback_buttons())
                return

    # Пример уточняющего вопроса
    if "болит" in user_message.lower():
        user_memory[user_id] = {"symptom": user_message}
        await update.message.reply_text("Где именно болит? Опиши подробнее.")
        return

    # Системный промпт
    system_prompt = "Ты — TendAI, доброжелательный AI-помощник по здоровью и долголетию. Отвечай кратко и понятно."

    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.7,
            max_tokens=400
        )
        bot_reply = (user_memory.get(user_id, {}).get("symptom", "") + "\n" if user_id in user_memory else "") \
                    + response.choices[0].message["content"].strip()
    except Exception as e:
        logging.error(f"Ошибка OpenAI: {e}")
        bot_reply = "Произошла ошибка при обработке запроса."

    await update.message.reply_text(bot_reply, reply_markup=feedback_buttons())

# Запуск бота
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(feedback_callback))
    app.run_polling()
