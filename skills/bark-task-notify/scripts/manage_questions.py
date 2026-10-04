#!/usr/bin/env python3
"""Manage the single owned async-question hook without changing native trust."""
import argparse
import json
import os
import shlex
import sqlite3
import sys
import time

sys.dont_write_bytecode = True
import manage as completion
import notify as base
import question

FILES = ("question.py", "manage_questions.py")
OWNERSHIP = "questions-ownership.json"
ACTIVATION = "questions-activation.json"


def decode(raw):
    def reject_constant(_value):
        raise ValueError("invalid_json")
    return json.loads(raw, object_pairs_hook=question.unique_object, parse_constant=reject_constant)


def completion_install(runtime, codex):
    completion.private_directory(runtime)
    raw, _info = completion.read_regular(os.path.join(runtime, "ownership.json"), private=True)
    try:
        config = decode(raw)["config"]
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        completion.fail("invalid_manifest")
    manifest = completion.installed(runtime, config)
    if manifest["codex"] != codex:
        completion.fail("install_mismatch")
    return manifest


def owned_entry(runtime, codex, python):
    argv = [python, "-B", os.path.join(runtime, "question.py"), "--hook", "--runtime", runtime, "--codex", codex]
    if os.name == "nt":
        import base64
        # Encode a PowerShell call with single-quoted argv. The outer hook shell
        # sees no path metacharacters; stdin and the payload exit status survive.
        ps = "& " + " ".join("'" + item.replace("'", "''") + "'" for item in argv) + "; exit $LASTEXITCODE"
        command = "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
        return {"matcher": "^request_user_input_async$", "hooks": [
            {"type": "command", "command": command, "commandWindows": command, "timeout": 3}]}
    command = shlex.join(argv)
    return {"matcher": "^request_user_input_async$", "hooks": [{"type": "command", "command": command, "timeout": 3}]}


def installed(runtime, hooks, codex, python):
    try:
        raw, _info = completion.read_regular(os.path.join(runtime, OWNERSHIP), private=True)
    except FileNotFoundError:
        return None
    try:
        value = decode(raw)
        fields = {"version", "runtime", "hooks", "codex", "python", "entry", "hashes"}
        if (not isinstance(value, dict) or set(value) != fields or value["version"] != 1 or
                value["runtime"] != runtime or value["hooks"] != hooks or value["codex"] != codex or value["python"] != python or
                value["entry"] != owned_entry(runtime, codex, python) or not isinstance(value["hashes"], dict) or
                set(value["hashes"]) != set(FILES)):
            completion.fail("invalid_manifest")
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        completion.fail("invalid_manifest")


def code_intact(runtime, manifest):
    try:
        return all(completion.digest(completion.read_regular(os.path.join(runtime, name), private=True)[0]) == manifest["hashes"][name]
                   for name in FILES)
    except (completion.InstallError, OSError):
        return False


def question_enabled(runtime):
    try:
        value = decode(base.read_private(os.path.join(runtime, ACTIVATION), 1024))
        return isinstance(value, dict) and set(value) == {"enabled"} and value["enabled"] is True
    except (base.RuntimeErrorCode, OSError, ValueError, UnicodeError, RecursionError):
        return False


def set_active(runtime, enabled):
    completion.set_active(runtime, enabled, name=ACTIVATION)


def read_hooks(path):
    try:
        raw, info = completion.read_regular(path)
    except FileNotFoundError:
        return {}, None, None
    try:
        document = decode(raw)
        if (not isinstance(document, dict) or ("hooks" in document and not isinstance(document["hooks"], dict)) or
                ("PostToolUse" in document.get("hooks", {}) and not isinstance(document["hooks"]["PostToolUse"], list))):
            completion.fail("invalid_hooks")
        return document, raw, info
    except (ValueError, TypeError, UnicodeError, RecursionError):
        completion.fail("invalid_hooks")


