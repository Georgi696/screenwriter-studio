import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from screenwriter_studio import kie
from screenwriter_studio.images import generator
from screenwriter_studio.kie import CreditsExhausted, JobFailed, ProviderHTTPError

OK = {"code": 200, "data": {"taskId": "task-1"}}
DONE = {"code": 200, "data": {"state": "success", "resultJson": {"resultUrls": ["https://example.com/a.png"]}}}


class FastRetries(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(kie, "RETRY_BASE", 0))
        self.enterContext(patch.object(generator, "POLL_INTERVAL", 0))


class SubmitTest(FastRetries):
    def submit(self, send, **kwargs):
        return kie.submit_task("https://kie/create", "key", {"model": "m"}, send=send, timeout=5, **kwargs)

    def test_rate_limit_is_retried_until_it_clears(self):
        send = Mock(side_effect=[ProviderHTTPError(429, "HTTP 429"), {"code": 429, "msg": "slow down"}, OK])
        self.assertEqual(self.submit(send), OK)
        self.assertEqual(send.call_count, 3)

    def test_rate_limit_that_never_clears_fails_the_job_safely(self):
        send = Mock(side_effect=ProviderHTTPError(429, "HTTP 429"))
        with self.assertRaises(JobFailed):
            self.submit(send)
        self.assertEqual(send.call_count, kie.SUBMIT_RETRIES + 1)

    def test_retry_after_is_honoured(self):
        waits = []
        send = Mock(side_effect=[ProviderHTTPError(429, "HTTP 429", retry_after=7), OK])
        with patch.object(kie, "sleep", waits.append):
            self.submit(send)
        self.assertEqual(waits, [7.0])

    def test_server_errors_and_timeouts_are_never_resent(self):
        for error in (ProviderHTTPError(503, "HTTP 503"), TimeoutError("lost")):
            send = Mock(side_effect=error)
            with self.assertRaises(type(error)):
                self.submit(send)
            self.assertEqual(send.call_count, 1)

    def test_other_client_errors_are_a_clean_rejection(self):
        send = Mock(side_effect=ProviderHTTPError(422, "HTTP 422: bad prompt"))
        with self.assertRaisesRegex(JobFailed, "rejected"):
            self.submit(send)
        self.assertEqual(send.call_count, 1)

    def test_out_of_credits_is_recognised_in_status_and_in_body(self):
        for send in (Mock(side_effect=ProviderHTTPError(402, "HTTP 402")),
                     Mock(return_value={"code": 402, "msg": "Insufficient credits"})):
            with self.assertRaises(CreditsExhausted):
                self.submit(send)
            self.assertEqual(send.call_count, 1)

    def test_unreachable_provider_is_not_sent(self):
        with self.assertRaisesRegex(JobFailed, "not sent"):
            self.submit(Mock(side_effect=httpx.ConnectError("refused")))

    def test_stop_during_rate_limit_wait_submits_nothing(self):
        cancel = threading.Event()
        cancel.set()
        send = Mock(side_effect=ProviderHTTPError(429, "HTTP 429"))
        with patch.object(kie, "RETRY_BASE", 1):
            with self.assertRaisesRegex(JobFailed, "nothing was submitted"):
                self.submit(send, cancel=cancel)
        self.assertEqual(send.call_count, 1)


class PollTest(FastRetries):
    def test_a_network_blip_does_not_end_the_wait(self):
        fetch = Mock(side_effect=[httpx.ReadTimeout("slow"), ProviderHTTPError(502, "HTTP 502"),
                                  {"code": 503, "msg": "busy"}, DONE])
        self.assertEqual(kie.poll_reply(fetch), DONE)

    def test_a_permanent_error_is_not_retried(self):
        fetch = Mock(side_effect=ProviderHTTPError(401, "HTTP 401"))
        with self.assertRaises(ProviderHTTPError):
            kie.poll_reply(fetch)
        self.assertEqual(fetch.call_count, 1)

    def test_repeated_failure_surfaces_after_the_limit(self):
        fetch = Mock(side_effect=ProviderHTTPError(500, "HTTP 500"))
        with self.assertRaises(ProviderHTTPError):
            kie.poll_reply(fetch)
        self.assertEqual(fetch.call_count, kie.POLL_RETRIES + 1)

    def test_image_generation_survives_a_poll_blip(self):
        replies = [OK, ProviderHTTPError(502, "HTTP 502"), DONE]

        def send(url, key, payload=None, query=None):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        with patch.object(generator, "_request", side_effect=send):
            result = generator.generate_one("key", {"input": {}})
        self.assertEqual(result, {"task_id": "task-1", "url": "https://example.com/a.png"})


class CreditsTest(FastRetries):
    def test_running_out_of_credits_stops_the_batch_and_keeps_the_failure(self):
        spec = {"aspect_ratio": "16:9", "resolution": "1K", "jobs": [
            {"id": "a", "kind": "keyframe", "prompt": "First."},
            {"id": "b", "kind": "keyframe", "prompt": "Second.", "references": ["a"]}]}
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            with patch.object(generator, "load_api_key", return_value="key"), \
                    patch.object(generator, "_request", side_effect=ProviderHTTPError(402, "HTTP 402")) as request:
                with self.assertRaises(CreditsExhausted):
                    generator.run_batch(spec, out)
            self.assertEqual(request.call_count, 1, "the dependent job must not be submitted")
            saved = (out / "manifest.json").read_text()
            self.assertIn('"state": "failed"', saved)
            self.assertIn("credits", saved.lower())


class ParallelSettingTest(unittest.TestCase):
    def test_plain_batches_follow_the_desk_setting(self):
        with patch("screenwriter_studio.settings.load", return_value={"images": {"parallel": 2}}):
            self.assertEqual(generator._parallel(), 2)
        with patch("screenwriter_studio.settings.load", return_value={}):
            self.assertEqual(generator._parallel(), 3)


if __name__ == "__main__":
    unittest.main()
