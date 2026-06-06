# Facebook Groups → Telegram Bot

Bot theo dõi bài đăng nhóm Facebook theo từ khóa, tự động gửi thông báo lên Telegram.
Chạy 24/7 trên Railway.app — không cần mở máy tính.

---

## Deploy lên Railway (10 phút)

### Bước 1 — Tạo tài khoản GitHub & upload code
1. Vào https://github.com → Đăng ký / Đăng nhập
2. Nhấn **New repository** → đặt tên `fb-telegram-bot` → Create
3. Upload toàn bộ file trong thư mục này lên repo

### Bước 2 — Tạo project trên Railway
1. Vào https://railway.app → **Login with GitHub**
2. Nhấn **New Project** → **Deploy from GitHub repo**
3. Chọn repo `fb-telegram-bot` vừa tạo
4. Railway tự build và deploy

### Bước 3 — Điền biến môi trường (Variables)
Vào project → tab **Variables** → thêm từng biến:

| Tên biến | Giá trị | Bắt buộc |
|----------|---------|----------|
| `TELEGRAM_TOKEN` | Token từ @BotFather | ✅ |
| `TELEGRAM_CHAT_ID` | Chat ID từ @userinfobot | ✅ |
| `GROUPS` | Xem định dạng bên dưới | ✅ |
| `KEYWORDS` | cần thuê,can thue,tìm thuê | ✅ |
| `NOTIFICATION_HEADER` | KHÁCH TÌM THUÊ | ✅ |
| `CHECK_INTERVAL_MINUTES` | 5 | ✅ |
| `DB_PATH` | /data/fb_bot.db | ✅ |

### Định dạng biến GROUPS
Mỗi nhóm một dòng, tên và RSS URL cách nhau bởi dấu `|`

```
Nhóm BĐS Hà Nội|https://rss.app/feeds/v1.1/~XXXXX.xml
Nhóm BĐS HCM|https://rss.app/feeds/v1.1/~YYYYY.xml
Nhóm Tuyển dụng|https://rss.app/feeds/v1.1/~ZZZZZ.xml
```

Trong Railway Dashboard, nhập vào ô giá trị của `GROUPS` — xuống dòng bằng `\n`.

### Bước 4 — Thêm Volume (lưu database)
1. Vào project → tab **Volumes** → **Add Volume**
2. Mount path: `/data`
3. Bot sẽ lưu database vào `/data/fb_bot.db` — không mất khi restart

### Bước 5 — Kiểm tra
- Vào tab **Deployments** → xem log
- Nếu thấy `✅ Telegram bot: @TenBot` → bot đang chạy
- Telegram của bạn sẽ nhận tin nhắn chào

---

## Cập nhật từ khóa / nhóm
Chỉ cần vào Railway Dashboard → Variables → sửa → Save.
Railway tự động restart bot với cấu hình mới.

---

## Định dạng thông báo Telegram
```
🔔 KHÁCH TÌM THUÊ: CẦN THUÊ
ℹ️ 🪑 Nội dung bài đăng...
--------------------
👉 https://www.facebook.com/groups/.../posts/...
```
