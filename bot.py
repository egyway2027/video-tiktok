import os
import re
import asyncio
import logging
import sqlite3

from datetime import datetime
from pathlib import Path

import yt_dlp

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)


# =========================================================
# الإعدادات
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Telegram User ID الخاص بمالك البوت
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")

# الجروب الافتراضي
TARGET_GROUP_ID = int(
    os.getenv(
        "TARGET_GROUP_ID",
        "-1004468483224",
    )
)

# =========================================================
# Cookies
# =========================================================

# يمكن وضع ملف cookies.txt بجانب ملف البوت
# أو تحديد مساره من Environment Variable:
#
# COOKIES_FILE=/path/to/cookies.txt
#
COOKIES_FILE = os.getenv(
    "COOKIES_FILE",
    "cookies.txt",
).strip()

# ---------------------------------------------------------
# اختياري:
#
# لو السيرفر عليه Chrome / Firefox / Edge وغيرها
# يمكن تحديد:
#
# COOKIES_FROM_BROWSER=chrome
#
# أو:
# COOKIES_FROM_BROWSER=firefox
#
# اتركه فارغًا إذا كنت تستخدم cookies.txt
# ---------------------------------------------------------

COOKIES_FROM_BROWSER = os.getenv(
    "COOKIES_FROM_BROWSER",
    "",
).strip()

# User-Agent
USER_AGENT = os.getenv(
    "USER_AGENT",
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
)

# مجلد تحميل الفيديوهات
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

# قاعدة البيانات
DATABASE_PATH = Path("bot.db")

# الحد الأقصى للملف
MAX_FILE_SIZE = 49 * 1024 * 1024


# =========================================================
# Logging
# =========================================================