def references_owned(group, runtime):
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return False
    script = os.path.join(runtime, "question.py")
    for hook in group["hooks"]:
        command = hook.get("command") if isinstance(hook, dict) else None
        if isinstance(command, str) and script in command:
            return True
        if os.name == "nt" and isinstance(hook, dict):
            import base64
            for command in (hook.get("command"), hook.get("commandWindows")):
                if isinstance(command, str) and " -EncodedCommand " in command:
                    try:
                        decoded = base64.b64decode(command.split(" -EncodedCommand ", 1)[1], validate=True).decode("utf-16-le")
                    except (ValueError, UnicodeError):
                        continue
                    if script.replace("'", "''") in decoded:
                        return True
    return False


def hook_state(document, manifest, runtime):
    groups = document.get("hooks", {}).get("PostToolUse", [])
    exact = sum(group == manifest["entry"] for group in groups)
    altered = any(group != manifest["entry"] and references_owned(group, runtime) for group in groups)
    return exact, altered


def write_hooks(path, document, raw, info):
    data = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if raw is None:
        # O_EXCL rechecks absence instead of overwriting a concurrently created file.
        completion.create_file(path, data)
        if os.name == "nt":
            completion.native.sync_directory(os.path.dirname(path))
            return
        directory_fd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    else:
        completion.replace_checked(path, raw, info, data)


def install_new(runtime, hooks, codex, python):
    if any(os.path.lexists(os.path.join(runtime, name)) for name in FILES + (OWNERSHIP, ACTIVATION, "questions.sqlite")):
        completion.fail("runtime_conflict")
    source_root = os.path.dirname(os.path.abspath(__file__))
    sources = {}
    for name in FILES:
        data, _info = completion.read_regular(os.path.join(source_root, name))
        try:
            compile(data, name, "exec")
        except (SyntaxError, ValueError):
            completion.fail("source_invalid")
        sources[name] = data
    manifest = {"version": 1, "runtime": runtime, "hooks": hooks, "codex": codex, "python": python,
                "entry": owned_entry(runtime, codex, python),
                "hashes": {name: completion.digest(data) for name, data in sources.items()}}
    for name, data in sources.items():
        completion.create_file(os.path.join(runtime, name), data)
    completion.create_file(os.path.join(runtime, OWNERSHIP), (json.dumps(manifest, ensure_ascii=True) + "\n").encode("ascii"))
    set_active(runtime, False)
    completion.create_file(os.path.join(runtime, "questions.sqlite"), b"")
    return manifest


