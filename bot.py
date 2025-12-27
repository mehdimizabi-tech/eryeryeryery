import os
import csv
import io
import re
import asyncio
import traceback
import json

from telethon import TelegramClient, events, Button
from telethon.tl.functions.messages import GetDialogsRequest
from telethon.tl.types import InputPeerEmpty, InputPeerChannel, InputPeerUser
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.errors.rpcerrorlist import PeerFloodError, UserPrivacyRestrictedError


# ------------------ تنظیمات محیطی (برای خود ربات Bot) ------------------

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("API_ID / API_HASH / BOT_TOKEN باید تو Environment Variable ست بشن.")

BOT_SESSION = "bot_session"

client = TelegramClient(BOT_SESSION, API_ID, API_HASH)

# ------------------ فایل‌های داده ------------------

ADMINS_FILE = "admins.json"
SETTINGS_FILE = "settings.json"
ACCOUNTS_FILE = "accounts.json"

ADMINS = set()         # ادمین‌ها (آی‌دی عددی)
INVITE_DELAY = 60      # تاخیر بین هر اد (ثانیه)

ACCOUNTS = []          # اکانت‌ها برای add user
ACTIVE_ACCOUNT = None  # نام اکانت فعال برای add user

account_clients = {}   # name -> TelegramClient (اکانت‌ها برای add user)

user_states = {}       # user_id -> {"mode": ..., "step": ..., "temp": {...}}

# برای ویزارد جدید export (شماره → کد → chat_id)
export_clients = {}    # user_id -> {"client": TelegramClient, "phone": str}

# وضعیت گروه‌ها برای add user
groups_cache = []              # لیست گروه‌ها برای add user
target_group = None            # گروه انتخاب‌شده برای add user
awaiting_group_number = False  # آیا منتظر عدد گروه هستیم یا نه


# ------------------ load/save ها ------------------

def load_admins():
    global ADMINS
    try:
        with open(ADMINS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            ADMINS = set(data.get("admins", []))
    except FileNotFoundError:
        ADMINS = set()
    except Exception:
        ADMINS = set()


def save_admins():
    try:
        with open(ADMINS_FILE, "w", encoding="utf-8") as f:
            json.dump({"admins": list(ADMINS)}, f, ensure_ascii=False, indent=2)
    except Exception:
        traceback.print_exc()


def load_settings():
    global INVITE_DELAY
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            INVITE_DELAY = int(data.get("invite_delay", 60))
    except FileNotFoundError:
        INVITE_DELAY = 60
    except Exception:
        INVITE_DELAY = 60


def save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"invite_delay": INVITE_DELAY}, f, ensure_ascii=False, indent=2)
    except Exception:
        traceback.print_exc()


def load_accounts():
    global ACCOUNTS, ACTIVE_ACCOUNT
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            ACCOUNTS = data.get("accounts", [])
            ACTIVE_ACCOUNT = data.get("active", None)
    except FileNotFoundError:
        ACCOUNTS = []
        ACTIVE_ACCOUNT = None
    except Exception:
        ACCOUNTS = []
        ACTIVE_ACCOUNT = None


def save_accounts():
    data = {
        "active": ACTIVE_ACCOUNT,
        "accounts": ACCOUNTS,
    }
    try:
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        traceback.print_exc()


# ------------------ ادمین و اکانت برای add user ------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def get_account_by_name(name: str):
    for acc in ACCOUNTS:
        if acc["name"] == name:
            return acc
    return None

def list_accounts_text() -> str:
    if not ACCOUNTS:
        return "هیچ اکانتی ثبت نشده."
    lines = ["اکانت‌های ثبت‌شده:\n"]
    for acc in ACCOUNTS:
        mark = "(active)" if acc["name"] == ACTIVE_ACCOUNT else ""
        lines.append(f"* {acc['name']} {mark}\n  phone: {acc['phone']}")
    return "\n".join(lines)


