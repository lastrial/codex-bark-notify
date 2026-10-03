"""Private notifier contract tests; all metadata and HTTP are fixtures."""
import concurrent.futures
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/bark-task-notify/scripts"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notifier = load("bark_task_notifier", SCRIPTS / "notify.py")
reader = load("bark_task_title_reader", SCRIPTS / "read_thread_title.py")
manager = load("bark_task_manager", SCRIPTS / "manage.py")
THREAD = "00000000-0000-4000-8000-000000000a10"
TURN = "00000000-0000-4000-8000-000000000b11"
HOST = "00000000-0000-4000-8000-000000000001"
RAW = json.dumps({"type": "agent-turn-complete", "thread-id": THREAD, "turn-id": TURN,
                  "last-assistant-message": "PRIVATE PROMPT"})
METADATA = {"source": "vscode", "threadSource": "agent_created_thread", "name": "  中文🚀\n任务\"\\\x7f  "}
ACCEPTED = {"state": "sent", "reason": "accepted"}


class TempRuntime(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.runtime = self.base / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.key = self.base / "key"
        self.key.write_text("PRIVATE-KEY")
        self.key.chmod(0o600)
        self.write_private("activation.json", {"enabled": True})
        self.write_private("config.json", {"host_id": HOST, "key_file": str(self.key)})

    def write_private(self, name, value):
        path = self.runtime / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def worker(self, turn=TURN):
        with mock.patch.dict(sys.modules, {"read_thread_title": reader}):
            return notifier.worker(str(self.runtime), "/fixture/codex", THREAD, turn)

    def rows(self):
        db = sqlite3.connect(str(self.runtime / "state.sqlite"))
        try:
            return db.execute("SELECT state,reason FROM events ORDER BY turn_id").fetchall()
        finally:
            db.close()


class ForwardingTests(unittest.TestCase):
    def command(self, code, raw):
        return [sys.executable, "-B", str(SCRIPTS / "notify.py"), "--runtime", "/missing",
                "--codex", "/missing", "--forward", sys.executable, "-c", code,
                "中文 ' $HOME $(touch unsafe); `echo bad`", raw]

    def test_raw_forward_once_and_exit_status_signal(self):
        for raw in (RAW, "{broken", "x" * (notifier.MAX_PAYLOAD_BYTES + 1)):
            result = subprocess.run(self.command("import sys,json;print(json.dumps(sys.argv[1:]))", raw),
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)[-1], raw)
            self.assertEqual(len(result.stdout.splitlines()), 1)
            self.assertEqual(result.stderr, b"")
        nonzero = subprocess.run(self.command("raise SystemExit(37)", RAW), check=False)
        killed = subprocess.run(self.command("import os,signal;os.kill(os.getpid(),signal.SIGTERM)", RAW), check=False)
        self.assertEqual(nonzero.returncode, 37)
        self.assertEqual(killed.returncode, -signal.SIGTERM)

    def test_all_preparation_errors_still_exec_original(self):
        argv = ["--runtime", "/runtime", "--codex", "/codex", "--forward", "/callback", "fixed", RAW]
        for error in (OSError("PRIVATE"), RuntimeError("PRIVATE"), KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(notifier, "launch_worker", side_effect=error), \
                    mock.patch("signal.signal"), \
                    mock.patch.object(notifier.os, "execv") as execute:
                notifier.forward(argv)
                execute.assert_called_once_with("/callback", ["/callback", "fixed", RAW])

    def test_callback_sigpipe_matches_direct_subprocess(self):
        callback = ["/bin/sh", "-c", "kill -s PIPE $$; exit 17"]
        direct = subprocess.run(callback, capture_output=True, check=False)
        wrapped = subprocess.run([sys.executable, "-B", str(SCRIPTS / "notify.py"),
                                  "--runtime", "/missing", "--codex", "/missing", "--forward"] + callback + ["{broken"],
                                 capture_output=True, check=False)
        self.assertEqual(direct.returncode, -signal.SIGPIPE)
        self.assertEqual(wrapped.returncode, direct.returncode)
        self.assertEqual((wrapped.stdout, wrapped.stderr), (b"", b""))

    def test_worker_arguments_are_detached_identity_only(self):
        with mock.patch("subprocess.Popen") as spawn:
            notifier.launch_worker("/runtime", "/codex", RAW)
            args, kwargs = spawn.call_args
            self.assertNotIn("PRIVATE", " ".join(args[0]))
            self.assertNotIn(RAW, args[0])
            self.assertEqual(args[0][-4:], ["--thread-id", THREAD, "--turn-id", TURN])
            self.assertTrue(kwargs["close_fds"] and kwargs["start_new_session"])
            for name in ("stdin", "stdout", "stderr"):
                self.assertEqual(kwargs[name], subprocess.DEVNULL)

    def test_invalid_events_skip_worker_but_missing_helper_forwards(self):
        with mock.patch("subprocess.Popen") as spawn:
            for value in ({}, {"type": "wrong"}, {"type": "agent-turn-complete", "thread-id": THREAD.upper(), "turn-id": TURN},
                          {"type": "agent-turn-complete", "thread-id": THREAD, "turn-id": "x"}):
                notifier.launch_worker("/runtime", "/codex", json.dumps(value))
            spawn.assert_not_called()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "notify.py"
            path.write_bytes((SCRIPTS / "notify.py").read_bytes())
            command = self.command("print('forwarded')", RAW)
            command[2] = str(path)
            result = subprocess.run(command, capture_output=True, check=False)
            self.assertEqual((result.returncode, result.stdout), (0, b"forwarded\n"))


class ClassificationTests(unittest.TestCase):
    def test_known_root_sources_and_agent_created_thread(self):
        for source in ("cli", "vscode", "exec", "appServer"):
            for origin in ("user", "agent_created_thread"):
                self.assertEqual(notifier.classify(dict(METADATA, source=source, threadSource=origin)), "eligible")

    def test_children_auxiliary_unknown_and_missing_title(self):
        self.assertEqual(notifier.classify(dict(METADATA, source={"subAgent": {"parentThreadId": THREAD}})), "child_skip")
        for source in ("system", "voice", "custom", None, {}, [], 4, "future"):
            self.assertEqual(notifier.classify(dict(METADATA, source=source)), "source_skip")
        for origin in ("system", "subagent", "voice", None, {}, "future"):
            self.assertEqual(notifier.classify(dict(METADATA, threadSource=origin)), "thread_source_skip")
        for name in (None, "", " \n\t", 9, "\ud800"):
            self.assertEqual(notifier.classify(dict(METADATA, name=name)), "title_unavailable")
        self.assertEqual(notifier.classify(dict(METADATA, name="🚀" * 1025)), "title_too_large")


class HttpTests(TempRuntime):
    def fake_send(self, status=200, raw=b'{"code":200}', error=None):
        response = mock.Mock(status=status)
        response.read.return_value = raw
        connection = mock.Mock()
        connection.getresponse.return_value = response
        if error:
            connection.request.side_effect = error
        with mock.patch("http.client.HTTPSConnection", return_value=connection) as create:
            result = notifier.http_send(str(self.key), {"title": "HOST", "body": METADATA["name"] + " 已完成"}, 1)
        return result, create, connection

    def test_fixed_https_json_preserves_exact_title_and_name(self):
        result, create, connection = self.fake_send()
        self.assertEqual(result, ACCEPTED)
        create.assert_called_once_with("api.day.app", 443, timeout=1)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("POST", "/push"))
        payload = json.loads(kwargs["body"])
        self.assertEqual(payload, {"device_key": "PRIVATE-KEY", "title": "HOST", "body": METADATA["name"] + " 已完成",
                                  "group": "bark-task-notify", "sound": "calypso", "level": "timeSensitive"})
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_response_classes_are_conservative_and_fixed(self):
        cases = [(200, b'{"code":200}', "sent", "accepted"), (400, b"bad", "rejected", "http_rejected"),
                 (429, b"bad", "rejected", "http_rejected"), (500, b'{"code":200}', "unknown", "http_unknown"),
                 (302, b'{"code":200}', "unknown", "http_unknown"), (200, b'{"code":401}', "rejected", "api_rejected"),
                 (200, b'{"code":true}', "unknown", "invalid_response"), (200, b'{"code":"200"}', "unknown", "invalid_response"),
                 (200, b"not json", "unknown", "invalid_response"), (200, b"[]", "unknown", "invalid_response"),
                 (200, b"x" * (notifier.MAX_HTTP_BYTES + 1), "unknown", "response_too_large")]
        for status, raw, state, reason in cases:
            with self.subTest(status=status, raw=raw[:30]):
                result, _create, _connection = self.fake_send(status, raw)
                self.assertEqual(result, {"state": state, "reason": reason})
        result, _create, connection = self.fake_send(error=socket.timeout("PRIVATE KEY PROMPT"))
        self.assertEqual(result, {"state": "unknown", "reason": "transport_error"})
        connection.close.assert_called_once()

    def test_key_permissions_links_size_and_controls_fail_before_http(self):
        for text in ("", "x" * 513, "bad\n", "bad\x7f", "bad\x00"):
            self.key.write_text(text)
            result, create, _connection = self.fake_send()
            self.assertEqual(result, {"state": "rejected", "reason": "key_invalid"})
            create.assert_not_called()
        self.key.write_bytes(b"\xff")
        self.assertEqual(self.fake_send()[0]["reason"], "key_invalid")
        self.key.write_text("PRIVATE-KEY")
        self.key.chmod(0o644)
        self.assertEqual(self.fake_send()[0]["reason"], "key_invalid")
        self.key.chmod(0o600)
        os.link(self.key, self.base / "hardlink")
        self.assertEqual(self.fake_send()[0]["reason"], "key_invalid")
        (self.base / "hardlink").unlink()
        original = self.base / "original-key"
        self.key.rename(original)
        self.key.symlink_to(original)
        self.assertEqual(self.fake_send()[0]["reason"], "key_invalid")

    def test_network_arguments_contain_only_paths_and_timeout(self):
        proc = mock.Mock(pid=12345, returncode=0)
        proc.communicate.return_value = (json.dumps(ACCEPTED).encode(), None)
        with mock.patch("subprocess.Popen", return_value=proc) as spawn, mock.patch.object(notifier, "stop_child"):
            result = notifier.network_child(str(self.key), {"title": "PRIVATE TITLE", "body": "PRIVATE BODY"}, time.monotonic() + 5)
        self.assertEqual(result, ACCEPTED)
        command = spawn.call_args[0][0]
        self.assertNotIn("PRIVATE", " ".join(command))
        self.assertIn(str(self.key), command)
        self.assertEqual(json.loads(proc.communicate.call_args.kwargs["input"]), {"title": "PRIVATE TITLE", "body": "PRIVATE BODY"})

    def test_dns_hang_is_killed_within_total_deadline(self):
        script = self.base / "blocked_dns.py"
        pid_path = self.base / "network.pid"
        script.write_text("import sys,socket,time,os\n" +
                          "sys.path.insert(0," + repr(str(SCRIPTS)) + ")\nimport notify\n" +
                          "open(" + repr(str(pid_path)) + ",'w').write(str(os.getpid()))\n" +
                          "socket.getaddrinfo=lambda *a,**k: time.sleep(30)\n" +
                          "raise SystemExit(notify.main())\n")
        start = time.monotonic()
        with mock.patch.object(notifier, "__file__", str(script)):
            result = notifier.network_child(str(self.key), {"title": "HOST", "body": "任务 已完成"}, start + 2.2)
        self.assertEqual(result, {"state": "unknown", "reason": "deadline"})
        self.assertLess(time.monotonic() - start, 2.4)
        self.assertTrue(pid_path.exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)


