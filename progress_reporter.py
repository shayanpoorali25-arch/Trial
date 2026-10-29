"""
گزارشگر پیشرفت مرحله‌ای — با ظاهر حرفه‌ای برای تلگرام
"""

import time
import threading
import logging

logger = logging.getLogger(__name__)

class ProgressReporter:
    # تعریف مراحل پروژه
    STAGES = [
        "پردازش ویدیو",
        "تولید زیرنویس",
        "آپلود به تلگرام"
    ]

    def __init__(self, bot, chat_id):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = None
        self._lock = threading.Lock()
        self._last_update = 0.0
        self._min_interval = 1.5  # ثانیه (برای جلوگیری از بلاک شدن توسط تلگرام)
        
        self.start_time = time.time()
        self.current_stage = 0
        self.sub_text = "در حال آماده‌سازی..."
        self.pct = 0.0
        self.extra_stats = ""
        
        # ارسال پیام اولیه
        self._send_initial()

    def _send_initial(self):
        try:
            text = self._render_message()
            msg = self.bot.send_message(self.chat_id, text, parse_mode="HTML")
            self.message_id = msg.message_id
        except Exception as e:
            logger.warning(f"ارسال پیام اولیه ناموفق: {e}")

    def _make_bar(self, pct: float, length: int = 10) -> str:
        """ساخت نوار پیشرفت با ایموجی"""
        pct = max(0.0, min(100.0, pct))
        filled = int(length * pct / 100)
        return f"[{'🟩' * filled}{'⬜' * (length - filled)}]"

    def _render_message(self) -> str:
        """ساخت متن نهایی بر اساس وضعیت فعلی"""
        lines = ["🚀 <b>عملیات ساخت زیرنویس</b>\n"]
        
        for i, stage_name in enumerate(self.STAGES):
            if i < self.current_stage:
                # مراحل انجام شده
                lines.append(f"✅ مرحله {i+1}: {stage_name}")
            elif i == self.current_stage:
                # مرحله فعلی (بدون نمایش متن‌های ریز زیرشاخه)
                lines.append(f"⏳ مرحله {i+1}: {stage_name}")
            else:
                # مراحل آینده
                lines.append(f"⬜ مرحله {i+1}: {stage_name}")
        
        lines.append("")
        
        # نوار پیشرفت و درصد
        bar = self._make_bar(self.pct)
        lines.append(f"📊 <b>پیشرفت:</b> {self.pct:.0f}٪ {bar}")
        
        # زمان
        elapsed = int(time.time() - self.start_time)
        lines.append(f"⏱ <b>زمان سپری‌شده:</b> {elapsed} ثانیه")
        
        if self.extra_stats:
            lines.append(self.extra_stats)
            
        return "\n".join(lines)

    def update(self, stage: int, sub_text: str = "", pct: float = None, extra: str = "", force: bool = False):
        """آپدیت وضعیت مراحل"""
        with self._lock:
            self.current_stage = stage
            self.sub_text = sub_text
            if pct is not None:
                self.pct = pct
            self.extra_stats = extra

            now = time.time()
            if not force and (now - self._last_update) < self._min_interval:
                return
            self._last_update = now

            # لاگ در ترمینال
            logger.info(f"[{stage+1}/{len(self.STAGES)}] {self.STAGES[stage]} | {sub_text} | {self.pct:.0f}%")

            if not self.message_id:
                return

            try:
                text = self._render_message()
                self.bot.edit_message_text(
                    text,
                    self.chat_id,
                    self.message_id,
                    parse_mode="HTML",
                )
            except Exception as e:
                if "not modified" not in str(e).lower():
                    logger.debug(f"خطا در ادیت پیام: {e}")

    def finish(self, final_text: str):
        """اتمام کار با پیام نهایی"""
        with self._lock:
            if not self.message_id:
                return
            try:
                self.bot.edit_message_text(
                    final_text,
                    self.chat_id,
                    self.message_id,
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"خطا در ارسال پیام نهایی: {e}")

    def error(self, error_text: str, reply_markup=None):
        """اتمام کار با خطا"""
        with self._lock:
            if not self.message_id:
                return
            try:
                self.bot.edit_message_text(
                    error_text,
                    self.chat_id,
                    self.message_id,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
            except Exception as e:
                logger.warning(f"خطا در ارسال پیام خطا: {e}")
