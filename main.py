import os, logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
TOKEN = os.getenv("TELEGRAM_TOKEN")

async def on_startup(app):
    me = await app.bot.get_me()
    logging.info(f"Running as @{me.username}")
    await app.bot.delete_webhook(drop_pending_updates=True)
    logging.info("Webhook cleared")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    logging.info(f"Message from {update.effective_user.id}: {txt!r}")
    await update.message.reply_text(f"echo: {txt}")

app = ApplicationBuilder().token(TOKEN).post_init(on_startup).build()
app.add_handler(CommandHandler("ping", ping))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
