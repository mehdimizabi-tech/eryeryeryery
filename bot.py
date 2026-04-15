import os
import csv
import io
import re
import asyncio
import traceback
import random

import psycopg
from psycopg.rows import dict_row

from telethon import TelegramClient, events, Button
from telethon.tl.functions.messages import GetDialogsRequest, ImportChatInviteRequest
from telethon.tl.types import InputPeerEmpty, InputPeerUser, InputPeerChannel
from telethon.tl.functions.channels import InviteToChannelRequest, JoinChannelRequest
from telethon.errors.rpcerrorlist import (
    FloodWaitError,
    PeerFloodError,
    UserPrivacyRestrictedError,
    UserAlreadyParticipantError,
    ChannelInvalidError,
)
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError
from telethon.sessions import StringSession


# ------------ تنظیمات اصلی ------------
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

OWNER_ID = 6474515118
DATABASE_URL = os.environ.get("DATABASE_URL")

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("API_ID / API_HASH / BOT_TOKEN must be set.")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL must be set.")

BOT_SESSION = "bot_session"
client = TelegramClient(BOT_SESSION, API_ID, API_HASH)

# ------------ متغیرهای سراسری در حافظه ------------
ADMINS = set()
INVITE_DELAY = 60               # پیش‌فرض: ۶۰ ثانیه
INVITE_DELAY_MODE = "fixed"     # "fixed" یا "random"

ACCOUNTS_ADD = []               # لیست اکانت‌های add از دیتابیس
ACTIVE_ADD_ACCOUNT = None       # فقط جهت نمایش

user_states = {}                # state ماشین برای هر یوزر
login_clients_add = {}          # سشن‌های موقت برای لاگین اکانت‌های add
login_clients_export = {}       # سشن‌های موقت برای لاگین اکانت‌های export

groups_cache = []               # کش گروه‌ها بعد از 🧾 شروع add
target_group = None             # آبجکت گروه انتخاب شده (از اکانت export)
target_group_id = None          # chat_id گروه
target_group_username = None    # @username اگر عمومی باشه
target_group_title = None       # اسم گروه برای لاگ‌ها
awaiting_group_number = False   # اگر True یعنی منتظر شماره گروه هستیم

current_add_jobs = {}           # برای کنترل استاپ add به ازای هر چت


# ------------ دیتابیس ------------
def get_db_connection():
    return psycopg.connect(DATABASE_URL)


def init_db():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS admins (user_id BIGINT PRIMARY KEY)")
            cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    phone TEXT,
                    api_id BIGINT NOT NULL,
                    api_hash TEXT NOT NULL,
                    session_string TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('add', 'export')),
                    UNIQUE(name, kind)
                )
                """
            )
        conn.commit()

    load_admins_from_db()
    load_settings_from_db()
    load_accounts_add_from_db()


def load_admins_from_db():
    global ADMINS
    with get_db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT user_id FROM admins")
            rows = cur.fetchall()
            ADMINS = {row["user_id"] for row in rows}
            # حتما صاحب ربات را اضافه کن
            if OWNER_ID not in ADMINS:
                with conn.cursor() as cur2:
                    cur2.execute(
                        "INSERT INTO admins (user_id) VALUES (%s) "
                        "ON CONFLICT (user_id) DO NOTHING",
                        (OWNER_ID,),
                    )
                    conn.commit()
                ADMINS.add(OWNER_ID)


def add_admin_db(user_id: int):
    global ADMINS
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO admins (user_id) VALUES (%s) "
                "ON CONFLICT (user_id) DO NOTHING",
                (user_id,),
            )
            conn.commit()
    ADMINS.add(user_id)


def remove_admin_db(user_id: int):
    global ADMINS
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admins WHERE user_id = %s", (user_id,))
            conn.commit()
    ADMINS.discard(user_id)


def load_settings_from_db():
    global INVITE_DELAY, ACTIVE_ADD_ACCOUNT, INVITE_DELAY_MODE
    with get_db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            # delay
            cur.execute("SELECT value FROM settings WHERE key = 'invite_delay'")
            row = cur.fetchone()
            if row:
                try:
                    INVITE_DELAY = int(row["value"])
                except ValueError:
                    INVITE_DELAY = 60
            else:
                INVITE_DELAY = 60

            # delay mode
            cur.execute("SELECT value FROM settings WHERE key = 'invite_delay_mode'")
            row = cur.fetchone()
            if row and row["value"] in ("fixed", "random"):
                INVITE_DELAY_MODE = row["value"]
            else:
                INVITE_DELAY_MODE = "fixed"

            # active add account (صرفا برای نمایش)
            cur.execute("SELECT value FROM settings WHERE key = 'active_add_account'")
            row = cur.fetchone()
            if row:
                ACTIVE_ADD_ACCOUNT = row["value"]
            else:
                ACTIVE_ADD_ACCOUNT = None


def set_setting(key: str, value: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO settings(key, value)
                VALUES (%s, %s)
                ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value
                """,
                (key, value),
            )
            conn.commit()


def load_accounts_add_from_db():
    global ACCOUNTS_ADD
    with get_db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM accounts WHERE kind = 'add'")
            rows = cur.fetchall()
            ACCOUNTS_ADD = []
            for r in rows:
                ACCOUNTS_ADD.append(
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "phone": r["phone"],
                        "api_id": r["api_id"],
                        "api_hash": r["api_hash"],
                        "session_string": r["session_string"],
                    }
                )


def insert_account(name, phone, api_id, api_hash, session_string, kind):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO accounts(name, phone, api_id, api_hash, session_string, kind)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (name, phone, api_id, api_hash, session_string, kind),
            )
            acc_id = cur.fetchone()[0]
            conn.commit()
    return acc_id


def delete_account_by_id(acc_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM accounts WHERE id = %s", (acc_id,))
            conn.commit()


def get_export_accounts():
    with get_db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id, name, phone FROM accounts WHERE kind = 'export'")
            rows = cur.fetchall()
    return [
        {"id": r["id"], "name": r["name"], "phone": r["phone"] or ""}
        for r in rows
    ]


def get_account_row_by_id(acc_id: int):
    with get_db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM accounts WHERE id = %s", (acc_id,))
            return cur.fetchone()


def export_account_name_exists(name: str) -> bool:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM accounts WHERE kind = 'export' AND name = %s",
                (name,),
            )
            return cur.fetchone() is not None


# ------------ ابزارهای کمکی ------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def get_add_account_by_name(name: str):
    for acc in ACCOUNTS_ADD:
        if acc["name"] == name:
            return acc
    return None


def main_menu():
    return [
        [Button.text("➕ افزودن اکانت"), Button.text("📜 اکانت‌ها")],
        [Button.text("🧾 شروع add"), Button.text("🧠 Add پیشرفته"), Button.text("📤 خروج اعضا")],
        [Button.text("⏱ تنظیم تاخیر"), Button.text("🗑 حذف اکانت add")],
        [Button.text("🚪 خروج اکانت‌های export"), Button.text("👥 جوین اکانت‌ها")],
        [Button.text("⛔ توقف add")],
    ]


async def send_main_menu(chat_id, text="از منوی زیر استفاده کن:"):
    await client.send_message(chat_id, text, buttons=main_menu())


def sanitize_filename(title: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9]+", "-", (title or "chat").lower())
    return f"members-{safe}.csv"


def parse_group_link(link: str):
    link = link.strip()
    if link.startswith("https://"):
        link = link[len("https://"):]
    elif link.startswith("http://"):
        link = link[len("http://"):]

    if "joinchat/" in link:
        part = link.split("joinchat/", 1)[1]
        invite_hash = part.split("?", 1)[0]
        return "invite", invite_hash

    if "t.me/+" in link:
        part = link.split("t.me/+", 1)[1]
        invite_hash = part.split("?", 1)[0]
        return "invite", invite_hash

    if link.startswith("t.me/"):
        after = link[len("t.me/"):]
    else:
        after = link

    if after.startswith("+"):
        invite_hash = after[1:].split("?", 1)[0]
        return "invite", invite_hash

    if after.startswith("@"):
        username = after[1:]
    else:
        username = after

    if "/" in username:
        username = username.split("/", 1)[0]

    return "username", username


