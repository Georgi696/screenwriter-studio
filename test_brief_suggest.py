"""Brief suggestions. The HTTP call is a mock. No KIE credits."""

import json
import unittest
from unittest import mock

import httpx2

from brief_suggest import (
    BriefSuggestError,
    brief_from_body,
    brief_from_response,
    build_request,
    parse_brief_text,
    spark_for,
    suggest_brief,
)
from models import DEVELOPMENT_MODEL, kie_responses_client


def _fake_response(text: str) -> dict:
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "model": DEVELOPMENT_MODEL,
        "status": "completed",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [],
            },
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                    }
                ],
            },
        ],
        "parallel_tool_calls": True,
        "tool_choice": "none",
        "tools": [],
    }


class PromptTest(unittest.TestCase):
    def test_empty_box_invents_inside_the_shot_budget(self):
        body = build_request("", salt="00000001")
        self.assertEqual(body["model"], DEVELOPMENT_MODEL)
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertGreaterEqual(body["max_output_tokens"], 4096)
        self.assertIn("at most 7", body["instructions"])
        self.assertIn("at most 3", body["instructions"])
        self.assertIn("one shot per 8 seconds", body["instructions"])
        self.assertIn("not a feature", body["instructions"].lower())
        self.assertIn("Invent", body["input"])
        self.assertIn(spark_for("00000001"), body["input"])
        self.assertIn("Variation 00000001", body["input"])
        self.assertNotIn("Note:", body["input"])

    def test_two_salts_do_not_ask_for_the_same_story(self):
        first = build_request("", salt="alpha")["input"]
        second = build_request("", salt="bravo")["input"]
        self.assertNotEqual(spark_for("alpha"), spark_for("bravo"))
        self.assertNotEqual(first, second)

    def test_a_note_is_rewritten_not_replaced(self):
        note = "a dented thermos on the roof at dawn"
        body = build_request(note, salt="alpha")
        self.assertIn(note, body["input"])
        self.assertIn("Keep this idea", body["input"])
        self.assertNotIn("Invent", body["input"])
        self.assertNotIn(spark_for("alpha"), body["input"])
        self.assertIn("at most 7", body["input"])


class ParseTest(unittest.TestCase):
    def test_strips_a_json_fence(self):
        raw = '```json\n{"brief": "A 30-second 9:16 comedy. **Nia** wants the last ferry."}\n```'
        text = parse_brief_text(raw)
        self.assertEqual(text, "A 30-second 9:16 comedy. Nia wants the last ferry.")
        self.assertNotIn("```", text)
        self.assertNotIn("{", text)

    def test_response_payload_skips_reasoning(self):
        payload = {
            "output": [
                {"type": "reasoning", "content": [{"type": "output_text", "text": "thinking"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "Brief: Ada wants the key."}]},
            ]
        }
        self.assertEqual(brief_from_response(payload), "Ada wants the key.")


class HttpTest(unittest.TestCase):
    def _client(self, text: str, captured: dict):
        def handler(request):
            captured["url"] = str(request.url)
            captured["auth"] = request.headers["authorization"]
            captured["body"] = json.loads(request.content.decode())
            return httpx2.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(_fake_response(text)).encode(),
            )

        return kie_responses_client(
            api_key="unit-test-key",
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )

    def test_mocked_http_returns_plain_prose(self):
        captured: dict = {}
        fenced = '```json\n{"brief": "A 30-second 9:16 comedy. **Nia** wants the last ferry."}\n```'
        client = self._client(fenced, captured)
        with mock.patch("brief_suggest.load_kie_api_key", return_value="unit-test-key"):
            brief = suggest_brief("", client=client, salt="00000001")
        self.assertEqual(brief, "A 30-second 9:16 comedy. Nia wants the last ferry.")
        self.assertTrue(captured["url"].endswith("/responses"))
        self.assertNotIn("api.openai.com", captured["url"])
        self.assertEqual(captured["auth"], "Bearer unit-test-key")
        self.assertEqual(captured["body"]["model"], DEVELOPMENT_MODEL)
        self.assertIn("Invent", captured["body"]["input"])
        self.assertIn("at most 7", captured["body"]["instructions"])

    def test_mocked_http_keeps_the_users_note_in_the_prompt(self):
        captured: dict = {}
        client = self._client("A 60-second 16:9 film about the dented thermos.", captured)
        with mock.patch("brief_suggest.load_kie_api_key", return_value="unit-test-key"):
            brief = suggest_brief("  the dented thermos  ", client=client, salt="keep")
        self.assertIn("dented thermos", brief)
        self.assertIn("the dented thermos", captured["body"]["input"])
        self.assertIn("Keep this idea", captured["body"]["input"])

    def test_missing_key_does_not_call_the_model(self):
        def create(**_kwargs):
            raise AssertionError("the model should not be called")

        client = mock.Mock()
        client.responses.create = create
        with mock.patch("brief_suggest.load_kie_api_key", return_value=""):
            with self.assertRaises(BriefSuggestError) as caught:
                suggest_brief("", client=client)
        self.assertIn("API key is not set", str(caught.exception))

    def test_errors_drop_the_key(self):
        client = mock.Mock()
        client.responses.create.side_effect = RuntimeError("rejected unit-test-key")
        with mock.patch("brief_suggest.load_kie_api_key", return_value="unit-test-key"):
            with self.assertRaises(BriefSuggestError) as caught:
                suggest_brief("a thermos", client=client)
        self.assertNotIn("unit-test-key", str(caught.exception))
        self.assertIn("[key]", str(caught.exception))


