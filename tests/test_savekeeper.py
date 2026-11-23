"""Kiểm thử SaveKeeper bằng Steam giả lập: đồng hồ, account, game đang chạy và Steam Cloud đều là giả.

Chạy:  python -m unittest discover -s tests -v
"""
import contextlib
import datetime as dt
import io
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import savekeeper as sk  # noqa: E402

LOG = logging.getLogger("savekeeper-test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False

CP_APPID = 1091500
USER_A = 11111111
USER_B = 22222222


class FakeEnv:
    """Giả lập Windows + Steam."""

    def __init__(self, root: Path):
        self.t = dt.datetime(2026, 11, 23, 20, 0, 0).timestamp()
        self.steam_on = True
        self.user = USER_A
        self.appid = 0
        self.procs: set[str] = set()
        self.root = root
        self.notes: list[tuple[str, str]] = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def process_names(self):
        return set(self.procs) | ({"steam.exe"} if self.steam_on else set())

    def steam_state(self, procs):
        return sk.SteamState(True, self.user, self.appid) if "steam.exe" in procs else sk.SteamState()

    def steam_path(self):
        return str(self.root / "Steam")

    def known_folder(self, key):
        return str(self.root / "home" / key)

    def notify(self, title, body):
        self.notes.append((title, body))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="savekeeper-test-"))
        self.env = FakeEnv(self.tmp)
        self.game = sk.GameConfig(id="cp", name="Cyberpunk 2077",
                                  save_dir=r"{saved_games}\CD Projekt Red\Cyberpunk 2077",
                                  steam_appids=(CP_APPID,), processes=("cyberpunk2077.exe",))
        self.cfg = sk.Config(vault_dir=self.tmp / "vault", games=[self.game], quiet_seconds=0)
        self.vault = sk.Vault(self.cfg.vault_dir, self.cfg.keep, self.cfg.keep_safety, clock=self.env.now, log=LOG)
        self.vault.retry_delay = 0
        self.saves = self.tmp / "home" / "saved_games" / "CD Projekt Red" / "Cyberpunk 2077"
        self.mtime = 1_700_000_000

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Game "ghi save": nội dung mới + mtime mới (giống game thật).
    def write(self, rel, text, root=None):
        p = (root or self.saves) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        self.mtime += 10
        os.utime(p, (self.mtime, self.mtime))
        return p

    def read(self, rel, root=None):
        return ((root or self.saves) / rel).read_text(encoding="utf-8")

    def watcher(self):
        w = sk.Watcher(self.cfg, self.env, self.vault, LOG)
        w.startup()
        return w

    def tick(self, w, seconds=2):
        self.env.t += seconds
        w.tick()

    def play(self, w, appid=CP_APPID):
        self.env.appid = appid
        self.tick(w)

    def quit(self, w):
        self.env.appid = 0
        self.tick(w)     # game tắt
        self.tick(w, 6)  # sau vài giây thì backup lần cuối

    def tags(self, game_id="cp"):
        return [s.tag for s in self.vault.snapshots(game_id)]


