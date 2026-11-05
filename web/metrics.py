"""Lightweight system telemetry for the web HUD.

The desktop HUD reads the same sort of numbers from ``ui._SysMetrics``.  This
module is deliberately independent of any UI toolkit so the web server starts without a
Qt import; it only reports what the operating system actually exposes and uses
``-1`` for unavailable GPU/temperature/frequency readings.

Hardware values are collected on one background thread (never in a request
handler path) with the same cadence as before: cheap counters every 2s, slow
sensors (GPU/temperature) every 6s, and the drive inventory every 60s so an
unresponsive removable volume cannot stall the steady numbers.
"""
from __future__ import annotations

import platform
import threading
import time

import psutil

_OS = platform.system()


def _nvml_gpu_windows() -> float:
    """NVIDIA utilisation through nvml.dll, without spawning a process."""
    try:
        import ctypes

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        lib = None
        for name in ("nvml", r"C:\Windows\System32\nvml.dll"):
            try:
                lib = ctypes.WinDLL(name)
                lib.nvmlInit_v2()
                break
            except Exception:
                lib = None
        if lib is None:
            return -1.0
        device = ctypes.c_void_p()
        lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(device))
        util = _Util()
        lib.nvmlDeviceGetUtilizationRates(device, ctypes.byref(util))
        return float(util.gpu)
    except Exception:
        return -1.0


_EDITION_LABELS = {
    "Professional": "Pro",
    "ProfessionalN": "Pro N",
    "ProfessionalWorkstation": "Pro for Workstations",
    "ProfessionalSingleLanguage": "Pro Single Language",
    "ProfessionalCountrySpecific": "Pro Country Specific",
    "Core": "Home",
    "CoreN": "Home N",
    "CoreSingleLanguage": "Home Single Language",
    "CoreCountrySpecific": "Home Country Specific",
    "Enterprise": "Enterprise",
    "EnterpriseN": "Enterprise N",
    "Education": "Education",
    "EducationN": "Education N",
    "ServerStandard": "Server Standard",
    "ServerDatacenter": "Server Datacenter",
}


def _windows_identity() -> tuple[str, str]:
    """(name, version) from the registry, with neutral fallbacks.

    Known quirk: Windows 11 machines still report ProductName "Windows 10 ...",
    so the CurrentBuildNumber is the authority for the major version
    (build >= 22000 means Windows 11).  The edition comes from EditionID,
    and DisplayVersion (e.g. "24H2") is the friendly version.
    """
    if _OS != "Windows":
        return (f"{_OS} {platform.release()}", platform.version())
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
        try:
            def _read(name: str) -> str:
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                    return str(value)
                except OSError:
                    return ""

            product_name = _read("ProductName")
            edition_id = _read("EditionID")
            display_version = _read("DisplayVersion") or _read("ReleaseId")
            build = _read("CurrentBuildNumber")
        finally:
            key.Close()

        try:
            build_number = int(build.split(".")[0])
        except ValueError:
            build_number = 0
        edition = _EDITION_LABELS.get(edition_id, edition_id or "")
        if build_number >= 22000:
            name = f"Windows 11 {edition}".strip()
        elif product_name:
            name = product_name
        else:
            name = f"Windows 10 {edition}".strip()
        return (name, display_version or build)
    except Exception:
        return (f"Windows {platform.release()}", platform.version())


def _drives(limit: int = 6) -> list[dict]:
    """Local fixed drives only, degraded gracefully.

    Removable/network volumes frequently hang on query; ``all=False`` keeps the
    call to local partitions, and any single failing drive is skipped without
    aborting the rest of the inventory.
    """
    out: list[dict] = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception:
        return out
    for part in partitions:
        opts = (part.opts or "").lower()
        if "cdrom" in opts or "removable" in opts:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except Exception:
            continue
        total_gb = usage.total / 1e9
        if total_gb <= 0:
            continue
        device = str(part.device or part.mountpoint)
        if len(device) >= 2 and device[1] == ":":
            letter = device[:2]
        else:
            letter = part.mountpoint.rstrip("\\/") or "?"
        if total_gb < 1:  # tiny ESP/ramdisks are noise in the panel
            continue
        out.append({
            "letter": letter,
            "used_gb": round((usage.total - usage.free) / 1e9, 1),
            "total_gb": round(total_gb, 1),
            "percent": round(usage.percent, 1),
        })
        if len(out) >= limit:
            break
    return out


