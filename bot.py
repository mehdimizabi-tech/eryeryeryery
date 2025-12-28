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
from telethon.tl.functions.messages import GetDialogsRequest
from telethon.tl.types import InputPeerEmpty, InputPeerChannel, InputPeerUser
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.errors.rpcerrorlist import PeerFloodError, UserPrivacyRestrictedError
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError
from telethon.sessions import StringSession


# ------------------ تنظیمات محیطی ------------------

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# آیدی عددی ادمین اصلی
OWNER_ID = 6474515118

DATABASE_URL = os.environ.get("DATABASE_URL")

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("API_ID / API_HASH / BOT_TOKEN باید تو Environment Variable ست بشن.")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL برای اتصال به Postgres ست نشده.")

BOT_SESSION = "bot_session"
client = TelegramClient(BOT_SESSION, API_ID, API_HASH)


# ------------------ متغیرهای درون حافظه ------------------

ADMINS = set()
INVITE_DELAY = 60             # ثانیه، در حالت fixed
INVITE_DELAY_MODE = "fixed"   # "fixed" یا "random" (30-100)

ACCOUNTS_ADD = []             # list of dicts: {id, name, phone, api_id, api_hash, session_string}
ACTIVE_ADD_ACCOUNT = None     # نام اکانت فعال برای add user (فقط برای گرفتن لیست گروه‌ها)

user_states = {}           # user_id -> {mode, step, temp}
login_clients_add = {}     # user_id -> TelegramClient موقت هنگام لاگین add
login_clients_export = {}  # user_id -> TelegramClient موقت هنگام لاگین export

groups_cache = []
target_group = None
awaiting_group_number = False


# ------------------ توابع دیتابیس (PostgreSQL - psycopg3) ------------------

def get_db_connection():
    return psycopg.connect(DATABASE_URL)


def init_db():
    """ایجاد جداول و لود تنظیمات/ادمین/اکانت‌ها"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # جدول ادمین‌ها
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    user_id BIGINT PRIMARY KEY
                )
            """)

            # تنظیمات کلی
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            # اکانت‌ها (add + export)
            cur.execute("""
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
            """)

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

            # حتماً OWNER_ID همیشه ادمین باشد
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
            # مقدار delay
            cur.execute("SELECT value FROM settings WHERE key = 'invite_delay'")
            row = cur.fetchone()
            if row:
                try:
                    INVITE_DELAY = int(row["value"])
                except ValueError:
                    INVITE_DELAY = 60
            else:
                INVITE_DELAY = 60

            # مود delay (fixed / random)
            cur.execute("SELECT value FROM settings WHERE key = 'invite_delay_mode'")
            row = cur.fetchone()
            if row and row["value"] in ("fixed", "random"):
                INVITE_DELAY_MODE = row["value"]
            else:
                INVITE_DELAY_MODE = "fixed"

            # اکانت فعال
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
    """فقط اکانت‌های نوع add را در حافظه می‌آورد"""
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
    """لیست همه اکانت‌های export از دیتابیس"""
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


# ------------------ منو ------------------

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
            Button.text("🗑 حذف اکانت add"),
        ],
        [
            Button.text("🚪 خروج اکانت‌های export"),
        ],
    ]


async def send_main_menu(chat_id, text="از منوی زیر استفاده کن:"):
    await client.send_message(chat_id, text, buttons=main_menu())


# ------------------ کمک برای add user ------------------

async def get_add_account_client():
    """
    فقط برای گرفتن لیست گروه‌ها از یک اکانت فعال استفاده می‌شود.
    خود add از همه اکانت‌های ACCOUNTS_ADD استفاده می‌کند.
    """
    if not ACTIVE_ADD_ACCOUNT:
        raise RuntimeError("هیچ اکانت فعالی برای add user تنظیم نشده است.")
    acc = get_add_account_by_name(ACTIVE_ADD_ACCOUNT)
    if not acc:
        raise RuntimeError("اکانت فعال در دیتابیس پیدا نشد.")

    session = StringSession(acc["session_string"])
    user_client = TelegramClient(session, acc["api_id"], acc["api_hash"])
    await user_client.connect()
    if not await user_client.is_user_authorized():
        raise RuntimeError("این اکانت دیگر لاگین نیست (سشن منقضی شده). دوباره اکانت را اضافه کن.")
    return user_client