class VaultTests(Base):
    def test_skip_unchanged_and_hardlink_unchanged_files(self):
        self.write("ManualSave-1/sav.dat", "story 1")
        self.write("ManualSave-2/sav.dat", "story 2")
        status, s1 = self.vault.create_snapshot(self.game, self.saves, "manual")
        self.assertEqual(status, "created")
        self.assertEqual(len(s1.files), 2)

        status, _ = self.vault.create_snapshot(self.game, self.saves, "manual")
        self.assertEqual(status, "unchanged")

        self.write("ManualSave-2/sav.dat", "story 2 - later")
        status, s2 = self.vault.create_snapshot(self.game, self.saves, "periodic")
        self.assertEqual(status, "created")
        same_a = os.stat(s1.files_dir / "ManualSave-1/sav.dat")
        same_b = os.stat(s2.files_dir / "ManualSave-1/sav.dat")
        self.assertEqual(same_a.st_ino, same_b.st_ino, "file không đổi phải dùng chung (hardlink)")
        self.assertEqual(self.read("ManualSave-2/sav.dat", s2.files_dir), "story 2 - later")
        self.assertEqual(self.read("ManualSave-2/sav.dat", s1.files_dir), "story 2")

    def test_rotation_keeps_newest(self):
        self.vault.keep = 3
        for i in range(5):
            self.write("AutoSave-0/sav.dat", f"v{i}")
            self.env.t += 60
            self.vault.create_snapshot(self.game, self.saves, "periodic")
        snaps = self.vault.snapshots("cp")
        self.assertEqual(len(snaps), 3)
        self.assertEqual(self.read("AutoSave-0/sav.dat", snaps[0].files_dir), "v2")
        self.assertEqual(self.read("AutoSave-0/sav.dat", snaps[-1].files_dir), "v4")

    def test_file_written_during_copy_is_copied_again(self):
        p = self.write("QuickSave-0/sav.dat", "half")
        real_copy = sk._copy_hash
        calls = []

        def game_writes_meanwhile(src, dst):
            result = real_copy(src, dst)
            if not calls:
                calls.append(1)
                p.write_text("full save", encoding="utf-8")
                os.utime(p, (self.mtime + 5, self.mtime + 5))
            return result

        with mock.patch.object(sk, "_copy_hash", game_writes_meanwhile):
            _, snap = self.vault.create_snapshot(self.game, self.saves, "periodic")
        self.assertEqual(self.read("QuickSave-0/sav.dat", snap.files_dir), "full save")
        self.assertEqual(snap.files["QuickSave-0/sav.dat"]["size"], len("full save"))


