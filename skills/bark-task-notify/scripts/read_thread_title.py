#!/usr/bin/env python3
"""Read one existing Codex thread's display name through app-server stdio.

Only initialize and thread/read are sent. No turns are requested, resumed or
started. Raw server payloads and stderr are never printed. This POSIX prototype
uses the caller's Codex home and disables hooks only for its child invocation.
"""

import argparse
import json
import math
import os
import selectors
import signal
import subprocess
import sys
import time
import uuid


MAX_LINE_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_NAME_BYTES = 4096


class ProbeError(Exception):
    """Contains only a fixed, safe diagnostic code."""


class RpcReader:
    def __init__(self, stream, deadline):
        self.stream = stream
        self.deadline = deadline
        self.buffer = bytearray()
        self.total = 0
        self.selector = selectors.DefaultSelector()
        self.selector.register(stream, selectors.EVENT_READ)

    def close(self):
        self.selector.close()

    def response(self, request_id):
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError("timeout")
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                if newline > MAX_LINE_BYTES:
                    raise ProbeError("response_too_large")
                line = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    raise ProbeError("invalid_response") from None
                if not isinstance(message, dict):
                    raise ProbeError("invalid_response")
                if "method" in message:
                    # A read must never require approval, tool work or input.
                    if "id" in message:
                        raise ProbeError("unexpected_server_request")
                    continue
                if type(message.get("id")) is not int or message["id"] != request_id:
                    raise ProbeError("unexpected_response_id")
                if "error" in message:
                    raise ProbeError("rpc_error")
                if not isinstance(message.get("result"), dict):
                    raise ProbeError("invalid_response")
                return message["result"]
            if len(self.buffer) > MAX_LINE_BYTES:
                raise ProbeError("response_too_large")
            if not self.selector.select(remaining):
                raise ProbeError("timeout")
            chunk = os.read(self.stream.fileno(), 16384)
            if not chunk:
                raise ProbeError("server_closed")
            self.total += len(chunk)
            if self.total > MAX_TOTAL_BYTES:
                raise ProbeError("response_too_large")
            self.buffer.extend(chunk)


def send(proc, message):
    # All outbound messages are small fixed requests with a validated UUID.
    data = (json.dumps(message, separators=(",", ":")) + "\n").encode()
    proc.stdin.write(data)
    proc.stdin.flush()


def stop(proc):
    """Terminate the isolated process group, including surviving children."""
    if proc.stdin:
        proc.stdin.close()
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass
    # The leader can exit before its children; still kill the group.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        raise ProbeError("cleanup_failed") from None
    finally:
        if proc.stdout:
            proc.stdout.close()


def _read_thread(codex, thread_id, timeout):
    if os.name != "posix":
        raise ProbeError("unsupported_platform")
    deadline = time.monotonic() + timeout
    proc = None
    reader = None
    try:
        # Defer interruption until the child is assigned and can be cleaned up.
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            proc = subprocess.Popen(
                [codex, "app-server", "--listen", "stdio://", "-c",
                 "features.hooks=false", "-c", "analytics.enabled=false"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0,
                # This CLI is single-threaded. Restore the child's signal mask
                # before exec so it receives TERM during cleanup.
                preexec_fn=lambda: signal.pthread_sigmask(
                    signal.SIG_SETMASK, previous_mask),
            )
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        reader = RpcReader(proc.stdout, deadline)
        send(proc, {"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "bark-title-probe", "version": "1"},
            "capabilities": {},
        }})
        reader.response(1)
        send(proc, {"method": "initialized", "params": {}})
        send(proc, {"id": 2, "method": "thread/read", "params": {
            "threadId": thread_id, "includeTurns": False,
        }})
        result = reader.response(2)
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise ProbeError("thread_id_mismatch")
        if thread.get("turns") not in (None, []):
            raise ProbeError("unexpected_turns")
        return thread
    finally:
        if reader:
            reader.close()
        if proc:
            previous_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
            try:
                stop(proc)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def read_completion_metadata(codex, thread_id, timeout):
    """Read source and exact display name in one request without emitting text."""
    thread = _read_thread(codex, thread_id, timeout)
    return {"source": thread.get("source"),
            "threadSource": thread.get("threadSource"), "name": thread.get("name")}
