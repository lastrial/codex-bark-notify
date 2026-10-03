"""Accepted asynchronous-question hooks, private delivery and owned JSON merge."""
import concurrent.futures
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_task_notify import SCRIPTS, THREAD, TURN, HOST, METADATA, ACCEPTED, TempRuntime, notifier, reader, load

sys.path.insert(0, str(SCRIPTS))
question = load("bark_question_notifier", SCRIPTS / "question.py")
question_manager = load("bark_question_manager", SCRIPTS / "manage_questions.py")
completion = question_manager.completion
CALL = "call_question_1"
OTHER = "00000000-0000-4000-8000-000000000002"


def event(**changes):
    value = {"hook_event_name": "PostToolUse", "tool_name": "request_user_input_async", "session_id": THREAD,
             "turn_id": TURN, "tool_use_id": CALL, "transcript_path": "/never/open/rollout-2026-10-03-" + THREAD + ".jsonl",
             "tool_input": {"questions": [{"question": "PRIVATE QUESTION"}]},
             "tool_response": json.dumps({"accepted": True, "request_id": "PRIVATE RESPONSE"})}
    value.update(changes)
    return value


def identity(value):
    with mock.patch.dict(sys.modules, {"notify": notifier}):
        return question.event_identity(json.dumps(value))


class HookTests(unittest.TestCase):
    def test_accepts_only_post_async_boolean_true_and_both_response_forms(self):
        self.assertEqual(identity(event()), (THREAD, TURN, CALL))
        self.assertEqual(identity(event(tool_response={"accepted": True})), (THREAD, TURN, CALL))
        for changes in ({"hook_event_name": "PreToolUse"}, {"tool_name": "request_user_input"},
                        {"hook_event_name": "Stop"}, {"tool_response": "{broken"}, {"tool_response": "[]"},
                        {"tool_response": {}}, {"tool_response": {"accepted": False}}, {"tool_response": {"accepted": 1}},
                        {"tool_response": {"accepted": "true"}}, {"tool_response": {"accepted": None}},
                        {"tool_response": '{"accepted":false,"accepted":true}'}):
            with self.subTest(changes=changes):
                self.assertIsNone(identity(event(**changes)))

    def test_rejects_flat_child_markers_suffix_mismatch_and_bad_identities(self):
        for changes in ({"agent_id": None}, {"agent_type": None}, {"agent_id": THREAD},
                        {"transcript_path": "/never/open/rollout-" + OTHER + ".jsonl"},
                        {"transcript_path": None}, {"transcript_path": THREAD + ".jsonl.backup"},
                        {"session_id": THREAD.upper()}, {"turn_id": "missing"},
                        {"tool_use_id": "call_bad;command"}, {"tool_use_id": "call_中文"},
                        {"tool_use_id": "call_"}, {"tool_use_id": "call_" + "x" * 124}, {"tool_use_id": "call_bad\n"}):
            with self.subTest(changes=changes):
                self.assertIsNone(identity(event(**changes)))
        with mock.patch.dict(sys.modules, {"notify": notifier}), mock.patch("builtins.open", side_effect=AssertionError("transcript opened")):
            self.assertEqual(question.event_identity(json.dumps(event())), (THREAD, TURN, CALL))

    def test_bad_oversize_utf8_and_duplicate_inputs_fail_closed(self):
        with mock.patch.dict(sys.modules, {"notify": notifier}):
            for raw in (b"\xff", b"x" * (question.MAX_HOOK_BYTES + 1), b"{broken", b"[]", b'{"tool_name":"a","tool_name":"b"}'):
                self.assertIsNone(question.event_identity(raw))
            oversized = event(tool_input={"questions": ["私" * question.MAX_HOOK_BYTES]})
            self.assertIsNone(question.event_identity(json.dumps(oversized, ensure_ascii=False)))

    def test_detached_worker_contains_only_identifiers_and_paths(self):
        with mock.patch("subprocess.Popen") as spawn:
            question.launch_worker("/runtime", "/codex", (THREAD, TURN, CALL))
        args, kwargs = spawn.call_args
        self.assertEqual(args[0][-6:], ["--thread-id", THREAD, "--turn-id", TURN, "--tool-use-id", CALL])
        self.assertNotIn("PRIVATE", " ".join(args[0]))
        self.assertNotIn("transcript", " ".join(args[0]))
        self.assertTrue(kwargs["close_fds"] and kwargs["start_new_session"])
        for name in ("stdin", "stdout", "stderr"):
            self.assertEqual(kwargs[name], subprocess.DEVNULL)

    def test_hook_is_silent_and_zero_on_invalid_valid_or_missing_dependencies(self):
        command = [sys.executable, "-B", str(SCRIPTS / "question.py"), "--hook", "--runtime", "/missing", "--codex", "/missing"]
        for raw in (b"{broken", json.dumps(event()).encode(), b"x" * (question.MAX_HOOK_BYTES + 1)):
            result = subprocess.run(command, input=raw, capture_output=True, check=False,
                                    env=dict(os.environ, PYTHONWARNINGS="always"))
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "question.py"
            copy.write_bytes((SCRIPTS / "question.py").read_bytes())
            command[2] = str(copy)
            result = subprocess.run(command, input=json.dumps(event()).encode(), capture_output=True, check=False)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_hook_worker_start_error_never_affects_output(self):
        args = ["question.py", "--hook", "--runtime", "/runtime", "--codex", "/codex"]
        stdin = mock.Mock(buffer=io.BytesIO(json.dumps(event()).encode()))
        with mock.patch.dict(sys.modules, {"notify": notifier}), mock.patch.object(sys, "argv", args), \
                mock.patch.object(sys, "stdin", stdin), mock.patch.object(question, "launch_worker", side_effect=OSError("PRIVATE")), \
                mock.patch.object(sys, "stdout", io.StringIO()) as out, mock.patch.object(sys, "stderr", io.StringIO()) as err:
            self.assertEqual(question.main(), 0)
            self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))