class WatcherTests(Base):
    def test_backup_every_15_minutes_and_on_exit(self):
        self.write("AutoSave-0/sav.dat", "start")
        w = self.watcher()
        self.assertEqual(self.tags(), ["startup"])

        self.play(w)
        self.write("AutoSave-0/sav.dat", "after 10 minutes")
        self.tick(w, 10 * 60)
        self.assertEqual(self.tags(), ["startup"], "chưa tới 15 phút")
        self.tick(w, 6 * 60)
        self.assertEqual(self.tags(), ["startup", "periodic"])

        self.tick(w, 15 * 60)  # save không đổi thì không tạo bản mới
        self.assertEqual(self.tags(), ["startup", "periodic"])

        self.write("ManualSave-1/sav.dat", "before quitting")
        self.quit(w)
        self.assertEqual(self.tags(), ["startup", "periodic", "exit"])
        self.assertIn("ManualSave-1/sav.dat", self.vault.latest("cp").files)

    def test_steam_cloud_overwrite_is_undone_when_game_starts(self):
        self.write("ManualSave-1/sav.dat", "mine lv30")
        self.write("ManualSave-2/sav.dat", "mine lv31")
        w = self.watcher()
        self.play(w)
        self.write("ManualSave-3/sav.dat", "mine lv32")
        self.quit(w)

        # Đổi sang account B; Steam Cloud của B kéo save của người khác về lúc bấm Play.
        self.env.user = USER_B
        self.tick(w)
        self.write("ManualSave-2/sav.dat", "someone else lv5")   # ghi đè
        (self.saves / "ManualSave-3/sav.dat").unlink()           # xoá
        self.write("ManualSave-9/sav.dat", "someone else lv2")   # thêm
        self.play(w)

        self.assertEqual(self.read("ManualSave-2/sav.dat"), "mine lv31")
        self.assertEqual(self.read("ManualSave-3/sav.dat"), "mine lv32")
        self.assertEqual(self.read("ManualSave-9/sav.dat"), "someone else lv2", "không được xoá file lạ")
        base = self.vault.latest("cp")
        self.assertEqual(os.stat(self.saves / "ManualSave-2/sav.dat").st_mtime_ns,
                         base.files["ManualSave-2/sav.dat"]["mtime_ns"], "giữ nguyên giờ lưu gốc")
        safety = [s for s in self.vault.snapshots("cp") if s.tag == "before-restore"]
        self.assertEqual(len(safety), 1)
        self.assertEqual(self.read("ManualSave-2/sav.dat", safety[0].files_dir), "someone else lv5")
        self.assertTrue(any("giữ lại save" in title for title, _ in self.env.notes))

    def test_nothing_happens_when_save_unchanged(self):
        self.write("ManualSave-1/sav.dat", "a")
        w = self.watcher()
        self.play(w)
        self.quit(w)
        self.env.user = USER_B
        self.tick(w)
        self.play(w)
        self.assertNotIn("before-restore", self.tags())
        self.assertEqual(self.env.notes, [])

    def test_quick_restart_never_rolls_back_progress(self):
        self.write("AutoSave-0/sav.dat", "old")
        w = self.watcher()
        self.play(w)
        self.write("AutoSave-0/sav.dat", "new progress")
        self.env.appid = 0
        self.tick(w)  # tắt game...
        self.play(w)  # ...và mở lại sau 2 giây, trước khi kịp backup lúc thoát
        self.assertEqual(self.read("AutoSave-0/sav.dat"), "new progress")
        self.assertEqual(self.vault.latest("cp").tag, "exit")
        self.assertNotIn("before-restore", self.tags())

    def test_failed_exit_backup_never_triggers_restore(self):
        self.write("AutoSave-0/sav.dat", "old")
        w = self.watcher()
        self.play(w)
        self.write("AutoSave-0/sav.dat", "new progress")
        with mock.patch.object(sk, "_copy_hash", side_effect=PermissionError("file đang bị khoá")):
            self.quit(w)
            self.play(w)
        self.assertEqual(self.read("AutoSave-0/sav.dat"), "new progress")
        self.assertTrue(any("backup lỗi" in title for title, _ in self.env.notes))

    def test_per_account_save_follows_you_between_accounts(self):
        game = sk.GameConfig(id="pa", name="Per-account game",
                             save_dir=r"{steam}\userdata\{steam_id3}\4242\remote", steam_appids=(4242,))
        self.cfg.games = [game]
        dir_a = self.tmp / "Steam" / "userdata" / str(USER_A) / "4242" / "remote"
        dir_b = self.tmp / "Steam" / "userdata" / str(USER_B) / "4242" / "remote"
        self.write("save.dat", "progress A1", root=dir_a)
        w = self.watcher()
        self.play(w, 4242)
        self.write("save.dat", "progress A2", root=dir_a)
        self.quit(w)

        self.write("save.dat", "old save of B", root=dir_b)
        self.env.user = USER_B
        self.tick(w)
        self.assertEqual(self.read("save.dat", dir_b), "progress A2")
        safety = [s for s in self.vault.snapshots("pa") if s.tag == "before-restore"]
        self.assertEqual(self.read("save.dat", safety[-1].files_dir), "old save of B")

        self.play(w, 4242)
        self.write("save.dat", "progress B1", root=dir_b)
        self.quit(w)
        self.env.user = USER_A
        self.tick(w)
        self.assertEqual(self.read("save.dat", dir_a), "progress B1")

    def test_stale_registry_ignored_when_steam_closed(self):
        self.write("AutoSave-0/sav.dat", "x")
        w = self.watcher()
        self.env.steam_on = False
        self.env.appid = CP_APPID  # giá trị cũ còn sót trong registry
        self.tick(w)
        self.assertFalse(w.states["cp"].running)

    def test_game_detected_by_process_name_without_steam(self):
        w = self.watcher()
        self.env.steam_on = False
        self.env.procs = {"cyberpunk2077.exe"}
        self.tick(w)
        self.assertTrue(w.states["cp"].running)


