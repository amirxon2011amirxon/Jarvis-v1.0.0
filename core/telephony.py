"""SIP/VoIP telephony for Jarvis.

This module is deliberately independent of the voice engine.  It owns the SIP
signalling, RTP media, registration state and call history; ``main.py`` only
supplies a 16 kHz PCM callback for caller audio and hands 24 kHz PCM replies
back to the active call.  The existing STT, LLM and TTS pipeline is therefore
used unchanged.

The implementation is a small SIP user agent over TLS with G.711 audio.  It was
written against the Linphone Free SIP Service (Flexisip) which uses digest MD5
authentication with ``qop=auth``; nothing in the signalling layer is Linphone
specific.  A missing password is reported, never guessed, and no call is ever
placed unless the ``call`` action is invoked explicitly.

Threading model:
  * one TLS signalling thread (connect, register, read requests/responses);
  * one RTP thread per active call (20 ms pacing, tx and rx);
  * action calls are short and wait on events rather than the media threads.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import select
import socket
import ssl
import struct
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import audioop

try:  # optional at import time; calls that request SRTP fail clearly without it
    import pylibsrtp
except Exception:  # noqa: BLE001 - a missing native wheel must not break startup
    pylibsrtp = None

import config

# ─────────────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────────────

SIP_RATE = 8000
PCM_FRAME_BYTES = 320              # 20 ms of 16-bit mono at 8 kHz
RTP_PTIME = 0.020
RTP_PAYLOAD_PCMU = 0
RTP_PAYLOAD_PCMA = 8
RTP_PAYLOAD_DTMF = 101

GEMINI_IN_RATE = 16000
GEMINI_OUT_RATE = 24000
GEMINI_OUT_FRAME_BYTES = 960       # 20 ms of 16-bit mono at 24 kHz

# SDES SRTP suite used by Linphone mobile. AES_CM_128_HMAC_SHA1_80 needs a
# 16-byte master key plus a 14-byte master salt (30 bytes) encoded base64 in SDP.
SRTP_SUITE = "AES_CM_128_HMAC_SHA1_80"
SRTP_KEY_BYTES = 30

MAX_TX_SECONDS = 1.0
MAX_RX_PACKETS = 40
RING_TIMEOUT = 60.0
ANSWER_ACK_TIMEOUT = 10.0
REGISTER_TIMEOUT = 20.0
RECONNECT_BACKOFF = (2.0, 4.0, 8.0, 16.0, 30.0)
KEEPALIVE_SECONDS = 20.0
MAX_SIP_BUFFER = 1024 * 1024

USER_AGENT = "Jarvis-SIP/1.0"


class TelephonyError(Exception):
    """A user-correctable telephony failure (configuration or call state)."""


class SipProtocolError(TelephonyError):
    """A malformed or unsupported SIP message."""


# ─────────────────────────────────────────────────────────────────────────────
#  Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(
        ts if ts is not None else _now(), timezone.utc).isoformat(
            timespec="seconds")


def _new_tag() -> str:
    return uuid.uuid4().hex[:12]


def _new_branch() -> str:
    return "z9hG4bK" + uuid.uuid4().hex


def _random_ssrc() -> int:
    return random.getrandbits(32)


def _redact_uri(uri: str) -> str:
    """Keep a SIP address useful in logs without ever leaking a password.

    SIP URIs do not normally carry passwords, but a hand-written URI can use
    ``user:password@host``.  Callers only ever see this sanitised form.
    """
    text = str(uri or "").strip()
    if ":" in text and "@" in text:
        scheme, rest = text.split(":", 1)
        if scheme.lower() in ("sip", "sips", "tel") and "@" in rest:
            userinfo, host = rest.rsplit("@", 1)
            if ":" in userinfo:
                userinfo = userinfo.split(":", 1)[0]
            return f"{scheme}:{userinfo}@{host}"
    return text


def _parse_number(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is None:
        return default
    return bool(value)


# ─────────────────────────────────────────────────────────────────────────────
#  SIP messages
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SipMessage:
    start: str
    headers: List[Tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    @property
    def is_response(self) -> bool:
        return self.start.upper().startswith("SIP/2.0 ")

    @property
    def method(self) -> str:
        if self.is_response:
            return ""
        return self.start.split(" ", 1)[0].upper()

    @property
    def uri(self) -> str:
        if self.is_response:
            return ""
        parts = self.start.split(" ", 2)
        return parts[1] if len(parts) > 1 else ""

    @property
    def status_code(self) -> int:
        if not self.is_response:
            return 0
        parts = self.start.split(" ", 2)
        try:
            return int(parts[1])
        except (IndexError, ValueError):
            return 0

    @property
    def reason(self) -> str:
        if not self.is_response:
            return ""
        parts = self.start.split(" ", 2)
        return parts[2] if len(parts) > 2 else ""

    def first(self, name: str, default: str = "") -> str:
        wanted = name.strip().lower()
        for key, value in self.headers:
            if key.strip().lower() == wanted:
                return value
        return default

    def all(self, name: str) -> List[str]:
        wanted = name.strip().lower()
        return [value for key, value in self.headers
                if key.strip().lower() == wanted]

    def body_text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def build(self) -> bytes:
        lines = [self.start]
        for key, value in self.headers:
            lines.append(f"{key}: {value}")
        head = "\r\n".join(lines) + "\r\n\r\n"
        return head.encode("latin-1", "replace") + self.body


def parse_sip_message(raw: bytes) -> SipMessage:
    if b"\r\n\r\n" in raw:
        head, body = raw.split(b"\r\n\r\n", 1)
    elif b"\n\n" in raw:
        head, body = raw.split(b"\n\n", 1)
    else:
        raise SipProtocolError("SIP message has no header terminator")
    text = head.decode("latin-1", "replace").replace("\r\n", "\n")
    lines = text.split("\n")
    if not lines or not lines[0].strip():
        raise SipProtocolError("SIP message has no start line")
    start = lines[0].strip()
    headers: List[Tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        if line[:1] in (" ", "\t") and headers:
            key, value = headers[-1]
            headers[-1] = (key, f"{value} {line.strip()}")
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip():
            headers.append((key.strip(), value.strip()))
    return SipMessage(start=start, headers=headers, body=body)


def extract_sip_messages(buffer: bytearray) -> List[SipMessage]:
    """Pull complete SIP messages out of a TLS/TCP stream."""
    out: List[SipMessage] = []
    while buffer:
        # RFC 5626 CRLF keepalive.  Ignore empty leading lines.
        while buffer.startswith(b"\r\n") or buffer.startswith(b"\n"):
            del buffer[:2 if buffer.startswith(b"\r\n") else 1]
        if not buffer:
            break
        if b"\r\n\r\n" in buffer:
            head_end = buffer.index(b"\r\n\r\n") + 4
        elif b"\n\n" in buffer:
            head_end = buffer.index(b"\n\n") + 2
        else:
            break
        head = bytes(buffer[:head_end]).decode("latin-1", "replace")
        match = re.search(
            r"(?im)^Content-Length\s*:\s*(\d+)\s*$", head)
        length = int(match.group(1)) if match else 0
        if len(buffer) < head_end + length:
            break
        raw = bytes(buffer[:head_end + length])
        del buffer[:head_end + length]
        try:
            out.append(parse_sip_message(raw))
        except SipProtocolError:
            # A bad frame must not wedge the receive stream.
            continue
    return out


def _header_params(value: str) -> Dict[str, str]:
    """Parse semicolon parameters from a header value."""
    params: Dict[str, str] = {}
    for part in str(value or "").split(";"):
        if "=" in part:
            key, val = part.split("=", 1)
            params[key.strip().lower()] = val.strip().strip('"')
        elif part.strip():
            params[part.strip().lower()] = ""
    return params


def _parse_uri(value: str) -> str:
    match = re.search(r"<([^>]+)>", str(value or ""))
    if match:
        return match.group(1).strip()
    text = str(value or "").strip()
    return text.split(";", 1)[0].strip()


def _display_name(value: str) -> str:
    text = str(value or "").strip()
    match = re.match(r'^\s*"?([^"<]*)"?\s*<', text)
    if match:
        return match.group(1).strip()
    return ""


# ─────────────────────────────────────────────────────────────────────────────
#  Digest authentication
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DigestChallenge:
    realm: str
    nonce: str
    opaque: str = ""
    algorithm: str = "MD5"
    qop: str = ""
    stale: bool = False


def parse_www_authenticate(value: str) -> DigestChallenge:
    text = str(value or "").strip()
    if text.lower().startswith("digest "):
        text = text[7:]
    params: Dict[str, str] = {}
    for match in re.finditer(
            r'([A-Za-z0-9_-]+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', text):
        params[match.group(1).lower()] = match.group(2) or match.group(3) or ""
    if not params.get("realm") or not params.get("nonce"):
        raise SipProtocolError("SIP authentication challenge is incomplete")
    return DigestChallenge(
        realm=params["realm"],
        nonce=params["nonce"],
        opaque=params.get("opaque", ""),
        algorithm=(params.get("algorithm") or "MD5").upper(),
        qop=params.get("qop", ""),
        stale=_bool(params.get("stale", "false")),
    )


def _digest_hash(algorithm: str, text: str) -> str:
    algorithm = (algorithm or "MD5").upper()
    if algorithm.startswith("SHA-256"):
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    else:
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    return digest


def digest_authorization(
    challenge: DigestChallenge,
    *,
    method: str,
    uri: str,
    username: str,
    password: str,
    nc: int = 1,
    cnonce: Optional[str] = None,
) -> str:
    """Build the value of one RFC 2617/7616 digest Authorization header.

    The return value starts with ``Digest `` and is meant to be used as the
    *value* of ``Authorization``/``Proxy-Authorization``; it deliberately does
    not include the header name, because the SIP message builder already adds
    it.  PJSIP/liblinphone use MD5 with qop=auth against Flexisip.  SHA-256 is
    supported too because RFC 7616 servers may prefer it; the challenge decides.
    """
    cnonce = cnonce or uuid.uuid4().hex[:16]
    algorithm = (challenge.algorithm or "MD5").upper()
    ha1 = _digest_hash(
        algorithm, f"{username}:{challenge.realm}:{password}")
    if algorithm.endswith("-SESS"):
        ha1 = _digest_hash(algorithm, f"{ha1}:{challenge.nonce}:{cnonce}")
    ha2 = _digest_hash(algorithm, f"{method.upper()}:{uri}")
    qop = ""
    if challenge.qop:
        options = [item.strip().lower() for item in challenge.qop.split(",")]
        qop = "auth" if "auth" in options else options[0]
    nc_value = f"{nc:08x}"
    if qop:
        response = _digest_hash(
            algorithm,
            f"{ha1}:{challenge.nonce}:{nc_value}:{cnonce}:{qop}:{ha2}")
    else:
        response = _digest_hash(
            algorithm, f"{ha1}:{challenge.nonce}:{ha2}")
    parts = [
        f'username="{username}"',
        f'realm="{challenge.realm}"',
        f'nonce="{challenge.nonce}"',
        f'uri="{uri}"',
        f'response="{response}"',
        f"algorithm={algorithm.replace('-SESS', '-sess')}",
    ]
    if challenge.opaque:
        parts.append(f'opaque="{challenge.opaque}"')
    if qop:
        parts.extend([f"qop={qop}", f"nc={nc_value}", f'cnonce="{cnonce}"'])
    return "Digest " + ", ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
#  SDP
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SdpMedia:
    address: str = ""
    port: int = 0
    payload: int = RTP_PAYLOAD_PCMU
    codec: str = "PCMU"
    telephone_event: Optional[int] = None
    profile: str = "RTP/AVP"
    srtp: bool = False
    crypto_tag: str = ""
    crypto_suite: str = ""
    crypto_key: bytes = b""
    rtcp_mux: bool = False


def _local_ip() -> str:
    """The interface address used for an outbound connection, without I/O."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def build_sdp(
    address: str,
    port: int,
    *,
    srtp: bool = False,
    crypto_key: bytes = b"",
) -> str:
    session_id = random.getrandbits(32)
    profile = "RTP/SAVPF" if srtp else "RTP/AVP"
    lines = [
        "v=0",
        f"o=jarvis {session_id} {session_id} IN IP4 {address}",
        "s=Jarvis",
        f"c=IN IP4 {address}",
        "t=0 0",
        f"m=audio {port} {profile} 0 8 101",
        "a=rtpmap:0 PCMU/8000",
        "a=rtpmap:8 PCMA/8000",
        "a=rtpmap:101 telephone-event/8000",
        "a=fmtp:101 0-16",
        "a=ptime:20",
        "a=maxptime:40",
    ]
    if srtp:
        if len(crypto_key) < SRTP_KEY_BYTES:
            raise TelephonyError("SRTP key is too short")
        encoded = base64.b64encode(
            crypto_key[:SRTP_KEY_BYTES]).decode("ascii")
        lines.extend([
            f"a=crypto:1 {SRTP_SUITE} inline:{encoded}",
            "a=rtcp-mux",
        ])
    lines.extend(["a=sendrecv", ""])
    return "\r\n".join(lines)


