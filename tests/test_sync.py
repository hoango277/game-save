"""Kiểm thử đồng bộ nhiều máy: hai "máy" giả lập dùng chung một kho (thư mục, git repo, server HTTP).

Chạy:  python -m unittest discover -s tests -v
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import savekeeper as sk  # noqa: E402
import savestore  # noqa: E402
import server  # noqa: E402
from test_savekeeper import CP_APPID, USER_A, FakeEnv  # noqa: E402

LOG = logging.getLogger("savekeeper-sync-test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False

GAME = sk.GameConfig(id="cp", name="Cyberpunk 2077", save_dir=r"{saved_games}\CD Projekt Red\Cyberpunk 2077",
                     steam_appids=(CP_APPID,), processes=("cyberpunk2077.exe",))
TOKEN = "test-token-0123456789abcdef"
_mtime = [1_700_000_000]


class Machine:
    """Một máy chơi game giả lập: thư mục save, kho riêng và kết nối tới kho chung."""

    def __init__(self, root: Path, name: str, store):
        self.env = FakeEnv(root)
        self.saves = root / "home" / "saved_games" / "CD Projekt Red" / "Cyberpunk 2077"
        self.vault = sk.Vault(root / "vault", clock=self.env.now, log=LOG)
        self.vault.retry_delay = 0
        self.notes: list[tuple[str, str]] = []
        self.syncer = sk.Syncer(sk.SyncConfig(remote=str(store)), store, self.vault, LOG,
                                lambda title, body: self.notes.append((title, body)))
        self.syncer.machine = name

    def write(self, rel: str, text: str) -> None:
        p = self.saves / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        _mtime[0] += 10
        os.utime(p, (_mtime[0], _mtime[0]))

    def read(self, rel: str) -> str:
        return (self.saves / rel).read_text(encoding="utf-8")

    def backup(self) -> None:
        self.env.t += 60
        self.vault.create_snapshot(GAME, self.saves, "exit")

    def sync(self, can_write: bool = True) -> str:
        self.env.t += 60
        return self.syncer.sync_game(GAME, self.saves, USER_A, lambda: can_write)


class SyncScenarios:
    """Các tình huống chung, chạy lại cho từng loại kho (lớp con định nghĩa make_store)."""

    def make_store(self, name: str):
        raise NotImplementedError

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="savekeeper-sync-"))
        self.a = Machine(self.tmp / "A", "PC-A", self.make_store("A"))
        self.b = Machine(self.tmp / "B", "PC-B", self.make_store("B"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start_on_a_then_b(self):
        self.a.write("ManualSave-1/sav.dat", "A lv10")
        self.a.write("ManualSave-2/sav.dat", "A lv11")
        self.a.backup()
        self.assertEqual(self.a.sync(), "pushed")
        self.assertEqual(self.b.sync(), "pulled")

    def test_new_machine_gets_latest_progress(self):
        self.start_on_a_then_b()
        self.assertEqual(self.b.read("ManualSave-2/sav.dat"), "A lv11")
        self.assertEqual(os.stat(self.b.saves / "ManualSave-2/sav.dat").st_mtime_ns,
                         os.stat(self.a.saves / "ManualSave-2/sav.dat").st_mtime_ns)
        self.assertEqual(self.b.sync(), "in-sync")
        self.assertEqual(self.a.sync(), "in-sync")

    def test_progress_moves_back_and_forth(self):
        self.start_on_a_then_b()
        self.b.write("ManualSave-3/sav.dat", "B lv12")
        self.b.backup()
        self.assertEqual(self.b.sync(), "pushed")
        self.assertEqual(self.a.sync(), "pulled")
        self.assertEqual(self.a.read("ManualSave-3/sav.dat"), "B lv12")

    def test_pull_waits_while_game_running(self):
        self.a.write("AutoSave-0/sav.dat", "A")
        self.a.backup()
        self.a.sync()
        self.assertEqual(self.b.sync(can_write=False), "pull-pending")
        self.assertFalse(self.b.saves.exists())

    def test_both_changed_is_conflict_and_nothing_is_overwritten(self):
        self.start_on_a_then_b()
        self.a.write("AutoSave-0/sav.dat", "A played more")
        self.a.backup()
        self.assertEqual(self.a.sync(), "pushed")
        self.b.write("AutoSave-0/sav.dat", "B played offline")
        self.b.backup()
        self.assertEqual(self.b.sync(), "conflict")
        self.assertEqual(self.b.read("AutoSave-0/sav.dat"), "B played offline")
        self.assertTrue(any("2 tiến trình" in t for t, _ in self.b.notes))
        store = self.b.syncer.store
        store.refresh()
        head = store.get_head("cp")
        self.assertEqual(head["machine"], "PC-A")
        side = [sid for sid in store.list_snapshots("cp") if "pc-b" in sid]
        self.assertEqual(len(side), 1, "bản của máy B phải được giữ trên kho làm bản phụ")
        self.assertEqual(self.b.sync(), "conflict", "không tự quyết khi cả hai cùng đổi")

        # Người chơi chọn bản của máy B (bản phụ) trên máy A -> thành bản chung.
        _snap, res = self.a.syncer.pull(GAME, side[0], self.a.saves, USER_A)
        self.assertEqual(self.a.read("AutoSave-0/sav.dat"), "B played offline")
        self.assertIsNotNone(res.safety, "save cũ của máy A phải được giữ lại")
        self.assertEqual(self.b.sync(), "in-sync")
        self.assertEqual(self.a.sync(), "in-sync")

    def test_push_local_resolves_conflict(self):
        self.start_on_a_then_b()
        self.a.write("AutoSave-0/sav.dat", "A")
        self.a.backup()
        self.a.sync()
        self.b.write("AutoSave-0/sav.dat", "B wins")
        self.b.backup()
        self.assertEqual(self.b.sync(), "conflict")
        self.b.syncer.push_local(GAME)
        self.assertEqual(self.a.sync(), "pulled")
        self.assertEqual(self.a.read("AutoSave-0/sav.dat"), "B wins")


class DirStoreSyncTests(SyncScenarios, unittest.TestCase):
    def make_store(self, name):
        return savestore.DirStore(self.tmp / "shared")

    def test_tampered_file_is_rejected(self):
        self.a.write("AutoSave-0/sav.dat", "good")
        self.a.backup()
        self.a.sync()
        objects = list((self.tmp / "shared" / "objects").rglob("*"))
        for f in objects:
            if f.is_file():
                f.write_bytes(b"corrupted")
        with self.assertRaises(sk.SnapshotError):
            self.b.sync()
        self.assertFalse(self.b.saves.exists())
        self.assertEqual(self.b.vault.snapshots("cp"), [])

    def test_path_traversal_in_remote_list_is_rejected(self):
        self.a.write("AutoSave-0/sav.dat", "x")
        self.a.backup()
        self.a.sync()
        store = savestore.DirStore(self.tmp / "shared")
        head = store.get_head("cp")
        doc = store.get_snapshot("cp", head["id"])
        doc["files"] = {"../../evil.txt": next(iter(doc["files"].values()))}
        store.put_snapshot("cp", "evil", doc)
        store.put_head("cp", {"id": "evil", "content": "evil"}, expected=head["id"])
        with self.assertRaises(sk.SnapshotError):
            self.b.sync()
        self.assertFalse((self.tmp / "B" / "home" / "evil.txt").exists())

    def test_gc_keeps_head_and_newest(self):
        for i in range(5):
            self.a.write("AutoSave-0/sav.dat", f"v{i}")
            self.a.backup()
            self.a.sync()
        store = savestore.DirStore(self.tmp / "shared")
        stats = store.gc(keep=2, grace_seconds=-1)
        self.assertEqual(len(store.list_snapshots("cp")), 2)
        self.assertEqual(stats["snapshots"], 3)
        self.assertEqual(stats["objects"], 3)
        self.assertEqual(self.b.sync(), "pulled")
        self.assertEqual(self.b.read("AutoSave-0/sav.dat"), "v4")


class GitStoreSyncTests(SyncScenarios, unittest.TestCase):
    def make_store(self, name):
        bare = self.tmp / "remote.git"
        if not bare.exists():
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        return savestore.GitStore(str(bare), self.tmp / name / "git-store")

    def test_concurrent_push_is_detected(self):
        self.a.write("AutoSave-0/sav.dat", "base")
        self.a.backup()
        self.a.sync()
        self.b.sync()
        store_a, store_b = self.a.syncer.store, self.b.syncer.store
        store_a.refresh()
        store_b.refresh()
        head = store_a.get_head("cp")
        self.assertTrue(store_a.put_head("cp", {"id": "from-a"}, expected=head["id"]))
        self.assertFalse(store_b.put_head("cp", {"id": "from-b"}, expected=head["id"]),
                         "máy B đẩy sau trên bản cũ phải bị từ chối")
        store_b.refresh()
        self.assertEqual(store_b.get_head("cp")["id"], "from-a")

    def test_gc_squashes_history(self):
        for i in range(3):
            self.a.write("AutoSave-0/sav.dat", f"v{i}")
            self.a.backup()
            self.a.sync()
        self.a.syncer.store.gc(keep=200)
        count = subprocess.run(["git", "-C", str(self.tmp / "remote.git"), "rev-list", "--count", "main"],
                               capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(count, "1")
        self.assertEqual(self.b.sync(), "pulled")
        self.assertEqual(self.b.read("AutoSave-0/sav.dat"), "v2")


class HttpStoreSyncTests(SyncScenarios, unittest.TestCase):
    """Chạy server.py thật trên 127.0.0.1 và đồng bộ qua HTTP."""

    def setUp(self):
        self.server_dir = Path(tempfile.mkdtemp(prefix="savekeeper-server-"))
        self.httpd = server.make_server("127.0.0.1", 0, self.server_dir, TOKEN)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        super().setUp()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.server_dir, ignore_errors=True)
        super().tearDown()

    def make_store(self, name):
        return savestore.HttpStore(self.url, TOKEN, timeout=10)

    def _raw(self, method, path, data=None, token=TOKEN):
        req = urllib.request.Request(self.url + path, data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_wrong_token_rejected(self):
        with self.assertRaises(savestore.AuthError):
            savestore.HttpStore(self.url, "wrong-token-0000000000").list_games()
        self.assertEqual(self._raw("GET", "/v1/games", token=None), 401)

    def test_health_needs_no_token(self):
        self.assertEqual(self._raw("GET", "/v1/health", token=None), 200)

    def test_bad_requests_rejected(self):
        self.assertEqual(self._raw("PUT", "/v1/objects/" + "0" * 64, data=b"content does not match"), 400)
        self.assertEqual(self._raw("GET", "/v1/games/..%2F..%2Fetc/head"), 400)
        self.assertEqual(self._raw("GET", "/v1/objects/../../secret"), 404)
        self.assertEqual(self._raw("PUT", "/v1/games/cp/snapshots/x", data=json.dumps({"files": 1}).encode()), 400)
        self.assertFalse(any(self.server_dir.rglob("secret")))

    def test_head_compare_and_swap(self):
        store = savestore.HttpStore(self.url, TOKEN)
        self.assertTrue(store.put_head("cp", {"id": "one"}, expected=None))
        self.assertFalse(store.put_head("cp", {"id": "two"}, expected=None))
        self.assertTrue(store.put_head("cp", {"id": "two"}, expected="one"))
        self.assertEqual(store.get_head("cp")["id"], "two")


class WatcherSyncTests(unittest.TestCase):
    """SaveKeeper chạy nền trên hai máy: chơi ở máy A, sang máy B chơi tiếp."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="savekeeper-wsync-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def machine(self, name):
        root = self.tmp / name
        env = FakeEnv(root)
        cfg = sk.Config(vault_dir=root / "vault", games=[GAME], quiet_seconds=0,
                        sync=sk.SyncConfig(remote=str(self.tmp / "shared"), machine=name))
        vault = sk.Vault(cfg.vault_dir, clock=env.now, log=LOG)
        vault.retry_delay = 0
        w = sk.Watcher(cfg, env, vault, LOG)
        w.syncer = sk.make_syncer(cfg, vault, LOG, w.notify)
        saves = root / "home" / "saved_games" / "CD Projekt Red" / "Cyberpunk 2077"
        return env, w, saves

    @staticmethod
    def tick(env, w, seconds=2):
        env.t += seconds
        w.tick()

    def test_play_on_one_pc_continue_on_another(self):
        env_a, wa, saves_a = self.machine("PC-A")
        wa.startup()
        self.tick(env_a, wa)
        env_a.appid = CP_APPID
        self.tick(env_a, wa)  # mở game
        (saves_a / "AutoSave-0").mkdir(parents=True)
        (saves_a / "AutoSave-0" / "sav.dat").write_text("A progress", encoding="utf-8")
        env_a.appid = 0
        self.tick(env_a, wa)     # thoát game
        self.tick(env_a, wa, 6)  # backup lúc thoát -> tự đẩy lên kho chung
        self.assertEqual(savestore.DirStore(self.tmp / "shared").get_head("cp")["machine"], "PC-A")

        env_b, wb, saves_b = self.machine("PC-B")
        wb.startup()
        self.tick(env_b, wb)  # lần kiểm tra đầu tiên -> tự tải về
        self.assertEqual((saves_b / "AutoSave-0" / "sav.dat").read_text(encoding="utf-8"), "A progress")
        self.assertTrue(any("tải save mới nhất" in title for title, _ in env_b.notes))

        # Máy B chơi tiếp, máy A tự nhận bản mới ở lần kiểm tra sau.
        env_b.appid = CP_APPID
        self.tick(env_b, wb)
        (saves_b / "AutoSave-0" / "sav.dat").write_text("B progress", encoding="utf-8")
        os.utime(saves_b / "AutoSave-0" / "sav.dat", (1_800_000_000, 1_800_000_000))
        env_b.appid = 0
        self.tick(env_b, wb)
        self.tick(env_b, wb, 6)
        self.tick(env_a, wa, 6 * 60)
        self.assertEqual((saves_a / "AutoSave-0" / "sav.dat").read_text(encoding="utf-8"), "B progress")


