"""Narrow Windows primitives for the notifier; no process-wide monkeypatches.

Files are validated through their opened handles. A bootstrap waits for a byte
before executing any payload, so Job assignment always precedes execution.
"""
import ctypes as c
from ctypes import wintypes as w
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from contextlib import contextmanager

if os.name != "nt":
    raise ImportError("windows_only")

k = c.WinDLL("kernel32", use_last_error=True)
a = c.WinDLL("advapi32", use_last_error=True)
P = c.c_void_p
DWORD = w.DWORD

def api(dll, name, restype, args):
    f = getattr(dll, name)
    f.restype, f.argtypes = restype, args
    return f

CloseHandle = api(k, "CloseHandle", w.BOOL, [P])
LocalFree = api(k, "LocalFree", P, [P])
CreateFile = api(k, "CreateFileW", P, [w.LPCWSTR, DWORD, DWORD, P, DWORD, DWORD, P])
GetFileType = api(k, "GetFileType", DWORD, [P])
GetSecurityInfo = api(a, "GetSecurityInfo", DWORD, [P, c.c_int, DWORD, c.POINTER(P), P, c.POINTER(P), P, c.POINTER(P)])
GetSecurityDescriptorLength = api(a, "GetSecurityDescriptorLength", DWORD, [P])
GetAce = api(a, "GetAce", w.BOOL, [P, DWORD, c.POINTER(P)])
EqualSid = api(a, "EqualSid", w.BOOL, [P, P])
ConvertSid = api(a, "ConvertSidToStringSidW", w.BOOL, [P, c.POINTER(P)])
ConvertSD = api(a, "ConvertStringSecurityDescriptorToSecurityDescriptorW", w.BOOL, [w.LPCWSTR, DWORD, c.POINTER(P), c.POINTER(DWORD)])
SetSecurity = api(a, "SetKernelObjectSecurity", w.BOOL, [P, DWORD, P])

class SA(c.Structure):
    _fields_ = [("length", DWORD), ("descriptor", P), ("inherit", w.BOOL)]

class FILEINFO(c.Structure):
    _fields_ = [("attributes", DWORD), ("created", w.FILETIME), ("accessed", w.FILETIME),
                ("modified", w.FILETIME), ("volume", DWORD), ("size_hi", DWORD),
                ("size_lo", DWORD), ("links", DWORD), ("index_hi", DWORD), ("index_lo", DWORD)]

GetInfo = api(k, "GetFileInformationByHandle", w.BOOL, [P, c.POINTER(FILEINFO)])
CreateDirectory = api(k, "CreateDirectoryW", w.BOOL, [w.LPCWSTR, c.POINTER(SA)])

def checked(ok):
    if not ok:
        raise c.WinError(c.get_last_error())
    return ok

def current_sid():
    token = P()
    OpenToken = api(a, "OpenProcessToken", w.BOOL, [P, DWORD, c.POINTER(P)])
    GetToken = api(a, "GetTokenInformation", w.BOOL, [P, c.c_int, P, DWORD, c.POINTER(DWORD)])
    checked(OpenToken(P(-1), 8, c.byref(token)))
    try:
        size = DWORD()
        GetToken(token, 1, None, 0, c.byref(size))
        buf = c.create_string_buffer(size.value)
        checked(GetToken(token, 1, buf, size, c.byref(size)))
        sid = c.cast(buf, c.POINTER(P))[0]
        out = P()
        checked(ConvertSid(sid, c.byref(out)))
        try:
            return c.wstring_at(out)
        finally:
            LocalFree(out)
    finally:
        CloseHandle(token)

SID = current_sid()

def descriptor(sddl):
    sd = P()
    checked(ConvertSD(sddl, 1, c.byref(sd), None))
    return sd