def parse_sdp(text: str, fallback_address: str = "") -> SdpMedia:
    body = str(text or "")
    address = fallback_address
    port = 0
    payloads: List[int] = []
    rtpmap: Dict[int, str] = {}
    telephone_event: Optional[int] = None
    profile = "RTP/AVP"
    crypto_options: List[Tuple[str, str, bytes]] = []
    rtcp_mux = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("c=IN IP4 ") or line.startswith("c=IN IP6 "):
            address = line.split()[-1]
        elif line.startswith("m=audio "):
            parts = line.split()
            if len(parts) >= 4:
                try:
                    port = int(parts[1])
                except ValueError:
                    port = 0
                profile = parts[2].upper()
                for item in parts[3:]:
                    try:
                        payloads.append(int(item))
                    except ValueError:
                        pass
        elif line.startswith("a=rtpmap:"):
            match = re.match(r"a=rtpmap:(\d+)\s+([^/]+)/", line, re.I)
            if match:
                rtpmap[int(match.group(1))] = match.group(2).upper()
        elif line.startswith("a=fmtp:"):
            match = re.match(r"a=fmtp:(\d+)\s+(.*)", line, re.I)
            if match and "telephone-event" in rtpmap.get(
                    int(match.group(1)), ""):
                telephone_event = int(match.group(1))
        elif line.lower() == "a=rtcp-mux":
            rtcp_mux = True
        elif line.startswith("a=crypto:"):
            rest = line[len("a=crypto:"):].strip()
            parts = rest.split(None, 2)
            if len(parts) >= 3:
                tag, suite, key_part = parts[0], parts[1].upper(), parts[2]
                key_text = key_part.split(":", 1)[-1].split("|", 1)[0]
                try:
                    key = base64.b64decode(key_text, validate=True)
                except Exception:
                    key = b""
                if key:
                    crypto_options.append((tag, suite, key))
    codec = "PCMU"
    chosen = RTP_PAYLOAD_PCMU
    for pt in payloads:
        name = rtpmap.get(pt, "")
        if pt == RTP_PAYLOAD_PCMU or name == "PCMU":
            chosen, codec = pt, "PCMU"
            break
        if pt == RTP_PAYLOAD_PCMA or name == "PCMA":
            chosen, codec = pt, "PCMA"
    if port <= 0 or not address:
        raise SipProtocolError("SIP audio SDP has no usable RTP endpoint")
    if not payloads:
        codec = "PCMU"
    srtp = "SAVP" in profile
    crypto_tag = ""
    crypto_suite = ""
    crypto_key = b""
    if srtp:
        supported = {
            "AES_CM_128_HMAC_SHA1_80",
            "AES_CM_128_HMAC_SHA1_32",
        }
        for tag, suite, key in crypto_options:
            if suite in supported and len(key) >= SRTP_KEY_BYTES:
                crypto_tag, crypto_suite, crypto_key = tag, suite, key
                break
        if not crypto_key:
            raise SipProtocolError(
                "SIP audio SDP requires SRTP but offers no supported "
                "AES_CM_128_HMAC_SHA1 suite")
    return SdpMedia(
        address=address, port=port, payload=chosen, codec=codec,
        telephone_event=telephone_event, profile=profile, srtp=srtp,
        crypto_tag=crypto_tag, crypto_suite=crypto_suite,
        crypto_key=crypto_key, rtcp_mux=rtcp_mux,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  STUN (best-effort public RTP address discovery)
# ─────────────────────────────────────────────────────────────────────────────

_STUN_MAGIC = 0x2112A442


def _stun_binding(sock: socket.socket, server: str,
                  timeout: float = 1.2) -> Optional[Tuple[str, int]]:
    try:
        host, _, port_text = server.partition(":")
        port = int(port_text) if port_text else 3478
        infos = socket.getaddrinfo(host, port, socket.AF_INET,
                                   socket.SOCK_DGRAM)
        if not infos:
            return None
        target = infos[0][4]
    except OSError:
        return None
    txid = os.urandom(12)
    packet = struct.pack("!HHI", 0x0001, 0, _STUN_MAGIC) + txid
    try:
        sock.sendto(packet, target)
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            readable, _, _ = select.select(
                [sock], [], [], max(0.0, deadline - time.monotonic()))
        except (OSError, ValueError):
            return None
        if not readable:
            return None
        try:
            data, _ = sock.recvfrom(2048)
        except OSError:
            return None
        if len(data) < 20:
            continue
        msg_type, msg_len, cookie = struct.unpack("!HHI", data[:8])
        if msg_type != 0x0101 or cookie != _STUN_MAGIC:
            continue
        offset = 20
        end = min(len(data), 20 + msg_len)
        while offset + 4 <= end:
            attr_type, attr_len = struct.unpack("!HH", data[offset:offset + 4])
            value = data[offset + 4:offset + 4 + attr_len]
            offset += 4 + attr_len + ((4 - attr_len % 4) % 4)
            if attr_type == 0x0020 and len(value) >= 8:  # XOR-MAPPED-ADDRESS
                family = value[1]
                port = struct.unpack("!H", value[2:4])[0] ^ (
                    _STUN_MAGIC >> 16)
                if family == 0x01:
                    ip_int = struct.unpack("!I", value[4:8])[0] ^ _STUN_MAGIC
                    ip = socket.inet_ntoa(struct.pack("!I", ip_int))
                    return ip, port
            if attr_type == 0x0001 and len(value) >= 8:  # MAPPED-ADDRESS
                family = value[1]
                port = struct.unpack("!H", value[2:4])[0]
                if family == 0x01:
                    ip = socket.inet_ntoa(value[4:8])
                    return ip, port
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  RTP session
# ─────────────────────────────────────────────────────────────────────────────

class RtpSession:
    """One G.711 RTP stream with a small jitter buffer and paced sender."""

    def __init__(
        self,
        *,
        on_pcm16k: Optional[Callable[[bytes], None]] = None,
        stun_server: str = "",
        trace: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_pcm16k = on_pcm16k
        self._stun_server = stun_server
        self._trace = trace
        self._tx_count = 0
        self._tx_payload_bytes = 0
        self._tx_wire_bytes = 0
        self._dropped_chunks = 0
        self._rx_source_logged = False
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("0.0.0.0", 0))
        self._sock.setblocking(False)
        self._local_port = self._sock.getsockname()[1]
        self._public: Optional[Tuple[str, int]] = None
        if stun_server:
            self._public = _stun_binding(self._sock, stun_server)
        self._remote: Optional[Tuple[str, int]] = None
        self._payload = RTP_PAYLOAD_PCMU
        self._codec = "PCMU"
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ssrc = _random_ssrc()
        self._seq = random.randrange(0, 65536)
        self._timestamp = random.getrandbits(32)
        self._muted = False
        self._first_packet = True
        self._rx: Dict[int, bytes] = {}
        self._rx_next: Optional[int] = None
        self._rx_packets = 0
        self._rx_last_at = 0.0
        self._local_srtp_key = os.urandom(SRTP_KEY_BYTES)
        self._srtp_out = None
        self._srtp_in = None
        self._tx_chunks: Deque[bytes] = deque()
        self._tx_buffer = bytearray()
        self._tx_bytes = 0
        self._tx_lock = threading.Lock()
        self._rate_state_out: Any = None
        self._rate_state_in: Any = None
        self._lock = threading.Lock()

    # ── address / lifecycle ────────────────────────────────────────────────

    @property
    def local_port(self) -> int:
        return self._local_port

    @property
    def public_address(self) -> Optional[Tuple[str, int]]:
        return self._public

    @property
    def local_srtp_key(self) -> bytes:
        return self._local_srtp_key

    def advertise_address(self) -> Tuple[str, int]:
        if self._public:
            return self._public
        return _local_ip(), self._local_port

    def start(
        self,
        remote: Tuple[str, int],
        payload: int,
        codec: str = "PCMU",
        *,
        srtp: bool = False,
        remote_key: bytes = b"",
        srtp_suite: str = SRTP_SUITE,
    ) -> None:
        with self._lock:
            self._remote = remote
            self._payload = payload
            self._codec = "PCMA" if str(codec).upper() == "PCMA" else "PCMU"
            self._srtp_out = None
            self._srtp_in = None
            if self._trace:
                self._trace(
                    f"RTP start target={remote[0]}:{remote[1]} "
                    f"codec={self._codec} srtp={srtp}")
            if srtp:
                if pylibsrtp is None:
                    raise TelephonyError(
                        "SRTP is required but pylibsrtp is not installed.")
                if len(remote_key) < SRTP_KEY_BYTES:
                    raise TelephonyError(
                        "SRTP is required but the remote crypto key is missing.")
                profile = (
                    pylibsrtp.Policy.SRTP_PROFILE_AES128_CM_SHA1_32
                    if str(srtp_suite).upper().endswith("SHA1_32")
                    else pylibsrtp.Policy.SRTP_PROFILE_AES128_CM_SHA1_80
                )
                out_policy = pylibsrtp.Policy(
                    key=self._local_srtp_key,
                    ssrc_type=pylibsrtp.Policy.SSRC_ANY_OUTBOUND,
                    srtp_profile=profile,
                )
                in_policy = pylibsrtp.Policy(
                    key=remote_key[:SRTP_KEY_BYTES],
                    ssrc_type=pylibsrtp.Policy.SSRC_ANY_INBOUND,
                    srtp_profile=profile,
                )
                self._srtp_out = pylibsrtp.Session(out_policy)
                self._srtp_in = pylibsrtp.Session(in_policy)
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._media_loop, name="jarvis-rtp", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._thread = None

    # ── outbound audio ─────────────────────────────────────────────────────

    def send_pcm24k(self, pcm: bytes) -> None:
        if not pcm:
            return
        with self._tx_lock:
            self._tx_chunks.append(bytes(pcm))
            self._tx_bytes += len(pcm)
            cap = int(GEMINI_OUT_RATE * 2 * MAX_TX_SECONDS)
            while self._tx_bytes > cap and self._tx_chunks:
                removed = self._tx_chunks.popleft()
                self._tx_bytes -= len(removed)
                self._dropped_chunks += 1
                if self._dropped_chunks == 1 and self._trace:
                    self._trace(
                        "RTP TX queue overflow - dropping oldest audio chunk")

    def flush(self) -> None:
        with self._tx_lock:
            self._tx_chunks.clear()
            self._tx_buffer.clear()
            self._tx_bytes = 0

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        if muted:
            self.flush()

    def buffered_seconds(self) -> float:
        with self._tx_lock:
            return self._tx_bytes / float(GEMINI_OUT_RATE * 2)

    def audio_stats(self) -> Dict[str, Any]:
        with self._tx_lock:
            buffered = self._tx_bytes / float(GEMINI_OUT_RATE * 2)
        return {
            "tx_packets": self._tx_count,
            "tx_payload_bytes": self._tx_payload_bytes,
            "tx_wire_bytes": self._tx_wire_bytes,
            "rx_packets": self._rx_packets,
            "dropped_chunks": self._dropped_chunks,
            "buffered_seconds": round(buffered, 3),
            "srtp": self._srtp_out is not None,
        }

    # ── media loop ─────────────────────────────────────────────────────────

    def _media_loop(self) -> None:
        next_tick = time.monotonic() + RTP_PTIME
        while not self._stop.is_set():
            now = time.monotonic()
            timeout = max(0.0, min(0.02, next_tick - now))
            try:
                readable, _, _ = select.select([self._sock], [], [], timeout)
            except (OSError, ValueError):
                break
            if readable and not self._stop.is_set():
                self._drain_socket()
            now = time.monotonic()
            if now < next_tick:
                continue
            next_tick += RTP_PTIME
            if next_tick < now - 0.10:
                next_tick = now + RTP_PTIME
            self._send_tick()
            self._receive_tick()

    def _drain_socket(self) -> None:
        while not self._stop.is_set():
            try:
                packet, source = self._sock.recvfrom(4096)
            except BlockingIOError:
                return
            except OSError:
                return
            if self._srtp_in is not None:
                try:
                    packet = self._srtp_in.unprotect(packet)
                except Exception:
                    # Bad authentication tag, or SRTCP on an rtcp-mux port.
                    continue
            if len(packet) < 12:
                continue
            version = packet[0] >> 6
            if version != 2:
                continue
            padding = bool(packet[0] & 0x20)
            extension = bool(packet[0] & 0x10)
            csrc_count = packet[0] & 0x0F
            header_len = 12 + csrc_count * 4
            if len(packet) < header_len:
                continue
            payload_type = packet[1] & 0x7F
            seq, timestamp, _ssrc = struct.unpack("!HII", packet[2:12])
            offset = header_len
            if extension and len(packet) >= offset + 4:
                ext_len = struct.unpack("!H", packet[offset + 2:offset + 4])[0]
                offset += 4 + ext_len * 4
            if offset > len(packet):
                continue
            payload = packet[offset:]
            if padding and payload:
                payload = payload[:-max(1, payload[-1])]
            if not payload:
                continue
            # Symmetric RTP: use the source address the peer actually sends
            # from, which is the address that can reach us through NAT.
            if payload_type == RTP_PAYLOAD_DTMF:
                continue
            with self._lock:
                codec = self._codec
                negotiated = self._payload
                old_remote = self._remote
            if not self._rx_source_logged and self._trace:
                self._rx_source_logged = True
                self._trace(
                    f"RTP first rx source={source[0]}:{source[1]} "
                    f"sdp_target={old_remote[0]}:{old_remote[1]}"
                    if old_remote else
                    f"RTP first rx source={source[0]}:{source[1]}")
            if payload_type not in (RTP_PAYLOAD_PCMU, RTP_PAYLOAD_PCMA,
                                    negotiated):
                continue
            if source and self._remote != source:
                with self._lock:
                    self._remote = source
            if payload_type == RTP_PAYLOAD_PCMU:
                pcm = audioop.ulaw2lin(payload, 2)
            elif payload_type == RTP_PAYLOAD_PCMA:
                pcm = audioop.alaw2lin(payload, 2)
            elif codec == "PCMA":
                pcm = audioop.alaw2lin(payload, 2)
            else:
                pcm = audioop.ulaw2lin(payload, 2)
            self._rx[seq] = pcm
            self._rx_packets += 1
            self._rx_last_at = time.monotonic()
            if len(self._rx) > MAX_RX_PACKETS:
                for old in sorted(self._rx)[:len(self._rx) - MAX_RX_PACKETS]:
                    self._rx.pop(old, None)

    def _receive_tick(self) -> None:
        if not self._rx:
            if self._rx_next is not None:
                self._emit_pcm(b"\x00\x00" * 160)
            return
        if self._rx_next is None:
            if len(self._rx) < 2:
                return
            self._rx_next = min(self._rx)
        expected = self._rx_next & 0xFFFF
        pcm = self._rx.pop(expected, None)
        next_seq = expected
        if pcm is None:
            # Tolerate a couple of lost packets; skip forward if the peer
            # already sent the next one rather than replaying stale audio.
            candidates = [seq for seq in self._rx if seq > expected]
            if candidates and min(candidates) <= expected + 3:
                next_seq = min(candidates)
                pcm = self._rx.pop(next_seq, None)
            if pcm is None:
                pcm = b"\x00\x00" * 160
        self._rx_next = (next_seq + 1) & 0xFFFF
        self._emit_pcm(pcm)

    def _emit_pcm(self, pcm: bytes) -> None:
        callback = self._on_pcm16k
        if callback is None:
            return
        try:
            resampled, self._rate_state_in = audioop.ratecv(
                pcm, 2, 1, SIP_RATE, GEMINI_IN_RATE, self._rate_state_in)
            if resampled:
                callback(resampled)
        except Exception:
            # A callback failure must not kill the RTP thread; the call stays
            # up and the UI can report the bridge failure separately.
            pass

    def _send_tick(self) -> None:
        remote = self._remote
        if remote is None or self._stop.is_set():
            return
        frame = self._next_tx_frame()
        if self._muted:
            frame = b"\x00" * PCM_FRAME_BYTES
        if self._trace and (self._tx_count < 3
                            or self._tx_count % 50 == 0):
            try:
                rms = audioop.rms(frame, 2)
            except Exception:
                rms = -1
            with self._tx_lock:
                queued = self._tx_bytes
            self._trace(
                f"RTP tx frame count={self._tx_count} rms={rms} "
                f"queued={queued}B target={remote[0]}:{remote[1]}")
        try:
            if self._codec == "PCMA":
                payload = audioop.lin2alaw(frame, 2)
            else:
                payload = audioop.lin2ulaw(frame, 2)
        except Exception:
            payload = b"\xff" * PCM_FRAME_BYTES
        header = struct.pack(
            "!BBHII",
            0x80,
            (0x80 if self._first_packet else 0) | (self._payload & 0x7F),
            self._seq & 0xFFFF,
            self._timestamp & 0xFFFFFFFF,
            self._ssrc & 0xFFFFFFFF,
        )
        packet = header + payload
        if self._srtp_out is not None:
            try:
                packet = self._srtp_out.protect(packet)
            except Exception:
                return
        try:
            self._sock.sendto(packet, remote)
        except OSError:
            return
        self._tx_count += 1
        self._tx_payload_bytes += len(payload)
        self._tx_wire_bytes += len(packet)
        if self._tx_count == 1 and self._trace:
            self._trace(
                f"RTP first tx target={remote[0]}:{remote[1]} "
                f"pt={self._payload} seq={self._seq} "
                f"payload={len(payload)}B srtp={self._srtp_out is not None}")
        self._first_packet = False
        self._seq = (self._seq + 1) & 0xFFFF
        self._timestamp = (self._timestamp + 160) & 0xFFFFFFFF

    def _next_tx_frame(self) -> bytes:
        with self._tx_lock:
            # Fill the resampler input to exactly one 20 ms frame.  The
            # condition must test the *buffer*, not the total queued bytes:
            # with more than one frame queued, testing `_tx_bytes` skipped
            # popping entirely and sent silence while the queue grew.
            while (len(self._tx_buffer) < GEMINI_OUT_FRAME_BYTES
                   and self._tx_chunks):
                chunk = self._tx_chunks.popleft()
                self._tx_bytes -= len(chunk)
                self._tx_buffer.extend(chunk)
            if len(self._tx_buffer) >= GEMINI_OUT_FRAME_BYTES:
                chunk = bytes(self._tx_buffer[:GEMINI_OUT_FRAME_BYTES])
                del self._tx_buffer[:GEMINI_OUT_FRAME_BYTES]
            else:
                chunk = bytes(self._tx_buffer) + b"\x00" * (
                    GEMINI_OUT_FRAME_BYTES - len(self._tx_buffer))
                self._tx_buffer.clear()
        try:
            pcm8, self._rate_state_out = audioop.ratecv(
                chunk, 2, 1, GEMINI_OUT_RATE, SIP_RATE,
                self._rate_state_out)
        except Exception:
            pcm8 = b"\x00" * PCM_FRAME_BYTES
        if len(pcm8) < PCM_FRAME_BYTES:
            pcm8 += b"\x00" * (PCM_FRAME_BYTES - len(pcm8))
        return pcm8[:PCM_FRAME_BYTES]


# ─────────────────────────────────────────────────────────────────────────────
#  Calls
# ─────────────────────────────────────────────────────────────────────────────

_STATE_PHASE = {
    "incoming": "ringing",
    "dialing": "starting",
    "ringing": "ringing",
    "connecting": "connecting",
    "active": "active",
    "ending": "ending",
    "ended": "ended",
    "failed": "failed",
}


def _overall_call_state(calls: List[Dict[str, Any]]) -> str:
    """One accurate aggregate state for the current workstation call list.

    Terminal records are kept for history, but they are not a current call:
    with nothing live the service is idle.  Per-call snapshots still expose the
    terminal state and reason.
    """
    states = {str(call.get("state") or "") for call in calls}
    for state in ("active", "ending", "connecting", "ringing",
                  "dialing", "incoming"):
        if state in states:
            return _STATE_PHASE.get(state, state)
    return "idle"


@dataclass
class Call:
    id: str
    direction: str
    remote_uri: str
    remote_display: str
    local_uri: str
    call_id: str
    state: str = "dialing"
    reason: str = ""
    created_at: float = field(default_factory=_now)
    answered_at: Optional[float] = None
    ended_at: Optional[float] = None
    muted: bool = False
    bridged: bool = False
    srtp: bool = False
    media: Optional[SdpMedia] = None
    sip: Optional[SipMessage] = None
    from_tag: str = ""
    to_tag: str = ""
    cseq: int = 1
    invite_branch: str = ""
    route_set: List[str] = field(default_factory=list)
    remote_target: str = ""
    rtp: Optional[RtpSession] = None
    auth_sent: int = 0
    challenge: Optional[DigestChallenge] = None
    last_response: Optional[SipMessage] = None
    history_written: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)
    answered_event: threading.Event = field(default_factory=threading.Event)
    ended_event: threading.Event = field(default_factory=threading.Event)
    local_tag: str = field(default_factory=_new_tag)

    def duration(self) -> float:
        if self.answered_at is None:
            return 0.0
        end = self.ended_at if self.ended_at is not None else _now()
        return max(0.0, end - self.answered_at)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            address = ""
            port = 0
            if self.rtp is not None:
                address, port = self.rtp.advertise_address()
            return {
                "call_id": self.id,
                "direction": self.direction,
                "state": self.state,
                "phase": _STATE_PHASE.get(self.state, self.state),
                "terminal": self.state in ("ended", "failed"),
                "remote": _redact_uri(self.remote_uri),
                "remote_display": self.remote_display,
                "local": _redact_uri(self.local_uri),
                "created_at": _iso(self.created_at),
                "answered_at": _iso(self.answered_at) if self.answered_at else "",
                "ended_at": _iso(self.ended_at) if self.ended_at else "",
                "duration_seconds": round(self.duration(), 1),
                "muted": self.muted,
                "bridged": self.bridged,
                "srtp": self.srtp,
                "reason": self.reason,
                "rtp": {"address": address, "port": port} if address else {},
            }


