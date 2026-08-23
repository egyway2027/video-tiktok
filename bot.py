import os
import re
import asyncio
import logging
import tempfile
import shutil
from pathlib import Path

import yt_dlp

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes
)

# =========================================================
# الإعدادات
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

TARGET_GROUP_ID = -1004468483224

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# الحد الأقصى لحجم الملف قبل محاولة إرساله إلى Telegram
MAX_FILE_SIZE = 49 * 1024 * 1024


# =========================================================
# Logging
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)


# =========================================================
# استخراج الرابط من الرسالة
# =========================================================

URL_REGEX = re.compile(
    r'https?://[^\s<>"\']+',
    re.IGNORECASE
)


def extract_url(text: str):
    """
    استخراج أول رابط موجود داخل الرسالة.
    """

    if not text:
        return None

    match = URL_REGEX.search(text)

    if not match:
        return None

    url = match.group(0)

    # إزالة علامات الترقيم التي قد تكون بعد الرابط
    url = url.rstrip(".,!?;:)]}\"'")

    return url


# =========================================================
# معرفة الموقع
# =========================================================

def get_site_name(url: str):
    """
    محاولة معرفة اسم الموقع من الرابط.
    """

    url_lower = url.lower()

    sites = {
        "tiktok.com": "TikTok",
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

    for domain, name in sites.items():
        if domain in url_lower:
            return name

    return "الموقع"


# =========================================================
# تحميل الفيديو باستخدام yt-dlp
# =========================================================

def download_video(url: str, output_template: str):
    """
    تحميل الفيديو من أي موقع يدعمه yt-dlp.
    """

    ydl_opts = {
        # أفضل فيديو + أفضل صوت
        "format": "bestvideo*+bestaudio/best",

        # إخراج MP4
        "merge_output_format": "mp4",

        # اسم الملف
        "outtmpl": output_template,

        # عدم إظهار تفاصيل yt-dlp الكثيرة
        "quiet": True,
        "no_warnings": True,

        # محاولة استخدام IPv4
        "source_address": "0.0.0.0",

        # إعادة المحاولة
        "retries": 3,
        "fragment_retries": 3,

        # عدم تحميل Playlist كاملة
        "noplaylist": True,

        # معلومات الملف
        "writethumbnail": False,
        "writeinfojson": False,

        # تحويل الفيديو إلى MP4 عند الحاجة
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
    }

    try:

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

            if not info:
                return None, None

            # الملف النهائي
            requested_downloads = info.get(
                "requested_downloads"
            )

            if requested_downloads:

                filepath = requested_downloads[0].get(
                    "filepath"
                )

                if filepath and os.path.exists(filepath):
                    return filepath, info

            # محاولة معرفة اسم الملف من yt-dlp
            filepath = ydl.prepare_filename(info)

            # بعد الدمج قد يتحول إلى mp4
            mp4_path = os.path.splitext(filepath)[0] + ".mp4"

            if os.path.exists(mp4_path):
                return mp4_path, info

            if os.path.exists(filepath):
                return filepath, info

            return None, info

    except Exception as e:

        logger.exception(
            "Download error: %s",
            e
        )

        return None, None


# =========================================================
# معالجة الرسائل
# =========================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.message.text:
        return

    text = update.message.text

    # استخراج الرابط
    url = extract_url(text)

    if not url:
        return

    # اسم الموقع
    site_name = get_site_name(url)

    logger.info(
        "Received URL: %s",
        url
    )

    status_msg = await update.message.reply_text(
        f"⏳ جاري تحميل الفيديو من {site_name}..."
    )

    # إنشاء اسم فريد للملف
    user_id = (
        update.effective_user.id
        if update.effective_user
        else 0
    )

    message_id = update.message.message_id

    filename = (
        f"video_{user_id}_{message_id}.%(ext)s"
    )

    output_template = str(
        DOWNLOAD_DIR / filename
    )

    file_path = None

    try:

        # تشغيل yt-dlp في Thread منفصل
        loop = asyncio.get_running_loop()

        file_path, info = await loop.run_in_executor(
            None,
            download_video,
            url,
            output_template
        )

        # -------------------------------------------------
        # فشل التحميل
        # -------------------------------------------------

        if not file_path or not os.path.exists(file_path):

            await status_msg.edit_text(
                "❌ لم أستطع تحميل الفيديو.\n\n"
                "قد يكون الرابط غير مدعوم، "
                "أو الفيديو خاص، أو الموقع يحتاج تسجيل دخول."
            )

            return

        # -------------------------------------------------
        # حجم الملف
        # -------------------------------------------------

        file_size = os.path.getsize(file_path)

        logger.info(
            "Downloaded file: %s | Size: %.2f MB",
            file_path,
            file_size / (1024 * 1024)
        )

        # Telegram Bot API له حد لحجم الملفات المرسلة
        if file_size > MAX_FILE_SIZE:

            await status_msg.edit_text(
                f"⚠️ تم تحميل الفيديو بنجاح، "
                f"لكن حجمه كبير جدًا للإرسال عبر Telegram.\n\n"
                f"الحجم: {file_size / (1024 * 1024):.1f} MB"
            )

            return

        # -------------------------------------------------
        # عنوان الفيديو
        # -------------------------------------------------

        title = ""

        if info:
            title = info.get("title") or ""

        caption = "🎬 تم تحميل الفيديو"

        if title:

            # منع Caption طويل جدًا
            title = title[:800]

            caption += f"\n\n{title}"

        # -------------------------------------------------
        # إرسال الفيديو للجروب
        # -------------------------------------------------

        with open(file_path, "rb") as video_file:

            await context.bot.send_video(
                chat_id=TARGET_GROUP_ID,
                video=video_file,
                caption=caption,
                supports_streaming=True
            )

        # -------------------------------------------------
        # تحديث الرسالة
        # -------------------------------------------------

        await status_msg.edit_text(
            f"✅ تم تحميل الفيديو وإرساله إلى المجموعة.\n"
            f"🌐 المصدر: {site_name}"
        )

    except Exception as e:

        logger.exception(
            "Processing error: %s",
            e
        )

        try:

            await status_msg.edit_text(
                "⚠️ حدث خطأ أثناء معالجة الفيديو.\n\n"
                "حاول إرسال الرابط مرة أخرى."
            )

        except Exception:
            pass

    finally:

        # -------------------------------------------------
        # حذف الملف بعد الإرسال
        # -------------------------------------------------

        if file_path and os.path.exists(file_path):

            try:
                os.remove(file_path)

            except Exception as e:

                logger.warning(
                    "Could not delete file: %s",
                    e
                )


# =========================================================
# تشغيل البوت
# =========================================================

def main():

    if not BOT_TOKEN:

        raise ValueError(
            "BOT_TOKEN is not set in environment variables."
        )

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # استقبال أي رسالة نصية تحتوي على رابط
    app.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            handle_message
        )
    )

    logger.info(
        "Universal Video Downloader Bot started..."
    )

    app.run_polling()


# =========================================================
# Start
# =========================================================

if __name__ == "__main__":
    main()