class WorkerTests(TempRuntime):
    def test_claim_precedes_metadata_and_exact_payload_uses_hostname(self):
        def metadata(*_args):
            self.assertEqual(self.rows(), [("processing", "claimed")])
            return METADATA
        with mock.patch.object(reader, "read_completion_metadata", side_effect=metadata) as read, \
                mock.patch.object(notifier, "network_child", return_value=ACCEPTED) as send, \
                mock.patch("socket.gethostname", return_value="Exact.Host"):
            self.assertEqual(self.worker(), ACCEPTED)
            self.assertEqual(self.worker()["reason"], "duplicate")
        read.assert_called_once()
        self.assertEqual(send.call_args[0][1], {"title": "Exact.Host", "body": METADATA["name"] + " 已完成"})
        self.assertEqual(self.rows(), [("sent", "accepted")])
        self.assertNotIn("PRIVATE", (self.runtime / "state.sqlite").read_bytes().decode("latin1"))
        self.assertEqual(stat.S_IMODE((self.runtime / "state.sqlite").stat().st_mode), 0o600)

    def test_concurrent_duplicate_claims_send_once(self):
        with mock.patch.object(reader, "read_completion_metadata", return_value=METADATA) as read, \
                mock.patch.object(notifier, "network_child", side_effect=lambda *_a: (time.sleep(0.15) or ACCEPTED)) as send:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _n: self.worker(), range(6)))
        self.assertEqual(sum(value["state"] == "sent" for value in results), 1)
        read.assert_called_once()
        send.assert_called_once()

    def test_unknown_rejected_skipped_and_crash_are_never_retried(self):
        for i, result in enumerate(({"state": "unknown", "reason": "transport_error"},
                                    {"state": "rejected", "reason": "http_rejected"})):
            turn = str(uuid.UUID(int=100 + i))
            with mock.patch.object(reader, "read_completion_metadata", return_value=METADATA) as read, \
                    mock.patch.object(notifier, "network_child", return_value=result) as send:
                self.assertEqual(self.worker(turn), result)
                self.assertEqual(self.worker(turn)["reason"], "duplicate")
            read.assert_called_once()
            send.assert_called_once()
        turn = str(uuid.UUID(int=999))
        with mock.patch.object(reader, "read_completion_metadata", side_effect=reader.ProbeError("PRIVATE RAW ERROR")), \
                mock.patch.object(notifier, "network_child") as send:
            self.assertEqual(self.worker(turn), {"state": "skipped", "reason": "metadata_error"})
            self.assertEqual(self.worker(turn)["reason"], "duplicate")
            send.assert_not_called()

    def test_stale_processing_becomes_unknown_without_flipping_active_row(self):
        db = notifier.open_state(str(self.runtime))
        notifier.claim(db, HOST, THREAD, TURN, time.time() - 100)
        fresh_turn = str(uuid.UUID(int=10))
        notifier.claim(db, HOST, THREAD, fresh_turn, time.time())
        db.close()
        with mock.patch.object(reader, "read_completion_metadata") as read, mock.patch.object(notifier, "network_child") as send:
            self.assertEqual(self.worker()["reason"], "duplicate")
        self.assertIn(("unknown", "stale_processing"), self.rows())
        self.assertIn(("processing", "claimed"), self.rows())
        read.assert_not_called()
        send.assert_not_called()

    def test_database_failure_before_claim_and_after_http_cannot_resend(self):
        with mock.patch.object(notifier, "open_state", side_effect=sqlite3.OperationalError("PRIVATE")), \
                mock.patch.object(reader, "read_completion_metadata") as read, mock.patch.object(notifier, "network_child") as send:
            self.assertEqual(self.worker()["state"], "unknown")
            read.assert_not_called()
            send.assert_not_called()
        with mock.patch.object(reader, "read_completion_metadata", return_value=METADATA), \
                mock.patch.object(notifier, "network_child", return_value=ACCEPTED) as send, \
                mock.patch.object(notifier, "finish", side_effect=sqlite3.OperationalError("PRIVATE")):
            self.assertEqual(self.worker(), {"state": "unknown", "reason": "state_error"})
            self.assertEqual(self.worker()["reason"], "duplicate")
            send.assert_called_once()
        self.assertEqual(self.rows(), [("processing", "claimed")])

    def test_disabled_metadata_failure_auxiliary_and_deadline_no_http(self):
        self.write_private("activation.json", {"enabled": False})
        with mock.patch.object(reader, "read_completion_metadata") as read:
            self.assertEqual(self.worker()["reason"], "disabled")
            read.assert_not_called()
        self.write_private("activation.json", {"enabled": True})
        with mock.patch.object(reader, "read_completion_metadata", return_value=dict(METADATA, source="system")), \
                mock.patch.object(notifier, "network_child") as send:
            self.assertEqual(self.worker()["reason"], "source_skip")
            send.assert_not_called()
        def delayed(*_args):
            time.sleep(0.08)
            return METADATA
        with mock.patch.object(notifier, "WORKER_BUDGET", 0.06), mock.patch.object(notifier, "CLEANUP_MARGIN", 0.01), \
                mock.patch.object(reader, "read_completion_metadata", side_effect=delayed), mock.patch("subprocess.Popen") as spawn:
            self.assertEqual(self.worker(str(uuid.UUID(int=20)))["reason"], "deadline")
            spawn.assert_not_called()

    def test_activation_recheck_after_metadata_prevents_send(self):
        def deactivate(*_args):
            self.write_private("activation.json", {"enabled": False})
            return METADATA
        with mock.patch.object(reader, "read_completion_metadata", side_effect=deactivate), \
                mock.patch.object(notifier, "network_child") as send:
            self.assertEqual(self.worker()["reason"], "disabled")
            send.assert_not_called()