logging.basicConfig(
    format=(
        "%(asctime)s - "
        "%(name)s - "
        "%(levelname)s - "
        "%(message)s"
    ),
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# أدوات yt-dlp
# =========================================================

def get_common_ydl_options():
    """
    إعدادات مشتركة لكل عمليات yt-dlp.

    أهم شيء هنا:
    - Cookies
    - User-Agent
    - timeout
    - retries
    - IPv4
    """

    options = {
        "quiet": True,
        "no_warnings": True,

        "noplaylist": True,

        "source_address": "0.0.0.0",

        "socket_timeout": 30,

        "retries": 5,
        "fragment_retries": 5,

        "retry_sleep_functions": {
            "http": lambda n: min(5, n),
        },

        "http_headers": {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },

        "geo_bypass": True,

        "nocheckcertificate": False,
    }

    # -----------------------------------------------------
    # استخدام cookies.txt إذا كان موجودًا
    # -----------------------------------------------------

    cookie_path = Path(COOKIES_FILE)

    if cookie_path.exists() and cookie_path.is_file():

        logger.info(
            "Using cookies file: %s",
            cookie_path,
        )

        options["cookiefile"] = str(
            cookie_path.resolve()
        )

    # -----------------------------------------------------
    # أو استخدام Cookies من المتصفح
    # -----------------------------------------------------

    elif COOKIES_FROM_BROWSER:

        browser_name = (
            COOKIES_FROM_BROWSER
            .strip()
            .lower()
        )

        supported_browsers = {
            "chrome",
            "chromium",
            "firefox",
            "edge",
            "opera",
            "brave",
            "safari",
        }

        if browser_name in supported_browsers:

            logger.info(
                "Using cookies from browser: %s",
                browser_name,
            )

            options["cookiesfrombrowser"] = (
                browser_name,
            )

        else:

            logger.warning(
                "Unsupported browser for "
                "COOKIES_FROM_BROWSER: %s",
                browser_name,
            )

    else:

        logger.info(
            "No cookies configured."
        )

    return options


# =========================================================
# SQLite
# =========================================================

def init_database():
    """
    إنشاء قاعدة البيانات والجداول المطلوبة.
    """

    try:

        with sqlite3.connect(
            DATABASE_PATH
        ) as conn:

            # -------------------------------------------------
            # جدول الإعدادات
            # -------------------------------------------------

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )

            # -------------------------------------------------
            # جدول الجروبات
            # -------------------------------------------------

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

            # -------------------------------------------------
            # جدول الفيديوهات
            # -------------------------------------------------

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

            # -------------------------------------------------
            # منع تكرار الفيديو
            # -------------------------------------------------

            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                idx_videos_platform_video_id
                ON videos (platform, video_id)
                """
            )

            # -------------------------------------------------
            # تسجيل الجروب الافتراضي
            # -------------------------------------------------

            conn.execute(
                """
                INSERT INTO groups (
                    group_id,
                    title,
                    registered_at
                )
                VALUES (?, ?, ?)
                ON CONFLICT(group_id)
                DO NOTHING
                """,
                (
                    str(TARGET_GROUP_ID),
                    "الجروب الافتراضي",
                    datetime.now().isoformat(
                        timespec="seconds"
                    ),
                ),
            )

            conn.commit()

        logger.info(
            "SQLite database initialized: %s",
            DATABASE_PATH,
        )

    except Exception as e:

        logger.exception(
            "Database initialization error: %s",
            e,
        )

        raise


# =========================================================
# إدارة الجروبات
# =========================================================

def get_target_group_id():

    try:

        with sqlite3.connect(
            DATABASE_PATH
        ) as conn:

            row = conn.execute(
                """
                SELECT value
                FROM settings
                WHERE key = 'target_group_id'
                LIMIT 1
                """
            ).fetchone()

            if row and row[0]:

                return int(row[0])

    except Exception as e:

        logger.exception(
            "Get target group error: %s",
            e,
        )

    return TARGET_GROUP_ID


def set_target_group_id(
    group_id: int,
):

    try:

        with sqlite3.connect(
            DATABASE_PATH
        ) as conn:

            conn.execute(
                """
                INSERT INTO settings (
                    key,
                    value
                )
                VALUES (
                    'target_group_id',
                    ?
                )
                ON CONFLICT(key)
                DO UPDATE SET
                    value = excluded.value
                """,
                (
                    str(group_id),
                ),
            )

            conn.commit()

        return True

    except Exception as e:

        logger.exception(
            "Set target group error: %s",
            e,
        )

        return False


def register_group(
    group_id: int,
    title: str = "",
):

    try:

        with sqlite3.connect(
            DATABASE_PATH
        ) as conn:

            conn.execute(
                """
                INSERT INTO groups (
                    group_id,
                    title,
                    registered_at
                )
                VALUES (?, ?, ?)
                ON CONFLICT(group_id)
                DO UPDATE SET
                    title = excluded.title,
                    registered_at = excluded.registered_at
                """,
                (
                    str(group_id),
                    title,
                    datetime.now().isoformat(
                        timespec="seconds"
                    ),
                ),
            )

            conn.commit()

        return True

    except Exception as e:

        logger.exception(
            "Register group error: %s",
            e,
        )

        return False


def get_registered_groups():

    try:

        with sqlite3.connect(
            DATABASE_PATH
        ) as conn:

            rows = conn.execute(
                """
                SELECT group_id, title
                FROM groups
                ORDER BY registered_at DESC
                """
            ).fetchall()

            return rows

    except Exception as e:

        logger.exception(
            "Get registered groups error: %s",
            e,
        )

        return []


# =========================================================
# التحقق من صلاحيات الأدمن
# =========================================================

async def is_authorized_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.effective_user:
        return False

    user_id = update.effective_user.id

    # -----------------------------------------------------
    # مالك البوت
    # -----------------------------------------------------

    if (
        ADMIN_USER_ID
        and str(user_id)
        == str(ADMIN_USER_ID)
    ):
        return True

    # -----------------------------------------------------
    # أدمن الجروب
    # -----------------------------------------------------

    chat = update.effective_chat

    if not chat:
        return False

    if chat.type not in (
        "group",
        "supergroup",
    ):
        return False

    try:

        member = (
            await context.bot.get_chat_member(
                chat.id,
                user_id,
            )
        )

        return member.status in (
            "administrator",
            "creator",
        )

    except Exception as e:

        logger.exception(
            "Admin permission check error: %s",
            e,
        )

        return False


# =========================================================
# لوحة الإدارة
# =========================================================

async def admin_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    authorized = (
        await is_authorized_admin(
            update,
            context,
        )
    )

    if not authorized:

        await update.message.reply_text(
            "⛔ غير مسموح لك باستخدام لوحة الإدارة."
        )

        return

    current_group_id = (
        get_target_group_id()
    )

    groups = get_registered_groups()

    keyboard = []

    for group_id, title in groups:

        display_name = (
            title
            or f"جروب {group_id}"
        )

        prefix = (
            "✅ "
            if str(group_id)
            == str(current_group_id)
            else "📍 "
        )

        callback_data = (
            f"select_group:{group_id}"
        )

        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{prefix}{display_name}",
                    callback_data=callback_data,
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "➕ تسجيل الجروب الحالي",
                callback_data=(
                    "register_current_group"
                ),
            )
        ]
    )

    keyboard.append(
        [
            InlineKeyboardButton(
                "ℹ️ الجروب الحالي",
                callback_data="current_group",
            )
        ]
    )

    await update.message.reply_text(
        "⚙️ إدارة الجروبات\n\n"
        "اختر الجروب الذي تريد استقبال "
        "الفيديوهات فيه:",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# =========================================================
# Callback الإدارة
# =========================================================

async def admin_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not query:
        return

    authorized = (
        await is_authorized_admin(
            update,
            context,
        )
    )

    if not authorized:

        await query.answer(
            "⛔ غير مسموح لك.",
            show_alert=True,
        )

        return

    await query.answer()

    data = query.data or ""

    # =====================================================
    # الجروب الحالي
    # =====================================================

    if data == "current_group":

        group_id = (
            get_target_group_id()
        )

        title = "غير معروف"

        try:

            chat = await context.bot.get_chat(
                group_id
            )

            title = (
                chat.title
                or "غير معروف"
            )

        except Exception as e:

            logger.warning(
                "Could not get target group: %s",
                e,
            )

        await query.edit_message_text(
            "📍 الجروب الحالي:\n\n"
            f"🏷️ الاسم: {title}\n"
            f"🆔 ID: `{group_id}`",
            parse_mode="Markdown",
        )

        return

    # =====================================================
    # تسجيل الجروب الحالي
    # =====================================================

    if data == "register_current_group":

        chat = update.effective_chat

        if not chat:

            await query.edit_message_text(
                "❌ لم أستطع معرفة الجروب الحالي."
            )

            return

        if chat.type not in (
            "group",
            "supergroup",
        ):

            await query.edit_message_text(
                "📌 هذا الخيار يجب استخدامه "
                "من داخل الجروب المطلوب."
            )

            return

        success = register_group(
            chat.id,
            chat.title or "",
        )

        if success:

            await query.edit_message_text(
                "✅ تم تسجيل الجروب بنجاح.\n\n"
                f"🏷️ الاسم: "
                f"{chat.title or 'بدون اسم'}\n"
                f"🆔 ID: `{chat.id}`\n\n"
                "استخدم /admin مرة أخرى "
                "لاختياره كجروب استقبال.",
                parse_mode="Markdown",
            )

        else:

            await query.edit_message_text(
                "❌ حدث خطأ أثناء تسجيل الجروب."
            )

        return

    # =====================================================
    # اختيار جروب
    # =====================================================

    if data.startswith("select_group:"):

        try:

            group_id = int(
                data.split(
                    ":",
                    1,
                )[1]
            )

        except (
            ValueError,
            IndexError,
        ):

            await query.edit_message_text(
                "❌ معرف الجروب غير صحيح."
            )

            return

        groups = get_registered_groups()

        allowed_group_ids = set()

        for row in groups:

            try:

                allowed_group_ids.add(
                    int(row[0])
                )

            except (
                ValueError,
                TypeError,
            ):
                continue

        if group_id not in allowed_group_ids:

            await query.edit_message_text(
                "❌ هذا الجروب غير مسجل في النظام."
            )

            return

        try:

            chat = await context.bot.get_chat(
                group_id
            )

        except Exception as e:

            logger.exception(
                "Selected group access error: %s",
                e,
            )

            await query.edit_message_text(
                "❌ لا أستطيع الوصول إلى هذا الجروب.\n\n"
                "تأكد أن البوت ما زال موجودًا داخله."
            )

            return

        success = set_target_group_id(
            group_id
        )

        if not success:

            await query.edit_message_text(
                "❌ حدث خطأ أثناء حفظ الجروب."
            )

            return

        await query.edit_message_text(
            "✅ تم تغيير جروب استقبال الفيديوهات.\n\n"
            f"🏷️ الاسم: "
            f"{chat.title or 'غير معروف'}\n"
            f"🆔 ID: `{group_id}`",
            parse_mode="Markdown",
        )

        return


# =========================================================
# أمر /setgroup
# =========================================================

async def set_group_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.effective_chat:
        return

    chat = update.effective_chat

    if chat.type not in (
        "group",
        "supergroup",
    ):

        await update.message.reply_text(
            "❌ استخدم /setgroup داخل الجروب المطلوب."
        )

        return

    authorized = (
        await is_authorized_admin(
            update,
            context,
        )
    )

    if not authorized:

        await update.message.reply_text(
            "⛔ غير مسموح لك بتغيير جروب استقبال الفيديوهات."
        )

        return

    group_id = chat.id
    title = chat.title or ""

    register_success = register_group(
        group_id,
        title,
    )

    if not register_success:

        await update.message.reply_text(
            "❌ حدث خطأ أثناء تسجيل الجروب."
        )

        return

    success = set_target_group_id(
        group_id
    )

    if success:

        await update.message.reply_text(
            "✅ تم تعيين هذا الجروب كمجموعة "
            "استقبال الفيديوهات.\n\n"
            f"🏷️ الاسم: "
            f"{title or 'بدون اسم'}\n"
            f"🆔 ID: `{group_id}`",
            parse_mode="Markdown",
        )

    else:

        await update.message.reply_text(
            "❌ حدث خطأ أثناء حفظ الجروب."
        )


# =========================================================
# قاعدة بيانات الفيديوهات
# =========================================================

def get_video_key(
    info,
    url: str,
):

    if not info:
        return None, None

    platform = (
        info.get("extractor_key")
        or info.get("extractor")
        or ""
    )

    video_id = info.get("id")

    if not video_id:

        return (
            platform,
            url,
        )

    return (
        platform,
        str(video_id),
    )


def is_video_downloaded(
    platform: str,
    video_id: str,
    url: str,
):

    try:

        with sqlite3.connect(
            DATABASE_PATH
        ) as conn:

            if platform and video_id:

                row = conn.execute(
                    """
                    SELECT id
                    FROM videos
                    WHERE platform = ?
                    AND video_id = ?
                    LIMIT 1
                    """,
                    (
                        platform,
                        video_id,
                    ),
                ).fetchone()

                if row:
                    return True

            if url:

                row = conn.execute(
                    """
                    SELECT id
                    FROM videos
                    WHERE url = ?
                    LIMIT 1
                    """,
                    (url,),
                ).fetchone()

                if row:
                    return True

            return False

    except Exception as e:

        logger.exception(
            "Database check error: %s",
            e,
        )

        return False


def save_downloaded_video(
    platform: str,
    video_id: str,
    url: str,
    title: str = "",
):

    try:

        with sqlite3.connect(
            DATABASE_PATH
        ) as conn:

            conn.execute(
                """
                INSERT INTO videos (
                    platform,
                    video_id,
                    url,
                    title,
                    downloaded_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    video_id,
                    url,
                    title,
                    datetime.now().isoformat(
                        timespec="seconds"
                    ),
                ),
            )

            conn.commit()

        logger.info(
            "Video saved: %s | %s",
            platform,
            video_id,
        )

        return True

    except sqlite3.IntegrityError:

        logger.info(
            "Video already exists: %s | %s",
            platform,
            video_id,
        )

        return False

    except Exception as e:

        logger.exception(
            "Database save error: %s",
            e,
        )

        return False


