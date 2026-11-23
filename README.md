# SaveKeeper

Tự động backup save game Steam và giữ tiến trình khi bạn chơi bằng nhiều account khác nhau.

- Khi đang chơi: cứ **15 phút** backup một lần, và backup thêm ngay khi **thoát game**.
- Khi **mở game**: nếu save bị thay đổi lúc bạn không chơi (thường do Steam Cloud của account khác kéo save về), SaveKeeper khôi phục bản của bạn trước khi game kịp đọc.
- Với game lưu save **riêng theo account** (thư mục có ID account): mỗi lần đổi account, SaveKeeper tự chép tiến trình mới nhất sang account mới.
- **Không bao giờ xoá save**: chỉ ghi đè hoặc thêm file, và trước mỗi lần ghi đè luôn giữ lại một "bản an toàn".

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
python savekeeper.py install | uninstall | run | test-notify
```

Nhật ký hoạt động nằm ở `savekeeper.log` cạnh app.

## Kiểm thử

```
python -m unittest discover -s tests -v
```

Bộ test giả lập Steam (đồng hồ, account, game đang chạy, Steam Cloud đè save) để kiểm tra các tình huống: backup 15 phút/lần và khi thoát, Steam Cloud đè save, đổi account, mở lại game ngay sau khi thoát, backup lỗi, khôi phục bản cũ.