# ─────────────────────────────────────────────────────────────────────────────
#  SIP engine
# ─────────────────────────────────────────────────────────────────────────────

class SipEngine:
    """TLS SIP user agent with one registration and multiple call records."""

    def __init__(
        self,
        settings: Dict[str, Any],
        *,
        on_call: Optional[Callable[[str, Call], None]] = None,
        on_incoming: Optional[Callable[[Call], None]] = None,
        on_registration: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_audio: Optional[Callable[[str, bytes], None]] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.settings = dict(settings or {})
        self.on_call = on_call
        self.on_incoming = on_incoming
        self.on_registration = on_registration
        self.on_audio = on_audio
        self.log = log or (lambda _msg: None)

        self.username = str(self.settings.get("username") or "")
        self.password = str(self.settings.get("password") or "")
        self.server = str(self.settings.get("server") or "sip.linphone.org")
        self.port = int(self.settings.get("port") or 5061)
        self.identity = str(
            self.settings.get("identity")
            or f"sip:{self.username}@{self.server}")
        self.display_name = str(self.settings.get("display_name") or "Jarvis")
        self.transport = str(self.settings.get("transport") or "TLS").upper()
        self.register_expires = _parse_number(
            self.settings.get("register_expires"), 300, 60, 3600)
        self.ring_timeout = float(self.settings.get("ring_timeout") or RING_TIMEOUT)
        self.stun_server = str(self.settings.get("stun_server") or "")
        self.tls_verify = _bool(self.settings.get("tls_verify"), True)
        self.auto_answer = _bool(self.settings.get("auto_answer"), False)
        self.srtp = _bool(self.settings.get("srtp"), True)
        self.contacts = dict(self.settings.get("contacts") or {})
        # Optional signalling trace. It prints only method/status and redacted
        # SIP URIs; headers (including Authorization) and bodies are never
        # printed, so a trace cannot leak a password or digest response.
        self.trace = _bool(os.environ.get("JARVIS_SIP_TRACE"), False)

        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._calls: Dict[str, Call] = {}
        self._by_sip_id: Dict[str, Call] = {}
        self._sock: Optional[ssl.SSLSocket] = None
        self._buffer = bytearray()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._connected = False
        self._registered = False
        self._registration_state = "idle"
        self._registration_error = ""
        self._registered_at = 0.0
        self._reg_event = threading.Event()
        self._reg_kind = ""
        self._reg_nonce: Optional[DigestChallenge] = None
        self._reg_cseq = 1
        self._reg_auth_sent = 0
        self._reg_sent_at = 0.0
        # A challenged request is a new transaction, not a new registration:
        # keep the Call-ID and From tag stable across the retry (and refreshes).
        self._reg_call_id = ""
        self._reg_from_tag = ""
        self._last_sip_rx = 0.0
        self._last_ping = 0.0
        self._reconnect_backoff = 0
        self._public_ip = ""
        self._public_port = 0
        self._instance = str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"jarvis-sip:{self.username}@{self.server}"))
        self._history_lock = threading.Lock()
        self._auto_answer_timers: Dict[str, threading.Timer] = {}

    # ── public lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        problems = config.validate_telephony_settings(
            self.settings, require_password=True)
        if problems:
            raise TelephonyError(
                "SIP configuration is incomplete: "
                + "; ".join(problems)
                + ". Set the values in .env and restart Jarvis.")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="jarvis-sip", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close_socket()
        with self._lock:
            calls = list(self._calls.values())
        for call in calls:
            self._end_call(call, "shutdown", notify=False)
        for timer in list(self._auto_answer_timers.values()):
            timer.cancel()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    def registered(self) -> bool:
        return bool(self._registered)

    def registration(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self._registration_state,
                "registered": self._registered,
                "server": self.server,
                "transport": self.transport,
                "identity": _redact_uri(self.identity),
                "port": self.port,
                "registered_at": _iso(self._registered_at)
                if self._registered_at else "",
                "last_error": self._registration_error,
                "public_ip": self._public_ip,
            }

    def wait_until_registered(self, timeout: float = REGISTER_TIMEOUT) -> None:
        problems = config.validate_telephony_settings(
            self.settings, require_password=True)
        if problems:
            raise TelephonyError(
                "SIP configuration is incomplete: "
                + "; ".join(problems)
                + ". Set the values in .env and restart Jarvis.")
        self.start()
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            if self._registered:
                return
            if self._registration_state == "failed":
                raise TelephonyError(
                    self._registration_error or "SIP registration failed.")
            self._reg_event.wait(0.2)
        if self._registered:
            return
        raise TelephonyError(
            self._registration_error
            or "Timed out waiting for SIP registration from "
               f"{self.server}:{self.port}.")

    def ensure_started(self) -> None:
        self.start()

    # ── registration / transport thread ─────────────────────────────────────

    def _run(self) -> None:
        backoff = 0
        while not self._stop.is_set():
            try:
                self._registration_state = "connecting"
                self._registration_error = ""
                self._emit_registration()
                sock = self._connect()
                self._sock = sock
                self._buffer.clear()
                self._connected = True
                self._registered = False
                self._reg_event.clear()
                self._reg_nonce = None
                self._reg_cseq = 1
                self._reg_auth_sent = 0
                self._reg_call_id = f"{uuid.uuid4().hex}@jarvis"
                self._reg_from_tag = _new_tag()
                self._public_ip = ""
                self._public_port = 0
                self._last_sip_rx = time.monotonic()
                self._last_ping = time.monotonic()
                self.log(f"SIP TLS connected to {self.server}:{self.port}")
                self._send_register()
                self._read_loop(sock)
            except Exception as exc:  # noqa: BLE001 - network loop must retry
                if not self._stop.is_set():
                    self._registration_state = "reconnecting"
                    self._registration_error = (
                        f"{type(exc).__name__}: {exc}"[:240])
                    self._registered = False
                    self._reg_event.set()
                    self._emit_registration()
                    self.log(f"SIP connection lost: {self._registration_error}")
            finally:
                self._connected = False
                self._registered = False
                self._close_socket()
                self._end_all_calls("network disconnected", notify=True,
                                    terminal_state="failed")
            if self._stop.is_set():
                break
            delay = RECONNECT_BACKOFF[min(backoff, len(RECONNECT_BACKOFF) - 1)]
            backoff += 1
            self._stop.wait(delay)

    def _connect(self) -> ssl.SSLSocket:
        context = ssl.create_default_context()
        if not self.tls_verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((self.server, self.port), timeout=12)
        raw.settimeout(1.0)
        return context.wrap_socket(raw, server_hostname=self.server)

    def _close_socket(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _send(self, message: SipMessage) -> None:
        sock = self._sock
        if sock is None or not self._connected:
            raise TelephonyError("SIP transport is not connected.")
        if self.trace:
            if message.is_response:
                self.log(f"SIP TX {message.start}")
            else:
                self.log(
                    f"SIP TX {message.method} {_redact_uri(message.uri)}")
        payload = message.build()
        with self._send_lock:
            try:
                sock.sendall(payload)
            except (OSError, ssl.SSLError) as exc:
                raise TelephonyError(f"SIP send failed: {exc}") from exc

    def _send_bytes(self, payload: bytes) -> None:
        sock = self._sock
        if sock is None or not self._connected:
            return
        with self._send_lock:
            try:
                sock.sendall(payload)
            except (OSError, ssl.SSLError):
                pass

    def _read_loop(self, sock: ssl.SSLSocket) -> None:
        while not self._stop.is_set() and sock is self._sock:
            try:
                chunk = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                chunk = None
            except ssl.SSLError as exc:
                if "timed out" in str(exc).lower():
                    chunk = None
                else:
                    raise
            if chunk is None:
                self._tick()
                continue
            if not chunk:
                # A blocking TLS read returning EOF is a closed connection,
                # not an idle timeout (idle reads raise socket.timeout).
                raise TelephonyError("SIP TLS connection closed by the server")
            self._buffer.extend(chunk)
            if len(self._buffer) > MAX_SIP_BUFFER:
                self._buffer.clear()
                raise TelephonyError("SIP receive buffer overflow")
            self._last_sip_rx = time.monotonic()
            for message in extract_sip_messages(self._buffer):
                self._dispatch(message)
            self._tick()
            if self._stop.is_set():
                return

    def _tick(self) -> None:
        now = time.monotonic()
        # Call timestamps come from _now() (wall clock); registration and
        # keepalive timestamps use the monotonic clock.  Mixing them made
        # ring/ACK timeouts silently unreachable.
        wall = time.time()
        if (self._registration_state == "connecting" and self._reg_sent_at
                and now - self._reg_sent_at > 10.0):
            raise TelephonyError("SIP registration timed out")
        if self._registered and now - self._reg_sent_at >= max(
                60, self.register_expires - 45):
            self._send_register()
        if self._connected and now - self._last_ping >= KEEPALIVE_SECONDS:
            # CRLF keepalive keeps the NAT/TLS binding alive between refreshes.
            self._send_bytes(b"\r\n\r\n")
            self._last_ping = now
        for call in list(self._calls.values()):
            with call.lock:
                state = call.state
                created = call.created_at
            if state in ("dialing", "ringing") and wall - created > self.ring_timeout:
                self.log(f"Call {call.id} not answered — cancelling.")
                try:
                    self._cancel(call)
                except TelephonyError:
                    pass
                self._end_call(call, "no answer", terminal_state="failed")
            if state == "connecting" and call.answered_at and wall - call.answered_at > ANSWER_ACK_TIMEOUT:
                self._end_call(call, "no ACK from caller", terminal_state="failed")

    # ── registration messages ───────────────────────────────────────────────

    def _contact(self) -> str:
        address = self._public_ip or _local_ip()
        port = self._public_port or self.port
        return (f"<sip:{self.username}@{address}:{port};transport=tls;ob>"
                f';reg-id=1;+sip.instance="<urn:uuid:{self._instance}>"')

    def _send_register(
        self,
        challenge: Optional[DigestChallenge] = None,
        auth_header: str = "Authorization",
    ) -> None:
        if not self._connected:
            return
        self._reg_kind = "REGISTER"
        self._reg_sent_at = time.monotonic()
        self._reg_cseq += 1
        branch = _new_branch()
        if not self._reg_call_id:
            self._reg_call_id = f"{uuid.uuid4().hex}@jarvis"
        if not self._reg_from_tag:
            self._reg_from_tag = _new_tag()
        headers = [
            ("Via", f"SIP/2.0/TLS {_local_ip()}:{self.port};branch={branch};rport"),
            ("Max-Forwards", "70"),
            ("From", f'"{self.display_name}" <{self.identity}>;tag={self._reg_from_tag}'),
            ("To", f"<{self.identity}>"),
            ("Call-ID", self._reg_call_id),
            ("CSeq", f"{self._reg_cseq} REGISTER"),
            ("Contact", self._contact()),
            ("Expires", str(self.register_expires)),
            ("Allow", "INVITE, ACK, CANCEL, BYE, OPTIONS, INFO, UPDATE, MESSAGE"),
            ("Supported", "outbound, path"),
            ("User-Agent", USER_AGENT),
            ("Content-Length", "0"),
        ]
        if challenge is not None:
            self._reg_auth_sent += 1
            headers.append((auth_header, digest_authorization(
                challenge,
                method="REGISTER",
                uri=f"sip:{self.server}",
                username=self.username,
                password=self.password,
                nc=self._reg_auth_sent,
            )))
        self._send(SipMessage(start=f"REGISTER sip:{self.server} SIP/2.0",
                              headers=headers))

    def _handle_register_response(self, message: SipMessage) -> None:
        code = message.status_code
        via = message.first("Via")
        params = _header_params(via.split(";", 1)[1]) if ";" in via else {}
        if params.get("received"):
            self._public_ip = params["received"]
        if params.get("rport"):
            try:
                self._public_port = int(params["rport"])
            except ValueError:
                pass
        if code in (401, 407):
            header = message.first(
                "WWW-Authenticate" if code == 401 else "Proxy-Authenticate")
            try:
                challenge = parse_www_authenticate(header)
            except SipProtocolError as exc:
                self._registration_state = "failed"
                self._registration_error = str(exc)
                self._reg_event.set()
                self._emit_registration()
                return
            self._reg_nonce = challenge
            if self._reg_auth_sent < 2 or challenge.stale:
                auth_header = ("Authorization" if code == 401
                               else "Proxy-Authorization")
                self._send_register(challenge, auth_header)
            else:
                self._registration_state = "failed"
                self._registration_error = (
                    "SIP server rejected the registration credentials. "
                    "Check SIP_PASSWORD in .env.")
                self._registered = False
                self._reg_event.set()
                self._emit_registration()
                self.log("SIP registration rejected by the server.")
            return
        if code == 200:
            self._registered = True
            self._registration_state = "registered"
            self._registration_error = ""
            self._registered_at = _now()
            self._reg_event.set()
            self._emit_registration()
            self.log("SIP registration active.")
            return
        if code in (408, 430, 439) or code >= 500:
            # Transient server/flow failures are worth a reconnect; permanent
            # 4xx (bad credentials, unknown account) are not.
            raise TelephonyError(
                f"SIP registration unavailable: {code} {message.reason}".strip())
        self._registered = False
        self._registration_state = "failed"
        self._registration_error = (
            f"SIP registration failed: {code} {message.reason}".strip())
        self._reg_event.set()
        self._emit_registration()

    # ── call creation ───────────────────────────────────────────────────────

    def make_call(self, target: str) -> Call:
        self.wait_until_registered()
        if self.srtp and pylibsrtp is None:
            raise TelephonyError(
                "SRTP is required but pylibsrtp is not installed. "
                "Install the telephony requirements and retry.")
        sip_id = f"{uuid.uuid4().hex}@jarvis"
        call = Call(
            id=uuid.uuid4().hex[:10],
            direction="outgoing",
            remote_uri=target,
            remote_display="",
            local_uri=self.identity,
            call_id=sip_id,
            state="dialing",
            from_tag=_new_tag(),
            cseq=1,
            invite_branch=_new_branch(),
            remote_target=target,
            srtp=self.srtp,
        )
        call.rtp = RtpSession(
            on_pcm16k=lambda pcm: self._forward_audio(call.id, pcm),
            stun_server=self.stun_server,
            trace=(self.log if self.trace else None),
        )
        with self._lock:
            self._calls[call.id] = call
            self._by_sip_id[sip_id] = call
        self._emit_call(call)
        self._send_invite(call)
        return call

    def _send_invite(
        self, call: Call, challenge: Optional[DigestChallenge] = None,
        auth_header: str = "Authorization",
    ) -> None:
        address, port = call.rtp.advertise_address() if call.rtp else (
            _local_ip(), 0)
        sdp = build_sdp(
            address, port,
            srtp=self.srtp,
            crypto_key=(call.rtp.local_srtp_key if call.rtp else b""),
        )
        call.cseq += 1
        branch = _new_branch()
        call.invite_branch = branch
        headers = [
            ("Via", f"SIP/2.0/TLS {_local_ip()}:{self.port};branch={branch};rport"),
            ("Max-Forwards", "70"),
            ("From", f'"{self.display_name}" <{self.identity}>;tag={call.from_tag}'),
            ("To", f"<{call.remote_uri}>"),
            ("Call-ID", call.call_id),
            ("CSeq", f"{call.cseq} INVITE"),
            ("Contact", self._contact()),
            ("Allow", "INVITE, ACK, CANCEL, BYE, OPTIONS, INFO, UPDATE"),
            ("Supported", "outbound, path"),
            ("User-Agent", USER_AGENT),
            ("Content-Type", "application/sdp"),
            ("Content-Length", str(len(sdp.encode("utf-8")))),
        ]
        if challenge is not None:
            headers.append((auth_header, digest_authorization(
                challenge,
                method="INVITE",
                uri=call.remote_target or call.remote_uri,
                username=self.username,
                password=self.password,
                nc=call.auth_sent or 1,
            )))
        if self.trace:
            self.log("SDP offer:\n" + sdp)
        self._send(SipMessage(start=f"INVITE {call.remote_target or call.remote_uri} SIP/2.0",
                              headers=headers, body=sdp.encode("utf-8")))

    def _incoming_invite(self, message: SipMessage) -> None:
        sip_id = message.first("Call-ID")
        if not sip_id:
            self._reply(message, 400, "Bad Request")
            return
        with self._lock:
            if sip_id in self._by_sip_id:
                self._reply(message, 482, "Loop Detected")
                return
            active = [c for c in self._calls.values()
                      if c.state in ("incoming", "dialing", "ringing",
                                     "connecting", "active", "ending")]
            if len(active) >= 2:
                self._reply(message, 486, "Busy Here")
                return
        from_header = message.first("From")
        remote_uri = _parse_uri(from_header)
        remote_display = _display_name(from_header)
        call = Call(
            id=uuid.uuid4().hex[:10],
            direction="incoming",
            remote_uri=remote_uri,
            remote_display=remote_display,
            local_uri=self.identity,
            call_id=sip_id,
            state="incoming",
            sip=message,
            from_tag=_header_params(
                from_header.split(";", 1)[1]).get("tag", "") if ";" in from_header
            else "",
            cseq=0,
            to_tag=_new_tag(),
            remote_target=_parse_uri(message.first("Contact")) or remote_uri,
            route_set=message.all("Record-Route"),
        )
        with self._lock:
            self._calls[call.id] = call
            self._by_sip_id[sip_id] = call
        # Ring first; STUN address discovery (up to ~1.2 s) must never delay
        # the caller hearing the phone ring. The RTP socket only needs to exist
        # before the call is answered.
        self._reply(message, 180, "Ringing", to_tag=call.to_tag)
        call.rtp = RtpSession(
            on_pcm16k=lambda pcm: self._forward_audio(call.id, pcm),
            stun_server=self.stun_server,
            trace=(self.log if self.trace else None),
        )
        self._emit_call(call)
        self.log(
            f"Incoming SIP call from {remote_display or _redact_uri(remote_uri)}")
        try:
            callback = self.on_incoming
            if callback:
                callback(call)
        except Exception:
            pass

    def answer(self, call_id: str) -> Call:
        call = self._get_call(call_id)
        with call.lock:
            if call.direction != "incoming" or call.state != "incoming":
                raise TelephonyError(
                    f"Call {call_id} is not an incoming ringing call.")
            call.state = "connecting"
            call.answered_at = _now()
            message = call.sip
            address, port = call.rtp.advertise_address() if call.rtp else (
                _local_ip(), 0)
            media: Optional[SdpMedia] = None
            if message is not None and message.body:
                try:
                    media = parse_sdp(
                        message.body_text(),
                        _parse_uri(message.first("Contact")))
                except SipProtocolError as exc:
                    raise TelephonyError(
                        f"Incoming media cannot be accepted: {exc}") from exc
            use_srtp = bool(media and media.srtp)
            if use_srtp and pylibsrtp is None:
                raise TelephonyError(
                    "The caller requires SRTP but pylibsrtp is not installed.")
            call.media = media
            call.srtp = use_srtp
            sdp = build_sdp(
                address, port,
                srtp=use_srtp,
                crypto_key=(call.rtp.local_srtp_key if call.rtp else b""),
            )
            if message is None:
                raise TelephonyError("Incoming call has no INVITE to answer.")
            call.cseq = 1
            self._reply(
                message, 200, "OK", to_tag=call.to_tag,
                extra_headers=[
                    ("Contact", self._contact()),
                    ("Content-Type", "application/sdp"),
                ],
                body=sdp.encode("utf-8"),
            )
        self._emit_call(call)
        return call

    def reject(self, call_id: str, reason: str = "declined") -> Call:
        call = self._get_call(call_id)
        with call.lock:
            if call.direction != "incoming" or call.state != "incoming":
                raise TelephonyError(
                    f"Call {call_id} is not an incoming ringing call.")
            message = call.sip
            if message is not None:
                self._reply(message, 486, "Busy Here", to_tag=call.to_tag)
        self._end_call(call, reason or "declined")
        return call

    def hangup(self, call_id: str, reason: str = "hung up") -> Call:
        call = self._get_call(call_id)
        state = call.state
        if state == "incoming":
            return self.reject(call_id, reason)
        if state in ("dialing", "ringing"):
            # A call that has not been answered is cancelled with CANCEL, not
            # BYE.  The local state must still terminate if signalling fails.
            with call.lock:
                call.state = "ending"
            self._emit_call(call)
            try:
                self._cancel(call)
            except TelephonyError:
                pass
            self._end_call(call, reason)
            return call
        if state in ("connecting", "active", "ending"):
            with call.lock:
                call.state = "ending"
            self._emit_call(call)
            self._bye(call)
            self._end_call(call, reason)
            return call
        raise TelephonyError(f"Call {call_id} is already {state}.")

    def mute(self, call_id: str, muted: bool = True) -> Call:
        call = self._get_call(call_id)
        with call.lock:
            call.muted = bool(muted)
            if call.rtp is not None:
                call.rtp.set_muted(call.muted)
        self._emit_call(call)
        return call

    def active_call(self) -> Optional[Call]:
        with self._lock:
            for call in self._calls.values():
                if call.state == "active":
                    return call
        return None

    def get_call(self, call_id: str = "") -> Optional[Call]:
        if call_id:
            call = self._calls.get(call_id)
            return call
        with self._lock:
            active = [c for c in self._calls.values()
                      if c.state in ("incoming", "dialing", "ringing",
                                     "connecting", "active", "ending")]
            return active[-1] if active else None

    def calls(self) -> List[Dict[str, Any]]:
        with self._lock:
            calls = list(self._calls.values())
        return [call.snapshot() for call in calls]

    # ── in-dialog signalling ────────────────────────────────────────────────

    def _reply(
        self,
        request: SipMessage,
        code: int,
        reason: str,
        *,
        to_tag: str = "",
        extra_headers: Optional[List[Tuple[str, str]]] = None,
        body: bytes = b"",
    ) -> None:
        headers: List[Tuple[str, str]] = []
        for via in request.all("Via"):
            headers.append(("Via", via))
        for record_route in request.all("Record-Route"):
            headers.append(("Record-Route", record_route))
        headers.append(("From", request.first("From")))
        to = request.first("To")
        if to_tag and "tag=" not in to:
            to = f"{to};tag={to_tag}"
        headers.append(("To", to))
        headers.append(("Call-ID", request.first("Call-ID")))
        headers.append(("CSeq", request.first("CSeq")))
        headers.append(("User-Agent", USER_AGENT))
        for key, value in extra_headers or []:
            headers.append((key, value))
        if body and not any(key.lower() == "content-length"
                            for key, _ in headers):
            headers.append(("Content-Length", str(len(body))))
        elif not body and not any(key.lower() == "content-length"
                                  for key, _ in headers):
            headers.append(("Content-Length", "0"))
        self._send(SipMessage(
            start=f"SIP/2.0 {code} {reason}", headers=headers, body=body))

    def _cancel(self, call: Call) -> None:
        headers = [
            ("Via", f"SIP/2.0/TLS {_local_ip()}:{self.port};branch={call.invite_branch};rport"),
            ("Max-Forwards", "70"),
            ("From", f'"{self.display_name}" <{self.identity}>;tag={call.from_tag}'),
            ("To", f"<{call.remote_uri}>"),
            ("Call-ID", call.call_id),
            ("CSeq", f"{call.cseq} CANCEL"),
            ("User-Agent", USER_AGENT),
            ("Content-Length", "0"),
        ]
        self._send(SipMessage(
            start=f"CANCEL {call.remote_target or call.remote_uri} SIP/2.0",
            headers=headers))

    def _bye(self, call: Call) -> None:
        call.cseq += 1
        to = f"<{call.remote_uri}>"
        if call.to_tag:
            to += f";tag={call.to_tag}"
        headers = [
            ("Via", f"SIP/2.0/TLS {_local_ip()}:{self.port};branch={_new_branch()};rport"),
            ("Max-Forwards", "70"),
            ("From", f'"{self.display_name}" <{self.identity}>;tag={call.from_tag}'),
            ("To", to),
            ("Call-ID", call.call_id),
            ("CSeq", f"{call.cseq} BYE"),
            ("User-Agent", USER_AGENT),
            ("Content-Length", "0"),
        ]
        for route in self._route_headers(
                call, outgoing=call.direction == "outgoing"):
            headers.append(("Route", route))
        try:
            self._send(SipMessage(
                start=f"BYE {call.remote_target or call.remote_uri} SIP/2.0",
                headers=headers))
        except TelephonyError:
            pass

    def _ack(self, call: Call, response: SipMessage) -> None:
        contact = _parse_uri(response.first("Contact")) or call.remote_target
        call.remote_target = contact or call.remote_target
        to = response.first("To")
        if "tag=" in to:
            call.to_tag = _header_params(to.split(";", 1)[1]).get("tag", "")
        call.route_set = response.all("Record-Route")
        headers = [
            ("Via", f"SIP/2.0/TLS {_local_ip()}:{self.port};branch={_new_branch()};rport"),
            ("Max-Forwards", "70"),
            ("From", f'"{self.display_name}" <{self.identity}>;tag={call.from_tag}'),
            ("To", to),
            ("Call-ID", call.call_id),
            ("CSeq", f"{call.cseq} ACK"),
            ("User-Agent", USER_AGENT),
            ("Content-Length", "0"),
        ]
        for route in self._route_headers(call, outgoing=True):
            headers.append(("Route", route))
        self._send(SipMessage(
            start=f"ACK {call.remote_target} SIP/2.0", headers=headers))

    def _route_headers(self, call: Call, outgoing: bool) -> List[str]:
        routes = list(call.route_set)
        if outgoing:
            routes.reverse()
        return routes

    # ── message dispatch ────────────────────────────────────────────────────

    def _dispatch(self, message: SipMessage) -> None:
        if self.trace:
            if message.is_response:
                self.log(f"SIP RX {message.start}")
                if message.status_code >= 300:
                    for name in ("Warning", "Reason", "Server",
                                 "Content-Type", "Retry-After"):
                        value = message.first(name)
                        if value:
                            self.log(f"  {name}: {value}")
                    if message.body and len(message.body) <= 1000:
                        self.log(
                            "  body: "
                            + message.body_text().replace("\r", " ").replace(
                                "\n", " ")[:800])
            else:
                self.log(
                    "SIP RX " + message.method
                    + " from " + _redact_uri(_parse_uri(message.first("From")))
                    + " to " + _redact_uri(_parse_uri(message.first("To"))))
        if message.is_response:
            cseq = message.first("CSeq").upper()
            if cseq.endswith(" REGISTER"):
                self._handle_register_response(message)
                return
            call = self._by_sip_id.get(message.first("Call-ID"))
            if call is None:
                return
            self._handle_response(call, message)
            return
        method = message.method
        if method == "INVITE":
            self._incoming_invite(message)
        elif method == "ACK":
            call = self._by_sip_id.get(message.first("Call-ID"))
            if call is not None:
                self._handle_ack(call)
        elif method == "BYE":
            call = self._by_sip_id.get(message.first("Call-ID"))
            if call is not None:
                self._reply(message, 200, "OK")
                self._end_call(call, "remote hung up")
        elif method == "CANCEL":
            call = self._by_sip_id.get(message.first("Call-ID"))
            if call is not None:
                self._reply(message, 200, "OK")
                self._end_call(call, "caller cancelled")
        elif method in ("OPTIONS", "INFO", "NOTIFY", "MESSAGE", "UPDATE"):
            self._reply(message, 200, "OK")
        else:
            self._reply(message, 501, "Not Implemented")

    def _handle_response(self, call: Call, message: SipMessage) -> None:
        code = message.status_code
        call.last_response = message
        if code in (401, 407) and call.auth_sent < 2:
            header = message.first(
                "WWW-Authenticate" if code == 401 else "Proxy-Authenticate")
            try:
                challenge = parse_www_authenticate(header)
            except SipProtocolError:
                return
            call.auth_sent += 1
            call.challenge = challenge
            auth_header = ("Authorization" if code == 401
                           else "Proxy-Authorization")
            with call.lock:
                if call.direction == "outgoing" and call.state in (
                        "dialing", "ringing"):
                    self._send_invite(call, challenge, auth_header)
            return
        if code in (180, 183):
            with call.lock:
                if call.state == "dialing":
                    call.state = "ringing"
            self._emit_call(call)
            return
        if code == 200:
            method = message.first("CSeq").split()
            method_name = method[-1].upper() if method else ""
            if method_name == "INVITE":
                with call.lock:
                    call.state = "active"
                    call.answered_at = call.answered_at or _now()
                    try:
                        media = parse_sdp(
                            message.body_text(),
                            _parse_uri(message.first("Contact")))
                    except SipProtocolError as exc:
                        call.state = "failed"
                        call.reason = str(exc)
                        self._end_call(call, str(exc), terminal_state="failed")
                        return
                    call.remote_target = (
                        _parse_uri(message.first("Contact"))
                        or call.remote_target)
                    call.to_tag = _header_params(
                        message.first("To").split(";", 1)[1]).get(
                            "tag", "") if ";" in message.first("To") else ""
                    call.route_set = message.all("Record-Route")
                    call.media = media
                    call.srtp = bool(media.srtp)
                if self.trace:
                    self.log(
                        "Remote answer media: "
                        f"{media.address}:{media.port} {media.codec} "
                        f"srtp={media.srtp} suite={media.crypto_suite}")
                    if message.body:
                        self.log("Remote SDP:\n" + message.body_text())
                self._ack(call, message)
                if call.rtp is not None:
                    call.rtp.start(
                        (media.address, media.port), media.payload,
                        media.codec, srtp=media.srtp,
                        remote_key=media.crypto_key,
                        srtp_suite=media.crypto_suite,
                    )
                call.answered_event.set()
                self._emit_call(call)
                self.log(f"Call {call.id} answered.")
                return
            if method_name == "BYE":
                self._end_call(call, "remote hung up")
                return
            self._emit_call(call)
            return
        if code >= 400:
            reason = f"{code} {message.reason}".strip()
            self.log(f"Call {call.id} failed: {reason}")
            self._end_call(call, reason, terminal_state="failed")

    def _handle_ack(self, call: Call) -> None:
        if call.state != "connecting":
            return
        with call.lock:
            call.state = "active"
            call.answered_at = call.answered_at or _now()
            remote = _parse_uri(call.sip.first("Contact")) if call.sip else ""
            if remote:
                call.remote_target = remote
            media = call.media
            if media is None and call.sip is not None and call.sip.body:
                try:
                    media = parse_sdp(call.sip.body_text(), remote)
                except SipProtocolError:
                    media = None
            if media is not None:
                call.media = media
                call.srtp = bool(media.srtp)
                if self.trace:
                    self.log(
                        "Caller media: "
                        f"{media.address}:{media.port} {media.codec} "
                        f"srtp={media.srtp} suite={media.crypto_suite}")
            if media is not None and call.rtp is not None:
                call.rtp.start(
                    (media.address, media.port), media.payload, media.codec,
                    srtp=media.srtp,
                    remote_key=media.crypto_key,
                    srtp_suite=media.crypto_suite,
                )
        call.answered_event.set()
        self._emit_call(call)
        self.log(f"Call {call.id} connected.")

    # ── audio bridge ────────────────────────────────────────────────────────

    def _forward_audio(self, call_id: str, pcm16k: bytes) -> None:
        call = self._calls.get(call_id)
        if call is None or call.state != "active":
            return
        callback = self.on_audio
        if callback is not None:
            try:
                callback(call_id, pcm16k)
            except Exception:
                pass

    def send_audio_24k(self, call_id: str, pcm: bytes) -> bool:
        call = self._calls.get(call_id)
        if call is None or call.state != "active" or call.rtp is None:
            return False
        if call.muted:
            return False
        call.rtp.send_pcm24k(pcm)
        return True

    def buffered_seconds(self, call_id: str = "") -> float:
        call = self._calls.get(call_id) if call_id else self.get_call("")
        if call is None or call.rtp is None:
            return 0.0
        return call.rtp.buffered_seconds()

    def audio_stats(self, call_id: str = "") -> Dict[str, Any]:
        call = self._calls.get(call_id) if call_id else self.get_call("")
        if call is None or call.rtp is None:
            return {}
        return call.rtp.audio_stats()

    def flush_audio(self, call_id: str = "") -> None:
        if call_id:
            call = self._calls.get(call_id)
            if call is not None and call.rtp is not None:
                call.rtp.flush()
            return
        for call in list(self._calls.values()):
            if call.rtp is not None:
                call.rtp.flush()

    # ── state / history ─────────────────────────────────────────────────────

    def _end_call(self, call: Call, reason: str, notify: bool = True,
                  terminal_state: str = "ended") -> None:
        with call.lock:
            if call.ended_at is not None:
                return
            call.ended_at = _now()
            call.state = ("failed" if terminal_state == "failed" else "ended")
            call.reason = reason or call.reason
            call.ended_event.set()
            call.answered_event.set()
            if call.rtp is not None:
                try:
                    call.rtp.stop()
                finally:
                    call.rtp = None
            call.media = None
            call.sip = None
        with self._lock:
            self._by_sip_id.pop(call.call_id, None)
        timer = self._auto_answer_timers.pop(call.id, None)
        if timer is not None:
            timer.cancel()
        self._write_history(call)
        # Keep a short recent-call tail for status, but never let a long
        # process accumulate every completed call in memory.
        with self._lock:
            ended = sorted(
                (c for c in self._calls.values()
                 if c.state in ("ended", "failed")),
                key=lambda c: c.ended_at or 0.0,
            )
            for old in ended[:-20]:
                self._calls.pop(old.id, None)
        if notify:
            self._emit_call(call)
            self.log(f"Call {call.id} ended: {reason}")

    def _end_all_calls(self, reason: str, notify: bool = True,
                       terminal_state: str = "ended") -> None:
        with self._lock:
            calls = list(self._calls.values())
        for call in calls:
            self._end_call(call, reason, notify=notify,
                           terminal_state=terminal_state)
        with self._lock:
            self._calls.clear()
            self._by_sip_id.clear()

    def _get_call(self, call_id: str) -> Call:
        call = self._calls.get(str(call_id or "").strip())
        if call is None:
            if not call_id:
                call = self.get_call("")
            if call is None:
                raise TelephonyError("No active call with that id.")
        return call

    def _emit_call(self, call: Call) -> None:
        callback = self.on_call
        if callback is None:
            return
        try:
            callback(call.id, call)
        except Exception:
            pass

    def _emit_registration(self) -> None:
        callback = self.on_registration
        if callback is None:
            return
        try:
            callback(self.registration())
        except Exception:
            pass

    def _write_history(self, call: Call) -> None:
        if call.history_written:
            return
        call.history_written = True
        record = {
            "call_id": call.id,
            "direction": call.direction,
            "remote": _redact_uri(call.remote_uri),
            "remote_display": call.remote_display,
            "state": call.state,
            "reason": call.reason,
            "started_at": _iso(call.created_at),
            "answered_at": _iso(call.answered_at) if call.answered_at else "",
            "ended_at": _iso(call.ended_at) if call.ended_at else "",
            "duration_seconds": round(call.duration(), 1),
        }
        path = Path(self.settings.get("history_path")
                    or config.CONFIG_DIR / "telephony_history.json")
        with self._history_lock:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []
            existing.append(record)
            existing = existing[-200:]
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(path.suffix + ".tmp")
                temp.write_text(json.dumps(existing, indent=2),
                                encoding="utf-8")
                os.replace(temp, path)
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────────────
#  Service used by the action layer and the voice bridge
# ─────────────────────────────────────────────────────────────────────────────

class TelephonyService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._engine: Optional[SipEngine] = None
        self._engine_lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._settings: Dict[str, Any] = {}
        self._publish: Optional[Callable[[Dict[str, Any]], None]] = None
        self._log: Optional[Callable[[str], None]] = None
        self._audio_bridge: Optional[Callable[[str, bytes], None]] = None
        self._bridge_call_id = ""
        self._last_error = ""
        # Auto-answer is only allowed while the Gemini voice session is
        # actually connected. main.py flips this around each Live connection.
        self._voice_online = False
        self._auto_answer_timers: Dict[str, threading.Timer] = {}

    # ── configuration / events ──────────────────────────────────────────────

    def configure(
        self,
        *,
        publish: Optional[Callable[[Dict[str, Any]], None]] = None,
        log: Optional[Callable[[str], None]] = None,
        audio_bridge: Optional[Callable[[str, bytes], None]] = None,
    ) -> None:
        if publish is not None:
            self._publish = publish
        if log is not None:
            self._log = log
        if audio_bridge is not None:
            self._audio_bridge = audio_bridge

    def _emit(self, kind: str, data: Dict[str, Any]) -> None:
        callback = self._publish
        if callback is None:
            return
        try:
            callback({
                "type": f"telephony.{kind}",
                "data": data,
                "ts": _iso(),
            })
        except Exception:
            pass

    def _write_log(self, text: str) -> None:
        callback = self._log
        if callback is not None:
            try:
                callback(text)
            except Exception:
                pass

    # ── engine lifecycle ────────────────────────────────────────────────────

    def settings(self) -> Dict[str, Any]:
        return config.get_telephony_settings()

    def _ensure_engine(self, *, start: bool = True) -> SipEngine:
        with self._engine_lock:
            settings = self.settings()
            current = self._engine
            password = str(settings.get("password") or "")
            problems = config.validate_telephony_settings(
                settings, require_password=True)
            if problems:
                raise TelephonyError(
                    "SIP configuration is incomplete: "
                    + "; ".join(problems)
                    + ". Set SIP_USERNAME, SIP_PASSWORD, SIP_DOMAIN, "
                      "SIP_TRANSPORT and SIP_DISPLAY_NAME in .env, then "
                      "restart Jarvis.")
            if current is None or current.password != password or (
                    current.server != settings.get("server") or
                    current.username != settings.get("username") or
                    current.port != settings.get("port")):
                if current is not None:
                    current.stop()
                current = SipEngine(
                    settings,
                    on_call=self._on_call,
                    on_incoming=self._on_incoming,
                    on_registration=self._on_registration,
                    on_audio=self._on_audio,
                    log=self._write_log,
                )
                self._engine = current
                self._settings = settings
            if start:
                current.start()
            return current

    def registration(self) -> Dict[str, Any]:
        settings = self.settings()
        problems = config.validate_telephony_settings(
            settings, require_password=True)
        if problems:
            return {
                "state": "unconfigured",
                "registered": False,
                "server": settings.get("server", ""),
                "transport": settings.get("transport", "TLS"),
                "identity": _redact_uri(settings.get("identity", "")),
                "last_error": (
                    "SIP is not configured: " + "; ".join(problems)
                    + ". Fill in the SIP settings in .env and restart Jarvis."),
            }
        engine = self._engine
        if engine is None:
            return {
                "state": "idle",
                "registered": False,
                "server": settings.get("server", ""),
                "transport": settings.get("transport", "TLS"),
                "identity": _redact_uri(settings.get("identity", "")),
                "last_error": self._last_error,
            }
        return engine.registration()

    def start(self) -> Dict[str, Any]:
        engine = self._ensure_engine(start=True)
        return engine.registration()

    def stop(self) -> None:
        for timer in list(self._auto_answer_timers.values()):
            timer.cancel()
        self._auto_answer_timers.clear()
        with self._engine_lock:
            engine = self._engine
            self._engine = None
        if engine is not None:
            engine.stop()
        self._bridge_call_id = ""
        self._voice_online = False

    # ── actions ─────────────────────────────────────────────────────────────

    def call(self, target: str) -> Dict[str, Any]:
        target = resolve_target(target, self.settings())
        # Serialize the entire check-and-dial sequence. Two concurrent model
        # turns or an approval retry must not both pass the idle check.
        with self._call_lock:
            engine = self._ensure_engine(start=True)
            engine.wait_until_registered()
            if engine.active_call() is not None or any(
                    call.get("state") in ("incoming", "dialing", "ringing",
                                          "connecting", "active", "ending")
                    for call in engine.calls()):
                raise TelephonyError(
                    "Another call is already active or connecting. Hang it up "
                    "before placing a new call.")
            call = engine.make_call(target)
        self._last_error = ""
        snapshot = call.snapshot()
        return {
            "ok": True,
            "result": (
                f"Calling {_redact_uri(target)}; waiting for the remote "
                "answer."),
            "state": snapshot.get("phase", "starting"),
            "connected": snapshot.get("phase") == "active",
            "pending_connection": snapshot.get("phase") != "active",
            "call": snapshot,
            "registration": engine.registration(),
        }

    def _select_call(
        self, engine: SipEngine, call_id: str = "",
        *, incoming: bool = False,
    ) -> Optional[Call]:
        if call_id:
            return engine.get_call(call_id)
        snapshots = engine.calls()
        for snapshot in reversed(snapshots):
            state = snapshot.get("state")
            if incoming and state != "incoming":
                continue
            if not incoming and state not in ("incoming", "dialing",
                                              "ringing", "connecting",
                                              "active", "ending"):
                continue
            return engine.get_call(snapshot["call_id"])
        return None

    def answer(self, call_id: str = "") -> Dict[str, Any]:
        engine = self._ensure_engine(start=True)
        call = self._select_call(engine, call_id, incoming=True)
        if call is None:
            raise TelephonyError("There is no incoming call to answer.")
        if call.state != "incoming":
            raise TelephonyError(
                f"Call {call.id} is {call.state}, not incoming.")
        active = engine.active_call()
        if active is not None and active.id != call.id:
            raise TelephonyError(
                "Another call is active. Hang it up before answering.")
        engine.answer(call.id)
        # The 200 OK is not the connection: wait briefly for the ACK, which is
        # the only event that makes the call active.  Do not block a voice turn
        # for the full protocol timeout; report the truthful intermediate state.
        deadline = time.monotonic() + 1.5
        while (time.monotonic() < deadline and call.state == "connecting"
               and not call.ended_event.is_set()):
            time.sleep(0.05)
        snapshot = call.snapshot()
        connected = snapshot["state"] == "active"
        return {
            "ok": connected,
            "state": snapshot.get("phase", snapshot["state"]),
            "connected": connected,
            "pending_connection": not connected
            and not snapshot.get("terminal", False),
            "result": (
                f"Call {call.id} is connected."
                if connected
                else f"Call {call.id} is {snapshot['state']}."),
            "call": snapshot,
        }

    def reject(self, call_id: str = "", reason: str = "declined") -> Dict[str, Any]:
        engine = self._ensure_engine(start=True)
        call = self._select_call(engine, call_id, incoming=True)
        if call is None:
            raise TelephonyError("There is no incoming call to reject.")
        engine.reject(call.id, reason)
        snapshot = call.snapshot()
        return {
            "ok": True,
            "state": snapshot.get("phase", snapshot["state"]),
            "connected": False,
            "result": f"Rejected the call from {call.remote_display or call.remote_uri}.",
            "call": snapshot,
        }

    def hangup(self, call_id: str = "") -> Dict[str, Any]:
        engine = self._ensure_engine(start=True)
        call = self._select_call(engine, call_id)
        if call is None:
            raise TelephonyError("There is no active call to hang up.")
        engine.hangup(call.id)
        snapshot = call.snapshot()
        return {
            "ok": True,
            "state": snapshot.get("phase", snapshot["state"]),
            "connected": False,
            "result": f"Ended the call with {call.remote_display or call.remote_uri}.",
            "call": snapshot,
        }

    def mute(self, call_id: str = "", muted: bool = True) -> Dict[str, Any]:
        engine = self._ensure_engine(start=True)
        call = self._select_call(engine, call_id)
        if call is None:
            raise TelephonyError("There is no active call to mute.")
        engine.mute(call.id, muted)
        snapshot = call.snapshot()
        return {
            "ok": True,
            "state": snapshot.get("phase", snapshot["state"]),
            "connected": snapshot["state"] == "active",
            "result": (
                f"Call {call.id} is muted."
                if muted else f"Call {call.id} is unmuted."),
            "call": snapshot,
        }

    def status(self, call_id: str = "") -> Dict[str, Any]:
        settings = self.settings()
        engine = self._engine
        calls = engine.calls() if engine is not None else []
        selected = None
        if engine is not None:
            call = engine.get_call(call_id)
            if call is not None:
                selected = call.snapshot()
        elif call_id:
            calls = []
        return {
            "ok": True,
            "state": _overall_call_state(calls),
            "connected": bool(
                selected and selected.get("state") == "active")
            or any(call.get("state") == "active" for call in calls),
            "configured": bool(settings.get("username") and settings.get("password")),
            "auto_answer": bool(settings.get("auto_answer")),
            "voice_online": bool(self._voice_online),
            "registration": self.registration(),
            "bridge": {
                "active": bool(self._bridge_call_id),
                "call_id": self._bridge_call_id,
            },
            "audio": (
                engine.audio_stats(self._bridge_call_id)
                if engine is not None and self._bridge_call_id else {}),
            "calls": calls,
            "call": selected,
            "last_error": self._last_error,
        }

    def history(self, limit: int = 20) -> Dict[str, Any]:
        limit = _parse_number(limit, 20, 1, 100)
        path = Path(self.settings().get("history_path")
                    or config.CONFIG_DIR / "telephony_history.json")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            records = raw if isinstance(raw, list) else []
        except Exception:
            records = []
        records = records[-limit:][::-1]
        return {
            "ok": True,
            "count": len(records),
            "calls": records,
        }

    # ── bridge callbacks ────────────────────────────────────────────────────

    def bridge_active(self) -> bool:
        return bool(self._bridge_call_id)

    def bridge_call_id(self) -> str:
        return self._bridge_call_id

    def send_audio_24k(self, pcm: bytes) -> bool:
        engine = self._engine
        call_id = self._bridge_call_id
        if engine is None or not call_id:
            return False
        return engine.send_audio_24k(call_id, pcm)

    def buffered_seconds(self) -> float:
        engine = self._engine
        call_id = self._bridge_call_id
        if engine is None or not call_id:
            return 0.0
        return engine.buffered_seconds(call_id)

    def flush_audio(self) -> None:
        engine = self._engine
        if engine is not None:
            engine.flush_audio(self._bridge_call_id)

    def _on_call(self, call_id: str, call: Call) -> None:
        state = call.state
        if state == "active":
            if not self._bridge_call_id:
                self._bridge_call_id = call_id
                with call.lock:
                    call.bridged = True
                self._write_log(
                    "SYS: Phone call connected — caller audio routed through "
                    "Jarvis.")
        elif state in ("ended", "failed"):
            timer = self._auto_answer_timers.pop(call_id, None)
            if timer is not None:
                timer.cancel()
            if self._bridge_call_id == call_id:
                self._bridge_call_id = ""
                with call.lock:
                    call.bridged = False
        snapshot = call.snapshot()
        if state == "failed" and snapshot.get("reason"):
            self._last_error = str(snapshot["reason"])
        self._emit("call", snapshot)
        self._write_log(
            f"SYS: Call {snapshot['call_id']} {snapshot['state']}"
            + (f" ({snapshot['reason']})" if snapshot.get("reason") else ""))

    def _on_incoming(self, call: Call) -> None:
        snapshot = call.snapshot()
        self._emit("incoming", snapshot)
        self._write_log(
            "SYS: Incoming phone call from "
            f"{snapshot['remote_display'] or snapshot['remote']}. "
            "Say 'answer' or 'reject'.")
        if (self.settings().get("auto_answer") and self._voice_online):
            self._schedule_auto_answer(call.id)

    def set_voice_online(self, online: bool) -> None:
        """Tell telephony whether the Gemini voice session is connected."""
        self._voice_online = bool(online)
        if self._voice_online:
            engine = self._engine
            if engine is not None:
                for snapshot in reversed(engine.calls()):
                    if snapshot.get("state") == "incoming":
                        self._schedule_auto_answer(snapshot["call_id"])
                        break

    def voice_online(self) -> bool:
        return bool(self._voice_online)

    def _schedule_auto_answer(self, call_id: str) -> None:
        if call_id in self._auto_answer_timers:
            return
        timer = threading.Timer(0.1, self._auto_answer, args=(call_id,))
        timer.daemon = True
        self._auto_answer_timers[call_id] = timer
        timer.start()

    def _auto_answer(self, call_id: str) -> None:
        self._auto_answer_timers.pop(call_id, None)
        try:
            result = self.answer(call_id)
            if result.get("ok"):
                self._write_log("SYS: Auto-answered the incoming call.")
            else:
                self._write_log(
                    "SYS: Auto-answer failed: "
                    + str(result.get("result") or "unknown reason"))
        except Exception as exc:  # noqa: BLE001 - timer thread must not die
            self._write_log(f"SYS: Auto-answer failed: {exc}")

    def _on_registration(self, state: Dict[str, Any]) -> None:
        self._emit("registration", state)

    def _on_audio(self, call_id: str, pcm16k: bytes) -> None:
        if call_id != self._bridge_call_id:
            # The bridge is only opened for the one call the voice engine is
            # allowed to hear. Other call records stay signalling-only.
            return
        callback = self._audio_bridge
        if callback is not None:
            try:
                callback(call_id, pcm16k)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
#  Contacts / target resolution
# ─────────────────────────────────────────────────────────────────────────────

def _load_contacts(path: Path) -> Dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: Dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, str) and key.strip() and value.strip():
                out[str(key).strip().casefold()] = value.strip()
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            address = str(item.get("sip") or item.get("address") or "").strip()
            if name and address:
                out[name.casefold()] = address
    return out


