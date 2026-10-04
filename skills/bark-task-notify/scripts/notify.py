#!/usr/bin/env python3
"""Preserve the existing callback and deliver eligible completion events once.

The forwarding path never reads a key, title or state. Worker diagnostics and
SQLite retain only identities, fixed states, reasons and times.
"""
import os
import sys

if os.name == "nt":
    import windows_native as native

sys.dont_write_bytecode = True
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_NAME_BYTES = 4096
MAX_HTTP_BYTES = 16 * 1024
WORKER_BUDGET = 10.0
CLEANUP_MARGIN = 1.2
FINAL_MARGIN = CLEANUP_MARGIN + 0.6
STALE_AFTER = WORKER_BUDGET + CLEANUP_MARGIN + 1.0
STATES = {"processing", "sent", "rejected", "unknown", "skipped"}
REASONS = {"claimed", "accepted", "http_rejected", "api_rejected", "transport_error",
           "http_unknown", "invalid_response", "response_too_large", "key_invalid",
           "deadline", "interrupted", "stale_processing", "source_skip", "child_skip",
           "thread_source_skip", "title_unavailable", "title_too_large", "metadata_error",
           "disabled", "worker_error", "network_child_error", "state_error"}


class RuntimeErrorCode(Exception):
    pass


def canonical_uuid(value):
    import uuid
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def event_identity(raw):
    import json
    try:
        if len(raw) > MAX_PAYLOAD_BYTES or len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("type") != "agent-turn-complete":
            return None
        ids = [value.get("thread-id"), value.get("turn-id")]
        return ids if all(canonical_uuid(item) for item in ids) else None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def no_links(path):
    if os.name == "nt":
        return native.no_links(path)
    current = os.path.abspath(path)
    while True:
        if os.path.islink(current):
            raise RuntimeErrorCode("unsafe_path")
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def private_runtime(path):
    if os.name == "nt":
        return native.private_directory(path)
    import stat
    no_links(path)
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeErrorCode("unsafe_runtime")


def check_private(info):
    import stat
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
            info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeErrorCode("unsafe_file")


def read_private(path, maximum):
    if os.name == "nt":
        return native.read_regular(path, maximum, private=True)[0]
    no_links(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        check_private(os.fstat(fd))
        data = os.read(fd, maximum + 1)
        if len(data) > maximum:
            raise RuntimeErrorCode("file_too_large")
        return data
    finally:
        os.close(fd)


def active_runtime(runtime):
    import json
    try:
        value = json.loads(read_private(os.path.join(runtime, "activation.json"), 1024))
        return isinstance(value, dict) and set(value) == {"enabled"} and value["enabled"] is True
    except (RuntimeErrorCode, OSError, ValueError, UnicodeError, RecursionError):
        return False


def settings(runtime):
    import json
    value = json.loads(read_private(os.path.join(runtime, "config.json"), 4096))
    if (not isinstance(value, dict) or set(value) != {"host_id", "key_file"} or
            not canonical_uuid(value["host_id"]) or not isinstance(value["key_file"], str) or
            not os.path.isabs(value["key_file"]) or "\0" in value["key_file"]):
        raise RuntimeErrorCode("invalid_config")
    return value


def classify(metadata):
    source = metadata.get("source")
    if isinstance(source, dict) and "subAgent" in source:
        return "child_skip"
    if not isinstance(source, str) or source not in ("cli", "vscode", "exec", "appServer"):
        return "source_skip"
    if metadata.get("threadSource") not in ("user", "agent_created_thread"):
        return "thread_source_skip"
    name = metadata.get("name")
    if not isinstance(name, str) or not name.strip():
        return "title_unavailable"
    try:
        if len(name.encode("utf-8")) > MAX_NAME_BYTES:
            return "title_too_large"
    except UnicodeError:
        return "title_unavailable"
    return "eligible"


def open_gate(runtime):
    path = os.path.join(runtime, "delivery.lock")
    if os.name == "nt":
        return native.open_fd(path, write=True, create=True)[0]
    no_links(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        check_private(os.fstat(fd))
        return fd
    except BaseException:
        os.close(fd)
        raise


def lock_until(fd, exclusive, deadline):
    if os.name == "nt":
        return native.lock_until(fd, exclusive, deadline)
    import fcntl
    import time
    while True:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))


