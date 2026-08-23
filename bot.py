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
# مثال:
# ADMIN_USER_ID=123456789
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")

# الجروب الافتراضي
TARGET_GROUP_ID = -1004468483224

# مجلد تحميل الفيديوهات
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# قاعدة البيانات
DATABASE_PATH = Path("bot.db")

# الحد الأقصى للملف
# نترك هامشًا تحت حد Telegram
MAX_FILE_SIZE = 49 * 1024 * 1024


# =========================================================
# Logging
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# SQLite
# =========================================================

def init_database():
    """
    إنشاء قاعدة البيانات والجداول المطلوبة.
    """

    try:
        with sqlite3.connect(DATABASE_PATH) as conn:

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
            # منع تكرار الفيديو حسب المنصة + ID
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
    """
    الحصول على الجروب المستهدف من قاعدة البيانات.
    """

    try:
        with sqlite3.connect(DATABASE_PATH) as conn:

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


def set_target_group_id(group_id: int):
    """
    حفظ الجروب المستهدف.
    """

    try:
        with sqlite3.connect(DATABASE_PATH) as conn:

            conn.execute(
                """
                INSERT INTO settings (key, value)
                VALUES ('target_group_id', ?)
                ON CONFLICT(key)
                DO UPDATE SET value = excluded.value
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
        with sqlite3.connect(DATABASE_PATH) as conn:

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
        with sqlite3.connect(DATABASE_PATH) as conn:

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
        and str(user_id) == str(ADMIN_USER_ID)
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
        member = await context.bot.get_chat_member(
            chat.id,
            user_id,
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
    """
    عرض لوحة إدارة الجروبات.
    """

    if not update.message:
        return

    authorized = await is_authorized_admin(
        update,
        context,
    )

    if not authorized:

        await update.message.reply_text(
            "⛔ غير مسموح لك باستخدام لوحة الإدارة."
        )

        return

    current_group_id = get_target_group_id()
    groups = get_registered_groups()

    keyboard = []

    # -------------------------------------------------
    # الجروبات المسجلة
    # -------------------------------------------------

    for group_id, title in groups:

        display_name = (
            title
            or f"جروب {group_id}"
        )

        prefix = (
            "✅ "
            if str(group_id) == str(current_group_id)
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

    # -------------------------------------------------
    # تسجيل الجروب الحالي
    # -------------------------------------------------

    keyboard.append(
        [
            InlineKeyboardButton(
                "➕ تسجيل الجروب الحالي",
                callback_data="register_current_group",
            )
        ]
    )

    # -------------------------------------------------
    # عرض الجروب الحالي
    # -------------------------------------------------

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
# Callback الخاص بلوحة الإدارة
# =========================================================

async def admin_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    معالجة أزرار لوحة الإدارة.
    """

    query = update.callback_query

    if not query:
        return

    # -------------------------------------------------
    # التحقق من الصلاحيات
    # -------------------------------------------------

    authorized = await is_authorized_admin(
        update,
        context,
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
    # عرض الجروب الحالي
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

    # =================================================
    # اختيار جروب
    # =================================================

    if data.startswith("select_group:"):

        try:

            group_id = int(
                data.split(
                    ":",
                    1
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

        # -------------------------------------------------
        # التأكد أن الجروب مسجل
        # -------------------------------------------------

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

        # -------------------------------------------------
        # التأكد أن البوت يستطيع الوصول إليه
        # -------------------------------------------------

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

        # -------------------------------------------------
        # حفظ الجروب
        # -------------------------------------------------

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
    """
    تعيين الجروب الحالي كجروب استقبال.
    """

    if not update.effective_chat:
        return

    chat = update.effective_chat

    # -------------------------------------------------
    # يجب أن يكون داخل جروب
    # -------------------------------------------------

    if chat.type not in (
        "group",
        "supergroup",
    ):

        await update.message.reply_text(
            "❌ استخدم /setgroup داخل الجروب المطلوب."
        )

        return

    # -------------------------------------------------
    # الصلاحيات
    # -------------------------------------------------

    authorized = await is_authorized_admin(
        update,
        context,
    )

    if not authorized:

        await update.message.reply_text(
            "⛔ غير مسموح لك بتغيير جروب استقبال الفيديوهات."
        )

        return

    group_id = chat.id
    title = chat.title or ""

    # -------------------------------------------------
    # تسجيل الجروب
    # -------------------------------------------------

    register_success = register_group(
        group_id,
        title,
    )

    if not register_success:

        await update.message.reply_text(
            "❌ حدث خطأ أثناء تسجيل الجروب."
        )

        return

    # -------------------------------------------------
    # جعله الجروب المستهدف
    # -------------------------------------------------

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


def get_video_info(url: str):
    """
    استخراج معلومات الفيديو بدون تحميله.
    """

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }

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

        logger.exception(
            "Video information error: %s",
            e,
        )

        return None


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

            # -------------------------------------------------
            # البحث باستخدام platform + video_id
            # -------------------------------------------------

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

            # -------------------------------------------------
            # البحث بالرابط
            # -------------------------------------------------

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
    استخراج أول رابط من الرسالة.
    """

    if not text:
        return None

    match = URL_REGEX.search(text)

    if not match:
        return None

    url = match.group(0)

    # إزالة علامات الترقيم الموجودة بعد الرابط
    url = url.rstrip(
        ".,!?;:)]}\"'"
    )

    return url


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
# تحميل الفيديو
# =========================================================

def download_video(
    url: str,
    output_template: str,
):
    """
    تحميل الفيديو باستخدام yt-dlp.
    """

    ydl_opts = {

        # اختيار جودة مناسبة لتليجرام
        # الأولوية لـ 720p ثم 480p ثم أقل جودة متاحة
        "format": (
            "bestvideo[height<=720][filesize<=45000000][ext=mp4]+"
            "bestaudio[filesize<=5000000][ext=m4a]/"
            "bestvideo[height<=720][filesize<=45000000]+"
            "bestaudio/"
            "best[height<=720][filesize<=49000000][ext=mp4]/"
            "best[height<=720][filesize<=49000000]/"
            "best"
        ),

        # دمج النتيجة إلى MP4
        "merge_output_format": "mp4",

        # مكان الملف
        "outtmpl": output_template,

        # تقليل الـ logs
        "quiet": True,
        "no_warnings": True,

        # IPv4
        "source_address": "0.0.0.0",

        # إعادة المحاولة
        "retries": 3,
        "fragment_retries": 3,

        # عدم تحميل Playlist
        "noplaylist": True,

        # عدم حفظ ملفات إضافية
        "writethumbnail": False,
        "writeinfojson": False,

        # تحويل الفيديو إلى MP4
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
    }

    try:

        with yt_dlp.YoutubeDL(
            ydl_opts
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=True,
            )

            if not info:
                return None, None

            # -------------------------------------------------
            # محاولة الحصول على الملف النهائي
            # -------------------------------------------------

            filepath = None

            requested_downloads = (
                info.get(
                    "requested_downloads"
                )
            )

            if requested_downloads:

                for item in requested_downloads:

                    candidate = item.get(
                        "filepath"
                    )

                    if (
                        candidate
                        and os.path.exists(candidate)
                    ):

                        filepath = candidate
                        break

            # -------------------------------------------------
            # fallback
            # -------------------------------------------------

            if not filepath:

                prepared_path = (
                    ydl.prepare_filename(info)
                )

                if os.path.exists(
                    prepared_path
                ):

                    filepath = prepared_path

                else:

                    mp4_path = (
                        os.path.splitext(
                            prepared_path
                        )[0]
                        + ".mp4"
                    )

                    if os.path.exists(
                        mp4_path
                    ):

                        filepath = mp4_path

            if filepath:

                return filepath, info

            return None, info

    except Exception as e:

        logger.exception(
            "Download error: %s",
            e,
        )

        return None, None


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

    if not update.message.text:
        return

    text = update.message.text

    # -------------------------------------------------
    # استخراج الرابط
    # -------------------------------------------------

    url = extract_url(text)

    if not url:
        return

    # -------------------------------------------------
    # معرفة الموقع
    # -------------------------------------------------

    site_name = get_site_name(
        url
    )

    logger.info(
        "Received URL: %s",
        url,
    )

    # -------------------------------------------------
    # رسالة الحالة
    # -------------------------------------------------

    try:

        status_msg = (
            await update.message.reply_text(
                f"⏳ جاري تحميل الفيديو من "
                f"{site_name}..."
            )
        )

    except Exception as e:

        logger.exception(
            "Could not send status message: %s",
            e,
        )

        return

    # -------------------------------------------------
    # إنشاء اسم ملف فريد
    # -------------------------------------------------

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
        # استخراج معلومات الفيديو
        # =================================================

        info = await loop.run_in_executor(
            None,
            get_video_info,
            url,
        )

        if not info:

            await status_msg.edit_text(
                "❌ لم أستطع قراءة معلومات الفيديو.\n\n"
                "قد يكون الرابط غير مدعوم، "
                "أو الفيديو خاص، "
                "أو الموقع يحتاج تسجيل دخول."
            )

            return

        # =================================================
        # الحصول على مفتاح الفيديو
        # =================================================

        platform, video_id = get_video_key(
            info,
            url,
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
        # تحميل الفيديو
        # =================================================

        await status_msg.edit_text(
            f"⬇️ جاري تحميل الفيديو من "
            f"{site_name}..."
        )

        file_path, info = (
            await loop.run_in_executor(
                None,
                download_video,
                url,
                output_template,
            )
        )

        # =================================================
        # التحقق من نجاح التحميل
        # =================================================

        if (
            not file_path
            or not os.path.exists(file_path)
        ):

            await status_msg.edit_text(
                "❌ لم أستطع تحميل الفيديو.\n\n"
                "قد يكون الرابط غير مدعوم، "
                "أو الفيديو خاص، "
                "أو الموقع يحتاج تسجيل دخول."
            )

            return

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

        # Telegram caption محدود
        title = title[:800]

        caption = "🎬 تم تحميل الفيديو"

        if title:
            caption += (
                f"\n\n{title}"
            )

        # =================================================
        # الحصول على الجروب المستهدف
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
            )

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
        # رسالة النجاح
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
            and os.path.exists(file_path)
        ):

            try:

                os.remove(file_path)

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
    # التأكد من BOT_TOKEN
    # -------------------------------------------------

    if not BOT_TOKEN:

        raise ValueError(
            "BOT_TOKEN is not set in environment variables."
        )

    # -------------------------------------------------
    # تهيئة قاعدة البيانات
    # -------------------------------------------------

    init_database()

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
    # =================================================

    app.add_handler(
        MessageHandler(
            filters.TEXT
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
