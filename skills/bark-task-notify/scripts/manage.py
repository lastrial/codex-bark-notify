#!/usr/bin/env python3
"""Enable, disable, inspect or explicitly test the private completion notifier.

Only an existing first-line JSON-compatible notify array is supported. Config
changes preserve unrelated bytes and recheck file identity before replacement.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid

if os.name == "nt":
    import windows_native as native
else:
    import fcntl

sys.dont_write_bytecode = True
DEFAULT_CODEX = native.default_codex() if os.name == "nt" else "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
DEFAULT_RUNTIME = (os.path.join(os.environ["LOCALAPPDATA"], "bark-task-notify") if os.name == "nt"
                   else os.path.expanduser("~/.local/share/bark-task-notify"))
MAX_CONFIG = 1024 * 1024
FILES = ("notify.py", "read_thread_title.py", "manage.py") + (("windows_native.py",) if os.name == "nt" else ())
LOCK_BUDGET = 13.0
STATES = {"processing", "sent", "rejected", "unknown", "skipped"}
REASONS = {"claimed", "accepted", "http_rejected", "api_rejected", "transport_error", "http_unknown",
           "invalid_response", "response_too_large", "key_invalid", "deadline", "interrupted",
           "stale_processing", "source_skip", "child_skip", "thread_source_skip", "title_unavailable",
           "title_too_large", "metadata_error", "disabled", "worker_error", "network_child_error", "state_error"}
STALE_AFTER = 12.2


class InstallError(Exception):
    pass


def fail(code):
    raise InstallError(code)


def no_links(path):
    if os.name == "nt":
        return native.no_links(path)
    current = os.path.abspath(path)
    while True:
        if os.path.islink(current):
            fail("unsafe_path")
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def read_regular(path, maximum=MAX_CONFIG, private=False):
    if os.name == "nt":
        return native.read_regular(path, maximum, private)
    no_links(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or
                (private and stat.S_IMODE(info.st_mode) != 0o600)):
            fail("unsafe_file")
        data = os.read(fd, maximum + 1)
        if len(data) > maximum:
            fail("file_too_large")
        return data, info
    finally:
        os.close(fd)


def private_directory(path):
    if os.name == "nt":
        return native.private_directory(path)
    no_links(path)
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        fail("unsafe_runtime")


def notify_line(data):
    try:
        # Split only CR/LF; Unicode separators and DEL may be valid argv data.
        text = data.decode("utf-8")
        boundary = text.find("\n")
        line = text if boundary < 0 else text[:boundary + 1]
        tail = b"" if boundary < 0 else text[boundary + 1:].encode("utf-8")
        match = re.fullmatch(r"notify[ \t]*=[ \t]*(\[[^\r\n]*\])[ \t]*(?:\r?\n)?", line)
        if not match or re.search(r"(?m)^[ \t]*(?:notify|[\"']notify[\"'])[ \t]*=", tail.decode("utf-8")):
            fail("unsupported_notify")
        argv = json.loads(match.group(1))
        if (not isinstance(argv, list) or not argv or not all(isinstance(item, str) and "\0" not in item for item in argv)
                or not os.path.isabs(argv[0])):
            fail("unsupported_notify")
        return line, argv, tail
    except (ValueError, UnicodeError, TypeError, RecursionError):
        fail("unsupported_notify")


def notify_array(argv):
    return json.dumps(argv, ensure_ascii=False).replace("\x7f", "\\u007f")


def executable(path):
    if os.name == "nt":
        no_links(path)
        # Batch files may route through cmd.exe even with shell=False. Require
        # a native executable; scripts can be arguments to a native interpreter.
        if os.path.splitext(path)[1].lower() not in (".exe", ".com"):
            fail("invalid_executable")
    if not os.path.isabs(path) or not os.path.isfile(path) or not os.access(path, os.X_OK):
        fail("invalid_executable")


def key_reference(path):
    # Check metadata only. Reading the secret belongs exclusively to the HTTP child.
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        fail("key_file_required")
    no_links(path)
    if os.name == "nt":
        handle, info = native.open_handle(path, private=True)
        native.CloseHandle(handle)
        if not 0 < info.st_size <= 512:
            fail("unsafe_key_file")
        return
    info = os.lstat(path)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or
            stat.S_IMODE(info.st_mode) != 0o600 or not 0 < info.st_size <= 512):
        fail("unsafe_key_file")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def create_file(path, data):
    if os.name == "nt":
        return native.create_file(path, data)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def replace_checked(path, before, info, after, private=False):
    if os.name == "nt":
        return native.replace_checked(path, before, info, after, private)
    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(prefix=".bark-task-notify-", dir=directory)
    try:
        os.fchmod(fd, 0o600 if private else stat.S_IMODE(info.st_mode))
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(after)
            stream.flush()
            os.fsync(fd)
        current, latest = read_regular(path)
        if (current != before or latest.st_ino != info.st_ino or latest.st_dev != info.st_dev or
                stat.S_IMODE(latest.st_mode) != stat.S_IMODE(info.st_mode)):
            fail("config_changed")
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(fd)
        if os.path.lexists(temporary):
            os.unlink(temporary)


def set_private_json(path, value):
    data = (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    try:
        before, info = read_regular(path)
    except FileNotFoundError:
        create_file(path, data)
    else:
        replace_checked(path, before, info, data, private=True)


def set_active(runtime, active, name="activation.json"):
    # Ownership comes from the valid manifest and private runtime. The contents
    # may be missing, malformed or oversized; disabling never needs to read them.
    if name not in ("activation.json", "questions-activation.json"):
        fail("invalid_activation")
    path = os.path.join(runtime, name)
    data = (json.dumps({"enabled": active}, separators=(",", ":")) + "\n").encode("ascii")
    if os.name == "nt":
        try:
            handle, info = native.open_handle(path)
        except FileNotFoundError:
            create_file(path, data)
        else:
            native.CloseHandle(handle)
            native.replace_metadata(path, info, data)
        return
    no_links(path)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        create_file(path, data)
        return
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1:
        fail("unsafe_file")
    fd, temporary = tempfile.mkstemp(prefix=".activation-", dir=runtime)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
        no_links(path)
        latest = os.lstat(path)
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode, info.st_nlink)
        if identity(latest) != identity(before) or latest.st_uid != os.getuid():
            fail("config_changed")
        os.replace(temporary, path)
        directory_fd = os.open(runtime, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(fd)
        if os.path.lexists(temporary):
            os.unlink(temporary)


def activation_enabled(runtime):
    try:
        raw, _info = read_regular(os.path.join(runtime, "activation.json"), maximum=1024, private=True)
        value = json.loads(raw)
        return isinstance(value, dict) and set(value) == {"enabled"} and value["enabled"] is True
    except (InstallError, OSError, ValueError, UnicodeError, RecursionError):
        return False


def expected_line(runtime, codex, python, original_line, argv):
    newline = "\r\n" if original_line.endswith("\r\n") else ("\n" if original_line.endswith("\n") else "")
    owned_argv = [python, os.path.join(runtime, "notify.py"), "--runtime", runtime,
                  "--codex", codex, "--forward"] + argv
    return "notify = " + notify_array(owned_argv) + newline


def installed(runtime, config):
    private_directory(runtime)
    raw, _info = read_regular(os.path.join(runtime, "ownership.json"), private=True)
    try:
        value = json.loads(raw)
        fields = {"version", "runtime", "config", "codex", "python", "original_line", "owned_line", "hashes", "host_id"}
        if (not isinstance(value, dict) or set(value) != fields or value["version"] != 1 or
                value["runtime"] != runtime or value["config"] != config or
                not all(isinstance(value[key], str) and os.path.isabs(value[key]) for key in ("python", "codex")) or
                not isinstance(value["hashes"], dict) or set(value["hashes"]) != set(FILES) or
                str(uuid.UUID(value["host_id"])) != value["host_id"]):
            fail("invalid_manifest")
        original, argv, tail = notify_line(value["original_line"].encode("utf-8"))
        if (tail or original != value["original_line"] or
                value["owned_line"] != expected_line(runtime, value["codex"], value["python"], original, argv)):
            fail("invalid_manifest")
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, AttributeError, RecursionError):
        fail("invalid_manifest")


def code_intact(runtime, manifest):
    try:
        return all(digest(read_regular(os.path.join(runtime, name), private=True)[0]) == manifest["hashes"][name]
                   for name in FILES)
    except (InstallError, OSError):
        return False


def self_check(runtime, python):
    try:
        result = subprocess.run([python, "-B", os.path.join(runtime, "notify.py"), "--self-check"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=3, check=False)
        if result.returncode or result.stdout.strip() != b'{"result":"self_check_ok"}':
            fail("self_check_failed")
    except (OSError, subprocess.TimeoutExpired):
        fail("self_check_failed")


def install_new(runtime, config, codex, python, original_line, original_argv, key_file):
    no_links(runtime)
    if os.path.lexists(runtime):
        fail("runtime_conflict")
    sources = {}
    source_root = os.path.dirname(os.path.abspath(__file__))
    for name in FILES:
        data, _info = read_regular(os.path.join(source_root, name))
        try:
            compile(data, name, "exec")
        except (SyntaxError, ValueError):
            fail("source_invalid")
        sources[name] = data
    if os.name == "nt":
        native.makedirs(os.path.dirname(runtime))
        native.mkdir(runtime)
    else:
        os.makedirs(os.path.dirname(runtime), mode=0o700, exist_ok=True)
    no_links(runtime)
    if os.name != "nt":
        os.mkdir(runtime, 0o700)
        os.chmod(runtime, 0o700)
    host_id = str(uuid.uuid4())
    value = {"version": 1, "runtime": runtime, "config": config, "codex": codex, "python": python,
             "original_line": original_line,
             "owned_line": expected_line(runtime, codex, python, original_line, original_argv),
             "hashes": {name: digest(data) for name, data in sources.items()}, "host_id": host_id}
    for name, data in sources.items():
        create_file(os.path.join(runtime, name), data)
    create_file(os.path.join(runtime, "ownership.json"), (json.dumps(value, ensure_ascii=True) + "\n").encode("ascii"))
    set_private_json(os.path.join(runtime, "config.json"), {"host_id": host_id, "key_file": key_file})
    set_active(runtime, False)
    # Precreate the private database and delivery gate; never reset existing state.
    create_file(os.path.join(runtime, "state.sqlite"), b"")
    create_file(os.path.join(runtime, "delivery.lock"), b"")
    return value


def lock_file(path, deadline):
    if os.name == "nt":
        fd, _info = native.open_fd(path, write=True, create=True)
        try:
            if not native.lock_until(fd, True, deadline):
                fail("lock_timeout")
            return fd
        except BaseException:
            os.close(fd)
            raise
    no_links(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or
                stat.S_IMODE(info.st_mode) != 0o600):
            fail("unsafe_lock")
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    fail("lock_timeout")
                time.sleep(0.02)
    except BaseException:
        os.close(fd)
        raise


def state_summary(runtime):
    # Never query arbitrary stored text. Only validated UUIDs and fixed enums leave SQLite.
    path = os.path.join(runtime, "state.sqlite")
    _check_state_file(path)
    db = sqlite3.connect(path, timeout=0.5)
    try:
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='events'").fetchall()
        if not tables:
            return {"counts": {}, "recent": []}
        now = time.time()
        db.execute("UPDATE events SET state='unknown',reason='stale_processing',finished_at=? "
                   "WHERE state='processing' AND started_at<?", (now, now - STALE_AFTER))
        db.commit()
        counts = {state: count for state, count in db.execute("SELECT state,COUNT(*) FROM events GROUP BY state")
                  if state in STATES}
        recent = []
        for host, thread, turn, state, reason, started, finished in db.execute(
                "SELECT host_id,thread_id,turn_id,state,reason,started_at,finished_at FROM events ORDER BY started_at DESC LIMIT 20"):
            try:
                valid = all(isinstance(value, str) and str(uuid.UUID(value)) == value for value in (host, thread, turn))
            except (ValueError, TypeError, AttributeError):
                valid = False
            if valid and state in STATES and reason in REASONS:
                recent.append({"host_id": host, "thread_id": thread, "turn_id": turn, "state": state, "reason": reason,
                               "started_at": started, "finished_at": finished})
        return {"counts": counts, "recent": recent}
    finally:
        db.close()


def _check_state_file(path):
    if os.name == "nt":
        handle, _info = native.open_handle(path, private=True)
        native.CloseHandle(handle)
        return
    no_links(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or
                stat.S_IMODE(info.st_mode) != 0o600):
            fail("unsafe_file")
    finally:
        os.close(fd)


def manage(action, config, runtime, codex=DEFAULT_CODEX, python=None, key_file=None, thread_id=None, turn_id=None):
    python = python or os.path.abspath(sys.executable)
    no_links(config)
    deadline = time.monotonic() + LOCK_BUDGET
    lock = lock_file(config + ".bark-task-notify.lock", deadline)
    gate = None
    try:
        if os.path.lexists(runtime):
            manifest = installed(runtime, config)
        elif action == "enable":
            data, info = read_regular(config)
            line, argv, _tail = notify_line(data)
            executable(python)
            executable(codex)
            executable(argv[0])
            if "--forward" in argv:
                fail("notify_conflict")
            key_reference(key_file)
            manifest = install_new(runtime, config, codex, python, line, argv, key_file)
        else:
            return {"result": "not_installed"}
        # Disable relies only on validated ownership, gate and activation, so it
        # still recovers when helper scripts/settings/activation have been lost.
        if action == "disable":
            gate = lock_file(os.path.join(runtime, "delivery.lock"), deadline)
            set_active(runtime, False)
            data, info = read_regular(config)
            try:
                line, _argv, tail = notify_line(data)
            except InstallError:
                return {"result": "disabled_config_conflict"}
            if line == manifest["owned_line"]:
                replace_checked(config, data, info, manifest["original_line"].encode("utf-8") + tail)
                return {"result": "disabled"}
            return {"result": "already_disabled" if line == manifest["original_line"] else "disabled_config_conflict"}
        data, info = read_regular(config)
        line, _argv, tail = notify_line(data)
        owned = line == manifest["owned_line"]
        original = line == manifest["original_line"]
        if action == "status":
            state = ("config_conflict" if not owned and not original else "disabled" if not owned else
                     "enabled_but_damaged" if not code_intact(runtime, manifest) else
                     "enabled_but_inactive" if not activation_enabled(runtime) else "enabled")
            try:
                summary = state_summary(runtime)
            except (InstallError, OSError, sqlite3.Error, ValueError):
                summary = {"counts": {}, "recent": [], "state_error": "state_unavailable"}
            return dict({"result": state, "active": activation_enabled(runtime)}, **summary)
        if not owned and not original:
            fail("notify_conflict")
        if manifest["codex"] != codex or manifest["python"] != python or not code_intact(runtime, manifest):
            fail("install_mismatch")
        if action == "enable":
            gate = lock_file(os.path.join(runtime, "delivery.lock"), deadline)
            executable(python)
            executable(codex)
            original_argv = notify_line(manifest["original_line"].encode("utf-8"))[1]
            executable(original_argv[0])
            if key_file is None:
                raw, _info = read_regular(os.path.join(runtime, "config.json"), private=True)
                key_file = json.loads(raw)["key_file"]
            key_reference(key_file)
            self_check(runtime, python)
            set_private_json(os.path.join(runtime, "config.json"), {"host_id": manifest["host_id"], "key_file": key_file})
            if not owned:
                replace_checked(config, data, info, manifest["owned_line"].encode("utf-8") + tail)
            set_active(runtime, True)
            return {"result": "already_enabled" if owned else "enabled"}
        if action == "test":
            from notify import canonical_uuid, worker
            import signal
            if not all(canonical_uuid(value) for value in (thread_id, turn_id)):
                fail("completed_event_required")
            if not owned or not activation_enabled(runtime):
                fail("not_enabled")
            def interrupted(_signum, _frame):
                raise KeyboardInterrupt
            previous = signal.signal(signal.SIGTERM, interrupted)
            try:
                return dict({"result": "test_finished"}, **worker(runtime, codex, thread_id, turn_id))
            finally:
                signal.signal(signal.SIGTERM, previous)
        fail("invalid_action")
    finally:
        if gate is not None:
            os.close(gate)
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("enable", "disable", "status", "test"))
    parser.add_argument("--config", default=os.path.expanduser("~/.codex/config.toml"))
    parser.add_argument("--runtime", default=DEFAULT_RUNTIME)
    parser.add_argument("--codex", default=DEFAULT_CODEX)
    parser.add_argument("--key-file")
    parser.add_argument("--thread-id")
    parser.add_argument("--turn-id")
    args = parser.parse_args()
    if args.action == "test" and (not args.thread_id or not args.turn_id):
        parser.error("test requires --thread-id and --turn-id from a known completed event")
    try:
        value = manage(args.action, os.path.abspath(args.config), os.path.abspath(args.runtime),
                       os.path.abspath(args.codex), key_file=os.path.abspath(args.key_file) if args.key_file else None,
                       thread_id=args.thread_id, turn_id=args.turn_id)
        print(json.dumps(value, ensure_ascii=True, separators=(",", ":")))
        return 0
    except InstallError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
    except (OSError, ValueError, UnicodeError, sqlite3.Error, KeyError):
        print('{"error":"io_error"}', file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
