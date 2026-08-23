import os
import re
import asyncio
import logging
import sqlite3
import shutil
import subprocess

from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

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
TARGET_GROUP_ID = -1004468483224

# =========================================================
# Cookies
# =========================================================
#
# مثال:
# COOKIES_FILE=/home/bot/cookies.txt
#
# اترك المتغير فارغًا إذا لم تكن بحاجة إلى Cookies.
#

COOKIES_FILE = os.getenv(
    "COOKIES_FILE",
    "",
).strip()

if COOKIES_FILE:
    COOKIES_FILE = str(
        Path(COOKIES_FILE).expanduser()
    )

# =========================================================
# مجلد التحميل
# =========================================================

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

# =========================================================
# قاعدة البيانات
# =========================================================

DATABASE_PATH = Path("bot.db")

# =========================================================
# الحد الأقصى للملف
# =========================================================

MAX_FILE_SIZE = 49 * 1024 * 1024

# حجم نستهدفه عند الضغط
COMPRESSION_TARGET_SIZE = 47 * 1024 * 1024


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
# أدوات عامة
# =========================================================

def ffmpeg_exists():
    """
    التأكد من وجود FFmpeg.
    """

    return shutil.which("ffmpeg") is not None


def ffprobe_exists():
    """
    التأكد من وجود ffprobe.
    """

    return shutil.which("ffprobe") is not None


def cleanup_download_files(prefix: str):
    """
    حذف جميع الملفات المؤقتة المرتبطة بعملية تحميل معينة.
    """

    try:

        for path in DOWNLOAD_DIR.glob(
            f"{prefix}*"
        ):

            if path.is_file():

                try:
                    path.unlink()

                    logger.info(
                        "Temporary file deleted: %s",
                        path,
                    )

                except Exception as e:

                    logger.warning(
                        "Could not delete %s: %s",
                        path,
                        e,
                    )

    except Exception as e:

        logger.warning(
            "Cleanup error: %s",
            e,
        )


def normalize_url(url: str):
    """
    تنظيف الرابط فقط بدون تغيير محتواه الأساسي.
    """

    if not url:
        return url

    url = url.strip()

    # إزالة علامات الترقيم التي قد تأتي بعد الرابط
    url = url.rstrip(
        ".,!?;:)]}\"'"
    )

    return url


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
            # Index للرابط
            # -------------------------------------------------

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_videos_url
                ON videos (url)
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
    """
    الحصول على الجروب المستهدف.
    """

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
    """
    حفظ الجروب المستهدف.
    """

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
                (str(group_id),),
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
    """
    تسجيل الجروب في قاعدة البيانات.
    """

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
    """
    الحصول على الجروبات المسجلة.
    """

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
    """
    السماح لمالك البوت أو أدمن الجروب الحالي.
    """

    if not update.effective_user:
        return False

    user_id = update.effective_user.id

    # -------------------------------------------------
    # مالك البوت
    # -------------------------------------------------

    if (
        ADMIN_USER_ID
        and str(user_id)
        == str(ADMIN_USER_ID)
    ):
        return True

    # -------------------------------------------------
    # أدمن الجروب
    # -------------------------------------------------

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

        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{prefix}{display_name}",
                    callback_data=(
                        f"select_group:{group_id}"
                    ),
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

    # =================================================
    # الجروب الحالي
    # =================================================

    if data == "current_group":

        group_id = get_target_group_id()

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

    # =================================================
    # تسجيل الجروب الحالي
    # =================================================

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
                "📌 استخدم هذا الخيار "
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

    # =================================================
    # اختيار جروب
    # =================================================

    if data.startswith(
        "select_group:"
    ):

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
                "❌ هذا الجروب غير مسجل."
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
                "تأكد أن البوت موجود داخله."
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
    """
    الحصول على platform + video_id.
    """

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
    """
    التحقق من أن الفيديو تم إرساله سابقًا.
    """

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
    """
    حفظ الفيديو بعد نجاح الإرسال.
    """

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
    """
    استخراج أول رابط.
    """

    if not text:
        return None

    match = URL_REGEX.search(text)

    if not match:
        return None

    return normalize_url(
        match.group(0)
    )


