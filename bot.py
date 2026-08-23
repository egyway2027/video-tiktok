import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# The old hard-coded group is intentionally only a migration fallback.
LEGACY_TARGET_GROUP_ID = os.getenv("TARGET_GROUP_ID", "").strip()

COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()
if COOKIES_FILE:
    COOKIES_FILE = str(Path(COOKIES_FILE).expanduser())

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "bot.db"))
STATE_FILE = Path(os.getenv("STATE_FILE", "bot_state.json"))

# Telegram bots currently have a 50 MB send limit. We deliberately stay below it.
TELEGRAM_LIMIT_BYTES = 50 * 1024 * 1024
SAFE_MAX_BYTES = 48 * 1024 * 1024
COMPRESSION_TARGET_BYTES = 47 * 1024 * 1024
UNKNOWN_SIZE_TARGET_BYTES = 45 * 1024 * 1024

# One personal bot: keep the system stable and predictable.
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "1"))
JOB_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

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


def cleanup_download_files(prefix: str) -> None:
    for path in DOWNLOAD_DIR.glob(f"{prefix}*"):
        if path.is_file():
            try:
                path.unlink()
            except Exception as exc:
                logger.warning("Could not delete %s: %s", path, exc)


def safe_filename(value: str, fallback: str = "video") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "")
    return value[:100] or fallback


# =========================================================
# Persistent state
# =========================================================

# SQLite is used locally for fast access. On GitHub Actions, the selected target
# group is also synchronized to a repository file so it survives runner replacement.


def db_connect():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


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
            CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_platform_video_id
            ON videos(platform, video_id)
            WHERE platform IS NOT NULL AND platform != ''
              AND video_id IS NOT NULL AND video_id != ''
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_videos_url ON videos(url)"
        )
        # Pending confirmations should not survive for days after a runner restart.
        cutoff = datetime.fromtimestamp(datetime.now().timestamp() - 3600).isoformat(timespec="seconds")
        conn.execute("DELETE FROM pending_downloads WHERE created_at < ?", (cutoff,))
        conn.commit()


def _read_local_state() -> dict:
    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_local_state(data: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)


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
        return None
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