async def get_account_client(name: str) -> TelegramClient:
    """client مربوط به یک اکانت (برای add user)"""
    acc = get_account_by_name(name)
    if not acc:
        raise RuntimeError("اکانت پیدا نشد.")

    if name in account_clients:
        c = account_clients[name]
    else:
        c = TelegramClient(acc["session_name"], acc["api_id"], acc["api_hash"])
        account_clients[name] = c

    if not c.is_connected():
        await c.connect()
    return c


def set_active_account(name: str):
    global ACTIVE_ACCOUNT
    ACTIVE_ACCOUNT = name
    save_accounts()


# ------------------ منوی اصلی ------------------

def main_menu():
    return [
        [
            Button.text("➕ افزودن اکانت"),
            Button.text("📜 اکانت‌ها"),
        ],
        [
            Button.text("🧾 گروه‌ها"),
            Button.text("📤 خروج اعضا"),
        ],
        [
            Button.text("⏱ تنظیم تاخیر"),
        ],
    ]


async def send_main_menu(chat_id, text="از منوی زیر استفاده کن:"):
    await client.send_message(chat_id, text, buttons=main_menu())


# ------------------ گروه‌ها برای add user ------------------

async def fetch_groups_for_active():
    """لیست گروه‌های اکانت فعال برای add user"""
    global groups_cache
    if not ACTIVE_ACCOUNT:
        raise RuntimeError("هیچ اکانت فعالی انتخاب نشده.")
    user_client = await get_account_client(ACTIVE_ACCOUNT)

    result = await user_client(GetDialogsRequest(
        offset_date=None,
        offset_id=0,
        offset_peer=InputPeerEmpty(),
        limit=200,
        hash=0
    ))
    groups_cache = [c for c in result.chats if getattr(c, "megagroup", False)]
    return groups_cache


def groups_text():
    if not groups_cache:
        return "هیچ سوپرگروهی یافت نشد (یا این اکانت در سوپرگروهی نیست)."
    lines = [f"اکانت فعال برای add user: {ACTIVE_ACCOUNT}\n", "لیست سوپرگروه‌ها:"]
    for i, g in enumerate(groups_cache):
        lines.append(f"{i}: {g.title}")
    lines.append("\nیک عدد بفرست تا همان گروه برای add user انتخاب شود.")
    return "\n".join(lines)


def sanitize_filename(title: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower())
    return f"members-{safe}.csv"


async def add_users_from_csv_file(file_path, chat_id):
    """add user از CSV با استفاده از اکانت فعال و گروه انتخاب‌شده"""
    global target_group
    if not ACTIVE_ACCOUNT:
        await client.send_message(chat_id, "هیچ اکانتی برای add user فعال نیست. اول اکانت را تنظیم کن.")
        return
    if target_group is None:
        await client.send_message(chat_id, "هیچ گروهی برای add user انتخاب نشده. از دکمه 🧾 گروه‌ها استفاده کن.")
        return

    user_client = await get_account_client(ACTIVE_ACCOUNT)

    users = []
    try:
        with open(file_path, encoding="utf-8") as f:
            reader = csv.reader(f, delimiter=",", lineterminator="\n")
            next(reader, None)
            for row in reader:
                if len(row) < 3:
                    continue
                users.append({
                    "username": row[0],
                    "id": int(row[1]) if row[1] else 0,
                    "access_hash": int(row[2]) if row[2] else 0
                })
    except Exception as e:
        await client.send_message(chat_id, f"خطا در خواندن CSV:\n{e}")
        traceback.print_exc()
        return

    target_entity = InputPeerChannel(target_group.id, target_group.access_hash)
    await client.send_message(chat_id, f"شروع اضافه کردن {len(users)} کاربر به گروه: {target_group.title}")

    for idx, user in enumerate(users, start=1):
        username_or_id = user["username"] or f"id:{user['id']}"
        try:
            await client.send_message(chat_id, f"[{idx}/{len(users)}] در حال اضافه کردن: {username_or_id}")

