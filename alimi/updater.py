"""Local, versioned Windows installation and conservative legacy-app upgrade.

Only processes owned by this user, in this session, and running an exact managed
executable are eligible. Restart Manager is always used without force. The
database is never moved, replaced, or marked complete during an upgrade.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid


class UpgradeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManagedProcess:
    pid: int
    parent_pid: int
    path: Path
    started_at: int


def version_key(version: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise UpgradeError("올바르지 않은 앱 버전입니다.")
    return tuple(int(part) for part in version.split("."))


def _managed_bin(data_dir: Path) -> Path:
    root = Path(data_dir).resolve()
    directory = root / "bin"
    if directory.resolve().parent != root:
        raise UpgradeError("앱 설치 폴더가 데이터 폴더 밖으로 연결되어 있습니다.")
    return directory


def managed_executable(data_dir: Path, version: str) -> Path:
    version_key(version)
    directory = _managed_bin(data_dir) / version
    if directory.resolve().parent != _managed_bin(data_dir).resolve():
        raise UpgradeError("버전 설치 폴더가 앱 설치 폴더 밖으로 연결되어 있습니다.")
    target = directory / "SchoolAlimi.exe"
    if target.resolve().parent != directory.resolve():
        raise UpgradeError("앱 실행 파일이 설치 폴더 밖으로 연결되어 있습니다.")
    return target


def is_managed_executable(path: Path, data_dir: Path) -> bool:
    try:
        directory = _managed_bin(data_dir).resolve()
        candidate = Path(path).resolve()
        if candidate.name.casefold() != "schoolalimi.exe":
            return False
        if candidate.parent == directory:
            return True
        if candidate.parent.parent != directory:
            return False
        version_key(candidate.parent.name)
        return True
    except (OSError, UpgradeError, ValueError):
        return False


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_versioned(source: Path, data_dir: Path, version: str) -> Path:
    """Install an immutable verified copy; never overwrite an existing release."""
    target = managed_executable(data_dir, version)
    temporary = None
    try:
        source = Path(source).resolve(strict=True)
        source_hash = hash_file(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if hash_file(target) != source_hash:
                raise UpgradeError("같은 버전의 설치 파일이 다릅니다. 기존 파일을 덮어쓰지 않았습니다.")
        else:
            temporary = target.parent / (".install-" + uuid.uuid4().hex)
            shutil.copyfile(source, temporary)
            if hash_file(temporary) != source_hash:
                raise UpgradeError("앱 복사 파일의 무결성 검사가 실패했습니다.")
            os.replace(temporary, target)
            temporary = None
        manifest = target.with_suffix(".sha256")
        if manifest.resolve().parent != target.parent.resolve():
            raise UpgradeError("앱 무결성 기록이 설치 폴더 밖으로 연결되어 있습니다.")
        if manifest.exists() and manifest.read_text(encoding="ascii").strip() != source_hash:
            raise UpgradeError("설치 파일의 무결성 기록이 일치하지 않습니다.")
        manifest.write_text(source_hash + "\n", encoding="ascii")
        return target
    except UpgradeError:
        raise
    except (OSError, UnicodeError) as exc:
        raise UpgradeError("새 앱을 설치하지 못했습니다: " + str(exc)) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _latest_installed(data_dir: Path, fallback: Path) -> Path:
    selected = fallback
    selected_version = version_key(fallback.parent.name)
    for directory in _managed_bin(data_dir).iterdir():
        if not directory.is_dir():
            continue
        try:
            key = version_key(directory.name)
            candidate = managed_executable(data_dir, directory.name)
            manifest = candidate.with_suffix(".sha256")
            if key > selected_version and candidate.is_file() and manifest.is_file():
                expected = manifest.read_text(encoding="ascii").strip()
                if re.fullmatch(r"[0-9a-f]{64}", expected) and hash_file(candidate) == expected:
                    selected, selected_version = candidate, key
        except (OSError, UnicodeError, UpgradeError):
            continue
    return selected


def backup_database(data_dir: Path, label: str) -> Path | None:
    """Create a WAL-aware SQLite backup without changing the original database."""
    source = Path(data_dir).resolve() / "alimi.sqlite3"
    if not source.exists():
        return None
    destination = None
    incoming = outgoing = None
    try:
        if source.resolve().parent != Path(data_dir).resolve():
            raise UpgradeError("일정 데이터가 데이터 폴더 밖으로 연결되어 있습니다.")
        directory = Path(data_dir).resolve() / "backups"
        if directory.resolve().parent != Path(data_dir).resolve():
            raise UpgradeError("백업 폴더가 데이터 폴더 밖으로 연결되어 있습니다.")
        directory.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^a-zA-Z0-9_-]", "-", label)
        destination = directory / (f"alimi-{safe_label}-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite3")
        incoming = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=15)
        outgoing = sqlite3.connect(destination)
        deadline = time.monotonic() + 20
        def progress(_status, _remaining, _total):
            if time.monotonic() > deadline:
                raise UpgradeError("일정 백업이 지연되어 업데이트를 중단했습니다.")
        incoming.backup(outgoing, pages=256, progress=progress, sleep=0.05)
        if outgoing.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise UpgradeError("일정 백업의 무결성 검사가 실패했습니다.")
        return destination
    except (OSError, sqlite3.Error) as exc:
        raise UpgradeError("일정 백업에 실패하여 업데이트를 중단했습니다: " + str(exc)) from exc
    finally:
        if outgoing is not None:
            outgoing.close()
        if incoming is not None:
            incoming.close()


@lru_cache(maxsize=1)
def _win_api():
    import ctypes
    from ctypes import wintypes
    if os.name != "nt":
        raise UpgradeError("Windows에서만 앱을 업데이트할 수 있습니다.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    restart = ctypes.WinDLL("rstrtmgr", use_last_error=True)
    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
    class UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]
    class TOKEN_USER(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for function in (kernel.Process32FirstW, kernel.Process32NextW):
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
        function.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi.GetLengthSid.restype = wintypes.DWORD
    restart.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
    restart.RmStartSession.restype = wintypes.DWORD
    restart.RmRegisterResources.argtypes = [wintypes.DWORD, wintypes.UINT, ctypes.c_void_p,
                                          wintypes.UINT, ctypes.POINTER(UNIQUE_PROCESS), wintypes.UINT, ctypes.c_void_p]
    restart.RmRegisterResources.restype = wintypes.DWORD
    restart.RmShutdown.argtypes = [wintypes.DWORD, wintypes.ULONG, ctypes.c_void_p]
    restart.RmShutdown.restype = wintypes.DWORD
    restart.RmEndSession.argtypes = [wintypes.DWORD]
    restart.RmEndSession.restype = wintypes.DWORD
    return ctypes, wintypes, kernel, advapi, restart, PROCESSENTRY32, UNIQUE_PROCESS, TOKEN_USER


def _process_sid(handle) -> bytes | None:
    ctypes, wintypes, kernel, advapi, _, _, _, TOKEN_USER = _win_api()
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):
        return None
    try:
        length = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not length.value:
            return None
        buffer = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
            return None
        sid = ctypes.cast(buffer, ctypes.POINTER(TOKEN_USER)).contents.sid
        return ctypes.string_at(sid, advapi.GetLengthSid(sid))
    finally:
        kernel.CloseHandle(token)


def _process_path_time(handle) -> tuple[Path, int] | None:
    ctypes, wintypes, kernel, *_ = _win_api()
    length = wintypes.DWORD(32768)
    image = ctypes.create_unicode_buffer(length.value)
    if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
        return None
    creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
    if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
        return None
    return Path(image.value).resolve(), creation.dwLowDateTime | (creation.dwHighDateTime << 32)


def discover_managed_processes(data_dir: Path, exclude_pid: int | None = None) -> list[ManagedProcess]:
    ctypes, wintypes, kernel, _, _, PROCESSENTRY32, *_ = _win_api()
    _managed_bin(data_dir)
    excluded = os.getpid() if exclude_pid is None else exclude_pid
    own_sid = _process_sid(kernel.GetCurrentProcess())
    own_session = wintypes.DWORD()
    if own_sid is None or not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(own_session)):
        raise UpgradeError("현재 Windows 사용자 정보를 확인할 수 없습니다.")
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise UpgradeError("실행 중인 앱 목록을 확인할 수 없습니다.")
    processes = []
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while available:
            pid = int(entry.th32ProcessID)
            if pid != excluded and entry.szExeFile.casefold() == "schoolalimi.exe":
                handle = kernel.OpenProcess(0x1000, False, pid)
                if handle:
                    try:
                        details = _process_path_time(handle)
                        session = wintypes.DWORD()
                        if (details and is_managed_executable(details[0], data_dir)
                                and kernel.ProcessIdToSessionId(pid, ctypes.byref(session))
                                and session.value == own_session.value and _process_sid(handle) == own_sid):
                            processes.append(ManagedProcess(pid, int(entry.th32ParentProcessID), *details))
                    finally:
                        kernel.CloseHandle(handle)
            available = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return processes


def _wake_legacy_manager(data_dir: Path, processes: list[ManagedProcess]) -> None:
    """Ask only the verified legacy pipe owner to create its manager window."""
    directory = _managed_bin(data_dir).resolve()
    eligible = {process.pid for process in processes if process.path.resolve().parent == directory}
    if not eligible:
        return
    ctypes, wintypes, kernel, *_ = _win_api()
    kernel.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel.WaitNamedPipeW.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    kernel.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    kernel.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.WriteFile.restype = wintypes.BOOL
    server = "SchoolAlimi-" + hashlib.sha256(str(Path(data_dir).resolve()).encode()).hexdigest()[:16]
    pipe_name = "\\\\.\\pipe\\" + server
    if not kernel.WaitNamedPipeW(pipe_name, 500):
        return
    handle = kernel.CreateFileW(pipe_name, 0xC0000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        return
    try:
        owner = wintypes.ULONG()
        if not kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(owner)) or owner.value not in eligible:
            raise UpgradeError("기존 앱의 통신 창 소유자를 확인할 수 없어 업데이트를 중단했습니다.")
        written = wintypes.DWORD()
        message = ctypes.create_string_buffer(b"show")
        if kernel.WriteFile(handle, message, 4, ctypes.byref(written), None):
            # Keep the connection alive long enough for the old queued newConnection
            # callback to create its stable main-window HWND before closing it.
            time.sleep(0.25)
    finally:
        kernel.CloseHandle(handle)


def _graceful_windows_shutdown(processes: list[ManagedProcess]) -> bool:
    """Send Qt's normal session-close handshake to an exactly validated window.

    Some windowed onefile applications acknowledge Restart Manager's query but
    are not subsequently closed by it. These messages exercise Qt's existing
    commitData/aboutToQuit path, without terminating any process.
    """
    ctypes, wintypes, kernel, *_ = _win_api()
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetClassNameW.restype = ctypes.c_int
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowTextW.restype = ctypes.c_int
    user.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                        wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                                        ctypes.POINTER(ctypes.c_size_t)]
    user.SendMessageTimeoutW.restype = wintypes.LPARAM
    user.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.PostThreadMessageW.restype = wintypes.BOOL
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.GetProcessIdOfThread.argtypes = [wintypes.HANDLE]
    kernel.GetProcessIdOfThread.restype = wintypes.DWORD
    handles = []
    thread_handles = []
    targets = []
    try:
        # Collect all eligible Qt windows before asking any application to close.
        # False therefore means no shutdown message was sent at all.
        for process in processes:
            handle = kernel.OpenProcess(0x1000 | 0x00100000, False, process.pid)
            if not handle:
                if ctypes.get_last_error() == 87:
                    continue
                raise UpgradeError("기존 앱의 정상 종료 요청을 검증하지 못했습니다.")
            handles.append(handle)
            if kernel.WaitForSingleObject(handle, 0) == 0:
                continue
            if _process_path_time(handle) != (process.path.resolve(), process.started_at):
                raise UpgradeError("기존 앱의 프로세스 정보가 바뀌어 종료 요청을 중단했습니다.")
            windows = []
            @callback_type
            def collect(hwnd, _parameter):
                pid = wintypes.DWORD()
                user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == process.pid:
                    name = ctypes.create_unicode_buffer(256)
                    title = ctypes.create_unicode_buffer(256)
                    user.GetClassNameW(hwnd, name, len(name))
                    user.GetWindowTextW(hwnd, title, len(title))
                    # Tray/menu/tool windows can disappear when commitData hides
                    # the tray; use the stable native main-window handle only.
                    if (re.match(r"^Qt5\d+QWindowIcon", name.value)
                            and re.fullmatch(r"학교 알리미 · \d+\.\d+\.\d+", title.value)):
                        windows.append(hwnd)
                return True
            if not user.EnumWindows(collect, 0):
                raise UpgradeError("기존 앱의 Windows 창 목록을 확인하지 못했습니다.")
            if not windows:
                return False
            hwnd = windows[0]
            window_pid = wintypes.DWORD()
            thread_id = user.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            thread_handle = kernel.OpenThread(0x0800 | 0x00100000, False, thread_id)
            if (window_pid.value != process.pid or not thread_handle
                    or kernel.GetProcessIdOfThread(thread_handle) != process.pid):
                if thread_handle:
                    kernel.CloseHandle(thread_handle)
                raise UpgradeError("기존 앱의 기본 창 스레드를 검증하지 못했습니다.")
            thread_handles.append(thread_handle)
            targets.append((process, handle, hwnd, thread_id, thread_handle))
        for process, handle, hwnd, thread_id, thread_handle in targets:
            def send(message, accepted):
                pid = wintypes.DWORD()
                user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value != process.pid:
                    raise UpgradeError("기존 앱 창의 프로세스 정보가 바뀌었습니다.")
                response = ctypes.c_size_t()
                # BLOCK | ABORTIFHUNG | ERRORONEXIT; no asynchronous forced close.
                delivered = user.SendMessageTimeoutW(hwnd, message, accepted, 1, 0x23, 5000, ctypes.byref(response))
                return bool(delivered), response.value
            delivered, approved = send(0x0011, 0)  # WM_QUERYENDSESSION, ENDSESSION_CLOSEAPP
            if not delivered or not approved:
                try:
                    send(0x0016, 0)  # WM_ENDSESSION(FALSE): cancel the request.
                except UpgradeError:
                    pass
                raise UpgradeError("기존 앱이 정상 종료 요청을 승인하지 않았습니다. 강제 종료하지 않았습니다.")
            window_pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            delivered = False
            if window_pid.value == process.pid:
                delivered, _ = send(0x0016, 1)  # WM_ENDSESSION(TRUE) invokes Qt's quit path.
            if not delivered:
                if kernel.WaitForSingleObject(handle, 0) == 0:
                    continue
                # Qt commitData can close and destroy the queried HWND itself.
                # After its TRUE approval only, ask the retained original main
                # GUI thread to leave its normal event loop. Qt's WM_QUIT handler
                # calls QCoreApplication::quit(), including aboutToQuit cleanup.
                # This is never a process-termination API or an unapproved close.
                if (kernel.GetProcessIdOfThread(thread_handle) != process.pid
                        or _process_path_time(handle) != (process.path.resolve(), process.started_at)):
                    raise UpgradeError("종료를 승인한 기존 앱의 스레드 정보가 바뀌었습니다.")
                if kernel.WaitForSingleObject(thread_handle, 0) == 0:
                    continue
                if not user.PostThreadMessageW(thread_id, 0x0012, 0, 0):
                    raise UpgradeError("기존 앱의 정상 종료 요청이 지연되었습니다. 강제 종료하지 않았습니다.")
        return True
    finally:
        for handle in thread_handles:
            kernel.CloseHandle(handle)
        for handle in handles:
            kernel.CloseHandle(handle)


def shutdown_managed_processes(processes: list[ManagedProcess]) -> None:
    """Request graceful shutdown of validated application children, never force."""
    if not processes:
        return
    ctypes, wintypes, kernel, _, restart, _, UNIQUE_PROCESS, _ = _win_api()
    handles = []
    session = wintypes.DWORD()
    started = False
    try:
        live = []
        for process in processes:
            handle = kernel.OpenProcess(0x1000 | 0x00100000, False, process.pid)
            if not handle:
                # A process which exited between discovery and shutdown needs no action.
                if ctypes.get_last_error() == 87:
                    continue
                raise UpgradeError("기존 앱의 실행 상태를 확인하지 못했습니다. 강제 종료하지 않았습니다.")
            details = _process_path_time(handle)
            if details != (process.path.resolve(), process.started_at):
                kernel.CloseHandle(handle)
                raise UpgradeError("기존 앱의 프로세스 정보가 바뀌어 업데이트를 중단했습니다.")
            handles.append(handle)
            live.append(process)
        if not live:
            return
        # Onefile bootloader parents exit naturally once their application child exits.
        parents = {process.parent_pid for process in live}
        application_processes = [process for process in live if process.pid not in parents]
        if not _graceful_windows_shutdown(application_processes):
            unique = (UNIQUE_PROCESS * len(application_processes))()
            for item, process in zip(unique, application_processes):
                item.dwProcessId = process.pid
                item.ProcessStartTime.dwLowDateTime = process.started_at & 0xFFFFFFFF
                item.ProcessStartTime.dwHighDateTime = process.started_at >> 32
            result = restart.RmStartSession(ctypes.byref(session), 0, ctypes.create_unicode_buffer(33))
            if result:
                raise UpgradeError(f"Windows 업데이트 세션을 시작하지 못했습니다. 오류 {result}")
            started = True
            result = restart.RmRegisterResources(session, 0, None, len(unique), unique, 0, None)
            if result:
                raise UpgradeError(f"기존 앱을 업데이트 대상으로 등록하지 못했습니다. 오류 {result}")
            result = restart.RmShutdown(session, 0, None)  # RmForceShutdown is deliberately absent.
            if result == 351 and _graceful_windows_shutdown(application_processes):
                result = 0
            if result:
                raise UpgradeError(f"기존 앱이 정상 종료되지 않았습니다. 강제 종료하지 않았습니다. 오류 {result}")
        deadline = time.monotonic() + 15
        for handle in handles:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if kernel.WaitForSingleObject(handle, remaining) != 0:
                raise UpgradeError("기존 앱 종료가 지연되었습니다. 일정을 완료 처리하거나 강제 종료하지 않았습니다.")
    finally:
        if started:
            restart.RmEndSession(session)
        for handle in handles:
            kernel.CloseHandle(handle)


@contextmanager
def install_lock(data_dir: Path):
    ctypes, _, kernel, *_ = _win_api()
    name = "Local\\SchoolAlimi-Install-" + hashlib.sha256(str(Path(data_dir).resolve()).casefold().encode()).hexdigest()[:24]
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise UpgradeError("앱 업데이트 잠금을 만들지 못했습니다.")
    acquired = False
    try:
        acquired = kernel.WaitForSingleObject(handle, 30000) in (0, 0x80)
        if not acquired:
            raise UpgradeError("다른 앱 업데이트가 진행 중입니다. 잠시 후 다시 실행해 주세요.")
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def ensure_installed(source: Path, data_dir: Path, version: str) -> Path:
    """Return the newest installed target, safely stopping only older managed apps."""
    try:
        with install_lock(data_dir):
            installed = install_versioned(source, data_dir, version)
            target = _latest_installed(data_dir, installed)
            running = discover_managed_processes(data_dir)
            older = [process for process in running if process.path.resolve() != target.resolve()]
            target_version = version_key(target.parent.name)
            managed_bin = _managed_bin(data_dir).resolve()
            if any(process.path.resolve().parent != managed_bin
                   and version_key(process.path.resolve().parent.name) > target_version for process in older):
                raise UpgradeError("더 최신 버전의 알리미가 실행 중입니다. 이전 버전으로 바꾸지 않았습니다.")
            if older:
                backup_database(data_dir, "before-update")
                _wake_legacy_manager(data_dir, older)
                shutdown_managed_processes(older)
                backup_database(data_dir, "after-update")
            return target
    except UpgradeError:
        raise
    except (OSError, ValueError) as exc:
        raise UpgradeError("앱 업데이트를 완료하지 못했습니다: " + str(exc)) from exc
