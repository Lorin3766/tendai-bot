import os
import time
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from keep_alive import keep_alive
from openai import OpenAI

# Загрузка переменных среды
load_dotenv()

# Настройка логов
logging.basicConfig(level=logging.INFO)

# Ключи
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# Память пользователя
user_memory = {}
message_counter = {}

# Приветствие
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет, я TendAI 🤗 Что тебя беспокоит и волнует? Я подскажу, что делать.")

# Обработка отзывов по кнопке
async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    feedback = query.data
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Запись в лог-файл
    with open("feedback_log.txt", "a", encoding="utf-8") as f:
        f.write(f"{timestamp} | user_id={user_id} | feedback={feedback}\n")

    logging.info(f"[ОТЗЫВ] {timestamp} | user_id={user_id} | feedback={feedback}")
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Спасибо за отзыв 🙏")

# Основная логика
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text.strip().lower()
    message_counter[user_id] = message_counter.get(user_id, 0) + 1

    # Быстрый режим
    if "#60сек" in user_message or "/fast" in user_message:
        quick = (
            "🔎 Возможные причины: стресс, инфекция, усталость\n"
            "🏠 Что делать: отдых, тёплое питьё, проветривание\n"
            "🧑‍⚕ Когда к врачу: если симптомы сохраняются более 2 дней или усиливаются"
        )
        await update.message.reply_text(quick, reply_markup=feedback_buttons())
        return

    # Уточняющие вопросы
    if "голова" in user_message:
        await update.message.reply_text(
            "Где именно болит голова? Лоб, затылок, виски?\n"
            "Какой характер боли: тупая, острая, пульсирующая?\n"
            "Есть ли ещё симптомы — тошнота, светобоязнь?"
        )
        user_memory[user_id] = "головная боль"
        return
    elif "горло" in user_message:
        await update.message.reply_text(
            "Горло болит при глотании или постоянно?\n"
            "Есть ли температура или кашель?\n"
            "Когда началось?"
        )
        user_memory[user_id] = "боль в горле"
        return
    elif "кашель" in user_message:
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

    # Запрос к OpenAI
    system_prompt = (
        "Ты – заботливый и умный помощник по здоровью. "
        "Отвечай естественно, обоснованно и по-человечески. "
        "Ты – бот TendAI, не врач, но хорошо разбираешься в здоровье и долголетии."
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.7,
            max_tokens=500
        )
        bot_reply = memory_text + response.choices[0].message.content.strip()
    except Exception as e:
        bot_reply = f"Произошла ошибка при обращении к ИИ: {e}"

    await update.message.reply_text(bot_reply, reply_markup=feedback_buttons())

# Кнопки фидбека
def feedback_buttons():
    buttons = [
        [
            InlineKeyboardButton("👍 Да", callback_data="feedback_yes"),
            InlineKeyboardButton("👎 Нет", callback_data="feedback_no")
        ]
    ]
    return InlineKeyboardMarkup(buttons)

# Запуск с авто-перезапуском
if __name__ == "__main__":
    keep_alive()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(feedback_callback))

    print("TendAI запущен!")

    while True:
        try:
            app.run_polling()
        except Exception as e:
            logging.error(f"Произошла ошибка в боте: {e}")
            time.sleep(5)