# =========================================================
# استخراج الرابط
# =========================================================

URL_REGEX = re.compile(
    r'https?://[^\s<>"\']+',
    re.IGNORECASE,
)


def extract_url(text: str):

    if not text:
        return None

    match = URL_REGEX.search(text)

    if not match:
        return None

    url = match.group(0)

    url = url.rstrip(
        ".,!?;:)]}\"'"
    )

    return url


# =========================================================
# معرفة الموقع
# =========================================================

def get_site_name(url: str):

    url_lower = url.lower()

    sites = {
        "tiktok.com": "TikTok",
        "youtu.be": "YouTube",
        "youtube.com": "YouTube",
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

    for domain, name in sites.items():

        if domain in url_lower:
            return name

    return "الموقع"


# =========================================================
# استخراج معلومات الفيديو
# =========================================================

def get_video_info(url: str):

    ydl_opts = get_common_ydl_options()

    ydl_opts.update(
        {
            "skip_download": True,
            "noplaylist": True,
        }
    )

    try:

        with yt_dlp.YoutubeDL(
            ydl_opts
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=False,
            )

            return info

    except Exception as e:

        logger.warning(
            "Video information extraction failed: %s",
            e,
        )

        return None


# =========================================================
# تحميل الفيديو
# =========================================================

def download_video(
    url: str,
    output_template: str,
):

    """
    تحميل الفيديو.

    لا نضع filesize داخل format selection
    لأن بعض المواقع لا ترسل filesize للصيغ،
    وبالتالي كان yt-dlp ممكن يستبعد كل الصيغ.

    بدل ذلك:
    1. نحاول MP4 حتى 720p.
    2. إذا لم توجد صيغة مناسبة نرجع لأفضل فيديو متاح.
    3. بعد التحميل نفحص حجم الملف فعليًا.
    """

    base_options = get_common_ydl_options()

    formats = [
        (
            "bestvideo[height<=720][ext=mp4]+"
            "bestaudio[ext=m4a]/"
            "best[height<=720][ext=mp4]/"
            "best[height<=720]"
        ),
        (
            "bestvideo[height<=720]+"
            "bestaudio/"
            "best[height<=720]"
        ),
        (
            "best[ext=mp4]/"
            "best"
        ),
    ]

    last_error = None

    for format_string in formats:

        ydl_opts = dict(
            base_options
        )

        ydl_opts.update(
            {
                "format": format_string,

                "merge_output_format": "mp4",

                "outtmpl": output_template,

                "writethumbnail": False,

                "writeinfojson": False,

                "postprocessors": [
                    {
                        "key": "FFmpegVideoConvertor",
                        "preferedformat": "mp4",
                    }
                ],
            }
        )

        try:

            logger.info(
                "Trying download format: %s",
                format_string,
            )

            with yt_dlp.YoutubeDL(
                ydl_opts
            ) as ydl:

                info = ydl.extract_info(
                    url,
                    download=True,
                )

                if not info:
                    continue

                filepath = None

                # -------------------------------------------------
                # requested_downloads
                # -------------------------------------------------

                requested_downloads = (
                    info.get(
                        "requested_downloads"
                    )
                )

                if requested_downloads:

                    for item in requested_downloads:

                        candidate = (
                            item.get(
                                "filepath"
                            )
                        )

                        if (
                            candidate
                            and os.path.exists(
                                candidate
                            )
                        ):

                            filepath = candidate
                            break

                # -------------------------------------------------
                # prepared filename
                # -------------------------------------------------

                if not filepath:

                    prepared_path = (
                        ydl.prepare_filename(
                            info
                        )
                    )

                    if os.path.exists(
                        prepared_path
                    ):

                        filepath = (
                            prepared_path
                        )

                    else:

                        possible_extensions = [
                            ".mp4",
                            ".mkv",
                            ".webm",
                            ".mov",
                            ".avi",
                        ]

                        for ext in possible_extensions:

                            candidate = (
                                os.path.splitext(
                                    prepared_path
                                )[0]
                                + ext
                            )

                            if os.path.exists(
                                candidate
                            ):

                                filepath = candidate
                                break

                # -------------------------------------------------
                # البحث عن آخر ملف داخل downloads
                # -------------------------------------------------

                if not filepath:

                    try:

                        template_prefix = (
                            Path(output_template)
                            .name
                            .split(
                                ".%(ext)s"
                            )[0]
                        )

                        candidates = list(
                            DOWNLOAD_DIR.glob(
                                f"{template_prefix}.*"
                            )
                        )

                        candidates = [
                            p
                            for p in candidates
                            if p.is_file()
                        ]

                        if candidates:

                            filepath = str(
                                max(
                                    candidates,
                                    key=lambda p: p.stat().st_mtime,
                                )
                            )

                    except Exception:
                        pass

                if filepath:

                    return (
                        filepath,
                        info,
                        None,
                    )

                last_error = (
                    "تم التحميل ولكن لم أجد الملف الناتج."
                )

        except Exception as e:

            last_error = str(e)

            logger.warning(
                "Download attempt failed: %s",
                e,
            )

            # نحاول format آخر
            continue

    return (
        None,
        None,
        last_error,
    )


# =========================================================
# رسالة خطأ مفهومة
# =========================================================

def get_download_error_message(
    error,
    site_name: str,
):

    error_text = (
        str(error or "")
        .lower()
    )

    # -----------------------------------------------------
    # Facebook / login / cookies
    # -----------------------------------------------------

    if (
        "login" in error_text
        or "cookies" in error_text
        or "private" in error_text
        or "sign in" in error_text
    ):

        return (
            "❌ الموقع رفض الوصول إلى الفيديو.\n\n"
            f"🌐 المصدر: {site_name}\n\n"
            "غالبًا الفيديو يحتاج تسجيل دخول "
            "أو Cookies صالحة.\n\n"
            "إذا كان الفيديو يعمل عندك في المتصفح، "
            "ضع ملف cookies.txt الصحيح في السيرفر "
            "أو فعّل COOKIES_FROM_BROWSER."
        )

    # -----------------------------------------------------
    # TikTok extraction
    # -----------------------------------------------------

    if "tiktok" in error_text:

        return (
            "❌ لم أستطع استخراج فيديو TikTok.\n\n"
            "قد يكون الرابط مختصرًا أو أن TikTok "
            "غيّر طريقة الوصول للفيديو.\n\n"
            "تأكد أولًا أن yt-dlp محدث إلى آخر إصدار."
        )

    # -----------------------------------------------------
    # Facebook
    # -----------------------------------------------------

    if "facebook" in error_text:

        return (
            "❌ لم أستطع تحميل فيديو Facebook.\n\n"
            "بعض روابط Facebook تحتاج Cookies "
            "من جلسة تسجيل الدخول، خصوصًا روابط "
            "share/reels.\n\n"
            "ضع cookies.txt صالحًا في السيرفر "
            "ثم أعد المحاولة."
        )

    # -----------------------------------------------------
    # Generic
    # -----------------------------------------------------

    return (
        "❌ لم أستطع تحميل الفيديو.\n\n"
        f"🌐 المصدر: {site_name}\n\n"
        "قد يكون الرابط غير مدعوم، "
        "أو الفيديو خاص، "
        "أو الموقع يحتاج تسجيل دخول، "
        "أو أن الموقع غيّر طريقة الحماية."
    )


# =========================================================
# معالجة الرسائل
# =========================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    # -----------------------------------------------------
    # دعم النص العادي + Caption
    # -----------------------------------------------------

    text = (
        update.message.text
        or update.message.caption
        or ""
    )

    if not text:
        return

    # -----------------------------------------------------
    # استخراج الرابط
    # -----------------------------------------------------

    url = extract_url(text)

    if not url:
        return

    site_name = get_site_name(
        url
    )

    logger.info(
        "Received URL: %s",
        url,
    )

    # -----------------------------------------------------
    # رسالة الحالة
    # -----------------------------------------------------

    try:

        status_msg = (
            await update.message.reply_text(
                f"⏳ جاري معالجة الفيديو من "
                f"{site_name}..."
            )
        )

    except Exception as e:

        logger.exception(
            "Could not send status message: %s",
            e,
        )

        return

    # -----------------------------------------------------
    # اسم ملف فريد
    # -----------------------------------------------------

    user_id = (
        update.effective_user.id
        if update.effective_user
        else 0
    )

    message_id = (
        update.message.message_id
    )

    filename = (
        f"video_{user_id}_{message_id}.%(ext)s"
    )

    output_template = str(
        DOWNLOAD_DIR / filename
    )

    file_path = None

    try:

        loop = asyncio.get_running_loop()

        # =================================================
        # محاولة استخراج المعلومات
        # =================================================

        info = await loop.run_in_executor(
            None,
            get_video_info,
            url,
        )

        # -------------------------------------------------
        # إذا نجحت المعلومات
        # -------------------------------------------------

        platform = ""
        video_id = ""

        if info:

            platform, video_id = (
                get_video_key(
                    info,
                    url,
                )
            )

            # -------------------------------------------------
            # منع التكرار
            # -------------------------------------------------

            already_downloaded = (
                is_video_downloaded(
                    platform,
                    video_id,
                    url,
                )
            )

            if already_downloaded:

                await status_msg.edit_text(
                    "ℹ️ هذا الفيديو تم تحميله "
                    "وإرساله من قبل."
                )

                return

        else:

            # -------------------------------------------------
            # مهم:
            #
            # لا نوقف العملية هنا.
            #
            # نحاول التحميل مباشرة مرة أخرى.
            # -------------------------------------------------

            logger.warning(
                "Could not extract info first. "
                "Trying direct download."
            )

        # =================================================
        # تحميل الفيديو
        # =================================================

        await status_msg.edit_text(
            f"⬇️ جاري تحميل الفيديو من "
            f"{site_name}..."
        )

        (
            file_path,
            downloaded_info,
            download_error,
        ) = await loop.run_in_executor(
            None,
            download_video,
            url,
            output_template,
        )

        # -------------------------------------------------
        # فشل التحميل
        # -------------------------------------------------

        if (
            not file_path
            or not os.path.exists(
                file_path
            )
        ):

            error_message = (
                get_download_error_message(
                    download_error,
                    site_name,
                )
            )

            await status_msg.edit_text(
                error_message
            )

            return

        # =================================================
        # تحديث info
        # =================================================

        if downloaded_info:

            info = downloaded_info

        # =================================================
        # الحصول على مفتاح الفيديو
        # =================================================

        if info:

            platform, video_id = (
                get_video_key(
                    info,
                    url,
                )
            )

        # =================================================
        # حجم الملف
        # =================================================

        file_size = os.path.getsize(
            file_path
        )

        logger.info(
            "Downloaded file: %s | Size: %.2f MB",
            file_path,
            file_size / (
                1024 * 1024
            ),
        )

        # =================================================
        # التحقق من الحجم
        # =================================================

        if file_size > MAX_FILE_SIZE:

            await status_msg.edit_text(
                "⚠️ تم تحميل الفيديو بنجاح، "
                "لكن حجمه كبير جدًا للإرسال عبر Telegram.\n\n"
                f"📦 الحجم: "
                f"{file_size / (1024 * 1024):.1f} MB\n"
                f"📌 الحد الحالي: "
                f"{MAX_FILE_SIZE / (1024 * 1024):.0f} MB"
            )

            return

        # =================================================
        # عنوان الفيديو
        # =================================================

        title = ""

        if info:

            title = (
                info.get("title")
                or ""
            )

        title = title[:800]

        caption = "🎬 تم تحميل الفيديو"

        if title:

            caption += (
                f"\n\n{title}"
            )

        # =================================================
        # الجروب المستهدف
        # =================================================

        target_group_id = (
            get_target_group_id()
        )

        if not target_group_id:

            await status_msg.edit_text(
                "❌ لم يتم تحديد جروب استقبال الفيديوهات.\n\n"
                "استخدم /admin لإدارة الجروب."
            )

            return

        # =================================================
        # إرسال الفيديو
        # =================================================

        await status_msg.edit_text(
            "📤 جاري إرسال الفيديو إلى الجروب..."
        )

        with open(
            file_path,
            "rb",
        ) as video_file:

            await context.bot.send_video(
                chat_id=target_group_id,
                video=video_file,
                caption=caption,
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )

        # =================================================
        # حفظ الفيديو
        # =================================================

        save_downloaded_video(
            platform,
            video_id,
            url,
            title,
        )

        # =================================================
        # نجاح
        # =================================================

        await status_msg.edit_text(
            "✅ تم تحميل الفيديو وإرساله "
            "إلى المجموعة بنجاح.\n\n"
            f"🌐 المصدر: {site_name}"
        )

    except Exception as e:

        logger.exception(
            "Processing error: %s",
            e,
        )

        try:

            await status_msg.edit_text(
                "⚠️ حدث خطأ أثناء معالجة الفيديو.\n\n"
                f"🌐 المصدر: {site_name}\n\n"
                "حاول إرسال الرابط مرة أخرى."
            )

        except Exception:
            pass

    finally:

        # =================================================
        # حذف الملف المؤقت
        # =================================================

        if (
            file_path
            and os.path.exists(
                file_path
            )
        ):

            try:

                os.remove(
                    file_path
                )

                logger.info(
                    "Temporary file deleted: %s",
                    file_path,
                )

            except Exception as e:

                logger.warning(
                    "Could not delete file: %s",
                    e,
                )


# =========================================================
# Error Handler
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.exception(
        "Telegram application error: %s",
        context.error,
    )


# =========================================================
# تشغيل البوت
# =========================================================

def main():

    # -----------------------------------------------------
    # BOT TOKEN
    # -----------------------------------------------------

    if not BOT_TOKEN:

        raise ValueError(
            "BOT_TOKEN is not set in environment variables."
        )

    # -----------------------------------------------------
    # DATABASE
    # -----------------------------------------------------

    init_database()

    # -----------------------------------------------------
    # عرض إعدادات Cookies
    # -----------------------------------------------------

    logger.info(
        "Cookies file configured: %s",
        (
            str(
                Path(COOKIES_FILE).resolve()
            )
            if COOKIES_FILE
            else "None"
        ),
    )

    if Path(COOKIES_FILE).exists():

        logger.info(
            "Cookies file FOUND."
        )

    else:

        logger.info(
            "Cookies file NOT FOUND."
        )

    if COOKIES_FROM_BROWSER:

        logger.info(
            "Browser cookies enabled: %s",
            COOKIES_FROM_BROWSER,
        )

    # -----------------------------------------------------
    # إنشاء التطبيق
    # -----------------------------------------------------

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # =====================================================
    # أوامر الإدارة
    # =====================================================

    app.add_handler(
        CommandHandler(
            "admin",
            admin_panel,
        )
    )

    app.add_handler(
        CommandHandler(
            "setgroup",
            set_group_command,
        )
    )

    # =====================================================
    # أزرار الإدارة
    # =====================================================

    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
        )
    )

    # =====================================================
    # الرسائل النصية
    # =====================================================

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        )
    )

    # =====================================================
    # Error Handler
    # =====================================================

    app.add_error_handler(
        error_handler
    )

    # =====================================================
    # Start
    # =====================================================

    logger.info(
        "Universal Video Downloader Bot started."
    )

    app.run_polling()


# =========================================================
# Start
# =========================================================

if __name__ == "__main__":
    main()
