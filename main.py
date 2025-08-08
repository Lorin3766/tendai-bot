import os
import logging
import json
import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)
from openai import OpenAI
from langdetect import detect

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# === Google Sheets настройка ===
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
CREDS_FILE = "credentials.json"
SHEET_NAME = "TendAI Feedback"
WORKSHEET_NAME = "Feedback"

try:
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, SCOPE)
    client_sheet = gspread.authorize(creds)
    sheet = client_sheet.open(SHEET_NAME).worksheet(WORKSHEET_NAME)
    logging.info("✅ Подключение к Google Sheets успешно")
except Exception as e:
    logging.error(f"❌ Ошибка подключения к Google Sheets: {e}")
    sheet = None

# === OpenAI клиент ===
client_ai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Память сообщений
user_sessions = {}

# === Команды ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет, я TendAI. Что тебя беспокоит и волнует? Я подскажу, что делать."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Напиши свой симптом или вопрос, а я подскажу, что делать.")

# === Основная логика ответа ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_text = update.message.text.strip()

    # Определение языка
    try:
        lang = detect(user_text)
    except:
        lang = "unknown"

    # Запоминаем историю
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    user_sessions[user_id].append({"role": "user", "content": user_text})

    # Проверка на режим "Здоровье за 60 секунд"
    if "#60сек" in user_text.lower():
        system_prompt = (
            "Отвечай очень кратко (3 пункта): возможные причины, что делать сейчас, и когда обратиться к врачу."
        )
    else:
        system_prompt = (
            "Ты — заботливый AI-помощник по здоровью и долголетию. "
            "Задавай уточняющие вопросы, если симптом не ясен. "
            "Отвечай коротко, по делу, и по-дружески."
        )

    # Запрос в OpenAI
    try:
        completion = client_ai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                *user_sessions[user_id]
            ],
            temperature=0.7
        )
        bot_reply = completion.choices[0].message["content"]
    except Exception as e:
        logging.error(f"Ошибка OpenAI: {e}")
        bot_reply = "Извини, у меня сейчас техническая проблема."

    # Отправка ответа
    await update.message.reply_text(bot_reply)

    # Сохраняем отзыв в Google Sheets
    if sheet:
        try:
            sheet.append_row([
                str(datetime.datetime.now()),
                str(user_id),
                user_text,
                bot_reply,
                lang
            ])
        except Exception as e:
            logging.error(f"Ошибка записи в Google Sheets: {e}")

# === Запуск бота ===
def main():
    app = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN")).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logging.info("🤖 TendAI запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