# =========================================================
# معرفة الموقع
# =========================================================

def get_site_name(url: str):
    """
    معرفة الموقع بشكل تقريبي.
    """

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
# خيارات yt-dlp المشتركة
# =========================================================

def get_common_ydl_options():
    """
    الخيارات المشتركة لكل عمليات yt-dlp.
    """

    options = {
        "quiet": True,
        "no_warnings": True,

        # مهم للروابط التي تحتوي Playlist
        "noplaylist": True,

        # IPv4
        "source_address": "0.0.0.0",

        # إعادة المحاولة
        "retries": 5,
        "fragment_retries": 5,

        # مهلة الاتصال
        "socket_timeout": 30,

        # User Agent حديث
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 "
                "Safari/537.36"
            ),
            "Accept-Language": (
                "en-US,en;q=0.9,ar;q=0.8"
            ),
        },
    }

    # =====================================================
    # Cookies
    # =====================================================

    if COOKIES_FILE:

        cookies_path = Path(
            COOKIES_FILE
        )

        if cookies_path.exists():

            options["cookiefile"] = str(
                cookies_path
            )

            logger.info(
                "Using cookies file: %s",
                cookies_path,
            )

        else:

            logger.warning(
                "COOKIES_FILE was configured "
                "but file does not exist: %s",
                cookies_path,
            )

    return options


# =========================================================
# قراءة معلومات الفيديو
# =========================================================