# ------------ جوین همه اکانت‌های add به یک گروه ------------
async def join_all_add_accounts(group_link: str, chat_id: int):
    if not ACCOUNTS_ADD:
        await client.send_message(chat_id, "هیچ اکانتی برای add user ثبت نشده.")
        return

    mode, value = parse_group_link(group_link)
    await client.send_message(
        chat_id,
        f"در حال جوین کردن همه اکانت‌های add به گروه با لینک:\n{group_link}",
    )

    for acc in ACCOUNTS_ADD:
        name = acc["name"]
        api_id = acc["api_id"]
        api_hash = acc["api_hash"]
        session_string = acc["session_string"]
        session = StringSession(session_string)
        user_client = TelegramClient(session, api_id, api_hash)

        try:
            await user_client.connect()
            if not await user_client.is_user_authorized():
                await client.send_message(
                    chat_id, f"⚠️ [{name}] لاگین نیست، از این اکانت استفاده نشد."
                )
                continue

            try:
                if mode == "invite":
                    invite_hash = value
                    try:
                        await user_client(ImportChatInviteRequest(invite_hash))
                        await client.send_message(
                            chat_id,
                            f"✅ [{name}] با لینک خصوصی به گروه join شد.",
                        )
                    except UserAlreadyParticipantError:
                        await client.send_message(
                            chat_id,
                            f"ℹ️ [{name}] قبلاً عضو این گروه بوده.",
                        )
                else:
                    username = value
                    try:
                        entity = await user_client.get_entity(username)
                    except Exception as ee:
                        await client.send_message(
                            chat_id,
                            f"⚠️ [{name}] نتوانست گروه را از روی لینک/یوزرنیم پیدا کند:\n{ee}",
                        )
                        continue

                    try:
                        await user_client(JoinChannelRequest(entity))
                        await client.send_message(
                            chat_id,
                            f"✅ [{name}] به گروه عمومی join شد.",
                        )
                    except UserAlreadyParticipantError:
                        await client.send_message(
                            chat_id,
                            f"ℹ️ [{name}] قبلاً عضو این گروه بوده.",
                        )
            except Exception as e:
                await client.send_message(
                    chat_id,
                    f"❌ خطا در جوین برای اکانت [{name}]:\n{e}",
                )
                traceback.print_exc()

        except Exception as e:
            await client.send_message(
                chat_id,
                f"❌ خطا در اتصال سشن [{name}]:\n{e}",
            )
            traceback.print_exc()
        finally:
            try:
                await user_client.disconnect()
            except Exception:
                pass

    await client.send_message(
        chat_id,
        "✅ فرآیند جوین برای همه اکانت‌های add تمام شد.",
    )


# ------------ ADD از روی CSV با تقسیم بین همه اکانت‌های add ------------
async def add_users_from_csv_file(file_path, chat_id: int, advanced=False):
    global target_group, target_group_id, target_group_username, target_group_title
    global current_add_jobs

    if not ACCOUNTS_ADD:
        await client.send_message(
            chat_id,
            "هیچ اکانتی برای add user ثبت نشده. اول از «➕ افزودن اکانت» استفاده کن.",
        )
        return

    if target_group is None or target_group_id is None:
        await client.send_message(
            chat_id,
            "هیچ گروهی برای add user انتخاب نشده. از دکمه 🧾 شروع add استفاده کن.",
        )
        return

    if chat_id in current_add_jobs:
        await client.send_message(
            chat_id,
            "الان یک فرآیند add برای این چت در حال اجراست. اول با «⛔ توقف add» متوقفش کن.",
        )
        return

    users = []
    try:
        with open(file_path, encoding="utf-8") as f:
            reader = csv.reader(f, delimiter=",", lineterminator="\n")
            next(reader, None)
            for row in reader:
                if len(row) < 3:
                    continue
                users.append(
                    {
                        "username": row[0],
                        "id": int(row[1]) if row[1] else 0,
                        "access_hash": int(row[2]) if row[2] else 0,
                    }
                )
    except Exception as e:
        await client.send_message(chat_id, f"⚠️ خطا در خواندن CSV:\n{e}")
        traceback.print_exc()
        return

    if not users:
        await client.send_message(chat_id, "هیچ کاربری در CSV پیدا نشد.")
        return

    total_users = len(users)
    total_accounts = len(ACCOUNTS_ADD)

    job = {"cancel": False}
    current_add_jobs[chat_id] = job

    per_account_users = [[] for _ in range(total_accounts)]
    for idx, user in enumerate(users):
        acc_index = idx % total_accounts
        per_account_users[acc_index].append(user)

    await client.send_message(
        chat_id,
        f"در حال تقسیم {total_users} کاربر بین {total_accounts} اکانت add و شروع اد همزمان...\n"
        "برای توقف وسط کار می‌تونی از دکمه «⛔ توقف add» استفاده کنی.",
    )

    async def add_worker(acc, users_for_this_acc, job):
        if not users_for_this_acc:
            return

        name = acc["name"]
        api_id = acc["api_id"]
        api_hash = acc["api_hash"]
        session_string = acc["session_string"]

        session = StringSession(session_string)
        user_client = TelegramClient(session, api_id, api_hash)

        try:
            await user_client.connect()
            if not await user_client.is_user_authorized():
                await client.send_message(
                    chat_id, f"⚠️ اکانت {name} لاگین نیست، از این اکانت استفاده نشد."
                )
                return

            # ---- کانال را برای همین سشن resolve می‌کنیم ----
            target_entity = None
            try:
                if target_group_username:
                    # اگر گروه public است
                    target_entity = await user_client.get_entity(target_group_username)
                else:
                    # اگر private است و فقط id داریم
                    target_entity = await user_client.get_entity(target_group_id)
            except Exception:
                # fallback: تلاش با InputPeerChannel
                try:
                    channel_input = InputPeerChannel(target_group_id, target_group.access_hash)
                    target_entity = await user_client.get_entity(channel_input)
                except Exception as e2:
                    await client.send_message(
                        chat_id,
                        f"⚠️ [{name}] نتوانست کانال هدف را resolve کند:\n"
                        f"{e2}\n"
                        "پیشنهاد: با دکمه «👥 جوین اکانت‌ها» مطمئن شو این اکانت عضو گروه شده "
                        "و سپس دوباره فرآیند add را شروع کن.",
                    )
                    return

            await client.send_message(
                chat_id,
                f"ℹ️ [{name}] کانال هدف برای این اکانت resolve شد: "
                f"{getattr(target_entity, 'title', target_group_title)} (id={getattr(target_entity, 'id', 'unknown')})"
            )

            total_for_acc = len(users_for_this_acc)
            await client.send_message(
                chat_id,
                f"▶️ اکانت {name} شروع کرد. تعداد سهم این اکانت: {total_for_acc} کاربر.",
            )

            
            index = 0
            while index < len(users_for_this_acc):
                user = users_for_this_acc[index]
                idx = index + 1

                if job.get("cancel"):
                    await client.send_message(
                        chat_id, f"⏹ اکانت {name} به درخواست شما متوقف شد."
                    )
                    break

                username_or_id = user["username"] or f"id:{user['id']}"

                try:
                    await client.send_message(
                        chat_id,
                        f"[{name} {idx}/{total_for_acc}] در حال اضافه کردن: {username_or_id}",
                    )

                    if user["username"]:
                        user_entity = await user_client.get_input_entity(user["username"])
                    else:
                        user_entity = InputPeerUser(user["id"], user["access_hash"])

                    await user_client(
                        InviteToChannelRequest(target_entity, [user_entity])
                    )
                    await client.send_message(
                        chat_id, f"✅ [{name}] اضافه شد: {username_or_id}"
                    )

                except FloodWaitError as e:
                    wait_time = e.seconds
                    extra = random.randint(1800, 7200) if advanced else 0
                    total_wait = wait_time + extra
                    await client.send_message(chat_id, f"⏳ [{name}] FloodWait: {wait_time}s → با تاخیر اضافه: {extra}s")
                    await asyncio.sleep(total_wait)
                    continue
                except PeerFloodError:
                    await client.send_message(
                        chat_id,
                        f"⛔ [{name}] خطای Flood از سمت تلگرام. این اکانت متوقف شد.",
                    )
                    break
                except UserPrivacyRestrictedError:
                    await client.send_message(
                        chat_id,
                        f"⚠️ [{name}] محدودیت حریم خصوصی، رد شد: {username_or_id}",
                    )
                except ChannelInvalidError as e:
                    await client.send_message(
                        chat_id,
                        f"⚠️ [{name}] خطای کانال نامعتبر (ChannelInvalidError) برای {username_or_id}:\n"
                        f"{e}\n"
                        "این یعنی این اکانت از دید تلگرام دسترسی معتبر به کانال هدف ندارد.\n"
                        "پیشنهاد: از «👥 جوین اکانت‌ها» استفاده کن و مطمئن شو این اکانت عضو همون گروهه.",
                    )
                    break
                except Exception as e:
                    await client.send_message(
                        chat_id,
                        f"⚠️ [{name}] خطا برای {username_or_id}:\n{e}",
                    )
                    traceback.print_exc()

                if job.get("cancel"):
                    await client.send_message(
                        chat_id, f"⏹ اکانت {name} به درخواست شما متوقف شد."
                    )
                    break

                # تاخیر
                if INVITE_DELAY_MODE == "random":
                    delay = random.randint(30, 100)
                else:
                    delay = INVITE_DELAY
                    if delay < 1:
                        delay = 1

                await asyncio.sleep(delay)
                index += 1

            else:
                await client.send_message(chat_id, f"⏹ اکانت {name} کارش تمام شد.")

        except Exception as e:
            await client.send_message(
                chat_id, f"❌ خطای کلی برای اکانت {name}:\n{e}"
            )
            traceback.print_exc()
        finally:
            try:
                await user_client.disconnect()
            except Exception:
                pass

    tasks = []
    for acc, acc_users in zip(ACCOUNTS_ADD, per_account_users):
        if acc_users:
            tasks.append(asyncio.create_task(add_worker(acc, acc_users, job)))

    if not tasks:
        current_add_jobs.pop(chat_id, None)
        await client.send_message(
            chat_id, "هیچ کاربری بین اکانت‌ها توزیع نشد (لیست خالی بود)."
        )
        return

    await asyncio.gather(*tasks)

    if job.get("cancel"):
        await client.send_message(chat_id, "⛔ فرآیند add به درخواست شما متوقف شد.")
    else:
        await client.send_message(
            chat_id, "✅ فرآیند add با همه اکانت‌ها تمام شد."
        )

    current_add_jobs.pop(chat_id, None)