if user["username"]:
                user_entity = await user_client.get_input_entity(user["username"])
            else:
                user_entity = InputPeerUser(user["id"], user["access_hash"])

            await user_client(InviteToChannelRequest(target_entity, [user_entity]))
            await client.send_message(chat_id, f"✅ اضافه شد: {username_or_id}")

            await asyncio.sleep(INVITE_DELAY)

        except PeerFloodError:
            await client.send_message(chat_id, "⛔ خطای Flood از سمت تلگرام. روند متوقف شد.")
            break
        except UserPrivacyRestrictedError:
            await client.send_message(chat_id, f"⚠️ محدودیت حریم خصوصی، رد شد: {username_or_id}")
        except Exception as e:
            await client.send_message(chat_id, f"⚠️ خطا برای {username_or_id}:\n{e}")
            traceback.print_exc()

    await client.send_message(chat_id, "پروسه اد کردن کاربران تمام شد.")


# ------------------ state handler برای ویزاردها ------------------

async def handle_state_message(event, state):
    """پیام‌هایی که وسط ویزاردها (addacc / setdelay / export) هستیم"""
    user_id = event.sender_id
    chat_id = event.chat_id
    text = (event.raw_text or "").strip()
    mode = state.get("mode")
    step = state.get("step")
    temp = state.get("temp", {})

    # ---------- ویزارد افزودن اکانت برای add user ----------
    if mode == "addacc":
        if step == "name":
            name = text
            if get_account_by_name(name):
                await event.reply("این نام قبلاً وجود دارد، یک نام دیگر بفرست.")
                return
            temp["name"] = name
            state["step"] = "api_id"
            state["temp"] = temp
            user_states[user_id] = state
            await event.reply("حالا API_ID را بفرست (عدد):")
            return

        if step == "api_id":
            if not text.isdigit():
                await event.reply("API_ID باید عدد باشد. دوباره بفرست:")
                return
            temp["api_id"] = int(text)
            state["step"] = "api_hash"
            state["temp"] = temp
            user_states[user_id] = state
            await event.reply("حالا API_HASH را بفرست:")
            return

        if step == "api_hash":
            temp["api_hash"] = text
            state["step"] = "phone"
            state["temp"] = temp
            user_states[user_id] = state
            await event.reply("شماره تلفن اکانت را با فرمت +98912... بفرست:")
            return

        if step == "phone":
            phone = text
            temp["phone"] = phone
            name = temp["name"]
            api_id = temp["api_id"]
            api_hash = temp["api_hash"]
            session_name = f"session_{name}"

            ACCOUNTS.append({
                "name": name,
                "phone": phone,
                "api_id": api_id,
                "api_hash": api_hash,
                "session_name": session_name
            })
            save_accounts()

            try:
                user_client = await get_account_client(name)
                await user_client.send_code_request(phone)
                state["step"] = "code"
                state["temp"] = temp
                user_states[user_id] = state
                await event.reply(
                    f"کد به شماره {phone} ارسال شد.\nکد را همینجا بفرست (فقط عدد):"
                )
            except Exception as e:
                await event.reply(f"خطا در ارسال کد:\n{e}")
                traceback.print_exc()
                ACCOUNTS.remove(get_account_by_name(name))
                save_accounts()
                user_states.pop(user_id, None)
            return

        if step == "code":
            code = text
            name = temp["name"]
            phone = temp["phone"]
            try:
                user_client = await get_account_client(name)
                await user_client.sign_in(phone=phone, code=code)
                await event.reply(f"✅ اکانت {name} برای add user لاگین شد.")

