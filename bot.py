"""
Facebook Groups → Telegram Bot
Chạy 24/7 trên Railway.app
"""
import asyncio
import hashlib
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
# (Điền vào Railway Dashboard → Variables)
# ══════════════════════════════════════════════════════════════

def get_env(key, default=None, required=False):
    val = os.environ.get(key, default)
    if required and not val:
        raise ValueError(f"Thiếu biến môi trường: {key}")
    return val


# Telegram
TELEGRAM_TOKEN   = get_env("TELEGRAM_TOKEN", required=True)
TELEGRAM_CHAT_ID = get_env("TELEGRAM_CHAT_ID", required=True)

# Tiêu đề thông báo
NOTIFICATION_HEADER = get_env("NOTIFICATION_HEADER", "BÀI MỚI")

# Từ khóa — phân cách bằng dấu phẩy
# Ví dụ: KEYWORDS=cần thuê,can thue,tìm thuê,tim thue
KEYWORDS = [
    k.strip()
    for k in get_env("KEYWORDS", "cần thuê,can thue,tìm thuê,tim thue").split(",")
    if k.strip()
]

# Danh sách nhóm — hỗ trợ 2 cách cấu hình:
#
# Cách 1 (khuyên dùng): Biến riêng cho từng nhóm
#   GROUP_1=Tên nhóm A|https://rss.app/feeds/xxx.xml
#   GROUP_2=Tên nhóm B|https://rss.app/feeds/yyy.xml
#   ... đến GROUP_20
#
# Cách 2: Một biến GROUPS, phân cách bằng dấu phẩy
#   GROUPS=Tên A|https://url-a.xml,Tên B|https://url-b.xml
GROUPS = []

# Cách 1: đọc GROUP_1 đến GROUP_20
for i in range(1, 21):
    val = os.environ.get(f"GROUP_{i}", "").strip()
    if val and "|" in val:
        name, url = val.split("|", 1)
        GROUPS.append({"name": name.strip(), "rss_url": url.strip()})

# Cách 2: đọc GROUPS nếu cách 1 không có nhóm nào
if not GROUPS:
    groups_raw = get_env("GROUPS", "")
    # Thử tách bằng dấu phẩy trước
    for entry in groups_raw.replace("\n", ",").split(","):
        entry = entry.strip()
        if "|" in entry:
            name, url = entry.split("|", 1)
            GROUPS.append({"name": name.strip(), "rss_url": url.strip()})

# Tần suất quét (phút)
CHECK_INTERVAL = int(get_env("CHECK_INTERVAL_MINUTES", "5"))

# Cài đặt khác
POSTS_TO_CHECK  = int(get_env("POSTS_TO_CHECK", "15"))
CASE_SENSITIVE  = get_env("CASE_SENSITIVE", "false").lower() == "true"
MIN_POST_LENGTH = int(get_env("MIN_POST_LENGTH", "10"))

# Database
DB_PATH = get_env("DB_PATH", "/data/fb_bot.db")


# ── Khởi động log ────────────────────────────────────────────
log.info("=" * 50)
log.info("Facebook Groups → Telegram Bot")
log.info(f"  Từ khóa ({len(KEYWORDS)}): {', '.join(KEYWORDS)}")
log.info(f"  Nhóm ({len(GROUPS)}): {', '.join(g['name'] for g in GROUPS)}")
log.info(f"  Quét mỗi: {CHECK_INTERVAL} phút")
log.info("=" * 50)

if not GROUPS:
    log.warning("⚠️  Chưa cấu hình nhóm nào! Điền biến GROUPS trong Railway Dashboard.")


# ══════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute(
        "CREATE TABLE IF NOT EXISTS seen_posts "
        "(post_id TEXT PRIMARY KEY, group_name TEXT, keywords TEXT, seen_at TEXT)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS stats "
        "(date TEXT PRIMARY KEY, checked INT DEFAULT 0, matched INT DEFAULT 0, sent INT DEFAULT 0)"
    )
    con.commit()
    return con


def is_seen(con, post_id):
    return con.execute(
        "SELECT 1 FROM seen_posts WHERE post_id=?", (post_id,)
    ).fetchone() is not None


def mark_seen(con, post_id, group_name, keywords):
    con.execute(
        "INSERT OR IGNORE INTO seen_posts VALUES (?,?,?,?)",
        (post_id, group_name, ",".join(keywords), datetime.now().isoformat()),
    )
    con.commit()


