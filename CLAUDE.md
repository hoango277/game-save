# SaveKeeper: báo cáo dự án

Công cụ Windows tự backup save game Steam và giữ tiến trình khi người chơi dùng **nhiều account Steam** và **nhiều máy**. Người dùng chính chơi game offline (Cyberpunk 2077, The Witcher...) bằng nhiều account. Máy chơi game khác máy phát triển, và máy phát triển không cài Steam.

## Vấn đề gốc

Đổi account Steam thì mất tiến trình vì ba lý do:

1. Thư mục save chứa ID account (`Steam\userdata\<id3>\...`).
2. **Steam Cloud** của account mới kéo save khác về, đè lên thư mục save dùng chung. Đây là nguyên nhân chính với Cyberpunk và Witcher 3.
3. Một số game ghi SteamID vào trong file save (Elden Ring, DS3). Trường hợp này **chưa hỗ trợ**.

## Kiến trúc

| File | Vai trò |
|---|---|
| `savekeeper.py` | App chính, chạy trên Windows: cấu hình, `Vault` (kho cục bộ), `Watcher` (theo dõi chạy nền), `Syncer` (đồng bộ nhiều máy), menu/CLI, tích hợp Windows (registry, toast, mutex, autostart) |
| `savestore.py` | Kho chung, dùng chung cho client và server: `DirStore` (thư mục), `GitStore` (git repo), `HttpStore` (client của server), `gc` |
| `server.py` | Server HTTP chỉ dùng thư viện chuẩn, token Bearer, API `/v1/...`, tự dọn kho mỗi ngày |
| `config.toml` | Danh sách game (được đưa lên git). `local.toml` là cấu hình riêng từng máy, có mục `[sync]` và token (gitignored, ghi đè và nối thêm `[[games]]`) |
| `SaveKeeper.bat` | Mở menu, tìm Python qua `py -3` rồi tới `python` |
| `Dockerfile`, `docker-compose.yml`, `.env.example` | Triển khai server (mặc định chỉ nghe 127.0.0.1:8765) |
| `tests/test_savekeeper.py` | 20 test, giả lập Steam bằng `FakeEnv` (đồng hồ, account, AppID, process) |
| `tests/test_sync.py` | 28 test, hai máy giả lập × ba kiểu kho, server HTTP thật trên localhost |

Chỉ dùng thư viện chuẩn Python 3.11+ (`tomllib`, `ctypes`, `winreg`, `msvcrt`, `urllib`, `http.server`).

### Kho cục bộ (`Vault`)

- `vault/<game>/<YYYY-MM-DD_HH-MM-SS>_<tag>/files/...` cộng `snapshot.json` (seq, tag, trusted, account, files: size/mtime_ns/sha256).
- File không đổi được **hardlink** với bản trước. Bản đang tạo nằm trong `.tmp-*`, xong mới `os.rename`.
- Bản **tin cậy** (`startup`, `periodic`, `exit`, `manual`, `restore`, `pull`) làm mốc để khôi phục. Bản **an toàn** (`before-restore`) không bao giờ được dùng làm nguồn khôi phục tự động.
- `restore()` chỉ **ghi đè hoặc thêm, không bao giờ xoá**, và luôn giữ lại một bản an toàn trước khi ghi đè.

### Theo dõi chạy nền (`Watcher`, vòng lặp 2 giây)

- Biết game đang chạy qua `HKCU\Software\Valve\Steam\RunningAppID` (chỉ tin khi `steam.exe` đang chạy) hoặc qua tên process. Account đang dùng lấy từ `...\ActiveProcess\ActiveUser`.
- Đang chơi thì 15 phút backup một lần (đợi tới khi thư mục save "yên"). Thoát game thì đợi 5 giây rồi backup.
- **Lúc mở game (guard):** nếu save khác bản tin cậy mới nhất thì khôi phục lại (chủ yếu để chống Steam Cloud).
- Bất biến quan trọng, mỗi điều đều có test:
  - Còn bản backup lúc thoát chưa làm thì phải làm trước khi guard chạy. Nếu không, mở lại game ngay sau khi thoát sẽ bị lùi tiến trình.
  - `dirty` (backup lúc thoát bị lỗi) thì guard và chép save sang account mới đều bị chặn.