class QuestionWorkerTests(TempRuntime):
    def setUp(self):
        super().setUp()
        self.write_private("questions-activation.json", {"enabled": True})

    def worker(self, call=CALL):
        with mock.patch.dict(sys.modules, {"notify": notifier, "read_thread_title": reader}):
            return question.worker(str(self.runtime), "/fixture/codex", THREAD, TURN, call)

    def question_rows(self):
        db = sqlite3.connect(str(self.runtime / "questions.sqlite"))
        try:
            return db.execute("SELECT tool_use_id,state,reason FROM questions ORDER BY tool_use_id").fetchall()
        finally:
            db.close()

    def test_claim_is_committed_before_metadata_and_alarm_body_is_exact(self):
        def metadata(*_args):
            self.assertEqual(self.question_rows(), [(CALL, "processing", "claimed")])
            return METADATA
        with mock.patch.object(reader, "read_completion_metadata", side_effect=metadata) as read, \
                mock.patch.object(notifier, "network_child", return_value=ACCEPTED) as send, \
                mock.patch("socket.gethostname", return_value="Exact.Host"):
            self.assertEqual(self.worker(), ACCEPTED)
            self.assertEqual(self.worker()["reason"], "duplicate")
        read.assert_called_once()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], {"title": "Exact.Host", "body": METADATA["name"] + " 等待你回复", "sound": "alarm"})
        self.assertEqual(self.question_rows(), [(CALL, "sent", "accepted")])
        contents = (self.runtime / "questions.sqlite").read_bytes()
        self.assertNotIn(METADATA["name"].encode(), contents)
        self.assertNotIn(b"PRIVATE-KEY", contents)
        self.assertNotIn(b"PRIVATE QUESTION", contents)

    def test_distinct_question_calls_and_completion_keep_independent_state(self):
        db = notifier.open_state(str(self.runtime))
        notifier.claim(db, HOST, THREAD, TURN, time.time())
        notifier.finish(db, (HOST, THREAD, TURN), "sent", "accepted")
        db.close()
        before = (self.runtime / "state.sqlite").read_bytes()
        with mock.patch.object(reader, "read_completion_metadata", return_value=METADATA), \
                mock.patch.object(notifier, "network_child", return_value=ACCEPTED) as send:
            self.assertEqual(self.worker(), ACCEPTED)
            self.assertEqual(self.worker("call_question_2"), ACCEPTED)
            self.assertEqual(self.worker()["reason"], "duplicate")
            self.assertEqual(send.call_count, 2)
        self.assertEqual(len(self.question_rows()), 2)
        self.assertEqual((self.runtime / "state.sqlite").read_bytes(), before)

    def test_concurrent_same_call_sends_once(self):
        with mock.patch.object(reader, "read_completion_metadata", return_value=METADATA) as read, \
                mock.patch.object(notifier, "network_child", side_effect=lambda *_a: (time.sleep(0.1) or ACCEPTED)) as send:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _n: self.worker(), range(6)))
        self.assertEqual(sum(value["state"] == "sent" for value in results), 1)
        read.assert_called_once()
        send.assert_called_once()

    def test_either_activation_stops_questions_and_recheck_prevents_race(self):
        for filename in ("activation.json", "questions-activation.json"):
            self.write_private(filename, {"enabled": False})
            with mock.patch.object(reader, "read_completion_metadata") as read, mock.patch.object(notifier, "network_child") as send:
                self.assertEqual(self.worker()["reason"], "disabled")
                read.assert_not_called()
                send.assert_not_called()
            self.write_private(filename, {"enabled": True})
        def disable(*_args):
            self.write_private("questions-activation.json", {"enabled": False})
            return METADATA
        with mock.patch.object(reader, "read_completion_metadata", side_effect=disable), mock.patch.object(notifier, "network_child") as send:
            self.assertEqual(self.worker()["reason"], "disabled")
            send.assert_not_called()

    def test_metadata_failure_child_or_missing_title_never_sends(self):
        for i, metadata in enumerate((dict(METADATA, source={"subAgent": {}}), dict(METADATA, name=None),
                                      dict(METADATA, threadSource="system"))):
            with mock.patch.object(reader, "read_completion_metadata", return_value=metadata), mock.patch.object(notifier, "network_child") as send:
                self.assertEqual(self.worker("call_skip_" + str(i))["state"], "skipped")
                send.assert_not_called()
        with mock.patch.object(reader, "read_completion_metadata", side_effect=reader.ProbeError("PRIVATE")), \
                mock.patch.object(notifier, "network_child") as send:
            self.assertEqual(self.worker("call_failed")["reason"], "metadata_error")
            send.assert_not_called()

    def test_unknown_and_processing_never_retry_and_stale_rows_remain_unknown(self):
        with mock.patch.object(reader, "read_completion_metadata", return_value=METADATA), \
                mock.patch.object(notifier, "network_child", return_value={"state": "unknown", "reason": "transport_error"}) as send:
            self.assertEqual(self.worker()["state"], "unknown")
            self.assertEqual(self.worker()["reason"], "duplicate")
            send.assert_called_once()
        with mock.patch.dict(sys.modules, {"notify": notifier}):
            db = question.open_state(str(self.runtime))
            question.claim(db, (HOST, THREAD, TURN, "call_crashed"), time.time() - 100)
            db.close()
        with mock.patch.object(reader, "read_completion_metadata") as read, mock.patch.object(notifier, "network_child") as send:
            self.assertEqual(self.worker("call_crashed")["reason"], "duplicate")
            read.assert_not_called()
            send.assert_not_called()
        self.assertIn(("call_crashed", "unknown", "stale_processing"), self.question_rows())

    def test_preclaim_database_failure_and_postsend_persistence_failure_cannot_resend(self):
        with mock.patch.object(question, "open_state", side_effect=sqlite3.OperationalError("PRIVATE")), \
                mock.patch.object(reader, "read_completion_metadata") as read, mock.patch.object(notifier, "network_child") as send:
            self.assertEqual(self.worker()["state"], "unknown")
            read.assert_not_called()
            send.assert_not_called()
        with mock.patch.object(reader, "read_completion_metadata", return_value=METADATA), \
                mock.patch.object(notifier, "network_child", return_value=ACCEPTED) as send, \
                mock.patch.object(question, "finish", side_effect=sqlite3.OperationalError("PRIVATE")):
            self.assertEqual(self.worker(), {"state": "unknown", "reason": "state_error"})
            self.assertEqual(self.worker()["reason"], "duplicate")
            send.assert_called_once()
        self.assertEqual(self.question_rows(), [(CALL, "processing", "claimed")])