# Prefix captured from api.kie.ai `POST /codex/v1/responses` (content-type
# text/event-stream). The model had only emitted a reasoning item and
# `: keep-alive` comments; the assistant paragraph had not arrived.
_OBSERVED_SSE_PREFIX = """\
event: response.created
data: {"type":"response.created","sequence_number":0,"response":{"id":"resp_test","object":"response","status":"in_progress","model":"gpt-6-astra","instructions":"You fill the brief box on a short-film desk.","output":[],"error":null,"incomplete_details":null}}

: keep-alive

event: response.in_progress
data: {"type":"response.in_progress","sequence_number":1,"response":{"id":"resp_test","object":"response","status":"in_progress","model":"gpt-6-astra","output":[]}}

: keep-alive

event: response.output_item.added
data: {"type":"response.output_item.added","sequence_number":2,"output_index":0,"item":{"type":"reasoning","id":"rs_test","summary":[],"content":[],"encrypted_content":"gAAAA-not-the-brief"}}

"""

_BRIEF = "A 60-second 16:9 action film. Mara wants the Frankfurt bridge clear before the blast."

_SSE_WITH_BRIEF = _OBSERVED_SSE_PREFIX + f"""\
event: response.output_text.delta
data: {{"type":"response.output_text.delta","delta":"A 60-second 16:9 action film. "}}

event: response.output_text.delta
data: {{"type":"response.output_text.delta","delta":"Mara wants the Frankfurt bridge clear before the blast."}}

event: response.completed
data: {{"type":"response.completed","response":{{"id":"resp_test","status":"completed","output":[{{"type":"reasoning","encrypted_content":"gAAAA-not-the-brief","summary":[{{"type":"summary_text","text":"thinking about the blast"}}]}},{{"type":"message","role":"assistant","status":"completed","content":[{{"type":"output_text","text":"{_BRIEF}"}}]}}]}}}}

"""

_SSE_OUT_OF_ROOM = _OBSERVED_SSE_PREFIX + """\
event: response.incomplete
data: {"type":"response.incomplete","response":{"id":"resp_test","status":"incomplete","incomplete_details":{"reason":"max_output_tokens"},"output":[{"type":"reasoning","summary":[{"type":"summary_text","text":"still thinking about Frankfurt"}]}]}}

"""


class EventStreamParseTest(unittest.TestCase):
    def test_observed_stream_with_deltas_becomes_plain_prose(self):
        brief = brief_from_body(_SSE_WITH_BRIEF)
        self.assertEqual(brief, _BRIEF)
        self.assertNotIn("gAAAA", brief)
        self.assertNotIn("thinking about the blast", brief)
        self.assertNotIn("You fill the brief box", brief)

    def test_reasoning_only_stream_is_not_a_brief(self):
        with self.assertRaises(BriefSuggestError) as caught:
            brief_from_body(_OBSERVED_SSE_PREFIX)
        self.assertIn("empty brief", str(caught.exception))
        self.assertNotIn("gAAAA", str(caught.exception))
        self.assertNotIn("You fill the brief box", str(caught.exception))

    def test_incomplete_stream_does_not_claim_success(self):
        with self.assertRaises(BriefSuggestError) as caught:
            brief_from_body(_SSE_OUT_OF_ROOM)
        self.assertIn("ran out of room", str(caught.exception))
        self.assertNotIn("still thinking", str(caught.exception))


class EventStreamHttpTest(unittest.TestCase):
    def _stream_client(self, body: str):
        def handler(request):
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream;charset=UTF-8"},
                content=body.encode(),
            )

        return kie_responses_client(
            api_key="unit-test-key",
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )

    def test_event_stream_content_type_fills_the_brief(self):
        client = self._stream_client(_SSE_WITH_BRIEF)
        with mock.patch("brief_suggest.load_kie_api_key", return_value="unit-test-key"):
            brief = suggest_brief(
                "ww3, action, military style explosions, gun fire, destruction, frankfurt, germany",
                client=client,
                salt="diag",
            )
        self.assertEqual(brief, _BRIEF)
        self.assertNotIn("```", brief)
        self.assertNotIn("{", brief)

    def test_event_stream_failure_is_not_a_brief(self):
        client = self._stream_client(_SSE_OUT_OF_ROOM)
        with mock.patch("brief_suggest.load_kie_api_key", return_value="unit-test-key"):
            with self.assertRaises(BriefSuggestError) as caught:
                suggest_brief("frankfurt", client=client, salt="diag")
        self.assertIn("ran out of room", str(caught.exception))
        self.assertNotIn("still thinking", str(caught.exception))
        self.assertNotIn(_BRIEF, str(caught.exception))

    def test_json_error_payload_is_not_a_brief(self):
        def handler(_request):
            payload = {
                "id": "resp_fail",
                "object": "response",
                "created_at": 1,
                "model": DEVELOPMENT_MODEL,
                "status": "failed",
                "error": {"code": "server_error", "message": "model overloaded"},
                "output": [],
            }
            return httpx2.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(payload).encode(),
            )

        client = kie_responses_client(
            api_key="unit-test-key",
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )
        with mock.patch("brief_suggest.load_kie_api_key", return_value="unit-test-key"):
            with self.assertRaises(BriefSuggestError) as caught:
                suggest_brief("frankfurt", client=client)
        self.assertIn("model overloaded", str(caught.exception))
        self.assertNotIn("frankfurt", str(caught.exception).lower())


if __name__ == "__main__":
    unittest.main()
