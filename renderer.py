"""
رندر نهایی ویدیو با زیرنویس (ffmpeg) + واترمارک اختصاصی و ارسال به تلگرام.
"""

import os
import time
import shutil
import logging
import subprocess
import tempfile

from video_processor import WORK_DIR

logger = logging.getLogger(__name__)

# ==========================================
# تنظیمات ظاهر زیرنویس
# ==========================================
FONT_NAME = "Vazir"
FONT_SIZE_RATIO = 0.07  # 👈 سایز فونت (متعادل و خوانا)
SUBTITLE_ALIGNMENT = 2

# 👈 فاصله زیرنویس از پایین (0.20 یعنی ۲۰ درصد ارتفاع ویدیو)
MARGIN_BOTTOM_RATIO = 0.20 

BOX_PADDING = 8
BACK_COLOR = "&H00000000"
PRIMARY_COLOR = "&H00FFFFFF"

FFMPEG_TIMEOUT = 1800
TELEGRAM_MAX_VIDEO_MB = 50

# ==========================================
# تنظیمات واترمارک (تصویر فتوشاپ)
# ==========================================
ENABLE_WATERMARK = False  # 👈 برای فعال شدن واترمارک، این رو True کن. برای نسخه تست فعلا False بذار.
WATERMARK_PATH = "assets/watermark.png" 
WATERMARK_HEIGHT_RATIO = 0.025           
WATERMARK_GAP = 5                      


def _srt_time_to_ass(t: str) -> str:
    """تبدیل زمان SRT به ASS"""
    t = t.strip()
    parts = t.split(":")
    if len(parts) == 3:
        h, m, rest = parts
    elif len(parts) == 2:
        h, m, rest = 0, parts[0], parts[1]
    else:
        h, m, rest = 0, 0, parts[0]
        
    rest = rest.replace(",", ".")
    s_parts = rest.split(".")
    if len(s_parts) == 2:
        s, ms = s_parts
    else:
        s, ms = s_parts[0], "00"
        
    return f"{int(h)}:{int(m):02d}:{int(s):02d}.{ms[:2].ljust(2, '0')}"


def _srt_to_seconds(srt_time: str) -> float:
    """تبدیل زمان SRT به ثانیه برای محاسبات ریاضی"""
    srt_time = srt_time.strip()
    parts = srt_time.split(":")
    if len(parts) == 3:
        h, m, rest = parts
    elif len(parts) == 2:
        h, m, rest = 0, parts[0], parts[1]
    else:
        h, m, rest = 0, 0, parts[0]
    
    rest = rest.replace(",", ".")
    s_parts = rest.split(".")
    s = float(s_parts[0])
    ms = float(s_parts[1]) / 1000 if len(s_parts) > 1 else 0
    return int(h) * 3600 + int(m) * 60 + s + ms


def _seconds_to_ass(seconds: float) -> str:
    """تبدیل ثانیه به فرمت زمان ASS"""
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 100)
    return f"{h}:{m:02d}:{s:02d}.{ms:02d}"


def _get_video_size(path: str):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=s=x:p=0", path],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        w, h = map(int, r.stdout.strip().split("x"))
        return w, h
    except Exception:
        return 1080, 1920


def _get_duration(video_path: str) -> float:
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        return float(probe.stdout.strip())
    except Exception:
        return 0.0


def _create_ass_file(subtitles, ass_path, video_w, video_h) -> tuple[int, int]:
    font_size = max(30, int(video_w * FONT_SIZE_RATIO))
    margin_bottom = int(video_h * MARGIN_BOTTOM_RATIO)
    
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {video_w}\n"
        f"PlayResY: {video_h}\n"
        "WrapStyle: 0\n"  # 👈 0 یعنی اگر متن طولانی بود، ffmpeg خودش تمیز می‌شکنه
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{FONT_NAME},{font_size},"
        f"{PRIMARY_COLOR},&H000000FF,&H00000000,{BACK_COLOR},"
        "0,0,0,0,100,100,0,0,3,"
        f"{BOX_PADDING},0,{SUBTITLE_ALIGNMENT},10,10,{margin_bottom},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    RLE = "\u202B"
    PDF = "\u202C"

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        
        last_end_time = 0.0  # برای جلوگیری از تداخل زمانی

        for sub in subtitles:
            text = (sub.get("text") or "").replace("\r", "").replace("\n", " ").strip()
            if not text:
                continue
                
            start_sec = _srt_to_seconds(sub["start_time"])
            end_sec = _srt_to_seconds(sub["end_time"])
            
            # 🛠 اصلاح زمان‌های خراب (تداخل یا زمان صفر)
            if start_sec < last_end_time:
                start_sec = last_end_time + 0.1 
            if end_sec <= start_sec:
                end_sec = start_sec + 1.5 
                
            last_end_time = end_sec
            
            start_ass = _seconds_to_ass(start_sec)
            end_ass = _seconds_to_ass(end_sec)
            
            # 👈 بدون هیچ تقسیم کلمه یا زمانی، دقیقاً همون چیزی که مدل داده رو نشون بده
            f.write(f"Dialogue: 0,{start_ass},{end_ass},Default,,0,0,0,,{RLE}{text}{PDF}\n")
            
    return font_size, margin_bottom


