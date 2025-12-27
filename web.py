import os
import threading

from aiohttp import web
import bot  # همین bot.py خودت


def start_bot():
    # ربات تلگرامی رو تو یه ترد جداگانه بالا می‌آرم تا وب‌سرور همزمان کار کنه
    t = threading.Thread(target=bot.main, daemon=True)
    t.start()


async def handle_root(request):
    return web.json_response({"status": "ok", "message": "Telegram bot is running"})


def main():
    # اول ربات رو استارت کن
    start_bot()

    # بعد وب‌سرور aiohttp رو بالا بیار
    app = web.Application()
    app.router.add_get("/", handle_root)

    port = int(os.environ.get("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)


if name == "main":
    main()
