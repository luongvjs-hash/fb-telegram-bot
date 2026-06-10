"""
Facebook Groups → Telegram Bot + Gemini AI Filter
Chạy 24/7 trên Railway.app
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime

import feedparser
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# CẤU HÌNH — đọc từ biến môi trường Railway
# ══════════════════════════════════════════════════════════════

def get_env(key, default=None, required=False):
    val = os.environ.get(key, default)
    if required and not val:
        raise ValueError(f"Thiếu biến môi trường: {key}")
    return val


# Telegram
TELEGRAM_TOKEN   = get_env("TELEGRAM_TOKEN", required=True)
TELEGRAM_CHAT_ID = get_env("TELEGRAM_CHAT_ID", required=True)
NOTIFICATION_HEADER = get_env("NOTIFICATION_HEADER", "BÀI MỚI")

# Gemini AI
GEMINI_API_KEY = get_env("GEMINI_API_KEY", "")
GEMINI_ENABLED = bool(GEMINI_API_KEY)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"

# BOT_MODE: "rent" = tìm thuê | "buy" = tìm mua | "both" = cả hai
BOT_MODE = get_env("BOT_MODE", "both").lower()

# Từ khóa
KEYWORDS = [
    k.strip()
    for k in get_env("KEYWORDS", "cần thuê,can thue,tìm thuê,tim thue").split(",")
    if k.strip()
]

# Danh sách nhóm — GROUP_1 đến GROUP_20
GROUPS = []
for i in range(1, 21):
    val = os.environ.get(f"GROUP_{i}", "").strip()
    if val and "|" in val:
        name, url = val.split("|", 1)
        GROUPS.append({"name": name.strip(), "rss_url": url.strip()})

if not GROUPS:
    groups_raw = get_env("GROUPS", "")
    for entry in groups_raw.replace("\n", ",").split(","):
        entry = entry.strip()
        if "|" in entry:
            name, url = entry.split("|", 1)
            GROUPS.append({"name": name.strip(), "rss_url": url.strip()})

CHECK_INTERVAL  = int(get_env("CHECK_INTERVAL_MINUTES", "5"))
POSTS_TO_CHECK  = int(get_env("POSTS_TO_CHECK", "15"))
CASE_SENSITIVE  = get_env("CASE_SENSITIVE", "false").lower() == "true"
MIN_POST_LENGTH = int(get_env("MIN_POST_LENGTH", "10"))
DB_PATH         = get_env("DB_PATH", "/data/fb_bot.db")

log.info("=" * 55)
log.info("Facebook Groups → Telegram Bot + Gemini AI")
log.info(f"  Mode: {BOT_MODE} | AI: {'✅ Bật' if GEMINI_ENABLED else '❌ Tắt'}")
log.info(f"  Từ khóa ({len(KEYWORDS)}): {', '.join(KEYWORDS)}")
log.info(f"  Nhóm ({len(GROUPS)}): {', '.join(g['name'] for g in GROUPS)}")
log.info(f"  Quét mỗi: {CHECK_INTERVAL} phút")
log.info("=" * 55)

if not GROUPS:
    log.warning("⚠️  Chưa cấu hình nhóm nào!")
if not GEMINI_ENABLED:
    log.warning("⚠️  GEMINI_API_KEY chưa điền — chạy không có AI filter")


# ══════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute(
        "CREATE TABLE IF NOT EXISTS seen_posts "
        "(post_id TEXT PRIMARY KEY, group_name TEXT, keywords TEXT, "
        "ai_result TEXT, seen_at TEXT)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS stats "
        "(date TEXT PRIMARY KEY, checked INT DEFAULT 0, matched INT DEFAULT 0, "
        "ai_passed INT DEFAULT 0, ai_rejected INT DEFAULT 0, sent INT DEFAULT 0)"
    )
    con.commit()
    return con

def is_seen(con, post_id):
    return con.execute(
        "SELECT 1 FROM seen_posts WHERE post_id=?", (post_id,)
    ).fetchone() is not None

def mark_seen(con, post_id, group_name, keywords, ai_result=""):
    con.execute(
        "INSERT OR IGNORE INTO seen_posts VALUES (?,?,?,?,?)",
        (post_id, group_name, ",".join(keywords), ai_result, datetime.now().isoformat()),
    )
    con.commit()

def update_stats(con, checked=0, matched=0, ai_passed=0, ai_rejected=0, sent=0):
    today = datetime.now().strftime("%Y-%m-%d")
    con.execute(
        "INSERT INTO stats(date,checked,matched,ai_passed,ai_rejected,sent) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
        "checked=checked+excluded.checked, matched=matched+excluded.matched, "
        "ai_passed=ai_passed+excluded.ai_passed, "
        "ai_rejected=ai_rejected+excluded.ai_rejected, sent=sent+excluded.sent",
        (today, checked, matched, ai_passed, ai_rejected, sent),
    )
    con.commit()

def get_stats(con):
    today = datetime.now().strftime("%Y-%m-%d")
    row = con.execute(
        "SELECT checked, matched, ai_passed, ai_rejected, sent "
        "FROM stats WHERE date=?", (today,)
    ).fetchone()
    return row or (0, 0, 0, 0, 0)


# ══════════════════════════════════════════════════════════════
# KEYWORD MATCHING
# ══════════════════════════════════════════════════════════════

def find_keywords(text, keywords, case_sensitive):
    if not case_sensitive:
        text = text.lower()
        keywords = [k.lower() for k in keywords]
    return [k for k in keywords if k in text]


# ══════════════════════════════════════════════════════════════
# GEMINI AI FILTER
# ══════════════════════════════════════════════════════════════

def build_prompt(post_content, mode):
    """Tạo prompt phù hợp với từng mode bot."""
    if mode == "rent":
        intent = "khách hàng đang TÌM THUÊ bất động sản (người đi thuê)"
        counter = "người cho thuê, môi giới, saller, agency đăng quảng cáo"
        examples_yes = [
            "Mình cần thuê căn 2PN tại Vinhomes, budget 10tr/tháng",
            "Gia đình 4 người tìm thuê nhà khu Smart City, có thể nhận ngay",
            "Tìm thuê studio hoặc 1PN gần trung tâm, giá tốt",
        ]
        examples_no = [
            "Cho thuê căn hộ 2PN full nội thất, giá 12tr/tháng, ai cần thuê liên hệ",
            "Nhận ký gửi cho thuê BĐS khu vực Vinhomes, tìm thuê liên hệ ngay",
            "CĐT mở bán, cần thuê văn phòng đại lý phân phối",
        ]
    elif mode == "buy":
        intent = "khách hàng đang TÌM MUA bất động sản (người mua)"
        counter = "người bán, môi giới, saller, agency đăng bán hàng"
        examples_yes = [
            "Gia đình mình cần mua căn 2-3PN tại Vinhomes, tài chính 3-4 tỷ",
            "Tìm mua căn góc tầng cao, chính chủ, không qua môi giới",
            "Muốn mua căn hộ Smart City để ở, budget linh hoạt",
        ]
        examples_no = [
            "Cần bán gấp căn 2PN view hồ, chính chủ cần tiền, ai mua liên hệ",
            "Nhận ký gửi mua bán BĐS, tìm mua tìm bán liên hệ ngay",
            "Mở bán block mới, cần mua liên hệ để được hỗ trợ",
        ]
    else:  # both
        intent = "khách hàng đang TÌM MUA hoặc TÌM THUÊ bất động sản"
        counter = "người bán, người cho thuê, môi giới, saller, agency"
        examples_yes = [
            "Mình cần thuê căn 2PN tại Vinhomes, budget 10tr/tháng",
            "Gia đình tìm mua căn hộ Smart City, tài chính 3 tỷ",
            "Tìm thuê hoặc mua studio khu vực này",
        ]
        examples_no = [
            "Cho thuê căn hộ 2PN full nội thất, ai cần thuê liên hệ",
            "Nhận ký gửi mua bán cho thuê BĐS, tìm mua tìm thuê liên hệ",
            "Cần bán gấp 3 căn, chiết khấu cao, ai mua liên hệ ngay",
        ]

    yes_str = "\n".join(f'  - "{e}"' for e in examples_yes)
    no_str  = "\n".join(f'  - "{e}"' for e in examples_no)

    return f"""Bạn là AI chuyên phân tích bài đăng trong nhóm Facebook bất động sản Việt Nam.

