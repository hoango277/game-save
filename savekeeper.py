#!/usr/bin/env python3
"""SaveKeeper: tự backup save game và giữ tiến trình khi đổi account Steam.

    python savekeeper.py            mở menu
    python savekeeper.py run        chạy nền (lệnh dùng khi tự khởi động cùng Windows)
    python savekeeper.py --help     xem tất cả lệnh

Xem README.md để biết cách dùng.
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import fnmatch
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
import uuid
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("SaveKeeper cần Python 3.11 trở lên.")

import tomllib

IS_WINDOWS = os.name == "nt"
if IS_WINDOWS:
    import ctypes
    import msvcrt
    import winreg
    from ctypes import wintypes

APP_NAME = "SaveKeeper"
APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.toml"
LOG_PATH = APP_DIR / "savekeeper.log"
STOP_FLAG = APP_DIR / "savekeeper.stop"

STEAM_ID64_BASE = 76561197960265728
MANIFEST = "snapshot.json"
TMP_SUFFIX = ".savekeeper-tmp"
MUTEX_NAME = "Local\\SaveKeeper.Watcher"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# Nhãn hiển thị cho từng loại bản backup.
TAG_LABELS = {
    "startup": "lúc SaveKeeper khởi động",
    "periodic": "định kỳ khi đang chơi",
    "exit": "khi thoát game",
    "manual": "backup thủ công",
    "restore": "sau khi khôi phục",
    "before-restore": "bản bị thay thế (giữ để an toàn)",
}
# Bản "an toàn" chỉ để phòng hờ, không bao giờ được dùng làm nguồn khôi phục tự động.
SAFETY_TAGS = {"before-restore"}

# Biến dùng trong save_dir -> FOLDERID của Windows (đúng cả khi OneDrive chuyển chỗ Documents).
KNOWN_FOLDERS = {
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "saved_games": "4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4",
    "appdata": "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D",
    "localappdata": "F1B32785-6FBA-4FCF-9D55-7B8E7F157091",
    "locallow": "A520A1A4-1780-4FF6-BD18-167343C5AF16",
    "home": "5E6C858F-0E22-4760-9AFE-EA3317B67173",
}
KNOWN_FOLDER_FALLBACK = {
    "documents": r"%USERPROFILE%\Documents",
    "saved_games": r"%USERPROFILE%\Saved Games",
    "appdata": r"%APPDATA%",
    "localappdata": r"%LOCALAPPDATA%",
    "locallow": r"%USERPROFILE%\AppData\LocalLow",
    "home": r"%USERPROFILE%",
}
ACCOUNT_KEYS = {"steam_id3", "steam_id64"}
PLACEHOLDERS = set(KNOWN_FOLDERS) | {"steam"} | ACCOUNT_KEYS
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")

FileSig = tuple[int, int]  # (kích thước, mtime_ns)


class ConfigError(Exception):
    """config.toml viết sai."""


class SnapshotError(Exception):
    """Không backup / khôi phục được."""


# ----------------------------------------------------------------------------- cấu hình

@dataclasses.dataclass(frozen=True)
class GameConfig:
    id: str
    name: str
    save_dir: str
    steam_appids: tuple[int, ...] = ()
    processes: tuple[str, ...] = ()  # tên file .exe, chữ thường
    guard: bool = True
    exclude: tuple[str, ...] = ()

    @property
    def per_account(self) -> bool:
        """Thư mục save khác nhau theo từng account Steam."""
        return any(k.lower() in ACCOUNT_KEYS for k in PLACEHOLDER_RE.findall(self.save_dir))


@dataclasses.dataclass
class Config:
    vault_dir: Path
    games: list[GameConfig]
    interval_minutes: float = 15
    keep: int = 50
    keep_safety: int = 20
    notifications: bool = True
    poll_seconds: float = 2
    settle_seconds: float = 5
    quiet_seconds: float = 3


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "game"


def _number(data: dict, key: str, default, minimum):
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ConfigError(f"'{key}' phải là một số >= {minimum} (đang là {value!r}).")
    return value


def _str_list(raw: dict, key: str, game: str) -> list:
    value = raw.get(key, [])
    if isinstance(value, (str, int)):
        return [value]
    if not isinstance(value, list):
        raise ConfigError(f"{game}: '{key}' phải là một danh sách, ví dụ [\"a\", \"b\"].")
    return value


def load_config(path: Path) -> Config:
    try:
        data = tomllib.loads(Path(path).read_bytes().decode("utf-8-sig"))
    except FileNotFoundError:
        raise ConfigError(f"Không tìm thấy file cấu hình {path}") from None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"File {Path(path).name} viết sai cú pháp: {e}") from None

    games: list[GameConfig] = []
    seen: set[str] = set()
    for i, raw in enumerate(data.get("games", []), 1):
        if not isinstance(raw, dict):
            raise ConfigError(f"Mục [[games]] thứ {i} không hợp lệ.")
        if raw.get("enabled", True) is False:
            continue
        name = str(raw.get("name", "")).strip()
        save_dir = str(raw.get("save_dir", "")).strip()
        if not name or not save_dir:
            raise ConfigError(f"Mục [[games]] thứ {i} thiếu 'name' hoặc 'save_dir'.")
        gid = str(raw.get("id") or _slug(name)).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", gid):
            raise ConfigError(f"{name}: 'id' chỉ được dùng chữ không dấu, số, '.', '_' và '-'.")
        if gid.lower() in seen:
            raise ConfigError(f"Hai game trùng id '{gid}'.")
        seen.add(gid.lower())
        for key in PLACEHOLDER_RE.findall(save_dir):
            if key.lower() not in PLACEHOLDERS:
                raise ConfigError(f"{name}: không hiểu biến {{{key}}} trong save_dir.")
        try:
            appids = tuple(int(x) for x in _str_list(raw, "steam_appids", name))
        except (TypeError, ValueError):
            raise ConfigError(f"{name}: 'steam_appids' phải là danh sách số.") from None
        games.append(GameConfig(
            id=gid,
            name=name,
            save_dir=save_dir,
            steam_appids=appids,
            processes=tuple(str(p).strip().lower() for p in _str_list(raw, "processes", name)),
            guard=bool(raw.get("guard", True)),
            exclude=tuple(str(p) for p in _str_list(raw, "exclude", name)),
        ))

    vault = Path(os.path.expandvars(str(data.get("vault_dir", "") or "").strip() or "vault"))
    if not vault.is_absolute():
        vault = Path(path).resolve().parent / vault
    return Config(
        vault_dir=vault,
        games=games,
        interval_minutes=_number(data, "interval_minutes", 15, 1),
        keep=int(_number(data, "keep", 50, 1)),
        keep_safety=int(_number(data, "keep_safety", 20, 1)),
        notifications=bool(data.get("notifications", True)),
        poll_seconds=_number(data, "poll_seconds", 2, 0.5),
        settle_seconds=_number(data, "settle_seconds", 5, 0),
        quiet_seconds=_number(data, "quiet_seconds", 3, 0),
    )


@dataclasses.dataclass(frozen=True)
class SteamState:
    running: bool = False
    user: int = 0   # account ID (SteamID3) đang đăng nhập, 0 = chưa đăng nhập
    appid: int = 0  # AppID game Steam đang chạy, 0 = không có


def resolve_save_dir(template: str, env, user: int) -> Path | None:
    """Thay các biến {..} trong save_dir. Trả về None nếu cần account Steam mà chưa đăng nhập."""
    missing = False

    def sub(m: re.Match) -> str:
        nonlocal missing
        key = m.group(1).lower()
        if key in ACCOUNT_KEYS:
            if not user:
                missing = True
                return ""
            return str(user if key == "steam_id3" else user + STEAM_ID64_BASE)
        if key == "steam":
            steam = env.steam_path()
            if not steam:
                missing = True
                return ""
            return steam
        return env.known_folder(key)

    text = PLACEHOLDER_RE.sub(sub, template)
    return None if missing else Path(os.path.expandvars(text))


def is_running(game: GameConfig, st: SteamState, procs: set[str]) -> bool:
    return bool(st.appid and st.appid in game.steam_appids) or any(p in procs for p in game.processes)


# ----------------------------------------------------------------------------- file helpers

def scan_folder(root: Path, exclude: tuple[str, ...] = ()) -> dict[str, FileSig]:
    """Liệt kê file trong thư mục: đường dẫn tương đối (dấu /) -> (kích thước, mtime_ns)."""
    found: dict[str, FileSig] = {}
    stack = [Path(root)]
    while stack:
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except (FileNotFoundError, NotADirectoryError):
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                    continue
                if not e.is_file(follow_symlinks=False) or e.name.endswith(TMP_SUFFIX):
                    continue
                rel = Path(e.path).relative_to(root).as_posix()
                if any(fnmatch.fnmatch(rel, pat) for pat in exclude):
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            found[rel] = (st.st_size, st.st_mtime_ns)
    return found


def folder_quiet(root: Path, quiet_seconds: float) -> bool:
    """True nếu không có file nào vừa được ghi trong `quiet_seconds` giây (game không đang lưu dở)."""
    if quiet_seconds <= 0:
        return True
    files = scan_folder(root)
    if not files:
        return True
    newest = max(m for _, m in files.values()) / 1e9
    return time.time() - newest >= quiet_seconds


def _copy_hash(src: Path, dst: Path) -> str:
    h = hashlib.sha256()
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while chunk := fin.read(1024 * 1024):
            h.update(chunk)
            fout.write(chunk)
    return h.hexdigest()


def _make_writable(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass


def _rmtree(path: Path) -> None:
    def retry(func, p, _exc):
        _make_writable(p)
        try:
            func(p)
        except OSError:
            pass

    if not os.path.lexists(path):
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def _remove_tmp_files(root: Path) -> None:
    """Xoá file tạm sót lại nếu lần khôi phục trước bị ngắt giữa chừng."""
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(TMP_SUFFIX):
                try:
                    os.remove(os.path.join(dirpath, f))
                except OSError:
                    pass


# ----------------------------------------------------------------------------- kho save

@dataclasses.dataclass
class Snapshot:
    path: Path
    seq: int
    tag: str
    trusted: bool
    created_ts: float
    account: int
    files: dict[str, dict]  # rel -> {"size", "mtime_ns", "sha256"}

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def files_dir(self) -> Path:
        return self.path / "files"

    @property
    def created(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.created_ts)

    def sig(self) -> dict[str, FileSig]:
        return {rel: (m["size"], m["mtime_ns"]) for rel, m in self.files.items()}

    def differs_from(self, current: dict[str, FileSig]) -> list[str]:
        """File của bản này mà trong `current` bị thiếu hoặc khác. File thừa trong `current` không tính."""
        return [rel for rel, sig in self.sig().items() if current.get(rel) != sig]

    def label(self) -> str:
        return f"{self.created:%d/%m/%Y %H:%M:%S}  {TAG_LABELS.get(self.tag, self.tag)}  ({len(self.files)} file)"


@dataclasses.dataclass
class RestoreResult:
    restored: int
    extras: list[str]
    safety: Snapshot | None


class VaultLock:
    """Khoá kho save giữa các tiến trình (bản chạy nền và menu). Gọi lồng nhau trong cùng tiến trình được."""

    def __init__(self, path: Path, timeout: float = 120):
        self.path, self.timeout = path, timeout
        self.depth = 0
        self.fh = None

    def __enter__(self):
        if self.depth == 0 and IS_WINDOWS:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+b")
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        fh.close()
                        raise SnapshotError("Kho save đang bận (một tiến trình SaveKeeper khác đang dùng).")
                    time.sleep(0.2)
            self.fh = fh
        self.depth += 1
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        if self.depth == 0 and self.fh is not None:
            try:
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self.fh.close()
            self.fh = None


class Vault:
    """Kho save: <kho>/<id game>/<thời gian>_<loại>/files/... (mở bằng Explorer được).

    File không đổi giữa các bản được nối cứng (hardlink) thay vì chép lại,
    nên nhiều bản backup chỉ tốn dung lượng cho những file thực sự thay đổi.
    """

    def __init__(self, root: Path, keep: int = 50, keep_safety: int = 20, *,
                 clock=time.time, log: logging.Logger | None = None):
        self.root = Path(root)
        self.keep = keep
        self.keep_safety = keep_safety
        self.clock = clock
        self.log = log or logging.getLogger(APP_NAME)
        self.retry_delay = 0.5
        self._lock = VaultLock(self.root / ".lock")

    def game_dir(self, game_id: str) -> Path:
        return self.root / game_id

    def snapshots(self, game_id: str) -> list[Snapshot]:
        """Tất cả bản backup của game, cũ nhất trước."""
        gdir = self.game_dir(game_id)
        if not gdir.is_dir():
            return []
        found = []
        for d in gdir.iterdir():
            if d.name.startswith(".") or not d.is_dir():
                continue
            try:
                m = json.loads((d / MANIFEST).read_text(encoding="utf-8"))
                found.append(Snapshot(d, int(m.get("seq", 0)), str(m["tag"]), bool(m["trusted"]),
                                      float(m["created_ts"]), int(m.get("account", 0)), dict(m["files"])))
            except (OSError, ValueError, KeyError, TypeError):
                self.log.warning("Bỏ qua bản backup không đọc được: %s", d)
        found.sort(key=lambda s: (s.seq, s.created_ts, s.name))
        return found

    def latest(self, game_id: str) -> Snapshot | None:
        """Bản backup tin cậy mới nhất (không tính bản an toàn)."""
        for snap in reversed(self.snapshots(game_id)):
            if snap.trusted:
                return snap
        return None

    def cleanup_tmp(self) -> None:
        if not self.root.is_dir():
            return
        for gdir in self.root.iterdir():
            if gdir.is_dir():
                for d in gdir.glob(".tmp-*"):
                    _rmtree(d)

    # ---- backup

    def create_snapshot(self, game: GameConfig, src: Path, tag: str, account: int = 0,
                        *, force: bool = False) -> tuple[str, Snapshot | None]:
        """Chụp thư mục save. Trả về ('created', bản mới) | ('unchanged', bản mới nhất) | ('empty', None)."""
        trusted = tag not in SAFETY_TAGS
        with self._lock:
            current = scan_folder(src, game.exclude)
            if not current:
                return "empty", None
            snaps = self.snapshots(game.id)
            base = next((s for s in reversed(snaps) if s.trusted), None)
            if trusted and not force and base is not None and base.sig() == current:
                return "unchanged", base

            # File nào giống hệt một bản cũ thì nối cứng tới bản đó (bản mới hơn được ưu tiên).
            reuse: dict[str, tuple[Path, str]] = {}
            for s in snaps:
                for rel, m in s.files.items():
                    if current.get(rel) == (m["size"], m["mtime_ns"]):
                        reuse[rel] = (s.files_dir / rel, m.get("sha256", ""))

            gdir = self.game_dir(game.id)
            gdir.mkdir(parents=True, exist_ok=True)
            tmp = gdir / f".tmp-{uuid.uuid4().hex[:10]}"
            try:
                files: dict[str, dict] = {}
                for rel in sorted(current):
                    dst = tmp / "files" / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    meta = None
                    if rel in reuse:
                        try:
                            os.link(reuse[rel][0], dst)
                            size, mtime = current[rel]
                            meta = {"size": size, "mtime_ns": mtime, "sha256": reuse[rel][1]}
                        except OSError:
                            meta = None
                    if meta is None:
                        meta = self._copy_stable(src / rel, dst)
                    if meta is not None:
                        files[rel] = meta
                if not files:
                    _rmtree(tmp)
                    return "empty", None
                ts = self.clock()
                seq = (snaps[-1].seq + 1) if snaps else 1
                manifest = {"seq": seq, "tag": tag, "trusted": trusted, "created_ts": ts,
                            "created": dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds"),
                            "account": account, "game": game.name, "source": str(src), "files": files}
                (tmp / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
                final = self._final_dir(gdir, ts, tag)
                os.rename(tmp, final)
            except BaseException:
                _rmtree(tmp)
                raise
            snap = Snapshot(final, seq, tag, trusted, ts, account, files)
            self.rotate(game.id)
            return "created", snap

    def _copy_stable(self, src: Path, dst: Path, attempts: int = 5) -> dict | None:
        """Chép một file; nếu game ghi vào file đúng lúc đang chép thì chép lại. None = file đã bị xoá."""
        locked: OSError | None = None
        for i in range(attempts):
            try:
                before = os.stat(src)
                sha = _copy_hash(src, dst)
                after = os.stat(src)
            except FileNotFoundError:
                return None
            except PermissionError as e:  # game đang khoá file
                locked = e
                time.sleep(self.retry_delay * (i + 1))
                continue
            locked = None
            if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) \
                    and os.path.getsize(dst) == after.st_size:
                os.utime(dst, ns=(after.st_mtime_ns, after.st_mtime_ns))
                return {"size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": sha}
            time.sleep(self.retry_delay)
        if locked is not None:
            raise SnapshotError(f"Không đọc được {src}: {locked}")
        raise SnapshotError(f"File {src} thay đổi liên tục, sẽ thử lại sau.")

    @staticmethod
    def _final_dir(gdir: Path, ts: float, tag: str) -> Path:
        base = f"{dt.datetime.fromtimestamp(ts):%Y-%m-%d_%H-%M-%S}_{tag}"
        candidate, n = gdir / base, 2
        while candidate.exists():
            candidate = gdir / f"{base}_{n}"
            n += 1
        return candidate

    def rotate(self, game_id: str) -> None:
        snaps = self.snapshots(game_id)
        for group, keep in (([s for s in snaps if s.trusted], self.keep),
                            ([s for s in snaps if not s.trusted], self.keep_safety)):
            for old in group[:-keep] if len(group) > keep else []:
                _rmtree(old.path)
                self.log.info("Xoá bản backup cũ %s/%s", game_id, old.name)

    # ---- khôi phục

    def restore(self, game: GameConfig, snap: Snapshot, dest: Path, account: int = 0) -> RestoreResult:
        """Chép file của `snap` vào `dest`. Chỉ ghi đè / thêm, không xoá file nào.

        Trước khi ghi đè, trạng thái hiện tại của `dest` được giữ lại thành một bản an toàn.
        """
        with self._lock:
            current = scan_folder(dest, game.exclude)
            todo = snap.differs_from(current)
            extras = sorted(set(current) - set(snap.files))
            safety = None
            if todo and current:
                _status, safety = self.create_snapshot(game, dest, "before-restore", account, force=True)
            sig = snap.sig()
            for rel in todo:
                src = snap.files_dir / rel
                dst = Path(dest) / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.name + TMP_SUFFIX)
                shutil.copyfile(src, tmp)
                os.utime(tmp, ns=(sig[rel][1], sig[rel][1]))
                if dst.exists():
                    _make_writable(dst)
                os.replace(tmp, dst)
            bad = snap.differs_from(scan_folder(dest, game.exclude))
            if bad:
                raise SnapshotError(f"Khôi phục chưa trọn vẹn: {len(bad)} file lỗi (ví dụ {bad[0]}).")
            return RestoreResult(len(todo), extras, safety)


# ----------------------------------------------------------------------------- theo dõi game

@dataclasses.dataclass
class GameState:
    running: bool = False
    session_path: Path | None = None
    session_account: int = 0
    next_periodic: float = 0.0
    deferrals: int = 0
    pending_exit: float | None = None  # thời điểm sẽ backup sau khi thoát game
    exit_deadline: float = 0.0
    dirty: bool = False                # tiến trình của lần chơi trước chưa được backup xong
    failures: int = 0


class Watcher:
    """Theo dõi Steam và game, quyết định khi nào backup, khi nào khôi phục.

    Nguyên tắc:
      * Thay đổi xảy ra lúc game đang chạy là tiến trình của bạn -> backup (15 phút/lần + khi thoát).
      * Lúc mở game mà save khác bản backup mới nhất (Steam Cloud kéo save khác về, đổi account...)
        -> giữ bản hiện tại thành bản an toàn rồi khôi phục bản của bạn.
      * Không bao giờ xoá save: chỉ ghi đè/thêm, và luôn giữ lại bản bị ghi đè.
    """

    def __init__(self, cfg: Config, env, vault: Vault, log: logging.Logger | None = None):
        self.cfg, self.env, self.vault = cfg, env, vault
        self.log = log or logging.getLogger(APP_NAME)
        self.states = {g.id: GameState() for g in cfg.games}
        self.user = 0
        self.interval = cfg.interval_minutes * 60

    def notify(self, title: str, body: str) -> None:
        self.log.info("Thông báo: %s | %s", title, body)
        if self.cfg.notifications:
            try:
                self.env.notify(title, body)
            except Exception:
                self.log.exception("Không hiện được thông báo")

    def _backup(self, g: GameConfig, path: Path, tag: str, account: int) -> bool:
        try:
            status, snap = self.vault.create_snapshot(g, path, tag, account)
        except Exception:
            self.log.exception("%s: backup (%s) thất bại", g.name, TAG_LABELS.get(tag, tag))
            return False
        if status == "created":
            self.log.info("%s: backup %s -> %s (%d file)", g.name, TAG_LABELS.get(tag, tag), snap.name, len(snap.files))
        return True

    # ---- vòng đời

    def startup(self) -> None:
        self.vault.cleanup_tmp()
        procs = self.env.process_names()
        st = self.env.steam_state(procs)
        self.user = st.user
        now = self.env.now()
        for g in self.cfg.games:
            try:
                self._startup_game(g, st, procs, now)
            except Exception:
                self.log.exception("%s: lỗi lúc khởi động", g.name)

    def _startup_game(self, g: GameConfig, st: SteamState, procs: set[str], now: float) -> None:
        s = self.states[g.id]
        path = resolve_save_dir(g.save_dir, self.env, st.user)
        if path is not None:
            _remove_tmp_files(path)
        if is_running(g, st, procs):
            s.running, s.session_path, s.session_account = True, path, st.user
            s.next_periodic = now + self.interval
            self.log.info("%s đang chạy sẵn lúc SaveKeeper khởi động.", g.name)
        base = self.vault.latest(g.id)
        if g.per_account and not s.running and base and base.account and st.user and base.account != st.user:
            # Đã đổi account lúc SaveKeeper không chạy -> mang tiến trình sang account hiện tại.
            self.sync_to_account(g, st.user)
        elif path is not None and path.is_dir():
            self._backup(g, path, "startup", st.user)

    def tick(self) -> None:
        now = self.env.now()
        procs = self.env.process_names()
        st = self.env.steam_state(procs)
        account_changed = st.user != self.user
        for g in self.cfg.games:
            try:
                self._tick_game(g, st, procs, now, account_changed)
            except Exception:
                self.log.exception("%s: lỗi khi xử lý", g.name)
        if account_changed:
            self.log.info("Đổi account Steam: %s -> %s", self.user or "(không có)", st.user or "(đăng xuất)")
            self.user = st.user
            if st.user:
                for g in self.cfg.games:
                    if g.per_account and not self.states[g.id].running:
                        try:
                            self.sync_to_account(g, st.user)
                        except Exception:
                            self.log.exception("%s: không chuyển được save sang account mới", g.name)
                            self.notify(f"{g.name}: lỗi chuyển save", "Không chép được save sang account mới. Xem savekeeper.log.")

    def shutdown(self) -> None:
        now = self.env.now()
        for g in self.cfg.games:
            if self.states[g.id].pending_exit is not None:
                self.finish_session(g, now, force=True)

    def _tick_game(self, g: GameConfig, st: SteamState, procs: set[str], now: float, account_changed: bool) -> None:
        s = self.states[g.id]
        running = is_running(g, st, procs)
        if running and not s.running:
            self.on_game_start(g, st, now)
        elif running:
            if now >= s.next_periodic:
                self.periodic(g, now)
        elif s.running:
            self.on_game_stop(g, now)
        if not running and s.pending_exit is not None and (now >= s.pending_exit or account_changed):
            # Đổi account thì backup ngay, trước khi save của account mới được chép vào.
            self.finish_session(g, now, force=account_changed)

    def on_game_start(self, g: GameConfig, st: SteamState, now: float) -> None:
        s = self.states[g.id]
        if s.pending_exit is not None:
            # Vừa thoát đã mở lại: backup lần chơi trước trước khi so sánh, kẻo lùi mất tiến trình.
            self.finish_session(g, now, force=True)
        s.running = True
        s.session_path = resolve_save_dir(g.save_dir, self.env, st.user)
        s.session_account = st.user
        s.next_periodic = now + self.interval
        s.deferrals = 0
        self.log.info("%s bắt đầu chạy (account %s).", g.name, st.user or "?")
        if s.session_path is not None:
            self.guard(g, s.session_path, st.user)

    def periodic(self, g: GameConfig, now: float) -> None:
        s = self.states[g.id]
        path = s.session_path
        if path is None or not path.is_dir():
            s.next_periodic = now + self.interval
            return
        if not folder_quiet(path, self.cfg.quiet_seconds) and s.deferrals < 12:
            s.deferrals += 1  # game đang ghi save, đợi thêm vài giây
            s.next_periodic = now + 5
            return
        s.deferrals = 0
        ok = self._backup(g, path, "periodic", s.session_account)
        s.next_periodic = now + (self.interval if ok else 60)

    def on_game_stop(self, g: GameConfig, now: float) -> None:
        s = self.states[g.id]
        s.running = False
        s.dirty = True
        s.pending_exit = now + self.cfg.settle_seconds
        s.exit_deadline = now + 120
        self.log.info("%s đã tắt.", g.name)

    def finish_session(self, g: GameConfig, now: float, *, force: bool = False) -> None:
        """Backup lần cuối sau khi thoát game."""
        s = self.states[g.id]
        path = s.session_path
        if path is None:
            s.pending_exit, s.dirty = None, False
            return
        if not force and now < s.exit_deadline and not folder_quiet(path, self.cfg.quiet_seconds):
            s.pending_exit = now + 3
            return
        if self._backup(g, path, "exit", s.session_account):
            s.pending_exit, s.dirty, s.failures = None, False, 0
            return
        s.failures += 1
        s.pending_exit = now + 30
        if s.failures == 1:
            self.notify(f"{g.name}: backup lỗi",
                        "Không backup được save sau khi thoát game, SaveKeeper sẽ thử lại. Xem savekeeper.log.")

    # ---- bảo vệ save

    def guard(self, g: GameConfig, path: Path, user: int) -> None:
        """Lúc mở game: nếu save khác bản backup mới nhất thì khôi phục bản của bạn."""
        s = self.states[g.id]
        if s.dirty:
            self.log.warning("%s: lần chơi trước chưa backup được nên không kiểm tra save.", g.name)
            return
        base = self.vault.latest(g.id)
        if base is None:
            return
        changed = base.differs_from(scan_folder(path, g.exclude))
        if not changed:
            return
        when = f"{base.created:%H:%M %d/%m}"
        if not g.guard:
            self.notify(f"{g.name}: save đã bị thay đổi",
                        f"{len(changed)} file khác bản backup lúc {when}. guard = false nên SaveKeeper không khôi phục.")
            return
        try:
            res = self.vault.restore(g, base, path, user)
        except Exception:
            self.log.exception("%s: khôi phục thất bại", g.name)
            self.notify(f"{g.name}: khôi phục lỗi", "Save bị thay đổi nhưng không khôi phục được. Xem savekeeper.log.")
            return
        kept = f" Bản bị thay thế được giữ trong kho: {res.safety.name}." if res.safety else ""
        self.notify(f"{g.name}: đã giữ lại save của bạn",
                    f"Save bị thay đổi lúc bạn không chơi (thường do Steam Cloud). "
                    f"Đã khôi phục {res.restored} file về bản lúc {when}.{kept}")

    def sync_to_account(self, g: GameConfig, user: int) -> None:
        """Game lưu save theo account: chép tiến trình mới nhất sang thư mục của account `user`."""
        s = self.states[g.id]
        if s.dirty:
            self.log.warning("%s: chưa chép save sang account %s vì lần chơi trước chưa backup xong.", g.name, user)
            return
        base = self.vault.latest(g.id)
        dest = resolve_save_dir(g.save_dir, self.env, user)
        if base is None or dest is None:
            return
        if not base.differs_from(scan_folder(dest, g.exclude)):
            return
        res = self.vault.restore(g, base, dest, user)
        self.notify(f"{g.name}: đã chuyển save sang account mới",
                    f"Đã chép {res.restored} file (bản lúc {base.created:%H:%M %d/%m}) sang account {user}.")


# ----------------------------------------------------------------------------- Windows

if IS_WINDOWS:
    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32FirstW.restype = wintypes.BOOL
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32NextW.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.CreateMutexW.restype = wintypes.HANDLE
    _k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.OpenMutexW.restype = wintypes.HANDLE
    _shell32 = ctypes.WinDLL("shell32")
    _shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE,
                                              ctypes.POINTER(ctypes.c_wchar_p)]
    _shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    _ole32 = ctypes.WinDLL("ole32")
    _ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    _ole32.CoTaskMemFree.restype = None
    _INVALID_HANDLE = ctypes.c_void_p(-1).value


def _known_folder_path(key: str) -> str:
    if IS_WINDOWS:
        guid = _GUID.from_buffer_copy(uuid.UUID(KNOWN_FOLDERS[key]).bytes_le)
        out = ctypes.c_wchar_p()
        hr = _shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
        try:
            if hr == 0 and out.value:
                return out.value
        finally:
            if out:
                _ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))
    return os.path.expandvars(KNOWN_FOLDER_FALLBACK[key])


def _process_names() -> set[str]:
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        names: set[str] = set()
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            names.add(entry.szExeFile.lower())
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
        return names
    finally:
        _k32.CloseHandle(snap)


def _process_names_tasklist() -> set[str]:
    out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                         creationflags=CREATE_NO_WINDOW).stdout
    return {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line.startswith('"')}


def _reg_value(subkey: str, name: str):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _show_toast(title: str, body: str) -> None:
    """Thông báo Windows (khi đang chơi game toàn màn hình, Windows tự đưa vào Notification Center)."""
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("'", "&apos;"))

    xml = ('<toast><visual><binding template="ToastGeneric">'
           f"<text>{esc(title)}</text><text>{esc(body)}</text>"
           "</binding></visual></toast>")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml('{xml}');"
        "$t = New-Object Windows.UI.Notifications.ToastNotification $x;"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{TOAST_APP_ID}').Show($t)"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                      "-EncodedCommand", encoded],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=CREATE_NO_WINDOW)


class WindowsEnv:
    """Trạng thái thật của Windows và Steam (đọc registry, không can thiệp vào Steam)."""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def process_names(self) -> set[str]:
        try:
            return _process_names()
        except OSError:
            return _process_names_tasklist()

    def steam_state(self, procs: set[str]) -> SteamState:
        if "steam.exe" not in procs:  # Steam tắt thì giá trị trong registry có thể đã cũ
            return SteamState()
        user = _reg_value(r"Software\Valve\Steam\ActiveProcess", "ActiveUser")
        appid = _reg_value(r"Software\Valve\Steam", "RunningAppID")
        return SteamState(True, user if isinstance(user, int) else 0, appid if isinstance(appid, int) else 0)

    def steam_path(self) -> str | None:
        p = _reg_value(r"Software\Valve\Steam", "SteamPath")
        return os.path.normpath(p) if isinstance(p, str) and p else None

    def known_folder(self, key: str) -> str:
        return _known_folder_path(key)

    def notify(self, title: str, body: str) -> None:
        _show_toast(title, body)


def acquire_single_instance():
    """Giữ mutex suốt đời tiến trình chạy nền. None = đã có một bản đang chạy."""
    ctypes.set_last_error(0)
    handle = _k32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return None
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        _k32.CloseHandle(handle)
        return None
    return handle


def watcher_is_running() -> bool:
    handle = _k32.OpenMutexW(0x00100000, False, MUTEX_NAME)  # SYNCHRONIZE
    if handle:
        _k32.CloseHandle(handle)
        return True
    return False


def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" run'
    exe = Path(sys.executable)
    if exe.with_name("pythonw.exe").exists():  # chạy ẩn, không hiện cửa sổ đen
        exe = exe.with_name("pythonw.exe")
    return f'"{exe}" "{Path(__file__).resolve()}" run'


def autostart_installed() -> bool:
    return _reg_value(RUN_KEY, APP_NAME) is not None


def start_watcher() -> None:
    subprocess.Popen(autostart_command(), cwd=APP_DIR, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)


def stop_watcher(timeout: float = 20) -> bool:
    if not watcher_is_running():
        return True
    STOP_FLAG.touch()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not watcher_is_running():
            return True
        time.sleep(0.5)
    return False


# ----------------------------------------------------------------------------- chạy nền

def run_watcher(cfg: Config, env, log: logging.Logger) -> int:
    handle = acquire_single_instance()
    if handle is None:
        log.info("SaveKeeper đã đang chạy, bỏ qua lần mở thêm.")
        return 0
    try:
        STOP_FLAG.unlink()
    except FileNotFoundError:
        pass
    vault = Vault(cfg.vault_dir, cfg.keep, cfg.keep_safety, clock=env.now, log=log)
    watcher = Watcher(cfg, env, vault, log)
    log.info("SaveKeeper bắt đầu chạy. Kho save: %s. Theo dõi %d game.", cfg.vault_dir, len(cfg.games))
    watcher.startup()
    try:
        while not STOP_FLAG.exists():
            try:
                watcher.tick()
            except Exception:
                log.exception("Lỗi không mong muốn")
            env.sleep(cfg.poll_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        watcher.shutdown()
        try:
            STOP_FLAG.unlink()
        except OSError:
            pass
        log.info("SaveKeeper đã dừng.")
    return 0


# ----------------------------------------------------------------------------- menu / lệnh

def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _pick(items: list, render, title: str):
    if not items:
        return None
    print(title)
    for i, item in enumerate(items, 1):
        print(f"  {i:>2}. {render(item)}")
    answer = _ask("Chọn số (Enter để huỷ): ")
    if answer.isdigit() and 1 <= int(answer) <= len(items):
        return items[int(answer) - 1]
    return None


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _vault_size(root: Path) -> int:
    """Dung lượng thật của kho (file nối cứng chỉ tính một lần)."""
    seen, total = set(), 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            try:
                st = os.stat(os.path.join(dirpath, f))
            except OSError:
                continue
            if st.st_ino not in seen:
                seen.add(st.st_ino)
                total += st.st_size
    return total


class App:
    """Các lệnh dùng tay: xem trạng thái, backup ngay, khôi phục."""

    def __init__(self, cfg: Config, env, log: logging.Logger):
        self.cfg, self.env, self.log = cfg, env, log
        self.vault = Vault(cfg.vault_dir, cfg.keep, cfg.keep_safety, clock=env.now, log=log)

    def _state(self) -> tuple[SteamState, set[str]]:
        procs = self.env.process_names()
        return self.env.steam_state(procs), procs

    def find_game(self, key: str | None) -> GameConfig | None:
        if key:
            for g in self.cfg.games:
                if key.lower() in (g.id.lower(), g.name.lower()):
                    return g
            print(f"Không có game '{key}' trong config.toml.")
            return None
        return _pick(self.cfg.games, lambda g: g.name, "Chọn game:")

    def status(self) -> int:
        st, procs = self._state()
        print(f"SaveKeeper chạy nền : {'ĐANG CHẠY' if watcher_is_running() else 'không chạy'}")
        print(f"Tự chạy cùng Windows: {'bật' if autostart_installed() else 'tắt'}")
        print(f"Kho save            : {self.cfg.vault_dir}  ({_human_size(_vault_size(self.cfg.vault_dir))})")
        if st.running:
            playing = f", đang mở AppID {st.appid}" if st.appid else ""
            print(f"Steam               : đang chạy, account ID {st.user or '(chưa đăng nhập)'}{playing}")
        else:
            print("Steam               : không chạy")
        for g in self.cfg.games:
            path = resolve_save_dir(g.save_dir, self.env, st.user)
            snaps = self.vault.snapshots(g.id)
            trusted = [s for s in snaps if s.trusted]
            print(f"\n* {g.name}" + ("   [ĐANG CHƠI]" if is_running(g, st, procs) else ""))
            if path is None:
                print("    Thư mục save: (cần mở Steam và đăng nhập)")
            else:
                print(f"    Thư mục save: {path}" + ("" if path.is_dir() else "   (chưa có: game chưa cài hoặc chưa lưu lần nào)"))
            if trusted:
                print(f"    Bản mới nhất: {trusted[-1].label()}")
                print(f"    Số bản      : {len(trusted)} bản thường, {len(snaps) - len(trusted)} bản an toàn")
            else:
                print("    Chưa có bản backup nào.")
        return 0

    def backup(self, game_key: str | None = None) -> int:
        games = self.cfg.games if not game_key else [g for g in [self.find_game(game_key)] if g]
        if not games:
            return 1
        st, _procs = self._state()
        for g in games:
            path = resolve_save_dir(g.save_dir, self.env, st.user)
            if path is None:
                print(f"- {g.name}: cần mở Steam và đăng nhập để biết thư mục save.")
                continue
            status, snap = self.vault.create_snapshot(g, path, "manual", st.user)
            if status == "created":
                print(f"- {g.name}: đã backup {len(snap.files)} file -> {snap.name}")
            elif status == "unchanged":
                print(f"- {g.name}: save không đổi so với bản {snap.label()}")
            else:
                print(f"- {g.name}: chưa có save.")
        return 0

    def list_snapshots(self, game_key: str | None = None) -> int:
        g = self.find_game(game_key)
        if g is None:
            return 1
        snaps = self.vault.snapshots(g.id)
        if not snaps:
            print(f"{g.name}: chưa có bản backup nào.")
            return 0
        print(f"Các bản backup của {g.name} (mới nhất ở trên), nằm trong {self.vault.game_dir(g.id)}:")
        for s in reversed(snaps):
            print(f"  {s.name:<42} {s.label()}")
        return 0

    def restore(self, game_key: str | None = None, snap_name: str | None = None, yes: bool = False) -> int:
        g = self.find_game(game_key)
        if g is None:
            return 1
        snaps = list(reversed(self.vault.snapshots(g.id)))
        if not snaps:
            print(f"{g.name}: chưa có bản backup nào.")
            return 1
        if not snap_name:
            snap = _pick(snaps[:30], Snapshot.label, f"Các bản backup của {g.name} (mới nhất ở trên):")
        elif snap_name == "latest":
            snap = next((s for s in snaps if s.trusted), None)
        else:
            snap = next((s for s in snaps if s.name == snap_name), None)
        if snap is None:
            print("Đã huỷ hoặc không tìm thấy bản backup.")
            return 1
        st, procs = self._state()
        if is_running(g, st, procs):
            print(f"{g.name} đang chạy. Hãy thoát game rồi khôi phục.")
            return 1
        dest = resolve_save_dir(g.save_dir, self.env, st.user)
        if dest is None:
            print("Game này lưu save theo account: hãy mở Steam, đăng nhập account muốn chơi rồi chạy lại.")
            return 1
        print(f"\nSẽ chép bản [{snap.label()}] vào:\n  {dest}")
        print("Save hiện tại sẽ được giữ lại một bản trong kho trước khi bị ghi đè.")
        if not yes and _ask("Tiếp tục? (c/k): ").lower() not in ("c", "co", "có", "y", "yes"):
            print("Đã huỷ.")
            return 1
        res = self.vault.restore(g, snap, dest, st.user)
        self.vault.create_snapshot(g, dest, "restore", st.user)  # để bản vừa khôi phục thành bản chuẩn
        print(f"Xong: đã chép {res.restored} file.")
        if res.safety:
            print(f"Save cũ được giữ ở: {res.safety.path}")
        if res.extras:
            print(f"Lưu ý: thư mục save còn {len(res.extras)} file không thuộc bản này (SaveKeeper không xoá).")
        return 0

    def menu(self) -> int:
        actions = [
            ("Xem trạng thái", self.status),
            ("Backup ngay tất cả game (cũng dùng khi bạn vừa tự chép save vào)", self.backup),
            ("Khôi phục save từ kho", self.restore),
            ("Bật SaveKeeper chạy nền + tự chạy cùng Windows (chọn lại sau khi sửa config)", install),
            ("Tắt SaveKeeper và bỏ tự chạy cùng Windows", uninstall),
        ]
        while True:
            print("\n================ SaveKeeper ================")
            for i, (label, _fn) in enumerate(actions, 1):
                print(f"  {i}. {label}")
            print("  0. Thoát")
            try:
                answer = input("Chọn: ").strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if answer == "0":
                return 0
            if answer.isdigit() and 1 <= int(answer) <= len(actions):
                print()
                try:
                    actions[int(answer) - 1][1]()
                except Exception as e:  # menu không được chết vì một lỗi
                    self.log.exception("Lỗi trong menu")
                    print(f"LỖI: {e}")


def install() -> int:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
    if watcher_is_running():
        print("Đang khởi động lại SaveKeeper để nhận cấu hình mới...")
        if not stop_watcher():
            print("Bản cũ chưa tắt được (có thể đang backup). Đợi một lát rồi chọn lại.")
            return 1
    start_watcher()
    for _ in range(20):
        time.sleep(0.5)
        if watcher_is_running():
            print("SaveKeeper đang chạy nền và sẽ tự chạy mỗi khi bật máy.")
            return 0
    print(f"Không khởi động được SaveKeeper. Xem {LOG_PATH}")
    return 1


def uninstall() -> int:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
    except FileNotFoundError:
        pass
    if stop_watcher():
        print("Đã tắt SaveKeeper và bỏ tự chạy cùng Windows. Kho save vẫn giữ nguyên.")
    else:
        print("Đã bỏ tự chạy. SaveKeeper đang bận backup, sẽ tự tắt sau ít giây.")
    return 0


def setup_logging(rotate: bool) -> logging.Logger:
    log = logging.getLogger(APP_NAME)
    log.setLevel(logging.INFO)
    log.propagate = False
    for h in list(log.handlers):
        log.removeHandler(h)
    try:
        if rotate:
            handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        else:
            handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    return log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="savekeeper",
                                     description="Tự backup save game và giữ tiến trình khi đổi account Steam.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="đường dẫn config.toml")
    sub = parser.add_subparsers(dest="command", metavar="LỆNH")
    sub.add_parser("menu", help="mở menu (mặc định)")
    sub.add_parser("run", help="chạy nền: theo dõi Steam và backup tự động")
    sub.add_parser("status", help="xem trạng thái")
    p = sub.add_parser("backup", help="backup ngay")
    p.add_argument("game", nargs="?", help="id hoặc tên game (bỏ trống = tất cả)")
    p = sub.add_parser("list", help="liệt kê các bản backup của một game")
    p.add_argument("game", nargs="?")
    p = sub.add_parser("restore", help="khôi phục save từ kho")
    p.add_argument("game", nargs="?")
    p.add_argument("snapshot", nargs="?", help="tên bản backup, hoặc 'latest'")
    p.add_argument("-y", "--yes", action="store_true", help="không hỏi xác nhận")
    sub.add_parser("install", help="chạy nền ngay và tự chạy cùng Windows")
    sub.add_parser("uninstall", help="tắt và bỏ tự chạy cùng Windows")
    sub.add_parser("test-notify", help="hiện thử một thông báo")
    args = parser.parse_args(argv)
    command = args.command or "menu"

    log = setup_logging(rotate=command == "run")
    env = WindowsEnv()
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        log.error("%s", e)
        if command == "run":
            env.notify("SaveKeeper: lỗi cấu hình", str(e))
        else:
            print(f"LỖI cấu hình: {e}")
        return 2

    if command == "run":
        return run_watcher(cfg, env, log)
    if command == "install":
        return install()
    if command == "uninstall":
        return uninstall()
    if command == "test-notify":
        env.notify("SaveKeeper", "Thông báo thử: SaveKeeper hiện thông báo được trên máy này.")
        return 0
    app = App(cfg, env, log)
    try:
        if command == "status":
            return app.status()
        if command == "backup":
            return app.backup(args.game)
        if command == "list":
            return app.list_snapshots(args.game)
        if command == "restore":
            return app.restore(args.game, args.snapshot, args.yes)
        return app.menu()
    except SnapshotError as e:
        print(f"LỖI: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