class RestoreCommandTests(Base):
    def app(self):
        app = sk.App(self.cfg, self.env, LOG)
        app.vault.retry_delay = 0
        return app

    def test_restore_older_snapshot_becomes_new_baseline(self):
        self.write("AutoSave-0/sav.dat", "v1")
        _, s1 = self.vault.create_snapshot(self.game, self.saves, "manual")
        self.env.t += 60
        self.write("AutoSave-0/sav.dat", "v2")
        self.vault.create_snapshot(self.game, self.saves, "manual")

        app = self.app()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.restore("cp", s1.name, yes=True), 0)
        self.assertEqual(self.read("AutoSave-0/sav.dat"), "v1")
        self.assertEqual(app.vault.latest("cp").tag, "restore")

        w = self.watcher()  # lần mở game sau không được "sửa" lại thành v2
        self.play(w)
        self.assertEqual(self.read("AutoSave-0/sav.dat"), "v1")

    def test_restore_refused_while_game_running(self):
        self.write("AutoSave-0/sav.dat", "v1")
        self.vault.create_snapshot(self.game, self.saves, "manual")
        self.env.appid = CP_APPID
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.app().restore("cp", "latest", yes=True), 1)


class ConfigTests(unittest.TestCase):
    def test_shipped_config_loads(self):
        cfg = sk.load_config(sk.CONFIG_PATH)
        ids = [g.id for g in cfg.games]
        self.assertIn("cyberpunk2077", ids)
        self.assertIn("witcher3", ids)
        self.assertEqual(cfg.interval_minutes, 15)
        self.assertEqual(cfg.vault_dir, sk.APP_DIR / "vault")
        self.assertFalse(any(g.per_account for g in cfg.games))

    def test_unknown_placeholder_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text("[[games]]\nname = 'X'\nsave_dir = '{nope}\\x'\n", encoding="utf-8")
            with self.assertRaises(sk.ConfigError):
                sk.load_config(p)

    def test_placeholders(self):
        env = FakeEnv(Path(r"C:\fake"))
        self.assertEqual(sk.resolve_save_dir(r"{steam}\userdata\{steam_id3}\1\remote", env, 22),
                         Path(r"C:\fake\Steam\userdata\22\1\remote"))
        self.assertIsNone(sk.resolve_save_dir(r"{appdata}\G\{steam_id64}", env, 0))
        self.assertEqual(sk.resolve_save_dir(r"{appdata}\G\{steam_id64}", env, 1).name, "76561197960265729")


@unittest.skipUnless(sk.IS_WINDOWS, "chỉ chạy trên Windows")
class WindowsSmokeTests(unittest.TestCase):
    """Gọi API Windows thật (chỉ đọc)."""

    def test_known_folders(self):
        env = sk.WindowsEnv()
        for key in sk.KNOWN_FOLDERS:
            self.assertTrue(Path(env.known_folder(key)).is_absolute(), key)
        self.assertTrue(Path(env.known_folder("documents")).is_dir())

    def test_process_list_sees_python(self):
        self.assertIn(Path(sys.executable).name.lower(), sk.WindowsEnv().process_names())

    def test_steam_state_reads_without_error(self):
        env = sk.WindowsEnv()
        self.assertIsInstance(env.steam_state(env.process_names()), sk.SteamState)

    def test_single_instance_mutex(self):
        with mock.patch.object(sk, "MUTEX_NAME", "Local\\SaveKeeper.Test." + str(os.getpid())):
            self.assertFalse(sk.watcher_is_running())
            handle = sk.acquire_single_instance()
            self.assertIsNotNone(handle)
            self.assertTrue(sk.watcher_is_running())
            self.assertIsNone(sk.acquire_single_instance())
            sk._k32.CloseHandle(handle)
            self.assertFalse(sk.watcher_is_running())


if __name__ == "__main__":
    unittest.main()
