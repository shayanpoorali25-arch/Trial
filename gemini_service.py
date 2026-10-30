"""
ارتباط با Gemini API — فقط مدل ۳.۸ با تلاش مجدد تصاعدی
"""

import os
import json
import base64
import logging
import time
import random
import requests

import config

logger = logging.getLogger(__name__)

# ===========================================
# پرامپت (بدون تغییر)
# ===========================================
SUBTITLE_PROMPT = """
تو یک متخصص زیرنویس‌نویسی حرفه‌ای هستی.
فایل صوتی زیر را با دقت گوش کن و آن را به صورت زیرنویس زمان‌بندی‌شده به فارسی برگردان.

قوانین:
- هر خط زیرنویس حداکثر ۴۲ کاراکتر باشد.
- زمان شروع و پایان هر خط را با فرمت SRT (HH:MM:SS,mmm) مشخص کن.
- متن فارسی روان و طبیعی باشد، نه ترجمه‌ی تحت‌اللفظی.
- اصطلاحات و اسامی خاص را حفظ کن.
- خروجی را دقیقاً به فرمت JSON زیر برگردان:

{
  "subtitles": [
    {
      "line_index": 1,
      "start_time": "00:00:01,000",
      "end_time": "00:00:03,500",
      "text": "متن فارسی این خط"
    }
  ]
}

فقط JSON خالص برگردان، بدون هیچ توضیح اضافه.
""".strip()

MAX_INLINE_BYTES = 20 * 1024 * 1024

def _encode_audio(audio_path: str) -> str:
    size = os.path.getsize(audio_path)
    if size > MAX_INLINE_BYTES:
        raise ValueError(
            f"حجم فایل صوتی ({size / 1024 / 1024:.1f}MB) از حد مجاز "
            f"Gemini ({MAX_INLINE_BYTES / 1024 / 1024}MB) بیشتره."
        )
    with open(audio_path, "rb") as f:
        data = f.read()
    logger.info(f"Audio file read: {size / 1024 / 1024:.2f}MB")
    return base64.b64encode(data).decode("utf-8")

def _detect_mime(audio_path: str) -> str:
    ext = os.path.splitext(audio_path)[1].lower()
    return {
        ".wav": "audio/wav", ".mp3": "audio/mp3",
        ".m4a": "audio/m4a", ".aac": "audio/aac",
        ".ogg": "audio/ogg", ".flac": "audio/flac",
        ".webm": "audio/webm",
    }.get(ext, "audio/wav")

def _build_payload(audio_b64: str, mime_type: str) -> dict:
    return {
        "contents": [{
            "parts": [
                {"text": SUBTITLE_PROMPT},
                {"inline_data": {"mime_type": mime_type, "data": audio_b64}},
            ]
        }],
        "generationConfig": {
            "temperature": 0.2,
            "response_mime_type": "application/json",
        },
    }

def _extract_subtitles(response_json: dict) -> dict:
    try:
        text = response_json["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise ValueError(f"Unexpected response structure: {response_json}") from e

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        cleaned = text.strip().strip("`").removeprefix("json").strip()
        parsed = json.loads(cleaned)

    subtitles = parsed.get("subtitles")
    if not isinstance(subtitles, list) or not subtitles:
        raise ValueError("Response does not contain a valid 'subtitles' key.")
    return parsed

def _sleep_backoff(attempt: int, base: float = 5.0, max_wait: float = 300.0):
    """
    Exponential backoff with jitter.
    attempt=1 -> ~5s, attempt=2 -> ~10s, attempt=3 -> ~20s, ... max 300s.
    """
    wait = min(base * (2 ** (attempt - 1)), max_wait)
    jitter = random.uniform(0, 1.5)  # کمی تصادفی بودن برای جلوگیری از همزمانی
    total = wait + jitter
    logger.info(f"Waiting {total:.1f} seconds before next retry...")
    time.sleep(total)

# ===========================================
# تابع اصلی — فقط مدل ۳.۸
# ===========================================
def transcribe_audio(audio_path: str) -> dict:
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    if not config.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not set in config.py.")

    audio_b64 = _encode_audio(audio_path)
    mime_type = _detect_mime(audio_path)
    payload = _build_payload(audio_b64, mime_type)

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": config.GEMINI_API_KEY,
    }

    # 👈 فقط مدل ۳.۸ و ۱۲ بار تلاش
    model = "gemini-3.8-flash"
    max_retries = 12
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    last_error = None

    for attempt in range(1, max_retries + 1):
        logger.info(f"📡 [{model}] Attempt {attempt}/{max_retries} — Sending request...")
        t0 = time.time()

        try:
            # timeout=300 چون سرور گوگل حداکثر ۵ دقیقه اجازه می‌دهد[reference:4]
            response = requests.post(api_url, headers=headers, json=payload, timeout=300)
        except requests.RequestException as e:
            last_error = e
            logger.warning(f"Network error on attempt {attempt}: {e}")
            if attempt < max_retries:
                _sleep_backoff(attempt)
            continue

        elapsed = time.time() - t0
        logger.info(f"[{model}] Responded in {elapsed:.1f}s (Status: {response.status_code})")

        if response.status_code == 200:
            result = _extract_subtitles(response.json())
            logger.info(f"✅ Subtitles received: {len(result['subtitles'])} lines.")
            return result

        # خطاهای قابل تلاش مجدد (503، 429، 500، 502، 504)
        if response.status_code in (503, 429, 500, 502, 504):
            body = response.text[:300]
            logger.warning(f"⚠️ {model} → HTTP {response.status_code}: {body}")
            last_error = f"HTTP {response.status_code}: {body}"

            if attempt < max_retries:
                _sleep_backoff(attempt)
                continue
            else:
                break

        # خطاهای غیرقابل جبران
        raise RuntimeError(
            f"Gemini API error (status {response.status_code}): {response.text[:500]}"
        )

    raise RuntimeError(
        f"All {max_retries} attempts for {model} failed. Last error: {last_error}"
    )