def get_target_group_id() -> Optional[int]:
    # Prefer repository-persistent state when running on GitHub Actions.
    remote_state, _ = _github_get_state()
    remote_id = remote_state.get("target_group_id")
    if remote_id:
        try:
            return int(remote_id)
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
        logger.warning("Could not read local target group: %s", exc)

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

    # Persist on GitHub when the bot is hosted there.
    if GITHUB_TOKEN and GITHUB_REPO:
        state, sha = _github_get_state()
        state["target_group_id"] = int(group_id)
        if title:
            state["target_group_title"] = title
        state["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        if not _github_save_state(state, sha, "Update Telegram target group"):
            logger.warning("Target group was saved locally but GitHub persistence failed")

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

        if GITHUB_TOKEN and GITHUB_REPO:
            state, sha = _github_get_state()
            groups = {
                str(item.get("group_id")): str(item.get("title") or "")
                for item in state.get("groups", [])
                if isinstance(item, dict) and item.get("group_id") is not None
            }
            groups[str(group_id)] = title or ""
            state["groups"] = [
                {"group_id": int(gid) if str(gid).lstrip("-").isdigit() else gid, "title": name}
                for gid, name in groups.items()
            ]
            state["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            if not _github_save_state(state, sha, "Register Telegram target group"):
                logger.warning("Group was registered locally but GitHub persistence failed")

        return True
    except Exception as exc:
        logger.exception("Could not register group: %s", exc)
        return False


def get_registered_groups() -> list[tuple[str, str]]:
    """Return groups from local DB plus repository-persistent state."""
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

    remote_state, _ = _github_get_state()
    for item in remote_state.get("groups", []):
        if isinstance(item, dict) and item.get("group_id") is not None:
            merged[str(item["group_id"])] = str(item.get("title") or "")

    return list(merged.items())


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
        name = title or f"جروب {group_id}"
        prefix = "✅ " if current == numeric_id else "📍 "
        keyboard.append([
            InlineKeyboardButton(
                f"{prefix}{name}",
                callback_data=f"select_group:{numeric_id}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "➕ تسجيل الجروب الحالي",
            callback_data="register_current_group",
        )
    ])
    keyboard.append([
        InlineKeyboardButton(
            "🔄 تحديث القائمة",
            callback_data="refresh_admin",
        )
    ])

    current_text = str(current) if current else "غير محدد"
    await update.message.reply_text(
        "⚙️ لوحة البوت الشخصية\n\n"
        f"📤 جروب الاستقبال الحالي: `{current_text}`\n\n"
        "اختر الجروب الذي سيرسل إليه البوت الفيديوهات.\n"
        "التغيير يتم حفظه ويظل فعالًا بعد إعادة تشغيل السيرفر.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not await is_owner(update):
        await query.answer("⛔ غير مسموح لك.", show_alert=True)
        return

    await query.answer()
    data = query.data or ""

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
            "استخدم /admin لعرض قائمة الجروبات المسجلة.",
            parse_mode="Markdown",
        )
        return

    if data == "register_current_group":
        chat = update.effective_chat
        if not chat or chat.type not in ("group", "supergroup"):
            await query.edit_message_text(
                "📌 اضغط هذا الزر من داخل الجروب المطلوب تسجيله."
            )
            return
        if not register_group(chat.id, chat.title or ""):
            await query.edit_message_text("❌ تعذر تسجيل الجروب.")
            return
        await query.edit_message_text(
            "✅ تم تسجيل الجروب.\n\n"
            f"🏷️ {chat.title or 'بدون اسم'}\n"
            f"🆔 `{chat.id}`\n\n"
            "استخدم /admin مرة أخرى لاختياره كجروب استقبال.",
            parse_mode="Markdown",
        )
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
        except Exception as exc:
            logger.warning("Target group is unreachable: %s", exc)
            await query.edit_message_text(
                "❌ لا أستطيع الوصول إلى هذا الجروب.\n"
                "تأكد أن البوت موجود داخله."
            )
            return

        if not set_target_group_id(group_id, chat.title or ""):
            await query.edit_message_text("❌ تعذر حفظ الجروب.")
            return

        await query.edit_message_text(
            "✅ تم تغيير جروب الاستقبال وحفظه.\n\n"
            f"🏷️ {chat.title or 'غير معروف'}\n"
            f"🆔 `{group_id}`",
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
        await update.message.reply_text(
            "❌ استخدم /setgroup داخل الجروب الذي تريد تعيينه."
        )
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


# =========================================================
# Duplicate protection
# =========================================================


def get_video_key(info: Optional[dict], url: str) -> tuple[str, str]:
    if not info:
        return "", canonicalize_url(url)
    platform = str(info.get("extractor_key") or info.get("extractor") or "").strip().lower()
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
            if row:
                return True
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
        with db_connect() as conn:
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT OR IGNORE INTO videos(platform, video_id, url, file_hash, title, downloaded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (platform, video_id, canonicalize_url(url), file_hash, title, now),
            )
            conn.execute(
                """
                UPDATE videos
                SET url=?, file_hash=?, title=?, downloaded_at=?
                WHERE platform=? AND video_id=?
                """,
                (canonicalize_url(url), file_hash, title, now, platform, video_id),
            )
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    except Exception as exc:
        logger.warning("Could not save downloaded video: %s", exc)
        return False


def create_pending_download(
    user_id: int, chat_id: int, message_id: int, url: str, platform: str, video_id: str
) -> str:
    token = hashlib.sha256(
        f"{user_id}:{chat_id}:{message_id}:{url}:{datetime.now().timestamp()}".encode()
    ).hexdigest()[:24]
    try:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_downloads
                    (token, user_id, chat_id, message_id, url, platform, video_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (token, str(user_id), str(chat_id), str(message_id), canonicalize_url(url),
                 platform, video_id, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Could not create pending download: %s", exc)
        return ""
    return token


def get_pending_download(token: str) -> Optional[dict]:
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT user_id, chat_id, message_id, url, platform, video_id FROM pending_downloads WHERE token=?",
                (token,),
            ).fetchone()
        if not row:
            return None
        return {"user_id": int(row[0]), "chat_id": int(row[1]), "message_id": int(row[2]),
                "url": row[3], "platform": row[4] or "", "video_id": row[5] or ""}
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


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# =========================================================
# URL / site helpers
# =========================================================


def canonicalize_url(url: str) -> str:
    """Remove common tracking parameters so equivalent share links match."""
    url = normalize_url(url)
    try:
        parsed = urllib.parse.urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return url
        tracking = {
            "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "fbclid", "igsh", "igshid", "si", "feature", "share_id",
        }
        query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
                 if k.lower() not in tracking and not k.lower().startswith("utm_")]
        return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"),
                                        urllib.parse.urlencode(query), ""))
    except Exception:
        return url


def extract_url(text: str) -> Optional[str]:
    if not text:
        return None
    match = URL_REGEX.search(text)
    return normalize_url(match.group(0)) if match else None


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
        "no_warnings": False,
        "noplaylist": True,
        "source_address": "0.0.0.0",
        "retries": 5,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": HTTP_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Referer": "https://www.google.com/",
        },
    }

    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        options["cookiefile"] = COOKIES_FILE

    # yt-dlp now uses external JS runtimes for full support on some sites,
    # especially YouTube. Deno is preferred and is installed by the workflow.
    if shutil.which("deno"):
        options["js_runtimes"] = {"deno": {}}

    # Let yt-dlp use browser impersonation when a site challenges non-browser
    # clients. This is especially useful for modern social-media endpoints.
    options["extractor_args"] = {"generic": {"impersonate": [""]}}
    if os.getenv("YTDLP_IMPERSONATE", "1").strip().lower() in {"0", "false", "no"}:
        options.pop("extractor_args", None)

    return options