async def fetch_groups_for_active():
    """لیست گروه‌های اکانت فعال برای add user (برای انتخاب گروه هدف)"""
    global groups_cache
    user_client = await get_add_account_client()
    result = await user_client(GetDialogsRequest(
        offset_date=None,
        offset_id=0,
        offset_peer=InputPeerEmpty(),
        limit=200,
        hash=0
    ))
    groups_cache = [c for c in result.chats if getattr(c, "megagroup", False)]
    await user_client.disconnect()
    return groups_cache


def groups_text():
    if not groups_cache:
        return "هیچ سوپرگروهی یافت نشد (یا این اکانت در سوپرگروهی نیست)."
    lines = [f"اکانت فعال برای گرفتن لیست گروه‌ها: {ACTIVE_ADD_ACCOUNT}\n", "لیست سوپرگروه‌ها:"]
    for i, g in enumerate(groups_cache):
        lines.append(f"{i}: {g.title}")
    lines.append("\nیک عدد بفرست تا همان گروه برای add user انتخاب شود.\n"
                 "⚠️ توجه: بعد از انتخاب گروه، در مرحله add همه اکانت‌های add با هم استفاده می‌شوند.")
    return "\n".join(lines)


def sanitize_filename(title: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower())
    return f"members-{safe}.csv"


# ------------------ add multi-account از CSV ------------------

async def add_users_from_csv_file(file_path, chat_id):
    """
    add user از CSV با استفاده از همه اکانت‌های ACCOUNTS_ADD.
    - لیست یوزرها بین اکانت‌ها به صورت Round Robin تقسیم می‌شود.
    - برای هر اکانت یک کلاینت جدا ساخته می‌شود.
    - همه اکانت‌ها همزمان کار می‌کنند.
    - تاخیر برای هر اکانت مستقل اعمال می‌شود.
    """
    global target_group

    if not ACCOUNTS_ADD:
        await client.send_message(chat_id, "هیچ اکانتی برای add user ثبت نشده. اول از «➕ افزودن اکانت» استفاده کن.")
        return

    if target_group is None:
        await client.send_message(chat_id, "هیچ گروهی برای add user انتخاب نشده. از دکمه 🧾 گروه‌ها استفاده کن.")
        return

    # خواندن CSV
    users = []
    try:
        with open(file_path, encoding="utf-8") as f:
            reader = csv.reader(f, delimiter=",", lineterminator="\n")
            next(reader, None)  # پرش از هدر
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

    # تقسیم کاربران بین اکانت‌ها (Round Robin)
    per_account_users = [[] for _ in range(total_accounts)]
    for idx, user in enumerate(users):
        acc_index = idx % total_accounts
        per_account_users[acc_index].append(user)

    await client.send_message(
        chat_id,
        f"در حال تقسیم {total_users} کاربر بین {total_accounts} اکانت add و شروع اد همزمان..."
    )

    # تعریف worker برای هر اکانت
    async def add_worker(acc, users_for_this_acc):
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

            target_entity = InputPeerChannel(target_group.id, target_group.access_hash)
            total_for_acc = len(users_for_this_acc)

            await client.send_message(
                chat_id,
                f"▶️ اکانت {name} شروع کرد. تعداد سهم این اکانت: {total_for_acc} کاربر."
            )

            for idx, user in enumerate(users_for_this_acc, start=1):
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

                # تاخیر مخصوص این اکانت
                if INVITE_DELAY_MODE == "random":
                    delay = random.randint(30, 100)
                else:
                    delay = INVITE_DELAY
                    if delay < 1:
                        delay = 1
                await asyncio.sleep(delay)

            await client.send_message(chat_id, f"⏹ اکانت {name} کارش تمام شد.")

        except Exception as e:
            await client.send_message(chat_id, f"❌ خطای کلی برای اکانت {name}:\n{e}")
            traceback.print_exc()
        finally:
            try:
                await user_client.disconnect()
            except:
                pass

    # ساختن تسک‌ها برای همه اکانت‌ها
    tasks = []
    for acc, acc_users in zip(ACCOUNTS_ADD, per_account_users):
        if acc_users:
            tasks.append(asyncio.create_task(add_worker(acc, acc_users)))

    if not tasks:
        await client.send_message(chat_id, "هیچ کاربری بین اکانت‌ها توزیع نشد (لیست خالی بود).")
        return

    await asyncio.gather(*tasks)
    await client.send_message(chat_id, "✅ فرآیند add با همه اکانت‌ها تمام شد.")


