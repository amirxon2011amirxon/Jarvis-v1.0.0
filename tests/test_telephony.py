"""Offline protocol tests for the SIP/VoIP engine.

These tests never register with a real provider and never claim a call worked.
They exercise the deterministic pieces: digest auth, SIP framing, SDP, contact
resolution, action registration and a loopback RTP media check.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import config
from actions import tool_calling
from core import telephony
from core.telephony import (
    RtpSession,
    SipMessage,
    build_sdp,
    digest_authorization,
    extract_sip_messages,
    parse_sdp,
    parse_sip_message,
    parse_www_authenticate,
    resolve_target,
)


class DigestTests(unittest.TestCase):
    def test_rfc_2617_md5_example(self):
        challenge = parse_www_authenticate(
            'Digest realm="testrealm@host.com", '
            'qop="auth,auth-int", '
            'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
            'opaque="5ccc069c403ebaf9f0171e9517f40e41"')
        header = digest_authorization(
            challenge,
            method="GET",
            uri="/dir/index.html",
            username="Mufasa",
            password="Circle Of Life",
            nc=1,
            cnonce="0a4f113b",
        )
        self.assertIn('response="6629fae49393a05397450978507c4ef1"', header)
        self.assertIn("qop=auth", header)
        self.assertIn("nc=00000001", header)
        # The SIP message builder adds "Authorization: " itself. Returning the
        # header name here used to produce the invalid wire line
        # "Authorization: Authorization: Digest ..." and a 401 loop.
        self.assertTrue(header.startswith("Digest "))
        self.assertFalse(header.lower().startswith("authorization:"))

    def test_flexisip_challenge_shape(self):
        challenge = parse_www_authenticate(
            'Digest realm="sip.linphone.org", '
            'nonce="daiR7gAAAADXSlvvAAAB6ZtCBu8AAAAA", '
            'opaque="+GNywA==", algorithm=MD5, qop="auth"')
        self.assertEqual(challenge.realm, "sip.linphone.org")
        self.assertEqual(challenge.algorithm, "MD5")
        self.assertEqual(challenge.qop, "auth")


class SipFramingTests(unittest.TestCase):
    def test_extracts_multiple_messages_and_ignores_keepalive(self):
        first = SipMessage(
            start="SIP/2.0 200 OK",
            headers=[("Call-ID", "a"), ("Content-Length", "3")],
            body=b"abc",
        ).build()
        second = SipMessage(
            start="OPTIONS sip:x SIP/2.0",
            headers=[("Call-ID", "b"), ("Content-Length", "0")],
        ).build()
        buffer = bytearray(b"\r\n\r\n" + first + second)
        messages = extract_sip_messages(buffer)
        self.assertEqual([m.start for m in messages],
                         ["SIP/2.0 200 OK", "OPTIONS sip:x SIP/2.0"])
        self.assertEqual(messages[0].body, b"abc")
        self.assertEqual(buffer, b"")

    def test_parses_folded_headers(self):
        raw = (
            b"SIP/2.0 180 Ringing\r\n"
            b"Via: SIP/2.0/TLS host;branch=z9hG4bK1,\r\n"
            b" SIP/2.0/TLS host2;branch=z9hG4bK2\r\n"
            b"Call-ID: x\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        message = parse_sip_message(raw)
        self.assertEqual(message.status_code, 180)
        self.assertIn("host2", message.first("Via"))


class SdpTests(unittest.TestCase):
    def test_build_and_parse_pcmu_offer(self):
        text = build_sdp("203.0.113.10", 40000)
        self.assertIn("m=audio 40000 RTP/AVP 0 8 101", text)
        media = parse_sdp(text)
        self.assertEqual(media.address, "203.0.113.10")
        self.assertEqual(media.port, 40000)
        self.assertEqual(media.codec, "PCMU")
        self.assertEqual(media.payload, 0)

    def test_chooses_pcma_when_pcmu_is_absent(self):
        media = parse_sdp(
            "v=0\r\n"
            "c=IN IP4 198.51.100.4\r\n"
            "m=audio 6000 RTP/AVP 8 101\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n")
        self.assertEqual(media.codec, "PCMA")
        self.assertEqual(media.payload, 8)

    def test_rejects_missing_endpoint(self):
        with self.assertRaises(telephony.SipProtocolError):
            parse_sdp("v=0\r\nm=audio 0 RTP/AVP 0\r\n")

    def test_srtp_offer_round_trips_sdes_key(self):
        key = bytes(range(30))
        text = build_sdp("203.0.113.10", 40000, srtp=True,
                         crypto_key=key)
        self.assertIn("RTP/SAVPF", text)
        self.assertIn("AES_CM_128_HMAC_SHA1_80", text)
        media = parse_sdp(text)
        self.assertTrue(media.srtp)
        self.assertEqual(media.crypto_suite, "AES_CM_128_HMAC_SHA1_80")
        self.assertEqual(media.crypto_key, key)

    def test_srtp_without_supported_crypto_is_rejected(self):
        text = (
            "v=0\r\n"
            "c=IN IP4 203.0.113.10\r\n"
            "m=audio 40000 RTP/SAVPF 96\r\n"
            "a=rtpmap:96 opus/48000/2\r\n")
        with self.assertRaises(telephony.SipProtocolError):
            parse_sdp(text)


class ContactTests(unittest.TestCase):
    def test_known_contact_and_full_uri(self):
        settings = telephony.config.get_telephony_settings()
        self.assertEqual(
            resolve_target("Amirxon", settings),
            "sip:amirxon2011@sip.linphone.org")
        self.assertEqual(
            resolve_target("sip:someone@example.org", settings),
            "sip:someone@example.org")
        self.assertEqual(
            resolve_target("+998901234567", settings),
            "sip:+998901234567@sip.linphone.org")

    def test_unknown_name_is_an_error_not_an_invented_call(self):
        settings = telephony.config.get_telephony_settings()
        with self.assertRaises(telephony.TelephonyError):
            resolve_target("Nobody At All", settings)


class EnvFileTests(unittest.TestCase):
    def test_dotenv_loader_supports_comments_quotes_and_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\n"
                "export SIP_USERNAME=from_file\n"
                "SIP_PASSWORD=\"secret # not a comment\"\n"
                "SIP_DOMAIN='sip.example.test'\n"
                "SIP_TRANSPORT=tls\n"
                "SIP_DISPLAY_NAME=Jarvis\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {
                    "SIP_USERNAME": "from_process"}, clear=True):
                loaded = config._load_env_file(path)
                self.assertGreaterEqual(loaded, 4)
                self.assertEqual(os.environ["SIP_USERNAME"], "from_process")
                self.assertEqual(
                    os.environ["SIP_PASSWORD"], "secret # not a comment")
                self.assertEqual(
                    os.environ["SIP_DOMAIN"], "sip.example.test")

    def test_sip_env_names_reach_telephony_settings(self):
        with mock.patch.dict(os.environ, {
                "SIP_USERNAME": "alice",
                "SIP_PASSWORD": "opaque-value",
                "SIP_DOMAIN": "sip.example.test",
                "SIP_TRANSPORT": "tls",
                "SIP_DISPLAY_NAME": "Alice Phone",
                "SIP_PORT": "5061"}, clear=True):
            settings = telephony.get_service().settings()
            self.assertEqual(settings["username"], "alice")
            self.assertEqual(settings["password"], "opaque-value")
            self.assertEqual(settings["server"], "sip.example.test")
            self.assertEqual(settings["transport"], "TLS")
            self.assertEqual(settings["display_name"], "Alice Phone")
            self.assertEqual(settings["identity"],
                             "sip:alice@sip.example.test")

    def test_missing_password_is_actionable_and_never_echoed(self):
        with mock.patch.dict(os.environ, {
                "SIP_USERNAME": "alice",
                "SIP_PASSWORD": "",
                "SIP_DOMAIN": "sip.example.test"}, clear=True):
            settings = config.get_telephony_settings()
            problems = config.validate_telephony_settings(
                settings, require_password=True)
            self.assertTrue(problems)
            text = " ".join(problems)
            self.assertIn(".env", text)
            self.assertIn("SIP_PASSWORD", text)
            self.assertNotIn("alice", text)


class AutoAnswerTests(unittest.TestCase):
    def test_auto_answer_requires_voice_online_and_is_prompt(self):
        from core.telephony import Call

        service = telephony.TelephonyService()
        call = Call(
            id="call1", direction="incoming", remote_uri="sip:alice@example",
            remote_display="Alice", local_uri="sip:jarvis@example",
            call_id="sip-call-1")
        with mock.patch.object(
                service, "settings", return_value={"auto_answer": True}), \
                mock.patch.object(service, "answer",
                                  return_value={"ok": True}) as answer:
            service._on_incoming(call)
            time.sleep(0.25)
            answer.assert_not_called()
            service.set_voice_online(True)
            service._on_incoming(call)
            time.sleep(0.35)
            answer.assert_called_once_with("call1")
        service.stop()


class ConfigTests(unittest.TestCase):
    def test_password_is_environment_only_and_not_returned(self):
        with mock.patch.dict(os.environ, {
                "JARVIS_SIP_USERNAME": "jarvis2011",
                "JARVIS_SIP_PASSWORD": "super-secret",
                "JARVIS_SIP_SERVER": "sip.linphone.org"}, clear=False):
            settings = telephony.get_service().settings()
            self.assertEqual(settings["username"], "jarvis2011")
            self.assertEqual(settings["password"], "super-secret")
            status = telephony.get_service().status()
            self.assertNotIn("super-secret", repr(status))
            self.assertNotIn("password", status["registration"])

    def test_auto_answer_defaults_off(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_SIP_AUTO_ANSWER", None)
            settings = telephony.get_service().settings()
            self.assertFalse(settings["auto_answer"])
            self.assertTrue(settings["srtp"])


class RtpQueueTests(unittest.TestCase):
    def test_queued_audio_is_not_starved_by_more_than_one_frame(self):
        """Regression: the TX buffer checked total bytes and sent silence.

        With two 20 ms frames queued, `_next_tx_frame` must pop from the chunk
        deque, not assume an empty buffer because `_tx_bytes > frame size`.
        """
        import audioop
        import math
        import struct

        session = RtpSession(stun_server="")
        try:
            samples = [
                int(9000 * math.sin(2 * math.pi * 440 * i / 24000))
                for i in range(960)
            ]
            pcm = b"".join(struct.pack("<h", s) for s in samples)
            session.send_pcm24k(pcm)
            first = session._next_tx_frame()
            second = session._next_tx_frame()
            self.assertGreater(audioop.rms(first, 2), 1000)
            self.assertGreater(audioop.rms(second, 2), 1000)
            self.assertEqual(session.buffered_seconds(), 0.0)
        finally:
            session.stop()


class RtpLoopbackTests(unittest.TestCase):
    def test_loopback_audio_is_decoded_at_16khz(self):
        received: list[bytes] = []
        ready = threading.Event()

        def on_audio(pcm: bytes) -> None:
            if not received:
                received.append(pcm)
                ready.set()

        a = RtpSession(stun_server="")
        b = RtpSession(on_pcm16k=on_audio, stun_server="")
        try:
            b.start(a.advertise_address(), telephony.RTP_PAYLOAD_PCMU)
            a.start(b.advertise_address(), telephony.RTP_PAYLOAD_PCMU)
            # 20 ms of a 440 Hz tone at 24 kHz, 16-bit mono.
            import math
            import struct

            samples = [
                int(6000 * math.sin(2 * math.pi * 440 * i / 24000))
                for i in range(480)
            ]
            pcm24 = b"".join(struct.pack("<h", s) for s in samples)
            a.send_pcm24k(pcm24)
            self.assertTrue(ready.wait(2.0), "no RTP audio received")
            self.assertGreaterEqual(len(received[0]), 300)
            self.assertEqual(len(received[0]) % 2, 0)
        finally:
            a.stop()
            b.stop()

    @unittest.skipUnless(telephony.pylibsrtp is not None,
                         "pylibsrtp is not installed")
    def test_srtp_loopback_audio_is_decoded(self):
        received: list[bytes] = []
        ready = threading.Event()

        def on_audio(pcm: bytes) -> None:
            if not received:
                received.append(pcm)
                ready.set()

        key_a = bytes(range(30))
        key_b = bytes(range(30, 60))
        a = RtpSession(stun_server="")
        b = RtpSession(on_pcm16k=on_audio, stun_server="")
        a._local_srtp_key = key_a
        b._local_srtp_key = key_b
        try:
            b.start(a.advertise_address(), telephony.RTP_PAYLOAD_PCMU,
                    srtp=True, remote_key=key_a)
            a.start(b.advertise_address(), telephony.RTP_PAYLOAD_PCMU,
                    srtp=True, remote_key=key_b)
            import math
            import struct

            samples = [
                int(6000 * math.sin(2 * math.pi * 440 * i / 24000))
                for i in range(480)
            ]
            a.send_pcm24k(
                b"".join(struct.pack("<h", s) for s in samples))
            self.assertTrue(ready.wait(2.0), "no SRTP audio received")
            self.assertGreaterEqual(len(received[0]), 300)
        finally:
            a.stop()
            b.stop()


class RegistryTests(unittest.TestCase):
    def test_phone_actions_are_registered(self):
        expected = {
            "call", "answer_call", "reject_call", "hangup",
            "mute_call", "unmute_call", "get_call_status",
            "get_call_history",
        }
        self.assertTrue(expected.issubset(tool_calling.TOOL_REGISTRY))
        declarations = {
            item["name"]: item for item in
            tool_calling.get_tool_declarations()
        }
        self.assertIn("call", declarations)
        self.assertIn("SIP", declarations["call"]["description"])

    def test_phone_tool_module_is_discovered(self):
        from actions import get_tool_module

        module = get_tool_module("phone_call")
        self.assertIsNotNone(module)
        self.assertTrue(callable(module.call))
        self.assertTrue(callable(module.get_call_status))


if __name__ == "__main__":
    unittest.main()