def extract_info(url: str) -> Optional[dict]:
    url = normalize_url(url)
    attempts = []
    base = get_common_ydl_options()
    attempts.append({**base, "skip_download": True})
    attempts.append({**base, "skip_download": True, "geo_bypass": True})

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
        # tbr is kbps; leave a 10% overhead margin for estimation error.
        return int((float(tbr) * 1000 / 8) * duration * 1.10)
    return None


def _format_score(fmt: dict) -> tuple:
    height = int(fmt.get("height") or 0)
    fps = float(fmt.get("fps") or 0)
    vcodec = str(fmt.get("vcodec") or "")
    acodec = str(fmt.get("acodec") or "")
    ext = str(fmt.get("ext") or "")
    codec_bonus = 2 if "avc" in vcodec or "h264" in vcodec else 0
    audio_bonus = 2 if "mp4a" in acodec or "aac" in acodec else 0
    ext_bonus = 2 if ext == "mp4" else 0
    return height, fps, codec_bonus + audio_bonus + ext_bonus


def choose_best_format(info: dict, target_bytes: int = SAFE_MAX_BYTES) -> tuple[Optional[str], dict]:
    """Choose the highest-quality known/estimated format that should fit the target."""
    formats = [f for f in (info.get("formats") or []) if f.get("vcodec") not in (None, "none")]
    duration = info.get("duration")

    # Prefer a single progressive format when its size is known.
    candidates = []
    for fmt in formats:
        if fmt.get("acodec") in (None, "none"):
            continue
        size = _estimate_format_size(fmt, duration)
        if size is not None:
            candidates.append((fmt, size, False))

    # Separate video + audio: combine the best audio with each video stream.
    audio_formats = [
        f for f in formats
        if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")
    ]
    # The filter above uses formats list; if it excluded audio-only, recover from all formats.
    audio_formats = [
        f for f in (info.get("formats") or [])
        if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")
    ]
    audio_formats.sort(key=lambda f: _estimate_format_size(f, duration) or 10**18)
    best_audio = audio_formats[0] if audio_formats else None

    if best_audio:
        audio_size = _estimate_format_size(best_audio, duration)
        if audio_size is not None:
            for fmt in formats:
                if fmt.get("acodec") not in (None, "none"):
                    continue
                vsize = _estimate_format_size(fmt, duration)
                if vsize is not None:
                    candidates.append((fmt, vsize + audio_size, True))
        else:
            # If audio size is unknown, let yt-dlp choose the best compatible audio.
            for fmt in formats:
                if fmt.get("acodec") in (None, "none"):
                    candidates.append((fmt, None, True))

    fitting = [item for item in candidates if item[1] is not None and item[1] <= target_bytes]
    if fitting:
        fitting.sort(key=lambda item: _format_score(item[0]), reverse=True)
        fmt, size, separate = fitting[0]
        expression = f"{fmt['format_id']}+bestaudio/best" if separate else fmt["format_id"]
        return expression, {
            "height": fmt.get("height"),
            "estimated_size": size,
            "format_id": fmt.get("format_id"),
            "separate": separate,
        }

    # No format has a reliable size below target. Choose the best stream conservatively.
    video_only = [f for f in formats if f.get("height")]
    if video_only:
        video_only.sort(key=_format_score, reverse=True)
        chosen = video_only[0]
        if chosen.get("acodec") not in (None, "none"):
            expression = chosen["format_id"]
            separate = False
        else:
            expression = f"{chosen['format_id']}+bestaudio/best"
            separate = True
        return expression, {
            "height": chosen.get("height"),
            "estimated_size": _estimate_format_size(chosen, duration),
            "format_id": chosen.get("format_id"),
            "separate": separate,
            "fallback": True,
        }

    return None, {}


