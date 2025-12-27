import os
import asyncio
from aiohttp import web

import bot  # همین bot.py که کنارشه


routes = web.RouteTableDef()


@routes.get("/")
async def index(request):
    return web.Response(text="Bot is running ✅")


async def start_bot(app):
    """
    این تابع موقع استارت aiohttp صدا زده می‌شه
    و bot.main() رو تو یه ترد جدا اجرا می‌کنه
    تا بلاک نشه.
    """
    loop = asyncio.get_event_loop()
    # bot.main بلاک‌کننده‌ست (run_until_disconnected)، پس تو ترد جدا اجراش می‌کنیم
    loop.create_task(asyncio.to_thread(bot.main))


def main():
    app = web.Application()
    app.add_routes(routes)

    # موقع استارت، ربات تلگرام رو هم بالا بیار
    app.on_startup.append(start_bot)

    port = int(os.environ.get("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