class MetadataTests(TempRuntime):
    def make_codex(self, hang=False):
        path = self.base / "fake-codex"
        pid_path = self.base / "helper.pid"
        calls_path = self.base / "rpc.jsonl"
        path.write_text("#!" + sys.executable + "\nimport json,os,sys,time\n" +
                        "open(" + repr(str(pid_path)) + ",'w').write(str(os.getpid()))\n" +
                        ("time.sleep(30)\n" if hang else "") +
                        "first=json.loads(sys.stdin.readline())\n" +
                        "print(json.dumps({'id':first['id'],'result':{}}),flush=True)\n" +
                        "sys.stdin.readline()\nrequest=json.loads(sys.stdin.readline())\n" +
                        "open(" + repr(str(calls_path)) + ",'w').write(json.dumps(request))\n" +
                        "thread=" + repr(dict(METADATA, turns=[])) + "\nthread['id']=request['params']['threadId']\n" +
                        "print(json.dumps({'id':request['id'],'result':{'thread':thread}}),flush=True)\n" +
                        "time.sleep(30)\n")
        path.chmod(0o700)
        return path, pid_path, calls_path

    def test_one_read_returns_sources_and_exact_name_without_turns(self):
        codex, pid_path, calls_path = self.make_codex()
        result = reader.read_completion_metadata(str(codex), THREAD, 2)
        self.assertEqual(result, METADATA)
        request = json.loads(calls_path.read_text())
        self.assertEqual(request["method"], "thread/read")
        self.assertIs(request["params"]["includeTurns"], False)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)

    def test_worker_term_cleans_helper_process(self):
        codex, pid_path, _calls = self.make_codex(hang=True)
        proc = subprocess.Popen([sys.executable, "-B", str(SCRIPTS / "notify.py"), "--worker", "--runtime", str(self.runtime),
                                 "--codex", str(codex), "--thread-id", THREAD, "--turn-id", TURN],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            while not pid_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pid_path.exists())
            proc.terminate()
            output, error = proc.communicate(timeout=3)
            self.assertEqual(json.loads(output), {"state": "unknown", "reason": "interrupted"})
            self.assertEqual(error, b"")
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_path.read_text()), 0)
            self.assertEqual(self.rows(), [("unknown", "interrupted")])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=3)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.config = self.base / "config.toml"
        self.runtime = self.base / "runtime"
        self.key = self.base / "key"
        self.key.write_text("PRIVATE-KEY")
        self.key.chmod(0o600)
        self.codex = self.base / "codex"
        self.codex.write_text("#!/bin/sh\nexit 0\n")
        self.codex.chmod(0o700)
        self.line = "notify  = " + json.dumps([sys.executable, "-c", "print('原始🚀')", "DEL=\x7f"], ensure_ascii=False).replace("\x7f", "\\u007f") + "\r\n"
        self.tail = 'model = "unchanged"\r\nsecret = "PRIVATE CONFIG"\r\n'
        self.config.write_bytes((self.line + self.tail).encode("utf-8"))
        self.config.chmod(0o640)

    def manage(self, action, **kwargs):
        with mock.patch.dict(sys.modules, {"notify": notifier, "read_thread_title": reader}):
            return manager.manage(action, str(self.config), str(self.runtime), str(self.codex), sys.executable,
                                  key_file=str(self.key), **kwargs)

    def test_roundtrip_non_bmp_del_unrelated_edits_private_state_preserved(self):
        self.assertEqual(self.manage("enable")["result"], "enabled")
        enabled = self.config.read_bytes()
        self.assertIn("🚀".encode(), enabled)
        self.assertIn(b"\\u007f", enabled)
        self.assertNotIn(b"\x7f", enabled)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o700)
        for path in self.runtime.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn(b"PRIVATE-KEY", b"".join(path.read_bytes() for path in self.runtime.iterdir()))
        db = notifier.open_state(str(self.runtime))
        notifier.claim(db, HOST, THREAD, TURN, time.time())
        notifier.finish(db, (HOST, THREAD, TURN), "sent", "accepted")
        db.close()
        with self.config.open("ab") as stream:
            stream.write(b'new_setting = "keep"\r\n')
        self.assertEqual(self.manage("disable")["result"], "disabled")
        self.assertEqual(self.config.read_bytes(), (self.line + self.tail).encode() + b'new_setting = "keep"\r\n')
        self.assertEqual(self.manage("enable")["result"], "enabled")
        self.assertEqual(self.manage("status")["counts"], {"sent": 1})
        self.assertEqual(self.manage("disable")["result"], "disabled")
        self.assertEqual(self.manage("disable")["result"], "already_disabled")

    def test_disable_foreign_config_deactivates_without_overwriting(self):
        self.manage("enable")
        foreign = b'notify = ["/bin/echo", "foreign"]\nmodel="preserved"\n'
        self.config.write_bytes(foreign)
        self.assertEqual(self.manage("disable")["result"], "disabled_config_conflict")
        self.assertEqual(self.config.read_bytes(), foreign)
        self.assertFalse(manager.activation_enabled(str(self.runtime)))
        self.assertEqual(self.manage("status")["result"], "config_conflict")

    def test_missing_broken_helper_and_activation_do_not_prevent_disable(self):
        for mode in ("missing", "broken", "oversized"):
            with self.subTest(mode=mode):
                if not self.runtime.exists():
                    self.manage("enable")
                else:
                    # restore copied sources for an independent recovery case
                    for name in manager.FILES:
                        (self.runtime / name).write_bytes((SCRIPTS / name).read_bytes())
                        (self.runtime / name).chmod(0o600)
                    self.manage("enable")
                helper = self.runtime / "read_thread_title.py"
                if mode == "missing":
                    helper.unlink()
                    (self.runtime / "activation.json").unlink()
                else:
                    helper.write_text("broken syntax (")
                    (self.runtime / "activation.json").write_text("broken json" if mode == "broken" else "x" * (manager.MAX_CONFIG + 1))
                self.assertEqual(self.manage("disable")["result"], "disabled")
                self.assertFalse(manager.activation_enabled(str(self.runtime)))

    def test_nested_wrapper_symlinks_and_config_identity_conflicts_refused(self):
        self.config.write_text('notify = ["/bin/echo", "--forward"]\n')
        with self.assertRaises(manager.InstallError):
            self.manage("enable")
        self.config.write_bytes((self.line + self.tail).encode())
        link = self.base / "linked.toml"
        link.symlink_to(self.config)
        with self.assertRaises(manager.InstallError):
            manager.manage("enable", str(link), str(self.runtime), str(self.codex), sys.executable, str(self.key))
        before, info = manager.read_regular(str(self.config))
        self.config.write_bytes(before + b"new=true\n")
        with self.assertRaises(manager.InstallError):
            manager.replace_checked(str(self.config), before, info, b"replacement")
        self.assertTrue(self.config.read_bytes().endswith(b"new=true\n"))

    def test_delivery_gate_waits_and_disable_prevents_next_send(self):
        self.manage("enable")
        started, release = threading.Event(), threading.Event()
        def network(*_args):
            started.set()
            self.assertTrue(release.wait(3))
            return ACCEPTED
        def worker():
            with mock.patch.dict(sys.modules, {"read_thread_title": reader}):
                return notifier.worker(str(self.runtime), str(self.codex), THREAD, TURN)
        with mock.patch.object(reader, "read_completion_metadata", return_value=METADATA) as read, \
                mock.patch.object(notifier, "network_child", side_effect=network) as send, \
                concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(worker)
            self.assertTrue(started.wait(3))
            disabling = pool.submit(self.manage, "disable")
            time.sleep(0.1)
            self.assertFalse(disabling.done())
            self.assertTrue(manager.activation_enabled(str(self.runtime)))
            release.set()
            self.assertEqual(running.result(timeout=3), ACCEPTED)
            self.assertEqual(disabling.result(timeout=3)["result"], "disabled")
            next_turn = str(uuid.UUID(int=1000))
            self.assertEqual(notifier.worker(str(self.runtime), str(self.codex), THREAD, next_turn)["reason"], "disabled")
            read.assert_called_once()
            send.assert_called_once()

    def test_test_command_requires_completed_identity_and_uses_claim(self):
        self.manage("enable")
        with self.assertRaises(manager.InstallError):
            self.manage("test")
        with mock.patch.object(reader, "read_completion_metadata", return_value=METADATA), \
                mock.patch.object(notifier, "network_child", return_value=ACCEPTED) as send:
            self.assertEqual(self.manage("test", thread_id=THREAD, turn_id=TURN)["state"], "sent")
            self.assertEqual(self.manage("test", thread_id=THREAD, turn_id=TURN)["reason"], "duplicate")
            send.assert_called_once()
        value = self.manage("status")
        self.assertEqual(value["counts"], {"sent": 1})
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertNotIn(METADATA["name"], json.dumps(value))


if __name__ == "__main__":
    unittest.main()
