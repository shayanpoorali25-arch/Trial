"""
منطق اصلی ربات تلگرام — حالت آماده‌باش
لینک یا فایل ویدیو هر لحظه پذیرفته می‌شه، بدون نیاز به دکمه.
"""
from video_processor import process_incoming, save_srt, WORK_DIR, VideoTooLongError
import os
import re
import time
import logging
import threading
import telebot
from telebot import types
from flask import Flask, request
import queue_manager
import config
from progress_reporter import ProgressReporter
from video_processor import process_incoming, save_srt, WORK_DIR
from gemini_service import transcribe_audio
from renderer import render_and_send
from license_manager import verify_and_consume_license

logger = logging.getLogger(__name__)

# ساخت نمونه ربات با استفاده از config
bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN, parse_mode=None, threaded=True)

# برای استفاده در وبهوک
app = Flask(__name__)
BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

# الگوی لینک اینستاگرام
INSTAGRAM_PATTERN = re.compile(
    r"^https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+"
)

authenticated_users: set = set()  # کاربرانی که لایسنس تایید شده دارند
trial_used_users: set = set()     # 👈 کاربرانی که تستشون رو انجام دادند

# ===========================================
# مدیریت کاربران و وضعیت‌ها
# ===========================================
_busy_lock = threading.Lock()
_busy_users: set = set()
user_last_inputs = {}
authenticated_users: set = set()  # 👈 کاربرانی که لایسنس تایید شده دارند

def _is_busy(user_id: int) -> bool:
    with _busy_lock:
        return user_id in _busy_users

def _mark_busy(user_id: int) -> bool:
    with _busy_lock:
        if user_id in _busy_users:
            return False
        _busy_users.add(user_id)
        return True

def _mark_free(user_id: int) -> None:
    with _busy_lock:
        _busy_users.discard(user_id)

# ===========================================
# توابع کمکی
# ===========================================
def is_instagram_link(text: str) -> bool:
    if not text:
        return False
    return bool(INSTAGRAM_PATTERN.match(text.strip()))

def is_video_file(message: types.Message) -> bool:
    if message.video:
        return True
    if message.document and (message.document.mime_type or "").startswith("video/"):
        return True
    return False

# ===========================================
# هندلرها
# ===========================================
@bot.message_handler(commands=["start"])
def handle_start(message: types.Message):
    logger.info(f"[LOG] User {message.from_user.id} started the bot.")
    
    # 👈 چک میکنیم اگر کاربر قبلا لایسنس داده باشد
    if message.from_user.id in authenticated_users:
        text = (
            f"سلام مجدد {message.from_user.first_name} 👋\n\n"
            "✅ شما قبلاً احراز هویت کرده‌اید.\n\n"
            "🔗 حالا می‌توانید لینک اینستاگرام (زیر ۹۰ ثانیه) یا فایل ویدیو خود را بفرستید تا پردازش شود."
        )
    else:
        # اگر کاربر جدید است
        text = (
            f"سلام {message.from_user.first_name} 👋\n\n"
            "🌟 به ربات تست **Tolider** خوش آمدید!\n\n"
            "🔒 برای استفاده از نسخه تست (تولید یک ویدیو زیر ۹۰ ثانیه)، لطفاً **کد لایسنس** خود را ارسال کنید.\n"
            "🎫 اگر کد لایسنس ندارید، می‌توانید آن را از کانال/پیج ما دریافت کنید."
        )
        
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=["help"])
def handle_help(message: types.Message):
    text = (
        "📌 راهنما:\n\n"
        "برای استفاده از ربات، ابتدا کد لایسنس خود را ارسال کنید.\n"
        "پس از تایید، یکی از این‌ها را بفرستید:\n"
        "🔗 لینک پست / ریلز / تیوی اینستاگرام\n"
        "🎥 فایل ویدیو (mp4 / mov / webm)\n\n"
        "دستورات:\n"
        "/start — شروع\n"
        "/help — همین راهنما"
    )
    bot.send_message(message.chat.id, text)