def self_check(runtime, python):
    import subprocess
    try:
        result = subprocess.run([python, "-B", os.path.join(runtime, "question.py"), "--self-check"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3, check=False)
        if result.returncode or result.stdout.strip() != b'{"result":"self_check_ok"}':
            completion.fail("self_check_failed")
    except (OSError, subprocess.TimeoutExpired):
        completion.fail("self_check_failed")


def state_summary(runtime):
    path = os.path.join(runtime, "questions.sqlite")
    completion._check_state_file(path)
    db = sqlite3.connect(path, timeout=0.5)
    try:
        if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='questions'").fetchone():
            return {"counts": {}, "recent": []}
        now = time.time()
        db.execute("UPDATE questions SET state='unknown',reason='stale_processing',finished_at=? "
                   "WHERE state='processing' AND started_at<?", (now, now - base.STALE_AFTER))
        db.commit()
        counts = {state: count for state, count in db.execute("SELECT state,COUNT(*) FROM questions GROUP BY state") if state in base.STATES}
        recent = []
        for host, thread, turn, call, state, reason, started, finished in db.execute(
                "SELECT host_id,thread_id,turn_id,tool_use_id,state,reason,started_at,finished_at "
                "FROM questions ORDER BY started_at DESC LIMIT 20"):
            if (all(base.canonical_uuid(value) for value in (host, thread, turn)) and question.tool_id_valid(call) and
                    state in base.STATES and reason in base.REASONS):
                recent.append({"host_id": host, "thread_id": thread, "turn_id": turn, "tool_use_id": call,
                               "state": state, "reason": reason, "started_at": started, "finished_at": finished})
        return {"counts": counts, "recent": recent}
    finally:
        db.close()


def manage(action, hooks, runtime, codex=completion.DEFAULT_CODEX):
    manifest = completion_install(runtime, codex)
    python = manifest["python"]
    completion.no_links(hooks)
    deadline = time.monotonic() + completion.LOCK_BUDGET
    lock = completion.lock_file(hooks + ".bark-task-notify-questions.lock", deadline)
    gate = None
    try:
        own = installed(runtime, hooks, codex, python)
        if action != "enable" and own is None:
            return {"result": "not_installed", "active": False, "trust_status": "unknown"}
        if action in ("enable", "disable"):
            gate = completion.lock_file(os.path.join(runtime, "delivery.lock"), deadline)
        was_enabled = question_enabled(runtime)
        if action == "disable":
            set_active(runtime, False)
        try:
            document, raw, info = read_hooks(hooks)
        except (completion.InstallError, OSError):
            if action == "disable":
                return {"result": "disabled_config_conflict", "active": False, "trust_status": "unknown"}
            if action == "enable" and own is not None:
                set_active(runtime, False)
            raise
        if own is None:
            # A recognizable foreign wrapper cannot become this install's owned hook.
            if any(references_owned(group, runtime) for group in document.get("hooks", {}).get("PostToolUse", [])):
                completion.fail("hook_conflict")
            if not completion.code_intact(runtime, manifest):
                completion.fail("install_mismatch")
            config = base.settings(runtime)
            if config["host_id"] != manifest["host_id"]:
                completion.fail("install_mismatch")
            completion.key_reference(config["key_file"])
            own = install_new(runtime, hooks, codex, python)
        exact, altered = hook_state(document, own, runtime)
        conflict = altered or exact > 1 or (exact == 0 and was_enabled)
        if action == "disable":
            if conflict:
                return {"result": "disabled_config_conflict", "active": False, "trust_status": "unknown"}
            if exact == 1:
                groups = document["hooks"]["PostToolUse"]
                document["hooks"]["PostToolUse"] = [group for group in groups if group != own["entry"]]
                write_hooks(hooks, document, raw, info)
            return {"result": "disabled" if exact else "already_disabled", "active": False, "trust_status": "unknown"}
        if action == "status":
            result = "config_conflict" if conflict else "configured" if exact else "disabled"
            if exact and not code_intact(runtime, own):
                result = "configured_but_damaged"
            try:
                summary = state_summary(runtime)
            except (completion.InstallError, OSError, ValueError, sqlite3.Error):
                summary = {"counts": {}, "recent": [], "state_error": "state_unavailable"}
            return dict({"result": result, "active": question.active_questions(runtime), "trust_status": "unknown",
                         "trust_action": "/hooks"}, **summary)
        if action != "enable":
            completion.fail("invalid_action")
        if conflict:
            set_active(runtime, False)
            return {"result": "disabled_config_conflict", "active": False, "trust_status": "unknown"}
        if not completion.code_intact(runtime, manifest) or not code_intact(runtime, own):
            completion.fail("install_mismatch")
        config = base.settings(runtime)
        if config["host_id"] != manifest["host_id"]:
            completion.fail("install_mismatch")
        completion.key_reference(config["key_file"])
        completion.executable(python)
        completion.executable(codex)
        self_check(runtime, python)
        if exact == 0:
            document.setdefault("hooks", {}).setdefault("PostToolUse", []).append(own["entry"])
            write_hooks(hooks, document, raw, info)
        set_active(runtime, True)
        return {"result": "configured", "active": question.active_questions(runtime), "trust_status": "unknown", "trust_action": "/hooks"}
    finally:
        if gate is not None:
            os.close(gate)
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("enable", "disable", "status"))
    parser.add_argument("--hooks", default=os.path.expanduser("~/.codex/hooks.json"))
    parser.add_argument("--runtime", default=completion.DEFAULT_RUNTIME)
    parser.add_argument("--codex", default=completion.DEFAULT_CODEX)
    args = parser.parse_args()
    try:
        result = manage(args.action, os.path.abspath(args.hooks), os.path.abspath(args.runtime), os.path.abspath(args.codex))
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        return 0
    except completion.InstallError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
    except (OSError, ValueError, UnicodeError, KeyError, sqlite3.Error, base.RuntimeErrorCode):
        print('{"error":"io_error"}', file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
