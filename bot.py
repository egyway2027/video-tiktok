import os
import re
import asyncio
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import yt_dlp

# إعداد السجلات لمتابعة الأداء من GitHub Actions Logs
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# قراءة التوكن بأمان من Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_GROUP_ID = -1004468483224

TIKTOK_REGEX = r'(https?://(?:www\.|vm\.|vt\.)?tiktok\.com/[^\s]+)'

def download_tiktok_video(url: str, output_path: str) -> bool:
    """تحميل الفيديو من تيك توك بأعلى جودة بدون علامة مائية"""
    ydl_opts = {
        'outtmpl': output_path,
        'format': 'bestvideo*+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return os.path.exists(output_path)
    except Exception as e:
        logging.error(f"Download Error: {e}")
        return False

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الرسائل واستخراج روابط تيك توك"""
    if not update.message or not update.message.text:
        return

    text = update.message.text
    match = re.search(TIKTOK_REGEX, text)
    
    if not match:
        return

    tiktok_url = match.group(0)
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text("⏳ جاري سحب وتحميل الفيديو...")
    
    file_path = f"video_{user_id}_{update.message.message_id}.mp4"

    try:
        # تنفيذ التحميل في خيط منفصل لمنع تجميد البوت
        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(None, download_tiktok_video, tiktok_url, file_path)

        if success and os.path.exists(file_path):
            with open(file_path, 'rb') as video_file:
                await context.bot.send_video(
                    chat_id=TARGET_GROUP_ID,
                    video=video_file
                )
            await status_msg.edit_text("✅ تم إرسال الفيديو إلى المجموعة بنجاح.")
        else:
            await status_msg.edit_text("❌ تعذر تحميل الفيديو، تأكد من صحة الرابط أو أن الحساب ليس خاصاً.")

    except Exception as e:
        logging.error(f"Processing Error: {e}")
        await status_msg.edit_text(f"⚠️ حدث خطأ أثناء المعالجة: {str(e)}")

    finally:
        # تنظيف وحذف الملف فوراً لتوفير المساحة
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is not set in environment variables.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    logging.info("Bot is running and polling updates...")
    app.run_polling()

if __name__ == '__main__':
    main()