def no_links(path):
    # Exclude UNC, device namespaces, drive-relative paths and NTFS streams.
    path = os.fspath(path)
    drive, tail = os.path.splitdrive(path)
    if (not os.path.isabs(path) or len(drive) != 2 or drive[1] != ":" or
            ":" in tail or "\0" in path or path.startswith(("\\\\", "//"))):
        raise OSError("unsafe_path")
    current = os.path.abspath(path)
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            pass
        else:
            if info.st_file_attributes & 0x400:
                raise OSError("unsafe_path")
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

@dataclass(frozen=True)
class Info:
    st_dev: int
    st_ino: int
    st_size: int
    st_mtime_ns: int
    st_nlink: int
    security: bytes

def inspect(handle, private=False, directory=False):
    fi = FILEINFO()
    checked(GetInfo(handle, c.byref(fi)))
    if (GetFileType(handle) != 1 or fi.attributes & 0x400 or
            bool(fi.attributes & 0x10) != directory or (not directory and fi.links != 1)):
        raise OSError("unsafe_file")
    owner, acl, sd = P(), P(), P()
    code = GetSecurityInfo(handle, 1, 5, c.byref(owner), None, c.byref(acl), None, c.byref(sd))
    if code:
        raise c.WinError(code)
    try:
        out = P()
        checked(ConvertSid(owner, c.byref(out)))
        try:
            if c.wstring_at(out) != SID:
                raise OSError("unsafe_owner")
        finally:
            LocalFree(out)
        if private:
            if not acl:
                raise OSError("unsafe_acl")
            count = c.c_ushort.from_address(acl.value + 4).value
            allowed = False
            for index in range(count):
                ace = P()
                checked(GetAce(acl, index, c.byref(ace)))
                kind = c.c_ubyte.from_address(ace.value).value
                flags = c.c_ubyte.from_address(ace.value + 1).value
                # Reject object/callback/unknown ACEs rather than guessing layout.
                if kind not in (0, 1) or not EqualSid(P(ace.value + 8), owner):
                    raise OSError("unsafe_acl")
                if kind == 0 and not flags & 8:  # INHERIT_ONLY does not grant this object.
                    allowed = True
            if not allowed:
                raise OSError("unsafe_acl")
        security = c.string_at(sd, GetSecurityDescriptorLength(sd))
        modified = (fi.modified.dwHighDateTime << 32) | fi.modified.dwLowDateTime
        return Info(fi.volume, (fi.index_hi << 32) | fi.index_lo,
                    (fi.size_hi << 32) | fi.size_lo, modified * 100, fi.links, security)
    finally:
        LocalFree(sd)

@contextmanager
def pinned_parents(path):
    """Prevent ancestor renames/junction swaps throughout an operation."""
    no_links(path)
    parents = []
    current = os.path.dirname(os.path.abspath(path))
    while True:
        parents.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    handles = []
    try:
        for parent in reversed(parents):
            handle = CreateFile(parent, 0x80, 3, None, 3, 0x2200000, None)
            if handle == P(-1).value:
                raise c.WinError(c.get_last_error())
            handles.append(handle)
            fi = FILEINFO()
            checked(GetInfo(handle, c.byref(fi)))
            if not fi.attributes & 0x10 or fi.attributes & 0x400:
                raise OSError("unsafe_path")
        yield
    finally:
        for handle in reversed(handles):
            CloseHandle(handle)

def open_handle(path, write=False, create=False, private=False, directory=False, exclusive=False, write_dac=False):
    with pinned_parents(path):
        return _open_handle(path, write, create, private, directory, exclusive, write_dac)