class LocalConfigTests(unittest.TestCase):
    def test_local_toml_adds_sync_and_games(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.toml").write_text(
                "[[games]]\nname = 'A'\nsave_dir = '{documents}\\A'\n", encoding="utf-8")
            (Path(d) / "local.toml").write_text(
                "[sync]\nremote = 'https://save.example.com'\ntoken = '0123456789abcdef0123'\n"
                "[[games]]\nname = 'B'\nsave_dir = '{documents}\\B'\n", encoding="utf-8")
            cfg = sk.load_config(Path(d) / "config.toml")
            self.assertEqual([g.name for g in cfg.games], ["A", "B"])
            self.assertEqual(cfg.sync.remote, "https://save.example.com")

    def test_server_without_token_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.toml").write_text("[sync]\nremote = 'https://save.example.com'\n", encoding="utf-8")
            with self.assertRaises(sk.ConfigError):
                sk.load_config(Path(d) / "config.toml")

    def test_store_kind_from_remote(self):
        self.assertIsInstance(savestore.open_store("https://x.example.com", TOKEN), savestore.HttpStore)
        self.assertIsInstance(savestore.open_store("https://github.com/u/r.git", git_workdir="w"), savestore.GitStore)
        self.assertIsInstance(savestore.open_store("git:https://example.com/r", git_workdir="w"), savestore.GitStore)
        self.assertIsInstance(savestore.open_store(r"G:\My Drive\SaveKeeper"), savestore.DirStore)


if __name__ == "__main__":
    unittest.main()
