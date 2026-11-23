# SaveKeeper

Tự động backup save game Steam và giữ tiến trình khi bạn chơi bằng nhiều account khác nhau.

- Khi đang chơi: cứ **15 phút** backup một lần, và backup thêm ngay khi **thoát game**.
- Khi **mở game**: nếu save bị thay đổi lúc bạn không chơi (thường do Steam Cloud của account khác kéo save về), SaveKeeper khôi phục bản của bạn trước khi game kịp đọc.
- Với game lưu save **riêng theo account** (thư mục có ID account): mỗi lần đổi account, SaveKeeper tự chép tiến trình mới nhất sang account mới.
- **Không bao giờ xoá save**: chỉ ghi đè hoặc thêm file, và trước mỗi lần ghi đè luôn giữ lại một "bản an toàn".
- **Đồng bộ nhiều máy** (tuỳ chọn): save được đẩy lên một kho chung (thư mục cloud, git repo hoặc server riêng). Sang máy khác, SaveKeeper tự tải bản mới nhất về.

Đã cấu hình sẵn: Cyberpunk 2077, The Witcher 3, The Witcher 2, The Witcher 1. Game nào chưa cài sẽ được bỏ qua.

## Cài đặt trên máy chơi game

1. Cài **Python 3.11 trở lên** ([python.org](https://www.python.org/downloads/), nhớ tick *Add python.exe to PATH*), hoặc mở PowerShell và chạy `winget install Python.Python.3.13`.
2. Chép cả thư mục này vào một chỗ cố định, ví dụ `D:\SaveKeeper`. Đừng để trong `Program Files` hay OneDrive.
3. Mở `SaveKeeper.bat` rồi chọn **4**. SaveKeeper chạy nền ngay (không có cửa sổ) và tự chạy mỗi khi bật máy.

Chọn **1** để xem trạng thái: SaveKeeper có đang chạy không, thư mục save của từng game và bản backup mới nhất.

## Có cần tắt Steam Cloud không?

Không bắt buộc, nhưng **nên tắt** cho các game chơi bằng nhiều account. Vào thư viện Steam, chuột phải game → **Properties → General** → bỏ tick **Keep game saves in the Steam Cloud**. Mỗi account làm một lần.

- Nếu **tắt**: save chỉ nằm trên máy, đổi account thoải mái, SaveKeeper chỉ còn việc backup.
- Nếu **để bật**: mỗi lần bấm Play, Steam có thể kéo save của account đó về đè lên save của bạn (hoặc hiện bảng *Cloud Sync Conflict*). SaveKeeper sẽ khôi phục lại bản của bạn ngay khi game mở và báo cho bạn biết. Tuy vậy, danh sách save trong game có thể lẫn save lạ, và save của bạn cũng bị đẩy lên cloud của account đó.

## Khôi phục save

Thoát game, mở `SaveKeeper.bat` → **3** → chọn game → chọn bản backup. Save hiện tại được giữ lại thành một bản an toàn trước khi bị ghi đè, nên muốn quay lại thì chọn bản đó.

Kho save nằm ở thư mục `vault` cạnh app. Mỗi bản backup là một thư mục thường, có thể mở bằng Explorer:

```
vault\cyberpunk2077\2026-11-23_21-15-00_periodic\files\...
```

File giống nhau giữa các bản chỉ được lưu một lần (hardlink), nên giữ 50 bản không tốn gấp 50 lần dung lượng. **Đừng sửa file trong `vault`**, vì một file có thể thuộc nhiều bản backup cùng lúc.

Tự chép một file save vào thư mục game (ví dụ save tải trên mạng)? Hãy chọn **2. Backup ngay** sau đó. Nếu không, lần mở game sau SaveKeeper sẽ tưởng save bị thay đổi ngoài ý muốn và khôi phục bản cũ (bản bạn chép vào vẫn được giữ lại thành bản an toàn).

## Đồng bộ nhiều máy

Chép `local.example.toml` thành `local.toml` rồi điền mục `[sync]`. File này chỉ nằm trên máy của bạn và không bị đưa lên git. Có ba kiểu kho chung:

| Kiểu | `remote` | Cần gì |
|---|---|---|
| Thư mục cloud (dễ nhất) | `'G:\My Drive\SaveKeeper'` | App Google Drive / OneDrive / Dropbox trên mỗi máy |
| Git repo (lưu tạm) | `'https://github.com/<tên>/game-save-data.git'` | Git đã đăng nhập GitHub. Nên dùng repo **private** riêng, không dùng repo chứa code |
| Server riêng | `'https://save.ten-mien.com'` + `token` | Một máy chủ chạy `server.py` (xem phần Server) |

Sau đó chọn **4** trong menu để SaveKeeper đọc lại cấu hình. Từ đó:

- Sau mỗi lần backup, bản mới được đẩy lên kho chung.
- Cứ 5 phút (lúc không chơi) SaveKeeper hỏi kho chung. Nếu máy khác có bản mới hơn mà máy này chưa chơi thêm gì, nó tự tải về và chép vào thư mục save.
- Nếu **cả hai máy cùng có tiến trình mới**, SaveKeeper không tự quyết. Bản của máy này được lưu lên kho làm bản phụ và bạn nhận được thông báo. Mở menu và chọn **7. Tải save từ kho chung** để lấy bản muốn chơi, hoặc **8. Đẩy save máy này lên** để dùng bản của máy này. Bản bạn chọn trở thành bản chung cho mọi máy.

**Sang máy mới:** cài Python, `git clone` repo này, tạo `local.toml` như trên, mở `SaveKeeper.bat` → **4**. Save tự về sau vài giây. Muốn tải tay thì chọn **7**.

Kiểu git: mỗi ngày SaveKeeper gộp lịch sử repo lại còn một commit để repo không phình to. GitHub không nhận file lớn hơn 100 MB.

## Server

`server.py` chỉ cần Python 3.11+, không cài thêm gì. Dữ liệu chỉ ai có token mới đọc hay ghi được.

```bash
# Trên máy chủ (Linux), trong thư mục repo:
cp .env.example .env          # sửa SAVEKEEPER_TOKEN thành chuỗi bí mật dài
docker compose up -d          # hoặc: SAVEKEEPER_TOKEN=... python3 server.py --data /srv/savekeeper
```

Server mặc định chỉ nghe ở `127.0.0.1:8765`. Để các máy chơi game kết nối được, chọn một trong hai cách:

- **Có tên miền:** dùng [Caddy](https://caddyserver.com/) để có HTTPS tự động: `caddy reverse-proxy --from save.ten-mien.com --to 127.0.0.1:8765`. Khi đó `remote = 'https://save.ten-mien.com'`.
- **Không có tên miền:** cài [Tailscale](https://tailscale.com/) trên server và các máy chơi game, đổi dòng `ports` trong `docker-compose.yml` thành `"<IP Tailscale>:8765:8765"`, rồi dùng `remote = 'http://<IP Tailscale>:8765'`. Kết nối Tailscale đã được mã hoá sẵn.

Đừng mở cổng 8765 ra internet bằng HTTP thường, vì token sẽ đi qua mạng dưới dạng chữ thường.

Server giữ 200 bản gần nhất cho mỗi game (`--keep`) và tự dọn dữ liệu cũ mỗi ngày.

## Thêm game

Mở `config.toml`, chép một khối `[[games]]` rồi sửa `name`, `id`, `save_dir`, `steam_appids`, `processes`. Hướng dẫn và danh sách biến (`{documents}`, `{saved_games}`, `{steam}`, `{steam_id3}`…) có ngay trong file. Thư mục save của từng game tra trên [PCGamingWiki](https://www.pcgamingwiki.com/), mục *Save game data location*.

Sửa xong thì chọn **4** trong menu để SaveKeeper đọc lại cấu hình.

## Giới hạn

- Một số game ghi SteamID **vào trong** file save (ví dụ Elden Ring, Dark Souls 3). Chép save sang account khác thì game không nhận, phải dùng thêm công cụ sửa ID riêng cho game đó.
- SaveKeeper nhận ra game đang chạy qua Steam (registry `RunningAppID`) hoặc qua tên file `.exe` trong `processes`.
- Khi chơi game toàn màn hình, Windows gom thông báo vào Notification Center thay vì hiện lên giữa trận.

## Gỡ

Mở `SaveKeeper.bat` → **5**. Kho save `vault` vẫn được giữ nguyên, muốn xoá thì xoá tay.

## Lệnh dòng lệnh

```
python savekeeper.py status | backup [game] | list [game] | restore [game] [bản|latest] [-y]
python savekeeper.py sync | pull [game] [id|latest] [-y] | push [game] [-y]
python savekeeper.py install | uninstall | run | test-notify
```

Nhật ký hoạt động nằm ở `savekeeper.log` cạnh app.

## Kiểm thử

```
python -m unittest discover -s tests -v
```

Bộ test giả lập Steam (đồng hồ, account, game đang chạy, Steam Cloud đè save) để kiểm tra các tình huống: backup 15 phút/lần và khi thoát, Steam Cloud đè save, đổi account, mở lại game ngay sau khi thoát, backup lỗi, khôi phục bản cũ.

Phần đồng bộ được test bằng hai "máy" giả lập dùng chung cả ba kiểu kho: thư mục, git repo (bare repo cục bộ) và `server.py` thật chạy trên localhost. Các tình huống: máy mới tải về, chơi qua lại hai máy, hai máy cùng có tiến trình mới, file bị sửa trên kho, đường dẫn độc hại, token sai, hai máy đẩy lên cùng lúc.