Nhiệm vụ: Xác định bài đăng dưới đây có phải là {intent} không.

Phân loại:
- YES: Bài của {intent}
- NO: Bài của {counter}
- UNSURE: Không thể xác định rõ ràng

Ví dụ YES (cần gửi thông báo):
{yes_str}

Ví dụ NO (bỏ qua):
{no_str}

Lưu ý quan trọng:
- Môi giới thường dùng: "nhận ký gửi", "liên hệ ngay", "hỗ trợ mua bán", "chuyên mua bán"
- Người bán/cho thuê thường: liệt kê chi tiết giá, diện tích, nội thất để MỜI người khác
- Khách hàng thật thường: nêu NHU CẦU, ngân sách, yêu cầu cụ thể của họ
- Bài UNSURE: vừa có nhu cầu vừa có quảng cáo, hoặc không rõ vai trò

Bài đăng cần phân tích:
\"\"\"
{post_content[:800]}
\"\"\"

Trả lời bằng JSON, không giải thích thêm:
{{"result": "YES|NO|UNSURE", "confidence": 0-100, "reason": "lý do ngắn gọn"}}"""


async def gemini_classify(post_content, mode):
    """
    Gọi Gemini API để phân loại bài đăng.
    Trả về: ("YES"|"NO"|"UNSURE", confidence, reason)
    """
    if not GEMINI_ENABLED:
        return "YES", 100, "AI tắt — bỏ qua lọc"

    prompt = build_prompt(post_content, mode)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 200,
        }
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{GEMINI_URL}?key={GEMINI_API_KEY}",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        # Lấy text response
        raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()

        # Parse JSON từ response
        raw = re.sub(r"```json|```", "", raw).strip()
        parsed = json.loads(raw)

        result     = parsed.get("result", "UNSURE").upper()
        confidence = int(parsed.get("confidence", 50))
        reason     = parsed.get("reason", "")

        if result not in ("YES", "NO", "UNSURE"):
            result = "UNSURE"

        log.info(f"  🤖 Gemini: {result} ({confidence}%) — {reason}")
        return result, confidence, reason

    except json.JSONDecodeError:
        log.warning(f"  ⚠️  Gemini trả về không phải JSON: {raw[:100]}")
        return "UNSURE", 50, "Lỗi parse JSON"
    except Exception as e:
        log.error(f"  ❌ Gemini lỗi: {e}")
        # Nếu API lỗi → cho qua để không bỏ sót bài
        return "YES", 50, f"API lỗi: {str(e)[:50]}"


# ══════════════════════════════════════════════════════════════
# RSS FETCHER
# ══════════════════════════════════════════════════════════════

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; FeedParser/6.0)",
    "Accept": "application/rss+xml, application/atom+xml, */*",
}

async def fetch_rss(rss_url, group_name):
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(rss_url, headers=RSS_HEADERS)
            resp.raise_for_status()

        feed = feedparser.parse(resp.text)
        if feed.bozo and not feed.entries:
            log.warning(f"[{group_name}] RSS lỗi: {feed.bozo_exception}")
            return []

        posts = []
        for entry in feed.entries[:POSTS_TO_CHECK]:
            content = ""
            if hasattr(entry, "content"):
                content = entry.content[0].get("value", "")
            if not content:
                content = entry.get("summary", "")
            if not content:
                content = entry.get("title", "")

            content = re.sub(r"<[^>]+>", " ", content).strip()
            content = re.sub(r"\s+", " ", content)
            title   = re.sub(r"<[^>]+>", "", entry.get("title", "")).strip()
            link    = entry.get("link", "")
            post_id = (
                entry.get("id", "")
                or link
                or hashlib.md5(content[:200].encode()).hexdigest()
            )

            if len(content) < MIN_POST_LENGTH:
                continue

            posts.append({
                "id": post_id, "title": title,
                "content": content, "link": link,
                "group_name": group_name,
            })

        log.info(f"[{group_name}] Lấy được {len(posts)} bài")
        return posts

    except httpx.HTTPStatusError as e:
        log.error(f"[{group_name}] HTTP {e.response.status_code}")
        return []
    except Exception as e:
        log.error(f"[{group_name}] Lỗi fetch: {e}")
        return []


# ══════════════════════════════════════════════════════════════
# TELEGRAM SENDER
# ══════════════════════════════════════════════════════════════

async def send_notification(bot, post, matched_keywords, ai_result="YES", ai_reason=""):
    kw_upper = ", ".join(k.upper() for k in matched_keywords)
    preview  = post["content"][:200].strip()
    if len(post["content"]) > 200:
        preview += "..."
    if not preview or len(preview) < 5:
        preview = "Không rõ"

    # Tiêu đề thay đổi theo AI result
    if ai_result == "UNSURE":
        header_line = f"⚠️ {NOTIFICATION_HEADER} (không chắc chắn): {kw_upper}"
    else:
        header_line = f"🔔 {NOTIFICATION_HEADER}: {kw_upper}"

    message = (
        f"{header_line}\n"
        f"ℹ️ 🪑 {preview}\n"
        f"--------------------\n"
        f"👉 {post.get('link', '')}"
    )

    # Thêm lý do AI nếu là UNSURE
    if ai_result == "UNSURE" and ai_reason:
        message += f"\n🤖 AI: {ai_reason}"

    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            disable_web_page_preview=True,
        )
        log.info(f"✅ Đã gửi: [{post['group_name']}] {preview[:50]}...")
        return True
    except Exception as e:
        log.error(f"❌ Telegram lỗi: {e}")
        return False


# ══════════════════════════════════════════════════════════════
# MAIN CHECK LOOP
# ══════════════════════════════════════════════════════════════

async def check_all_groups(bot, db):
    now = datetime.now().strftime("%H:%M:%S")
    log.info(f"[{now}] 🔍 Kiểm tra {len(GROUPS)} nhóm...")

    total_checked = total_matched = total_ai_passed = total_ai_rejected = total_sent = 0

    for group in GROUPS:
        posts = await fetch_rss(group["rss_url"], group["name"])
        total_checked += len(posts)

        for post in posts:
            if is_seen(db, post["id"]):
                continue

            # Tầng 1: lọc từ khóa
            search_text = post["title"] + " " + post["content"]
            matched = find_keywords(search_text, KEYWORDS, CASE_SENSITIVE)

            if not matched:
                mark_seen(db, post["id"], group["name"], [], "NO_KEYWORD")
                continue

            total_matched += 1
            log.info(f"🎯 [{group['name']}] Từ khóa: {', '.join(matched)}")

            # Tầng 2: Gemini AI phân loại
            ai_result, confidence, ai_reason = await gemini_classify(
                post["content"], BOT_MODE
            )

            mark_seen(db, post["id"], group["name"], matched, ai_result)

            if ai_result == "NO":
                # Saller/môi giới → bỏ qua
                total_ai_rejected += 1
                log.info(f"  🚫 AI lọc bỏ: {ai_reason}")
                continue

            # YES hoặc UNSURE → gửi Telegram
            total_ai_passed += 1
            ok = await send_notification(bot, post, matched, ai_result, ai_reason)
            if ok:
                total_sent += 1
            await asyncio.sleep(1)

    update_stats(db, total_checked, total_matched, total_ai_passed, total_ai_rejected, total_sent)
    c, m, ap, ar, s = get_stats(db)
    log.info(f"✅ Lượt này: {total_checked} bài | {total_matched} khớp kw | "
             f"{total_ai_passed} AI pass | {total_ai_rejected} AI reject | {total_sent} gửi")
    log.info(f"📊 Hôm nay:  {c} bài | {m} khớp | {ap} pass | {ar} reject | {s} gửi")


# ══════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════

async def main():
    db  = init_db()
    bot = Bot(token=TELEGRAM_TOKEN)

    try:
        me = await bot.get_me()
        log.info(f"✅ Telegram bot: @{me.username}")
        ai_status = "✅ Gemini AI bật" if GEMINI_ENABLED else "⚠️ Không có AI (chỉ lọc từ khóa)"
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                f"🤖 Bot khởi động thành công!\n"
                f"📂 Theo dõi {len(GROUPS)} nhóm\n"
                f"🔑 Từ khóa: {', '.join(KEYWORDS)}\n"
                f"🧠 {ai_status}\n"
                f"⏱ Quét mỗi {CHECK_INTERVAL} phút"
            ),
        )
    except Exception as e:
        log.error(f"❌ Không kết nối được Telegram: {e}")
        return

    scheduler = AsyncIOScheduler(timezone="Asia/Ho_Chi_Minh")
    scheduler.add_job(
        check_all_groups,
        trigger="interval",
        minutes=CHECK_INTERVAL,
        jitter=int(CHECK_INTERVAL * 60 * 0.1),
        args=[bot, db],
        id="check_job",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info(f"🚀 Scheduler khởi động — quét mỗi {CHECK_INTERVAL} phút")

    await check_all_groups(bot, db)

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("🛑 Bot dừng.")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