# ------------ state machine برای پیام‌های متنی ------------
async def handle_state_message(event, state):
    global INVITE_DELAY, ACTIVE_ADD_ACCOUNT, ACCOUNTS_ADD, INVITE_DELAY_MODE
    global groups_cache, awaiting_group_number, target_group
    global target_group_id, target_group_username, target_group_title

    user_id = event.sender_id
    chat_id = event.chat_id
    text = (event.raw_text or "").strip()
    mode = state.get("mode")
    step = state.get("step")
    temp = state.get("temp", {})

    # ====== انتخاب اکانت export برای شروع add (لیست گروه‌ها) ======
    if mode == "add_choose_export" and step == "choose":
        accounts = temp.get("accounts", [])
        if not text.isdigit():
            await event.reply("فقط شماره اکانت export را بفرست (مثلاً 0 یا 1).")
            return
        idx = int(text)
        if idx < 0 or idx >= len(accounts):
            await event.reply("شماره نامعتبر است. دوباره سعی کن.")
            return

        acc_meta = accounts[idx]
        acc_id = acc_meta["id"]
        row = get_account_row_by_id(acc_id)
        if not row:
            await event.reply("این اکانت export در دیتابیس پیدا نشد.")
            user_states.pop(user_id, None)
            return

        name = row["name"]
        api_id = row["api_id"]
        api_hash = row["api_hash"]
        session_string = row["session_string"]

        session = StringSession(session_string)
        export_client = TelegramClient(session, api_id, api_hash)

        try:
            await export_client.connect()
            if not await export_client.is_user_authorized():
                await event.reply(
                    "این اکانت export دیگر لاگین نیست. دوباره از «📤 خروج اعضا» آن را بساز."
                )
                await export_client.disconnect()
                user_states.pop(user_id, None)
                return

            result = await export_client(
                GetDialogsRequest(
                    offset_date=None,
                    offset_id=0,
                    offset_peer=InputPeerEmpty(),
                    limit=200,
                    hash=0,
                )
            )
            groups_cache = [
                c for c in result.chats if getattr(c, "megagroup", False)
            ]

            await export_client.disconnect()

            if not groups_cache:
                await event.reply("هیچ سوپرگروهی در این اکانت export پیدا نشد.")
                user_states.pop(user_id, None)
                return

            lines = [f"لیست سوپرگروه‌ها با اکانت export `{name}`:\n"]
            for i, g in enumerate(groups_cache):
                lines.append(f"{i}: {g.title}")
            lines.append(
                "\nیک عدد بفرست تا همان گروه برای add user انتخاب شود.\n"
                "بعد از انتخاب گروه، فایل CSV را بفرست تا add انجام شود."
            )

            awaiting_group_number = True
            user_states.pop(user_id, None)
            await event.reply("\n".join(lines), parse_mode="markdown")

        except Exception as e:
            await event.reply(f"خطا در گرفتن لیست گروه‌ها از اکانت export:\n{e}")
            traceback.print_exc()
            try:
                await export_client.disconnect()
            except Exception:
                pass
            user_states.pop(user_id, None)
        return

    # ====== لینک برای جوین همه اکانت‌ها ======
    if mode == "join_all_add" and step == "link":
        group_link = text
        user_states.pop(user_id, None)
        await join_all_add_accounts(group_link, chat_id)
        await send_main_menu(chat_id, "کار جوین اکانت‌ها تمام شد. از منو ادامه بده:")
        return

    # ====== تایید شروع add از روی CSV یا انصراف ======
    if mode == "confirm_add_csv" and step == "confirm":
        file_path = temp.get("file_path")
        lower = text.strip().lower()
        if lower in ["✅ شروع add".lower(), "شروع add", "شروع", "yes", "y"]:
            user_states.pop(user_id, None)
            await event.reply("✅ شروع فرآیند add از روی این CSV...")
            await add_users_from_csv_file(file_path, chat_id, advanced=state.get("temp", {}).get("advanced", False) if "state" in locals() else False)
            return
        if lower in ["❌ انصراف".lower(), "انصراف", "cancel", "لغو"]:
            user_states.pop(user_id, None)
            try:
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                pass
            await event.reply("✅ فرآیند add برای این فایل CSV لغو شد.")
            await send_main_menu(chat_id)
            return
        await event.reply("برای ادامه، «✅ شروع add» یا «❌ انصراف» را بفرست.")
        return

    # ====== ویزارد افزودن اکانت add ======
    if mode == "addacc":
        if step == "name":
            name = text
            if get_add_account_by_name(name):
                await event.reply(
                    "این نام قبلاً برای اکانت add استفاده شده، یک نام دیگر بفرست."
                )
                return
            temp["name"] = name
            state["step"] = "api_id"
            state["temp"] = temp
            user_states[user_id] = state
            await event.reply("API_ID را بفرست (عدد):")
            return

        if step == "api_id":
            if not text.isdigit():
                await event.reply("API_ID باید عدد باشد. دوباره بفرست:")
                return
            temp["api_id"] = int(text)
            state["step"] = "api_hash"
            state["temp"] = temp
            user_states[user_id] = state
            await event.reply("API_HASH را بفرست:")
            return

        if step == "api_hash":
            temp["api_hash"] = text
            state["step"] = "phone"
            state["temp"] = temp
            user_states[user_id] = state
            await event.reply("شماره تلفن اکانت را با فرمت +98912... بفرست:")
            return


        if step == "auth_method":
            choice = text.strip()
            if choice == "1":
                state["step"] = "phone"
                state["temp"] = temp
                user_states[user_id] = state
                await event.reply("شماره تلفن اکانت add را با فرمت +98912... بفرست:")
                return
            if choice == "2":
                state["step"] = "session"
                state["temp"] = temp
                user_states[user_id] = state
                await event.reply("رشته سشن (StringSession) را بفرست:")
                return
            await event.reply("فقط 1 یا 2 را بفرست.")
            return

        if step == "session":
            session_text = text
            name = temp["name"]
            api_id = temp["api_id"]
            api_hash = temp["api_hash"]
            try:
                acc_client = TelegramClient(StringSession(session_text), api_id, api_hash)
                await acc_client.connect()
                if not await acc_client.is_user_authorized():
                    await event.reply("این سشن معتبر یا لاگین‌شده نیست.")
                    await acc_client.disconnect()
                    user_states.pop(user_id,None)
                    return
                me = await acc_client.get_me()
                phone = getattr(me,"phone",None) or ""
                session_string = acc_client.session.save()
                await acc_client.disconnect()

                acc_id = insert_account(name, phone, api_id, api_hash, session_string, "add")
                ACCOUNTS_ADD.append({
                    "id": acc_id,
                    "name": name,
                    "phone": phone,
                    "api_id": api_id,
                    "api_hash": api_hash,
                    "session_string": session_string,
                })
                user_states.pop(user_id,None)
                await event.reply(f"✅ اکانت {name} با سشن اضافه شد.")
                await send_main_menu(chat_id)
            except Exception as e:
                await event.reply(f"خطا در سشن:\n{e}")
                user_states.pop(user_id,None)
            return

        if step == "phone":
            phone = text
            temp["phone"] = phone

            name = temp["name"]
            api_id = temp["api_id"]
            api_hash = temp["api_hash"]

            acc_client = TelegramClient(StringSession(), api_id, api_hash)
            await acc_client.connect()

            try:
                sent = await acc_client.send_code_request(phone)
                temp["phone_code_hash"] = sent.phone_code_hash
                login_clients_add[user_id] = acc_client

                state["step"] = "code"
                state["temp"] = temp
                user_states[user_id] = state

                await event.reply(
                    f"کد به شماره {phone} ارسال شد.\n"
                    "کد را همینجا بفرست (فقط عدد):"
                )
            except Exception as e:
                await event.reply(f"خطا در ارسال کد:\n{e}")
                traceback.print_exc()
                await acc_client.disconnect()
                login_clients_add.pop(user_id, None)
                user_states.pop(user_id, None)
            return

        if step == "code":
            code = text
            phone = temp["phone"]
            api_id = temp["api_id"]
            api_hash = temp["api_hash"]
            name = temp["name"]
            phone_code_hash = temp.get("phone_code_hash")

            acc_client = login_clients_add.get(user_id)
            if not acc_client:
                await event.reply(
                    "سشن لاگین پیدا نشد. دوباره ➕ افزودن اکانت را بزن."
                )
                user_states.pop(user_id, None)
                return

            try:
                await acc_client.sign_in(
                    phone=phone, code=code, phone_code_hash=phone_code_hash
                )
                session_string = acc_client.session.save()
                await acc_client.disconnect()
                login_clients_add.pop(user_id, None)

                acc_id = insert_account(
                    name=name,
                    phone=phone,
                    api_id=api_id,
                    api_hash=api_hash,
                    session_string=session_string,
                    kind="add",
                )

                ACCOUNTS_ADD.append(
                    {
                        "id": acc_id,
                        "name": name,
                        "phone": phone,
                        "api_id": api_id,
                        "api_hash": api_hash,
                        "session_string": session_string,
                    }
                )

                if not ACTIVE_ADD_ACCOUNT:
                    ACTIVE_ADD_ACCOUNT = name
                    set_setting("active_add_account", name)

                user_states.pop(user_id, None)
                await event.reply(
                    f"✅ اکانت `{name}` برای add user ثبت و لاگین شد.",
                    parse_mode="markdown",
                )
                await send_main_menu(chat_id)

            except SessionPasswordNeededError:
                state["step"] = "2fa"
                state["temp"] = temp
                user_states[user_id] = state
                await event.reply(
                    "برای این اکانت رمز دو مرحله‌ای (2FA) فعال است.\n"
                    "رمز دو مرحله‌ای این اکانت را همینجا بفرست:"
                )
            except PhoneCodeExpiredError:
                await event.reply(
                    "کد منقضی شده. دوباره دکمه «➕ افزودن اکانت» را بزن و از اول شروع کن."
                )
                await acc_client.disconnect()
                login_clients_add.pop(user_id, None)
                user_states.pop(user_id, None)
            except Exception as e:
                await event.reply(f"خطا در لاگین:\n{e}")
                traceback.print_exc()
                await acc_client.disconnect()
                login_clients_add.pop(user_id, None)
                user_states.pop(user_id, None)
            return

        if step == "2fa":
            password = text
            phone = temp["phone"]
            api_id = temp["api_id"]
            api_hash = temp["api_hash"]
            name = temp["name"]

            acc_client = login_clients_add.get(user_id)
            if not acc_client:
                await event.reply(
                    "سشن لاگین پیدا نشد. دوباره ➕ افزودن اکانت را بزن."
                )
                user_states.pop(user_id, None)
                return

            try:
                await acc_client.sign_in(password=password)
                session_string = acc_client.session.save()
                await acc_client.disconnect()
                login_clients_add.pop(user_id, None)

                acc_id = insert_account(
                    name=name,
                    phone=phone,
                    api_id=api_id,
                    api_hash=api_hash,
                    session_string=session_string,
                    kind="add",
                )

                ACCOUNTS_ADD.append(
                    {
                        "id": acc_id,
                        "name": name,
                        "phone": phone,
                        "api_id": api_id,
                        "api_hash": api_hash,
                        "session_string": session_string,
                    }
                )

                if not ACTIVE_ADD_ACCOUNT:
                    ACTIVE_ADD_ACCOUNT = name
                    set_setting("active_add_account", name)

                user_states.pop(user_id, None)
                await event.reply(
                    f"✅ اکانت `{name}` (با 2FA) برای add user ثبت و لاگین شد.",
                    parse_mode="markdown",
                )
                await send_main_menu(chat_id)

            except Exception as e:
                await event.reply(
                    f"خطا در لاگین با رمز دو مرحله‌ای:\n{e}"
                )
                traceback.print_exc()
                await acc_client.disconnect()
                login_clients_add.pop(user_id, None)
                user_states.pop(user_id, None)
            return

    # ====== تنظیم تاخیر ======
    if mode == "setdelay":
        if step == "mode":
            lower = text.strip().lower()
            if lower in ("1", "ثابت", "fixed"):
                state["step"] = "value"
                state["temp"] = {}
                user_states[user_id] = state
                await event.reply("عدد تاخیر بین ادها (ثانیه) را بفرست:")
                return
            if lower in ("2", "رندوم", "random"):
                INVITE_DELAY_MODE = "random"
                set_setting("invite_delay_mode", "random")
                user_states.pop(user_id, None)
                await event.reply(
                    "✅ حالت تاخیر روی «رندوم بین 30 تا 100 ثانیه» تنظیم شد."
                )
                await send_main_menu(chat_id)
                return
            await event.reply("فقط عدد 1 (ثابت) یا 2 (رندوم) را بفرست.")
            return

        if step == "value":
            if not text.isdigit():
                await event.reply("تاخیر باید عدد (ثانیه) باشد. دوباره بفرست:")
                return
            INVITE_DELAY = int(text)
            if INVITE_DELAY < 1:
                INVITE_DELAY = 1
            INVITE_DELAY_MODE = "fixed"
            set_setting("invite_delay", str(INVITE_DELAY))
            set_setting("invite_delay_mode", "fixed")
            user_states.pop(user_id, None)
            await event.reply(
                f"✅ تاخیر بین ادها به صورت ثابت روی {INVITE_DELAY} ثانیه تنظیم شد."
            )
            await send_main_menu(chat_id)
            return

    # ====== ویزارد حذف اکانت add ======
    if mode == "delacc_wizard" and step == "choose":
        if not text.isdigit():
            await event.reply("فقط شماره اکانت را بفرست (مثلاً 0 یا 1).")
            return
        idx = int(text)
        names = temp.get("names", [])
        if idx < 0 or idx >= len(names):
            await event.reply("شماره نامعتبر است. دوباره سعی کن.")
            return
        acc_info = names[idx]
        acc_id = acc_info["id"]
        name = acc_info["name"]

        delete_account_by_id(acc_id)
        ACCOUNTS_ADD[:] = [a for a in ACCOUNTS_ADD if a["id"] != acc_id]

        if ACTIVE_ADD_ACCOUNT == name:
            ACTIVE_ADD_ACCOUNT = None
            set_setting("active_add_account", "")

        user_states.pop(user_id, None)
        await event.reply(f"✅ اکانت {name} حذف شد.")
        await send_main_menu(chat_id)
        return

    # ====== انتخاب اکانت export برای خروج اعضا ======
    if mode == "export_select" and step == "choose":
        accounts = temp.get("accounts", [])
        lower = text.lower()

        if lower == "new":
            user_states[user_id] = {
                "mode": "export_login",
                "step": "name",
                "temp": {},
            }
            await event.reply(
                "اسم دلخواه برای این اکانت export را بفرست (مثلاً exp1):"
            )
            return

        if not text.isdigit():
            await event.reply(
                'یک عدد برای انتخاب اکانت یا عبارت "new" برای ساخت اکانت جدید بفرست.'
            )
            return

        idx = int(text)
        if idx < 0 or idx >= len(accounts):
            await event.reply("شماره نامعتبر است. دوباره سعی کن.")
            return

        acc_id = accounts[idx]["id"]
        temp2 = {"account_id": acc_id}
        user_states[user_id] = {
            "mode": "export_mode",
            "step": "choose",
            "temp": temp2,
        }
        await event.reply(
            "کدام نوع خروج اعضا را می‌خواهی؟\n"
            "1️⃣ خروج کامل همه اعضای گروه\n"
            "2️⃣ فقط کسانی که در گروه پیام ارسال کرده‌اند\n\n"
            "فقط 1 یا 2 را بفرست."
        )
        return

    # ====== انتخاب نوع خروج اعضا (کامل / فعال‌ها) ======
    if mode == "export_mode" and step == "choose":
        choice = text.strip()
        acc_id = temp.get("account_id")
        if choice in ("1", "۱", "all", "members"):
            temp2 = {"account_id": acc_id}
            user_states[user_id] = {
                "mode": "export_chat",
                "step": "chat_id",
                "temp": temp2,
            }
            await event.reply(
                "حالا chat_id گروه را بفرست (مثلاً -1001234567890):"
            )
            return
        elif choice in ("2", "۲", "active"):
            temp2 = {"account_id": acc_id}
            user_states[user_id] = {
                "mode": "export_chat_active",
                "step": "chat_id",
                "temp": temp2,
            }
            await event.reply(
                "حالا chat_id گروهی که می‌خوای فقط اعضای **فعال** (کسانی که پیام داده‌اند) رو بگیرم بفرست (مثلاً -1001234567890):",
                parse_mode="markdown",
            )
            return
        else:
            await event.reply(
                "فقط 1 یا 2 را بفرست:\n"
                "1️⃣ خروج کامل همه اعضای گروه\n"
                "2️⃣ فقط کسانی که پیام داده‌اند."
            )
            return

    # ====== ویزارد لاگین اکانت export ======
    if mode == "export_login":
        if step == "name":
            name = text
            if export_account_name_exists(name):
                await event.reply(
                    "این نام قبلاً برای اکانت export استفاده شده، یک نام دیگر بفرست."
                )
                return
            temp["name"] = name
            state["step"] = "api_id"
            state["temp"] = temp
            user_states[user_id] = state
            await event.reply("API_ID را بفرست (عدد):")
            return

        if step == "api_id":
            if not text.isdigit():
                await event.reply("API_ID باید عدد باشد. دوباره بفرست:")
                return
            temp["api_id"] = int(text)
            state["step"] = "api_hash"
            state["temp"] = temp
            user_states[user_id] = state
            await event.reply("API_HASH را بفرست:")
            return

        if step == "api_hash":
            temp["api_hash"] = text
            state["step"] = "phone"
            state["temp"] = temp
            user_states[user_id] = state
            await event.reply("شماره تلفن اکانت export را با فرمت +98912... بفرست:")
            return

        if step == "phone":
            phone = text
            temp["phone"] = phone
            api_id = temp["api_id"]
            api_hash = temp["api_hash"]

            exp_client = TelegramClient(StringSession(), api_id, api_hash)
            await exp_client.connect()
            try:
                sent = await exp_client.send_code_request(phone)
                temp["phone_code_hash"] = sent.phone_code_hash
                login_clients_export[user_id] = exp_client
                state["step"] = "code"
                state["temp"] = temp
                user_states[user_id] = state
                await event.reply(
                    f"کد به شماره {phone} ارسال شد.\n" "کد را همینجا بفرست (فقط عدد):"
                )
            except Exception as e:
                await event.reply(f"خطا در ارسال کد:\n{e}")
                traceback.print_exc()
                await exp_client.disconnect()
                login_clients_export.pop(user_id, None)
                user_states.pop(user_id, None)
            return

        if step == "code":
            code = text
            name = temp["name"]
            phone = temp["phone"]
            api_id = temp["api_id"]
            api_hash = temp["api_hash"]
            phone_code_hash = temp.get("phone_code_hash")

            exp_client = login_clients_export.get(user_id)
            if not exp_client:
                await event.reply(
                    "سشن لاگین export پیدا نشد. دوباره 📤 خروج اعضا را بزن."
                )
                user_states.pop(user_id, None)
                return

            try:
                await exp_client.sign_in(
                    phone=phone, code=code, phone_code_hash=phone_code_hash
                )
                session_string = exp_client.session.save()
                await exp_client.disconnect()
                login_clients_export.pop(user_id, None)

                acc_id = insert_account(
                    name=name,
                    phone=phone,
                    api_id=api_id,
                    api_hash=api_hash,
                    session_string=session_string,
                    kind="export",
                )

                temp2 = {"account_id": acc_id}
                user_states[user_id] = {
                    "mode": "export_mode",
                    "step": "choose",
                    "temp": temp2,
                }
                await event.reply(
                    f"✅ اکانت export `{name}` لاگین شد.\n"
                    "حالا نوع خروج اعضا را انتخاب کن:\n"
                    "1️⃣ خروج کامل همه اعضای گروه\n"
                    "2️⃣ فقط کسانی که پیام داده‌اند\n\n"
                    "فقط 1 یا 2 را بفرست.",
                    parse_mode="markdown",
                )

            except SessionPasswordNeededError:
                state["step"] = "2fa"
                state["temp"] = temp
                user_states[user_id] = state
                await event.reply(
                    "برای این اکانت export رمز دو مرحله‌ای (2FA) فعال است.\n"
                    "رمز دو مرحله‌ای این اکانت را همینجا بفرست:"
                )
            except PhoneCodeExpiredError:
                await event.reply(
                    "کد منقضی شده. دوباره دکمه 📤 خروج اعضا را بزن و از اول شروع کن."
                )
                await exp_client.disconnect()
                login_clients_export.pop(user_id, None)
                user_states.pop(user_id, None)
            except Exception as e:
                await event.reply(f"خطا در لاگین:\n{e}")
                traceback.print_exc()
                await exp_client.disconnect()
                login_clients_export.pop(user_id, None)
                user_states.pop(user_id, None)
            return

        if step == "2fa":
            password = text
            name = temp["name"]
            phone = temp["phone"]
            api_id = temp["api_id"]
            api_hash = temp["api_hash"]

            exp_client = login_clients_export.get(user_id)
            if not exp_client:
                await event.reply(
                    "سشن لاگین export پیدا نشد. دوباره 📤 خروج اعضا را بزن."
                )
                user_states.pop(user_id, None)
                return

            try:
                await exp_client.sign_in(password=password)
                session_string = exp_client.session.save()
                await exp_client.disconnect()
                login_clients_export.pop(user_id, None)

                acc_id = insert_account(
                    name=name,
                    phone=phone,
                    api_id=api_id,
                    api_hash=api_hash,
                    session_string=session_string,
                    kind="export",
                )

                temp2 = {"account_id": acc_id}
                user_states[user_id] = {
                    "mode": "export_mode",
                    "step": "choose",
                    "temp": temp2,
                }
                await event.reply(
                    f"✅ اکانت export `{name}` (با 2FA) لاگین شد.\n"
                    "حالا نوع خروج اعضا را انتخاب کن:\n"
                    "1️⃣ خروج کامل همه اعضای گروه\n"
                    "2️⃣ فقط کسانی که پیام داده‌اند\n\n"
                    "فقط 1 یا 2 را بفرست.",
                    parse_mode="markdown",
                )
            except Exception as e:
                await event.reply(
                    f"خطا در لاگین با رمز دو مرحله‌ای:\n{e}"
                )
                traceback.print_exc()
                await exp_client.disconnect()
                login_clients_export.pop(user_id, None)
                user_states.pop(user_id, None)
            return

    # ====== گرفتن اعضای گروه با اکانت export (همه اعضا) ======
    if mode == "export_chat" and step == "chat_id":
        try:
            chat_id_val = int(text)
        except ValueError:
            await event.reply(
                "chat_id باید عدد باشد. مثلاً -1001234567890"
            )
            return

        acc_id = temp.get("account_id")
        row = get_account_row_by_id(acc_id)
        if not row:
            await event.reply("اکانت export در دیتابیس یافت نشد.")
            user_states.pop(user_id, None)
            return

        session_string = row["session_string"]
        api_id = row["api_id"]
        api_hash = row["api_hash"]

        exp_client = TelegramClient(StringSession(session_string), api_id, api_hash)
        try:
            await exp_client.connect()
            if not await exp_client.is_user_authorized():
                await event.reply(
                    "این اکانت export دیگر لاگین نیست. مجدداً آن را بساز."
                )
                await exp_client.disconnect()
                user_states.pop(user_id, None)
                return

            entity = await exp_client.get_entity(chat_id_val)
            participants = await exp_client.get_participants(
                entity, aggressive=True
            )

            buffer = io.StringIO()
            writer = csv.writer(buffer, delimiter=",", lineterminator="\n")
            writer.writerow(
                ["username", "user_id", "access_hash", "name", "group", "group_id"]
            )

            seen_ids = set()
            count = 0
            for u in participants:
                if u.id in seen_ids:
                    continue
                seen_ids.add(u.id)
                name = " ".join(
                    filter(None, [u.first_name, u.last_name])
                )
                writer.writerow(
                    [
                        u.username or "",
                        u.id,
                        u.access_hash,
                        name,
                        getattr(entity, "title", "chat"),
                        chat_id_val,
                    ]
                )
                count += 1

            csv_bytes = buffer.getvalue().encode("utf-8")
            buffer.close()

            filename = sanitize_filename(getattr(entity, "title", "chat")) + ".csv"
            await client.send_file(
                chat_id,
                csv_bytes,
                filename=filename,
                caption=f"تعداد اعضا: {count}",
            )

            await exp_client.disconnect()
            user_states.pop(user_id, None)
            await send_main_menu(
                chat_id, "خروج اعضا انجام شد. از منو ادامه بده:"
            )

        except Exception as e:
            await event.reply(f"خطا در گرفتن اعضای گروه:\n{e}")
            traceback.print_exc()
        return

    # ====== گرفتن اعضای گروه بر اساس کسانی که پیام داده‌اند (فقط از روی پیام‌ها) ======
    if mode == "export_chat_active" and step == "chat_id":
        try:
            chat_id_val = int(text)
        except ValueError:
            await event.reply(
                "chat_id باید عدد باشد. مثلاً -1001234567890"
            )
            return

        acc_id = temp.get("account_id")
        row = get_account_row_by_id(acc_id)
        if not row:
            await event.reply("اکانت export در دیتابیس یافت نشد.")
            user_states.pop(user_id, None)
            return

        session_string = row["session_string"]
        api_id = row["api_id"]
        api_hash = row["api_hash"]

        exp_client = TelegramClient(StringSession(session_string), api_id, api_hash)
        try:
            await exp_client.connect()
            if not await exp_client.is_user_authorized():
                await event.reply(
                    "این اکانت export دیگر لاگین نیست. مجدداً آن را بساز."
                )
                await exp_client.disconnect()
                user_states.pop(user_id, None)
                return

            entity = await exp_client.get_entity(chat_id_val)

            await event.reply(
                "در حال اسکن پیام‌ها برای پیدا کردن کاربران فعال...\n"
                "این کار ممکن است با توجه به تعداد پیام‌ها کمی طول بکشد."
            )

            # sender_id -> User
            active_users = {}
            async for msg in exp_client.iter_messages(entity):
                sid = msg.sender_id
                if not sid:
                    continue
                if sid in active_users:
                    continue
                try:
                    sender = await msg.get_sender()
                except Exception:
                    continue
                if not sender:
                    continue
                active_users[sid] = sender

            buffer = io.StringIO()
            writer = csv.writer(buffer, delimiter=",", lineterminator="\n")
            writer.writerow(
                ["username", "user_id", "access_hash", "name", "group", "group_id"]
            )

            count = 0
            for uid, u in active_users.items():
                if not hasattr(u, "id"):
                    continue
                name = " ".join(
                    filter(
                        None,
                        [getattr(u, "first_name", None), getattr(u, "last_name", None)],
                    )
                )
                writer.writerow(
                    [
                        getattr(u, "username", "") or "",
                        u.id,
                        getattr(u, "access_hash", 0) or 0,
                        name,
                        getattr(entity, "title", "chat"),
                        chat_id_val,
                    ]
                )
                count += 1

            csv_bytes = buffer.getvalue().encode("utf-8")
            buffer.close()

            filename = sanitize_filename(
                getattr(entity, "title", "chat") + "-active"
            ) + ".csv"
            await client.send_file(
                chat_id,
                csv_bytes,
                filename=filename,
                caption=f"تعداد کاربران فعال (حداقل یک پیام): {count}",
            )

            await exp_client.disconnect()
            user_states.pop(user_id, None)
            await send_main_menu(
                chat_id, "خروج اعضای فعال انجام شد. از منو ادامه بده:"
            )

        except Exception as e:
            await event.reply(f"خطا در گرفتن اعضای فعال گروه:\n{e}")
            traceback.print_exc()
        return

    # ====== logout اکانت export و حذفش ======
    if mode == "logout_export" and step == "choose":
        accounts = temp.get("accounts", [])
        if not text.isdigit():
            await event.reply("فقط شماره اکانت را بفرست (مثلاً 0 یا 1).")
            return
        idx = int(text)
        if idx < 0 or idx >= len(accounts):
            await event.reply("شماره نامعتبر است. دوباره سعی کن.")
            return

        acc = accounts[idx]
        acc_id = acc["id"]
        row = get_account_row_by_id(acc_id)
        if not row:
            await event.reply("این اکانت export دیگر در دیتابیس نیست.")
            user_states.pop(user_id, None)
            return

        session_string = row["session_string"]
        api_id = row["api_id"]
        api_hash = row["api_hash"]

        exp_client = TelegramClient(StringSession(session_string), api_id, api_hash)
        try:
            await exp_client.connect()
            if await exp_client.is_user_authorized():
                await exp_client.log_out()
            await exp_client.disconnect()
        except Exception as e:
            await event.reply(
                f"در حین logout این اکانت خطایی رخ داد (ولی ادامه می‌دهیم):\n{e}"
            )

        delete_account_by_id(acc_id)
        user_states.pop(user_id, None)
        await event.reply(
            f"✅ از اکانت export `{acc['name']}` خارج شدی و از دیتابیس حذف شد.",
            parse_mode="markdown",
        )
        await send_main_menu(chat_id)
        return


