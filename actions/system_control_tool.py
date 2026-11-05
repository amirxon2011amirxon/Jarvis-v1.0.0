"""Windows system inspection and control for Jarvis — one unified tool.

All operations are fixed, validated actions.  Nothing here executes an
arbitrary shell command, changes network/security settings, terminates a
process, or deletes files.  The LLM supplies an operation name and a few typed
arguments; this module maps them onto Windows-native interfaces and already
installed dependencies:

* ``psutil``        — CPU, memory, disks, battery, processes, network.
* ``pycaw``         — per-endpoint volume and mute (via ``core.audio_devices``,
                      which already owns the Windows volume plumbing).
* WMI (fixed script)— internal-display brightness on laptops.
* ``dxva2`` (ctypes)— DDC/CI brightness for compatible external monitors.
* ``ctypes`` Win32  — display enumeration and resolution/refresh.
* ``netsh`` (fixed) — Wi-Fi SSID/state (never keys).
* ``os.startfile``  — an allowlist of Settings pages and system utilities.

A capability that the machine does not expose returns ``supported: false`` with
a clear explanation; it is never reported as success.

Read-only inspection is safe to call repeatedly.  Brightness, volume and mute
are reversible and verified by re-reading the value afterwards. Workstation
lock and sleep are fixed Windows actions; sleep never substitutes shutdown,
restart, logout or hibernation.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import platform
import re
import socket
import subprocess
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psutil

# ── constants ────────────────────────────────────────────────────────────────
IS_WINDOWS = os.name == "nt"
MAX_PROCESSES = 40
DEFAULT_PROCESSES = 15
DEFAULT_TOP = 5
MAX_TOP = 25
PROCESS_SAMPLE_SECONDS = 0.2
COMMAND_TIMEOUT = 6.0
NET_PROBE_TIMEOUT = 1.5
CREATE_NO_WINDOW = 0x08000000

_SETTINGS_PAGES = {
    "display": "ms-settings:display",
    "sound": "ms-settings:sound",
    "bluetooth": "ms-settings:bluetooth",
    "wifi": "ms-settings:network-wifi",
    "network": "ms-settings:network",
    "battery": "ms-settings:batterysaver",
    "power": "ms-settings:power",
    "windows_update": "ms-settings:windowsupdate",
    "apps": "ms-settings:appsfeatures",
    "storage": "ms-settings:storagesense",
    "accessibility": "ms-settings:easeofaccess",
    "personalization": "ms-settings:personalization",
    "notifications": "ms-settings:notifications",
    "privacy": "ms-settings:privacy",
    "about": "ms-settings:about",
    "startup": "ms-settings:startupapps",
    "projection": "ms-settings:project",
    "night_light": "ms-settings:nightlight",
    "lock_screen": "ms-settings:lockscreen",
    "clipboard": "ms-settings:clipboard",
}

_UTILITIES = {
    "task_manager": "taskmgr",
    "taskmgr": "taskmgr",
    "file_explorer": "explorer",
    "explorer": "explorer",
    "device_manager": "devmgmt.msc",
    "control_panel": "control",
    "network_connections": "ncpa.cpl",
    "power_options": "powercfg.cpl",
    "sound_control": "mmsys.cpl",
    "programs_and_features": "appwiz.cpl",
    "system_properties": "sysdm.cpl",
    "date_time": "timedate.cpl",
    "region": "intl.cpl",
}


# ── result helpers ───────────────────────────────────────────────────────────
def _ok(operation: str, message: str, **fields: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {"success": True, "ok": True,
                              "operation": operation, "message": message}
    result.update(fields)
    return result


def _fail(operation: str, reason: str, error: str, **fields: Any
          ) -> Dict[str, Any]:
    print(f"[SystemControl] {operation} {reason}: {error}")
    result = {"success": False, "ok": False, "operation": operation,
              "reason": reason, "error": error}
    result.update(fields)
    return result


def _unsupported(operation: str, message: str, **fields: Any) -> Dict[str, Any]:
    result = _fail(operation, "unsupported", message, **fields)
    result["supported"] = False
    return result


def _windows_only(operation: str) -> Optional[Dict[str, Any]]:
    if IS_WINDOWS:
        return None
    return _unsupported(operation,
                        "This capability is only available on Windows.")


# ── generic helpers ──────────────────────────────────────────────────────────
def _clamp(value: Any, low: int = 0, high: int = 100,
           default: Optional[int] = None) -> Tuple[int, Optional[str]]:
    if value is None or str(value).strip() == "":
        return 0, "A value is required."
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return 0, "A numeric value is required."
    return max(low, min(high, number)), None


def _run(args: List[str], timeout: float = COMMAND_TIMEOUT) -> Tuple[int, str, str]:
    """Run a fixed argument vector (never a shell string)."""
    try:
        completed = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0)
        return completed.returncode, completed.stdout or "", completed.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"Timed out after {timeout:.0f}s."
    except FileNotFoundError:
        return -1, "", f"{args[0]} is not available."
    except OSError as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def _powershell(script: str, timeout: float = COMMAND_TIMEOUT) -> Tuple[int, str, str]:
    return _run(["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", script], timeout)


def _bytes_to_gb(value: float) -> float:
    return round(float(value) / (1024 ** 3), 2)


def _bytes_to_mb(value: float) -> float:
    return round(float(value) / (1024 ** 2), 1)


def _percent(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


# ── system / CPU / memory / disk / battery ──────────────────────────────────
def _cpu_info() -> Dict[str, Any]:
    frequency = psutil.cpu_freq()
    usage = psutil.cpu_percent(interval=0.15)
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    return {
        "usage_percent": usage,
        "per_core_percent": per_core,
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "frequency_mhz": round(frequency.current) if frequency else None,
        "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
    }


def _memory_info() -> Dict[str, Any]:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "total_gb": _bytes_to_gb(memory.total),
        "used_gb": _bytes_to_gb(memory.used),
        "available_gb": _bytes_to_gb(memory.available),
        "percent": memory.percent,
        "swap_total_gb": _bytes_to_gb(swap.total),
        "swap_used_gb": _bytes_to_gb(swap.used),
        "swap_percent": swap.percent,
    }


def _disk_info(path: str = "") -> Dict[str, Any]:
    drives: List[Dict[str, Any]] = []
    seen = set()
    target = os.path.abspath(path) if path else ""
    partitions = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except (PermissionError, OSError):
        partitions = []
    for part in partitions:
        if part.mountpoint in seen:
            continue
        seen.add(part.mountpoint)
        if target:
            mount = os.path.abspath(part.mountpoint)
            if not os.path.normcase(target).startswith(
                    os.path.normcase(mount)):
                continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        drives.append({
            "drive": part.mountpoint.rstrip("\\") or part.mountpoint,
            "filesystem": part.fstype,
            "total_gb": _bytes_to_gb(usage.total),
            "used_gb": _bytes_to_gb(usage.used),
            "free_gb": _bytes_to_gb(usage.free),
            "percent_used": usage.percent,
        })
    if not drives and target:
        try:
            usage = psutil.disk_usage(target)
            drives.append({
                "drive": target,
                "total_gb": _bytes_to_gb(usage.total),
                "used_gb": _bytes_to_gb(usage.used),
                "free_gb": _bytes_to_gb(usage.free),
                "percent_used": usage.percent,
            })
        except (PermissionError, OSError):
            pass
    drives.sort(key=lambda item: item["drive"])
    return {"drives": drives, "count": len(drives)}


def _battery_info() -> Dict[str, Any]:
    battery = psutil.sensors_battery()
    if battery is None:
        return {"supported": False, "message": "No battery is reported by this system."}
    remaining: Optional[int] = None
    if battery.secsleft not in (psutil.POWER_TIME_UNLIMITED,
                                psutil.POWER_TIME_UNKNOWN):
        remaining = int(battery.secsleft)
    return {
        "supported": True,
        "percent": round(float(battery.percent), 1),
        "plugged_in": bool(battery.power_plugged),
        "seconds_left": remaining,
        "minutes_left": round(remaining / 60, 1) if remaining is not None else None,
    }


def _system_info() -> Dict[str, Any]:
    boot = psutil.boot_time()
    uptime_seconds = max(0, int(time.time() - boot))
    return {
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "python": platform.python_version(),
        "uptime_seconds": uptime_seconds,
        "uptime_hours": round(uptime_seconds / 3600, 1),
        "boot_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(boot)),
        "cpu_count": psutil.cpu_count(logical=True),
        "memory_total_gb": _bytes_to_gb(psutil.virtual_memory().total),
    }


# ── displays ─────────────────────────────────────────────────────────────────
class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class _DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPositionX", wintypes.LONG),
        ("dmPositionY", wintypes.LONG),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", wintypes.SHORT),
        ("dmDuplex", wintypes.SHORT),
        ("dmYResolution", wintypes.SHORT),
        ("dmTTOption", wintypes.SHORT),
        ("dmCollate", wintypes.SHORT),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


def _displays() -> List[Dict[str, Any]]:
    if not IS_WINDOWS:
        return []
    monitors: List[Dict[str, Any]] = []
    user32 = ctypes.windll.user32
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def callback(hmonitor, _hdc, _rect, _data):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            return True
        device = str(info.szDevice)
        mode = _DEVMODEW()
        mode.dmSize = ctypes.sizeof(_DEVMODEW)
        width = height = refresh = None
        if user32.EnumDisplaySettingsW(device, -1, ctypes.byref(mode)):
            width = int(mode.dmPelsWidth)
            height = int(mode.dmPelsHeight)
            refresh = int(mode.dmDisplayFrequency) if mode.dmDisplayFrequency > 1 else None
        monitors.append({
            "device": device,
            "primary": bool(info.dwFlags & 1),
            "x": int(info.rcMonitor.left),
            "y": int(info.rcMonitor.top),
            "width": width or int(info.rcMonitor.right - info.rcMonitor.left),
            "height": height or int(info.rcMonitor.bottom - info.rcMonitor.top),
            "refresh_hz": refresh,
        })
        return True

    try:
        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
    except Exception as exc:  # noqa: BLE001
        print(f"[SystemControl] display enumeration failed: {exc}")
    return monitors


# ── brightness: WMI (internal) + DDC/CI (external) ─────────────────────────
class _PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("hPhysicalMonitor", wintypes.HANDLE),
                ("szPhysicalMonitorDescription", wintypes.WCHAR * 128)]


def _ddc_apply(index: Optional[int] = None,
               percent: Optional[int] = None) -> Dict[str, Any]:
    """Enumerate DDC/CI monitors; optionally set one while its handle is open.

    Physical-monitor handles are only valid until they are destroyed, so the
    set must happen inside the same enumeration — the earlier design returned
    stale handles.
    """
    if not IS_WINDOWS:
        return {"monitors": [], "applied": None, "error": "Windows only."}
    try:
        dxva2 = ctypes.windll.dxva2
        user32 = ctypes.windll.user32
    except (AttributeError, OSError) as exc:
        return {"monitors": [], "applied": None, "error": str(exc)}

    monitors: List[Dict[str, Any]] = []
    applied: Optional[Dict[str, Any]] = None

    def callback(hmonitor, _hdc, _rect, _data):
        nonlocal applied
        count = ctypes.c_ulong(0)
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
                hmonitor, ctypes.byref(count)) or count.value == 0:
            return True
        array = (_PHYSICAL_MONITOR * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(
                hmonitor, count.value, array):
            return True
        try:
            for item in array:
                minimum = ctypes.c_ulong(0)
                current = ctypes.c_ulong(0)
                maximum = ctypes.c_ulong(0)
                if not dxva2.GetMonitorBrightness(
                        item.hPhysicalMonitor, ctypes.byref(minimum),
                        ctypes.byref(current), ctypes.byref(maximum)):
                    continue
                if maximum.value <= minimum.value:
                    continue
                position = len(monitors)
                now = round(100 * (current.value - minimum.value)
                            / (maximum.value - minimum.value))
                entry = {
                    "index": position,
                    "name": str(item.szPhysicalMonitorDescription) or "Monitor",
                    "brightness": max(0, min(100, now)),
                }
                if index == position and percent is not None:
                    value = minimum.value + round(
                        percent / 100 * (maximum.value - minimum.value))
                    ok = dxva2.SetMonitorBrightness(
                        item.hPhysicalMonitor, wintypes.DWORD(value))
                    after = ctypes.c_ulong(0)
                    verified = False
                    if ok and dxva2.GetMonitorBrightness(
                            item.hPhysicalMonitor, ctypes.byref(minimum),
                            ctypes.byref(after), ctypes.byref(maximum)):
                        after_percent = round(
                            100 * (after.value - minimum.value)
                            / max(1, maximum.value - minimum.value))
                        entry["brightness"] = max(0, min(100, after_percent))
                        verified = abs(entry["brightness"] - percent) <= 2
                    applied = {"display_index": position, "ok": bool(ok),
                               "verified": verified,
                               "brightness": entry["brightness"],
                               "name": entry["name"]}
                monitors.append(entry)
        finally:
            try:
                dxva2.DestroyPhysicalMonitors(count.value, array)
            except Exception:
                pass
        return True

    try:
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
            ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
    except Exception as exc:  # noqa: BLE001
        return {"monitors": monitors, "applied": applied, "error": str(exc)}
    return {"monitors": monitors, "applied": applied, "error": ""}


def _ddc_monitors() -> List[Dict[str, Any]]:
    return _ddc_apply()["monitors"]


def _wmi_brightness_get() -> Optional[int]:
    script = (
        "$b = @(Get-CimInstance -Namespace root/WMI -ClassName "
        "WmiMonitorBrightness -ErrorAction SilentlyContinue); "
        "if ($b.Count -gt 0) { [int]$b[0].CurrentBrightness }")
    code, out, _err = _powershell(script)
    if code != 0:
        return None
    match = re.search(r"(\d{1,3})", out)
    return int(match.group(1)) if match else None


def _wmi_brightness_set(percent: int) -> bool:
    script = (
        "$m = @(Get-CimInstance -Namespace root/WMI -ClassName "
        "WmiMonitorBrightnessMethods -ErrorAction SilentlyContinue); "
        f"if ($m.Count -eq 0) {{ exit 2 }}; "
        f"Invoke-CimMethod -InputObject $m[0] -MethodName WmiSetBrightness "
        f"-Arguments @{{ Timeout = 1; Brightness = [byte]{int(percent)} }} "
        "| Out-Null")
    code, _out, _err = _powershell(script)
    return code == 0


def _brightness_state() -> Dict[str, Any]:
    internal = _wmi_brightness_get() if IS_WINDOWS else None
    ddc = []
    if IS_WINDOWS:
        for item in _ddc_monitors():
            ddc.append({"index": item["index"], "name": item["name"],
                        "brightness": item["brightness"]})
    if internal is None and not ddc:
        return {"supported": False,
                "message": ("This display does not expose brightness control "
                            "to Windows (no WMI backlight and no DDC/CI "
                            "monitor).")}
    return {"supported": True, "internal": internal, "monitors": ddc}


def _brightness_set(percent: int, operation: str = "set_brightness") -> Dict[str, Any]:
    if not IS_WINDOWS:
        return _unsupported(operation,
                            "Brightness control is Windows-only.")
    internal = _wmi_brightness_get()
    if internal is not None:
        _wmi_brightness_set(percent)
        after = _wmi_brightness_get()
        if after is not None and abs(after - percent) <= 2:
            return _ok(operation, f"Brightness set to {after}%.",
                       brightness=after, target="internal", verified=True)
        return _fail(operation, "not_verified",
                     "The display did not report the new brightness.",
                     requested=percent, reported=after, target="internal")
    return _unsupported(operation,
                        "The internal display does not expose a backlight "
                        "interface on this system.")


def _ddc_set(index: int, percent: int,
             operation: str = "set_brightness") -> Dict[str, Any]:
    outcome = _ddc_apply(index, percent)
    monitors = outcome["monitors"]
    if not monitors:
        return _unsupported(operation,
                            "No DDC/CI-capable monitor was detected.")
    applied = outcome["applied"]
    if applied is None:
        return _fail(operation, "invalid_display",
                     f"No external display with index {index}.",
                     available=[{k: item[k] for k in ("index", "name",
                                                      "brightness")}
                                for item in monitors])
    if not applied["ok"]:
        return _fail(operation, "failed",
                     "The monitor rejected the DDC/CI command.",
                     display_index=index)
    if not applied["verified"]:
        return _fail(operation, "not_verified",
                     "The monitor accepted the command but reports a "
                     "different brightness.",
                     requested=percent, reported=applied["brightness"],
                     display_index=index)
    return _ok(operation,
               f"{applied['name']} brightness set to "
               f"{applied['brightness']}%.",
               brightness=applied["brightness"], target=applied["name"],
               display_index=index, verified=True)


# ── processes ───────────────────────────────────────────────────────────────
def _process_rows(limit: int, sort_by: str = "memory",
                  sample: bool = False) -> List[Dict[str, Any]]:
    stats: Dict[int, Dict[str, Any]] = {}
    for proc in psutil.process_iter(["pid", "name", "memory_info",
                                     "memory_percent", "status", "username"]):
        try:
            info = proc.info
            if info["pid"] == 0 or (info.get("name") or "") == "System Idle Process":
                continue
            stats[info["pid"]] = {
                "pid": info["pid"],
                "name": info.get("name") or "?",
                "memory_percent": round(info.get("memory_percent") or 0.0, 2),
                "memory_mb": _bytes_to_mb(
                    getattr(info.get("memory_info"), "rss", 0) or 0),
                "status": info.get("status") or "",
                "user": (info.get("username") or "").split("\\")[-1],
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    if sample:
        for proc in psutil.process_iter(["pid"]):
            try:
                proc.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        time.sleep(PROCESS_SAMPLE_SECONDS)
        for proc in psutil.process_iter(["pid"]):
            try:
                entry = stats.get(proc.pid)
                if entry is not None:
                    entry["cpu_percent"] = round(proc.cpu_percent(None), 1)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    rows = list(stats.values())
    key = "cpu_percent" if (sample and sort_by == "cpu") else "memory_mb"
    rows.sort(key=lambda item: item.get(key) or 0, reverse=True)
    for row in rows:
        row.setdefault("cpu_percent", None)
    return rows[:limit]


def _process_details(pid: int) -> Dict[str, Any]:
    try:
        proc = psutil.Process(pid)
        try:
            connections = len(proc.connections(kind="inet"))  # psutil < 7
        except AttributeError:
            try:
                connections = len(psutil.net_connections(kind="inet"))
            except Exception:
                connections = None
        except (psutil.AccessDenied, OSError):
            connections = None
        with proc.oneshot():
            info = {
                "pid": pid,
                "name": proc.name(),
                "exe": proc.exe(),
                "status": proc.status(),
                "created": time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(proc.create_time())),
                "cpu_percent": round(proc.cpu_percent(interval=0.1), 1),
                "memory_mb": _bytes_to_mb(proc.memory_info().rss),
                "memory_percent": round(proc.memory_percent(), 2),
                "threads": proc.num_threads(),
                "user": (proc.username() or "").split("\\")[-1],
                "connections": connections,
                "cmdline": _redact_cmdline(proc.cmdline()),
            }
        return info
    except psutil.NoSuchProcess:
        raise FileNotFoundError(f"No process with pid {pid}.")
    except psutil.AccessDenied:
        raise PermissionError(f"Windows denied access to process {pid}.")


def _redact_cmdline(arguments: Iterable[str]) -> str:
    parts = []
    for argument in list(arguments)[:12]:
        low = argument.lower()
        if any(marker in low for marker in ("token=", "password=", "secret=",
                                            "apikey=", "api_key=",
                                            "authorization:")):
            parts.append(argument.split("=", 1)[0] + "=***")
        else:
            parts.append(argument[:300])
    return " ".join(parts)


def _diagnose() -> Dict[str, Any]:
    cpu = psutil.cpu_percent(interval=0.25)
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disks = _disk_info()["drives"]
    battery = psutil.sensors_battery()
    top_cpu = _process_rows(3, sort_by="cpu", sample=True)
    top_mem = _process_rows(3, sort_by="memory")
    findings: List[str] = []
    if cpu >= 85:
        findings.append(f"CPU is very busy ({cpu}% overall).")
    elif cpu >= 60:
        findings.append(f"CPU is moderately busy ({cpu}% overall).")
    else:
        findings.append(f"CPU load is normal ({cpu}% overall).")
    if memory.percent >= 90:
        findings.append(f"Memory is nearly full ({memory.percent}% used; "
                        f"{_bytes_to_gb(memory.available)} GB available).")
    elif memory.percent >= 75:
        findings.append(f"Memory is under pressure ({memory.percent}% used).")
    else:
        findings.append(f"Memory use is normal ({memory.percent}%).")
    if swap.total and swap.percent >= 40:
        findings.append(f"Swap is heavily used ({swap.percent}%).")
    for drive in disks:
        if drive["percent_used"] >= 90:
            findings.append(
                f"{drive['drive']} is almost full: {drive['free_gb']} GB free "
                f"({drive['percent_used']}% used).")
    if battery is not None and not battery.power_plugged and battery.percent <= 20:
        findings.append(f"Battery is low ({round(battery.percent)}%) and the "
                        "charger is not connected.")
    return {
        "cpu_percent": cpu,
        "memory_percent": memory.percent,
        "memory_available_gb": _bytes_to_gb(memory.available),
        "swap_percent": swap.percent,
        "top_cpu": top_cpu,
        "top_memory": top_mem,
        "findings": findings,
        "note": ("These are measurements, not conclusions: a high-usage "
                 "process is a lead, not proof of the cause."),
    }


# ── audio (existing plumbing in core.audio_devices) ─────────────────────────
def _audio_module():
    from core import audio_devices
    return audio_devices


def _volume_state(device: str = "") -> Dict[str, Any]:
    audio = _audio_module()
    value = audio.get_output_volume(device)
    if value is None:
        return {"supported": False,
                "message": "Windows did not report a volume for this output."}
    endpoint = audio._endpoint_for(device)  # noqa: SLF001 - shared endpoint
    muted = None
    if endpoint is not None:
        try:
            muted = bool(endpoint.GetMute())
        except Exception:
            muted = None
    return {"supported": True, "volume": value, "muted": muted,
            "device": device or "system default"}


def _volume_set(percent: int, device: str = "") -> Dict[str, Any]:
    audio = _audio_module()
    if not audio.set_output_volume(percent, device):
        return _fail("set_volume", "failed",
                     "The volume change was not accepted by Windows.")
    after = audio.get_output_volume(device)
    if after is None:
        return _fail("set_volume", "not_verified",
                     "The change was sent but the volume could not be re-read.")
    if abs(after - percent) > 2:
        return _fail("set_volume", "not_verified",
                     f"Requested {percent}% but Windows reports {after}%.",
                     volume=after)
    return _ok("set_volume", f"Volume set to {after}%.", volume=after,
               device=device or "system default", verified=True)


def _mute_set(muted: bool, device: str = "") -> Dict[str, Any]:
    audio = _audio_module()
    endpoint = audio._endpoint_for(device)  # noqa: SLF001
    if endpoint is None:
        return _unsupported("set_mute",
                            "No controllable output endpoint was found.")
    try:
        endpoint.SetMute(1 if muted else 0, None)
        after = bool(endpoint.GetMute())
    except Exception as exc:  # noqa: BLE001
        return _fail("set_mute", "failed",
                     f"Windows rejected the mute change: {exc}")
    if after != bool(muted):
        return _fail("set_mute", "not_verified",
                     "The mute state did not change.", muted=after)
    return _ok("set_mute", "Audio muted." if muted else "Audio unmuted.",
               muted=after, device=device or "system default")


# ── network ─────────────────────────────────────────────────────────────────
def _wifi_details() -> Dict[str, Any]:
    if not IS_WINDOWS:
        return {"supported": False}
    code, out, _err = _run(["netsh", "wlan", "show", "interfaces"], timeout=4)
    if code != 0 or not out.strip():
        return {"supported": False,
                "message": "Wi-Fi details are not available."}
    details: Dict[str, Any] = {"supported": True}
    patterns = {
        "ssid": r"^\s*SSID\s*:\s*(.+)$",
        "state": r"^\s*State\s*:\s*(.+)$",
        "signal": r"^\s*Signal\s*:\s*(.+)$",
        "radio": r"^\s*Radio type\s*:\s*(.+)$",
        "authentication": r"^\s*Authentication\s*:\s*(.+)$",
    }
    for line in out.splitlines():
        for key, pattern in patterns.items():
            match = re.match(pattern, line, re.IGNORECASE)
            if match and key not in details:
                details[key] = match.group(1).strip()
    # Never include keys, profiles or passwords: only the fields above.
    if "ssid" not in details:
        details["connected"] = False
    else:
        details["connected"] = str(details.get("state", "")).casefold() == "connected"
    return details


def _internet_reachable() -> bool:
    try:
        with socket.create_connection(("1.1.1.1", 53),
                                      timeout=NET_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def _network_info(probe: bool = False) -> Dict[str, Any]:
    interfaces: List[Dict[str, Any]] = []
    stats = psutil.net_if_stats()
    addresses = psutil.net_if_addrs()
    for name, stat in stats.items():
        if not stat.isup:
            continue
        folded = name.casefold()
        if "loopback" in folded or "pseudo" in folded:
            continue
        rows = []
        for address in addresses.get(name, []):
            if address.family == socket.AF_INET:
                rows.append({"type": "IPv4", "address": address.address})
            elif address.family == socket.AF_INET6 and "%" not in address.address:
                rows.append({"type": "IPv6", "address": address.address})
        if not rows:
            continue
        interfaces.append({
            "name": name,
            "speed_mbps": stat.speed or None,
            "addresses": rows,
        })
    return {
        "hostname": socket.gethostname(),
        "interfaces": interfaces,
        "connected": bool(interfaces),
        "wifi": _wifi_details(),
        "internet_reachable": _internet_reachable() if probe else None,
    }


# ── opening settings / utilities (fixed allowlist) ─────────────────────────
def _start_target(target: str) -> None:
    if IS_WINDOWS:
        os.startfile(target)  # type: ignore[attr-defined]
        return
    subprocess.Popen([target], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def _open_settings(page: str) -> Dict[str, Any]:
    key = str(page or "").strip().casefold().replace(" ", "_")
    target = _SETTINGS_PAGES.get(key)
    if target is None:
        return _fail("open_settings", "invalid_argument",
                     f"Unknown settings page '{page}'. Supported: "
                     + ", ".join(sorted(_SETTINGS_PAGES)),
                     available=sorted(_SETTINGS_PAGES))
    try:
        _start_target(target)
    except Exception as exc:  # noqa: BLE001
        return _fail("open_settings", "failed",
                     f"Windows could not open the settings page: {exc}")
    return _ok("open_settings", f"Opened the {key.replace('_', ' ')} page.",
               page=key, target=target, setting_changed=False)


def _open_utility(utility: str) -> Dict[str, Any]:
    key = str(utility or "").strip().casefold().replace(" ", "_")
    target = _UTILITIES.get(key)
    if target is None:
        return _fail("open_system_utility", "invalid_argument",
                     f"Unknown utility '{utility}'. Supported: "
                     + ", ".join(sorted(_UTILITIES)),
                     available=sorted(_UTILITIES))
    try:
        _start_target(target)
    except Exception as exc:  # noqa: BLE001
        return _fail("open_system_utility", "failed",
                     f"Windows could not open '{target}': {exc}")
    return _ok("open_system_utility", f"Opened {key.replace('_', ' ')}.",
               utility=key, target=target)


# ── dispatcher ──────────────────────────────────────────────────────────────
def _windows_lock_workstation() -> bool:
    """Native Windows workstation lock; never logs out or closes apps."""
    return bool(ctypes.windll.user32.LockWorkStation())


def _windows_sleep_workstation() -> bool:
    """Request S3 sleep (never hibernate/shutdown/restart)."""
    # SetSuspendState(bHibernate=0, bForce=0, bWakeupEventsDisabled=0)
    return bool(ctypes.windll.powrprof.SetSuspendState(0, 0, 0))


def _lock_workstation() -> Dict[str, Any]:
    unsupported = _windows_only("lock_workstation")
    if unsupported:
        return unsupported
    try:
        locked = _windows_lock_workstation()
    except Exception as exc:  # noqa: BLE001 - native API may be unavailable
        return _fail("lock_workstation", "failed",
                     f"Windows could not lock the workstation: "
                     f"{type(exc).__name__}: {exc}")
    if not locked:
        return _fail("lock_workstation", "failed",
                     "Windows did not accept the workstation lock request.")
    return _ok("lock_workstation", "Workstation locked.",
               locked=True, session_locked=True)


def _sleep_workstation() -> Dict[str, Any]:
    unsupported = _windows_only("sleep_workstation")
    if unsupported:
        return unsupported
    try:
        sleeping = _windows_sleep_workstation()
    except Exception as exc:  # noqa: BLE001 - native API may be unavailable
        return _fail("sleep_workstation", "unavailable",
                     f"Windows could not request sleep: "
                     f"{type(exc).__name__}: {exc}")
    if not sleeping:
        return _fail(
            "sleep_workstation", "unavailable",
            "Windows did not accept the sleep request. Sleep may be "
            "unavailable or disabled in power settings; nothing was "
            "shut down, restarted, logged out or hibernated.")
    return _ok(
        "sleep_workstation",
        "Sleep requested. The workstation may disconnect the current "
        "session while it is suspended.",
        sleep_requested=True,
        session_may_close=True,
        terminal=True,
    )


def system_control(
    operation: str,
    value: Any = "",
    delta: Any = 0,
    pid: Any = 0,
    limit: Any = 0,
    sort_by: str = "",
    device: str = "",
    path: str = "",
    page: str = "",
    utility: str = "",
    display_index: Any = -1,
) -> Dict[str, Any]:
    """Inspect and control Windows through one validated operation.

    Read-only: ``get_system_info``, ``get_cpu_info``, ``get_memory_info``,
    ``get_disk_info``, ``get_battery``, ``get_processes``, ``get_top_processes``,
    ``get_process_details``, ``diagnose_performance``, ``get_brightness``,
    ``get_volume``, ``get_audio_devices``, ``get_display_info``,
    ``get_network_info``.

    Reversible: ``set_brightness`` (``value``, optional ``display_index``),
    ``adjust_brightness`` (``delta``), ``set_volume`` (``value``),
    ``adjust_volume`` (``delta``), ``set_mute`` (``value`` 1/0).

    Navigation: ``open_settings`` (``page``) and ``open_system_utility``
    (``utility``) from a fixed allowlist; these never change a setting.

    Consequential: ``lock_workstation`` locks the current Windows session
    without logging out or closing applications. ``sleep_workstation``
    requests Windows sleep and never substitutes shutdown, restart, logout or
    hibernation; it may disconnect the Live session.
    """
    action = str(operation or "").strip().casefold()
    count = 0
    try:
        count = int(float(limit)) if str(limit).strip() not in ("", "0") else 0
    except (TypeError, ValueError):
        count = 0

    if action == "get_system_info":
        return _ok(action, "System information.", **_system_info())
    if action == "get_cpu_info":
        return _ok(action, "CPU information.", **_cpu_info())
    if action == "get_memory_info":
        return _ok(action, "Memory information.", **_memory_info())
    if action == "get_disk_info":
        data = _disk_info(str(path or ""))
        if not data["drives"]:
            return _fail(action, "not_found",
                         "No readable drive matched that path.", path=path)
        return _ok(action, "Disk information.", **data)
    if action == "get_battery":
        data = _battery_info()
        if not data.get("supported"):
            return _unsupported(action, data["message"])
        return _ok(action, "Battery status.", **data)
    if action == "lock_workstation":
        return _lock_workstation()
    if action == "sleep_workstation":
        return _sleep_workstation()
    if action == "get_display_info":
        monitors = _displays()
        if not monitors:
            return _unsupported(action,
                                "No display information is available.")
        return _ok(action, "Display information.", monitors=monitors,
                   count=len(monitors))
    if action == "get_brightness":
        state = _brightness_state()
        if not state.get("supported"):
            return _unsupported(action, state["message"])
        return _ok(action, "Brightness.", **state)
    if action == "set_brightness":
        percent, error = _clamp(value)
        if error:
            return _fail(action, "invalid_argument", error)
        index = int(display_index)
        if index >= 0:
            return _ddc_set(index, percent, "set_brightness")
        return _brightness_set(percent, "set_brightness")
    if action == "adjust_brightness":
        try:
            step = int(round(float(delta))) if str(delta).strip() not in ("", "0") else 10
        except (TypeError, ValueError):
            return _fail(action, "invalid_argument",
                         "delta must be a number of percent.")
        index = int(display_index)
        if index >= 0:
            monitors = _ddc_monitors()
            current = next((item["brightness"] for item in monitors
                            if item["index"] == index), None)
            if current is None:
                return _fail(action, "invalid_display",
                             f"No external display with index {index}.")
            return _ddc_set(index, max(0, min(100, current + step)),
                            "adjust_brightness")
        state = _brightness_state()
        if not state.get("supported") or state.get("internal") is None:
            return _unsupported(action,
                                "Brightness control is not available here.")
        target = max(0, min(100, int(state["internal"]) + step))
        return _brightness_set(target, "adjust_brightness")
    if action == "get_volume":
        state = _volume_state(str(device or ""))
        if not state.get("supported"):
            return _unsupported(action, state["message"])
        return _ok(action, "Volume.", **state)
    if action == "set_volume":
        percent, error = _clamp(value)
        if error:
            return _fail(action, "invalid_argument", error)
        return _volume_set(percent, str(device or ""))
    if action == "adjust_volume":
        try:
            step = int(round(float(delta))) if str(delta).strip() not in ("", "0") else 10
        except (TypeError, ValueError):
            return _fail(action, "invalid_argument",
                         "delta must be a number of percent.")
        state = _volume_state(str(device or ""))
        if not state.get("supported"):
            return _unsupported(action, state["message"])
        target = max(0, min(100, int(state["volume"]) + step))
        return _volume_set(target, str(device or ""))
    if action == "set_mute":
        raw = str(value).strip().casefold()
        if raw in ("1", "true", "on", "yes", "mute", "muted"):
            muted = True
        elif raw in ("0", "false", "off", "no", "unmute", "unmuted"):
            muted = False
        else:
            return _fail(action, "invalid_argument",
                         "value must be 1 (mute) or 0 (unmute).")
        return _mute_set(muted, str(device or ""))
    if action == "get_mute":
        audio = _audio_module()
        endpoint = audio._endpoint_for(str(device or ""))  # noqa: SLF001
        if endpoint is None:
            return _unsupported(action,
                                "No controllable output endpoint was found.")
        try:
            muted = bool(endpoint.GetMute())
        except Exception as exc:  # noqa: BLE001
            return _fail(action, "failed",
                         f"Windows did not report the mute state: {exc}")
        return _ok(action, "Audio is muted." if muted else "Audio is unmuted.",
                   muted=muted, device=str(device or "") or "system default")
    if action == "get_audio_devices":
        audio = _audio_module()
        try:
            devices = audio.list_devices("output", refresh=True)
        except Exception as exc:  # noqa: BLE001
            return _fail(action, "failed", f"Could not list audio devices: {exc}")
        return _ok(action, f"{len(devices)} output device(s).",
                   devices=devices, count=len(devices),
                   selected=config_output_device())
    if action == "set_audio_output":
        return _unsupported(
            action,
            "Windows does not provide a stable, documented API to change the "
            "default output device without third-party drivers. Open Sound "
            "settings and choose the device manually.",
            devices=_safe_output_devices())
    if action == "get_processes":
        limit_value = max(1, min(MAX_PROCESSES, count or DEFAULT_PROCESSES))
        metric = str(sort_by or "memory").strip().casefold()
        if metric not in ("cpu", "memory"):
            return _fail(action, "invalid_argument",
                         "sort_by must be 'cpu' or 'memory'.")
        rows = _process_rows(limit_value, sort_by=metric,
                             sample=(metric == "cpu"))
        return _ok(action, f"{len(rows)} process(es) by {metric}.",
                   processes=rows, sort_by=metric, count=len(rows))
    if action == "get_top_processes":
        limit_value = max(1, min(MAX_TOP, count or DEFAULT_TOP))
        metric = str(sort_by or "cpu").strip().casefold()
        if metric not in ("cpu", "memory"):
            return _fail(action, "invalid_argument",
                         "sort_by must be 'cpu' or 'memory'.")
        rows = _process_rows(limit_value, sort_by=metric, sample=True)
        return _ok(action, f"Top {len(rows)} process(es) by {metric}.",
                   processes=rows, sort_by=metric, count=len(rows))
    if action == "get_process_details":
        try:
            process_id = int(pid)
        except (TypeError, ValueError):
            return _fail(action, "invalid_argument", "pid is required.")
        if process_id <= 0:
            return _fail(action, "invalid_argument", "pid must be positive.")
        try:
            details = _process_details(process_id)
        except FileNotFoundError as exc:
            return _fail(action, "not_found", str(exc), pid=process_id)
        except PermissionError as exc:
            return _fail(action, "denied", str(exc), pid=process_id)
        return _ok(action, "Process details.", process=details)
    if action == "diagnose_performance":
        data = _diagnose()
        data["internet_reachable"] = _internet_reachable()
        return _ok(action, "Performance diagnosis.", **data)
    if action == "get_network_info":
        return _ok(action, "Network information. Reachability is checked by "
                           "diagnose_performance.", **_network_info())
    if action == "open_settings":
        return _open_settings(page)
    if action == "open_system_utility":
        return _open_utility(utility)
    return _fail(action or "unknown", "unsupported",
                 "Unknown operation. Use a supported system_control "
                 "operation.", available=sorted(_OPERATIONS))


def config_output_device() -> str:
    try:
        import config
        return str(config.get_output_device() or "system default")
    except Exception:  # noqa: BLE001
        return "system default"


def _safe_output_devices() -> List[str]:
    try:
        return _audio_module().list_devices("output", refresh=False)
    except Exception:  # noqa: BLE001
        return []


_OPERATIONS = (
    "get_system_info", "get_cpu_info", "get_memory_info", "get_disk_info",
    "get_battery", "get_processes", "get_top_processes",
    "get_process_details", "diagnose_performance", "get_brightness",
    "set_brightness", "adjust_brightness", "get_volume", "set_volume",
    "adjust_volume", "set_mute", "get_mute", "get_audio_devices",
    "set_audio_output", "get_display_info", "get_network_info",
    "lock_workstation", "sleep_workstation",
    "open_settings", "open_system_utility",
)