def get_video_info(url: str):
    """
    استخراج معلومات الفيديو بدون تحميله.

    نجرب أكثر من إعداد بدل أن يتوقف البوت
    من أول محاولة.
    """

    url = normalize_url(url)

    base_options = (
        get_common_ydl_options()
    )

    attempts = [
        {
            **base_options,
            "skip_download": True,
        },
        {
            **base_options,
            "skip_download": True,
            "geo_bypass": True,
        },
    ]

    last_error = None

    for index, options in enumerate(
        attempts,
        start=1,
    ):

        try:

            logger.info(
                "Extracting video information "
                "attempt %s: %s",
                index,
                url,
            )

            with yt_dlp.YoutubeDL(
                options
            ) as ydl:

                info = ydl.extract_info(
                    url,
                    download=False,
                )

                if info:

                    return info

        except Exception as e:

            last_error = e

            logger.warning(
                "Info extraction attempt %s failed: %s",
                index,
                e,
            )

    logger.error(
        "All info extraction attempts failed: %s",
        last_error,
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
    تحميل الفيديو بثلاث درجات جودة.

    720p
    ثم 480p
    ثم 360p

    وبعد كل محاولة يتم فحص حجم الملف.
    """

    url = normalize_url(url)

    base_options = (
        get_common_ydl_options()
    )

    # -----------------------------------------------------
    # درجات الجودة
    # -----------------------------------------------------

    formats = [
        (
            "720p",
            (
                "bv*[height<=720]"
                "+ba/"
                "b[height<=720]"
            ),
        ),
        (
            "480p",
            (
                "bv*[height<=480]"
                "+ba/"
                "b[height<=480]"
            ),
        ),
        (
            "360p",
            (
                "bv*[height<=360]"
                "+ba/"
                "b[height<=360]"
            ),
        ),
    ]

    output_path = None
    info = None
    last_error = None

    # اسم العملية من الـ template
    output_path_obj = Path(
        output_template
    )

    prefix = (
        output_path_obj.name
        .split(".%(", 1)[0]
    )

    for quality_name, format_string in formats:

        cleanup_download_files(
            prefix
        )

        options = {
            **base_options,

            "format": format_string,

            "merge_output_format": "mp4",

            "outtmpl": output_template,

            # عدم إنشاء ملفات إضافية
            "writethumbnail": False,
            "writeinfojson": False,

            # عدم حفظ Playlist
            "noplaylist": True,
        }

        try:

            logger.info(
                "Downloading using %s: %s",
                quality_name,
                url,
            )

            with yt_dlp.YoutubeDL(
                options
            ) as ydl:

                info = ydl.extract_info(
                    url,
                    download=True,
                )

                if not info:

                    continue

                # -------------------------------------------------
                # البحث عن الملف النهائي
                # -------------------------------------------------

                filepath = None

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
                # fallback
                # -------------------------------------------------

                if not filepath:

                    prepared_path = (
                        ydl.prepare_filename(
                            info
                        )
                    )

                    possible_paths = [
                        prepared_path,
                        (
                            os.path.splitext(
                                prepared_path
                            )[0]
                            + ".mp4"
                        ),
                        (
                            os.path.splitext(
                                prepared_path
                            )[0]
                            + ".mkv"
                        ),
                        (
                            os.path.splitext(
                                prepared_path
                            )[0]
                            + ".webm"
                        ),
                    ]

                    for candidate in possible_paths:

                        if os.path.exists(
                            candidate
                        ):

                            filepath = candidate
                            break

                if not filepath:

                    logger.warning(
                        "No output file found "
                        "after %s download.",
                        quality_name,
                    )

                    continue

                # -------------------------------------------------
                # حجم الملف
                # -------------------------------------------------

                file_size = os.path.getsize(
                    filepath
                )

                logger.info(
                    "Downloaded %s: %s | %.2f MB",
                    quality_name,
                    filepath,
                    file_size
                    / (
                        1024 * 1024
                    ),
                )

                # -------------------------------------------------
                # الحجم مناسب
                # -------------------------------------------------

                if file_size <= MAX_FILE_SIZE:

                    return filepath, info

                # -------------------------------------------------
                # الملف كبير
                # نجرب جودة أقل
                # -------------------------------------------------

                logger.warning(
                    "File from %s is too large: %.2f MB",
                    quality_name,
                    file_size
                    / (
                        1024 * 1024
                    ),
                )

                # لا نحتاج الملف الكبير
                try:

                    os.remove(filepath)

                except Exception:
                    pass

        except Exception as e:

            last_error = e

            logger.warning(
                "Download attempt %s failed: %s",
                quality_name,
                e,
            )

            continue

    # ---------------------------------------------------------
    # فشلت كل الجودات
    # ---------------------------------------------------------

    logger.error(
        "All download attempts failed: %s",
        last_error,
    )

    return None, None


# =========================================================
# معرفة مدة الفيديو
# =========================================================

def get_video_duration(
    file_path: str,
):
    """
    الحصول على مدة الفيديو باستخدام ffprobe.
    """

    if not ffprobe_exists():
        return None

    try:

        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return None

        value = result.stdout.strip()

        if not value:
            return None

        duration = float(value)

        if duration <= 0:
            return None

        return duration

    except Exception as e:

        logger.warning(
            "Could not get video duration: %s",
            e,
        )

        return None


# =========================================================
# ضغط الفيديو
# =========================================================

def compress_video(
    input_path: str,
):
    """
    ضغط الفيديو إذا كان أكبر من الحد.

    يتم إنشاء ملف جديد بحجم مستهدف أقل من حد Telegram.
    """

    if not ffmpeg_exists():

        logger.warning(
            "FFmpeg is not installed. "
            "Cannot compress video."
        )

        return None

    if not os.path.exists(input_path):
        return None

    file_size = os.path.getsize(
        input_path
    )

    if file_size <= MAX_FILE_SIZE:
        return input_path

    duration = get_video_duration(
        input_path
    )

    if not duration:

        logger.warning(
            "Could not determine video duration."
        )

        return None

    # ---------------------------------------------------------
    # حساب bitrate مناسب
    # ---------------------------------------------------------

    target_bits = (
        COMPRESSION_TARGET_SIZE
        * 8
    )

    # الصوت 96 kbps تقريبًا
    audio_bitrate = 96_000

    total_bitrate = (
        target_bits / duration
    )

    video_bitrate = (
        total_bitrate
        - audio_bitrate
    )

    # لا ننزل عن 150 kbps
    video_bitrate = max(
        int(video_bitrate),
        150_000,
    )

    # ---------------------------------------------------------
    # اسم الملف
    # ---------------------------------------------------------

    input_file = Path(
        input_path
    )

    output_path = (
        input_file.parent
        / (
            input_file.stem
            + "_compressed.mp4"
        )
    )

    # لو موجود من محاولة قديمة
    try:

        if output_path.exists():
            output_path.unlink()

    except Exception:
        pass

    bitrate_k = max(
        int(video_bitrate / 1000),
        150,
    )

    logger.info(
        "Compressing video: %s -> %s kbps",
        input_path,
        bitrate_k,
    )

    try:

        command = [
            "ffmpeg",
            "-y",

            "-i",
            str(input_path),

            # فيديو H264
            "-c:v",
            "libx264",

            "-preset",
            "veryfast",

            "-b:v",
            f"{bitrate_k}k",

            "-maxrate",
            f"{bitrate_k}k",

            "-bufsize",
            f"{bitrate_k * 2}k",

            # صوت AAC
            "-c:a",
            "aac",

            "-b:a",
            "96k",

            # توافق أفضل مع Telegram
            "-movflags",
            "+faststart",

            # عدم تغيير الحجم أكثر من اللازم
            "-vf",
            (
                "scale="
                "'min(720,iw)':"
                "'-2'"
            ),

            str(output_path),
        ]

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1800,
        )

        if result.returncode != 0:

            logger.error(
                "FFmpeg compression failed: %s",
                result.stderr[-3000:],
            )

            return None

        if not output_path.exists():

            return None

        compressed_size = (
            output_path.stat().st_size
        )

        logger.info(
            "Compressed video size: %.2f MB",
            compressed_size
            / (
                1024 * 1024
            ),
        )

        if compressed_size <= MAX_FILE_SIZE:

            return str(output_path)

        logger.warning(
            "Compressed file is still too large."
        )

        return None

    except Exception as e:

        logger.exception(
            "Compression error: %s",
            e,
        )

        return None


# =========================================================
# رسالة خطأ ذكية
# =========================================================

def get_download_error_message(
    site_name: str,
):
    """
    رسالة واضحة حسب نوع المشكلة.
    """

    cookies_hint = ""

    if site_name in (
        "Facebook",
        "Instagram",
        "TikTok",
    ):

        cookies_hint = (
            "\n\n"
            "💡 إذا كان الفيديو يتطلب تسجيل دخول، "
            "يمكنك إضافة ملف Cookies صالح للسيرفر "
            "عن طريق COOKIES_FILE."
        )

    return (
        f"❌ لم أستطع تحميل فيديو {site_name}.\n\n"
        "الأسباب المحتملة:\n"
        "• الرابط خاص أو يتطلب تسجيل دخول.\n"
        "• الرابط انتهت صلاحيته.\n"
        "• الموقع يمنع الوصول من السيرفر.\n"
        "• الفيديو غير متاح في بلد السيرفر.\n"
        "• الموقع غيّر طريقة تشغيل الفيديو."
        f"{cookies_hint}"
    )


# =========================================================
# معالجة الرسائل
# =========================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    استقبال الرسائل التي تحتوي على روابط.
    """

    if not update.message:
        return

    # =====================================================
    # دعم النص والـ Caption
    # =====================================================

    text = (
        update.message.text
        or update.message.caption
        or ""
    )

    if not text:
        return

    # =====================================================
    # استخراج الرابط
    # =====================================================

    url = extract_url(text)

    if not url:
        return

    # =====================================================
    # معرفة الموقع
    # =====================================================

    site_name = get_site_name(
        url
    )

    logger.info(
        "Received URL: %s",
        url,
    )

    # =====================================================
    # رسالة الحالة
    # =====================================================

    try:

        status_msg = (
            await update.message.reply_text(
                f"🔎 جاري قراءة رابط "
                f"{site_name}..."
            )
        )

    except Exception as e:

        logger.exception(
            "Could not send status message: %s",
            e,
        )

        return

    # =====================================================
    # اسم ملف فريد
    # =====================================================

    user_id = (
        update.effective_user.id
        if update.effective_user
        else 0
    )

    message_id = (
        update.message.message_id
    )

    file_prefix = (
        f"video_{user_id}_{message_id}"
    )

    filename = (
        f"{file_prefix}.%(ext)s"
    )

    output_template = str(
        DOWNLOAD_DIR / filename
    )

    file_path = None
    compressed_path = None

    try:

        loop = asyncio.get_running_loop()

        # =================================================
        # قراءة معلومات الفيديو
        # =================================================

        info = await loop.run_in_executor(
            None,
            get_video_info,
            url,
        )

        if not info:

            await status_msg.edit_text(
                get_download_error_message(
                    site_name
                )
            )

            return

        # =================================================
        # الحصول على مفتاح الفيديو
        # =================================================

        platform, video_id = (
            get_video_key(
                info,
                url,
            )
        )

        logger.info(
            "Video key: %s | %s",
            platform,
            video_id,
        )

        # =================================================
        # منع التكرار
        # =================================================

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

        # =================================================
        # بدء التحميل
        # =================================================

        await status_msg.edit_text(
            f"⬇️ جاري تحميل فيديو "
            f"{site_name}...\n\n"
            "الجودة: 720p ثم أقل تلقائيًا "
            "إذا كان الملف كبيرًا."
        )

        file_path, download_info = (
            await loop.run_in_executor(
                None,
                download_video,
                url,
                output_template,
            )
        )

        # =================================================
        # التحقق من التحميل
        # =================================================

        if (
            not file_path
            or not os.path.exists(
                file_path
            )
        ):

            await status_msg.edit_text(
                get_download_error_message(
                    site_name
                )
            )

            return

        # =================================================
        # حجم الملف
        # =================================================

        file_size = os.path.getsize(
            file_path
        )

        logger.info(
            "Final downloaded file: %s | %.2f MB",
            file_path,
            file_size
            / (
                1024 * 1024
            ),
        )

        # =================================================
        # ضغط إذا كان كبيرًا
        # =================================================

        if file_size > MAX_FILE_SIZE:

            await status_msg.edit_text(
                "🗜️ حجم الفيديو كبير.\n"
                "جاري ضغطه تلقائيًا ليصبح مناسبًا "
                "للإرسال عبر Telegram..."
            )

            compressed_path = (
                await loop.run_in_executor(
                    None,
                    compress_video,
                    file_path,
                )
            )

            if compressed_path:

                # حذف الملف الأصلي
                try:

                    if (
                        os.path.exists(
                            file_path
                        )
                        and compressed_path
                        != file_path
                    ):

                        os.remove(
                            file_path
                        )

                except Exception as e:

                    logger.warning(
                        "Could not remove original "
                        "after compression: %s",
                        e,
                    )

                file_path = compressed_path

                file_size = (
                    os.path.getsize(
                        file_path
                    )
                )

            else:

                await status_msg.edit_text(
                    "⚠️ تم تحميل الفيديو، "
                    "لكن حجمه ما زال أكبر من الحد "
                    "المسموح به للإرسال.\n\n"
                    f"📦 الحجم: "
                    f"{file_size / (1024 * 1024):.1f} MB\n"
                    f"📌 الحد: "
                    f"{MAX_FILE_SIZE / (1024 * 1024):.0f} MB\n\n"
                    "جرّب فيديو أقصر أو بجودة أقل."
                )

                return

        # =================================================
        # فحص نهائي للحجم
        # =================================================

        if (
            not file_path
            or not os.path.exists(
                file_path
            )
        ):

            await status_msg.edit_text(
                "❌ لم يتم العثور على الملف النهائي."
            )

            return

        file_size = os.path.getsize(
            file_path
        )

        if file_size > MAX_FILE_SIZE:

            await status_msg.edit_text(
                "❌ حجم الفيديو ما زال أكبر "
                "من الحد المسموح به."
            )

            return

        # =================================================
        # عنوان الفيديو
        # =================================================

        title = ""

        final_info = (
            download_info
            or info
        )

        if final_info:

            title = (
                final_info.get(
                    "title"
                )
                or ""
            )

        title = title[:800]

        caption = (
            "🎬 تم تحميل الفيديو"
        )

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

        send_success = False

        # -------------------------------------------------
        # المحاولة الأولى: send_video
        # -------------------------------------------------

        try:

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

            send_success = True

        except Exception as e:

            logger.warning(
                "send_video failed: %s",
                e,
            )

        # -------------------------------------------------
        # fallback: send_document
        # -------------------------------------------------

        if not send_success:

            await status_msg.edit_text(
                "📤 Telegram رفض إرسال الفيديو "
                "بالطريقة العادية.\n"
                "جاري المحاولة كملف..."
            )

            try:

                with open(
                    file_path,
                    "rb",
                ) as video_file:

                    await context.bot.send_document(
                        chat_id=target_group_id,
                        document=video_file,
                        caption=caption,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                        pool_timeout=30,
                    )

                send_success = True

            except Exception as e:

                logger.exception(
                    "send_document also failed: %s",
                    e,
                )

        # =================================================
        # فشل الإرسال
        # =================================================

        if not send_success:

            await status_msg.edit_text(
                "❌ تم تحميل الفيديو بنجاح، "
                "لكن فشل إرساله إلى الجروب.\n\n"
                "تأكد أن البوت موجود في الجروب "
                "ولديه صلاحية إرسال الفيديوهات والملفات."
            )

            return

        # =================================================
        # حفظ الفيديو بعد نجاح الإرسال
        # =================================================

        save_downloaded_video(
            platform,
            video_id,
            url,
            title,
        )

        # =================================================
        # النجاح
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
                "⚠️ حدث خطأ غير متوقع أثناء "
                "معالجة الفيديو.\n\n"
                "راجع Logs السيرفر لمعرفة السبب."
            )

        except Exception:
            pass

    finally:

        # =================================================
        # تنظيف الملفات المؤقتة
        # =================================================

        cleanup_download_files(
            file_prefix
        )


