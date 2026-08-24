import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaVideo
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# Configuration
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()

LEGACY_TARGET_GROUP_ID = os.getenv("TARGET_GROUP_ID", "").strip()

COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()
if COOKIES_FILE:
    COOKIES_FILE = str(Path(COOKIES_FILE).expanduser())

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "bot.db"))
STATE_FILE = Path(os.getenv("STATE_FILE", "bot_state.json"))

TELEGRAM_LIMIT_BYTES = 50 * 1024 * 1024
SAFE_MAX_BYTES = 48 * 1024 * 1024
COMPRESSION_TARGET_BYTES = 47 * 1024 * 1024

MAX_CONCURRENT_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "1")))
JOB_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

# Album settings requested by the owner.
ALBUM_SIZE = 3
# Album waits until 3 videos exist in the same topic. No timeout flush.
ALBUM_FLUSH_SECONDS = 0

# Set PROTECT_CONTENT=1 to prevent forwarding/saving of bot-sent media.
# This does NOT make a group message invisible to selected members; Telegram does
# not provide per-member message visibility for ordinary forum topics.
PROTECT_CONTENT = os.getenv("PROTECT_CONTENT", "0").strip().lower() in {
    "1", "true", "yes", "on"
}

URL_REGEX = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)

GITHUB_TOKEN = os.getenv("GH_TOKEN", "").strip()
GITHUB_REPO = os.getenv("REPO_NAME", "").strip()
GITHUB_BRANCH = os.getenv("GITHUB_REF_NAME", "main").strip() or "main"
GITHUB_STATE_PATH = os.getenv("GITHUB_STATE_PATH", "bot_state.json").strip()

HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =========================================================
# Runtime album queue
# =========================================================

# queue key = "chat_id:thread_id"
ALBUM_QUEUES: dict[str, list[dict]] = {}
ALBUM_TASKS: dict[str, asyncio.Task] = {}
ALBUM_LOCK = asyncio.Lock()


# =========================================================
# Generic helpers
# =========================================================


def ffmpeg_exists() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_exists() -> bool:
    return shutil.which("ffprobe") is not None


def human_size(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def normalize_url(url: str) -> str:
    if not url:
        return url
    return url.strip().rstrip(".,!?;:)]}\"'")


def canonicalize_url(url: str) -> str:
    """Normalize common sharing/tracking parameters without destroying useful IDs."""
    url = normalize_url(url)
    try:
        parsed = urllib.parse.urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return url

        tracking = {
            "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "fbclid", "igsh", "igshid", "share_id", "share_ref", "ref_src",
            "si", "feature", "spm", "mc_cid", "mc_eid",
        }
        query = [
            (k, v)
            for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in tracking and not k.lower().startswith("utm_")
        ]
        return urllib.parse.urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                urllib.parse.urlencode(query),
                "",
            )
        )
    except Exception:
        return url


def cleanup_download_files(prefix: str) -> None:
    for path in DOWNLOAD_DIR.glob(f"{prefix}*"):
        if not path.is_file():
            continue
        try:
            path.unlink()
        except Exception as exc:
            logger.warning("Could not delete %s: %s", path, exc)


def safe_filename(value: str, fallback: str = "video") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "")
    return value[:100] or fallback


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# =========================================================
# Local + GitHub persistent state
# =========================================================


