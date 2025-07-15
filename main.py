import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

openai = OpenAI(api_key=OPENAI_API_KEY)

# Простая память последних сообщений пользователя
user_memory = {}

def save_to_memory(user_id, message):
    user_memory[user_id] = message

def get_last_message(user_id):
    return user_memory.get(user_id, "")

# Приветствие при /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hi! I'm TendAI 😊 What's bothering you today?")

# Обработка сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    user_id = update.effective_user.id
    save_to_memory(user_id, user_message)

    if user_message.lower() == "tendai support":
        await update.message.reply_text(
            "🔍 Here's quick support from TendAI: tell me what’s bothering you, and I’ll quickly suggest what to check."
        )
        return

    try:
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful and warm medical AI assistant. "
                        "Always reply in the same language the user used. "
                        "Ask relevant follow-up questions to clarify the situation. "
                        "If user says 'headache' – ask: where, what kind (sharp/dull), duration, any nausea or light sensitivity. "
                        "If 'stomach pain' – ask location, nature, duration, any other symptoms. "
                        "If user asks a general question – answer concisely and helpfully."
                    )
                },
                {"role": "user", "content": user_message}
            ]
        )
        bot_reply = response.choices[0].message.content
        await update.message.reply_text(bot_reply)
    except Exception as e:
        await update.message.reply_text("Произошла ошибка. Попробуй позже.")

# Обработка обратной связи
async def feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    feedback_text = ' '.join(context.args)
    if feedback_text:
        print(f"[FEEDBACK] {update.effective_user.username}: {feedback_text}")
        await update.message.reply_text("Спасибо за обратную связь! Мы обязательно учтём её 🙏")
    else:
        await update.message.reply_text("Пожалуйста, напиши отзыв после команды /feedback.")

# Создание и запуск приложения
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("feedback", feedback))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

app.run_polling()