def open_state(runtime, timeout=0.5):
    import sqlite3
    path = os.path.join(runtime, "state.sqlite")
    if os.name == "nt":
        fd, _ = native.open_fd(path, write=True, create=True)
        os.close(fd)
        return _initialize_state(path, timeout)
    no_links(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        check_private(os.fstat(fd))
    finally:
        os.close(fd)
    return _initialize_state(path, timeout)


def _initialize_state(path, timeout):
    import sqlite3
    db = sqlite3.connect(path, timeout=timeout)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS events (host_id TEXT NOT NULL, thread_id TEXT NOT NULL, "
                   "turn_id TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, started_at REAL NOT NULL, "
                   "finished_at REAL, PRIMARY KEY(host_id,thread_id,turn_id))")
        db.commit()
        return db
    except BaseException:
        db.close()
        raise


def mark_stale(db, now):
    db.execute("UPDATE events SET state='unknown',reason='stale_processing',finished_at=? "
               "WHERE state='processing' AND started_at<?", (now, now - STALE_AFTER))
    db.commit()


def claim(db, host_id, thread_id, turn_id, now):
    mark_stale(db, now)
    cursor = db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,'processing','claimed',?,NULL)",
                        (host_id, thread_id, turn_id, now))
    db.commit()
    return cursor.rowcount == 1


def finish(db, ids, state, reason):
    import time
    if state not in STATES or reason not in REASONS:
        raise RuntimeErrorCode("invalid_result")
    db.execute("UPDATE events SET state=?,reason=?,finished_at=? WHERE host_id=? AND thread_id=? AND turn_id=? "
               "AND state='processing'", (state, reason, time.time()) + tuple(ids))
    db.commit()


def read_key(path):
    data = read_private(path, 512)
    key = data.decode("utf-8")
    if not key or any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise RuntimeErrorCode("key_invalid")
    return key


def http_payload_valid(payload):
    return (isinstance(payload, dict) and set(payload) in ({"title", "body"}, {"title", "body", "sound"}) and
            all(isinstance(value, str) for value in payload.values()) and payload.get("sound", "calypso") in ("calypso", "alarm"))