global ACTIVE_ACCOUNT
                if not ACTIVE_ACCOUNT:
                    ACTIVE_ACCOUNT = name
                save_accounts()

                user_states.pop(user_id, None)
                await send_main_menu(chat_id, "اکانت اضافه شد. از منو ادامه بده:")
            except Exception as e:
                await event.reply(f"خطا در تایید کد:\n{e}")
                traceback.print_exc()
            return

    # ---------- ویزارد تنظیم تاخیر ----------
    if mode == "setdelay":
        if not text.isdigit():
            await event.reply("تاخیر باید عدد (ثانیه) باشد. دوباره بفرست:")
            return
        global INVITE_DELAY
        INVITE_DELAY = int(text)
        if INVITE_DELAY < 1:
            INVITE_DELAY = 1
        save_settings()
        user_states.pop(user_id, None)
        await event.reply(f"✅ تاخیر بین ادها روی {INVITE_DELAY} ثانیه تنظیم شد.")
        await send_main_menu(chat_id)
        return

    # ---------- ویزارد جدید export (شماره → کد → chat_id) ----------
    if mode == "export":
        # مرحله ۱: phone
        if step == "phone":
            phone = text
            temp["phone"] = phone
            state["temp"] = temp
            user_states[user_id] = state

            session_name = "export_" + re.sub(r"[^0-9]+", "", phone)
            uclient = TelegramClient(session_name, API_ID, API_HASH)
            export_clients[user_id] = {"client": uclient, "phone": phone}

            try:
                await uclient.connect()
                if await uclient.is_user_authorized():
                    state["step"] = "chat_id"
                    user_states[user_id] = state
                    await event.reply(
                        "قبلاً با این شماره لاگین شده‌ای.\n"
                        "حالا chat_id گروه را بفرست (مثلاً -1001234567890):"
                    )
                else:
                    await uclient.send_code_request(phone)
                    state["step"] = "code"
                    user_states[user_id] = state
                    await event.reply("کد ارسال‌شده به تلگرام را بفرست (فقط عدد):")
            except Exception as e:
                await event.reply(f"خطا در اتصال/ارسال کد:\n{e}")
                traceback.print_exc()
                export_clients.pop(user_id, None)
                user_states.pop(user_id, None)
            return

        # مرحله ۲: code
        if step == "code":
            info = export_clients.get(user_id)
            if not info:
                await event.reply("خطا: سشن export پیدا نشد. دوباره دکمه 📤 خروج اعضا را بزن.")
                user_states.pop(user_id, None)
                return
            uclient = info["client"]
            phone = info["phone"]
            code = text
            try:
                await uclient.sign_in(phone=phone, code=code)
                state["step"] = "chat_id"
                user_states[user_id] = state
                await event.reply(
                    "✅ لاگین شدی.\n"
                    "حالا chat_id گروه را بفرست (مثلاً -1001234567890):"
                )
            except Exception as e:
                await event.reply(f"خطا در لاگین:\n{e}")
                traceback.print_exc()
            return

        # مرحله ۳: chat_id
        if step == "chat_id":
            info = export_clients.get(user_id)
            if not info:
                await event.reply("خطا: سشن export پیدا نشد. دوباره دکمه 📤 خروج اعضا را بزن.")
                user_states.pop(user_id, None)
                return
            uclient = info["client"]
            try:
                chat_id_val = int(text)
                entity = await uclient.get_entity(chat_id_val)
                participants = await uclient.get_participants(entity, aggressive=True)

                buffer = io.StringIO()
                writer = csv.writer(buffer, delimiter=",", lineterminator="\n")
                writer.writerow(["username", "user_id", "access_hash", "name", "group", "group_id"])

for u in participants:
                    name = " ".join(filter(None, [u.first_name, u.last_name]))
                    writer.writerow([
                        u.username or "",
                        u.id,
                        u.access_hash,
                        name,
                        getattr(entity, "title", "chat"),
                        chat_id_val
                    ])

                csv_bytes = buffer.getvalue().encode("utf-8")
                buffer.close()

                filename = sanitize_filename(getattr(entity, "title", "chat"))
                await client.send_file(
                    chat_id,
                    csv_bytes,
                    filename=filename,
                    caption=f"تعداد اعضا: {len(participants)}"
                )

                await uclient.disconnect()
                export_clients.pop(user_id, None)
                user_states.pop(user_id, None)
                await send_main_menu(chat_id, "خروج اعضا انجام شد. از منو ادامه بده:")
            except Exception as e:
                await event.reply(f"خطا در گرفتن اعضای گروه:\n{e}")
                traceback.print_exc()
            return


# ------------------ هندل اصلی پیام‌ها ------------------

