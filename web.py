import os
import asyncio

from aiohttp import web
import bot  # همین bot.py که کنارش هست


async def start_bot():
    """
    اینجا خود ربات را روی همان event loop بالا می‌آوریم
    """
    print("Initializing bot...")

    # لود کردن تنظیمات و ادمین‌ها و اکانت‌ها
    bot.load_admins()
    bot.load_settings()
    bot.load_accounts()

    # استارت کلاینت تلگرام با BOT_TOKEN
    await bot.client.start(bot_token=bot.BOT_TOKEN)

    print("Bot started in web service mode.")

    # یک تسک برای run_until_disconnected ایجاد می‌کنیم تا همیشه گوش بده
    asyncio.create_task(bot.client.run_until_disconnected())
    print("Bot run_until_disconnected task created.")


async def handle_root(request):
    return web.json_response({"status": "ok", "message": "Telegram bot is running"})


async def on_startup(app):
    # وقتی وب‌سرور بالا می‌آید، ربات هم استارت می‌شود
    await start_bot()


async def on_cleanup(app):
    # موقع خاموش شدن وب‌سرور، کلاینت تلگرام را هم می‌بندیم
    try:
        await bot.client.disconnect()
    except Exception:
        pass


def main():
    app = web.Application()
    app.router.add_get("/", handle_root)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    port = int(os.environ.get("PORT", "10000"))
    print(f"Starting web server on port {port} ...")
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