# ==========================================
# تابع اصلی
# ==========================================
def render_and_send(bot, chat_id, video_path, subtitles, progress=None):
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"ویدیو پیدا نشد: {video_path}")

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    job_dir = tempfile.mkdtemp(dir=str(WORK_DIR))
    ass_path = os.path.join(job_dir, "subs.ass")
    output_path = os.path.join(job_dir, "final.mp4")

    try:
        v_w, v_h = _get_video_size(video_path)
        duration = _get_duration(video_path)
        
        font_size, margin_bottom = _create_ass_file(subtitles, ass_path, v_w, v_h)
        logger.info(f"🎬 ابعاد: {v_w}x{v_h} | فونت: {font_size} | فاصله از پایین: {margin_bottom}px")

        ass_escaped = ass_path.replace("\\", "/").replace(":", "\\:")
        fonts_dir = os.path.abspath("font")
        
        if os.path.isdir(fonts_dir):
            fonts_escaped = fonts_dir.replace("\\", "/").replace(":", "\\:")
            sub_filter = f"subtitles='{ass_escaped}':fontsdir='{fonts_escaped}'"
        else:
            logger.warning(f"⚠️ پوشه فونت پیدا نشد: {fonts_dir}")
            sub_filter = f"subtitles='{ass_escaped}'"
            
        cmd = [
            "ffmpeg", "-y", "-nostdin",
            "-i", video_path,
        ]

        # 👈 چک میکنه هم فایل وجود داشته باشه، هم توی تنظیمات فعال شده باشه
        has_wm = ENABLE_WATERMARK and os.path.exists(WATERMARK_PATH)
        
        if has_wm:
            cmd.extend(["-i", WATERMARK_PATH])
            wm_target_h = max(30, int(v_h * WATERMARK_HEIGHT_RATIO))
            
            overlay_y = f"H-{margin_bottom}+{WATERMARK_GAP}"
            
            filter_complex = (
                f"[0:v]{sub_filter}[bg];"
                f"[1:v]scale=-1:{wm_target_h}[wm];"
                f"[bg][wm]overlay=x=(W-w)/2:y={overlay_y}[out]"
            )
        else:
            if not ENABLE_WATERMARK:
                logger.info("ℹ️ Watermark is disabled in settings.")
            else:
                logger.warning(f"⚠️ فایل واترمارک پیدا نشد ({WATERMARK_PATH}).")
            filter_complex = f"[0:v]{sub_filter}[out]"

        cmd.extend([
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-c:a", "copy",
            "-progress", "pipe:1",
            "-loglevel", "error",
            output_path,
        ])

        if progress:
            progress.update(stage=1, sub_text="در حال چسباندن زیرنویس روی ویدیو...", pct=0, force=True)

        err_tmp = tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".log", encoding="utf-8")
        err_path = err_tmp.name
        err_tmp.close()

        try:
            with open(err_path, "w", encoding="utf-8") as stderr_f:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=stderr_f,
                    stdin=subprocess.DEVNULL, text=True, bufsize=1,
                )
                for line in proc.stdout:
                    line = line.strip()
                    if line.startswith("out_time_us=") and duration > 0 and progress:
                        try:
                            us = int(line.split("=", 1)[1])
                            pct = min(100.0, us / (duration * 1_000_000) * 100)
                            # 👈 اینجا از stage=1 استفاده میکنیم که با مراحل جدید همخوانی داره
                            progress.update(stage=1, sub_text="در حال چسباندن روی ویدیو...", pct=pct)
                        except ValueError:
                            pass
                proc.wait()
        finally:
            if os.path.exists(err_path):
                try:
                    os.unlink(err_path)
                except OSError:
                    pass

        if not os.path.exists(output_path):
            raise RuntimeError("فایل خروجی ساخته نشد.")

        size_mb = os.path.getsize(output_path) / (1024 * 1024)

        # 👈 اینجا از stage=2 استفاده میکنیم (آپلود)
        if progress:
            progress.update(stage=2, sub_text="در حال ارسال به تلگرام...", pct=100, extra=f"📦 حجم ویدیو: {size_mb:.1f} MB", force=True)

        with open(output_path, "rb") as vf:
            bot.send_video(
                chat_id, vf,
                caption=f"✨ <b>ویدیوی زیرنویس‌شده آماده شد!</b>\n📦 {size_mb:.1f}MB",
                parse_mode="HTML",
                timeout=600,
            )
        return output_path

    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
