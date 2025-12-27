import os
import threading

from aiohttp import web
import bot  # همین bot.py


def start_bot():
    # ربات تلگرام را در یک ترد جداگانه بالا می‌آوریم
    t = threading.Thread(target=bot.main, daemon=True)
    t.start()


async def handle_root(request):
    return web.json_response({"status": "ok", "message": "Telegram bot is running"})


def main():
    # اول ربات را استارت می‌کنیم
    start_bot()

    # بعد وب‌سرور aiohttp را بالا می‌آوریم
    app = web.Application()
    app.router.add_get("/", handle_root)

    port = int(os.environ.get("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
