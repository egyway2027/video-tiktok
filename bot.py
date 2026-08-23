<<<<<<< SEARCH
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

            # بعد الدمج قد يتحول إلى MP4
            mp4_path = (
                os.path.splitext(filepath)[0]
                + ".mp4"
            )

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
=======
def download_video(
    url: str,
    output_template: str,
    max_file_size: int = MAX_FILE_SIZE
):
    """
    تحميل الفيديو بأعلى جودة ممكنة تناسب حجم Telegram.

    يتم تجربة الجودة بالترتيب:
    1080p -> 720p -> 480p -> 360p

    إذا تجاوز الملف النهائي الحد المسموح،
    يتم حذفه وتجربة جودة أقل تلقائيًا.
    """

    quality_levels = [
        1080,
        720,
        480,
        360,
    ]

    last_info = None

    for max_height in quality_levels:

        file_path = None

        logger.info(
            "Trying video quality up to %sp: %s",
            max_height,
            url
        )

        ydl_opts = {

            # أفضل فيديو متاح حتى الجودة المطلوبة
            # مع أفضل صوت متاح
            "format": (
                f"bestvideo[height<={max_height}]"
                f"+bestaudio/"
                f"best[height<={max_height}]"
            ),

            # إخراج MP4
            "merge_output_format": "mp4",

            # اسم الملف
            "outtmpl": output_template,

            # عدم إظهار تفاصيل yt-dlp الكثيرة
            "quiet": True,
            "no_warnings": True,

            # استخدام IPv4
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
                    "preferredformat": "mp4",
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
                    continue

                last_info = info

                # محاولة الحصول على الملف النهائي
                requested_downloads = info.get(
                    "requested_downloads"
                )

                if requested_downloads:

                    for download_info in requested_downloads:

                        possible_path = download_info.get(
                            "filepath"
                        )

                        if (
                            possible_path
                            and os.path.exists(possible_path)
                        ):
                            file_path = possible_path
                            break

                # محاولة معرفة اسم الملف من yt-dlp
                if not file_path:

                    filepath = ydl.prepare_filename(
                        info
                    )

                    # بعد الدمج قد يتحول إلى MP4
                    mp4_path = (
                        os.path.splitext(filepath)[0]
                        + ".mp4"
                    )

                    if os.path.exists(mp4_path):
                        file_path = mp4_path

                    elif os.path.exists(filepath):
                        file_path = filepath

                if not file_path:
                    logger.warning(
                        "Could not locate downloaded file at %sp.",
                        max_height
                    )
                    continue

                # -----------------------------------------
                # فحص الحجم النهائي
                # -----------------------------------------

                file_size = os.path.getsize(
                    file_path
                )

                logger.info(
                    "Quality %sp produced %.2f MB",
                    max_height,
                    file_size / (1024 * 1024)
                )

                # -----------------------------------------
                # الجودة مناسبة للحجم
                # -----------------------------------------

                if file_size <= max_file_size:

                    logger.info(
                        "Selected quality: %sp | Size: %.2f MB",
                        max_height,
                        file_size / (1024 * 1024)
                    )

                    return file_path, info

                # -----------------------------------------
                # الملف أكبر من الحد
                # -----------------------------------------

                logger.warning(
                    "Quality %sp is too large: %.2f MB. "
                    "Trying lower quality.",
                    max_height,
                    file_size / (1024 * 1024)
                )

                try:

                    os.remove(file_path)

                except OSError as e:

                    logger.warning(
                        "Could not remove oversized file: %s",
                        e
                    )

        except Exception as e:

            logger.warning(
                "Quality %sp failed: %s",
                max_height,
                e
            )

            # تنظيف أي ملف جزئي
            if file_path and os.path.exists(file_path):

                try:
                    os.remove(file_path)

                except OSError:
                    pass

            continue

    logger.error(
        "All available quality levels exceeded "
        "the Telegram file size limit or failed."
    )

    return None, last_info
>>>>>>> REPLACE


<<<<<<< SEARCH
        file_path, info = await loop.run_in_executor(
            None,
            download_video,
            url,
            output_template
        )
=======
        file_path, info = await loop.run_in_executor(
            None,
            download_video,
            url,
            output_template,
            MAX_FILE_SIZE
        )
>>>>>>> REPLACE


<<<<<<< SEARCH
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

        # -------------------------------------------------
        # التحقق من حجم الملف
        # -------------------------------------------------

        if file_size > MAX_FILE_SIZE:

            await status_msg.edit_text(
                f"⚠️ تم تحميل الفيديو بنجاح، "
                f"لكن حجمه كبير جدًا للإرسال عبر Telegram.\n\n"
                f"الحجم: "
                f"{file_size / (1024 * 1024):.1f} MB"
            )

            return
=======
        # -------------------------------------------------
        # فشل التحميل أو عدم وجود جودة مناسبة للحجم
        # -------------------------------------------------

        if not file_path or not os.path.exists(file_path):

            await status_msg.edit_text(
                "❌ لم أستطع تحميل الفيديو بالحجم المناسب.\n\n"
                "تمت تجربة عدة جودات تلقائيًا، "
                "لكن الفيديو ما زال أكبر من الحد المسموح "
                "أو أن الموقع لا يوفر صيغة مناسبة."
            )

            return

        # -------------------------------------------------
        # حجم الملف النهائي
        # -------------------------------------------------

        file_size = os.path.getsize(file_path)

        logger.info(
            "Final selected file: %s | Size: %.2f MB",
            file_path,
            file_size / (1024 * 1024)
        )

        # -------------------------------------------------
        # حماية إضافية قبل الإرسال
        # -------------------------------------------------

        if file_size > MAX_FILE_SIZE:

            await status_msg.edit_text(
                "⚠️ الملف النهائي أكبر من الحد المسموح "
                "للإرسال عبر Telegram."
            )

            return
>>>>>>> REPLACE