class QuestionHttpTests(TempRuntime):
    def test_alarm_is_whitelisted_completion_stays_calypso_and_other_sounds_do_not_send(self):
        response = mock.Mock(status=200)
        response.read.return_value = b'{"code":200}'
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch("http.client.HTTPSConnection", return_value=connection):
            for sound in (None, "calypso", "alarm"):
                payload = {"title": "Host", "body": "任务 等待你回复"}
                if sound is not None:
                    payload["sound"] = sound
                self.assertEqual(notifier.http_send(str(self.key), payload, 1), ACCEPTED)
                self.assertEqual(json.loads(connection.request.call_args.kwargs["body"])["sound"], sound or "calypso")
        with mock.patch("http.client.HTTPSConnection") as connect, mock.patch("subprocess.Popen") as spawn:
            for sound in ("minuet", "bad", 7, None):
                payload = {"title": "Host", "body": "PRIVATE", "sound": sound}
                self.assertEqual(notifier.http_send(str(self.key), payload, 1)["reason"], "network_child_error")
                self.assertEqual(notifier.network_child(str(self.key), payload, time.monotonic() + 5)["reason"], "network_child_error")
            connect.assert_not_called()
            spawn.assert_not_called()


class QuestionManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.runtime = self.base / "runtime"
        self.config = self.base / "config.toml"
        self.hooks = self.base / "hooks.json"
        self.codex = self.base / "codex"
        self.codex.write_text("#!/bin/sh\nexit 0\n")
        self.codex.chmod(0o700)
        self.key = self.base / "key"
        self.key.write_text("PRIVATE-KEY")
        self.key.chmod(0o600)
        self.config.write_text('notify = ' + json.dumps([sys.executable, "-c", "pass"]) + '\nmodel="keep"\n')
        self.config.chmod(0o600)
        completion.manage("enable", str(self.config), str(self.runtime), str(self.codex), sys.executable, str(self.key))

    def manage(self, action):
        with mock.patch.dict(sys.modules, {"notify": notifier, "read_thread_title": reader}):
            return question_manager.manage(action, str(self.hooks), str(self.runtime), str(self.codex))

    def read_hooks(self):
        return json.loads(self.hooks.read_text())

    def test_missing_file_idempotent_hook_entry_trust_unknown_and_private_files(self):
        result = self.manage("enable")
        self.assertEqual(result["result"], "configured")
        self.assertEqual(result["trust_status"], "unknown")
        self.assertEqual(result["trust_action"], "/hooks")
        entry = self.read_hooks()["hooks"]["PostToolUse"][0]
        self.assertEqual(entry["matcher"], "^request_user_input_async$")
        self.assertEqual(entry["hooks"][0]["timeout"], 3)
        self.assertNotIn("PRIVATE", entry["hooks"][0]["command"])
        self.assertEqual(self.manage("enable")["result"], "configured")
        self.assertEqual(len(self.read_hooks()["hooks"]["PostToolUse"]), 1)
        self.assertEqual(self.manage("status")["trust_status"], "unknown")
        for name in question_manager.FILES + (question_manager.OWNERSHIP, question_manager.ACTIVATION, "questions.sqlite"):
            self.assertEqual((self.runtime / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manage("disable")["result"], "disabled")
        self.assertEqual(self.read_hooks()["hooks"]["PostToolUse"], [])
        self.assertEqual(self.manage("disable")["result"], "already_disabled")

    def test_enable_and_remove_preserve_foreign_groups_values_and_permissions(self):
        foreign = {"version": 1, "metadata": {"foreign": ["keep", "中文", 3]}, "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "foreign-stop"}]}],
            "PostToolUse": [{"matcher": "another", "hooks": [{"type": "command", "command": "foreign-post"}]}]}}
        self.hooks.write_text(json.dumps(foreign))
        self.hooks.chmod(0o640)
        self.manage("enable")
        changed = self.read_hooks()
        self.assertEqual(changed["metadata"], foreign["metadata"])
        self.assertEqual(changed["hooks"]["Stop"], foreign["hooks"]["Stop"])
        self.assertEqual(changed["hooks"]["PostToolUse"][0], foreign["hooks"]["PostToolUse"][0])
        self.assertEqual(self.hooks.stat().st_mode & 0o777, 0o640)
        changed["later"] = {"preserve": True}
        self.hooks.write_text(json.dumps(changed))
        self.manage("disable")
        self.assertEqual(self.read_hooks(), dict(foreign, later={"preserve": True}))

    def test_foreign_edits_deactivate_first_preserve_config_and_report_conflict(self):
        for mode in ("alter", "replace", "invalid"):
            with self.subTest(mode=mode):
                self.manage("enable")
                changed = self.read_hooks()
                if mode == "alter":
                    changed["hooks"]["PostToolUse"][0]["hooks"][0]["timeout"] = 9
                    raw = json.dumps(changed)
                elif mode == "replace":
                    changed["hooks"]["PostToolUse"] = [{"hooks": [{"command": "totally-foreign"}]}]
                    raw = json.dumps(changed)
                else:
                    raw = "{broken"
                self.hooks.write_text(raw)
                self.assertEqual(self.manage("disable")["result"], "disabled_config_conflict")
                self.assertEqual(self.hooks.read_text(), raw)
                self.assertFalse(question_manager.question_enabled(str(self.runtime)))
                # Restore only this fixture's configuration for the next case.
                self.hooks.write_text(json.dumps({"hooks": {"PostToolUse": []}}))

    def test_symlink_invalid_json_and_new_array_are_conservative(self):
        target = self.base / "foreign.json"
        target.write_text('{}')
        self.hooks.symlink_to(target)
        with self.assertRaises(completion.InstallError):
            self.manage("enable")
        self.assertEqual(target.read_text(), '{}')
        self.hooks.unlink()
        for raw in ('{broken', '[]', '{"hooks":[]}', '{"hooks":{"PostToolUse":{}}}', '{"hooks":{},"hooks":{}}'):
            self.hooks.write_text(raw)
            with self.assertRaises(completion.InstallError):
                self.manage("enable")
            self.assertEqual(self.hooks.read_text(), raw)
        self.hooks.write_text('{"keep":7}')
        self.manage("enable")
        self.assertEqual(self.read_hooks()["keep"], 7)

    def test_disable_reenable_preserves_both_databases_and_status_never_has_titles(self):
        self.manage("enable")
        with mock.patch.dict(sys.modules, {"notify": notifier, "read_thread_title": reader}), \
                mock.patch.object(reader, "read_completion_metadata", return_value=METADATA), \
                mock.patch.object(notifier, "network_child", return_value=ACCEPTED):
            self.assertEqual(notifier.worker(str(self.runtime), str(self.codex), THREAD, TURN), ACCEPTED)
            self.assertEqual(question.worker(str(self.runtime), str(self.codex), THREAD, TURN, CALL), ACCEPTED)
        before = {name: (self.runtime / name).read_bytes() for name in ("state.sqlite", "questions.sqlite")}
        self.manage("disable")
        self.manage("enable")
        for name, data in before.items():
            self.assertEqual((self.runtime / name).read_bytes(), data)
        result = self.manage("status")
        self.assertEqual(result["counts"], {"sent": 1})
        self.assertEqual(result["recent"][0]["tool_use_id"], CALL)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn(METADATA["name"], json.dumps(result))

    def test_question_disable_gate_waits_then_old_hook_worker_cannot_send(self):
        self.manage("enable")
        started, release = threading.Event(), threading.Event()
        def network(*_args):
            started.set()
            self.assertTrue(release.wait(3))
            return ACCEPTED
        def worker():
            with mock.patch.dict(sys.modules, {"notify": notifier, "read_thread_title": reader}):
                return question.worker(str(self.runtime), str(self.codex), THREAD, TURN, CALL)
        with mock.patch.object(reader, "read_completion_metadata", return_value=METADATA) as read, \
                mock.patch.object(notifier, "network_child", side_effect=network) as send, \
                concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(worker)
            self.assertTrue(started.wait(3))
            disabled = pool.submit(self.manage, "disable")
            time.sleep(0.1)
            self.assertFalse(disabled.done())
            release.set()
            self.assertEqual(running.result(timeout=3), ACCEPTED)
            self.assertEqual(disabled.result(timeout=3)["result"], "disabled")
            with mock.patch.dict(sys.modules, {"notify": notifier, "read_thread_title": reader}):
                self.assertEqual(question.worker(str(self.runtime), str(self.codex), THREAD, TURN, "call_next")["reason"], "disabled")
            read.assert_called_once()
            send.assert_called_once()

    def test_completion_disable_stops_question_without_resetting_question_activation(self):
        self.manage("enable")
        completion.manage("disable", str(self.config), str(self.runtime), str(self.codex), sys.executable)
        self.assertTrue(question_manager.question_enabled(str(self.runtime)))
        self.assertFalse(self.manage("status")["active"])
        with mock.patch.dict(sys.modules, {"notify": notifier, "read_thread_title": reader}), \
                mock.patch.object(reader, "read_completion_metadata") as read, mock.patch.object(notifier, "network_child") as send:
            self.assertEqual(question.worker(str(self.runtime), str(self.codex), THREAD, TURN, CALL)["reason"], "disabled")
            read.assert_not_called()
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