@bot.message_handler(
    content_types=["text", "video", "document"],
    func=lambda m: not (m.text and m.text.startswith("/")),
)
def handle_input(message: types.Message):
    # 👈 چک میکنیم اگر کاربر قبلا تست خود را انجام داده باشد
    if user_id in trial_used_users:
        bot.reply_to(
            message, 
            "❌ شما قبلاً از فرصت تست رایگان خود استفاده کرده‌اید.\n\n"
            "🚀 برای ادامه کار، لطفاً از **نسخه پرمیوم** استفاده کنید."
        )
        return

    # مرحله احراز هویت (لایسنس)
    if user_id not in authenticated_users:
        if not message.text:
            bot.reply_to(message, "❌ لطفاً ابتدا کد لایسنس خود را به صورت متن ارسال کنید.")
            return

        is_valid, msg = verify_and_consume_license(message.text)
        
        if is_valid:
            authenticated_users.add(user_id)
            logger.info(f"[LOG] User {user_id} authenticated successfully.")
            bot.reply_to(
                message, 
                "✅ لایسنس شما تایید شد!\n\n"
                "حالا یک لینک اینستاگرام (زیر ۹۰ ثانیه) یا فایل ویدیو بفرستید."
            )
        else:
            logger.warning(f"[LOG] User {user_id} failed authentication.")
            bot.reply_to(message, f"❌ {msg}\n\nاگر لایسنس ندارید، از کانال ما تهیه کنید.")
        return

    # 👈 مرحله پردازش (کاربر احراز هویت شده است)
    if message.text and is_instagram_link(message.text):
        is_link = True
    elif is_video_file(message):
        is_link = False
    else:
        logger.warning(f"[LOG] Invalid input from user {user_id}.")
        bot.reply_to(
            message,
            "❌ ورودی نامعتبره!\nلطفاً یه لینک اینستاگرام معتبر یا یه فایل ویدیو بفرست.",
        )
        return

    if not _mark_busy(user_id):
        logger.warning(f"[LOG] User {user_id} is busy or already in queue.")
        bot.reply_to(message, "⏳ درخواست شما قبلاً در صف قرار گرفته است.\nلطفاً صبر کنید.")
        return

    # 👈 اضافه کردن به صف
    queue_position = queue_manager.add_to_queue(user_id, message, is_link)
    
    logger.info(f"[LOG] User {user_id} added to queue. Position: {queue_position}")
    
    # پیام آپ‌سل (Upsell) + شماره صف
    bot.reply_to(
        message,
        f"✅ درخواست شما در صف قرار گرفت.\n\n"
        f"📍 **نفر {queue_position} در صف**\n\n"
        f"⏳ درخواست شما به زودی پردازش می‌شود.\n"
        f"🚀 برای دریافت سریع و بدون صف، می‌توانید از **نسخه پرمیوم** استفاده کنید."
    )
    
    # 👈 اینجا دیگه `_process` رو مستقیم صدا نمی‌زنیم، کارگر صف این کار رو میکنه.
