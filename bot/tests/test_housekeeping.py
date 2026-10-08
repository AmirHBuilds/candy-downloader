"""
Nothing a job or an upload leaves behind may stay on the server: orphaned processes, zombies, the local Bot API
server's copies of files, and the folders of running jobs must be left alone.
"""
import asyncio
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import _env  # noqa: F401  (must come first)

import config  # noqa: E402
import main  # noqa: E402
from tests.test_procs import Fakes  # noqa: E402
from utils import cleanup, housekeeping, procs  # noqa: E402

JOB_A = "a" * 32
JOB_B = "b" * 32


class Orphans(unittest.TestCase):
    def setUp(self):
        self.fakes = Fakes(self)
        self.tmp = Path(tempfile.mkdtemp(prefix="hk-tmp-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def spawn(self, name, *parts):
        return self.fakes.spawn(name, str(self.tmp.joinpath(*parts)))

    def test_a_process_of_a_finished_job_is_killed_and_a_running_jobs_is_not(self):
        orphan = self.spawn("ffmpeg", JOB_A, "in.mp4")
        running = self.spawn("ffmpeg", JOB_B, "in.mp4")
        self.assertEqual(procs.kill_orphans(self.tmp, {JOB_B}), 1)
        orphan.wait(timeout=3)
        self.assertIsNone(running.poll())

    def test_every_downloader_counts_not_just_ffmpeg(self):
        spawned = [self.spawn(name, JOB_A, "x") for name in ("ffmpeg", "ffprobe", "aria2c", "gallery-dl")]
        self.assertEqual(procs.kill_orphans(self.tmp, set()), 4)
        for proc in spawned:
            proc.wait(timeout=3)

    def test_things_that_are_not_job_processes_are_left_alone(self):
        other_tool = self.spawn("python3", JOB_A, "x")                           # not a media tool
        upload = self.spawn("ffmpeg", "tools", "abcdef0123", "original.mp4")    # the toolbox's stored upload
        elsewhere = self.fakes.spawn("ffmpeg", f"/somewhere/else/{JOB_A}/in.mp4")
        self.assertEqual(procs.kill_orphans(self.tmp, set()), 0)
        for proc in (other_tool, upload, elsewhere):
            self.assertIsNone(proc.poll())

    def test_a_longer_hex_name_is_not_mistaken_for_a_job_folder(self):
        proc = self.spawn("ffmpeg", JOB_A + "ff", "x")
        self.assertEqual(procs.kill_orphans(self.tmp, set()), 0)
        self.assertIsNone(proc.poll())

    def test_nothing_running_is_fine(self):
        self.assertEqual(procs.kill_orphans(self.tmp, set()), 0)

    def test_a_running_jobs_workspace_is_registered_as_active(self):
        with cleanup.job_workspace() as workspace:
            self.assertIn(workspace.name, cleanup.ACTIVE_WORKSPACES)
            name = workspace.name
        self.assertNotIn(name, cleanup.ACTIVE_WORKSPACES)

    def test_an_exception_inside_a_job_still_unregisters_it(self):
        with self.assertRaises(RuntimeError):
            with cleanup.job_workspace() as workspace:
                name = workspace.name
                raise RuntimeError("boom")
        self.assertNotIn(name, cleanup.ACTIVE_WORKSPACES)


class Zombies(unittest.TestCase):
    def make_zombie(self):
        proc = subprocess.Popen(["true"])
        for _ in range(50):                                   # wait until it has exited but is not collected
            if proc.pid in procs.zombies():
                return proc
            time.sleep(0.05)
        self.fail("could not create a zombie")

    def test_a_zombie_is_listed_with_its_parent(self):
        proc = self.make_zombie()
        self.assertEqual(procs.zombies()[proc.pid], os.getpid())
        proc.wait()

    def test_it_is_collected_on_the_second_sweep_not_the_first(self):
        proc = self.make_zombie()
        reaper = procs.ZombieReaper()
        self.assertEqual(reaper.sweep(), (0, len(procs.zombies())))       # first sight: asyncio may still be about to
        self.assertIn(proc.pid, procs.zombies())
        reaped, _ = reaper.sweep()
        self.assertGreaterEqual(reaped, 1)
        self.assertNotIn(proc.pid, procs.zombies())

    def test_zombies_that_belong_to_someone_else_are_reported_not_touched(self):
        reaper = procs.ZombieReaper()
        with mock.patch.object(procs, "zombies", return_value={999999: 1}), \
                mock.patch.object(procs.os, "waitpid") as waitpid:
            self.assertEqual(reaper.sweep(), (0, 1))
            self.assertEqual(reaper.sweep(), (0, 1))
        waitpid.assert_not_called()

    def test_a_zombie_that_goes_away_is_forgotten(self):
        reaper = procs.ZombieReaper()
        with mock.patch.object(procs, "zombies", return_value={999999: 1}):
            reaper.sweep()
        with mock.patch.object(procs, "zombies", return_value={}):
            reaper.sweep()
        self.assertEqual(reaper._seen, {})


class BotApiStorage(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="botapi-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.token = self.root / "123456:ABC"
        self.now = 1_000_000.0

    def put(self, relative, age_minutes, size=10):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        stamp = self.now - age_minutes * 60
        os.utime(path, (stamp, stamp))
        return path

    def sweep(self, minutes=60):
        return cleanup.sweep_bot_api_files(self.root, minutes * 60, now=self.now)

    def test_old_files_go_and_recent_ones_stay(self):
        old = self.put("123456:ABC/videos/file_1.mp4", 120, size=1000)
        new = self.put("123456:ABC/videos/file_2.mp4", 5)
        self.assertEqual(self.sweep(), (1, 1000))
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_every_kind_of_sent_file_is_covered_at_both_levels(self):
        names = [f"123456:ABC/{d}/f.bin" for d in ("documents", "photos", "videos", "video_notes", "voice", "audios", "animations")]
        names += ["documents/f.bin", "temp/f.bin"]
        paths = [self.put(name, 300) for name in names]
        self.assertEqual(self.sweep()[0], len(paths))
        self.assertTrue(not any(p.exists() for p in paths))

    def test_the_servers_own_database_and_other_folders_are_never_touched(self):
        keep = [self.put("123456:ABC/td.binlog", 9999), self.put("123456:ABC/db/data.sqlite", 9999),
                self.put("webhooks_db.binlog", 9999), self.put("123456:ABC/secret_chats/x", 9999)]
        self.assertEqual(self.sweep(), (0, 0))
        self.assertTrue(all(p.exists() for p in keep))

    def test_nested_files_are_found_and_directories_survive(self):
        nested = self.put("123456:ABC/documents/sub/deeper/file.txt", 500)
        self.sweep()
        self.assertFalse(nested.exists())
        self.assertTrue((self.root / "123456:ABC" / "documents").is_dir())

    def test_symlinks_are_not_followed(self):
        outside = Path(tempfile.mkdtemp(prefix="outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        victim = outside / "precious.txt"
        victim.write_text("keep me")
        os.utime(victim, (0, 0))
        link = self.root / "123456:ABC" / "documents" / "link.txt"
        link.parent.mkdir(parents=True)
        link.symlink_to(victim)
        self.sweep()
        self.assertTrue(victim.exists())
        self.assertTrue(link.is_symlink())                    # only regular files are ever deleted

    def test_a_missing_storage_folder_is_fine(self):
        self.assertEqual(cleanup.sweep_bot_api_files(self.root / "nope", 60), (0, 0))

    def test_drop_server_copy_deletes_only_sent_files_inside_the_storage(self):
        sent = self.put("123456:ABC/videos/file_9.mp4", 0)
        binlog = self.put("123456:ABC/td.binlog", 0)
        elsewhere = Path(tempfile.mkdtemp(prefix="elsewhere-")) / "videos"
        elsewhere.mkdir()
        outside = elsewhere / "file.mp4"
        outside.write_bytes(b"x")
        self.addCleanup(shutil.rmtree, elsewhere.parent, ignore_errors=True)
        self.assertTrue(cleanup.drop_server_copy(str(sent), root=self.root))
        self.assertFalse(sent.exists())
        for path in (binlog, outside, self.root / "123456:ABC" / "videos"):
            self.assertFalse(cleanup.drop_server_copy(str(path), root=self.root))
            self.assertTrue(path.exists())
        self.assertFalse(cleanup.drop_server_copy(None, root=self.root))
        self.assertFalse(cleanup.drop_server_copy(str(self.root / "123456:ABC/videos/missing.mp4"), root=self.root))


class OnePass(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="hk-root-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        old = self.root / "tok" / "documents" / "old.bin"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"x" * 2_000_000)
        os.utime(old, (0, 0))
        self.old = old
        for name, value in (("BOT_API_DATA_DIR", str(self.root)), ("BOT_API_CLEANUP", True), ("BOT_API_FILE_MAX_AGE_MINUTES", 60)):
            self.addCleanup(setattr, config, name, getattr(config, name))
            setattr(config, name, value)

    def test_one_pass_does_everything_and_reports_it(self):
        result = housekeeping.run_once()
        self.assertEqual((result["server_files"], result["server_bytes"]), (1, 2_000_000))
        self.assertIn("removed 1 old file(s) (2 MB)", housekeeping.summary(result))

    def test_the_cleanup_can_be_switched_off(self):
        config.BOT_API_CLEANUP = False
        housekeeping.run_once()
        self.assertTrue(self.old.exists())

    def test_one_failing_step_does_not_stop_the_others(self):
        with mock.patch.object(housekeeping, "kill_orphans", side_effect=RuntimeError("boom")), \
                mock.patch.object(housekeeping.reaper, "sweep", side_effect=RuntimeError("boom")):
            result = housekeeping.run_once()
        self.assertEqual(result["server_files"], 1)

    def test_a_quiet_pass_says_nothing(self):
        self.assertEqual(housekeeping.summary({"orphans": 0, "zombies_reaped": 0, "zombies": 0, "server_files": 0, "server_bytes": 0}), "")

    def test_zombies_nobody_can_collect_are_called_out(self):
        text = housekeeping.summary({"orphans": 2, "zombies_reaped": 0, "zombies": 3, "server_files": 0, "server_bytes": 0})
        self.assertIn("killed 2 orphaned", text)
        self.assertIn("3 zombie(s) not ours", text)
        self.assertIn("init: true", text)


class LoopAndWiring(unittest.IsolatedAsyncioTestCase):
    async def test_the_loop_runs_passes_and_survives_a_crash(self):
        self.addCleanup(setattr, config, "HOUSEKEEPING_INTERVAL_SECONDS", config.HOUSEKEEPING_INTERVAL_SECONDS)
        config.HOUSEKEEPING_INTERVAL_SECONDS = 0
        calls, extra_calls = [], []

        def run_once():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("first pass explodes")
            return {"orphans": 0, "zombies_reaped": 0, "zombies": 0, "server_files": 0, "server_bytes": 0}

        with mock.patch.object(housekeeping, "run_once", run_once):
            task = asyncio.create_task(housekeeping.loop(extra=lambda: extra_calls.append(1)))
            for _ in range(100):
                if len(calls) >= 3:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
        self.assertGreaterEqual(len(calls), 3)
        self.assertGreaterEqual(len(extra_calls), 2)           # the toolbox upload expiry rides along

    async def test_startup_launches_the_housekeeping_loop(self):
        started = []

        def capture(coro, **kw):
            started.append(getattr(coro, "cr_code", None) and coro.cr_code.co_name)
            coro.close()
            return mock.Mock()

        patches = [mock.patch.object(main, "sweep_orphaned_workspaces"), mock.patch.object(main, "JobManager"),
                   mock.patch.object(main.asyncio, "create_task", capture)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(setattr, main, "job_manager", main.job_manager)
        await main.post_init(mock.Mock())
        self.assertIn("loop", started)                            # housekeeping.loop
        self.assertIn("_sweep_stale_link_state_loop", started)

    def test_the_compose_file_has_a_real_init_and_a_writable_bot_api_volume(self):
        compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
        bot_part = compose[compose.index("  bot:"):]
        self.assertIn("init: true", bot_part)
        self.assertNotIn("telegram-bot-api:ro", compose)


if __name__ == "__main__":
    unittest.main()