# ------------------ state handler ------------------

async def handle_state_message(event, state):
    """هدل کردن ویزاردها"""
    global INVITE_DELAY, ACTIVE_ADD_ACCOUNT, ACCOUNTS_ADD, INVITE_DELAY_MODE

    user_id = event.sender_id
    chat_id = event.chat_id
    text = (event.raw_text or "").strip()
    mode = state.get("mode")
    step = state.get("step")
    temp = state.get("temp", {})

    # ---------- افزودن اکانت add با لاگین کد + 2FA ----------
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
                # موفق بدون نیاز به 2FA
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
                # اکانت 2FA دارد → رمز دو مرحله‌ای را بخواه
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
            # رمز دو مرحله‌ای برای اکانت add
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
                # اینجا فقط password را می‌دیم، قبلاً code را داده بودیم
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

                if not ACTIVE_ADD_ACCOUNT:
                    ACTIVE_ADD_ACCOUNT = name
                    set_setting("active_add_account", name)

                user_states.pop(user_id, None)
                await event.reply(
                    f"✅ اکانت `{name}` (با 2FA) برای add user ثبت و لاگین شد.",
                    parse_mode="markdown"
                )
                await send_main_menu(chat_id)

            except Exception as e:
                await event.reply(f"خطا در لاگین با رمز دو مرحله‌ای:\n{e}")
                traceback.print_exc()
                await acc_client.disconnect()
                login_clients_add.pop(user_id, None)
                user_states.pop(user_id, None)
            return

    # ---------- تنظیم تاخیر ----------
    if mode == "setdelay":
        # مرحله انتخاب مود
        if step == "mode":
            lower = text.strip().lower()
            if lower in ("1", "ثابت", "fixed"):
                # می‌ریم سراغ گرفتن مقدار ثابت
                state["step"] = "value"
                state["temp"] = {}
                user_states[user_id] = state
                await event.reply("عدد تاخیر بین ادها (ثانیه) را بفرست:")
                return
            elif lower in ("2", "رندوم", "random"):
                INVITE_DELAY_MODE = "random"
                set_setting("invite_delay_mode", "random")
                user_states.pop(user_id, None)
                await event.reply("✅ حالت تاخیر روی «رندوم بین 30 تا 100 ثانیه» تنظیم شد.")
                await send_main_menu(chat_id)
                return
            else:
                await event.reply("فقط عدد 1 (ثابت) یا 2 (رندوم) را بفرست.")
                return

        # مرحله گرفتن مقدار ثابت
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
            await event.reply(f"✅ تاخیر بین ادها به صورت ثابت روی {INVITE_DELAY} ثانیه تنظیم شد.")
            await send_main_menu(chat_id)
            return

    # ---------- حذف اکانت add ----------
    if mode == "delacc_wizard":
        if step == "choose":
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

    # ---------- انتخاب اکانت export برای خروج اعضا ----------
    if mode == "export_select":
        if step == "choose":
            accounts = temp.get("accounts", [])
            lower = text.lower()
            if lower == "new":
                user_states[user_id] = {
                    "mode": "export_login",
                    "step": "name",
                    "temp": {},
                }
                await event.reply("اسم دلخواه برای این اکانت export را بفرست (مثلاً exp1):")
                return

            if not text.isdigit():
                await event.reply('یک عدد برای انتخاب اکانت یا عبارت "new" برای ساخت اکانت جدید بفرست.')
                return
            idx = int(text)
            if idx < 0 or idx >= len(accounts):
                await event.reply("شماره نامعتبر است. دوباره سعی کن.")
                return

            acc_id = accounts[idx]["id"]
            temp2 = {"account_id": acc_id}
            user_states[user_id] = {"mode": "export_chat", "step": "chat_id", "temp": temp2}
            await event.reply("حالا chat_id گروه را بفرست (مثلاً -1001234567890):")
            return

    # ---------- ویزارد لاگین اکانت export با کد + 2FA ----------
    if mode == "export_login":
        if step == "name":
            name = text
            if export_account_name_exists(name):
                await event.reply("این نام قبلاً برای اکانت export استفاده شده، یک نام دیگر بفرست.")
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
                    f"کد به شماره {phone} ارسال شد.\n"
                    "کد را همینجا بفرست (فقط عدد):"
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
                await event.reply("سشن لاگین export پیدا نشد. دوباره 📤 خروج اعضا را بزن.")
                user_states.pop(user_id, None)
                return

            try:
                await exp_client.sign_in(
                    phone=phone,
                    code=code,
                    phone_code_hash=phone_code_hash
                )
                # موفق بدون 2FA
                session_string = exp_client.session.save()
                await exp_client.disconnect()
                login_clients_export.pop(user_id, None)

                acc_id = insert_account(
                    name=name,
                    phone=phone,
                    api_id=api_id,
                    api_hash=api_hash,
                    session_string=session_string,
                    kind="export"
                )

                temp2 = {"account_id": acc_id}
                user_states[user_id] = {"mode": "export_chat", "step": "chat_id", "temp": temp2}
                await event.reply(
                    f"✅ اکانت export `{name}` لاگین شد.\n"
                    "حالا chat_id گروهی که می‌خوای اعضاش رو بگیری بفرست:",
                    parse_mode="markdown"
                )

            except SessionPasswordNeededError:
                # لازم است رمز دو مرحله‌ای بخواهیم
                state["step"] = "2fa"
                state["temp"] = temp
                user_states[user_id] = state
                await event.reply(
                    "برای این اکانت export رمز دو مرحله‌ای (2FA) فعال است.\n"
                    "رمز دو مرحله‌ای این اکانت را همینجا بفرست:"
                )
            except PhoneCodeExpiredError:
                await event.reply("کد منقضی شده. دوباره دکمه 📤 خروج اعضا را بزن و از اول شروع کن.")
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
            # رمز دو مرحله‌ای برای اکانت export
            password = text
            name = temp["name"]
            phone = temp["phone"]
            api_id = temp["api_id"]
            api_hash = temp["api_hash"]

            exp_client = login_clients_export.get(user_id)
            if not exp_client:
                await event.reply("سشن لاگین export پیدا نشد. دوباره 📤 خروج اعضا را بزن.")
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
                    kind="export"
                )

                temp2 = {"account_id": acc_id}
                user_states[user_id] = {"mode": "export_chat", "step": "chat_id", "temp": temp2}
                await event.reply(
                    f"✅ اکانت export `{name}` (با 2FA) لاگین شد.\n"
                    "حالا chat_id گروهی که می‌خوای اعضاش رو بگیری بفرست:",
                    parse_mode="markdown"
                )

            except Exception as e:
                await event.reply(f"خطا در لاگین با رمز دو مرحله‌ای:\n{e}")
                traceback.print_exc()
                await exp_client.disconnect()
                login_clients_export.pop(user_id, None)
                user_states.pop(user_id, None)
            return

    # ---------- گرفتن اعضای گروه با اکانت export ----------
    if mode == "export_chat":
        if step == "chat_id":
            try:
                chat_id_val = int(text)
            except ValueError:
                await event.reply("chat_id باید عدد باشد. مثلاً -1001234567890")
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
                    await event.reply("این اکانت export دیگر لاگین نیست. مجدداً آن را بساز.")
                    await exp_client.disconnect()
                    user_states.pop(user_id, None)
                    return

                entity = await exp_client.get_entity(chat_id_val)
                participants = await exp_client.get_participants(entity, aggressive=True)

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

                await exp_client.disconnect()
                user_states.pop(user_id, None)
                await send_main_menu(chat_id, "خروج اعضا انجام شد. از منو ادامه بده:")

            except Exception as e:
                await event.reply(f"خطا در گرفتن اعضای گروه:\n{e}")
                traceback.print_exc()
            return

    # ---------- logout اکانت‌های export ----------
    if mode == "logout_export":
        if step == "choose":
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
                await event.reply(f"در حین logout این اکانت خطایی رخ داد (ولی ادامه می‌دهیم):\n{e}")

            delete_account_by_id(acc_id)

            user_states.pop(user_id, None)
            await event.reply(
                f"✅ از اکانت export `{acc['name']}` خارج شدی و از دیتابیس حذف شد.",
                parse_mode="markdown",
            )
            await send_main_menu(chat_id)
            return