@client.on(events.NewMessage)
async def main_handler(event):
    global awaiting_group_number, target_group

    user_id = event.sender_id
    chat_id = event.chat_id
    text = (event.raw_text or "").strip()

    # /me -> آی‌دی عددی
    if text == "/me":
        await event.reply(f"آی‌دی عددی شما: {user_id}", parse_mode="markdown")
        return

    # /setmeadmin
    if text == "/setmeadmin":
        if not ADMINS:
            ADMINS.add(user_id)
            save_admins()
            await event.reply("✅ شما به عنوان ادمین اصلی ثبت شدید.")
            await send_main_menu(chat_id)
        else:
            if is_admin(user_id):
                await event.reply("شما قبلاً ادمین هستید.")
            else:
                await event.reply("ادمین قبلاً تعریف شده. فقط ادمین‌ها می‌توانند ادمین جدید اضافه کنند.")
        return

    # /start
    if text == "/start":
        if is_admin(user_id):
            await event.reply(
                "سلام ادمین 👋\n"
                "از دکمه‌های زیر برای مدیریت استفاده کن.\n\n"
                "دستورات تکمیلی:\n"
                "/accounts  → لیست اکانت‌ها (برای add user)\n"
                "/useacc <name> → انتخاب اکانت فعال برای add user\n"
                "/delacc <name> → حذف اکانت\n"
                "/admins → لیست ادمین‌ها\n"
                "/addadmin <id> /deladmin <id>\n"
                "/setdelay <sec> → تاخیر اد از CSV",
            )
            await send_main_menu(chat_id)
        else:
            await event.reply(
                "سلام 👋\n"
                "برای ادمین شدن (اگر هنوز ادمینی ثبت نشده) از دستور زیر استفاده کن:\n"
                "/setmeadmin\n"
                "برای دیدن آی‌دی عددی خودت:\n"
                "/me",
                parse_mode="markdown"
            )
        return

    # اگر ادمین نیستی، کاری نکن
    if not is_admin(user_id):
        return

    # اگر وسط ویزارد هستیم و پیام دستور / نیست، بفرست به state handler
    if user_id in user_states and not text.startswith("/"):
        await handle_state_message(event, user_states[user_id])
        return

    # ---------- مدیریت ادمین‌ها ----------

    if text == "/admins":
        if not ADMINS:
            await event.reply("هیچ ادمینی ثبت نشده.")
        else:
            ids_text = "\n".join(str(a) for a in ADMINS)
            await event.reply(f"لیست ادمین‌ها (آی‌دی عددی):\n{ids_text}")
        return

    if text.startswith("/addadmin"):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await event.reply("فرمت درست: /addadmin <user_id>", parse_mode="markdown")
            return
        new_id = int(parts[1])
        ADMINS.add(new_id)
        save_admins()
        await event.reply(f"✅ ادمین جدید اضافه شد: {new_id}", parse_mode="markdown")
        return