- Game có `{steam_id3}`/`{steam_id64}` trong `save_dir`: khi đổi account, chép bản mới nhất sang thư mục của account mới.
- Đồng bộ (`_sync_round`) chạy **cuối mỗi tick**, sau guard, để mạng chậm không làm trễ bước bảo vệ save.

### Đồng bộ nhiều máy (`Syncer` + `savestore`)

- Kho chung: `objects/<sha[:2]>/<sha>` (theo nội dung), `games/<id>/snapshots/<id>.json`, `games/<id>/head.json`.
- Id bản: `YYYYmmdd-HHMMSS-<máy>-<content[:8]>`. `content_id` là sha256 của danh sách (đường dẫn, sha), không tính mtime.
- Mỗi máy nhớ `vault/<game>/.sync.json` = {base: head đã đồng bộ, content}. Có bốn trường hợp:
  - Chỉ máy này đổi: đẩy lên, rồi `put_head` kiểu compare-and-swap.
  - Chỉ head đổi: tải về (`import_snapshot`, dùng lại file theo sha), rồi `restore`. Chỉ làm khi game không chạy và không `dirty`.
  - Cả hai cùng đổi: **xung đột**. Bản của máy này được đẩy lên làm bản phụ, gửi thông báo một lần, không tự quyết. Người dùng chọn qua `pull` (bản được chọn thành head) hoặc `push`.
- `GitStore`: clone `--depth 1` vào `vault/.git-store`. CAS dựa vào việc push bị từ chối (non-fast-forward), khi đó reset về remote. `gc` mỗi ngày gộp lịch sử thành một commit bằng `commit-tree` cộng `--force-with-lease`.
- Bảo mật: tên và sha được kiểm tra bằng regex ở cả client và server, đường dẫn file tải về bị chặn `..`, ổ đĩa và `\` (`_check_rel`), nội dung được kiểm sha256 khi upload và download, token so sánh bằng `hmac.compare_digest`.

## Trạng thái (23/11/2026)

- **48/48 test đạt:** `python -m unittest discover -s tests -v`, chạy khoảng 30 giây vì có git và HTTP.
- Đã chạy thật trên máy phát triển: known folders, liệt kê process, mutex, toast PowerShell, chạy nền + dừng bằng `savekeeper.stop`, CLI `status`/`sync`, kiểm tra token của server.
- **Chưa kiểm chứng với Steam và game thật**: giá trị registry `ActiveUser`/`RunningAppID` trên máy chơi game, đường dẫn save Witcher 1/2. Cách kiểm tra: mở game rồi chạy `SaveKeeper.bat` → 1, phải thấy `[ĐANG CHƠI]`.
- Server chưa được triển khai ở đâu. Người dùng chưa chọn kiểu kho chung (thư mục cloud, git hay server).

## Giới hạn và việc có thể làm tiếp

- Game ghi SteamID trong file save (Elden Ring, DS3...) cần thêm module sửa ID riêng cho từng game.
- Steam Cloud vẫn nên tắt cho các game chơi nhiều account (Properties → General). Guard chỉ sửa được sau khi Steam đã đồng bộ xong.
- Khi xung đột chưa có gộp file: Cyberpunk và Witcher lưu mỗi save một file nên về lý thuyết gộp được.
- Chưa có icon khay hệ thống. Thông báo dùng toast qua PowerShell, AUMID của PowerShell.
- Có thể đóng gói `.exe` (PyInstaller) để máy chơi game không cần cài Python. Nhược điểm là dễ bị Defender báo nhầm.

## Quy ước khi làm việc trên repo này

- Trả lời người dùng bằng **tiếng Việt**. Chuỗi hiển thị trong app cũng là tiếng Việt. File `.bat` chỉ dùng ASCII.
- Không thêm thư viện ngoài. Mọi thay đổi phải giữ cho `python -m unittest discover -s tests` đạt hết.
- Không bao giờ để app xoá save của người chơi. Mọi lần ghi đè phải có bản an toàn trước.
- Không commit `local.toml`, `.env`, `vault/`, log.
- **Làm xong thì commit và push lên `origin main` (https://github.com/hoango277/game-save) mà không cần hỏi lại.** Đây là yêu cầu của người dùng.