# ------------------ main handler ------------------

@client.on(events.NewMessage)
async def main_handler(event):
    global awaiting_group_number, target_group, ACTIVE_ADD_ACCOUNT, INVITE_DELAY, ACCOUNTS_ADD, INVITE_DELAY_MODE

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
            await event.reply("ادمین قبلاً تعریف شده. فقط ادمین‌ها می‌توانند ادمین جدید اضافه کنند.")
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
                "/useacc <name> → انتخاب اکانت فعال برای دیدن گروه‌ها\n"
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
                parse_mode="markdown"
            )
        return

    # اگر ادمین نیست، ادامه نده
    if not is_admin(user_id):
        return

    # اگر فایل Document فرستاده شده (برای CSV add user)
    if event.document:
        file_name = (event.file.name or "").lower()

        if ".csv" in file_name:
            await event.reply("فایل CSV دریافت شد، در حال دانلود...")
            try:
                file_path = await client.download_media(event.document)
                await event.reply("فایل دانلود شد، شروع اد کردن اعضا با همه اکانت‌های add...")
                await add_users_from_csv_file(file_path, chat_id)
            except Exception as e:
                await event.reply(f"خطا در دانلود/پردازش فایل:\n{e}")
                traceback.print_exc()
        else:
            await event.reply("این فایل برای هیچ کاری استفاده نشد. فقط CSV برای add user قابل استفاده است.")
        return

    # ------------ دستورات ادمین ------------

    # لیست ادمین‌ها
    if text == "/admins":
        if not ADMINS:
            await event.reply("هیچ ادمینی ثبت نشده.")
        else:
            ids_text = "\n".join(str(a) for a in ADMINS)
            await event.reply(f"لیست ادمین‌ها (آی‌دی عددی):\n{ids_text}")
        return

    # اضافه کردن ادمین
    if text.startswith("/addadmin"):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await event.reply("فرمت درست: `/addadmin <user_id>`", parse_mode="markdown")
            return
        new_id = int(parts[1])
        add_admin_db(new_id)
        await event.reply(f"✅ ادمین جدید اضافه شد: `{new_id}`", parse_mode="markdown")
        return

    # حذف ادمین
    if text.startswith("/deladmin"):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await event.reply("فرمت درست: `/deladmin <user_id>`", parse_mode="markdown")
            return
        rem_id = int(parts[1])
        if rem_id in ADMINS:
            remove_admin_db(rem_id)
            await event.reply(f"✅ ادمین حذف شد: `{rem_id}`", parse_mode="markdown")
        else:
            await event.reply("این آی‌دی جزو ادمین‌ها نیست.")
        return

    # تنظیم تاخیر با دستور
    if text.startswith("/setdelay"):
        parts = text.split()
        if len(parts) == 2:
            arg = parts[1].strip().lower()
            if arg.isdigit():
                INVITE_DELAY = int(arg)
                if INVITE_DELAY < 1:
                    INVITE_DELAY = 1
                INVITE_DELAY_MODE = "fixed"
                set_setting("invite_delay", str(INVITE_DELAY))
                set_setting("invite_delay_mode", "fixed")
                await event.reply(f"✅ تاخیر بین ادها به صورت ثابت روی {INVITE_DELAY} ثانیه تنظیم شد.")
            elif arg in ("random", "rand"):
                INVITE_DELAY_MODE = "random"
                set_setting("invite_delay_mode", "random")
                await event.reply("✅ حالت تاخیر روی «رندوم بین 30 تا 100 ثانیه» تنظیم شد.")
            else:
                await event.reply("فرمت درست: `/setdelay <seconds>` یا `/setdelay random`", parse_mode="markdown")
        else:
            await event.reply("فرمت درست: `/setdelay <seconds>` یا `/setdelay random`", parse_mode="markdown")
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

    # ----------- مدیریت اکانت‌های add -----------

    if text == "/accounts" or text == "📜 اکانت‌ها":
        if not ACCOUNTS_ADD:
            await event.reply("هیچ اکانتی برای add user ثبت نشده.")
        else:
            lines = ["اکانت‌های add:\n"]
            for acc in ACCOUNTS_ADD:
                mark = "(active-for-groups)" if acc["name"] == ACTIVE_ADD_ACCOUNT else ""
                lines.append(f"- {acc['name']} {mark}  phone: {acc['phone']}")
            lines.append("\n⚠️ همه‌ی این اکانت‌ها در add از CSV استفاده می‌شوند.")
            await event.reply("\n".join(lines))
        return

    if text.startswith("/useacc"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await event.reply("فرمت درست: `/useacc <name>`", parse_mode="markdown")
            return
        name = parts[1].strip()
        acc = get_add_account_by_name(name)
        if not acc:
            await event.reply("اکانت با این نام وجود ندارد.")
            return
        ACTIVE_ADD_ACCOUNT = name
        set_setting("active_add_account", name)
        await event.reply(f"✅ اکانت فعال برای گرفتن لیست گروه‌ها تنظیم شد: {name}")
        return

    if text.startswith("/delacc"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await event.reply("فرمت درست: `/delacc <name>`", parse_mode="markdown")
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

    if text == "➕ افزودن اکانت":
        user_states[user_id] = {"mode": "addacc", "step": "name", "temp": {}}
        await event.reply("اسم دلخواه برای این اکانت add را بفرست (مثلاً main یا acc1):")
        return

    if text == "🗑 حذف اکانت add":
        if not ACCOUNTS_ADD:
            await event.reply("هیچ اکانتی برای حذف وجود ندارد.")
            return
        names = [{"id": a["id"], "name": a["name"]} for a in ACCOUNTS_ADD]
        temp = {"names": names}
        user_states[user_id] = {"mode": "delacc_wizard", "step": "choose", "temp": temp}

        lines = ["اکانت‌های add ثبت‌شده:"]
        for i, a in enumerate(names):
            lines.append(f"{i}: {a['name']}")
        lines.append("\nشماره اکانتی که می‌خوای حذف کنی رو بفرست:")

        await event.reply("\n".join(lines))
        return

    # ----------- مدیریت گروه‌ها برای add -----------

    if text == "🧾 گروه‌ها" or text == "/groups":
        if not ACTIVE_ADD_ACCOUNT:
            await event.reply(
                "هیچ اکانتی برای گرفتن لیست گروه‌ها فعال نیست.\n"
                "از /useacc <name> استفاده کن یا اکانت add جدید بساز."
            )
            return
        await event.reply(
            "در حال گرفتن لیست سوپرگروه‌ها با اکانت فعال (فقط برای انتخاب گروه هدف)..."
        )
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
        await event.reply(
            f"✅ گروه برای add user انتخاب شد:\n{target_group.title}\n"
            f"(ID: {target_group.id})\n\n"
            f"از این به بعد، هنگام ارسال CSV، همه اکانت‌های add روی این گروه کار می‌کنند."
        )
        return

    # ----------- خروج اعضا (export) -----------

    if text == "📤 خروج اعضا" or text == "/export":
        accounts = get_export_accounts()
        if not accounts:
            user_states[user_id] = {"mode": "export_login", "step": "name", "temp": {}}
            await event.reply(
                "هیچ اکانت exportی ثبت نشده.\n"
                "اول یک اکانت export بساز.\n"
                "اسم دلخواه برای این اکانت را بفرست (مثلاً exp1):"
            )
            return

        temp = {"accounts": accounts}
        user_states[user_id] = {"mode": "export_select", "step": "choose", "temp": temp}

        lines = ["اکانت‌های export موجود:"]
        for i, a in enumerate(accounts):
            lines.append(f"{i}: {a['name']}  phone: {a['phone']}")
        lines.append('\nیک عدد برای انتخاب اکانت بفرست، یا عبارت "new" برای ساخت اکانت جدید:')

        await event.reply("\n".join(lines))
        return

    # ----------- logout اکانت‌های export -----------

    if text == "🚪 خروج اکانت‌های export":
        accounts = get_export_accounts()
        if not accounts:
            await event.reply("هیچ اکانت exportی برای logout وجود ندارد.")
            return

        temp = {"accounts": accounts}
        user_states[user_id] = {"mode": "logout_export", "step": "choose", "temp": temp}

        lines = ["اکانت‌های export:"]
        for i, a in enumerate(accounts):
            lines.append(f"{i}: {a['name']}  phone: {a['phone']}")
        lines.append("\nشماره اکانتی که می‌خوای logout و حذف کنی رو بفرست:")

        await event.reply("\n".join(lines))
        return

    # ----------- ادامه‌ی ویزاردها اگر در state هستیم -----------

    if user_id in user_states and not text.startswith("/"):
        await handle_state_message(event, user_states[user_id])
        return

    # ----------- سایر موارد -----------

    if text:
        await event.reply("دستور نامعتبر.\nاز /start یا منوی دکمه‌ای استفاده کن.")
        return


# ------------------ main (async) ------------------

async def run_bot():
    """
    این تابع ربات تلگرام رو روی event loop اجرا می‌کند
    (برای استفاده در web.py داخل aiohttp)
    """
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
    """
    برای وقتی که بخوای مستقیماً `python bot.py` اجرا کنی.
    """
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
