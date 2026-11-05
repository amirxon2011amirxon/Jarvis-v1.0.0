"""One-command launcher for Jarvis's local web interface.

Production mode serves the built React assets from FastAPI.  Development mode
starts Vite on a second loopback port and proxies API/WebSocket traffic back to
the same Python process.  In both cases the browser is opened only after the
services are listening, and Ctrl+C tears the child processes down.
"""
from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

from web import frontend_dist, frontend_root

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = frontend_root()
DIST_INDEX = frontend_dist() / "index.html"
BACKEND_PREFERRED = 8765
VITE_PREFERRED = 5173
# A natural-language shutdown returns this from main() so the Windows `jarvis`
# shim can close the console window it was launched from. Ctrl+C keeps the
# terminal open and returns the normal 0.
SHUTDOWN_EXIT_CODE = 7

_DEV_PROCESS: subprocess.Popen | None = None
_JOB_HANDLE = None


def _configure_console() -> None:
    """Windows consoles are not always UTF-8; status text may contain symbols."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def find_free_port(preferred: int, attempts: int = 40) -> int:
    """Bind-test loopback ports and return the first one available."""
    for offset in range(max(0, attempts)):
        port = preferred + offset
        if port > 65535:
            break
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_open(port: int, timeout: float = 0.15) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _print_missing_frontend() -> None:
    print("\nFrontend assets are not available.\n")
    if (FRONTEND / "src").is_dir():
        print("  cd frontend")
        print("  npm install")
        print("  npm run build")
        print("  cd ..")
        print("  python main.py\n")
        print("For frontend development instead:  python main.py --dev\n")
    else:
        print("This installation is missing its packaged frontend assets.")
        print("Reinstall Jarvis from the release package/wheel, then run")
        print("`jarvis` again.\n")


def _close_console_window() -> None:
    """Close the console this process owns after a requested shutdown.

    Only called when the natural-language shutdown path completed; a normal
    Ctrl+C must leave the user's terminal open. On Windows Terminal/conhost the
    window may be owned by the terminal host, so `jarvis.cmd` also exits the
    console when it receives SHUTDOWN_EXIT_CODE.
    """
    if os.name != "nt":
        return
    try:
        hwnd = int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
        if not hwnd:
            return
        # Let main() return first; WM_CLOSE then ends the cmd.exe window.
        threading.Timer(
            0.25,
            lambda: ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0),
        ).start()
    except Exception:
        # The .cmd shim still receives the dedicated exit code.
        pass


def _assign_to_job(proc: subprocess.Popen) -> None:
    """Windows: kill Vite and its node child if this process disappears."""
    global _JOB_HANDLE
    if os.name != "nt":
        return
    try:
        from ctypes import wintypes

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC_LIMIT),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return
        if not kernel32.AssignProcessToJobObject(job, int(proc._handle)):
            kernel32.CloseHandle(job)
            return
        _JOB_HANDLE = job
    except Exception as exc:
        print(f"[Jarvis] Vite cleanup guard unavailable: {exc}")


def _stop_process_tree(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, check=False)
        else:
            os.killpg(os.getpgid(proc.pid), 15)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


@atexit.register
def _cleanup_dev_process() -> None:
    _stop_process_tree(_DEV_PROCESS)


def _start_vite(backend_port: int, vite_port: int) -> subprocess.Popen:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError("npm was not found on PATH. Install Node.js to use --dev.")
    if not (FRONTEND / "node_modules").is_dir():
        raise RuntimeError(
            "frontend/node_modules is missing. Run `cd frontend && npm install` first.")
    env = os.environ.copy()
    env["AZE_BACKEND_PORT"] = str(backend_port)
    env["AZE_VITE_PORT"] = str(vite_port)
    kwargs = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [npm, "run", "dev", "--", "--port", str(vite_port), "--strictPort"],
        cwd=str(FRONTEND), env=env, **kwargs)
    _assign_to_job(proc)
    return proc


def _wait_for(condition, seconds: float, what: str,
              proc: subprocess.Popen | None = None) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"{what} exited during startup (code {proc.returncode}).")
        time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for {what}.")


def _wait_for_server(server, thread: threading.Thread, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return
        if not thread.is_alive():
            raise RuntimeError(
                "the Python server exited during startup (see the log above).")
        time.sleep(0.1)
    raise RuntimeError("Timed out waiting for the Python server.")


def existing_instance(port: int, timeout: float = 0.4) -> bool:
    """Whether a Jarvis server is already answering on this loopback port.

    Typing the command twice must not start a second copy: two instances would
    each open the microphone and the speaker. The health payload carries the
    app name, so an unrelated service on the same port is not mistaken for us.
    """
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{int(port)}/api/health",
                timeout=timeout) as response:
            payload = json.loads(
                response.read(4096).decode("utf-8", "replace"))
        return payload.get("app") == "Jarvis"
    except Exception:
        return False


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="Jarvis",
        description="Launch Jarvis's local web interface.",
    )
    parser.add_argument("--dev", action="store_true",
                        help="run Vite's development server with hot reload")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the default browser")
    parser.add_argument("--port", type=int, default=None,
                        help="preferred localhost port for the Python server")
    return parser.parse_args(argv)


def _make_shutdown_handler(runtime, server, *, delay: float = 2.0,
                            settle: float = 0.35):
    """Build the process handler registered with ``core.lifecycle``.

    Split out from ``main`` so the shutdown sequence can be tested without
    actually stopping a live uvicorn server.
    """
    def _perform_shutdown(reason: str) -> None:
        try:
            runtime.prepare_shutdown(reason)
        except Exception:  # noqa: BLE001 - still stop what we can
            traceback.print_exc()
        # Let the model finish its short confirmation and the browser show
        # the power-down screen before closing the tab.
        time.sleep(max(0.0, delay))
        try:
            runtime.close_interface_tab()
        except Exception:  # noqa: BLE001 - browser close is best effort
            traceback.print_exc()
        time.sleep(max(0.0, settle))
        try:
            server.should_exit = True
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    return _perform_shutdown


def _install_shutdown_signals() -> None:
    """Route Windows Ctrl+Break / termination through normal cleanup.

    Ctrl+C already raises KeyboardInterrupt in Python. Windows Ctrl+Break
    (``SIGBREAK``) otherwise terminates the process before ``finally`` can stop
    uvicorn and the application services.
    """
    def _interrupt(_signum, _frame) -> None:
        raise KeyboardInterrupt

    for name in ("SIGBREAK", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, _interrupt)
        except (OSError, ValueError):
            # Not every host/thread supports installing these handlers.
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    _install_shutdown_signals()
    args = _parse(argv)
    if not args.dev and not DIST_INDEX.is_file():
        _print_missing_frontend()
        return 2
    if args.port is not None and not (1 <= args.port <= 65535):
        print("--port must be between 1 and 65535.")
        return 2

    # One instance per machine. Reusing the running instance is what makes it
    # idempotent: the command opens the interface instead of starting a second
    # server that would fight over the microphone and speaker.
    if not args.dev:
        preferred = args.port or BACKEND_PREFERRED
        if existing_instance(preferred):
            url = f"http://127.0.0.1:{preferred}"
            print(f"Jarvis is already running at {url}")
            if not args.no_browser:
                try:
                    if not webbrowser.open(url, new=2):
                        print(f"Open this address in a browser: {url}")
                except Exception:
                    print(f"Open this address in a browser: {url}")
            return 0

    backend_port = find_free_port(args.port or BACKEND_PREFERRED)
    vite_port = find_free_port(VITE_PREFERRED) if args.dev else None
    if args.dev and vite_port is not None:
        # The Vite proxy forwards these exact origins to the backend.
        os.environ["AZE_DEV_ORIGINS"] = (
            f"http://127.0.0.1:{vite_port},http://localhost:{vite_port}")
        os.environ["AZE_BACKEND_PORT"] = str(backend_port)
        os.environ["AZE_VITE_PORT"] = str(vite_port)

    # Import after the dev environment is set: create_app reads the allowed
    # development origins from it.
    try:
        import uvicorn

        from web.server import create_app
    except Exception as exc:
        print(f"Jarvis could not load its web dependencies: {exc}")
        print("Run `python -m pip install -r requirements.txt` and try again.")
        return 3

    try:
        app = create_app()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=backend_port,
            log_level="info",
            access_log=False,
            ws_ping_interval=20,
            ws_ping_timeout=20,
        )
        server = uvicorn.Server(config)
    except Exception as exc:
        print(f"Jarvis could not prepare the local server: {exc}")
        return 3

    # A shutdown command belongs to the process lifecycle, not the action
    # registry. The core module owns the intent; this launcher owns the real
    # uvicorn server and the browser tab.
    from core import lifecycle

    runtime = app.state.aze.runtime
    lifecycle.register_shutdown_handler(
        _make_shutdown_handler(runtime, server))

    thread = threading.Thread(target=server.run, name="aze-uvicorn",
                              daemon=True)
    thread.start()

    vite_proc = None
    shutdown_requested = False
    try:
        _wait_for_server(server, thread, 25.0)
        # Uvicorn may bind a different port if our free-port probe raced with
        # another process.  Read the real socket when possible.
        try:
            sockets = server.servers[0].sockets
            backend_port = int(sockets[0].getsockname()[1])
        except Exception:
            pass

        if args.dev:
            vite_proc = _start_vite(backend_port, int(vite_port))
            _wait_for(lambda: port_open(int(vite_port)), 40.0,
                      "Vite", vite_proc)
            url = f"http://127.0.0.1:{vite_port}"
        else:
            url = f"http://127.0.0.1:{backend_port}"

        print("\n" + "=" * 58)
        print("  J.A.R.V.I.S.  //  LOCAL NEURAL INTERFACE")
        print("=" * 58)
        print(f"  UI       {url}")
        print(f"  MODE     {'development (Vite HMR)' if args.dev else 'production'}")
        print(f"  BACKEND  http://127.0.0.1:{backend_port}")
        print("  STOP     Ctrl+C")
        print("=" * 58 + "\n")

        if not args.no_browser:
            try:
                opened = webbrowser.open(url, new=2)
                if not opened:
                    print(f"Open this address in a browser: {url}")
            except Exception:
                print(f"Open this address in a browser: {url}")

        while thread.is_alive():
            thread.join(timeout=0.5)
        shutdown_requested = lifecycle.shutdown_requested()
        return SHUTDOWN_EXIT_CODE if shutdown_requested else 0
    except KeyboardInterrupt:
        print("\nShutting down Jarvis...")
        return 0
    except Exception as exc:
        print(f"\nJarvis could not start: {exc}")
        return 3
    finally:
        if vite_proc is not None:
            _stop_process_tree(vite_proc)
        try:
            server.should_exit = True
        except Exception:
            pass
        thread.join(timeout=10.0)
        if shutdown_requested or lifecycle.shutdown_requested():
            _close_console_window()


if __name__ == "__main__":
    raise SystemExit(main())
