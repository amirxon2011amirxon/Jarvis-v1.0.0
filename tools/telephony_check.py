"""Standalone SIP check for the Jarvis telephony engine.

This is intentionally independent of Gemini and the web UI.  It is the
real-phone test harness described in the telephony notes:

    # fill SIP_PASSWORD in the project-root .env
    python tools/telephony_check.py register
    python tools/telephony_check.py call Amirxon
    python tools/telephony_check.py serve

``register`` and ``call`` are not considered successful unless the real SIP
server reports it.  ``serve`` waits for a real incoming call, prints the caller
and answers when you press Enter.  With ``--audio`` the computer microphone and
speaker are bridged to the call through the same RTP engine Jarvis uses; this is
only for the standalone check, never the Jarvis voice path.

The password is read from the project-root .env (SIP_PASSWORD) or the
JARVIS_SIP_PASSWORD environment override.  It is never printed or written to
disk.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

# Running this file directly puts tools/ (not the repo root) on sys.path.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import telephony


class AudioLoop:
    """Small sounddevice loopback used only by this standalone checker."""

    def __init__(self) -> None:
        import sounddevice as sd

        self.sd = sd
        self.input = sd.RawInputStream(
            samplerate=telephony.GEMINI_OUT_RATE,
            channels=1,
            dtype="int16",
            blocksize=480,
            callback=self._on_mic,
        )
        self.output = sd.RawOutputStream(
            samplerate=telephony.GEMINI_IN_RATE,
            channels=1,
            dtype="int16",
            blocksize=320,
            callback=self._on_speaker,
        )
        self.service = telephony.get_service()
        self._pending = bytearray()
        self._lock = threading.Lock()

    def start(self) -> None:
        self.input.start()
        self.output.start()

    def stop(self) -> None:
        for stream in (self.input, self.output):
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def _on_mic(self, indata, frames, time_info, status) -> None:
        if not self.service.bridge_active():
            return
        try:
            self.service.send_audio_24k(bytes(indata))
        except Exception:
            pass

    def _on_speaker(self, outdata, frames, time_info, status) -> None:
        with self._lock:
            need = len(outdata)
            while len(self._pending) < need:
                self._pending.extend(b"\x00\x00")
            chunk = bytes(self._pending[:need])
            del self._pending[:need]
        outdata[:] = chunk

    def caller_audio(self, _call_id: str, pcm16k: bytes) -> None:
        with self._lock:
            self._pending.extend(pcm16k)
            # Keep the local monitor bounded; the phone call remains real-time.
            limit = telephony.GEMINI_IN_RATE * 2 // 2
            if len(self._pending) > limit:
                del self._pending[:-limit]


def _print_status(service: telephony.TelephonyService) -> None:
    print(json.dumps(service.status(), indent=2, ensure_ascii=False))


def _wait_until(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def _call_is_active(service: telephony.TelephonyService) -> bool:
    return any(call.get("state") == "active" for call in service.status()["calls"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("register", "call", "serve"),
        help="register only, place a call, or wait for an incoming call")
    parser.add_argument("target", nargs="?", default="",
                        help="contact name or sip:user@domain for 'call'")
    parser.add_argument("--no-audio", action="store_true",
                        help="signalling only; do not open microphone/speaker")
    parser.add_argument("--seconds", type=int, default=120,
                        help="maximum call/watch duration (default 120)")
    args = parser.parse_args(argv)

    service = telephony.get_service()
    service.configure(
        log=lambda text: print(text, flush=True),
        publish=lambda event: print(
            json.dumps(event, ensure_ascii=False), flush=True),
    )
    audio = None
    if not args.no_audio and args.command in ("call", "serve"):
        try:
            audio = AudioLoop()
            service.configure(audio_bridge=audio.caller_audio)
        except Exception as exc:  # noqa: BLE001 - signalling still works
            print(f"[audio] disabled: {type(exc).__name__}: {exc}")

    try:
        if args.command == "register":
            service.start()
            if not _wait_until(
                    lambda: service.registration().get("registered"), 25):
                _print_status(service)
                print("REGISTRATION FAILED — the provider did not accept it.")
                return 2
            _print_status(service)
            print("REGISTRATION OK")
            return 0

        if args.command == "call":
            if not args.target:
                print("A contact name or SIP address is required for 'call'.")
                return 2
            result = service.call(args.target)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            if not result.get("ok"):
                return 2
            if audio:
                audio.start()
            if not _wait_until(lambda: _call_is_active(service), 45):
                print("CALL DID NOT CONNECT")
                _print_status(service)
                service.hangup()
                return 3
            print("CALL CONNECTED — speak into the microphone. Ctrl+C ends it.")
            deadline = time.monotonic() + max(5, args.seconds)
            while time.monotonic() < deadline:
                status = service.status()
                if not any(c.get("state") in ("active", "connecting")
                           for c in status["calls"]):
                    print("CALL ENDED")
                    _print_status(service)
                    return 0
                time.sleep(0.5)
            service.hangup()
            return 0

        if args.command == "serve":
            print("Waiting for an incoming SIP call. Ctrl+C stops.")
            seen: set[str] = set()
            deadline = time.monotonic() + max(10, args.seconds)
            while time.monotonic() < deadline:
                status = service.status()
                incoming = [c for c in status["calls"]
                            if c.get("state") == "incoming"]
                for call in incoming:
                    if call["call_id"] in seen:
                        continue
                    seen.add(call["call_id"])
                    print(f"INCOMING {call['call_id']} from "
                          f"{call.get('remote_display') or call.get('remote')}")
                    answer = input("Answer? [y/N] ").strip().lower()
                    if answer in ("y", "yes"):
                        result = service.answer(call["call_id"])
                        print(json.dumps(result, indent=2, ensure_ascii=False))
                        if result.get("ok") and audio:
                            audio.start()
                            print("CALL CONNECTED — audio bridged locally.")
                        else:
                            print("CALL NOT CONNECTED")
                    else:
                        service.reject(call["call_id"])
                active = [c for c in status["calls"]
                          if c.get("state") == "active"]
                if active and audio:
                    time.sleep(0.5)
                    continue
                time.sleep(0.5)
            return 0
    except telephony.TelephonyError as exc:
        # Validation errors are actionable and never contain the password.
        print(f"SIP configuration error: {exc}")
        return 2
    except KeyboardInterrupt:
        print("\nStopping.")
        return 0
    finally:
        if audio:
            audio.stop()
        # A checker run must not leave a real call up after the process exits.
        try:
            if any(c.get("state") in ("active", "incoming", "connecting")
                   for c in service.status()["calls"]):
                service.hangup()
        except Exception:
            pass
        telephony.shutdown()


if __name__ == "__main__":
    sys.exit(main())
