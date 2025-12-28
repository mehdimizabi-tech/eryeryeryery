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
    PeerFloodError,
    UserPrivacyRestrictedError,
    UserAlreadyParticipantError,
)
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError
from telethon.sessions import StringSession


API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# آیدی عددی ادمین اصلی
OWNER_ID = 6474515118
# آدرس دیتابیس (Neon / Render)
DATABASE_URL = os.environ.get("DATABASE_URL")

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("API_ID / API_HASH / BOT_TOKEN must be set.")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL must be set.")

BOT_SESSION = "bot_session"
client = TelegramClient(BOT_SESSION, API_ID, API_HASH)

# متغیرهای کلی
ADMINS = set()
INVITE_DELAY = 60              # ثانیه
INVITE_DELAY_MODE = "fixed"    # fixed یا random

ACCOUNTS_ADD = []              # لیست اکانت‌های add از دیتابیس
ACTIVE_ADD_ACCOUNT = None      # فقط برای نمایش (سمبلیک)

user_states = {}               # state ماشین برای مکالمه
login_clients_add = {}         # سشن‌های موقت لاگین add
login_clients_export = {}      # سشن‌های موقت لاگین export

groups_cache = []              # کش لیست گروه‌ها (از اکانت export)
target_group = None            # گروه انتخاب‌شده برای add
awaiting_group_number = False  # آیا منتظر شماره گروه هستیم؟

current_add_jobs = {}          # برای استاپ کردن add ها بر اساس chat_id


# ---------- اتصال به دیتابیس ----------

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
            cur.execute("SELECT value FROM settings WHERE key = 'invite_delay'")
            row = cur.fetchone()
            if row:
                try:
                    INVITE_DELAY = int(row["value"])
                except ValueError:
                    INVITE_DELAY = 60
            else:
                INVITE_DELAY = 60

            cur.execute("SELECT value FROM settings WHERE key = 'invite_delay_mode'")
            row = cur.fetchone()
            if row and row["value"] in ("fixed", "random"):
                INVITE_DELAY_MODE = row["value"]
            else:
                INVITE_DELAY_MODE = "fixed"

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
                ACCOUNTS_ADD.append({
                    "id": r["id"],
                    "name": r["name"],
                    "phone": r["phone"],
                    "api_id": r["api_id"],
                    "api_hash": r["api_hash"],
                    "session_string": r["session_string"],
                })


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
    accounts = []
    for r in rows:
        accounts.append({
            "id": r["id"],
            "name": r["name"],
            "phone": r["phone"] or ""
        })
    return accounts


def get_account_row_by_id(acc_id: int):
    with get_db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM accounts WHERE id = %s", (acc_id,))
            row = cur.fetchone()
    return row


def export_account_name_exists(name: str) -> bool:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM accounts WHERE kind = 'export' AND name = %s",
                (name,),
            )
            row = cur.fetchone()
    return row is not None


def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def get_add_account_by_name(name: str):
    for acc in ACCOUNTS_ADD:
        if acc["name"] == name:
            return acc
    return None


# ---------- منوی اصلی ----------

def main_menu():
    return [
        [
            Button.text("➕ افزودن اکانت"),
            Button.text("📜 اکانت‌ها"),
        ],
        [
            Button.text("🧾 شروع add"),
            Button.text("📤 خروج اعضا"),
        ],
        [
            Button.text("⏱ تنظیم تاخیر"),
            Button.text("🗑 حذف اکانت add"),
        ],
        [
            Button.text("🚪 خروج اکانت‌های export"),
            Button.text("👥 جوین اکانت‌ها"),
        ],
        [
            Button.text("⛔ توقف add"),
        ],
    ]


async def send_main_menu(chat_id, text="از منوی زیر استفاده کن:"):
    await client.send_message(chat_id, text, buttons=main_menu())


def sanitize_filename(title: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower())
    return f"members-{safe}.csv"


# ---------- پردازش لینک گروه برای جوین ----------

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