def db_connect():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, sql_type: str) -> None:
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def init_database() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT UNIQUE NOT NULL,
                title TEXT,
                registered_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT,
                video_id TEXT,
                url TEXT,
                file_hash TEXT,
                title TEXT,
                downloaded_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_downloads (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                url TEXT NOT NULL,
                platform TEXT,
                video_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_access (
                user_id TEXT PRIMARY KEY,
                unlocked_at TEXT NOT NULL
            )
            """
        )

        # New columns are added without destroying the user's existing database.
        _ensure_column(conn, "pending_downloads", "topic_thread_id", "INTEGER")
        _ensure_column(conn, "pending_downloads", "topic_name", "TEXT")

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_platform_video_id
            ON videos(platform, video_id)
            WHERE platform IS NOT NULL AND platform != ''
              AND video_id IS NOT NULL AND video_id != ''
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_url ON videos(url)")

        cutoff = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM pending_downloads WHERE created_at < ?", (cutoff,))
        conn.commit()


def _read_local_state() -> dict:
    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
            return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_local_state(data: dict) -> None:
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(STATE_FILE)


def _github_request(method: str, url: str, payload: Optional[dict] = None):
    if not GITHUB_TOKEN:
        return None

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "personal-telegram-video-bot",
    }
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        logger.warning("GitHub API %s %s failed: %s", method, url, exc)
    except Exception as exc:
        logger.warning("GitHub API error: %s", exc)
    return None


def _github_get_state() -> tuple[dict, Optional[str]]:
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return {}, None

    api_url = (
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/"
        f"{urllib.parse.quote(GITHUB_STATE_PATH, safe='/')}"
        f"?ref={urllib.parse.quote(GITHUB_BRANCH)}"
    )
    result = _github_request("GET", api_url)
    if not result or "content" not in result:
        return {}, None

    try:
        content = base64.b64decode(result["content"]).decode("utf-8")
        data = json.loads(content)
        return (data if isinstance(data, dict) else {}), result.get("sha")
    except Exception as exc:
        logger.warning("Could not decode GitHub state: %s", exc)
        return {}, result.get("sha")


def _github_save_state(data: dict, sha: Optional[str], message: str) -> bool:
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return False

    encoded = base64.b64encode(
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("ascii")
    api_url = (
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/"
        f"{urllib.parse.quote(GITHUB_STATE_PATH, safe='/')}"
    )
    payload = {
        "message": message,
        "content": encoded,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    result = _github_request("PUT", api_url, payload)
    return bool(result and result.get("content"))


def _get_combined_state() -> dict:
    local = _read_local_state()
    remote, _ = _github_get_state()
    if remote:
        merged = {**local, **remote}
        return merged
    return local


def save_state(data: dict, message: str = "Update bot state") -> bool:
    data = dict(data)
    data["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    _write_local_state(data)

    if GITHUB_TOKEN and GITHUB_REPO:
        remote, sha = _github_get_state()
        merged = {**remote, **data}
        if not _github_save_state(merged, sha, message):
            logger.warning("State saved locally but GitHub persistence failed")
            return False
    return True


def get_target_group_id() -> Optional[int]:
    state = _get_combined_state()
    value = state.get("target_group_id")
    if value:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass

    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key='target_group_id' LIMIT 1"
            ).fetchone()
            if row and row[0]:
                return int(row[0])
    except Exception as exc:
        logger.warning("Could not read target group: %s", exc)

    if LEGACY_TARGET_GROUP_ID:
        try:
            return int(LEGACY_TARGET_GROUP_ID)
        except ValueError:
            pass
    return None


def set_target_group_id(group_id: int, title: str = "") -> bool:
    try:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES('target_group_id', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(group_id),),
            )
            if title:
                conn.execute(
                    """
                    INSERT INTO settings(key, value) VALUES('target_group_title', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (title,),
                )
            conn.commit()
    except Exception as exc:
        logger.exception("Could not save target group locally: %s", exc)
        return False

    state = _get_combined_state()
    state["target_group_id"] = int(group_id)
    if title:
        state["target_group_title"] = title
    save_state(state, "Update Telegram target group")
    return True


def register_group(group_id: int, title: str = "") -> bool:
    try:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO groups(group_id, title, registered_at)
                VALUES (?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    title=excluded.title,
                    registered_at=excluded.registered_at
                """,
                (str(group_id), title, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
    except Exception as exc:
        logger.exception("Could not register group: %s", exc)
        return False

    state = _get_combined_state()
    groups = {
        str(item.get("group_id")): str(item.get("title") or "")
        for item in state.get("groups", [])
        if isinstance(item, dict) and item.get("group_id") is not None
    }
    groups[str(group_id)] = title or ""
    state["groups"] = [
        {
            "group_id": int(gid) if str(gid).lstrip("-").isdigit() else gid,
            "title": name,
        }
        for gid, name in groups.items()
    ]
    save_state(state, "Register Telegram target group")
    return True


def get_registered_groups() -> list[tuple[str, str]]:
    merged: dict[str, str] = {}
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT group_id, title FROM groups ORDER BY registered_at DESC"
            ).fetchall()
            for group_id, title in rows:
                merged[str(group_id)] = title or ""
    except Exception as exc:
        logger.warning("Could not read local groups: %s", exc)

    state = _get_combined_state()
    for item in state.get("groups", []):
        if isinstance(item, dict) and item.get("group_id") is not None:
            merged[str(item["group_id"])] = str(item.get("title") or "")
    return list(merged.items())


# =========================================================
# Topics
# =========================================================


def get_topics_for_group(group_id: Optional[int] = None) -> dict:
    group_id = group_id or get_target_group_id()
    if not group_id:
        return {}
    state = _get_combined_state()
    raw = state.get("topics", {})
    if not isinstance(raw, dict):
        return {}
    values = raw.get(str(group_id), {})
    if not isinstance(values, dict):
        return {}
    return {str(k): v for k, v in values.items() if isinstance(v, dict)}


def register_topic(group_id: int, thread_id: int, name: str) -> bool:
    state = _get_combined_state()
    all_topics = state.get("topics", {})
    if not isinstance(all_topics, dict):
        all_topics = {}
    group_topics = all_topics.get(str(group_id), {})
    if not isinstance(group_topics, dict):
        group_topics = {}

    group_topics[str(thread_id)] = {
        "thread_id": int(thread_id),
        "name": name.strip()[:80] or f"موضوع {thread_id}",
    }
    all_topics[str(group_id)] = group_topics
    state["topics"] = all_topics
    state.setdefault("last_topic", {})
    save_state(state, "Register Telegram forum topic")
    return True


def set_last_topic(group_id: int, thread_id: int) -> None:
    state = _get_combined_state()
    state.setdefault("last_topic", {})
    state["last_topic"][str(group_id)] = int(thread_id)
    save_state(state, "Update last selected Telegram topic")


def get_last_topic(group_id: Optional[int] = None) -> Optional[int]:
    group_id = group_id or get_target_group_id()
    if not group_id:
        return None
    state = _get_combined_state()
    last = state.get("last_topic", {})
    try:
        value = last.get(str(group_id))
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def get_topic_name(group_id: int, thread_id: int) -> str:
    topics = get_topics_for_group(group_id)
    item = topics.get(str(thread_id))
    if isinstance(item, dict):
        return str(item.get("name") or f"موضوع {thread_id}")
    return f"موضوع {thread_id}"


# =========================================================
# Owner-only administration
# =========================================================

async def is_owner(update: Update) -> bool:
    return bool(
        update.effective_user
        and ADMIN_USER_ID
        and str(update.effective_user.id) == str(ADMIN_USER_ID)
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not await is_owner(update):
        await update.message.reply_text("⛔ هذا البوت خاص بالمالك فقط.")
        return

    current = get_target_group_id()
    groups = get_registered_groups()
    keyboard = []
    for group_id, title in groups:
        try:
            numeric_id = int(group_id)
        except ValueError:
            continue
        prefix = "✅ " if current == numeric_id else "📍 "
        keyboard.append([
            InlineKeyboardButton(
                f"{prefix}{title or ('جروب ' + group_id)}",
                callback_data=f"select_group:{numeric_id}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton("➕ تسجيل الجروب الحالي", callback_data="register_current_group")
    ])
    keyboard.append([
        InlineKeyboardButton("📂 عرض الـTopics", callback_data="show_topics")
    ])
    keyboard.append([
        InlineKeyboardButton("🔄 تحديث", callback_data="refresh_admin")
    ])

    await update.message.reply_text(
        "⚙️ لوحة البوت الشخصية\n\n"
        f"📤 جروب الاستقبال الحالي: `{current or 'غير محدد'}`\n\n"
        "اختر الجروب أو افتح قائمة الـTopics.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def set_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not await is_owner(update):
        await update.message.reply_text("⛔ هذا الأمر للمالك فقط.")
        return

    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ استخدم /setgroup داخل الجروب المطلوب.")
        return

    if not register_group(chat.id, chat.title or ""):
        await update.message.reply_text("❌ تعذر تسجيل الجروب.")
        return
    if set_target_group_id(chat.id, chat.title or ""):
        await update.message.reply_text(
            "✅ تم تعيين هذا الجروب كجروب الاستقبال وحفظ الاختيار.\n\n"
            f"🏷️ {chat.title or 'بدون اسم'}\n"
            f"🆔 `{chat.id}`",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("❌ تعذر حفظ جروب الاستقبال.")


async def set_topic_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not await is_owner(update):
        return

    chat = update.effective_chat
    message = update.effective_message
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ استخدم /settopic داخل Topic في الجروب المطلوب.")
        return

    thread_id = getattr(message, "message_thread_id", None)
    if not thread_id:
        await update.message.reply_text(
            "❌ شغّل الأمر داخل موضوع (Topic) وليس داخل General."
        )
        return

    raw_name = " ".join(context.args).strip()
    if not raw_name:
        await update.message.reply_text(
            "اكتب اسم الموضوع، مثال:\n`/settopic 🎵 أغاني`",
            parse_mode="Markdown",
        )
        return

    # The group where /settopic is executed is the real receiving group.
    if not register_group(chat.id, chat.title or ""):
        await update.message.reply_text("❌ تعذر تسجيل الجروب."); return
    if not set_target_group_id(chat.id, chat.title or ""):
        await update.message.reply_text("❌ تعذر تعيين هذا الجروب كجروب الاستقبال."); return
    if not register_topic(chat.id, int(thread_id), raw_name):
        await update.message.reply_text("❌ تعذر تسجيل الـTopic."); return
    set_last_topic(chat.id, int(thread_id))

    await update.message.reply_text(
        "✅ تم تسجيل الـTopic بنجاح.\n\n"
        f"📂 {raw_name}\n"
        f"🆔 Thread ID: `{thread_id}`",
        parse_mode="Markdown",
    )


async def topics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not await is_owner(update):
        return

    group_id = get_target_group_id()
    if not group_id:
        await update.message.reply_text("❌ لم يتم تحديد جروب الاستقبال بعد.")
        return

    topics = get_topics_for_group(group_id)
    if not topics:
        await update.message.reply_text(
            "📂 لا توجد Topics مسجلة بعد.\n\n"
            "ادخل كل Topic واكتب:\n"
            "`/settopic اسم الموضوع`",
            parse_mode="Markdown",
        )
        return

    last = get_last_topic(group_id)
    keyboard = []
    for key, item in sorted(
        topics.items(),
        key=lambda kv: str(kv[1].get("name", "")),
    ):
        tid = int(key)
        name = str(item.get("name") or key)
        prefix = "✅ " if tid == last else "📂 "
        keyboard.append([
            InlineKeyboardButton(
                f"{prefix}{name}",
                callback_data=f"topic_default:{tid}",
            )
        ])

    await update.message.reply_text(
        "📂 Topics المسجلة:\n\n"
        "اضغط Topic لجعله الاختيار الافتراضي للفيديوهات القادمة.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# Duplicate protection
# =========================================================


def get_video_key(info: Optional[dict], url: str) -> tuple[str, str]:
    if not info:
        return "", canonicalize_url(url)
    platform = str(
        info.get("extractor_key") or info.get("extractor") or ""
    ).strip().lower()
    video_id = str(info.get("id") or "").strip()
    return platform, video_id or canonicalize_url(url)


def is_video_downloaded(platform: str, video_id: str, url: str) -> bool:
    try:
        with db_connect() as conn:
            if platform and video_id:
                row = conn.execute(
                    "SELECT 1 FROM videos WHERE platform=? AND video_id=? LIMIT 1",
                    (platform, video_id),
                ).fetchone()
                if row:
                    return True

            canonical = canonicalize_url(url)
            row = conn.execute(
                "SELECT 1 FROM videos WHERE url=? LIMIT 1",
                (canonical,),
            ).fetchone()
            return bool(row)
    except Exception as exc:
        logger.warning("Duplicate check failed: %s", exc)
        return False


def save_downloaded_video(
    platform: str,
    video_id: str,
    url: str,
    title: str = "",
    file_hash: str = "",
) -> bool:
    try:
        canonical = canonicalize_url(url)
        with db_connect() as conn:
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT OR IGNORE INTO videos
                (platform, video_id, url, file_hash, title, downloaded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (platform, video_id, canonical, file_hash, title, now),
            )
            conn.execute(
                """
                UPDATE videos
                SET url=?, file_hash=?, title=?, downloaded_at=?
                WHERE platform=? AND video_id=?
                """,
                (canonical, file_hash, title, now, platform, video_id),
            )
            conn.commit()
        return True
    except Exception as exc:
        logger.warning("Could not save downloaded video: %s", exc)
        return False


def create_pending_download(
    user_id: int,
    chat_id: int,
    message_id: int,
    url: str,
    platform: str,
    video_id: str,
    topic_thread_id: Optional[int],
    topic_name: str,
) -> str:
    token = hashlib.sha256(
        f"{user_id}:{chat_id}:{message_id}:{url}:{datetime.now().timestamp()}".encode()
    ).hexdigest()[:24]
    try:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_downloads
                (token, user_id, chat_id, message_id, url, platform, video_id, created_at,
                 topic_thread_id, topic_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    str(user_id),
                    str(chat_id),
                    str(message_id),
                    canonicalize_url(url),
                    platform,
                    video_id,
                    datetime.now().isoformat(timespec="seconds"),
                    topic_thread_id,
                    topic_name,
                ),
            )
            conn.commit()
        return token
    except Exception as exc:
        logger.warning("Could not create pending download: %s", exc)
        return ""


def get_pending_download(token: str) -> Optional[dict]:
    try:
        with db_connect() as conn:
            row = conn.execute(
                """
                SELECT user_id, chat_id, message_id, url, platform, video_id,
                       topic_thread_id, topic_name
                FROM pending_downloads
                WHERE token=?
                """,
                (token,),
            ).fetchone()
        if not row:
            return None
        return {
            "user_id": int(row[0]),
            "chat_id": int(row[1]),
            "message_id": int(row[2]),
            "url": row[3],
            "platform": row[4] or "",
            "video_id": row[5] or "",
            "topic_thread_id": int(row[6]) if row[6] is not None else None,
            "topic_name": row[7] or "",
        }
    except Exception as exc:
        logger.warning("Could not read pending download: %s", exc)
        return None


def delete_pending_download(token: str) -> None:
    try:
        with db_connect() as conn:
            conn.execute("DELETE FROM pending_downloads WHERE token=?", (token,))
            conn.commit()
    except Exception:
        pass


# =========================================================
# URL/site helpers
# =========================================================



def resolve_social_share_url(url: str) -> str:
    """Resolve Facebook/Instagram share links before yt-dlp."""
    url = canonicalize_url(url)
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not any(x in host for x in ["facebook.com", "fb.watch", "instagram.com"]):
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": HTTP_USER_AGENT})
        with urllib.request.urlopen(req, timeout=12) as r:
            final = r.geturl()
            if final:
                return canonicalize_url(final)
    except Exception as exc:
        logger.info("Share resolve failed %s: %s", url, exc)
    return url

def extract_urls(text: str) -> list[str]:
    """Extract every URL from a message, preserving message order."""
    if not text:
        return []

    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_REGEX.finditer(text):
        url = resolve_social_share_url(normalize_url(match.group(0)))
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def extract_url(text: str) -> Optional[str]:
    """Backward-compatible helper: return the first URL only."""
    urls = extract_urls(text)
    return urls[0] if urls else None


def get_site_name(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    known = {
        "tiktok.com": "TikTok",
        "vt.tiktok.com": "TikTok",
        "youtube.com": "YouTube",
        "youtu.be": "YouTube",
        "instagram.com": "Instagram",
        "facebook.com": "Facebook",
        "fb.watch": "Facebook",
        "twitter.com": "X / Twitter",
        "x.com": "X / Twitter",
        "reddit.com": "Reddit",
        "pinterest.com": "Pinterest",
        "vimeo.com": "Vimeo",
        "dailymotion.com": "Dailymotion",
        "twitch.tv": "Twitch",
        "snapchat.com": "Snapchat",
    }
    for domain, name in known.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host or "الموقع"


# =========================================================
# yt-dlp engine
# =========================================================


def get_common_ydl_options() -> dict:
    options = {
        "quiet": True,
        "retries": 5,
        "fragment_retries": 5,
        "no_warnings": False,
        "noplaylist": True,
        "source_address": "0.0.0.0",
        "retries": 5,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "http_headers": {
            "User-Agent": HTTP_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
        },
    }

    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        options["cookiefile"] = COOKIES_FILE

    # Deno is installed by the workflow for sites that require JavaScript execution.
    if shutil.which("deno"):
        options["js_runtimes"] = {"deno": {}}

    # curl-cffi is supplied by yt-dlp[default,curl-cffi] in the current project.
    # Use browser impersonation when the runtime supports it.
    if os.getenv("YTDLP_IMPERSONATE", "1").strip().lower() not in {"0", "false", "no"}:
        options["impersonate"] = "chrome"

    return options


def extract_info(url: str) -> Optional[dict]:
    url = normalize_url(url)
    attempts = []
    base = get_common_ydl_options()
    attempts.append({**base, "skip_download": True})
    attempts.append({**base, "skip_download": True, "geo_bypass": True})

    # A second pass without impersonation helps when a site's extractor rejects
    # browser emulation for a particular endpoint.
    plain = dict(base)
    plain.pop("impersonate", None)
    attempts.append({**plain, "skip_download": True})

    last_error = None
    for index, options in enumerate(attempts, 1):
        try:
            logger.info("Extracting info attempt %s: %s", index, url)
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    return info
        except Exception as exc:
            last_error = exc
            logger.warning("Info extraction attempt %s failed: %s", index, exc)

    logger.error("All yt-dlp info attempts failed: %s", last_error)
    return None


def _estimate_format_size(fmt: dict, duration: Optional[float]) -> Optional[int]:
    for key in ("filesize", "filesize_approx"):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    tbr = fmt.get("tbr")
    if isinstance(tbr, (int, float)) and tbr > 0 and duration and duration > 0:
        return int((float(tbr) * 1000 / 8) * duration * 1.10)
    return None


def _format_score(fmt: dict) -> tuple:
    height = int(fmt.get("height") or 0)
    fps = float(fmt.get("fps") or 0)
    vcodec = str(fmt.get("vcodec") or "")
    acodec = str(fmt.get("acodec") or "")
    ext = str(fmt.get("ext") or "")
    codec_bonus = 2 if any(x in vcodec.lower() for x in ("avc", "h264")) else 0
    audio_bonus = 2 if any(x in acodec.lower() for x in ("mp4a", "aac")) else 0
    ext_bonus = 2 if ext == "mp4" else 0
    return height, fps, codec_bonus + audio_bonus + ext_bonus


def choose_best_format(info: dict, target_bytes: int = SAFE_MAX_BYTES) -> tuple[Optional[str], dict]:
    all_formats = info.get("formats") or []
    duration = info.get("duration")

    progressive = [
        f for f in all_formats
        if f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
    ]
    video_only = [
        f for f in all_formats
        if f.get("vcodec") not in (None, "none")
        and f.get("acodec") in (None, "none")
    ]
    audio_only = [
        f for f in all_formats
        if f.get("vcodec") in (None, "none")
        and f.get("acodec") not in (None, "none")
    ]

    candidates: list[tuple[dict, Optional[int], bool]] = []
    for fmt in progressive:
        size = _estimate_format_size(fmt, duration)
        if size is not None:
            candidates.append((fmt, size, False))

    best_audio = None
    if audio_only:
        known_audio = [
            (f, _estimate_format_size(f, duration))
            for f in audio_only
        ]
        known_audio = [pair for pair in known_audio if pair[1] is not None]
        if known_audio:
            # Choose the smallest reliable audio stream that still has audio.
            best_audio = min(known_audio, key=lambda pair: pair[1])[0]

    if best_audio:
        audio_size = _estimate_format_size(best_audio, duration) or 0
        for fmt in video_only:
            vsize = _estimate_format_size(fmt, duration)
            if vsize is not None:
                candidates.append((fmt, vsize + audio_size, True))

    fitting = [item for item in candidates if item[1] is not None and item[1] <= target_bytes]
    if fitting:
        fitting.sort(key=lambda item: _format_score(item[0]), reverse=True)
        fmt, size, separate = fitting[0]
        expression = (
            f"{fmt['format_id']}+bestaudio/best"
            if separate else fmt["format_id"]
        )
        return expression, {
            "height": fmt.get("height"),
            "estimated_size": size,
            "format_id": fmt.get("format_id"),
            "separate": separate,
            "direct_fit": True,
        }

    # No reliable known size fits: choose the best video and let compression handle it.
    possible = [f for f in progressive + video_only if f.get("height")]
    if possible:
        possible.sort(key=_format_score, reverse=True)
        chosen = possible[0]
        separate = chosen.get("acodec") in (None, "none")
        expression = (
            f"{chosen['format_id']}+bestaudio/best"
            if separate else chosen["format_id"]
        )
        return expression, {
            "height": chosen.get("height"),
            "estimated_size": _estimate_format_size(chosen, duration),
            "format_id": chosen.get("format_id"),
            "separate": separate,
            "direct_fit": False,
        }

    return None, {}


def _find_downloaded_file(info: dict, ydl: yt_dlp.YoutubeDL) -> Optional[str]:
    for item in info.get("requested_downloads") or []:
        path = item.get("filepath")
        if path and os.path.isfile(path):
            return path

    prepared = ydl.prepare_filename(info)
    stem = os.path.splitext(prepared)[0]
    for candidate in (
        prepared,
        stem + ".mp4",
        stem + ".mkv",
        stem + ".webm",
        stem + ".mov",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def download_with_yt_dlp(
    url: str,
    output_template: str,
    preloaded_info: Optional[dict] = None,
) -> tuple[Optional[str], Optional[dict], dict]:
    info = preloaded_info or extract_info(url)
    if not info:
        return None, None, {}

    expression, selection = choose_best_format(info, SAFE_MAX_BYTES)
    if not expression:
        expression = "bestvideo+bestaudio/best"
        selection = {"fallback": True}

    logger.info("Selected format: %s | %s", expression, selection)

    options = {
        **get_common_ydl_options(),
        "format": expression,
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "writethumbnail": False,
        "writeinfojson": False,
        "noplaylist": True,
    }

    attempts = [options]
    plain = dict(options)
    plain.pop("impersonate", None)
    attempts.append(plain)

    last_error = None
    for index, attempt_options in enumerate(attempts, 1):
        try:
            with yt_dlp.YoutubeDL(attempt_options) as ydl:
                downloaded_info = ydl.extract_info(url, download=True)
                path = _find_downloaded_file(downloaded_info, ydl) if downloaded_info else None
                if path:
                    return path, downloaded_info, selection
        except Exception as exc:
            last_error = exc
            logger.warning("yt-dlp download attempt %s failed: %s", index, exc)

    logger.warning("yt-dlp download failed after retries: %s", last_error)
    return None, info, selection


# =========================================================
# Direct video URL fallback
# =========================================================


def _looks_like_video_url(url: str, content_type: str = "") -> bool:
    if content_type.lower().split(";", 1)[0].startswith("video/"):
        return True
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith((".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".flv", ".ts"))


def download_direct_video(url: str, output_base: Path) -> Optional[str]:
    request = urllib.request.Request(
        canonicalize_url(url),
        headers={"User-Agent": HTTP_USER_AGENT, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "")
            if not _looks_like_video_url(final_url, content_type):
                return None

            total = response.headers.get("Content-Length")
            if total:
                try:
                    if int(total) > 2 * 1024 * 1024 * 1024:
                        return None
                except ValueError:
                    pass

            suffix = Path(urllib.parse.urlparse(final_url).path).suffix or ".mp4"
            target = output_base.with_suffix(suffix)
            with target.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            return str(target) if target.exists() and target.stat().st_size else None
    except Exception as exc:
        logger.warning("Direct video download failed: %s", exc)
        return None


# =========================================================
# FFmpeg / compression
# =========================================================


def get_video_metadata(path: str) -> dict:
    if not ffprobe_exists():
        return {}
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration:stream=width,height",
                "-of", "json", path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {}
        return json.loads(result.stdout)
    except Exception:
        return {}


def get_video_duration(path: str) -> Optional[float]:
    data = get_video_metadata(path)
    try:
        duration = float(data.get("format", {}).get("duration"))
        return duration if duration > 0 else None
    except (TypeError, ValueError):
        return None


def compress_video_to_limit(input_path: str) -> Optional[str]:
    if not ffmpeg_exists() or not os.path.isfile(input_path):
        return None
    if os.path.getsize(input_path) <= SAFE_MAX_BYTES:
        return input_path

    duration = get_video_duration(input_path)
    if not duration:
        return None

    source = Path(input_path)
    final_path = source.with_name(source.stem + "_telegram.mp4")
    targets = [
        COMPRESSION_TARGET_BYTES,
        46 * 1024 * 1024,
        44 * 1024 * 1024,
        42 * 1024 * 1024,
    ]

    for target_bytes in targets:
        audio_bps = 96_000
        target_bits = target_bytes * 8
        video_bps = max(int(target_bits / duration - audio_bps), 120_000)
        bitrate_k = max(video_bps // 1000, 120)

        try:
            if final_path.exists():
                final_path.unlink()
        except Exception:
            pass

        vf = "scale='min(720,iw)':-2:force_original_aspect_ratio=decrease"
        command = [
            "ffmpeg", "-y", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{bitrate_k}k", "-maxrate", f"{bitrate_k}k",
            "-bufsize", f"{bitrate_k * 2}k",
            "-c:a", "aac", "-b:a", "96k",
            "-vf", vf,
            "-movflags", "+faststart",
            "-sn", "-dn",
            str(final_path),
        ]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=1800,
            )
        except subprocess.TimeoutExpired:
            logger.warning("FFmpeg timed out")
            continue

        if result.returncode != 0 or not final_path.exists():
            logger.warning("FFmpeg failed: %s", result.stderr[-1200:])
            continue

        size = final_path.stat().st_size
        if size <= SAFE_MAX_BYTES:
            return str(final_path)

    return None


# =========================================================
# Error messages
# =========================================================


def get_download_error_message(site_name: str) -> str:
    cookies = (
        "✅ Cookies configured. لو استمر الفشل فالجلسة قد تكون منتهية."
        if COOKIES_FILE and Path(COOKIES_FILE).exists()
        else "ℹ️ المحتوى المقيد قد يحتاج COOKIES_B64 / cookies.txt صالح."
    )
    return (
        f"❌ لم أستطع استخراج فيديو {site_name}.\n\n"
        "الأسباب المحتملة:\n"
        "• الرابط خاص أو يحتاج تسجيل دخول.\n"
        "• الموقع رفض طلب السيرفر.\n"
        "• الرابط منتهي أو يعيد التوجيه إلى محتوى غير متاح.\n"
        "• طريقة الاستخراج تغيرت لدى الموقع.\n\n"
        f"{cookies}"
    )


# =========================================================
# Topic selection UI
# =========================================================


def topic_keyboard(group_id: int) -> InlineKeyboardMarkup:
    topics = get_topics_for_group(group_id)
    last = get_last_topic(group_id)
    rows: list[list[InlineKeyboardButton]] = []
    pending: list[InlineKeyboardButton] = []

    for key, item in sorted(
        topics.items(),
        key=lambda kv: str(kv[1].get("name", "")),
    ):
        try:
            tid = int(key)
        except (TypeError, ValueError):
            continue
        name = str(item.get("name") or f"موضوع {tid}")
        mark = "✅ " if last == tid else ""
        pending.append(
            InlineKeyboardButton(
                f"{mark}{name}",
                callback_data=f"topic_select:{tid}",
            )
        )
        if len(pending) == 2:
            rows.append(pending)
            pending = []
    if pending:
        rows.append(pending)

    rows.append([
        InlineKeyboardButton("⚙️ إدارة الـTopics", callback_data="show_topics")
    ])
    return InlineKeyboardMarkup(rows)


async def show_topic_picker(query_or_message, group_id: int, edit: bool = False):
    topics = get_topics_for_group(group_id)

    async def _edit_message(text: str, reply_markup=None):
        if hasattr(query_or_message, "edit_message_text"):
            await query_or_message.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        else:
            await query_or_message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)

    if not topics:
        text = (
            "❌ لا توجد Topics مسجلة للبوت بعد.\n\n"
            "ادخل كل Topic واكتب داخله:\n"
            "`/settopic اسم الموضوع`\n\n"
            "مثال:\n"
            "`/settopic 🎵 أغاني`"
        )
        if edit:
            await _edit_message(text)
        else:
            await query_or_message.reply_text(text, parse_mode="Markdown")
        return

    text = "📂 اختر المكان الذي تريد حفظ الفيديو فيه:"
    if edit:
        await _edit_message(text, reply_markup=topic_keyboard(group_id))
    else:
        await query_or_message.reply_text(text, reply_markup=topic_keyboard(group_id))


# =========================================================
# Album queue
# =========================================================


def _album_key(chat_id: int, thread_id: Optional[int]) -> str:
    return f"{chat_id}:{thread_id or 0}"


async def _send_album(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    thread_id: Optional[int],
    topic_name: str,
    items: list[dict],
) -> bool:
    """Send the queued items as one Telegram album. Return True only on album success."""
    if not items:
        return False

    # Telegram media groups contain 2-10 items; the project uses 4.
    if len(items) == 1:
        item = items[0]
        path = item["path"]
        try:
            with open(path, "rb") as handle:
                await context.bot.send_video(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    video=handle,
                    caption=item.get("caption", ""),
                    supports_streaming=True,
                    protect_content=PROTECT_CONTENT,
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=30,
                    pool_timeout=30,
                )
        except Exception:
            try:
                with open(path, "rb") as handle:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        document=handle,
                        caption=item.get("caption", ""),
                        protect_content=PROTECT_CONTENT,
                        read_timeout=180,
                        write_timeout=180,
                        connect_timeout=30,
                        pool_timeout=30,
                    )
            except Exception:
                logger.exception("Single-item album fallback failed")
        finally:
            cleanup_download_files(item.get("cleanup_prefix", ""))
        return False

    handles = []
    media = []
    try:
        for index, item in enumerate(items):
            handle = open(item["path"], "rb")
            handles.append(handle)
            media.append(
                InputMediaVideo(
                    media=handle,
                    caption=item.get("caption", "") if index == 0 else None,
                    supports_streaming=True,
                )
            )

        await context.bot.send_media_group(
            chat_id=chat_id,
            message_thread_id=thread_id,
            media=media,
            protect_content=PROTECT_CONTENT,
            read_timeout=180,
            write_timeout=180,
            connect_timeout=30,
            pool_timeout=30,
        )
        return True
    except Exception as exc:
        logger.warning("send_media_group failed in topic %s: %s", topic_name, exc)
        # Fallback: keep the videos usable even if album sending is rejected.
        for item in items:
            try:
                with open(item["path"], "rb") as handle:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        video=handle,
                        caption=item.get("caption", ""),
                        supports_streaming=True,
                        protect_content=PROTECT_CONTENT,
                        read_timeout=180,
                        write_timeout=180,
                        connect_timeout=30,
                        pool_timeout=30,
                    )
            except Exception:
                logger.exception("Album individual fallback failed")
    finally:
        for handle in handles:
            try:
                handle.close()
            except Exception:
                pass
        for item in items:
            cleanup_download_files(item.get("cleanup_prefix", ""))
        return False


async def _flush_album_after_delay(
    context: ContextTypes.DEFAULT_TYPE,
    key: str,
):
    try:
        await asyncio.sleep(ALBUM_FLUSH_SECONDS)
    except asyncio.CancelledError:
        return

    async with ALBUM_LOCK:
        items = ALBUM_QUEUES.pop(key, [])
        ALBUM_TASKS.pop(key, None)

    if not items:
        return

    chat_id = int(key.split(":", 1)[0])
    thread_raw = key.split(":", 1)[1]
    thread_id = int(thread_raw) if thread_raw != "0" else None
    topic_name = items[0].get("topic_name", "")

    await _send_album(context, chat_id, thread_id, topic_name, items)


async def enqueue_for_album(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    thread_id: Optional[int],
    topic_name: str,
    path: str,
    caption: str,
    cleanup_prefix: str,
) -> bool:
    key = _album_key(chat_id, thread_id)
    ready_items = None

    async with ALBUM_LOCK:
        queue = ALBUM_QUEUES.setdefault(key, [])
        queue.append(
            {
                "path": path,
                "caption": caption,
                "topic_name": topic_name,
                "cleanup_prefix": cleanup_prefix,
            }
        )

        old_task = ALBUM_TASKS.pop(key, None)
        if old_task and not old_task.done():
            old_task.cancel()

        if len(queue) >= ALBUM_SIZE:
            ready_items = ALBUM_QUEUES.pop(key, [])[:ALBUM_SIZE]
            # Any accidental extra items remain queued.
            extras = queue[ALBUM_SIZE:]
            if extras:
                ALBUM_QUEUES[key] = extras
        else:
            # Keep waiting until this exact topic reaches ALBUM_SIZE videos.
            # No timer is created, so topics are never mixed or flushed early.
            pass

    if ready_items:
        return await _send_album(context, chat_id, thread_id, topic_name, ready_items)
    return False


# =========================================================
# Main processing
# =========================================================


async def process_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    status_msg,
    topic_thread_id: Optional[int],
    topic_name: str,
    force_download: bool = False,
    known_key: Optional[tuple[str, str]] = None,
):
    site_name = get_site_name(url)
    user_id = update.effective_user.id if update.effective_user else 0
    source_message = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    message_id = source_message.message_id if source_message else int(datetime.now().timestamp())
    prefix = f"video_{user_id}_{message_id}_{hashlib.md5(url.encode()).hexdigest()[:8]}"
    output_template = str(DOWNLOAD_DIR / f"{prefix}.%(ext)s")

    target_group_id = get_target_group_id()
    if not target_group_id:
        await status_msg.edit_text(
            "❌ لم يتم تحديد جروب الاستقبال.\n\n"
            "استخدم /setgroup داخل الجروب المطلوب أولًا."
        )
        return

    async with JOB_SEMAPHORE:
        loop = asyncio.get_running_loop()
        file_path = None
        final_info = None
        selection = {}

        try:
            await status_msg.edit_text(
                f"🔎 جاري تحليل رابط {site_name}...\n\n"
                f"📂 المكان: {topic_name}\n"
                f"📦 الحد الآمن: {SAFE_MAX_BYTES // (1024 * 1024)} MB"
            )

            info = await loop.run_in_executor(None, extract_info, url)
            platform, video_id = get_video_key(info, url)
            if not info and known_key:
                platform, video_id = known_key

            if not force_download and is_video_downloaded(platform, video_id, url):
                owner_id = update.effective_user.id if update.effective_user else 0
                chat_id = update.effective_chat.id if update.effective_chat else 0
                source_message_id = source_message.message_id if source_message else 0
                token = create_pending_download(
                    owner_id,
                    chat_id,
                    source_message_id,
                    url,
                    platform,
                    video_id,
                    topic_thread_id,
                    topic_name,
                )
                keyboard = []
                if token:
                    keyboard = [[
                        InlineKeyboardButton(
                            "🔄 تحميل مرة أخرى",
                            callback_data=f"download_again:{token}",
                        ),
                        InlineKeyboardButton(
                            "❌ إلغاء",
                            callback_data=f"cancel_download:{token}",
                        ),
                    ]]
                await status_msg.edit_text(
                    "⚠️ هذا الفيديو تم تحميله وإرساله من قبل.\n\n"
                    f"📂 الموضوع: {topic_name}\n\n"
                    "هل تريد تحميله مرة أخرى؟",
                    reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
                )
                return

            await status_msg.edit_text(
                "⬇️ جاري التحميل بأفضل جودة مناسبة للحجم...\n"
                "إذا لم توجد جودة تحت 48 MB سأضغط الفيديو تلقائيًا."
            )

            if info:
                file_path, final_info, selection = await loop.run_in_executor(
                    None,
                    download_with_yt_dlp,
                    url,
                    output_template,
                    info,
                )

            if not file_path:
                file_path = await loop.run_in_executor(
                    None,
                    download_direct_video,
                    url,
                    DOWNLOAD_DIR / prefix,
                )

            if not file_path or not os.path.isfile(file_path):
                await status_msg.edit_text(get_download_error_message(site_name))
                return

            file_size = os.path.getsize(file_path)
            if file_size > SAFE_MAX_BYTES:
                await status_msg.edit_text(
                    f"🗜️ الحجم الحالي {human_size(file_size)} أكبر من 48 MB.\n"
                    "جاري الضغط تلقائيًا..."
                )
                compressed = await loop.run_in_executor(
                    None,
                    compress_video_to_limit,
                    file_path,
                )
                if not compressed:
                    await status_msg.edit_text(
                        "❌ تعذر الوصول إلى حجم مناسب لـ Telegram حتى بعد الضغط."
                    )
                    return
                if compressed != file_path:
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass
                    file_path = compressed

            file_size = os.path.getsize(file_path)
            attempts = 0
            while file_size > SAFE_MAX_BYTES and attempts < 2:
                attempts += 1
                compressed = await loop.run_in_executor(None, compress_video_to_limit, file_path)
                if not compressed:
                    break
                if compressed != file_path:
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass
                    file_path = compressed
                file_size = os.path.getsize(file_path)
            if file_size > SAFE_MAX_BYTES:
                await status_msg.edit_text("❌ الملف النهائي ما زال أكبر من 48 MB بعد كل محاولات الضغط.")
                return

            title = str((final_info or info or {}).get("title") or "").strip()[:800]
            caption = "🎬 تم تحميل الفيديو"
            if title:
                caption += f"\n\n{title}"

            # Save duplicate record before album send only after file is proven valid;
            # this prevents a successful re-queue from being mistaken for a failure.
            file_hash = await loop.run_in_executor(None, sha256_file, file_path)
            save_downloaded_video(platform, video_id, url, title, file_hash)

            selected_height = selection.get("height")
            quality_text = f"{int(selected_height)}p" if selected_height else "أفضل جودة متاحة"

            # The temporary status message is updated; the final media is queued.
            await status_msg.edit_text(
                "✅ تم تجهيز الفيديو.\n\n"
                f"🌐 المصدر: {site_name}\n"
                f"📂 الموضوع: {topic_name}\n"
                f"📐 الجودة: {quality_text}\n"
                f"📦 الحجم: {human_size(file_size)}\n\n"
                f"📦 تمت إضافته إلى Queue الخاصة بالقسم.\n"
                f"⏳ سيتم الإرسال تلقائيًا عند اكتمال {ALBUM_SIZE} فيديوهات في نفس القسم."
            )

            album_sent = await enqueue_for_album(
                context=context,
                chat_id=target_group_id,
                thread_id=topic_thread_id,
                topic_name=topic_name,
                path=file_path,
                caption=caption,
                cleanup_prefix=prefix,
            )
            file_path = None  # queue now owns cleanup

            if album_sent:
                await status_msg.edit_text(
                    "✅ تم إرسال الـ3 فيديوهات إلى الألبوم "
                    f"«{topic_name}» بنجاح.\n\n"
                    "🎬 تم إرسالهم معًا كألبوم واحد."
                )

        except Exception as exc:
            logger.exception("Processing error: %s", exc)
            try:
                await status_msg.edit_text(
                    "⚠️ حدث خطأ غير متوقع أثناء معالجة الفيديو.\n"
                    "راجع Logs الـActions للتفاصيل."
                )
            except Exception:
                pass
        finally:
            # Do not clean a file that has been handed to the album queue.
            if file_path:
                cleanup_download_files(prefix)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not await is_owner(update):
        return

    text = update.message.text or update.message.caption or ""
    urls = extract_urls(text)
    if not urls:
        return

    target_group_id = get_target_group_id()
    if not target_group_id:
        await update.message.reply_text(
            "❌ لم يتم تحديد جروب الاستقبال.\n"
            "استخدم /setgroup داخل الجروب المطلوب."
        )
        return

    # Recover from a stale target_group_id when exactly one registered group contains Topics.
    if not get_topics_for_group(target_group_id):
        state = _get_combined_state()
        all_topics = state.get("topics", {})
        candidates = []
        if isinstance(all_topics, dict):
            for gid, topic_map in all_topics.items():
                if isinstance(topic_map, dict) and topic_map:
                    try: candidates.append(int(gid))
                    except (TypeError, ValueError): pass
        if len(candidates) == 1 and set_target_group_id(candidates[0]):
            target_group_id = candidates[0]

    status_msg = await update.message.reply_text("📂 جاري فتح قائمة Topics...")
    # Store the URL against the picker/status message itself, because callback queries
    # point to that message rather than the original user message.
    try:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO settings(key, value)
                VALUES(?, ?)
                """,
                (
                    f"topic_picker_url:{status_msg.message_id}",
                    json.dumps(urls, ensure_ascii=False),
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Could not store topic picker URL: %s", exc)
    await show_topic_picker(status_msg, target_group_id, edit=True)


async def _get_picker_urls(message_id: int) -> list[str]:
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key=? LIMIT 1",
                (f"topic_picker_url:{message_id}",),
            ).fetchone()
            if not row or not row[0]:
                return []

            raw = str(row[0])
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = None

            # New format: JSON list of URLs.
            if isinstance(decoded, list):
                return [str(item) for item in decoded if item]

            # Backward compatibility with old picker records containing one URL.
            return [raw]
    except Exception:
        pass
    return []


async def _get_picker_url(message_id: int) -> Optional[str]:
    """Backward-compatible helper for older callers."""
    urls = await _get_picker_urls(message_id)
    return urls[0] if urls else None


async def _delete_picker_url(message_id: int) -> None:
    try:
        with db_connect() as conn:
            conn.execute(
                "DELETE FROM settings WHERE key=?",
                (f"topic_picker_url:{message_id}",),
            )
            conn.commit()
    except Exception:
        pass


# =========================================================
# Callbacks
# =========================================================


async def duplicate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not await is_owner(update):
        await query.answer("⛔ غير مسموح لك.", show_alert=True)
        return

    data = query.data or ""
    token = data.split(":", 1)[1] if ":" in data else ""
    pending = get_pending_download(token)
    if not pending:
        await query.answer("انتهت صلاحية الطلب.", show_alert=True)
        return
    if update.effective_user.id != pending["user_id"]:
        await query.answer("⛔ هذا الطلب ليس لك.", show_alert=True)
        return

    await query.answer()
    delete_pending_download(token)

    if data.startswith("cancel_download:"):
        await query.edit_message_text("❌ تم إلغاء إعادة التحميل.")
        return

    await query.edit_message_text(
        "🔄 تم اختيار إعادة التحميل...\n"
        f"📂 الموضوع: {pending['topic_name']}\n"
        "جاري البدء من جديد."
    )
    await process_url(
        update,
        context,
        pending["url"],
        query.message,
        topic_thread_id=pending["topic_thread_id"],
        topic_name=pending["topic_name"],
        force_download=True,
        known_key=(pending["platform"], pending["video_id"]),
    )


async def topic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not await is_owner(update):
        await query.answer("⛔ غير مسموح لك.", show_alert=True)
        return

    data = query.data or ""

    if data == "show_topics":
        group_id = get_target_group_id()
        await query.answer()
        if not group_id:
            await query.edit_message_text("❌ لم يتم تحديد جروب الاستقبال.")
            return
        await show_topic_picker(query, group_id, edit=True)
        return

    if data.startswith("topic_select:"):
        try:
            thread_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await query.answer("Topic غير صالح.", show_alert=True)
            return

        group_id = get_target_group_id()
        if not group_id:
            await query.answer("لم يتم تحديد الجروب.", show_alert=True)
            return

        topic_name = get_topic_name(group_id, thread_id)
        set_last_topic(group_id, thread_id)

        # The status message is the one directly below/created for the user's URL(s).
        source_message_id = query.message.message_id
        urls = await _get_picker_urls(source_message_id)

        if not urls:
            await query.answer("انتهت صلاحية روابط الفيديو.", show_alert=True)
            return

        await _delete_picker_url(source_message_id)
        await query.answer(f"تم اختيار: {topic_name}")
        await query.edit_message_text(
            f"📂 تم اختيار: {topic_name}\n\n"
            f"🔎 جاري بدء تحميل {len(urls)} فيديو..."
        )

        # Process every URL captured from the original message. The same Topic is
        # passed to every item, so the existing queue/album logic remains untouched.
        for url in urls:
            await process_url(
                update,
                context,
                url,
                query.message,
                topic_thread_id=thread_id,
                topic_name=topic_name,
            )
        return

    if data.startswith("topic_default:"):
        try:
            thread_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await query.answer("Topic غير صالح.", show_alert=True)
            return
        group_id = get_target_group_id()
        if not group_id:
            await query.answer("لم يتم تحديد الجروب.", show_alert=True)
            return
        set_last_topic(group_id, thread_id)
        await query.answer("✅ تم حفظ الاختيار الافتراضي.")
        await query.edit_message_text(
            f"✅ الـTopic الافتراضي أصبح:\n\n📂 {get_topic_name(group_id, thread_id)}"
        )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not await is_owner(update):
        await query.answer("⛔ غير مسموح لك.", show_alert=True)
        return

    data = query.data or ""
    await query.answer()

    if data in {"refresh_admin", "current_group"}:
        current = get_target_group_id()
        title = "غير معروف"
        if current:
            try:
                chat = await context.bot.get_chat(current)
                title = chat.title or title
            except Exception:
                pass
        await query.edit_message_text(
            "📍 جروب الاستقبال الحالي:\n\n"
            f"🏷️ الاسم: {title}\n"
            f"🆔 ID: `{current or 'غير محدد'}`\n\n"
            "استخدم /admin لعرض القائمة.",
            parse_mode="Markdown",
        )
        return

    if data == "show_topics":
        group_id = get_target_group_id()
        if not group_id:
            await query.edit_message_text("❌ لم يتم تحديد جروب الاستقبال.")
            return
        topics = get_topics_for_group(group_id)
        if not topics:
            await query.edit_message_text(
                "📂 لا توجد Topics مسجلة.\n\n"
                "ادخل كل Topic واكتب `/settopic اسم الموضوع`."
            )
            return
        lines = ["📂 Topics المسجلة:\n"]
        for key, item in sorted(topics.items(), key=lambda kv: str(kv[1].get("name", ""))):
            lines.append(f"• {item.get('name')} — `{key}`")
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")
        return

    if data == "register_current_group":
        chat = update.effective_chat
        if not chat or chat.type not in ("group", "supergroup"):
            await query.edit_message_text("📌 اضغط التسجيل من داخل الجروب المطلوب.")
            return
        if register_group(chat.id, chat.title or ""):
            await query.edit_message_text(
                "✅ تم تسجيل الجروب.\n\n"
                f"🏷️ {chat.title or 'بدون اسم'}\n"
                f"🆔 `{chat.id}`\n\n"
                "استخدم /admin لاختياره."
            )
        else:
            await query.edit_message_text("❌ تعذر التسجيل.")
        return

    if data.startswith("select_group:"):
        try:
            group_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await query.edit_message_text("❌ معرف الجروب غير صحيح.")
            return

        registered_ids = set()
        for raw_id, _ in get_registered_groups():
            try:
                registered_ids.add(int(raw_id))
            except ValueError:
                pass
        if group_id not in registered_ids:
            await query.edit_message_text("❌ هذا الجروب غير مسجل.")
            return

        try:
            chat = await context.bot.get_chat(group_id)
        except Exception:
            await query.edit_message_text(
                "❌ لا أستطيع الوصول إلى الجروب. تأكد أن البوت موجود داخله."
            )
            return

        if set_target_group_id(group_id, chat.title or ""):
            await query.edit_message_text(
                "✅ تم تغيير جروب الاستقبال وحفظه.\n\n"
                f"🏷️ {chat.title or 'غير معروف'}\n"
                f"🆔 `{group_id}`",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text("❌ تعذر حفظ الجروب.")


# =========================================================
# Start + error handling
# =========================================================



async def setpassword_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update): return
    if not context.args:
        await update.message.reply_text("الاستخدام: /setpassword كلمة_السر")
        return
    hashed=hashlib.sha256(context.args[0].encode()).hexdigest()
    with db_connect() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('password_hash',?)",(hashed,))
    await update.message.reply_text("✅ تم حفظ كلمة السر بشكل Hash")

async def unlock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام: /unlock كلمة_السر")
        return
    with db_connect() as conn:
        row=conn.execute("SELECT value FROM settings WHERE key='password_hash'").fetchone()
        if row and hashlib.sha256(context.args[0].encode()).hexdigest()==row[0]:
            conn.execute("INSERT OR REPLACE INTO bot_access(user_id,unlocked_at) VALUES(?,?)",(str(update.effective_user.id),datetime.now().isoformat()))
            await update.message.reply_text("✅ تم فتح الصلاحية")
        else:
            await update.message.reply_text("❌ كلمة السر غير صحيحة")

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update): return
    with db_connect() as conn:
        rows=conn.execute("SELECT user_id,unlocked_at FROM bot_access").fetchall()
    await update.message.reply_text("👥 المستخدمون:\n"+"\n".join(f"{a} - {b}" for a,b in rows) if rows else "لا يوجد مستخدمون")

async def removeuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update) or not context.args: return
    with db_connect() as conn: conn.execute("DELETE FROM bot_access WHERE user_id=?",(context.args[0],))
    await update.message.reply_text("✅ تم الحذف")

