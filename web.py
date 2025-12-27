import os
import asyncio
from aiohttp import web

import bot  # همین bot.py که کنارش هست


routes = web.RouteTableDef()


@routes.get("/")
async def index(request):
    return web.Response(text="Bot is running ✅")


async def on_startup(app):
    """
    موقع استارت وب‌سرور، ربات تلگرام هم روی همون event loop بالا می‌آد.
    """
    app["bot_task"] = asyncio.create_task(bot.run_bot())


async def on_cleanup(app):
    """
    موقع خاموش شدن وب‌سرور، تسک ربات رو کنسل می‌کنیم.
    """
    task = app.get("bot_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main():
    app = web.Application()
    app.add_routes(routes)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    port = int(os.environ.get("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