if text.startswith("/deladmin"):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await event.reply("فرمت درست: /deladmin <user_id>", parse_mode="markdown")
            return
        rem_id = int(parts[1])
        if rem_id in ADMINS:
            ADMINS.remove(rem_id)
            save_admins()
            await event.reply(f"✅ ادمین حذف شد: {rem_id}", parse_mode="markdown")
        else:
            await event.reply("این آی‌دی جزو ادمین‌ها نیست.")
        return

    # ---------- مدیریت delay ----------

    if text.startswith("/setdelay"):
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            global INVITE_DELAY
            INVITE_DELAY = int(parts[1])
            if INVITE_DELAY < 1:
                INVITE_DELAY = 1
            save_settings()
            await event.reply(f"✅ تاخیر بین ادها روی {INVITE_DELAY} ثانیه تنظیم شد.")
        else:
            await event.reply("فرمت درست: /setdelay <seconds>", parse_mode="markdown")
        return

    if text == "⏱ تنظیم تاخیر":
        user_states[user_id] = {"mode": "setdelay", "step": "value", "temp": {}}
        await event.reply("عدد تاخیر بین ادها (ثانیه) را بفرست:")
        return

    # ---------- مدیریت اکانت‌ها برای add user ----------

    if text == "/accounts" or text == "📜 اکانت‌ها":
        await event.reply(list_accounts_text())
        return

    if text.startswith("/useacc"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await event.reply("فرمت درست: /useacc <name>", parse_mode="markdown")
            return
        name = parts[1].strip()
        if not get_account_by_name(name):
            await event.reply("اکانت با این نام وجود ندارد.")
            return
        set_active_account(name)
        await event.reply(f"✅ اکانت فعال برای add user تنظیم شد: {name}")
        return

    if text.startswith("/delacc"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await event.reply("فرمت درست: /delacc <name>", parse_mode="markdown")
            return
        name = parts[1].strip()
        acc = get_account_by_name(name)
        if not acc:
            await event.reply("اکانت با این نام وجود ندارد.")
            return
        ACCOUNTS.remove(acc)
        global ACTIVE_ACCOUNT
        if ACTIVE_ACCOUNT == name:
            ACTIVE_ACCOUNT = None
        save_accounts()
        await event.reply(f"✅ اکانت حذف شد: {name}")
        return

    if text == "➕ افزودن اکانت":
        user_states[user_id] = {"mode": "addacc", "step": "name", "temp": {}}
        await event.reply("اسم دلخواه برای این اکانت را بفرست (مثلاً main یا acc1):")
        return

    # ---------- مدیریت گروه‌ها برای add user ----------

    if text == "🧾 گروه‌ها" or text == "/groups":
        if not ACTIVE_ACCOUNT:
            await event.reply("هیچ اکانتی برای add user فعال نیست. از منو اکانت اضافه کن یا /useacc بزن.")
            return
        await event.reply("در حال گرفتن لیست سوپرگروه‌ها با اکانت فعال (برای add user)...")
        try:
            await fetch_groups_for_active()
            msg = groups_text()
            awaiting_group_number = True
            await event.reply(msg)
        except Exception as e:
            awaiting_group_number = False
            await event.reply(f"خطا در گرفتن لیست گروه‌ها:\n{e}")
            traceback.print_exc()
        return

    if awaiting_group_number and text.isdigit():
        idx = int(text)
        if idx < 0 or idx >= len(groups_cache):
            await event.reply("شماره گروه نامعتبر است. دوباره دکمه 🧾 گروه‌ها را بزن.")
            return
        target_group = groups_cache[idx]
        awaiting_group_number = False
        await event.reply(f"✅ گروه برای add user انتخاب شد:\n{target_group.title}\n(ID: {target_group.id})")
        return

    # ---------- خروج اعضا با ویزارد جدید ----------

if text == "/export" or text == "📤 خروج اعضا":
        user_states[user_id] = {"mode": "export", "step": "phone", "temp": {}}
        await event.reply(
            "شماره اکانتی که می‌خوای باهاش لیست اعضای یک گروه رو بگیری بفرست "
            "(مثلاً +98912...):"
        )
        return

    # ---------- فایل CSV برای add user ----------

    if event.document:
        file_name = (event.file.name or "").lower()
        if ".csv" in file_name:
            await event.reply("فایل CSV دریافت شد، در حال دانلود...")
            try:
                file_path = await client.download_media(event.document)
                await event.reply("فایل دانلود شد، شروع اد کردن اعضا...")
                await add_users_from_csv_file(file_path, chat_id)
            except Exception as e:
                await event.reply(f"خطا در دانلود/پردازش فایل:\n{e}")
                traceback.print_exc()
        return

    # ---------- سایر موارد ----------

    if text:
        await event.reply("دستور نامعتبر.\nاز /start یا منوی دکمه‌ای استفاده کن.")
        return


# ------------------ main ------------------

def main():
    print("Loading admins, settings, accounts...")
    load_admins()
    load_settings()
    load_accounts()

    print("Bot starting...")
    client.start(bot_token=BOT_TOKEN)
    print("Bot is running. Waiting for commands...")
    client.run_until_disconnected()


if name == "main":
    main()