async def library_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update): return
    with db_connect() as conn:
        count=conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    await update.message.reply_text(f"📚 المكتبة: {count} فيديو")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update): return
    db=DATABASE_PATH.stat().st_size if DATABASE_PATH.exists() else 0
    await update.message.reply_text(f"📊 Status\n\nDownloads: {sum(1 for _ in DOWNLOAD_DIR.glob('*'))}\nDB: {human_size(db)}\nQueue: {len(ALBUM_QUEUES)}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not await is_owner(update):
        await update.message.reply_text("⛔ هذا البوت خاص بالمالك فقط.")
        return

    await update.message.reply_text(
        "👋 البوت جاهز.\n\n"
        "أرسل رابط فيديو، وبعدها اختر الـTopic يدويًا.\n\n"
        "/admin — إدارة الجروب\n"
        "/setgroup — تعيين الجروب الحالي\n"
        "/settopic — تسجيل Topic من داخله\n"
        "/topics — إدارة الـTopics"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Telegram application error: %s", context.error)


# =========================================================
# Startup
# =========================================================


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is not set")
    if not ADMIN_USER_ID:
        raise ValueError("ADMIN_USER_ID is not set. This bot is owner-only.")

    init_database()

    logger.info("FFmpeg: %s", "OK" if ffmpeg_exists() else "MISSING")
    logger.info("ffprobe: %s", "OK" if ffprobe_exists() else "MISSING")
    logger.info("yt-dlp version: %s", yt_dlp.version.__version__)
    logger.info(
        "Cookies: %s",
        "configured" if COOKIES_FILE and Path(COOKIES_FILE).exists() else "not configured",
    )
    logger.info(
        "GitHub persistent state: %s",
        "enabled" if GITHUB_TOKEN and GITHUB_REPO else "disabled",
    )
    logger.info("Album size: %s | flush: %ss", ALBUM_SIZE, ALBUM_FLUSH_SECONDS)
    logger.info("Protect content: %s", PROTECT_CONTENT)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("setgroup", set_group_command))
    app.add_handler(CommandHandler("settopic", set_topic_command))
    app.add_handler(CommandHandler("topics", topics_command))
    app.add_handler(CommandHandler("setpassword", setpassword_command))
    app.add_handler(CommandHandler("unlock", unlock_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("removeuser", removeuser_command))
    app.add_handler(CommandHandler("library", library_command))
    app.add_handler(CommandHandler("status", status_command))

    app.add_handler(
        CallbackQueryHandler(
            duplicate_callback,
            pattern=r"^(download_again|cancel_download):",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            topic_callback,
            pattern=r"^(topic_select|topic_default|show_topics):?",
        )
    )
    app.add_handler(
        CallbackQueryHandler(admin_callback)
    )

    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_message,
        )
    )
    app.add_error_handler(error_handler)

    logger.info("Personal Universal Video Downloader Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
