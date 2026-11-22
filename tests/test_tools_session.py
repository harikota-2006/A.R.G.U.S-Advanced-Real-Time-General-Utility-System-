"""Regression tests for authority, grounding fallbacks and conversational state."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ARGUS_DB_PATH", str(Path(tempfile.gettempdir()) / "argus-tools-tests.db"))
os.environ.setdefault("ARGUS_DATA_DIR", str(Path(tempfile.gettempdir()) / "argus-tools-tests"))
import tools_hub as hub
from session_manager import ArgusSession, build_system_prompt, has_wake_word
from evolution_engine import CapabilityRegistry


class SessionTests(unittest.TestCase):
    def test_strict_wake(self):
        self.assertTrue(has_wake_word("Hey Argus, explain voltage"))
        self.assertFalse(has_wake_word("argument about voltage"))
        self.assertFalse(has_wake_word("The TV says Argus"))

    def test_locked_session_rejects_active_flag(self):
        self.assertIsNone(ArgusSession().accept_text("open Paint", active=True))

    def test_followup_does_not_need_wake(self):
        session = ArgusSession(authenticated=True)
        self.assertIsNone(session.accept_text("yes"))
        session.expect_followup({"kind": "push", "payload": "answer"})
        session.playback_finished()
        self.assertEqual(session.accept_text("yes"), "yes")
        self.assertFalse(session.expecting_reply)

    def test_confirmation_is_exact_and_expires(self):
        session = ArgusSession(authenticated=True)
        session.expect_followup({"kind": "push", "payload": "answer"})
        self.assertIsNone(session.confirmation("yes but do not send"))
        self.assertFalse(session.confirmation("cancel"))
        session.pending["expires_at"] = time.monotonic() - 1
        session.confirmation("yes")
        self.assertIsNone(session.pending)

    def test_rules_reloaded_for_each_prompt(self):
        with patch("session_manager.db.get_active_rules", side_effect=[["Use SI units"], ["Use SI units", "Keep it brief"]]):
            first, second = build_system_prompt(), build_system_prompt()
        self.assertNotIn("Keep it brief", first)
        self.assertIn("Keep it brief", second)
        self.assertIn("UTC+05:30", second)

    def test_alarm_next_occurrence(self):
        evening = datetime(2026, 11, 14, 19, 0, tzinfo=hub.IST)
        self.assertEqual(hub.resolve_alarm("4:00", evening), evening.replace(day=15, hour=4))
        afternoon = evening.replace(hour=14)
        self.assertEqual(hub.resolve_alarm("four", afternoon), afternoon.replace(hour=16))
        self.assertEqual(hub.resolve_alarm("4 pm tomorrow", afternoon), afternoon.replace(day=15, hour=16))
        with self.assertRaises(hub.ArgusToolError):
            hub.resolve_alarm("25:00", evening)

    def test_invalid_clock_minutes_do_not_become_bare_hour(self):
        with self.assertRaises(hub.ArgusToolError):
            hub.resolve_alarm("4:99")
        with self.assertRaises(hub.ArgusToolError):
            hub.resolve_alarm("4.5")

    def test_duration_words_and_fractions(self):
        self.assertEqual(hub.timer_seconds("five minutes"), 300)
        self.assertEqual(hub.timer_seconds("one hour and thirty minutes"), 5400)
        self.assertEqual(hub.timer_seconds("1.5 hours"), 5400)
        self.assertEqual(hub.timer_seconds("an hour"), 3600)
        with self.assertRaises(hub.ArgusToolError):
            hub.timer_seconds("1,5 hours")

    def test_later_date_cannot_be_sent_as_earlier_clock_occurrence(self):
        now = datetime(2026, 11, 14, 14, 0, tzinfo=hub.IST)
        target = hub.resolve_alarm("4 pm tomorrow", now)
        with self.assertRaises(hub.ArgusToolError):
            hub.validate_alarm_occurrence(target, now)

    def test_planner_cannot_turn_presentation_request_into_script_execution(self):
        with self.assertRaises(hub.ArgusToolError):
            hub._authorize_plan("Create a presentation on circuits", {"tool": "build_and_run_script", "params": {"instruction": "run malicious code"}})

    def test_chart_cannot_override_event_type_or_invent_numbers(self):
        research = {"sources": [{"title": "Device", "url": "https://example.com", "text": "Power consumption is 10 watts."}]}
        result = hub._validated_visual({"type": "lock", "kind": "bar", "labels": ["Device"], "values": [999]}, research)
        self.assertEqual(result["type"], "visual")
        self.assertEqual(result["kind"], "table")


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = CapabilityRegistry()
        self.db = MagicMock()
        self.db.cache_get.return_value = None
        self.db.extract_rule.return_value = None
        self.db.extract_and_memorize.return_value = []
        self.db.search_pdf.return_value = []
        self.patches = [patch.object(hub, "registry", self.registry), patch.object(hub, "db", self.db)]
        for p in self.patches:
            p.start()
        hub._search_locks.clear()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    async def events(self, text, session=None):
        return [event async for event in hub.stream_response(text, session or ArgusSession(authenticated=True))]

    async def test_tavily_quota_fails_over_once_and_caches(self):
        sources = {"provider": "gemini_search", "sources": [{"title": "Fact", "url": "https://example.com/source", "text": "evidence"}]}
        with patch.object(hub, "env", return_value="configured"), patch.object(hub, "_tavily", AsyncMock(side_effect=RuntimeError("432 quota"))), \
             patch.object(hub, "_gemini_search", AsyncMock(return_value=sources)) as gemini, patch.object(hub, "push_phone", AsyncMock()) as push:
            result = await hub.grounded_search("a test")
            await hub.grounded_search("a different test")
            await asyncio.gather(*hub._background_tasks)
        self.assertTrue(result["verified"])
        self.assertEqual(gemini.await_count, 2)
        self.assertEqual(push.await_count, 1)
        self.assertEqual(self.db.cache_set.call_args.kwargs["ttl"], 43200)

    async def test_cache_prevents_all_network_calls(self):
        self.db.cache_get.return_value = {"provider": "tavily", "sources": [{"text": "fact"}], "verified": True}
        with patch.object(hub, "_tavily", AsyncMock()) as search:
            self.assertTrue((await hub.grounded_search("same question"))["cached"])
        search.assert_not_awaited()

    async def test_search_all_fail_closed(self):
        with patch.object(hub, "env", return_value="configured"), patch.object(hub, "_tavily", AsyncMock(side_effect=RuntimeError)), \
             patch.object(hub, "_gemini_search", AsyncMock(return_value={"sources": []})), patch.object(hub, "_ddg", AsyncMock(side_effect=RuntimeError)):
            result = await hub.grounded_search("test")
        self.assertFalse(result["verified"])
        self.assertEqual(result["message"], hub.UNVERIFIED)

    async def test_no_evidence_never_calls_language_model(self):
        with patch.object(hub, "grounded_search", AsyncMock(return_value={"verified": False, "sources": [], "provider": "none"})), patch.object(hub, "stream_llm") as llm:
            events = await self.events("What is the latest library version?")
        llm.assert_not_called()
        self.assertIn(hub.UNVERIFIED, events[-1]["text"])
        self.assertEqual(len([e for e in events if e["type"] == "token"]), 1)

    async def test_followup_alarm_resolves_locally(self):
        session = ArgusSession(authenticated=True)
        initial = await self.events("set an alarm", session)
        self.assertTrue(initial[-1]["follow_up"])
        with patch.object(hub, "set_tablet_alarm_timer", AsyncMock(return_value="Requested alarm.")) as clock:
            await self.events("4 am", session)
        clock.assert_awaited_once_with(4, 0)
        self.assertIsNone(session.pending)

    async def test_clock_cancellation_never_dispatches(self):
        session = ArgusSession(authenticated=True)
        session.expect_followup({"kind": "alarm_time", "prefix": "set an alarm for "})
        with patch.object(hub, "set_tablet_alarm_timer", AsyncMock()) as clock:
            events = await self.events("cancel", session)
        clock.assert_not_awaited()
        self.assertEqual(events[-1]["text"], "Cancelled.")

    async def test_action_clarification_keeps_original_request(self):
        session = ArgusSession(authenticated=True)
        plans = [{"tool": "clarify", "params": {}, "question": "How many slides?"},
                 {"tool": "create_presentation", "params": {"topic": "circuits", "num_slides": 8}}]
        with patch.object(hub, "_plan_action", AsyncMock(side_effect=plans)) as planner, patch.object(hub, "execute_tool", AsyncMock(return_value={"path": "circuits.pptx"})):
            await self.events("Create a presentation on circuits", session)
            await self.events("8 slides", session)
        self.assertIn("Create a presentation on circuits", planner.call_args.args[0])
        self.assertIn("8 slides", planner.call_args.args[0])

    async def test_pdf_pronoun_followup_reuses_substantive_question(self):
        session = ArgusSession(authenticated=True)
        session.study_mode = True
        session.document_id = "pdf-id"
        session.ephemeral_history = [{"role": "user", "content": "Explain the common emitter amplifier"}]
        await self.events("Why?", session)
        self.assertIn("common emitter amplifier", self.db.search_pdf.call_args.args[0])

    async def test_confirm_push_exactly_once(self):
        session = ArgusSession(authenticated=True)
        session.expect_followup({"kind": "push", "payload": "verified result"})
        with patch.object(hub, "push_phone", AsyncMock()) as push:
            events = await self.events("yes", session)
        push.assert_awaited_once_with("verified result")
        self.assertIsNone(session.pending)
        self.assertEqual([e["text"] for e in events if e["type"] == "token"], ["Sent to your phone."])

    async def test_draft_cannot_send_without_confirmation(self):
        session = ArgusSession(authenticated=True)
        draft = {"kind": "email", "recipient": "someone@example.com", "subject": "Test", "body": "Hello"}
        with patch.object(hub, "_json", AsyncMock(return_value=draft)), patch.object(hub, "push_phone", AsyncMock()) as push, patch.object(hub, "_confirm_draft", AsyncMock()) as send:
            events = await self.events("send an email to someone@example.com saying hello", session)
        push.assert_awaited_once()
        send.assert_not_awaited()
        self.assertEqual(session.pending["kind"], "draft")
        self.assertTrue(events[-1]["follow_up"])

    async def test_script_repair_is_bounded(self):
        failure = {"ok": False, "exit_code": 1, "stderr": "NameError: name 'x' is not defined", "stdout": ""}
        with patch.object(hub, "grounded_search", AsyncMock(return_value={"sources": []})), patch.object(hub, "_json", AsyncMock(return_value={"python_code": "print(x)"})), patch.object(hub, "workstation_rpc", AsyncMock(return_value=failure)) as rpc:
            result = await hub.build_and_run_script("print a calculation")
        self.assertFalse(result["ok"])
        self.assertEqual(rpc.await_count, 3)

    async def test_attachment_host_rejected_before_network(self):
        with patch.object(hub, "env", return_value="https://ntfy.sh/my-inbox"), patch.object(hub, "client") as network:
            with self.assertRaises(hub.ArgusToolError):
                await hub.download_attachment({"url": "https://evil.example/private"})
        network.assert_not_called()

    async def test_partial_provider_failure_does_not_replay_other_provider(self):
        async def broken(*args):
            yield "First clause."
            raise RuntimeError("offline")
        with patch.object(hub, "env", return_value="configured"), patch.object(hub, "stream_groq", broken), patch.object(hub, "stream_gemini") as alternate:
            text = "".join([token async for token in hub.stream_llm("test")])
        self.assertTrue(text.startswith("First clause."))
        self.assertIn("connection stopped", text)
        alternate.assert_not_called()

    async def test_hud_marker_split_across_tokens_never_reaches_speech(self):
        async def stream(*args):
            for part in ["Short spoken summary.", "\n<H", "UD", ">\n", "```python\nprint(1)\n```"]:
                yield part
        with patch.object(hub, "stream_llm", stream):
            events = [event async for event in hub._answer_events("question", "system")]
        spoken = "".join(event["text"] for event in events if event["speak"])
        visual = "".join(event["text"] for event in events if not event["speak"])
        self.assertEqual(spoken, "Short spoken summary.")
        self.assertEqual(visual, "```python\nprint(1)\n```")

    async def test_notice_does_not_block_search_fallback(self):
        blocker = asyncio.Event()
        async def slow_notice(*args, **kwargs):
            await blocker.wait()
        sources = {"provider": "gemini_search", "sources": [{"title": "Source", "url": "https://example.com", "text": "Fact"}]}
        with patch.object(hub, "env", return_value="configured"), patch.object(hub, "_tavily", AsyncMock(side_effect=RuntimeError("432 quota"))), \
             patch.object(hub, "_gemini_search", AsyncMock(return_value=sources)), patch.object(hub, "push_phone", slow_notice):
            result = await asyncio.wait_for(hub.grounded_search("test"), 0.5)
            self.assertTrue(result["verified"])
            blocker.set()
            await asyncio.gather(*hub._background_tasks)


if __name__ == "__main__":
    unittest.main()