# ------------ هندل اصلی پیام‌ها ------------
@client.on(events.NewMessage)
async def main_handler(event):
    global awaiting_group_number, target_group, target_group_id
    global target_group_username, target_group_title
    global ACTIVE_ADD_ACCOUNT, INVITE_DELAY, ACCOUNTS_ADD, INVITE_DELAY_MODE
    global current_add_jobs

    user_id = event.sender_id
    chat_id = event.chat_id
    text = (event.raw_text or "").strip()

    # /me
    if text == "/me":
        await event.reply(f"آی‌دی عددی شما: `{user_id}`", parse_mode="markdown")
        return

    # /setmeadmin
    if text == "/setmeadmin":
        if ADMINS and user_id not in ADMINS:
            await event.reply(
                "ادمین قبلاً تعریف شده. فقط ادمین‌ها می‌توانند ادمین جدید اضافه کنند."
            )
            return
        add_admin_db(user_id)
        await event.reply("✅ شما به عنوان ادمین ثبت شدید.")
        await send_main_menu(chat_id)
        return

    # /start
    if text == "/start":
        if is_admin(user_id):
            await event.reply(
                "سلام ادمین 👋\n"
                "از دکمه‌های زیر برای مدیریت استفاده کن.\n\n"
                "دستورات تکمیلی:\n"
                "/accounts  → لیست اکانت‌های add\n"
                "/useacc <name> → فقط برای علامت‌گذاری اکانت فعال (نمایشی)\n"
                "/delacc <name> → حذف اکانت add\n"
                "/admins → لیست ادمین‌ها\n"
                "/addadmin <id> /deladmin <id>\n"
                "/setdelay <sec|random> → تاخیر اد از CSV",
            )
            await send_main_menu(chat_id)
        else:
            await event.reply(
                "سلام 👋\n"
                "برای دیدن آی‌دی عددی خودت:\n`/me`\n\n"
                "اگر اولین بار استارت می‌کنی و ادمینی تعریف نشده:\n`/setmeadmin` را بزن.",
                parse_mode="markdown",
            )
        return

    # فقط ادمین‌ها
    if not is_admin(user_id):
        return

    # اگر فایل CSV فرستاده شده
    if event.document:
        file_name = (event.file.name or "").lower()
        if ".csv" in file_name:
            await event.reply("فایل CSV دریافت شد، در حال دانلود...")
            try:
                file_path = await client.download_media(event.document)
                user_states[user_id] = {
                    "mode": "confirm_add_csv",
                    "step": "confirm",
                    "temp": {"file_path": file_path},
                }
                await event.reply(
                    "فایل CSV دانلود شد.\n"
                    "اگر می‌خواهی فرآیند add روی این فایل شروع شود، «✅ شروع add» را بفرست.\n"
                    "اگر منصرف شدی، «❌ انصراف» را بفرست.",
                    buttons=[
                        [Button.text("✅ شروع add"), Button.text("❌ انصراف")]
                    ],
                )
            except Exception as e:
                await event.reply(
                    f"خطا در دانلود/پردازش فایل:\n{e}"
                )
                traceback.print_exc()
        else:
            await event.reply(
                "این فایل برای هیچ کاری استفاده نشد. فقط CSV برای add user قابل استفاده است."
            )
        return

    # توقف add
    if text == "⛔ توقف add":
        job = current_add_jobs.get(chat_id)
        if not job:
            await event.reply(
                "الان هیچ فرآیند add فعالی برای این چت در حال اجرا نیست."
            )
        else:
            job["cancel"] = True
            await event.reply(
                "⛔ درخواست توقف ثبت شد.\n"
                "اکانت‌ها بعد از تمام کردن کار روی یوزر فعلی متوقف می‌شوند."
            )
        return

    # مدیریت ادمین‌ها
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
            await event.reply(
                "فرمت درست: `/addadmin <user_id>`", parse_mode="markdown"
            )
            return
        new_id = int(parts[1])
        add_admin_db(new_id)
        await event.reply(
            f"✅ ادمین جدید اضافه شد: `{new_id}`", parse_mode="markdown"
        )
        return

    if text.startswith("/deladmin"):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await event.reply(
                "فرمت درست: `/deladmin <user_id>`", parse_mode="markdown"
            )
            return
        rem_id = int(parts[1])
        if rem_id in ADMINS:
            remove_admin_db(rem_id)
            await event.reply(
                f"✅ ادمین حذف شد: `{rem_id}`", parse_mode="markdown"
            )
        else:
            await event.reply("این آی‌دی جزو ادمین‌ها نیست.")
        return

    # /setdelay
    if text.startswith("/setdelay"):
        parts = text.split()
        if len(parts) == 2:
            arg = parts[1].strip().lower()
            if arg.isdigit():
                delay = int(arg)
                if delay < 1:
                    delay = 1
                INVITE_DELAY = delay
                INVITE_DELAY_MODE = "fixed"
                set_setting("invite_delay", str(INVITE_DELAY))
                set_setting("invite_delay_mode", "fixed")
                await event.reply(
                    f"✅ تاخیر بین ادها به صورت ثابت روی {INVITE_DELAY} ثانیه تنظیم شد."
                )
            elif arg in ("random", "rand"):
                INVITE_DELAY_MODE = "random"
                set_setting("invite_delay_mode", "random")
                await event.reply(
                    "✅ حالت تاخیر روی «رندوم بین 30 تا 100 ثانیه» تنظیم شد."
                )
            else:
                await event.reply(
                    "فرمت درست: `/setdelay <seconds>` یا `/setdelay random`",
                    parse_mode="markdown",
                )
        else:
            await event.reply(
                "فرمت درست: `/setdelay <seconds>` یا `/setdelay random`",
                parse_mode="markdown",
            )
        return

    if text == "⏱ تنظیم تاخیر":
        user_states[user_id] = {"mode": "setdelay", "step": "mode", "temp": {}}
        await event.reply(
            "نوع تاخیر را انتخاب کن:\n"
            "1️⃣ ثابت (عدد ثانیه مشخص)\n"
            "2️⃣ رندوم بین 30 تا 100 ثانیه\n\n"
            "فقط عدد 1 یا 2 را بفرست."
        )
        return

    # لیست اکانت‌های add
    if text in ("/accounts", "📜 اکانت‌ها"):
        if not ACCOUNTS_ADD:
            await event.reply("هیچ اکانتی برای add user ثبت نشده.")
        else:
            lines = ["اکانت‌های add:\n"]
            for acc in ACCOUNTS_ADD:
                mark = (
                    "(active-for-display)"
                    if acc["name"] == ACTIVE_ADD_ACCOUNT
                    else ""
                )
                lines.append(
                    f"- {acc['name']} {mark}  phone: {acc['phone']}"
                )
            lines.append(
                "\n⚠️ همه‌ی این اکانت‌ها در add از CSV استفاده می‌شوند."
            )
            await event.reply("\n".join(lines))
        return

    # useacc صرفا جهت نمایش
    if text.startswith("/useacc"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await event.reply(
                "فرمت درست: `/useacc <name>`", parse_mode="markdown"
            )
            return
        name = parts[1].strip()
        acc = get_add_account_by_name(name)
        if not acc:
            await event.reply("اکانت با این نام وجود ندارد.")
            return
        ACTIVE_ADD_ACCOUNT = name
        set_setting("active_add_account", name)
        await event.reply(f"✅ اکانت فعال (فقط نمایشی) تنظیم شد: {name}")
        return

    # delacc با دستور
    if text.startswith("/delacc"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await event.reply(
                "فرمت درست: `/delacc <name>`", parse_mode="markdown"
            )
            return
        name = parts[1].strip()
        acc = get_add_account_by_name(name)
        if not acc:
            await event.reply("اکانت با این نام وجود ندارد.")
            return
        acc_id = acc["id"]
        delete_account_by_id(acc_id)
        ACCOUNTS_ADD[:] = [a for a in ACCOUNTS_ADD if a["id"] != acc_id]
        if ACTIVE_ADD_ACCOUNT == name:
            ACTIVE_ADD_ACCOUNT = None
            set_setting("active_add_account", "")
        await event.reply(f"✅ اکانت حذف شد: {name}")
        return

    # افزودن اکانت add با دکمه
    if text == "➕ افزودن اکانت":
        user_states[user_id] = {"mode": "addacc", "step": "name", "temp": {}}
        await event.reply(
            "اسم دلخواه برای این اکانت add را بفرست (مثلاً main یا acc1):"
        )
        return

    # حذف اکانت add با دکمه
    if text == "🗑 حذف اکانت add":
        if not ACCOUNTS_ADD:
            await event.reply("هیچ اکانتی برای حذف وجود ندارد.")
            return
        names = [{"id": a["id"], "name": a["name"]} for a in ACCOUNTS_ADD]
        temp = {"names": names}
        user_states[user_id] = {
            "mode": "delacc_wizard",
            "step": "choose",
            "temp": temp,
        }
        lines = ["اکانت‌های add ثبت‌شده:"]
        for i, a in enumerate(names):
            lines.append(f"{i}: {a['name']}")
        lines.append("\nشماره اکانتی که می‌خوای حذف کنی رو بفرست:")
        await event.reply("\n".join(lines))
        return

    
    if text == "🧠 Add پیشرفته":
        if not ACCOUNTS_ADD:
            await event.reply("هیچ اکانتی برای add user ثبت نشده.")
            return
        accounts = get_export_accounts()
        if not accounts:
            await event.reply("هیچ اکانت export ثبت نشده.")
            return
        temp = {"accounts": accounts, "advanced": True}
        user_states[user_id] = {
            "mode": "add_choose_export",
            "step": "choose",
            "temp": temp,
        }
        lines = ["(حالت پیشرفته) اکانت export را انتخاب کن:"]
        for i, a in enumerate(accounts):
            lines.append(f"{i}: {a['name']}")
        await event.reply("\n".join(lines))
        return

    # شروع add (انتخاب اکانت export، بعد گروه)
    if text in ("🧾 شروع add", "/groups"):
        if not ACCOUNTS_ADD:
            await event.reply(
                "هیچ اکانتی برای add user ثبت نشده. اول از «➕ افزودن اکانت» استفاده کن."
            )
            return

        accounts = get_export_accounts()
        if not accounts:
            await event.reply(
                "هیچ اکانت export ثبت نشده.\n"
                "اول از طریق «📤 خروج اعضا» یک اکانت export بساز تا بتوانم با آن لیست گروه‌ها را بگیرم."
            )
            return

        temp = {"accounts": accounts}
        user_states[user_id] = {
            "mode": "add_choose_export",
            "step": "choose",
            "temp": temp,
        }

        lines = ["برای شروع add، اول اکانت export را انتخاب کن:"]
        for i, a in enumerate(accounts):
            lines.append(f"{i}: {a['name']}  phone: {a['phone']}")
        lines.append("\nشماره اکانت export را بفرست (مثلاً 0):")
        await event.reply("\n".join(lines))
        return

    # بعد از گرفتن لیست گروه‌ها، انتخاب شماره گروه
    if awaiting_group_number and text.isdigit():
        idx = int(text)
        if idx < 0 or idx >= len(groups_cache):
            await event.reply(
                "شماره گروه نامعتبر است. دوباره دکمه 🧾 شروع add را بزن."
            )
            return
        target = groups_cache[idx]
        target_group = target
        target_group_id = target.id
        target_group_username = getattr(target, "username", None)
        target_group_title = getattr(target, "title", "گروه")
        awaiting_group_number = False
        await event.reply(
            f"✅ گروه برای add user انتخاب شد:\n{target_group_title}\n"
            f"(ID: {target_group_id})\n\n"
            f"حالا فایل CSV را بفرست تا با همه اکانت‌های add روی این گروه add انجام شود."
        )
        return

    # خروج اعضا
    if text in ("📤 خروج اعضا", "/export"):
        accounts = get_export_accounts()
        if not accounts:
            user_states[user_id] = {
                "mode": "export_login",
                "step": "name",
                "temp": {},
            }
            await event.reply(
                "هیچ اکانت exportی ثبت نشده.\n"
                "اول یک اکانت export بساز.\n"
                "اسم دلخواه برای این اکانت را بفرست (مثلاً exp1):"
            )
            return

        temp = {"accounts": accounts}
        user_states[user_id] = {
            "mode": "export_select",
            "step": "choose",
            "temp": temp,
        }
        lines = ["اکانت‌های export موجود:"]
        for i, a in enumerate(accounts):
            lines.append(f"{i}: {a['name']}  phone: {a['phone']}")
        lines.append(
            '\nیک عدد برای انتخاب اکانت بفرست، یا عبارت "new" برای ساخت اکانت جدید:'
        )
        await event.reply("\n".join(lines))
        return

    # logout اکانت export
    if text == "🚪 خروج اکانت‌های export":
        accounts = get_export_accounts()
        if not accounts:
            await event.reply("هیچ اکانت exportی برای logout وجود ندارد.")
            return

        temp = {"accounts": accounts}
        user_states[user_id] = {
            "mode": "logout_export",
            "step": "choose",
            "temp": temp,
        }
        lines = ["اکانت‌های export:"]
        for i, a in enumerate(accounts):
            lines.append(f"{i}: {a['name']}  phone: {a['phone']}")
        lines.append(
            "\nشماره اکانتی که می‌خوای logout و حذف کنی رو بفرست:"
        )
        await event.reply("\n".join(lines))
        return

    # جوین همه اکانت‌های add به یک گروه
    if text == "👥 جوین اکانت‌ها":
        if not ACCOUNTS_ADD:
            await event.reply("هیچ اکانتی برای add user ثبت نشده.")
            return
        user_states[user_id] = {
            "mode": "join_all_add",
            "step": "link",
            "temp": {},
        }
        await event.reply(
            "لینک گروه مقصد را بفرست (عمومی یا خصوصی):\n"
            "مثال‌ها:\n"
            "https://t.me/SBMUgap\n"
            "t.me/SBMUgap\n"
            "https://t.me/+_FVFe-WWKtRhZTdk"
        )
        return

    # اگر یوزر state داشت، بفرست به state handler
    if user_id in user_states and not text.startswith("/"):
        await handle_state_message(event, user_states[user_id])
        return

    # هر چیز دیگه‌ای
    if text:
        await event.reply(
            "دستور نامعتبر.\nاز /start یا منوی دکمه‌ای استفاده کن."
        )
        return


# ------------ ران ربات ------------
async def run_bot():
    print("Initializing DB and loading data...")
    init_db()
    print("Admins:", ADMINS)
    print("Invite delay:", INVITE_DELAY, "mode:", INVITE_DELAY_MODE)
    print("Loaded add-accounts:", [a["name"] for a in ACCOUNTS_ADD])
    print("Bot starting (async)...")
    await client.start(bot_token=BOT_TOKEN)
    print("Bot is running. Waiting for commands...")
    await client.run_until_disconnected()


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