class SysMetrics:
    def __init__(self) -> None:
        self.cpu = 0.0
        self.mem = 0.0
        self.mem_used_gb = 0.0
        self.mem_total_gb = 0.0
        self.net = 0.0
        self.gpu = -1.0
        self.tmp = -1.0
        self.freq_ghz = -1.0
        self.drives: list[dict] = []
        self.os_name, self.os_version = _windows_identity()
        self.boot_time = psutil.boot_time()
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        self._slow_tick = 0
        self._drive_tick = 0
        self._gpu_probed = False
        thread = threading.Thread(target=self._loop, daemon=True,
                                  name="web-metrics")
        thread.start()

    def _loop(self) -> None:
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(2.0)

    def _update(self) -> None:
        cpu = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        mem_pct = memory.percent
        mem_used_gb = memory.used / 1e9
        mem_total_gb = memory.total / 1e9
        counters = psutil.net_io_counters()
        now = time.time()
        elapsed = now - self._last_net_t
        if elapsed > 0:
            sent = (counters.bytes_sent - self._last_net.bytes_sent) / elapsed
            recv = (counters.bytes_recv - self._last_net.bytes_recv) / elapsed
            net = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net = counters
        self._last_net_t = now

        try:
            freq = psutil.cpu_freq()
            freq_ghz = round(freq.current / 1000, 2) if freq and freq.current \
                else -1.0
        except Exception:
            freq_ghz = -1.0

        self._slow_tick = (self._slow_tick + 1) % 3
        if self._slow_tick == 1:
            gpu = self._read_gpu()
            tmp = self._read_temp()
        else:
            gpu = self.gpu
            tmp = self.tmp

        self._drive_tick = (self._drive_tick + 1) % 30
        if self._drive_tick == 0 or not self.drives:
            try:
                drives = _drives()
            except Exception:
                drives = self.drives
        else:
            drives = self.drives

        with self._lock:
            self.cpu = cpu
            self.mem = mem_pct
            self.mem_used_gb = mem_used_gb
            self.mem_total_gb = mem_total_gb
            self.net = net
            self.gpu = gpu
            self.tmp = tmp
            self.freq_ghz = freq_ghz
            self.drives = drives

    def _read_gpu(self) -> float:
        if self._gpu_probed and self.gpu < 0:
            return self.gpu
        self._gpu_probed = True
        try:
            import pynvml  # type: ignore

            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            return float(pynvml.nvmlDeviceGetUtilizationRates(handle).gpu)
        except Exception:
            pass
        if _OS == "Windows":
            return _nvml_gpu_windows()
        return -1.0

    def _read_temp(self) -> float:
        try:
            temps = psutil.sensors_temperatures()
            for name in ("coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"):
                if name in temps and temps[name]:
                    return float(temps[name][0].current)
            for entries in temps.values():
                if entries:
                    return float(entries[0].current)
        except Exception:
            pass
        return -1.0

    def snapshot(self) -> dict:
        with self._lock:
            uptime = time.time() - self.boot_time
            return {
                "cpu": round(float(self.cpu), 1),
                "mem": round(float(self.mem), 1),
                "mem_used_gb": round(float(self.mem_used_gb), 1),
                "mem_total_gb": round(float(self.mem_total_gb), 1),
                "net": round(float(self.net), 2),
                "gpu": round(float(self.gpu), 1),
                "tmp": round(float(self.tmp), 1),
                "freq_ghz": round(float(self.freq_ghz), 2),
                "cpu_temp": self.tmp,  # backward-compatible alias
                "drives": [dict(drive) for drive in self.drives],
                "os_name": self.os_name,
                "os_version": self.os_version,
                "uptime_s": int(uptime),
            }

    def stop(self) -> None:
        self._running = False
