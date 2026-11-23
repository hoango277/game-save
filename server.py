#!/usr/bin/env python3
"""SaveKeeper server: nơi lưu save chung cho mọi máy (chỉ cần Python 3.11+, không cài thêm gì).

    SAVEKEEPER_TOKEN=<chuỗi bí mật> python3 server.py --data /srv/savekeeper --port 8765

Nên đặt sau HTTPS (Caddy) hoặc chỉ mở trong mạng riêng (Tailscale). Xem README.md.
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import re
import shutil
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from savestore import CHUNK, MAX_JSON, MAX_OBJECT, DirStore, StoreError, check_name, check_sha

LOG = logging.getLogger("savekeeper-server")


class Handler(BaseHTTPRequestHandler):
    server_version = "SaveKeeper/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        LOG.info("%s %s", self.address_string(), fmt % args)

    # ---- trả lời

    def _send_json(self, status: int, doc) -> None:
        body = json.dumps(doc, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if status >= 400:
            self.send_header("Connection", "close")  # có thể còn dữ liệu chưa đọc của yêu cầu
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _length(self, limit: int):
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        return n if 0 <= n <= limit else None

    def _read_json(self):
        n = self._length(MAX_JSON)
        if n is None:
            raise StoreError("Thiếu hoặc sai Content-Length.")
        return json.loads(self.rfile.read(n) or b"null")

    def _authorized(self) -> bool:
        given = self.headers.get("Authorization", "").encode("utf-8")
        return hmac.compare_digest(given, f"Bearer {self.server.token}".encode("utf-8"))

    # ---- điều hướng

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        if method == "GET" and path == "/v1/health":
            return self._send_json(200, {"ok": True})
        if not self._authorized():
            return self._error(401, "Sai token.")
        for route_method, pattern, handler in ROUTES:
            match = pattern.fullmatch(path)
            if route_method == method and match:
                try:
                    return handler(self, *match.groups())
                except StoreError as e:
                    return self._error(400, str(e))
                except (ValueError, KeyError, TypeError, AttributeError) as e:
                    return self._error(400, f"Yêu cầu không hợp lệ: {e}")
        return self._error(404, "Không tìm thấy.")

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_POST(self):
        self._dispatch("POST")

    # ---- các API

    def missing(self):
        shas = self._read_json()["shas"]
        if not isinstance(shas, list) or len(shas) > 200_000:
            raise StoreError("Danh sách không hợp lệ.")
        self._send_json(200, {"missing": self.server.store.missing(check_sha(s) for s in shas)})

    def object_head(self, sha):
        path = self.server.store.object_path(check_sha(sha))
        self.send_response(200 if path.is_file() else 404)
        self.send_header("Content-Length", str(path.stat().st_size if path.is_file() else 0))
        self.end_headers()

    def object_get(self, sha):
        path = self.server.store.object_path(check_sha(sha))
        if not path.is_file():
            return self._error(404, "Không có file này.")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, CHUNK)

    def object_put(self, sha):
        n = self._length(MAX_OBJECT)
        if n is None:
            return self._error(413, "File quá lớn hoặc thiếu Content-Length.")
        self.server.store.put_object_stream(check_sha(sha), self.rfile, n)
        self._send_json(201, {"ok": True})

    def games(self):
        self._send_json(200, {"games": self.server.store.list_games()})

    def head_get(self, gid):
        doc = self.server.store.get_head(check_name(gid))
        if doc is None:
            return self._error(404, "Chưa có bản nào.")
        self._send_json(200, doc)

    def head_put(self, gid):
        body = self._read_json()
        if self.server.store.put_head(check_name(gid), body["doc"], body.get("expected")):
            return self._send_json(200, {"ok": True})
        self._send_json(409, {"error": "Bản mới nhất đã đổi.", "head": self.server.store.get_head(gid)})

    def snapshots(self, gid):
        self._send_json(200, {"snapshots": self.server.store.list_snapshots(check_name(gid))})

    def snapshot_get(self, gid, sid):
        doc = self.server.store.get_snapshot(check_name(gid), check_name(sid))
        if doc is None:
            return self._error(404, "Không có bản này.")
        self._send_json(200, doc)

    def snapshot_put(self, gid, sid):
        self.server.store.put_snapshot(check_name(gid), check_name(sid), self._read_json())
        self._send_json(201, {"ok": True})


_OBJ = r"/v1/objects/([^/]+)"
_GAME = r"/v1/games/([^/]+)"
ROUTES = [
    ("POST", re.compile(r"/v1/missing"), Handler.missing),
    ("HEAD", re.compile(_OBJ), Handler.object_head),
    ("GET", re.compile(_OBJ), Handler.object_get),
    ("PUT", re.compile(_OBJ), Handler.object_put),
    ("GET", re.compile(r"/v1/games"), Handler.games),
    ("GET", re.compile(_GAME + r"/head"), Handler.head_get),
    ("PUT", re.compile(_GAME + r"/head"), Handler.head_put),
    ("GET", re.compile(_GAME + r"/snapshots"), Handler.snapshots),
    ("GET", re.compile(_GAME + r"/snapshots/([^/]+)"), Handler.snapshot_get),
    ("PUT", re.compile(_GAME + r"/snapshots/([^/]+)"), Handler.snapshot_put),
]


class SaveServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store: DirStore, token: str):
        super().__init__(address, Handler)
        self.store = store
        self.token = token


def make_server(host: str, port: int, data_dir, token: str) -> SaveServer:
    if len(token) < 16:
        raise ValueError("Token phải dài ít nhất 16 ký tự.")
    return SaveServer((host, port), DirStore(data_dir), token)


def gc_loop(store: DirStore, keep: int, every: float = 86400) -> None:
    while True:
        try:
            LOG.info("Dọn dẹp kho: %s", store.gc(keep=keep))
        except Exception:
            LOG.exception("Dọn dẹp kho thất bại")
        time.sleep(every)


def main() -> int:
    parser = argparse.ArgumentParser(description="SaveKeeper server")
    parser.add_argument("--data", default=os.environ.get("SAVEKEEPER_DATA", "server-data"), help="thư mục lưu dữ liệu")
    parser.add_argument("--host", default=os.environ.get("SAVEKEEPER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SAVEKEEPER_PORT", "8765")))
    parser.add_argument("--keep", type=int, default=200, help="mỗi game giữ bao nhiêu bản trên server")
    parser.add_argument("--cert", help="file chứng chỉ TLS (nếu muốn server tự chạy HTTPS)")
    parser.add_argument("--key", help="file khoá TLS")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    token = os.environ.get("SAVEKEEPER_TOKEN", "")
    if len(token) < 16:
        sys.exit("Hãy đặt biến môi trường SAVEKEEPER_TOKEN (ít nhất 16 ký tự). Tạo nhanh: "
                 "python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    server = make_server(args.host, args.port, args.data, token)
    if args.cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.cert, args.key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=gc_loop, args=(server.store, args.keep), daemon=True).start()
    LOG.info("SaveKeeper server chạy tại %s:%s, dữ liệu ở %s", args.host, args.port, os.path.abspath(args.data))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
