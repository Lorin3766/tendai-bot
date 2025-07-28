import os
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)
from openai import OpenAI

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEETS_WEBHOOK = os.getenv("GOOGLE_SHEETS_WEBHOOK")

openai = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()
telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# 🔹 Отправка в Google Таблицу
def send_feedback_to_google_sheets(user_id: int, feedback: str):
    if not GOOGLE_SHEETS_WEBHOOK:
        print("❗ GOOGLE_SHEETS_WEBHOOK не указан в .env")
        return
    try:
        response = requests.post(GOOGLE_SHEETS_WEBHOOK, json={
            "user_id": user_id,
            "feedback": feedback
        })
        print(f"✔️ Отзыв отправлен: {response.status_code}")
    except Exception as e:
        print(f"❌ Ошибка при отправке отзыва: {e}")

# 🔹 Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет, я TendAI 🤗 Что тебя беспокоит или волнует? Я подскажу, что делать."
    )

# 🔹 Обработка текстов
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text

    if user_message.lower() == "tendai support":
        await update.message.reply_text(
            "🔍 Напиши, что тебя беспокоит — я подскажу, что важно проверить."
        )
        return

    try:
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a medical AI assistant. Always respond in the same language the user uses. "
                        "Ask follow-up questions to better understand the issue. "
                        "If the user says 'headache', ask where exactly, what kind of pain (sharp, dull, throbbing), "
                        "how long it has lasted, and whether there are other symptoms like nausea or light sensitivity."
                    ),
                },
                {"role": "user", "content": user_message},
            ],
        )
        bot_reply = response.choices[0].message.content
        await update.message.reply_text(bot_reply)
    except Exception as e:
        await update.message.reply_text("Произошла ошибка. Попробуй позже.")
        print(f"OpenAI Error: {e}")

# 🔹 Обратная связь
async def feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    feedback_text = ' '.join(context.args)
    user_id = update.effective_user.id

    if feedback_text:
        send_feedback_to_google_sheets(user_id, feedback_text)
        await update.message.reply_text("Спасибо за отзыв! Мы обязательно его учтём 🙏")
    else:
        await update.message.reply_text("Пожалуйста, напиши отзыв после команды /feedback.")

# 🔹 Хендлеры
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("feedback", feedback))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# 🔹 Вебхук от Telegram
@app.post("/")
async def root(request: Request):
    json_data = await request.json()
    update = Update.de_json(json_data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}