async def join_all_add_accounts(group_link: str, chat_id: int):
    if not ACCOUNTS_ADD:
        await client.send_message(chat_id, "هیچ اکانتی برای add user ثبت نشده.")
        return

    mode, value = parse_group_link(group_link)
    await client.send_message(
        chat_id,
        f"در حال جوین کردن همه اکانت‌های add به گروه با لینک:\n{group_link}"
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
                await client.send_message(chat_id, f"⚠️ [{name}] لاگین نیست، از این اکانت استفاده نشد.")
                continue

            try:
                if mode == "invite":
                    invite_hash = value
                    try:
                        await user_client(ImportChatInviteRequest(invite_hash))
                        await client.send_message(
                            chat_id,
                            f"✅ [{name}] با لینک خصوصی به گروه join شد."
                        )
                    except UserAlreadyParticipantError:
                        await client.send_message(
                            chat_id,
                            f"ℹ️ [{name}] قبلاً عضو این گروه بوده."
                        )
                else:
                    username = value
                    try:
                        entity = await user_client.get_entity(username)
                    except Exception as ee:
                        await client.send_message(
                            chat_id,
                            f"⚠️ [{name}] نتوانست گروه را از روی لینک/یوزرنیم پیدا کند:\n{ee}"
                        )
                        continue

                    try:
                        await user_client(JoinChannelRequest(entity))
                        await client.send_message(
                            chat_id,
                            f"✅ [{name}] به گروه عمومی join شد."
                        )
                    except UserAlreadyParticipantError:
                        await client.send_message(
                            chat_id,
                            f"ℹ️ [{name}] قبلاً عضو این گروه بوده."
                        )
            except Exception as e:
                await client.send_message(
                    chat_id,
                    f"❌ خطا در جوین برای اکانت [{name}]:\n{e}"
                )
                traceback.print_exc()

        except Exception as e:
            await client.send_message(
                chat_id,
                f"❌ خطا در اتصال سشن [{name}]:\n{e}"
            )
            traceback.print_exc()
        finally:
            try:
                await user_client.disconnect()
            except:
                pass

    await client.send_message(
        chat_id,
        "✅ فرآیند جوین برای همه اکانت‌های add تمام شد."
    )


# ---------- add از CSV با چند اکانت همزمان + استاپ ----------

async def add_users_from_csv_file(file_path, chat_id):
    global target_group, current_add_jobs

    if not ACCOUNTS_ADD:
        await client.send_message(chat_id, "هیچ اکانتی برای add user ثبت نشده. اول از «➕ افزودن اکانت» استفاده کن.")
        return

    if target_group is None:
        await client.send_message(chat_id, "هیچ گروهی برای add user انتخاب نشده. از دکمه 🧾 شروع add استفاده کن.")
        return

    if chat_id in current_add_jobs:
        await client.send_message(chat_id, "الان یک فرآیند add برای این چت در حال اجراست. اول با «⛔ توقف add» متوقفش کن.")
        return

    users = []
    try:
        with open(file_path, encoding="utf-8") as f:
            reader = csv.reader(f, delimiter=",", lineterminator="\n")
            next(reader, None)  # header
            for row in reader:
                if len(row) < 3:
                    continue
                users.append({
                    "username": row[0],
                    "id": int(row[1]) if row[1] else 0,
                    "access_hash": int(row[2]) if row[2] else 0
                })
    except Exception as e:
        await client.send_message(chat_id, f"⚠️ خطا در خواندن CSV:\n{e}")
        traceback.print_exc()
        return

    if not users:
        await client.send_message(chat_id, "هیچ کاربری در CSV پیدا نشد.")
        return

    total_users = len(users)
    total_accounts = len(ACCOUNTS_ADD)

    # آبجکت job برای استاپ
    job = {"cancel": False}
    current_add_jobs[chat_id] = job

    # تقسیم یوزرها بین اکانت‌ها
    per_account_users = [[] for _ in range(total_accounts)]
    for idx, user in enumerate(users):
        acc_index = idx % total_accounts
        per_account_users[acc_index].append(user)

    await client.send_message(
        chat_id,
        f"در حال تقسیم {total_users} کاربر بین {total_accounts} اکانت add و شروع اد همزمان...\n"
        "برای توقف وسط کار می‌تونی از دکمه «⛔ توقف add» استفاده کنی."
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
                await client.send_message(chat_id, f"⚠️ اکانت {name} لاگین نیست، از این اکانت استفاده نشد.")
                return

            # *** مهم: کانال هدف را با id و access_hash همین target_group می‌سازیم (بدون get_input_entity) ***
            target_entity = InputPeerChannel(target_group.id, target_group.access_hash)

            total_for_acc = len(users_for_this_acc)
            await client.send_message(
                chat_id,
                f"▶️ اکانت {name} شروع کرد. تعداد سهم این اکانت: {total_for_acc} کاربر."
            )

            for idx, user in enumerate(users_for_this_acc, start=1):
                if job.get("cancel"):
                    await client.send_message(chat_id, f"⏹ اکانت {name} به درخواست شما متوقف شد.")
                    break

                username_or_id = user["username"] or f"id:{user['id']}"

                try:
                    await client.send_message(
                        chat_id,
                        f"[{name} {idx}/{total_for_acc}] در حال اضافه کردن: {username_or_id}"
                    )

                    if user["username"]:
                        user_entity = await user_client.get_input_entity(user["username"])
                    else:
                        user_entity = InputPeerUser(user["id"], user["access_hash"])

                    await user_client(InviteToChannelRequest(target_entity, [user_entity]))
                    await client.send_message(chat_id, f"✅ [{name}] اضافه شد: {username_or_id}")

                except PeerFloodError:
                    await client.send_message(
                        chat_id,
                        f"⛔ [{name}] خطای Flood از سمت تلگرام. این اکانت متوقف شد."
                    )
                    break
                except UserPrivacyRestrictedError:
                    await client.send_message(
                        chat_id,
                        f"⚠️ [{name}] محدودیت حریم خصوصی، رد شد: {username_or_id}"
                    )
                except Exception as e:
                    await client.send_message(
                        chat_id,
                        f"⚠️ [{name}] خطا برای {username_or_id}:\n{e}"
                    )
                    traceback.print_exc()

                if job.get("cancel"):
                    await client.send_message(chat_id, f"⏹ اکانت {name} به درخواست شما متوقف شد.")
                    break

                # تاخیر بین ادها
                if INVITE_DELAY_MODE == "random":
                    delay = random.randint(30, 100)
                else:
                    delay = INVITE_DELAY
                    if delay < 1:
                        delay = 1

                await asyncio.sleep(delay)

            else:
                await client.send_message(chat_id, f"⏹ اکانت {name} کارش تمام شد.")

        except Exception as e:
            await client.send_message(chat_id, f"❌ خطای کلی برای اکانت {name}:\n{e}")
            traceback.print_exc()
        finally:
            try:
                await user_client.disconnect()
            except:
                pass

    # اجرای همزمان worker ها
    tasks = []
    for acc, acc_users in zip(ACCOUNTS_ADD, per_account_users):
        if acc_users:
            tasks.append(asyncio.create_task(add_worker(acc, acc_users, job)))

    if not tasks:
        current_add_jobs.pop(chat_id, None)
        await client.send_message(chat_id, "هیچ کاربری بین اکانت‌ها توزیع نشد (لیست خالی بود).")
        return

    await asyncio.gather(*tasks)

    if job.get("cancel"):
        await client.send_message(chat_id, "⛔ فرآیند add به درخواست شما متوقف شد.")
    else:
        await client.send_message(chat_id, "✅ فرآیند add با همه اکانت‌ها تمام شد.")

    current_add_jobs.pop(chat_id, None)


# ---------- state machine (پیام‌های متنی در حالت‌های مختلف) ----------

async def handle_state_message(event, state):
    global INVITE_DELAY, ACTIVE_ADD_ACCOUNT, ACCOUNTS_ADD, INVITE_DELAY_MODE, groups_cache, awaiting_group_number, target_group

    user_id = event.sender_id
    chat_id = event.chat_id
    text = (event.raw_text or "").strip()
    mode = state.get("mode")
    step = state.get("step")
    temp = state.get("temp", {})

    # --- انتخاب اکانت export برای شروع add (گرفتن لیست گروه‌ها) ---
    if mode == "add_choose_export":
        if step == "choose":
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
                    await event.reply("این اکانت export دیگر لاگین نیست. دوباره از «📤 خروج اعضا» آن را بساز.")
                    await export_client.disconnect()
                    user_states.pop(user_id, None)
                    return

                result = await export_client(GetDialogsRequest(
                    offset_date=None,
                    offset_id=0,
                    offset_peer=InputPeerEmpty(),
                    limit=200,
                    hash=0
                ))
                groups_cache = [c for c in result.chats if getattr(c, "megagroup", False)]

                await export_client.disconnect()

                if not groups_cache:
                    await event.reply("هیچ سوپرگروهی در این اکانت export پیدا نشد.")
                    user_states.pop(user_id, None)
                    return

                lines = [f"لیست سوپرگروه‌ها با اکانت export `{name}`:\n"]
                for i, g in enumerate(groups_cache):
                    lines.append(f"{i}: {g.title}")
                lines.append("\nیک عدد بفرست تا همان گروه برای add user انتخاب شود.\n"
                             "بعد از انتخاب گروه، فایل CSV را بفرست تا add انجام شود.")

                awaiting_group_number = True
                user_states.pop(user_id, None)
                await event.reply("\n".join(lines), parse_mode="markdown")

            except Exception as e:
                await event.reply(f"خطا در گرفتن لیست گروه‌ها از اکانت export:\n{e}")
                traceback.print_exc()
                try:
                    await export_client.disconnect()
                except:
                    pass
                user_states.pop(user_id, None)
            return

    # --- جوین همه اکانت‌های add به یک گروه ---
    if mode == "join_all_add":
        if step == "link":
            group_link = text
            user_states.pop(user_id, None)
            await join_all_add_accounts(group_link, chat_id)
            await send_main_menu(chat_id, "کار جوین اکانت‌ها تمام شد. از منو ادامه بده:")
            return

    # --- تایید شروع add از روی CSV ---
    if mode == "confirm_add_csv":
        if step == "confirm":
            file_path = temp.get("file_path")
            lower = text.strip().lower()

            if lower in ["✅ شروع add".lower(), "شروع add", "شروع", "yes", "y"]:
                user_states.pop(user_id, None)
                await event.reply("✅ شروع فرآیند add از روی این CSV...")
                await add_users_from_csv_file(file_path, chat_id)
                return

            elif lower in ["❌ انصراف".lower(), "انصراف", "cancel", "لغو"]:
                user_states.pop(user_id, None)
                try:
                    if file_path and os.path.exists(file_path):
                        os.remove(file_path)
                except Exception:
                    pass
                await event.reply("✅ فرآیند add برای این فایل CSV لغو شد.")
                await send_main_menu(chat_id)
                return
            else:
                await event.reply("برای ادامه، «✅ شروع add» یا «❌ انصراف» را بفرست.")
                return

    # --- افزودن اکانت add (لاگین با کد + 2FA) ---
    if mode == "addacc":
        if step == "name":
            name = text
            if get_add_account_by_name(name):
                await event.reply("این نام قبلاً برای اکانت add استفاده شده، یک نام دیگر بفرست.")
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
                await event.reply("سشن لاگین پیدا نشد. دوباره ➕ افزودن اکانت را بزن.")
                user_states.pop(user_id, None)
                return

            try:
                await acc_client.sign_in(
                    phone=phone,
                    code=code,
                    phone_code_hash=phone_code_hash
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
                    kind="add"
                )

                ACCOUNTS_ADD.append({
                    "id": acc_id,
                    "name": name,
                    "phone": phone,
                    "api_id": api_id,
                    "api_hash": api_hash,
                    "session_string": session_string,
                })

                if not ACTIVE_ADD_ACCOUNT:
                    ACTIVE_ADD_ACCOUNT = name
                    set_setting("active_add_account", name)

                user_states.pop(user_id, None)
                await event.reply(f"✅ اکانت `{name}` برای add user ثبت و لاگین شد.", parse_mode="markdown")
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
                await event.reply("کد منقضی شده. دوباره دکمه «➕ افزودن اکانت» را بزن و از اول شروع کن.")
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
                await event.reply("سشن لاگین پیدا نشد. دوباره ➕ افزودن اکانت را بزن.")
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
                    kind="add"
                )

                ACCOUNTS_ADD.append({
                    "id": acc_id,
                    "name": name,
                    "phone": phone,
                    "api_id": api_id,
                    "api_hash": api_hash,
                    "session_string": session_string,
                })

           

0