def update_stats(con, checked=0, matched=0, sent=0):
    today = datetime.now().strftime("%Y-%m-%d")
    con.execute(
        "INSERT INTO stats(date,checked,matched,sent) VALUES(?,?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET "
        "checked=checked+excluded.checked, "
        "matched=matched+excluded.matched, "
        "sent=sent+excluded.sent",
        (today, checked, matched, sent),
    )
    con.commit()


def get_stats(con):
    today = datetime.now().strftime("%Y-%m-%d")
    row = con.execute(
        "SELECT checked, matched, sent FROM stats WHERE date=?", (today,)
    ).fetchone()
    return row or (0, 0, 0)


# ══════════════════════════════════════════════════════════════
# KEYWORD MATCHING
# ══════════════════════════════════════════════════════════════

def find_keywords(text, keywords, case_sensitive):
    if not case_sensitive:
        text = text.lower()
        keywords = [k.lower() for k in keywords]
    return [k for k in keywords if k in text]


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
            # Lấy nội dung đầy đủ nhất
            content = ""
            if hasattr(entry, "content"):
                content = entry.content[0].get("value", "")
            if not content:
                content = entry.get("summary", "")
            if not content:
                content = entry.get("title", "")

            # Strip HTML
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
                "id":         post_id,
                "title":      title,
                "content":    content,
                "link":       link,
                "group_name": group_name,
            })

        log.info(f"[{group_name}] Lấy được {len(posts)} bài")
        return posts

    except httpx.HTTPStatusError as e:
        log.error(f"[{group_name}] HTTP {e.response.status_code}: {rss_url}")
        return []
    except Exception as e:
        log.error(f"[{group_name}] Lỗi fetch: {e}")
        return []


# ══════════════════════════════════════════════════════════════
# TELEGRAM SENDER
# ══════════════════════════════════════════════════════════════

async def send_notification(bot, post, matched_keywords):
    """
    Format:
    🔔 KHÁCH TÌM THUÊ: CẦN THUÊ
    ℹ️ 🪑 Nội dung bài đăng...
    --------------------
    👉 https://facebook.com/groups/.../posts/...
    """
    kw_upper = ", ".join(k.upper() for k in matched_keywords)
    preview  = post["content"][:200].strip()
    if len(post["content"]) > 200:
        preview += "..."
    if not preview or len(preview) < 5:
        preview = "Không rõ"

    message = (
        f"🔔 {NOTIFICATION_HEADER}: {kw_upper}\n"
        f"ℹ️ 🪑 {preview}\n"
        f"--------------------\n"
        f"👉 {post.get('link', '')}"
    )

    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            disable_web_page_preview=True,
        )
        log.info(f"✅ Đã gửi Telegram: [{post['group_name']}] {preview[:50]}...")
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

    total_checked = total_matched = total_sent = 0

    for group in GROUPS:
        posts = await fetch_rss(group["rss_url"], group["name"])
        total_checked += len(posts)

        for post in posts:
            if is_seen(db, post["id"]):
                continue

            search_text = post["title"] + " " + post["content"]
            matched = find_keywords(search_text, KEYWORDS, CASE_SENSITIVE)

            mark_seen(db, post["id"], group["name"], matched)

            if matched:
                total_matched += 1
                log.info(f"🎯 [{group['name']}] Từ khóa: {', '.join(matched)}")
                ok = await send_notification(bot, post, matched)
                if ok:
                    total_sent += 1
                await asyncio.sleep(1)

    update_stats(db, total_checked, total_matched, total_sent)
    c, m, s = get_stats(db)
    log.info(f"✅ Lượt này: {total_checked} bài | {total_matched} khớp | {total_sent} gửi")
    log.info(f"📊 Hôm nay:  {c} bài | {m} khớp | {s} gửi")


# ══════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════

async def main():
    db  = init_db()
    bot = Bot(token=TELEGRAM_TOKEN)

    # Kiểm tra kết nối Telegram
    try:
        me = await bot.get_me()
        log.info(f"✅ Telegram bot: @{me.username}")
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                f"🤖 Bot khởi động thành công!\n"
                f"📂 Theo dõi {len(GROUPS)} nhóm\n"
                f"🔑 Từ khóa: {', '.join(KEYWORDS)}\n"
                f"⏱ Quét mỗi {CHECK_INTERVAL} phút"
            ),
        )
    except Exception as e:
        log.error(f"❌ Không kết nối được Telegram: {e}")
        return

    # Scheduler
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

    # Chạy ngay lần đầu
    await check_all_groups(bot, db)

    # Giữ chương trình chạy
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("🛑 Bot dừng.")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