def _open_handle(path, write, create, private, directory, exclusive, write_dac):
    no_links(path)
    sd = descriptor("O:" + SID + "D:P(A;OICI;FA;;;" + SID + ")") if create else None
    sa = SA(c.sizeof(SA), sd, False) if sd else None
    try:
        handle = CreateFile(path, 0x20000 | (0x40000 if write_dac else 0) | (0xC0000000 if write else 0x80000000),
                            7, c.byref(sa) if sa else None, 1 if exclusive else 4 if create else 3,
                            0x200000 | (0x2000000 if directory else 0), None)
        if handle == P(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            info = inspect(handle, private, directory)
            # Recheck ancestors after opening, as well as the target handle.
            no_links(path)
            return handle, info
        except BaseException:
            CloseHandle(handle)
            raise
    finally:
        if sd:
            LocalFree(sd)

def open_fd(path, write=False, create=False, private=True, exclusive=False):
    import msvcrt
    handle, info = open_handle(path, write, create, private, exclusive=exclusive)
    try:
        fd = msvcrt.open_osfhandle(handle, (os.O_RDWR if write else os.O_RDONLY) | os.O_BINARY)
        return fd, info
    except BaseException:
        CloseHandle(handle)
        raise

def read_regular(path, maximum, private=False):
    fd, info = open_fd(path, private=private)
    try:
        data = os.read(fd, maximum + 1)
        if len(data) > maximum:
            raise OSError("file_too_large")
        return data, info
    finally:
        os.close(fd)

def private_directory(path):
    handle, _ = open_handle(path, private=True, directory=True)
    CloseHandle(handle)

def mkdir(path):
    with pinned_parents(path):
        return _mkdir(path)

def _mkdir(path):
    no_links(path)
    sd = descriptor("O:" + SID + "D:P(A;OICI;FA;;;" + SID + ")")
    try:
        sa = SA(c.sizeof(SA), sd, False)
        checked(CreateDirectory(path, c.byref(sa)))
    finally:
        LocalFree(sd)
    private_directory(path)

def makedirs(path):
    no_links(path)
    if os.path.isdir(path):
        return
    makedirs(os.path.dirname(path))
    try:
        mkdir(path)
    except FileExistsError:
        private_directory(path)

def create_file(path, data):
    fd, _ = open_fd(path, write=True, create=True, exclusive=True)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)

def sync_directory(path):
    # FlushFileBuffers on a directory is unsupported by ordinary NTFS handles.
    # File data is flushed before atomic replacement; validate the directory here.
    no_links(path)

def replace_checked(path, before, info, after, private=False):
    with pinned_parents(path):
        return _replace_checked(path, before, info, after, private)

def replace_metadata(path, info, after):
    """Replace owned activation even when its contents are damaged/oversized."""
    with pinned_parents(path):
        temporary = os.path.join(os.path.dirname(path), ".bark-" + uuid.uuid4().hex)
        try:
            create_file(temporary, after)
            handle, latest = open_handle(path)
            CloseHandle(handle)
            if latest != info:
                raise OSError("config_changed")
            os.replace(temporary, path)
            sync_directory(os.path.dirname(path))
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)

def _replace_checked(path, before, info, after, private):
    temporary = os.path.join(os.path.dirname(path), ".bark-" + uuid.uuid4().hex)
    try:
        create_file(temporary, after)
        if not private:
            handle, _ = open_handle(temporary, write=True, write_dac=True)
            try:
                sd = c.create_string_buffer(info.security)
                # Preserve DACL and its protection/inheritance state.
                control, revision = w.WORD(), DWORD()
                GetControl = api(a, "GetSecurityDescriptorControl", w.BOOL, [P, c.POINTER(w.WORD), c.POINTER(DWORD)])
                checked(GetControl(sd, c.byref(control), c.byref(revision)))
                checked(SetSecurity(handle, 4 | (0x80000000 if control.value & 0x1000 else 0x20000000), sd))
            finally:
                CloseHandle(handle)
        current, latest = read_regular(path, max(len(before), 1024 * 1024))
        if current != before or latest != info:
            raise OSError("config_changed")
        os.replace(temporary, path)
        sync_directory(os.path.dirname(path))
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)

class OVERLAPPED(c.Structure):
    _fields_ = [("internal", c.c_size_t), ("internal_hi", c.c_size_t),
                ("offset", DWORD), ("offset_hi", DWORD), ("event", P)]