# ===========================================
# پردازش اصلی
# ===========================================
def _process(message: types.Message, is_link: bool):
    logger.info(f"[LOG] Starting main processing for user {message.from_user.id}.")
    reporter = ProgressReporter(bot, message.chat.id)

    try:
        # مرحله 1 و 2
        logger.info(f"[LOG] Step 1 & 2: Download and audio extraction...")
        video_path, audio_path = process_incoming(
            bot, message, is_link, progress=reporter
        )

        # مرحله 3
        logger.info(f"[LOG] Step 3: Sending to Gemini...")
        reporter.update(stage=1, sub_text="در حال ارسال صدا و دریافت ترجمه...", pct=50, force=True)
        result = transcribe_audio(audio_path)
        logger.info(f"[LOG] Gemini returned {len(result['subtitles'])} subtitle lines.")

        # مرحله 4
        srt_path = str(WORK_DIR / f"subtitles_{message.from_user.id}.srt")
        save_srt(result["subtitles"], srt_path)
        reporter.update(stage=1, sub_text=f"{len(result['subtitles'])} خط زیرنویس دریافت شد", pct=100, force=True)

        # مرحله 5
        logger.info(f"[LOG] Step 4 & 5: Rendering and sending to Telegram...")
        render_and_send(
            bot=bot,
            chat_id=message.chat.id,
            video_path=video_path,
            subtitles=result["subtitles"],
            progress=reporter,
        )

        logger.info(f"[LOG] Processing for user {message.from_user.id} completed successfully.")
        reporter.finish(
            f"✅ <b>عملیات با موفقیت انجام شد!</b>\n\n"
            f"🎬 ویدیوی شما زیرنویس شد و در پیام پایین ارسال گردید.\n"
            f"⏱ زمان کل: {int(time.time() - reporter.start_time)} ثانیه\n\n"
            f"⚠️ <b>توجه:</b> شما از فرصت تست رایگان خود استفاده کردید."
        )
        
        # 👈 اینجا کاربر رو از لیست تایید شده‌ها خارج و به لیست استفاده‌کنندگان منتقل میکنیم
        authenticated_users.discard(message.from_user.id)
        trial_used_users.add(message.from_user.id)
        logger.info(f"[LOG] User {message.from_user.id} has used their 1-time trial.")

    except VideoTooLongError as e:
        # 👈 مدیریت اختصاصی خطای ویدیوی طولانی
        logger.warning(f"[LOG] Video too long for user {message.from_user.id}.")
        reporter.error(
            f"⚠️ <b>محدودیت نسخه تست:</b>\n\n"
            f"{str(e)}\n\n"
            f"🚀 برای پردازش ویدیوهای طولانی، از نسخه پرمیوم استفاده کنید."
        )


    except Exception as e:
        logger.exception(f"[ERROR] Processing error for user {message.from_user.id}: {e}")
        user_last_inputs[message.from_user.id] = message
        
        markup = types.InlineKeyboardMarkup()
        retry_btn = types.InlineKeyboardButton("🔄 تلاش مجدد", callback_data=f"retry_{message.from_user.id}")
        markup.add(retry_btn)
        
        safe_error = str(e)[:500].replace("<", "&lt;").replace(">", "&gt;")
        reporter.error(
            f"❌ <b>خطا در پردازش:</b>\n<code>{safe_error}</code>",
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("retry_"))
def handle_retry(call: types.CallbackQuery):
    user_id = int(call.data.split("_")[1])
    logger.info(f"[LOG] User {user_id} requested retry.")
    
    if _is_busy(user_id):
        bot.answer_callback_query(call.id, "⏳ ربات در حال پردازش است. لطفاً صبر کنید.", show_alert=True)
        return
        
    message = user_last_inputs.get(user_id)
    if not message:
        bot.answer_callback_query(call.id, "❌ درخواست قبلی یافت نشد. لطفاً دوباره لینک یا ویدیو را ارسال کنید.", show_alert=True)
        return
        
    is_link = False
    if message.text and is_instagram_link(message.text):
        is_link = True
    elif is_video_file(message):
        is_link = False
    else:
        bot.answer_callback_query(call.id, "❌ ورودی نامعتبر است.", show_alert=True)
        return
        
    try:
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
    except Exception:
        pass
        
    bot.answer_callback_query(call.id, "🔄 در حال تلاش مجدد...")
    
    if not _mark_busy(user_id):
        return
        
    try:
        _process(message, is_link)
    finally:
        _mark_free(user_id)

# ===========================================
# اجرا و وبهوک
# ===========================================
@app.route("/")
def health_check():
    return "Bot is alive!", 200

@app.route('/' + BOT_TOKEN, methods=['POST'])
def getMessage():
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "!", 200

def run_bot():
    """اجرای ربات: روی Render با Webhook، روی سیستم شخصی با Polling"""

   # 👈 شروع کارگر صف
    queue_manager.start_worker(_process, _mark_free)

    # اگر روی Render باشیم...
    if RENDER_EXTERNAL_URL:
        print("[LOG] Running on Render. Setting webhook...")

    
    # اگر روی Render باشیم، این متغیر وجود دارد
    if RENDER_EXTERNAL_URL:
        print("[LOG] Running on Render. Setting webhook...")
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=RENDER_EXTERNAL_URL + '/' + BOT_TOKEN)
        print(f"[LOG] Webhook set to: {RENDER_EXTERNAL_URL + '/' + BOT_TOKEN}")
        
        port = int(os.environ.get('PORT', 5000))
        app.run(host='0.0.0.0', port=port)
    
    # اگر روی سیستم شخصی باشیم (متغیر وجود ندارد)
    else:
        print("[LOG] Running locally. Starting infinity polling...")
        bot.remove_webhook() # حذف وبهوک قبلی برای جلوگیری از تداخل
        bot.infinity_polling(timeout=30, long_polling_timeout=25)