def http_send(key_file, payload, timeout):
    """Only the isolated child calls this function; raw errors never escape."""
    import http.client
    import json
    connection = None
    if not http_payload_valid(payload):
        return {"state": "unknown", "reason": "network_child_error"}
    try:
        key = read_key(key_file)
    except (RuntimeErrorCode, OSError, ValueError, UnicodeError):
        return {"state": "rejected", "reason": "key_invalid"}
    try:
        body = json.dumps({"device_key": key, "title": payload["title"], "body": payload["body"],
                           "group": "bark-task-notify", "sound": payload.get("sound", "calypso"), "level": "timeSensitive"},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        connection = http.client.HTTPSConnection("api.day.app", 443, timeout=timeout)
        connection.request("POST", "/push", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        if 400 <= response.status < 500:
            return {"state": "rejected", "reason": "http_rejected"}
        if response.status != 200:
            return {"state": "unknown", "reason": "http_unknown"}
        raw = response.read(MAX_HTTP_BYTES + 1)
        if len(raw) > MAX_HTTP_BYTES:
            return {"state": "unknown", "reason": "response_too_large"}
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            return {"state": "unknown", "reason": "invalid_response"}
        if not isinstance(value, dict) or type(value.get("code")) is not int:
            return {"state": "unknown", "reason": "invalid_response"}
        return ({"state": "sent", "reason": "accepted"} if value["code"] == 200 else
                {"state": "rejected", "reason": "api_rejected"})
    except BaseException:
        return {"state": "unknown", "reason": "transport_error"}
    finally:
        if connection:
            try:
                connection.close()
            except BaseException:
                pass


def stop_child(proc):
    if os.name == "nt":
        return native.stop_child(proc)
    import signal
    import subprocess
    # Kill the group even if its leader has exited, so descendants cannot linger.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    for stream in (proc.stdin, proc.stdout):
        if stream:
            stream.close()


def network_child(key_file, payload, deadline):
    import json
    import signal
    import subprocess
    import time
    if not http_payload_valid(payload):
        return {"state": "unknown", "reason": "network_child_error"}
    if os.name == "nt":
        return _windows_network_child(key_file, payload, deadline)
    remaining = deadline - time.monotonic() - FINAL_MARGIN
    if remaining <= 0:
        return {"state": "unknown", "reason": "deadline"}
    proc = None
    try:
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            proc = subprocess.Popen(
                [sys.executable, "-B", os.path.abspath(__file__), "--http", "--key-file", key_file,
                 "--timeout", str(remaining)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
                preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask))
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_HTTP_BYTES:
            return {"state": "unknown", "reason": "network_child_error"}
        output, _stderr = proc.communicate(input=raw, timeout=max(0.001, deadline - time.monotonic() - FINAL_MARGIN))
        if proc.returncode != 0 or len(output) > 512:
            return {"state": "unknown", "reason": "network_child_error"}
        value = json.loads(output)
        if (not isinstance(value, dict) or set(value) != {"state", "reason"} or
                value["state"] not in {"sent", "rejected", "unknown"} or value["reason"] not in REASONS):
            return {"state": "unknown", "reason": "network_child_error"}
        return value
    except subprocess.TimeoutExpired:
        return {"state": "unknown", "reason": "deadline"}
    except (OSError, ValueError, UnicodeError, RecursionError, RuntimeErrorCode):
        return {"state": "unknown", "reason": "network_child_error"}
    finally:
        if proc:
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
            try:
                stop_child(proc)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _windows_network_child(key_file, payload, deadline):
    import json
    import subprocess
    import time
    proc = None
    remaining = deadline - time.monotonic() - FINAL_MARGIN
    if remaining <= 0:
        return {"state": "unknown", "reason": "deadline"}
    try:
        proc = native.bounded_popen([sys.executable, "-B", os.path.abspath(__file__), "--http", "--key-file",
                                     key_file, "--timeout", str(remaining)])
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_HTTP_BYTES:
            return {"state": "unknown", "reason": "network_child_error"}
        proc.stdin.write(raw)
        proc.stdin.close()
        output = bytearray()
        while True:
            chunk = native.pipe_read(proc.stdout, 513 - len(output), deadline - FINAL_MARGIN)
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > 512:
                return {"state": "unknown", "reason": "network_child_error"}
        proc.wait(timeout=max(0.001, deadline - time.monotonic() - FINAL_MARGIN))
        value = json.loads(output)
        if (proc.returncode or not isinstance(value, dict) or set(value) != {"state", "reason"} or
                value["state"] not in {"sent", "rejected", "unknown"} or value["reason"] not in REASONS):
            return {"state": "unknown", "reason": "network_child_error"}
        return value
    except (TimeoutError, subprocess.TimeoutExpired):
        return {"state": "unknown", "reason": "deadline"}
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {"state": "unknown", "reason": "network_child_error"}
    finally:
        if proc:
            native.stop_child(proc)


def worker(runtime, codex, thread_id, turn_id):
    import socket
    import time
    from read_thread_title import read_completion_metadata
    deadline = time.monotonic() + WORKER_BUDGET
    db, gate, owned = None, None, False
    ids = None
    result = {"state": "unknown", "reason": "worker_error"}
    try:
        if not all(canonical_uuid(value) for value in (thread_id, turn_id)):
            return {"state": "skipped", "reason": "invalid_event"}
        private_runtime(runtime)
        if not active_runtime(runtime):
            return {"state": "skipped", "reason": "disabled"}
        config = settings(runtime)
        ids = (config["host_id"], thread_id, turn_id)
        db = open_state(runtime)
        owned = claim(db, *ids, time.time())
        if not owned:
            return {"state": "skipped", "reason": "duplicate"}
        try:
            timeout = min(3.5, deadline - time.monotonic() - CLEANUP_MARGIN - 0.5)
            if timeout <= 0:
                result = {"state": "unknown", "reason": "deadline"}
                return result
            metadata = read_completion_metadata(codex, thread_id, timeout)
        except (Exception, UnicodeError):
            result = {"state": "skipped", "reason": "metadata_error"}
            return result
        reason = classify(metadata)
        if reason != "eligible":
            result = {"state": "skipped", "reason": reason}
            return result
        gate = open_gate(runtime)
        if not lock_until(gate, False, deadline - CLEANUP_MARGIN):
            result = {"state": "unknown", "reason": "deadline"}
            return result
        if not active_runtime(runtime):
            result = {"state": "skipped", "reason": "disabled"}
            return result
        # Activation is rechecked while holding the shared gate until HTTP finishes.
        result = dict(network_child(config["key_file"],
                                    {"title": socket.gethostname(), "body": metadata["name"] + " 已完成"}, deadline))
        return result
    except (KeyboardInterrupt, SystemExit):
        result = {"state": "unknown", "reason": "interrupted"}
        return result
    except BaseException:
        return result
    finally:
        if owned and db:
            try:
                finish(db, ids, result["state"], result["reason"])
            except BaseException:
                # Leave a claimed processing/unknown row; a duplicate cannot send again.
                result.clear()
                result.update({"state": "unknown", "reason": "state_error"})
        if db:
            db.close()
        if gate is not None:
            os.close(gate)


def launch_worker(runtime, codex, raw):
    import subprocess
    ids = event_identity(raw)
    if ids:
        if os.name == "nt":
            native.detached_popen([sys.executable, "-B", os.path.abspath(__file__), "--worker", "--runtime", runtime,
                                  "--codex", codex, "--thread-id", ids[0], "--turn-id", ids[1]])
            return
        subprocess.Popen([sys.executable, "-B", os.path.abspath(__file__), "--worker", "--runtime", runtime,
                          "--codex", codex, "--thread-id", ids[0], "--turn-id", ids[1]],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=True)


def forward(argv):
    if len(argv) < 7 or argv[4] != "--forward":
        return 2
    original, raw = argv[5:-1], argv[-1]
    try:
        if argv[0] == "--runtime" and argv[2] == "--codex":
            launch_worker(argv[1], argv[3], raw)
    except BaseException:
        pass
    finally:
        if os.name == "nt":
            import subprocess
            return subprocess.call(original + [raw], shell=False)
        try:
            import signal
            # Match subprocess restore_signals: Python ignores these signals,
            # and ignored dispositions otherwise survive exec into the callback.
            for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
                signum = getattr(signal, name, None)
                if signum is not None:
                    signal.signal(signum, signal.SIG_DFL)
        finally:
            os.execv(original[0], original + [raw])


def main():
    argv = sys.argv[1:]
    if argv == ["--self-check"]:
        from read_thread_title import read_completion_metadata
        print('{"result":"self_check_ok"}')
        return 0
    if argv and argv[0] == "--http":
        import json
        import math
        try:
            if len(argv) != 5 or argv[1] != "--key-file" or argv[3] != "--timeout":
                return 2
            timeout = float(argv[4])
            if not math.isfinite(timeout) or not 0 < timeout <= WORKER_BUDGET:
                return 2
            raw = sys.stdin.buffer.read(MAX_HTTP_BYTES + 1)
            value = json.loads(raw)
            if len(raw) > MAX_HTTP_BYTES or not http_payload_valid(value):
                return 2
            result = http_send(argv[2], value, timeout)
        except BaseException:
            result = {"state": "unknown", "reason": "network_child_error"}
        print(json.dumps(result, separators=(",", ":")))
        return 0
    if argv and argv[0] == "--worker":
        import json
        import signal
        def interrupted(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupted)
        try:
            if (len(argv) != 9 or argv[1] != "--runtime" or argv[3] != "--codex" or
                    argv[5] != "--thread-id" or argv[7] != "--turn-id"):
                return 2
            result = worker(argv[2], argv[4], argv[6], argv[8])
            print(json.dumps(result, separators=(",", ":")))
            return 0
        except BaseException:
            return 1
    return forward(argv)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OSError:
        print('{"error":"exec_failed"}', file=sys.stderr)
        sys.exit(127)
