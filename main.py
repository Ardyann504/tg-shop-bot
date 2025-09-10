import os
from fastapi import FastAPI, Request, Header, HTTPException
from aiogram.types import Update
from bot import bot, dp  # импорт твоего бота и диспетчера
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # https://...onrender.com/webhook


@app.on_event("startup")
async def on_startup():
    # устанавливаем вебхук, когда сервис запускается
    if WEBHOOK_URL:
        await bot.set_webhook(
            WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True
        )

# рядом с остальным кодом FastAPI в main.py
from fastapi import HTTPException
import os
from bot import bot  # у тебя уже должно быть

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # https://tg-shop-bot-3e7t.onrender.com/webhook
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mysecret123")

@app.get("/set-webhook")
async def set_webhook_get():
    if not WEBHOOK_URL:
        raise HTTPException(400, "WEBHOOK_URL is empty")
    await bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=("message","edited_message","callback_query","my_chat_member","chat_member")
    )
    return {"ok": True, "url": WEBHOOK_URL}

@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Bad secret token")

    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}


@app.get("/")
async def root():
    return {"status": "ok"}
