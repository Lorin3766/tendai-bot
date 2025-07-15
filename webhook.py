import os
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

openai = OpenAI(api_key=OPENAI_API_KEY)

# Создаём FastAPI app
app = FastAPI()

telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет, я TendAI. Что тебя беспокоит или волнует? Я подскажу, что делать."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    if user_message.lower() == "tendai support":
        await update.message.reply_text(
            "🔍 Вот краткая поддержка от TendAI: опиши, что тебя беспокоит, и я сразу подскажу, что важно проверить."
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
                        "how long it has lasted, and whether there are other symptoms like nausea or light sensitivity. "
                        "If it's 'stomach pain', ask for location, character, duration, and other symptoms."
                    ),
                },
                {"role": "user", "content": user_message},
            ],
        )
        bot_reply = response.choices[0].message.content
        await update.message.reply_text(bot_reply)
    except Exception as e:
        await update.message.reply_text("Произошла ошибка. Попробуй позже.")


async def feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    feedback_text = ' '.join(context.args)
    if feedback_text:
        print(f"[FEEDBACK] {update.effective_user.username}: {feedback_text}")
        await update.message.reply_text("Спасибо за обратную связь! Мы обязательно учтём её 🙏")
    else:
        await update.message.reply_text("Пожалуйста, напиши отзыв после команды /feedback.")


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("feedback", feedback))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


@app.post("/")
async def root(request: Request):
    json_data = await request.json()
    update = Update.de_json(json_data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}
