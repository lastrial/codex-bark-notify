#!/usr/bin/env python3
"""Silently observe accepted asynchronous user questions and notify once.

Only identities survive hook input. No transcript, tool arguments, question
text or response is opened, logged or passed to a detached worker.
"""
import os
import sys

sys.dont_write_bytecode = True
MAX_HOOK_BYTES = 64 * 1024


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_key")
        value[key] = item
    return value


def tool_id_valid(value):
    import re
    return isinstance(value, str) and len(value) <= 128 and re.fullmatch(r"call_[A-Za-z0-9._-]+", value) is not None


def event_identity(raw):
    import json
    import re
    import notify as base
    try:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if not isinstance(encoded, bytes) or len(encoded) > MAX_HOOK_BYTES:
            return None
        event = json.loads(encoded, object_pairs_hook=unique_object)
        if (not isinstance(event, dict) or event.get("hook_event_name") != "PostToolUse" or
                event.get("tool_name") != "request_user_input_async" or "agent_id" in event or "agent_type" in event):
            return None
        thread_id, turn_id, call_id = (event.get("session_id"), event.get("turn_id"), event.get("tool_use_id"))
        if not base.canonical_uuid(thread_id) or not base.canonical_uuid(turn_id) or not tool_id_valid(call_id):
            return None
        transcript = event.get("transcript_path")
        if not isinstance(transcript, str) or "\0" in transcript:
            return None
        suffix = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", os.path.basename(transcript))
        if suffix is None or suffix.group(1) != thread_id:
            return None
        response = event.get("tool_response")
        if isinstance(response, str):
            response = json.loads(response, object_pairs_hook=unique_object)
        if not isinstance(response, dict) or response.get("accepted") is not True:
            return None
        return thread_id, turn_id, call_id
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def active_questions(runtime):
    import json
    import notify as base
    if not base.active_runtime(runtime):
        return False
    try:
        value = json.loads(base.read_private(os.path.join(runtime, "questions-activation.json"), 1024))
        return isinstance(value, dict) and set(value) == {"enabled"} and value["enabled"] is True
    except (base.RuntimeErrorCode, OSError, ValueError, UnicodeError, RecursionError):
        return False


def open_state(runtime):
    import sqlite3
    import notify as base
    path = os.path.join(runtime, "questions.sqlite")
    base.no_links(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        base.check_private(os.fstat(fd))
    finally:
        os.close(fd)
    db = sqlite3.connect(path, timeout=0.5)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS questions (host_id TEXT NOT NULL, thread_id TEXT NOT NULL, "
                   "turn_id TEXT NOT NULL, tool_use_id TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, "
                   "started_at REAL NOT NULL, finished_at REAL, PRIMARY KEY(host_id,thread_id,turn_id,tool_use_id))")
        db.commit()
        return db
    except BaseException:
        db.close()
        raise


def claim(db, ids, now):
    import notify as base
    db.execute("UPDATE questions SET state='unknown',reason='stale_processing',finished_at=? "
               "WHERE state='processing' AND started_at<?", (now, now - base.STALE_AFTER))
    db.commit()
    cursor = db.execute("INSERT OR IGNORE INTO questions VALUES(?,?,?,?,'processing','claimed',?,NULL)", tuple(ids) + (now,))
    db.commit()
    return cursor.rowcount == 1


def finish(db, ids, result):
    import notify as base
    import time
    if result["state"] not in base.STATES or result["reason"] not in base.REASONS:
        raise base.RuntimeErrorCode("invalid_result")
    db.execute("UPDATE questions SET state=?,reason=?,finished_at=? WHERE host_id=? AND thread_id=? AND turn_id=? "
               "AND tool_use_id=? AND state='processing'", (result["state"], result["reason"], time.time()) + tuple(ids))
    db.commit()


def worker(runtime, codex, thread_id, turn_id, call_id):
    import notify as base
    import socket
    import time
    from read_thread_title import read_completion_metadata
    deadline = time.monotonic() + base.WORKER_BUDGET
    db, gate, owned = None, None, False
    ids = None
    result = {"state": "unknown", "reason": "worker_error"}
    try:
        if not base.canonical_uuid(thread_id) or not base.canonical_uuid(turn_id) or not tool_id_valid(call_id):
            return {"state": "skipped", "reason": "invalid_event"}
        base.private_runtime(runtime)
        if not active_questions(runtime):
            return {"state": "skipped", "reason": "disabled"}
        config = base.settings(runtime)
        ids = (config["host_id"], thread_id, turn_id, call_id)
        db = open_state(runtime)
        owned = claim(db, ids, time.time())
        if not owned:
            return {"state": "skipped", "reason": "duplicate"}
        try:
            timeout = min(3.5, deadline - time.monotonic() - base.CLEANUP_MARGIN - 0.5)
            if timeout <= 0:
                result = {"state": "unknown", "reason": "deadline"}
                return result
            metadata = read_completion_metadata(codex, thread_id, timeout)
        except Exception:
            result = {"state": "skipped", "reason": "metadata_error"}
            return result
        reason = base.classify(metadata)
        if reason != "eligible":
            result = {"state": "skipped", "reason": reason}
            return result
        gate = base.open_gate(runtime)
        if not base.lock_until(gate, False, deadline - base.CLEANUP_MARGIN):
            result = {"state": "unknown", "reason": "deadline"}
            return result
        if not active_questions(runtime):
            result = {"state": "skipped", "reason": "disabled"}
            return result
        result = dict(base.network_child(config["key_file"],
                                        {"title": socket.gethostname(), "body": metadata["name"] + " 等待你回复",
                                         "sound": "alarm"}, deadline))
        return result
    except (KeyboardInterrupt, SystemExit):
        result = {"state": "unknown", "reason": "interrupted"}
        return result
    except BaseException:
        return result
    finally:
        if owned and db:
            try:
                finish(db, ids, result)
            except BaseException:
                result.clear()
                result.update({"state": "unknown", "reason": "state_error"})
        if db:
            db.close()
        if gate is not None:
            os.close(gate)


def launch_worker(runtime, codex, identity):
    import subprocess
    import warnings
    # Detachment is intentional. Popen's discarded handle can otherwise emit a
    # ResourceWarning to the hook's stderr when the caller enables warnings.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        subprocess.Popen([sys.executable, "-B", os.path.abspath(__file__), "--worker", "--runtime", runtime,
                          "--codex", codex, "--thread-id", identity[0], "--turn-id", identity[1], "--tool-use-id", identity[2]],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=True)


def main():
    argv = sys.argv[1:]
    if argv == ["--self-check"]:
        import notify as base
        from read_thread_title import read_completion_metadata
        if not base.http_payload_valid({"title": "", "body": "", "sound": "alarm"}):
            return 1
        print('{"result":"self_check_ok"}')
        return 0
    if argv and argv[0] == "--hook":
        try:
            if len(argv) == 5 and argv[1] == "--runtime" and argv[3] == "--codex":
                identity = event_identity(sys.stdin.buffer.read(MAX_HOOK_BYTES + 1))
                if identity:
                    launch_worker(argv[2], argv[4], identity)
        except BaseException:
            pass
        return 0
    if argv and argv[0] == "--worker":
        import signal
        def interrupted(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupted)
        try:
            if (len(argv) == 11 and argv[1] == "--runtime" and argv[3] == "--codex" and argv[5] == "--thread-id" and
                    argv[7] == "--turn-id" and argv[9] == "--tool-use-id"):
                worker(argv[2], argv[4], argv[6], argv[8], argv[10])
        except BaseException:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:
        sys.exit(0)
