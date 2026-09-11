"""Observable job lifecycle, ownership, busy and stale-publication regressions.

Real processes exercise cancellation; no COLMAP/Brush or GPU is needed here.
The real training smoke is run centrally against the actual pipeline.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from services.reveng import gaussian_jobs as jobs


_VIEW_CAMERA = {"id": "/old/batch_0001", "eye": [3, -2, 5], "forward": [0, 1, 0],
                "up": [-1, 0, 0], "focus_distance": 6,
                "intrinsics": [640, 620, 390, 280, 800, 600]}


@unittest.skipUnless(sys.platform == "linux", "Linux process identity and process-group lifecycle")
class GaussianJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"CHUDWARE_REVENG_DIR": str(self.base),
                                           "CHUDWARE_REVENG_RUNS_DIR": str(self.base / "runs")})
        self.env.start()
        self.root = self.base / "re_test"
        self.root.mkdir()
        jobs._atomic(self.root / "session.json", {"session_id": "re_test", "counts": {"images": 17},
                                                 "artifacts": {}, "run": None})

    def tearDown(self):
        jobs.shutdown()
        self.env.stop()
        self.temp.cleanup()

    def wait_status(self, expected, timeout=8):
        deadline = time.monotonic() + timeout
        rec = {}
        while time.monotonic() < deadline:
            rec = jobs.status("re_test")
            if rec.get("status") in expected:
                return rec
            time.sleep(0.03)
        self.fail(f"Did not reach {expected}: {rec}")

    def record(self, **changes):
        rec = dict(ok=True, session_id="re_test", run_id="old", status="training",
                   process=jobs._identity(os.getpid()), owner=jobs._identity(os.getpid()),
                   artifact_revision=7, splat_path="old-preview.ply", frames=8,
                   view_camera=_VIEW_CAMERA)
        rec.update(changes)
        jobs._atomic(self.root / "splat_job.json", rec)
        return rec

    def wait_json(self, path, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = jobs._read(path)
            if value:
                return value
            time.sleep(0.03)
        self.fail(f"No JSON arrived at {path}")

    def assert_stopped(self, identities, timeout=5):
        deadline = time.monotonic() + timeout
        while any(jobs._alive(value) for value in identities) and time.monotonic() < deadline:
            time.sleep(0.03)
        self.assertFalse([value for value in identities if jobs._alive(value)])

    def trainer_fixture(self):
        """Only SfM is deterministic: supervisor, pipeline, adapter and trainer are real processes."""
        support = self.base / "support"
        support.mkdir()
        (support / "sitecustomize.py").write_text(textwrap.dedent("""
            import sys, types, shutil
            from pathlib import Path
            sfm = types.ModuleType("services.vision.colmap_pipeline")
            class Reconstruction:
                def __init__(self, path):
                    self.images = {i: types.SimpleNamespace(name=p.name) for i, p in
                                   enumerate(sorted((Path(path).parent / "images").glob("*")))}
                def num_points3D(self): return 10
                def num_reg_images(self): return len(self.images)
                def reg_image_ids(self): return list(self.images)
            def undistort_images(output_path, input_path, image_path, **kwargs):
                root = Path(output_path)
                shutil.copytree(image_path, root / "images")
                shutil.copytree(input_path, root / "sparse")
            def run_sparse(image_dir, session_dir, **kwargs):
                sparse = Path(session_dir) / "source_sparse"
                sparse.mkdir()
                for name in ("cameras", "images", "points3D"):
                    (sparse / (name + ".txt")).write_text("# controlled SfM fixture")
                return {"ok": True, "registered": len(list(Path(image_dir).iterdir())),
                        "sparse_dir": str(sparse)}
            sfm.run_sparse = run_sparse
            sfm._import_pycolmap = lambda: types.SimpleNamespace(
                Reconstruction=Reconstruction, undistort_images=undistort_images)
            sys.modules[sfm.__name__] = sfm
        """))
        marker = self.base / "trainer.json"
        trainer = support / "trainer"
        trainer.write_text(f"#!{sys.executable}\n" + textwrap.dedent("""
            import json, os, signal, subprocess, sys, time
            from pathlib import Path
            from services.reveng.gaussian_jobs import _identity
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            child = subprocess.Popen([sys.executable, "-c",
                "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"])
            exports = Path(sys.argv[sys.argv.index("--export-path") + 1])
            names = ["x", "y", "z", "opacity", "scale_0", "scale_1", "scale_2",
                     "rot_0", "rot_1", "rot_2", "rot_3", "f_dc_0", "f_dc_1", "f_dc_2"]
            (exports / "export_1.ply").write_text(
                "ply\\nformat ascii 1.0\\nelement vertex 1\\n" +
                "".join("property float " + name + "\\n" for name in names) +
                "end_header\\n0 0 0 1 -2 -2 -2 1 0 0 0 0 0 0\\n")
            Path(os.environ["TRAINER_MARKER"]).write_text(json.dumps({
                "trainer": _identity(os.getpid()), "descendant": _identity(child.pid),
                "max_splats": int(sys.argv[sys.argv.index("--max-splats") + 1])}))
            time.sleep(60)
        """))
        trainer.chmod(0o755)
        images = self.root / "images"
        images.mkdir()
        for index in range(6):
            (images / f"{index}.png").write_bytes(b"controlled captured image")
        environment = patch.dict(os.environ, {
            "BRUSH_BIN": str(trainer), "TRAINER_MARKER": str(marker),
            "PYTHONPATH": os.pathsep.join((str(support), str(Path(__file__).resolve().parents[2]),
                                         os.environ.get("PYTHONPATH", ""))),
        })
        environment.start()
        self.addCleanup(environment.stop)
        return marker

    def cleanup_processes(self, identities):
        # Clean up even on a regression, but never signal a reused PID.
        for identity in identities:
            if jobs._alive(identity):
                try:
                    os.kill(identity["pid"], 9)
                except ProcessLookupError:
                    pass

    def test_cancel_composed_pipeline_brush_tree_preserves_preview(self):
        marker = self.trainer_fixture()
        started = jobs.start("re_test", live=True)
        self.assertTrue(started["ok"], started)
        trainer = self.wait_json(marker)
        identities = [trainer["trainer"], trainer["descendant"]]
        try:
            deadline = time.monotonic() + 8
            rec = {}
            while time.monotonic() < deadline:
                rec = jobs.status("re_test")
                if rec.get("splat_path"):
                    break
                time.sleep(0.03)
            self.assertTrue(rec.get("splat_path"), rec)
            preview = Path(rec["splat_path"]).read_bytes()
            cancelled = jobs.cancel("re_test")
            self.assertEqual(cancelled["status"], "cancelled")
            self.assert_stopped(identities)
            self.assertEqual(Path(cancelled["splat_path"]).read_bytes(), preview)
            self.assertEqual(cancelled["max_splats"], 2_000_000)
            self.assertEqual(trainer["max_splats"], 2_000_000)
            self.assertIsNone(jobs._publish(self.root, started["run_id"],
                                           {"splat_path": rec["splat_path"], "iteration": 2000}, True))
            self.assertEqual(jobs.status("re_test")["artifact_revision"], cancelled["artifact_revision"])
        finally:
            self.cleanup_processes(identities)

    def test_owner_death_stops_composed_pipeline_brush_tree(self):
        marker = self.trainer_fixture()
        owner = subprocess.Popen([sys.executable, "-c",
            "import time;from services.reveng.gaussian_jobs import start;"
            "assert start('re_test',live=True)['ok'];time.sleep(60)"])
        identities = []
        try:
            trainer = self.wait_json(marker)
            rec = jobs._read(self.root / "splat_job.json")
            identities = [rec["process"], rec["pipeline_process"],
                          trainer["trainer"], trainer["descendant"]]
            owner.kill()
            owner.wait(timeout=3)
            self.assert_stopped(identities)
            self.assertEqual(jobs.status("re_test")["status"], "interrupted")
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.wait(timeout=3)
            self.cleanup_processes(identities)

    def test_standalone_adapter_cancels_its_tree_when_caller_is_group_leader(self):
        marker = self.trainer_fixture()
        result_path = self.base / "standalone.json"
        code = textwrap.dedent("""
            import json, sys
            from pathlib import Path
            from services.vision import colmap_pipeline as sfm
            from services.vision.dense_backends import run_brush
            root = Path(sys.argv[1])
            sparse = sfm.run_sparse(str(root / "images"), str(root))
            dataset = root / "dataset"
            sfm._import_pycolmap().undistort_images(
                str(dataset), sparse["sparse_dir"], str(root / "images"))
            result = run_brush(str(dataset / "sparse"), str(root), image_dir=str(dataset),
                               cancel=lambda: Path(sys.argv[2]).exists())
            Path(sys.argv[3]).write_text(json.dumps(result))
        """)
        process = subprocess.Popen([sys.executable, "-c", code, str(self.root),
                                    str(marker), str(result_path)], start_new_session=True)
        identities = []
        try:
            trainer = self.wait_json(marker)
            identities = [trainer["trainer"], trainer["descendant"]]
            process.wait(timeout=10)
            self.assertEqual(self.wait_json(result_path)["reason"], "cancelled")
            self.assert_stopped(identities)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            self.cleanup_processes(identities)

    def test_worker_sigterm_while_holding_job_lock_exits_and_runs_other_teardown(self):
        marker = self.base / "held.json"
        teardown = self.base / "teardown"
        code = textwrap.dedent("""
            import json, sys, time
            from pathlib import Path
            from services import worker_main as worker
            from services.reveng import gaussian_jobs as jobs
            root, marker, teardown = map(Path, sys.argv[1:])
            worker.paths.RUNTIME = root / "runtime"
            worker._warm_prompt_cache = lambda: None
            worker._warm_chudcode_service = lambda: None
            worker._shutdown_chudcode = lambda: (teardown / "chudcode").touch()
            worker._drain_box_mirror_on_exit = lambda: (teardown / "box").touch()
            def hold(params, emit):
                assert jobs.start("re_test", live=True)["ok"]
                with jobs._locked(root):
                    marker.write_text(json.dumps(jobs._read(root / "splat_job.json")))
                    time.sleep(60)
            worker._METHODS["hold"] = hold
            worker.serve()
        """)
        teardown.mkdir()
        process = subprocess.Popen([sys.executable, "-c", code, str(self.root),
                                    str(marker), str(teardown)], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        identity = {}
        try:
            process.stdin.write('{"id": 1, "method": "hold"}\n')
            process.stdin.flush()
            identity = self.wait_json(marker, timeout=20)["process"]
            process.terminate()
            self.assertEqual(process.wait(timeout=8), 128 + 15)
            self.assert_stopped([identity])
            self.assertTrue((teardown / "chudcode").is_file())
            self.assertTrue((teardown / "box").is_file())
            self.assertEqual(jobs.status("re_test")["status"], "interrupted")
        finally:
            process.stdin.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            self.cleanup_processes([identity])

    def test_worker_gaussian_shutdown_error_still_closes_other_services(self):
        teardown = self.base / "error_teardown"
        teardown.mkdir()
        code = textwrap.dedent("""
            import sys
            from pathlib import Path
            from services import worker_main as worker
            from services.reveng import gaussian_jobs as jobs
            root = Path(sys.argv[1])
            worker.paths.RUNTIME = root / "runtime"
            worker._warm_prompt_cache = lambda: None
            worker._warm_chudcode_service = lambda: None
            worker._shutdown_chudcode = lambda: (root / "chudcode").touch()
            worker._drain_box_mirror_on_exit = lambda: (root / "box").touch()
            def fail():
                raise OSError("controlled cleanup failure")
            jobs.shutdown = fail
            worker.serve()
        """)
        process = subprocess.Popen([sys.executable, "-c", code, str(teardown)],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, text=True)
        try:
            process.communicate('{"id": 1, "method": "shutdown"}\n', timeout=20)
            self.assertEqual(process.returncode, 0)
            self.assertTrue((teardown / "chudcode").is_file())
            self.assertTrue((teardown / "box").is_file())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)

    def test_live_wait_finish_fails_without_frames_and_restart_works(self):
        first = jobs.start("re_test", live=True)
        self.assertTrue(first["ok"], first)
        self.assertEqual(jobs.start("re_test", live=True)["reason"], "splat_already_running")
        jobs.finish("re_test")
        failed = self.wait_status({"failed"})
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["reason"], "insufficient_frames")
        second = jobs.start("re_test", live=True)
        self.assertTrue(second["ok"], second)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(jobs.cancel("re_test")["status"], "cancelled")
        self.assertEqual(jobs.status("re_test")["status"], "cancelled")

    def test_mission_and_splat_reservations_exclude_each_other(self):
        from services.mcp import reveng_common as common
        self.assertIsNone(common._reserve_mission("re_test", "mission"))
        try:
            self.assertEqual(jobs.start("re_test", live=True)["reason"], "mission_already_running")
        finally:
            common._release_mission("re_test", "mission")
        started = jobs.start("re_test", live=True)
        self.assertTrue(started["ok"], started)
        self.assertEqual(common._reserve_mission("re_test", "other"), started["run_id"])
        jobs.cancel("re_test")
        self.assertIsNone(common._reserve_mission("re_test", "other"))
        common._release_mission("re_test", "other")

    def test_dead_or_reused_process_is_interrupted_not_signalled(self):
        identity = jobs._identity(os.getpid())
        identity["start"] = "not-this-process"
        self.record(process=identity, pgid=os.getpgrp())
        rec = jobs.status("re_test")
        self.assertEqual(rec["status"], "interrupted")
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["splat_path"], "old-preview.ply")
        self.assertEqual(rec["view_camera"], _VIEW_CAMERA)
        self.assertEqual(jobs.cancel("re_test")["status"], "interrupted")

    def test_cancel_kills_verified_group_including_descendants(self):
        identity_file = self.base / "descendant.json"
        code = (
            "import json,subprocess,sys,time; "
            "from pathlib import Path; "
            "from services.reveng.gaussian_jobs import _identity; "
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
            "Path(sys.argv[1]).write_text(json.dumps(_identity(p.pid)));time.sleep(60)"
        )
        process = subprocess.Popen([sys.executable, "-c", code, str(identity_file)],
                                   start_new_session=True)
        jobs._CHILDREN["old"] = (self.root, process)
        try:
            deadline = time.monotonic() + 5
            while not identity_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            descendant = json.loads(identity_file.read_text())
            self.record(process=jobs._identity(process.pid), pgid=process.pid)
            self.assertEqual(jobs.cancel("re_test")["status"], "cancelled")
            process.wait(timeout=2)
            deadline = time.monotonic() + 2
            while jobs._alive(descendant) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(jobs._alive(descendant))
            self.assertEqual(jobs.status("re_test")["splat_path"], "old-preview.ply")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, 9)
                process.wait(timeout=2)

    def test_cancel_and_new_run_fence_old_progress_and_terminal_result(self):
        self.record(status="cancelled")
        preview = self.base / "late.ply"
        preview.write_bytes(b"late worker output")
        self.assertIsNone(jobs._publish(self.root, "old", {"splat_path": str(preview)}, False))
        self.assertIsNone(jobs._update(self.root, "old", status="done", ok=True))
        self.record(run_id="new")
        self.assertIsNone(jobs._publish(self.root, "old", {"splat_path": str(preview)}, False))
        rec = jobs.status("re_test")
        self.assertEqual(rec["run_id"], "new")
        self.assertEqual(rec["artifact_revision"], 7)
        self.assertEqual(rec["splat_path"], "old-preview.ply")
        self.assertEqual(rec["view_camera"], _VIEW_CAMERA)

    def test_camera_changes_only_with_new_artifact_and_survives_failed_refresh(self):
        self.record()
        camera = {**_VIEW_CAMERA, "id": "/new/batch_0001", "eye": [1, 2, 3]}
        early = jobs._publish(self.root, "old", {"stage": "sparse", "view_camera": camera}, False)
        self.assertEqual(early["view_camera"], _VIEW_CAMERA)
        missing = jobs._publish(self.root, "old",
                                {"splat_path": str(self.base / "missing.ply"), "view_camera": camera}, False)
        self.assertEqual(missing["view_camera"], _VIEW_CAMERA)
        self.assertEqual(missing["artifact_revision"], 7)
        preview = self.base / "preview.ply"
        properties = ["x", "y", "z", "opacity", "scale_0", "scale_1", "scale_2",
                      "rot_0", "rot_1", "rot_2", "rot_3", "f_dc_0", "f_dc_1", "f_dc_2"]
        preview.write_text("ply\nformat ascii 1.0\nelement vertex 1\n" +
                           "".join(f"property float {name}\n" for name in properties) +
                           "end_header\n0 0 0 1 -2 -2 -2 1 0 0 0 0 0 0\n")
        published = jobs._publish(self.root, "old",
                                  {"splat_path": str(preview), "view_camera": camera}, False)
        self.assertEqual(published["view_camera"], camera)
        self.assertEqual(published["artifact_revision"], 8)
        unchanged = jobs._publish(self.root, "old",
                                  {"splat_path": str(preview), "view_camera": _VIEW_CAMERA}, False)
        self.assertEqual(unchanged["view_camera"], camera)
        self.assertEqual(unchanged["artifact_revision"], 8)
        final = self.base / "final.ply"
        final.write_bytes(preview.read_bytes())
        published = jobs._publish(self.root, "old",
                                  {"splat_path": str(final), "view_camera": camera}, False)
        self.assertEqual(published["view_camera"], camera)
        self.assertEqual(published["artifact_revision"], 9)
        jobs._update(self.root, "old", status="failed", ok=False)
        started = jobs.start("re_test", live=True)
        self.assertTrue(started["ok"], started)
        self.assertEqual(started["view_camera"], camera)
        self.assertEqual(started["splat_path"], str(final))
        cancelled = jobs.cancel("re_test")
        self.assertEqual(cancelled["view_camera"], camera)
        self.assertEqual(jobs.status("re_test")["view_camera"], camera)
        self.record()
        uncalibrated = jobs._publish(self.root, "old", {"splat_path": str(preview)}, False)
        self.assertNotIn("view_camera", uncalibrated)

    def test_session_merge_never_rewrites_capture_manifest(self):
        from services.mcp import reveng_common as common
        original = (self.root / "session.json").read_bytes()
        preview = self.base / "preview.ply"
        preview.write_bytes(b"immutable preview")
        self.record(status="done", splat_path=str(preview))
        session = common._load_session("re_test")
        self.assertEqual(session["counts"]["images"], 17)
        self.assertEqual(session["artifacts"]["splat_path"], str(preview))
        self.assertEqual(session["gaussian"]["artifact_revision"], 7)
        self.assertEqual((self.root / "session.json").read_bytes(), original)
        session["counts"]["images"] = 18
        common._save_session(session)
        persisted = json.loads((self.root / "session.json").read_text())
        self.assertEqual(persisted["counts"]["images"], 18)
        self.assertNotIn("splat_path", persisted["artifacts"])
        self.assertEqual(common._load_session("re_test")["artifacts"]["splat_path"], str(preview))

    def test_gpu_slot_waiting_keeps_counting_new_capture_frames(self):
        import fcntl
        images = self.root / "images"
        images.mkdir()
        for index in range(6):
            (images / f"{index}.png").write_bytes(b"not consumed while GPU is locked")
        with (self.base / ".gaussian_gpu.lock").open("a") as gpu:
            fcntl.flock(gpu, fcntl.LOCK_EX)
            started = jobs.start("re_test", live=True)
            self.assertTrue(started["ok"], started)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                rec = jobs.status("re_test")
                if rec.get("frames") == 6 and rec.get("stage") == "waiting":
                    break
                time.sleep(0.03)
            self.assertEqual(rec["frames"], 6)
            for index in range(6, 9):
                (images / f"{index}.png").write_bytes(b"new accepted frame")
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                rec = jobs.status("re_test")
                if rec.get("frames") == 9:
                    break
                time.sleep(0.03)
            self.assertEqual(rec["frames"], 9)
            self.assertEqual(rec["status"], "waiting")
            self.assertEqual(jobs.cancel("re_test")["status"], "cancelled")

    def test_hard_owner_death_stops_supervisor_without_status_polling(self):
        marker = self.base / "started.json"
        code = (
            "import json,os,sys;from pathlib import Path; "
            "from services.reveng.gaussian_jobs import start; "
            "r=start('re_test',live=True); "
            "Path(sys.argv[1]).write_text(json.dumps(r));os._exit(0)"
        )
        owner = subprocess.Popen([sys.executable, "-c", code, str(marker)])
        owner.wait(timeout=8)
        self.assertTrue(json.loads(marker.read_text())["ok"])
        deadline = time.monotonic() + 8
        rec = {}
        while time.monotonic() < deadline:
            rec = jobs._read(self.root / "splat_job.json")
            if rec.get("status") == "interrupted" and not jobs._alive(rec.get("process") or {}):
                break
            time.sleep(0.03)
        self.assertEqual(rec["status"], "interrupted")
        self.assertFalse(jobs._alive(rec.get("process") or {}))

    def test_repeat_crop_and_reset_use_original_and_busy_crop_refuses(self):
        from services.vision.gaussian_pipeline import inspect_splat
        source = self.base / "source.ply"
        properties = ["x", "y", "z", "opacity", "scale_0", "scale_1", "scale_2",
                      "rot_0", "rot_1", "rot_2", "rot_3", "f_dc_0", "f_dc_1", "f_dc_2", "custom"]
        source.write_text("ply\nformat ascii 1.0\nelement vertex 2\n" +
                          "".join(f"property float {name}\n" for name in properties) +
                          "end_header\n0 0 0 1 -2 -2 -2 1 0 0 0 0 0 0 11\n"
                          "10 0 0 1 -2 -2 -2 1 0 0 0 0 0 0 22\n")
        self.record(splat_path=str(source), source_splat_path=str(source))
        self.assertEqual(jobs.crop("re_test", [-1, -1, -1, 1, 1, 1])["reason"], "splat_busy")
        self.record(status="done", splat_path=str(source), source_splat_path=str(source))
        first = jobs.crop("re_test", [-1, -1, -1, 1, 1, 1])
        self.assertTrue(first["ok"], first)
        self.assertEqual(inspect_splat(first["splat_path"])["count"], 1)
        self.assertEqual(first["view_camera"], _VIEW_CAMERA)
        second = jobs.crop("re_test", [9, -1, -1, 11, 1, 1])
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["bounds"][0], 10)
        self.assertIn(b" 22", Path(second["splat_path"]).read_bytes())
        reset = jobs.crop("re_test", None)
        self.assertEqual(reset["splat_path"], str(source))
        self.assertEqual(reset["artifact_revision"], 10)
        self.assertEqual(inspect_splat(reset["splat_path"])["count"], 2)
        self.assertEqual(reset["view_camera"], _VIEW_CAMERA)
        invalid = jobs.crop("re_test", [0, 0, 0, float("nan"), 1, 1])
        self.assertFalse(invalid["ok"])
        self.assertEqual(jobs.status("re_test")["artifact_revision"], 10)


if __name__ == "__main__":
    unittest.main()