def _find_downloaded_file(info: dict, ydl: yt_dlp.YoutubeDL) -> Optional[str]:
    requested = info.get("requested_downloads") or []
    for item in requested:
        path = item.get("filepath")
        if path and os.path.isfile(path):
            return path

    prepared = ydl.prepare_filename(info)
    stem = os.path.splitext(prepared)[0]
    for candidate in (prepared, stem + ".mp4", stem + ".mkv", stem + ".webm", stem + ".mov"):
        if os.path.isfile(candidate):
            return candidate
    return None


def download_with_yt_dlp(url: str, output_template: str) -> tuple[Optional[str], Optional[dict], dict]:
    info = extract_info(url)
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

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            downloaded_info = ydl.extract_info(url, download=True)
            path = _find_downloaded_file(downloaded_info, ydl) if downloaded_info else None
            if path:
                return path, downloaded_info, selection
    except Exception as exc:
        logger.warning("yt-dlp download failed: %s", exc)

    return None, info, selection


# =========================================================
# Direct video URL fallback
# =========================================================


def _looks_like_video_url(url: str, content_type: str = "") -> bool:
    if content_type.lower().split(";", 1)[0].startswith("video/"):
        return True
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith((".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".flv", ".ts"))


def download_direct_video(url: str, output_path: Path) -> Optional[str]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": HTTP_USER_AGENT, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content_type = response.headers.get("Content-Type", "")
            if not _looks_like_video_url(response.geturl(), content_type):
                return None

            total = response.headers.get("Content-Length")
            if total:
                try:
                    if int(total) > 2 * 1024 * 1024 * 1024:
                        logger.warning("Direct video is larger than 2 GB; refusing early download")
                        return None
                except ValueError:
                    pass

            suffix = Path(urllib.parse.urlparse(response.geturl()).path).suffix or ".mp4"
            target = output_path.with_suffix(suffix)
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

    # Several targets make the operation robust when the first bitrate estimate overshoots.
    targets = [
        COMPRESSION_TARGET_BYTES,
        46 * 1024 * 1024,
        44 * 1024 * 1024,
        42 * 1024 * 1024,
    ]

    for target_bytes in targets:
        target_bits = target_bytes * 8
        audio_bps = 96_000
        video_bps = max(int(target_bits / duration - audio_bps), 120_000)
        bitrate_k = max(video_bps // 1000, 120)

        try:
            if final_path.exists():
                final_path.unlink()
        except Exception:
            pass

        # Keep the source aspect ratio and cap width at 720 for large videos.
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

        logger.info("Compression attempt: target=%s, video_bitrate=%sk", human_size(target_bytes), bitrate_k)
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
            logger.warning("FFmpeg failed: %s", result.stderr[-1500:])
            continue

        size = final_path.stat().st_size
        logger.info("Compressed result: %s", human_size(size))
        if size <= SAFE_MAX_BYTES:
            return str(final_path)

    return None


# =========================================================
# Error messages
# =========================================================


def get_download_error_message(site_name: str) -> str:
    cookie_hint = ""
    if COOKIES_FILE:
        cookie_hint = "\n• تم تفعيل Cookies؛ إذا استمر الفشل فالجلسة قد تكون منتهية."
    elif site_name in {"Facebook", "Instagram", "TikTok", "YouTube"}:
        cookie_hint = "\n• إذا كان المحتوى يحتاج تسجيل دخول، أضف Cookies صالحة عبر COOKIES_FILE."

    return (
        f"❌ لم أستطع استخراج فيديو {site_name}.\n\n"
        "الأسباب الأقرب:\n"
        "• الرابط خاص أو يحتاج تسجيل دخول.\n"
        "• الموقع غيّر طريقة الحماية/الاستخراج.\n"
        "• جلسة Cookies غير موجودة أو منتهية.\n"
        "• الفيديو غير متاح من سيرفر GitHub أو الرابط منتهي.\n\n"
        "💡 البوت يستخدم أحدث yt-dlp مع browser impersonation، لكن المحتوى المقيد لا يمكن تجاوزه بدون جلسة صالحة."
        f"{cookie_hint}"
    )


# =========================================================
# Main processing
# =========================================================


async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, status_msg, force_download: bool = False, known_key: Optional[tuple[str, str]] = None):
    site_name = get_site_name(url)
    user_id = update.effective_user.id if update.effective_user else 0
    message_id = update.message.message_id if update.message else 0
    prefix = f"video_{user_id}_{message_id}"
    output_template = str(DOWNLOAD_DIR / f"{prefix}.%(ext)s")

    async with JOB_SEMAPHORE:
        loop = asyncio.get_running_loop()
        file_path = None
        final_info = None
        selection = {}

        try:
            await status_msg.edit_text(f"🔎 جاري تحليل رابط {site_name}...\n\nأبحث عن أعلى جودة يمكن إرسالها تحت {SAFE_MAX_BYTES / 1024 / 1024:.0f} MB.")

            info = await loop.run_in_executor(None, extract_info, url)
            platform = ""
            video_id = canonicalize_url(url)
            if info:
                platform, video_id = get_video_key(info, url)
            elif known_key:
                platform, video_id = known_key

            if not force_download and is_video_downloaded(platform, video_id, url):
                owner_id = update.effective_user.id if update.effective_user else 0
                chat_id = update.effective_chat.id if update.effective_chat else 0
                source_message_id = (
                    update.message.message_id if update.message
                    else (update.callback_query.message.message_id if update.callback_query and update.callback_query.message else 0)
                )
                token = create_pending_download(owner_id, chat_id, source_message_id, url, platform, video_id)
                keyboard = []
                if token:
                    keyboard = [[
                        InlineKeyboardButton("🔄 تحميل مرة أخرى", callback_data=f"download_again:{token}"),
                        InlineKeyboardButton("❌ إلغاء", callback_data=f"cancel_download:{token}"),
                    ]]
                await status_msg.edit_text(
                    "⚠️ هذا الفيديو تم تحميله وإرساله من قبل.\n\n"
                    "هل تريد تحميله مرة أخرى؟",
                    reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
                )
                return

            await status_msg.edit_text(
                "⬇️ جاري التحميل بأفضل جودة مناسبة للحجم...\n"
                "إذا لم توجد جودة تحت الحد، سأضغط الفيديو تلقائيًا."
            )

            if info:
                file_path, final_info, selection = await loop.run_in_executor(
                    None, download_with_yt_dlp, url, output_template
                )

            # Direct-file fallback for plain MP4/WebM/etc. URLs.
            if not file_path:
                direct_base = DOWNLOAD_DIR / prefix
                file_path = await loop.run_in_executor(
                    None, download_direct_video, url, direct_base
                )

            if not file_path or not os.path.isfile(file_path):
                await status_msg.edit_text(get_download_error_message(site_name))
                return

            file_size = os.path.getsize(file_path)
            logger.info("Downloaded %s: %s", human_size(file_size), file_path)

            # If the chosen stream exceeded the limit, compress rather than fail.
            if file_size > SAFE_MAX_BYTES:
                await status_msg.edit_text(
                    f"🗜️ الحجم الحالي {human_size(file_size)} أكبر من الحد.\n"
                    "جاري ضغط الفيديو تلقائيًا مع الحفاظ على أعلى جودة ممكنة..."
                )
                compressed = await loop.run_in_executor(
                    None, compress_video_to_limit, file_path
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
            if file_size > SAFE_MAX_BYTES:
                await status_msg.edit_text("❌ الملف النهائي ما زال أكبر من 48 MB.")
                return

            title = str((final_info or info or {}).get("title") or "").strip()[:800]
            caption = "🎬 تم تحميل الفيديو"
            if title:
                caption += f"\n\n{title}"

            target_group_id = get_target_group_id()
            if not target_group_id:
                await status_msg.edit_text(
                    "❌ لم يتم تحديد جروب الاستقبال.\n\n"
                    "أدخل البوت إلى الجروب المطلوب ثم استخدم /setgroup داخله، "
                    "أو استخدم /admin لتغيير الجروب المحفوظ."
                )
                return

            try:
                target_chat = await context.bot.get_chat(target_group_id)
                if target_chat.type not in ("group", "supergroup", "channel"):
                    await status_msg.edit_text("❌ جروب الاستقبال المحفوظ غير صالح.")
                    return
            except Exception as exc:
                logger.warning("Could not access target group %s: %s", target_group_id, exc)
                await status_msg.edit_text(
                    "❌ لا أستطيع الوصول إلى جروب الاستقبال المحفوظ.\n"
                    "تأكد أن البوت ما زال عضوًا فيه."
                )
                return

            await status_msg.edit_text(
                f"📤 جاري إرسال الفيديو...\n📦 الحجم النهائي: {human_size(file_size)}"
            )

            sent = False
            try:
                with open(file_path, "rb") as video_file:
                    await context.bot.send_video(
                        chat_id=target_group_id,
                        video=video_file,
                        caption=caption,
                        supports_streaming=True,
                        read_timeout=180,
                        write_timeout=180,
                        connect_timeout=30,
                        pool_timeout=30,
                    )
                sent = True
            except Exception as exc:
                logger.warning("send_video failed: %s", exc)

            if not sent:
                try:
                    with open(file_path, "rb") as video_file:
                        await context.bot.send_document(
                            chat_id=target_group_id,
                            document=video_file,
                            caption=caption,
                            read_timeout=180,
                            write_timeout=180,
                            connect_timeout=30,
                            pool_timeout=30,
                        )
                    sent = True
                except Exception as exc:
                    logger.exception("send_document failed: %s", exc)

            if not sent:
                await status_msg.edit_text(
                    "❌ تم تحميل الفيديو وتجهيزه، لكن Telegram رفض الإرسال إلى الجروب.\n"
                    "تأكد أن البوت عضو في الجروب ولديه صلاحية إرسال الفيديوهات والملفات."
                )
                return

            file_hash = await loop.run_in_executor(None, sha256_file, file_path)
            save_downloaded_video(platform, video_id, url, title, file_hash)

            selected_height = selection.get("height")
            quality_text = f"{int(selected_height)}p" if selected_height else "أفضل جودة متاحة"
            await status_msg.edit_text(
                "✅ تم التحميل والإرسال بنجاح.\n\n"
                f"🌐 المصدر: {site_name}\n"
                f"📐 الجودة المختارة: {quality_text}\n"
                f"📦 الحجم النهائي: {human_size(file_size)}\n"
                f"📤 الجروب: {target_chat.title or target_group_id}"
            )

        except Exception as exc:
            logger.exception("Processing error: %s", exc)
            try:
                await status_msg.edit_text(
                    "⚠️ حدث خطأ غير متوقع أثناء معالجة الفيديو.\n"
                    "راجع Logs السيرفر لمعرفة التفاصيل."
                )
            except Exception:
                pass
        finally:
            cleanup_download_files(prefix)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not await is_owner(update):
        return

    text = update.message.text or update.message.caption or ""
    url = extract_url(text)
    if not url:
        return

    try:
        status_msg = await update.message.reply_text(
            f"🔎 استلمت الرابط ({get_site_name(url)}). جاري البدء..."
        )
    except Exception:
        return

    await process_url(update, context, url, status_msg)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not await is_owner(update):
        await update.message.reply_text("⛔ هذا البوت خاص بالمالك فقط.")
        return
    await update.message.reply_text(
        "👋 البوت جاهز.\n\n"
        "أرسل أي رابط فيديو وسأختار أعلى جودة مناسبة تحت 48 MB.\n\n"
        "/admin — اختيار جروب الاستقبال\n"
        "/setgroup — تعيين الجروب الحالي مباشرة"
    )


async def download_duplicate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not await is_owner(update):
        if query:
            await query.answer("⛔ غير مسموح لك.", show_alert=True)
        return

    data = query.data or ""
    token = data.split(":", 1)[1] if ":" in data else ""
    pending = get_pending_download(token)
    if not pending:
        await query.answer("انتهت صلاحية الطلب.", show_alert=True)
        return
    if pending["user_id"] != update.effective_user.id:
        await query.answer("⛔ هذا الطلب ليس لك.", show_alert=True)
        return

    await query.answer()
    if data.startswith("cancel_download:"):
        delete_pending_download(token)
        await query.edit_message_text("❌ تم إلغاء إعادة التحميل.")
        return

    delete_pending_download(token)
    await query.edit_message_text("🔄 تم اختيار إعادة التحميل...\nجاري بدء العملية من جديد.")
    await process_url(
        update, context, pending["url"], query.message,
        force_download=True,
        known_key=(pending["platform"], pending["video_id"]),
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
    logger.info("Cookies: %s", "configured" if COOKIES_FILE and Path(COOKIES_FILE).exists() else "not configured")
    logger.info("GitHub persistent state: %s", "enabled" if GITHUB_TOKEN and GITHUB_REPO else "disabled")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("setgroup", set_group_command))
    app.add_handler(CallbackQueryHandler(download_duplicate_callback, pattern=r"^(download_again|cancel_download):"))
    app.add_handler(CallbackQueryHandler(admin_callback))
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