Lock = api(k, "LockFileEx", w.BOOL, [P, DWORD, DWORD, DWORD, DWORD, c.POINTER(OVERLAPPED)])

def lock_until(fd, exclusive, deadline):
    import msvcrt
    while True:
        overlap = OVERLAPPED()
        if Lock(msvcrt.get_osfhandle(fd), 1 | (2 if exclusive else 0), 0, 1, 0, c.byref(overlap)):
            return True
        code = c.get_last_error()
        if code != 33:
            raise c.WinError(code)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.02, remaining))

class BASICLIMIT(c.Structure):
    _fields_ = [("process_time", c.c_longlong), ("job_time", c.c_longlong), ("flags", DWORD),
                ("min_working", c.c_size_t), ("max_working", c.c_size_t), ("active_limit", DWORD),
                ("affinity", c.c_size_t), ("priority", DWORD), ("scheduling", DWORD)]

class IOLIMIT(c.Structure):
    _fields_ = [(name, c.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

class EXTENDEDLIMIT(c.Structure):
    _fields_ = [("basic", BASICLIMIT), ("io", IOLIMIT), ("process_memory", c.c_size_t),
                ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]

def bounded_popen(argv):
    CreateJob = api(k, "CreateJobObjectW", P, [P, w.LPCWSTR])
    SetJob = api(k, "SetInformationJobObject", w.BOOL, [P, c.c_int, P, DWORD])
    AssignJob = api(k, "AssignProcessToJobObject", w.BOOL, [P, P])
    job = checked(CreateJob(None, None))
    proc = None
    try:
        limits = EXTENDEDLIMIT()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        checked(SetJob(job, 9, c.byref(limits), c.sizeof(limits)))
        proc = subprocess.Popen([sys.executable, "-B", os.path.abspath(__file__), "--bootstrap"] + list(argv),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                bufsize=0, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
        checked(AssignJob(job, P(proc._handle)))
        proc._bark_job = job
        proc.stdin.write(b"G")
        proc.stdin.flush()
        return proc
    except BaseException:
        CloseHandle(job)
        if proc:
            proc.kill()
            proc.wait(timeout=1)
            proc.stdin.close()
            proc.stdout.close()
        raise

def stop_child(proc):
    job = getattr(proc, "_bark_job", None)
    if job:
        CloseHandle(job)
        proc._bark_job = None
    try:
        proc.wait(timeout=1)
    finally:
        for stream in (proc.stdin, proc.stdout):
            if stream and not stream.closed:
                stream.close()

Peek = api(k, "PeekNamedPipe", w.BOOL, [P, P, DWORD, P, c.POINTER(DWORD), P])

def pipe_read(stream, maximum, deadline):
    import msvcrt
    while True:
        available = DWORD()
        if not Peek(msvcrt.get_osfhandle(stream.fileno()), None, 0, None, c.byref(available), None):
            if c.get_last_error() in (109, 232):
                return b""
            raise c.WinError(c.get_last_error())
        if available.value:
            return os.read(stream.fileno(), min(maximum, available.value))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timeout")
        time.sleep(min(0.01, remaining))

def detached_popen(argv):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True,
                            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)

def default_codex():
    import glob
    root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "OpenAI", "Codex", "bin")
    candidates = glob.glob(os.path.join(root, "*", "codex.exe"))
    safe = []
    for path in candidates:
        try:
            no_links(path)
        except OSError:
            continue
        safe.append(path)
    # Multiple app versions require the caller to pick the running one explicitly.
    return safe[0] if len(safe) == 1 else ""

if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "--bootstrap" or os.read(sys.stdin.fileno(), 1) != b"G":
        sys.exit(2)
    try:
        child = subprocess.Popen(sys.argv[2:], shell=False, close_fds=True)
        sys.exit(child.wait())
    except OSError:
        sys.exit(127)
