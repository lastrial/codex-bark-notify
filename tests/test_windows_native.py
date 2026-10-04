"""Isolated native Windows tests. Uses synthetic keys and never calls Bark."""
import contextlib
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "bark-task-notify" / "scripts"))
if os.name == "nt":
    import windows_native as native
    import manage
    import manage_questions
    import notify
    import question
    import read_thread_title as probe


@unittest.skipUnless(os.name == "nt", "native Windows tests")
class WindowsNativeTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.mkdtemp(prefix="bark-windows-test-")
        self.private = os.path.join(self.scratch, "private")
        native.mkdir(self.private)

    def tearDown(self):
        shutil.rmtree(self.scratch)

    def file(self, name="file", data=b"synthetic"):
        path = os.path.join(self.private, name)
        native.create_file(path, data)
        return path

    def process(self, code):
        return native.bounded_popen([sys.executable, "-B", "-c", code])

    def test_imports_and_self_checks(self):
        for name in ("notify.py", "question.py"):
            proc = subprocess.run([sys.executable, "-B", str(Path(notify.__file__).parent / name), "--self-check"],
                                  capture_output=True, timeout=3)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), b'{"result":"self_check_ok"}')

    def test_private_creation_read_and_replace_preserves_acl(self):
        path = self.file(data=b"old")
        raw, info = native.read_regular(path, 100, True)
        native.replace_checked(path, raw, info, b"new")
        new, latest = native.read_regular(path, 100, True)
        self.assertEqual(new, b"new")
        self.assertEqual(info.security, latest.security)
        native.private_directory(self.private)

    def test_replacement_rechecks_identity(self):
        path = self.file(data=b"old")
        raw, info = native.read_regular(path, 100, True)
        os.unlink(path)
        native.create_file(path, b"old")
        with self.assertRaises(OSError):
            native.replace_checked(path, raw, info, b"new")

    def test_hardlinks_and_streams_rejected(self):
        path = self.file()
        os.link(path, os.path.join(self.private, "link"))
        with self.assertRaises(OSError):
            native.read_regular(path, 100, True)
        for path in (path + ":secret", r"\\server\share\key", r"\\?\C:\key", "C:key"):
            with self.assertRaises(OSError):
                native.no_links(path)

    def test_junction_ancestor_rejected(self):
        junction = os.path.join(self.scratch, "junction")
        result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", junction, self.private],
                                capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        path = self.file()
        try:
            with self.assertRaises(OSError):
                native.read_regular(os.path.join(junction, os.path.basename(path)), 100, True)
        finally:
            os.rmdir(junction)

    def set_dacl(self, path, suffix):
        handle, _ = native.open_handle(path, write=True, write_dac=True)
        sd = native.descriptor("O:" + native.SID + suffix)
        try:
            native.checked(native.SetSecurity(handle, 4 | 0x80000000, sd))
        finally:
            native.LocalFree(sd)
            native.CloseHandle(handle)

    def test_unexpected_allow_and_null_dacl_rejected(self):
        for name, dacl in (("broad", "D:P(A;;FA;;;" + native.SID + ")(A;;FR;;;WD)"),
                           ("null", "D:NO_ACCESS_CONTROL")):
            path = self.file(name)
            self.set_dacl(path, dacl)
            with self.assertRaises(OSError):
                native.read_regular(path, 100, True)

    def test_safely_inherited_private_acl_accepted(self):
        path = os.path.join(self.private, "inherited")
        Path(path).write_bytes(b"synthetic")  # inherits only the private parent's SID
        self.assertEqual(native.read_regular(path, 100, True)[0], b"synthetic")

    def test_shared_workers_exclusive_manager_and_close_release(self):
        path = self.file("delivery.lock", b"")
        first, _ = native.open_fd(path, write=True)
        second, _ = native.open_fd(path, write=True)
        third, _ = native.open_fd(path, write=True)
        try:
            self.assertTrue(native.lock_until(first, False, time.monotonic() + 0.1))
            self.assertTrue(native.lock_until(second, False, time.monotonic() + 0.1))
            self.assertFalse(native.lock_until(third, True, time.monotonic() + 0.05))
            os.close(first)
            first = None
            self.assertFalse(native.lock_until(third, True, time.monotonic() + 0.05))
            os.close(second)
            second = None
            self.assertTrue(native.lock_until(third, True, time.monotonic() + 0.1))
            first, _ = native.open_fd(path, write=True)
            self.assertFalse(native.lock_until(first, False, time.monotonic() + 0.05))
        finally:
            for fd in (first, second, third):
                if fd is not None:
                    os.close(fd)

    def test_pipe_deadline_and_eof(self):
        proc = self.process("import time; time.sleep(20)")
        try:
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                native.pipe_read(proc.stdout, 100, start + 0.08)
            self.assertLess(time.monotonic() - start, 0.5)
        finally:
            native.stop_child(proc)
        proc = self.process("print('ready', flush=True)")
        try:
            self.assertEqual(native.pipe_read(proc.stdout, 100, time.monotonic() + 3).strip(), b"ready")
            self.assertEqual(native.pipe_read(proc.stdout, 100, time.monotonic() + 3), b"")
        finally:
            native.stop_child(proc)

    def test_job_kills_descendant_after_leader_exits(self):
        code = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); print(p.pid,flush=True)"
        proc = self.process(code)
        descendant = None
        try:
            pid = int(native.pipe_read(proc.stdout, 100, time.monotonic() + 3).strip())
            OpenProcess = native.api(native.k, "OpenProcess", native.P, [native.DWORD, ctypes.c_int, native.DWORD])
            descendant = native.checked(OpenProcess(0x100000, False, pid))
            proc.wait(timeout=3)
            native.stop_child(proc)
            Wait = native.api(native.k, "WaitForSingleObject", native.DWORD, [native.P, native.DWORD])
            self.assertEqual(Wait(descendant, 1000), 0)
        finally:
            native.stop_child(proc)
            if descendant:
                native.CloseHandle(descendant)

    def test_assignment_failure_never_executes_payload(self):
        marker = os.path.join(self.private, "must-not-exist")
        real_api = native.api
        def fail_assignment(dll, name, restype, args):
            if name == "AssignProcessToJobObject":
                return lambda *_: False
            return real_api(dll, name, restype, args)
        with mock.patch.object(native, "api", side_effect=fail_assignment):
            with self.assertRaises(OSError):
                self.process("from pathlib import Path; Path(" + repr(marker) + ").write_text('executed')")
        self.assertFalse(os.path.exists(marker))

    def test_forward_argv_stdin_stdout_stderr_and_exit(self):
        script = self.file("callback & '中文.py", b"import sys,json\nprint(json.dumps(sys.argv[1:],ensure_ascii=True))\nprint(sys.stdin.read())\nprint('stderr',file=sys.stderr)\nsys.exit(37)\n")
        raw = '{"raw":"a & b \\u4e2d"}'
        proc = subprocess.run([sys.executable, "-B", notify.__file__, "--runtime", self.private,
                               "--codex", sys.executable, "--forward", sys.executable, script, "first '&", raw],
                              input=b"stdin retained", capture_output=True, timeout=3)
        self.assertEqual(proc.returncode, 37)
        self.assertEqual(json.loads(proc.stdout.splitlines()[0]), ["first '&", raw])
        self.assertIn(b"stdin retained", proc.stdout)
        self.assertEqual(proc.stderr.strip(), b"stderr")

    def test_network_child_fixed_result_parsing_and_timeout(self):
        real_spawn = native.bounded_popen
        for output, expected in (({"state": "sent", "reason": "accepted"}, "sent"),
                                  ({"state": "sent", "reason": "secret"}, "unknown"),
                                  ({"extra": "untrusted"}, "unknown")):
            code = "import sys;sys.stdin.buffer.read();print(" + repr(json.dumps(output)) + ")"
            with mock.patch.object(native, "bounded_popen", side_effect=lambda _argv: real_spawn([sys.executable, "-c", code])):
                result = notify.network_child("unused", {"title": "", "body": ""}, time.monotonic() + 4)
            self.assertEqual(result["state"], expected)
        with mock.patch.object(native, "bounded_popen", side_effect=lambda _argv: real_spawn([sys.executable, "-c", "import time;time.sleep(30)"])):
            result = notify.network_child("unused", {"title": "", "body": ""}, time.monotonic() + 2)
            self.assertEqual(result, {"state": "unknown", "reason": "deadline"})

    def test_manager_enable_status_questions_disable_preserves_bytes(self):
        config = self.file("config.toml", ("notify = " + json.dumps([sys.executable, "-c", "pass"]) + "\r\nmodel = 'test'\r\n").encode())
        before, info = native.read_regular(config, 4096)
        key = self.file("synthetic.key")
        runtime = os.path.join(self.private, "runtime")
        hooks = os.path.join(self.private, "hooks.json")
        self.assertEqual(manage.manage("enable", config, runtime, sys.executable, key_file=key)["result"], "enabled")
        self.assertEqual(manage.manage("status", config, runtime, sys.executable)["result"], "enabled")
        self.assertTrue(manage.code_intact(runtime, manage.installed(runtime, config)))
        self.assertTrue(os.path.exists(os.path.join(runtime, "windows_native.py")))
        self.assertEqual(manage_questions.manage("enable", hooks, runtime, sys.executable)["result"], "configured")
        entry = json.loads(native.read_regular(hooks, 8192)[0])["hooks"]["PostToolUse"][0]
        self.assertIn("commandWindows", entry["hooks"][0])
        self.assertEqual(manage_questions.manage("disable", hooks, runtime, sys.executable)["result"], "disabled")
        # Recovery must not depend on parsing/reading damaged activation contents.
        activation = os.path.join(runtime, "activation.json")
        Path(activation).write_bytes(b"x" * (manage.MAX_CONFIG + 1))
        self.assertEqual(manage.manage("disable", config, runtime, sys.executable)["result"], "disabled")
        restored, latest = native.read_regular(config, 4096)
        self.assertEqual(restored, before)
        self.assertEqual(latest.security, info.security)

    def test_generated_powershell_hook_preserves_stdin_and_argv(self):
        runtime = os.path.join(self.private, "runtime '& 中文")
        native.mkdir(runtime)
        result = os.path.join(runtime, "result.json")
        code = "import sys,json;from pathlib import Path;Path(" + repr(result) + ").write_text(json.dumps([sys.argv[1:],sys.stdin.read()]))"
        native.create_file(os.path.join(runtime, "question.py"), code.encode())
        entry = manage_questions.owned_entry(runtime, "codex & 'fake", sys.executable)
        command = entry["hooks"][0]["commandWindows"]
        proc = subprocess.run(command.split(), input=b"hook input", capture_output=True, timeout=5, shell=False)
        self.assertEqual(proc.returncode, 0)
        args, data = json.loads(Path(result).read_text())
        self.assertEqual(args, ["--hook", "--runtime", runtime, "--codex", "codex & 'fake"])
        self.assertEqual(data, "hook input")


if __name__ == "__main__":
    unittest.main()
