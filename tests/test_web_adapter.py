"""The web UI adapter must expose the same surface JarvisLive used in Qt."""

from __future__ import annotations

import unittest

from web.ui import WebUI


class FakeHub:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


class WebUIAdapterTests(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.ui = WebUI(self.hub)
        self.ui.set_assistant_name("Aze")

    def types(self):
        return [event.get("type") for event in self.hub.events]

    def test_logs_are_classified_and_chat_messages_are_emitted(self):
        self.ui.write_log("You: open Telegram")
        self.ui.write_log("Aze: Opening it now.")
        snap = self.ui.snapshot()
        self.assertEqual([m["role"] for m in snap["messages"]], ["user", "assistant"])
        self.assertEqual(snap["messages"][0]["text"], "open Telegram")
        self.assertEqual(snap["messages"][1]["text"], "Opening it now.")
        levels = [entry["level"] for entry in snap["logs"]]
        self.assertEqual(levels, ["chat", "chat"])
        self.assertIn("log", self.types())
        self.assertIn("chat.message", self.types())

    def test_error_and_tool_logs_have_levels(self):
        self.ui.write_log("ERR: broken")
        self.ui.write_log("TOOL: open_web -> opened")
        self.ui.write_log("SYS: online")
        levels = [entry["level"] for entry in self.ui.snapshot()["logs"]]
        self.assertEqual(levels, ["error", "tool", "system"])

    def test_streaming_chunks_are_replaced_by_the_final_transcript(self):
        self.ui.stream_assistant_delta("Hel")
        self.ui.stream_assistant_delta("lo")
        streamed = self.ui.snapshot()
        self.assertEqual(streamed["messages"], [])
        self.assertIn("chat.delta", self.types())

        self.ui.write_log("Aze: Hello.")
        snap = self.ui.snapshot()
        self.assertEqual(len(snap["messages"]), 1)
        self.assertEqual(snap["messages"][0]["role"], "assistant")
        self.assertEqual(snap["messages"][0]["text"], "Hello.")
        deltas = [e for e in self.hub.events if e["type"] == "chat.delta"]
        finals = [e for e in self.hub.events if e["type"] == "chat.message"]
        self.assertEqual(finals[-1]["message"]["id"], deltas[0]["id"])

    def test_turn_complete_closes_a_stream_without_a_final_log(self):
        self.ui.stream_assistant_delta("Partial reply")
        self.ui.turn_complete()
        snap = self.ui.snapshot()
        self.assertEqual(snap["messages"][-1]["text"], "Partial reply")

    def test_suppressed_duplicate_discards_the_stream(self):
        self.ui.stream_assistant_delta("Repeated reply")
        self.ui.turn_complete(suppress=True)
        self.assertEqual(self.ui.snapshot()["messages"], [])
        self.assertIn("chat.discarded", self.types())

    def test_clear_conversation_starts_an_empty_transcript(self):
        self.ui.add_user_message("old turn", "old-1")
        self.ui.stream_assistant_delta("old reply")
        self.ui.turn_complete()
        self.ui.clear_conversation()
        self.assertEqual(self.ui.snapshot()["messages"], [])
        self.assertIn("chat.cleared", self.types())

    def test_typed_turn_is_not_duplicated_by_its_transcription(self):
        self.ui.add_user_message("open it", "typed-1")
        self.ui.write_log("You: open it")
        snap = self.ui.snapshot()
        self.assertEqual(len(snap["messages"]), 1)
        self.assertEqual(snap["messages"][0]["id"], "typed-1")

    def test_retried_client_message_id_is_idempotent(self):
        first = self.ui.add_user_message("hello", "same-id")
        second = self.ui.add_user_message("hello again", "same-id")
        snap = self.ui.snapshot()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual([m["text"] for m in snap["messages"]], ["hello"])

    def test_a_later_voice_turn_with_the_same_words_is_not_swallowed(self):
        self.ui.add_user_message("hello", "typed-1")
        self.ui.stream_assistant_delta("Hi.")
        self.ui.turn_complete()
        self.ui.write_log("You: hello")
        snap = self.ui.snapshot()
        self.assertEqual([m["text"] for m in snap["messages"]],
                         ["hello", "Hi.", "hello"])
        self.assertEqual(snap["messages"][-1]["source"], "voice")

    def test_a_typed_turn_queued_during_a_reply_still_deduplicates(self):
        # The reply belongs to the previous turn; the typed message is for the
        # next one and must keep its marker through the current completion.
        self.ui.stream_assistant_delta("Working.")
        self.ui.add_user_message("next", "typed-2")
        self.ui.turn_complete()
        self.ui.write_log("You: next")
        snap = self.ui.snapshot()
        self.assertEqual([m["text"] for m in snap["messages"]],
                         ["Working.", "next"])
        self.assertEqual(snap["messages"][0]["seq"], 0)
        self.assertEqual(snap["messages"][1]["seq"], 1)

    def test_a_spoken_turn_still_appears_after_a_typed_turn(self):
        self.ui.add_user_message("open it", "typed-1")
        self.ui.write_log("You: open it")
        self.ui.write_log("You: and then close it")
        snap = self.ui.snapshot()
        self.assertEqual([m["text"] for m in snap["messages"]],
                         ["open it", "and then close it"])

    def test_tool_start_and_end_update_one_row(self):
        self.ui.tool_event({"id": "call-1", "phase": "start",
                            "tool": "open_web", "target": "https://x.test",
                            "ms": 0})
        started = self.ui.snapshot()["tool_events"]
        self.assertEqual(started[0]["status"], "running")

        self.ui.tool_event({"id": "call-1", "phase": "end",
                            "tool": "open_web", "target": "https://x.test",
                            "ms": 42, "ok": True, "summary": "opened"})
        rows = self.ui.snapshot()["tool_events"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["phase"], "end")
        self.assertEqual(rows[0]["summary"], "opened")
        self.assertEqual(rows[0]["status"], "success")
        self.assertTrue(rows[0]["ok"])
        self.assertEqual(rows[0]["ms"], 42)

    def test_pending_tool_result_is_not_reported_as_success(self):
        self.ui.tool_event({
            "id": "call-pending", "phase": "end", "tool": "send_email",
            "target": "draft", "ms": 12, "ok": False, "pending": True,
            "summary": "Confirmation required",
        })
        row = self.ui.snapshot()["tool_events"][0]
        self.assertEqual(row["status"], "pending")
        self.assertFalse(row["ok"])

    def test_mute_changes_state_and_logs(self):
        self.ui.set_muted(True)
        self.assertEqual(self.ui.state, "MUTED")
        self.assertTrue(self.ui.muted)
        self.ui.set_muted(False)
        self.assertEqual(self.ui.state, "LISTENING")
        self.assertFalse(self.ui.muted)
        states = [e["state"] for e in self.hub.events if e["type"] == "state"]
        self.assertEqual(states, ["MUTED", "LISTENING"])

    def test_muting_does_not_overwrite_an_offline_link(self):
        self.ui.set_state("OFFLINE")
        self.ui.set_muted(True)
        self.assertEqual(self.ui.state, "OFFLINE")
        self.assertTrue(self.ui.muted)
        self.ui.set_muted(False)
        self.assertEqual(self.ui.state, "OFFLINE")
        self.assertFalse(self.ui.muted)

    def test_typed_entries_are_pruned_on_every_insert(self):
        for index in range(30):
            self.ui.add_user_message(f"message {index}", f"id-{index}")
        self.assertLessEqual(len(self.ui._recent_typed), 30)
        # A 20-second-old entry must not survive a later insert. The private
        # map is the only unbounded structure the adapter used to keep.
        self.ui._recent_typed["stale"] = 0.0
        self.ui.add_user_message("fresh", "id-fresh")
        self.assertNotIn("stale", self.ui._recent_typed)

    def test_audio_levels_are_throttled(self):
        self.ui.set_audio_level(0.1)
        self.ui.set_audio_level(0.9)
        audio = [e for e in self.hub.events if e["type"] == "audio"]
        self.assertEqual(len(audio), 1)

    def test_ready_contract_used_by_main_is_preserved(self):
        self.ui.mark_ready(False)
        self.assertFalse(self.ui._win._ready)
        self.ui.mark_ready(True)
        self.assertTrue(self.ui._win._ready)

    def test_snapshot_is_independent_of_internal_deques(self):
        self.ui.write_log("SYS: one")
        snap = self.ui.snapshot()
        snap["logs"].clear()
        self.assertEqual(len(self.ui.snapshot()["logs"]), 1)


if __name__ == "__main__":
    unittest.main()
