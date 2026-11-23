"""Kho save dùng chung giữa nhiều máy: một thư mục (Google Drive, NAS...) hoặc SaveKeeper server.

Cấu trúc giống nhau cho cả hai:
    objects/<2 ký tự đầu>/<sha256>           nội dung file save (nội dung giống nhau chỉ lưu một lần)
    games/<id game>/snapshots/<id bản>.json  danh sách file của một bản backup
    games/<id game>/head.json                bản mới nhất mà mọi máy nên dùng

Chỉ dùng thư viện chuẩn của Python để server chạy được ở bất cứ đâu.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SHA_RE = re.compile(r"[0-9a-f]{64}")
NAME_RE = re.compile(r"(?!\.+$)[A-Za-z0-9._-]{1,100}")
CHUNK = 1024 * 1024
MAX_OBJECT = 4 * 1024 ** 3  # 4 GB mỗi file
MAX_JSON = 64 * 1024 ** 2   # 64 MB mỗi file danh sách


class StoreError(Exception):
    """Lỗi kho đồng bộ: mạng, server, dữ liệu hỏng..."""


class AuthError(StoreError):
    """Server từ chối token."""


def check_sha(sha) -> str:
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise StoreError(f"Mã file không hợp lệ: {sha!r}")
    return sha


def check_name(name) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise StoreError(f"Tên không hợp lệ: {name!r}")
    return name


def check_snapshot_doc(doc) -> dict:
    if not isinstance(doc, dict) or not isinstance(doc.get("files"), dict):
        raise StoreError("Danh sách file không hợp lệ.")
    for meta in doc["files"].values():
        if not isinstance(meta, dict):
            raise StoreError("Danh sách file không hợp lệ.")
        check_sha(meta.get("sha256"))
    return doc


def content_id(files: dict) -> str:
    """Mã nội dung của một bản backup: chỉ phụ thuộc đường dẫn và nội dung các file."""
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(f"{rel}\0{files[rel]['sha256']}\n".encode("utf-8"))
    return h.hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


class DirStore:
    """Kho nằm trong một thư mục. Server cũng dùng lớp này để lưu dữ liệu."""

    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.Lock()

    def __str__(self) -> str:
        return str(self.root)

    def refresh(self) -> None:
        """Lấy trạng thái mới nhất của kho (chỉ kho git cần)."""

    def flush(self, message: str = "") -> bool:
        """Đẩy các thay đổi lên (chỉ kho git cần). False = máy khác vừa đẩy trước, đã bỏ thay đổi."""
        return True

    # ---- nội dung file

    def object_path(self, sha: str) -> Path:
        check_sha(sha)
        return self.root / "objects" / sha[:2] / sha

    def missing(self, shas) -> list[str]:
        return sorted({s for s in shas if not self.object_path(s).is_file()})

    def put_object(self, sha: str, src) -> None:
        with open(src, "rb") as f:
            self.put_object_stream(sha, f, os.path.getsize(src))

    def put_object_stream(self, sha: str, reader, length: int) -> None:
        """Ghi `length` byte đọc từ `reader`, kiểm tra đúng sha256 rồi mới lưu."""
        dst = self.object_path(sha)
        if not 0 <= length <= MAX_OBJECT:
            raise StoreError("File quá lớn.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{sha}.{os.getpid()}.{threading.get_ident()}.tmp")
        h = hashlib.sha256()
        remaining = length
        try:
            with open(tmp, "wb") as out:
                while remaining > 0:
                    chunk = reader.read(min(CHUNK, remaining))
                    if not chunk:
                        raise StoreError("Dữ liệu gửi lên bị thiếu.")
                    h.update(chunk)
                    out.write(chunk)
                    remaining -= len(chunk)
            if h.hexdigest() != sha:
                raise StoreError("Nội dung file không khớp mã sha256.")
            os.replace(tmp, dst)
        finally:
            if tmp.exists():
                tmp.unlink()

    def get_object(self, sha: str, dest) -> None:
        src = self.object_path(sha)
        if not src.is_file():
            raise StoreError(f"Kho thiếu file {sha[:12]}.")
        shutil.copyfile(src, dest)

    # ---- danh sách bản backup

    @staticmethod
    def _read_json(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError:
            raise StoreError(f"File hỏng: {path}") from None

    @staticmethod
    def _write_json(path: Path, doc) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def _game(self, gid: str) -> Path:
        return self.root / "games" / check_name(gid)

    def list_games(self) -> list[str]:
        d = self.root / "games"
        if not d.is_dir():
            return []
        return sorted(p.name for p in d.iterdir() if p.is_dir() and NAME_RE.fullmatch(p.name))

    def get_head(self, gid: str):
        return self._read_json(self._game(gid) / "head.json")

    def put_head(self, gid: str, doc: dict, expected) -> bool:
        """Đổi bản mới nhất, chỉ khi bản mới nhất hiện tại vẫn là `expected` (None = chưa có)."""
        if not isinstance(doc, dict):
            raise StoreError("head không hợp lệ.")
        check_name(doc.get("id"))
        with self._lock:
            current = self.get_head(gid)
            if (current or {}).get("id") != expected:
                return False
            self._write_json(self._game(gid) / "head.json", doc)
            return True

    def list_snapshots(self, gid: str) -> list[str]:
        """Id các bản backup, cũ nhất trước (id bắt đầu bằng thời gian)."""
        d = self._game(gid) / "snapshots"
        if not d.is_dir():
            return []
        return sorted(p.name[:-5] for p in d.glob("*.json") if NAME_RE.fullmatch(p.name[:-5]))

    def get_snapshot(self, gid: str, sid: str):
        return self._read_json(self._game(gid) / "snapshots" / f"{check_name(sid)}.json")

    def put_snapshot(self, gid: str, sid: str, doc: dict) -> None:
        check_snapshot_doc(doc)
        self._write_json(self._game(gid) / "snapshots" / f"{check_name(sid)}.json", doc)

    # ---- dọn dẹp

    def gc(self, keep: int = 200, grace_seconds: float = 2 * 86400) -> dict:
        """Mỗi game giữ `keep` bản mới nhất (và bản head). Xoá nội dung không còn bản nào dùng.

        Nội dung mới tải lên trong `grace_seconds` gần đây không bị xoá, để không đụng vào
        một lần đẩy lên đang dở dang.
        """
        removed_snaps = removed_objects = 0
        referenced: set[str] = set()
        for gid in self.list_games():
            head_id = (self.get_head(gid) or {}).get("id")
            sids = self.list_snapshots(gid)
            kept = set(sids[-keep:]) | {head_id}
            for sid in sids:
                path = self._game(gid) / "snapshots" / f"{sid}.json"
                if sid not in kept:
                    path.unlink(missing_ok=True)
                    removed_snaps += 1
                    continue
                doc = self._read_json(path) or {}
                referenced.update(m.get("sha256") for m in doc.get("files", {}).values() if isinstance(m, dict))
        objects = self.root / "objects"
        now = time.time()
        if objects.is_dir():
            for sub in objects.iterdir():
                if not sub.is_dir():
                    continue
                for f in sub.iterdir():
                    if f.name in referenced:
                        continue
                    try:
                        if now - f.stat().st_mtime > grace_seconds:
                            f.unlink()
                            removed_objects += 1
                    except OSError:
                        pass
        return {"snapshots": removed_snaps, "objects": removed_objects}


class HttpStore:
    """Kho trên SaveKeeper server (xem server.py)."""

    def __init__(self, url: str, token: str, timeout: float = 60):
        self.url = url.rstrip("/")
        self.base = self.url + "/v1"
        self.token = token
        self.timeout = timeout

    def __str__(self) -> str:
        return self.url

    def refresh(self) -> None:
        pass

    def flush(self, message: str = "") -> bool:
        return True

    def _open(self, method: str, path: str, *, data=None, headers=None, expect=(200,)):
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code in expect:
                return e
            detail = e.read(300).decode("utf-8", "replace")
            e.close()
            if e.code == 401:
                raise AuthError("Server từ chối: sai token.") from None
            raise StoreError(f"Server báo lỗi {e.code}: {detail}") from None
        except (urllib.error.URLError, OSError) as e:
            raise StoreError(f"Không kết nối được server {self.url}: {getattr(e, 'reason', e)}") from None

    def _json(self, method: str, path: str, body=None, expect=(200,)):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else {}
        try:
            with self._open(method, path, data=data, headers=headers, expect=expect) as resp:
                status, payload = resp.status, resp.read()
        except OSError as e:  # mất kết nối giữa chừng
            raise StoreError(f"Mất kết nối tới server {self.url}: {e}") from None
        try:
            return status, (json.loads(payload) if payload else None)
        except ValueError:
            raise StoreError("Server trả về dữ liệu không đọc được.") from None

    def missing(self, shas) -> list[str]:
        _, doc = self._json("POST", "/missing", {"shas": sorted(set(shas))})
        return sorted(check_sha(s) for s in doc["missing"])

    def put_object(self, sha: str, src) -> None:
        size = os.path.getsize(src)
        try:
            with open(src, "rb") as f, self._open("PUT", f"/objects/{check_sha(sha)}", data=f, expect=(200, 201),
                                                  headers={"Content-Length": str(size),
                                                           "Content-Type": "application/octet-stream"}) as resp:
                resp.read()
        except OSError as e:
            raise StoreError(f"Mất kết nối tới server {self.url}: {e}") from None

    def get_object(self, sha: str, dest) -> None:
        try:
            with self._open("GET", f"/objects/{check_sha(sha)}") as resp, open(dest, "wb") as out:
                shutil.copyfileobj(resp, out, CHUNK)
        except OSError as e:
            raise StoreError(f"Mất kết nối tới server {self.url}: {e}") from None

    def list_games(self) -> list[str]:
        return self._json("GET", "/games")[1]["games"]

    def get_head(self, gid: str):
        status, doc = self._json("GET", f"/games/{check_name(gid)}/head", expect=(200, 404))
        return doc if status == 200 else None

    def put_head(self, gid: str, doc: dict, expected) -> bool:
        status, _ = self._json("PUT", f"/games/{check_name(gid)}/head",
                               {"doc": doc, "expected": expected}, expect=(200, 409))
        return status == 200

    def list_snapshots(self, gid: str) -> list[str]:
        return self._json("GET", f"/games/{check_name(gid)}/snapshots")[1]["snapshots"]

    def get_snapshot(self, gid: str, sid: str):
        status, doc = self._json("GET", f"/games/{check_name(gid)}/snapshots/{check_name(sid)}", expect=(200, 404))
        return doc if status == 200 else None

    def put_snapshot(self, gid: str, sid: str, doc: dict) -> None:
        self._json("PUT", f"/games/{check_name(gid)}/snapshots/{check_name(sid)}", doc, expect=(200, 201))

    def gc(self, keep: int = 200, grace_seconds: float = 2 * 86400) -> dict:
        return {}  # server tự dọn dẹp mỗi ngày


class GitStore(DirStore):
    """Kho là một git repo (nên để private). Mỗi lần đồng bộ: kéo bản mới nhất, ghi xong thì commit + push.

    Bản clone nằm ở `workdir`. Mỗi ngày lịch sử repo được gộp lại còn một commit để repo không phình to.
    """

    def __init__(self, url: str, workdir, branch: str = "main"):
        super().__init__(workdir)
        self.url = url
        self.branch = branch
        self._dirty = False

    def __str__(self) -> str:
        return f"git {self.url}"

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        try:
            proc = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=900, env=env,
                                  creationflags=0x08000000 if os.name == "nt" else 0)
        except FileNotFoundError:
            raise StoreError("Máy này chưa cài git.") from None
        except subprocess.TimeoutExpired:
            raise StoreError("git chạy quá lâu, kiểm tra mạng.") from None
        if check and proc.returncode != 0:
            raise StoreError(f"git {args[0] if args[0] != '-C' else args[2]} lỗi: "
                             f"{(proc.stderr or proc.stdout).strip()[-400:]}")
        return proc

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return self._run(["-C", str(self.root), *args], check)

    def _ensure_clone(self) -> None:
        if (self.root / ".git").is_dir():
            return
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self._run(["clone", "-q", "--depth", "1", "--branch", self.branch, self.url, str(self.root)], check=False)
        if not (self.root / ".git").is_dir():  # repo trống: chưa có nhánh nào
            self._run(["clone", "-q", self.url, str(self.root)])
            self._git("symbolic-ref", "HEAD", f"refs/heads/{self.branch}")
        if not self._git("config", "user.email", check=False).stdout.strip():
            self._git("config", "user.email", "savekeeper@localhost")
            self._git("config", "user.name", "SaveKeeper")

    def _remote_tip(self) -> str | None:
        proc = self._git("rev-parse", "--verify", "--quiet", f"origin/{self.branch}", check=False)
        return proc.stdout.strip() or None

    def refresh(self) -> None:
        self._ensure_clone()
        fetch = self._git("fetch", "-q", "--depth", "1", "origin",
                          f"+refs/heads/{self.branch}:refs/remotes/origin/{self.branch}", check=False)
        if fetch.returncode != 0 and "couldn't find remote ref" not in fetch.stderr.lower():
            raise StoreError(f"git fetch lỗi: {fetch.stderr.strip()[-400:]}")  # repo trống thì không tính là lỗi
        if self._remote_tip():
            self._git("reset", "-q", "--hard", f"origin/{self.branch}")
        self._git("clean", "-q", "-fd")
        self._dirty = False

    def flush(self, message: str = "SaveKeeper") -> bool:
        if not self._dirty:
            return True
        self._git("add", "-A")
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            self._dirty = False
            return True
        self._git("commit", "-q", "-m", message)
        push = self._git("push", "-q", "origin", f"HEAD:refs/heads/{self.branch}", check=False)
        if push.returncode == 0:
            self._dirty = False
            return True
        error = (push.stderr or push.stdout).strip()
        self.refresh()  # quay về đúng bản trên server, bỏ commit vừa tạo
        if any(word in error for word in ("rejected", "fetch first", "non-fast-forward")):
            return False
        raise StoreError(f"git push lỗi: {error[-400:]}")

    def put_object_stream(self, sha: str, reader, length: int) -> None:
        super().put_object_stream(sha, reader, length)
        self._dirty = True

    def put_snapshot(self, gid: str, sid: str, doc: dict) -> None:
        super().put_snapshot(gid, sid, doc)
        self._dirty = True

    def put_head(self, gid: str, doc: dict, expected) -> bool:
        if not super().put_head(gid, doc, expected):
            return False
        self._dirty = True
        return self.flush(f"{gid}: {doc.get('id')}")

    def gc(self, keep: int = 200, grace_seconds: float = 2 * 86400) -> dict:
        self.refresh()
        stats = super().gc(keep, grace_seconds)
        self._dirty = True
        if not self.flush("SaveKeeper: dọn dẹp"):
            return stats
        old = self._git("rev-parse", "HEAD", check=False).stdout.strip()
        if not old:
            return stats
        tree = self._git("rev-parse", "HEAD^{tree}").stdout.strip()
        new = self._git("commit-tree", tree, "-m", "SaveKeeper: gộp lịch sử").stdout.strip()
        push = self._git("push", "-q", f"--force-with-lease=refs/heads/{self.branch}:{old}",
                         "origin", f"{new}:refs/heads/{self.branch}", check=False)
        if push.returncode == 0:
            self._git("reset", "-q", "--hard", new)
            self._git("reflog", "expire", "--expire=now", "--all", check=False)
            self._git("gc", "-q", "--prune=now", check=False)
            stats["squashed"] = True
        else:
            self.refresh()
        return stats


def open_store(remote: str, token: str = "", git_workdir=None):
    """remote: địa chỉ server http(s), một git repo (git:<url>, hoặc url kết thúc bằng .git),
    hoặc đường dẫn một thư mục dùng chung (Google Drive, OneDrive, NAS...)."""
    remote = remote.strip()
    if remote.lower().startswith("git:") or remote.endswith(".git") or remote.startswith("git@"):
        if git_workdir is None:
            raise StoreError("Thiếu thư mục để clone repo git.")
        return GitStore(remote[4:] if remote.lower().startswith("git:") else remote, git_workdir)
    if re.match(r"(?i)https?://", remote):
        return HttpStore(remote, token)
    return DirStore(Path(os.path.expandvars(remote)))