def resolve_target(value: str, settings: Optional[Dict[str, Any]] = None) -> str:
    text = str(value or "").strip()
    if not text:
        raise TelephonyError("No phone number or SIP address was given.")
    settings = settings or config.get_telephony_settings()
    contacts_path = Path(settings.get("contacts_path")
                         or config.CONFIG_DIR / "contacts.json")
    contacts = _load_contacts(contacts_path)
    contacts.update(settings.get("contacts") or {})
    lowered = text.casefold()
    if lowered in contacts:
        text = contacts[lowered]
    elif "@" not in text and not text.lower().startswith(("sip:", "sips:", "tel:")):
        # A bare number can still be a SIP user on the configured domain.
        digits = re.sub(r"[^\d+]", "", text)
        if digits and len(digits) >= 5:
            text = f"sip:{digits}@{settings.get('server', 'sip.linphone.org')}"
        else:
            available = ", ".join(sorted(contacts)) or "none"
            raise TelephonyError(
                f"Unknown contact '{text}'. Known contacts: {available}. "
                "Pass a full SIP address instead.")
    if text.lower().startswith("tel:"):
        text = "sip:" + text[4:] + f"@{settings.get('server', 'sip.linphone.org')}"
    if not text.lower().startswith(("sip:", "sips:")):
        text = "sip:" + text
    if "@" not in text:
        raise TelephonyError(
            f"SIP address '{text}' has no domain. Use sip:user@domain.")
    return text


# ─────────────────────────────────────────────────────────────────────────────
#  Singleton
# ─────────────────────────────────────────────────────────────────────────────

_service: Optional[TelephonyService] = None
_service_lock = threading.Lock()


def get_service() -> TelephonyService:
    global _service
    with _service_lock:
        if _service is None:
            _service = TelephonyService()
        return _service


def configure(**kwargs: Any) -> TelephonyService:
    service = get_service()
    service.configure(**kwargs)
    return service


def shutdown() -> None:
    global _service
    with _service_lock:
        service = _service
        _service = None
    if service is not None:
        service.stop()