# =========================================================
# Error Handler
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    تسجيل أخطاء Telegram.
    """

    logger.exception(
        "Telegram application error: %s",
        context.error,
    )


# =========================================================
# تشغيل البوت
# =========================================================

def main():

    # -------------------------------------------------
    # BOT TOKEN
    # -------------------------------------------------

    if not BOT_TOKEN:

        raise ValueError(
            "BOT_TOKEN is not set in environment variables."
        )

    # -------------------------------------------------
    # Database
    # -------------------------------------------------

    init_database()

    # -------------------------------------------------
    # FFmpeg
    # -------------------------------------------------

    if ffmpeg_exists():

        logger.info(
            "FFmpeg detected."
        )

    else:

        logger.warning(
            "FFmpeg was not detected. "
            "Video merging/compression may fail."
        )

    # -------------------------------------------------
    # Cookies
    # -------------------------------------------------

    if COOKIES_FILE:

        if Path(
            COOKIES_FILE
        ).exists():

            logger.info(
                "Cookies file detected."
            )

        else:

            logger.warning(
                "Cookies file configured "
                "but not found."
            )

    else:

        logger.info(
            "No cookies file configured."
        )

    # -------------------------------------------------
    # إنشاء التطبيق
    # -------------------------------------------------

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # =================================================
    # أوامر الإدارة
    # =================================================

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

    # =================================================
    # أزرار لوحة الإدارة
    # =================================================

    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
        )
    )

    # =================================================
    # الرسائل التي تحتوي على روابط
    #
    # TEXT + CAPTION
    # =================================================

    app.add_handler(
        MessageHandler(
            (
                filters.TEXT
                | filters.CAPTION
            )
            & ~filters.COMMAND,
            handle_message,
        )
    )

    # =================================================
    # Error Handler
    # =================================================

    app.add_error_handler(
        error_handler
    )

    # =================================================
    # بدء البوت
    # =================================================

    logger.info(
        "Universal Video Downloader Bot started."
    )

    app.run_polling()


# =========================================================
# Start
# =========================================================

if __name__ == "__main__":
    main()
