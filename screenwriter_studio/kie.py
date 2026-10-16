"""Shared KIE.ai transport: one pooled HTTP client, error classes, retry and backoff.

Image and video jobs both submit a task, poll it and download a file. The rules that keep
that safe live here once:

* A submit is retried only when the answer proves no task was created (429, 408, or a
  429 in the reply body). A timeout, a reset or a 5xx may have created a billable task,
  so those are never retried automatically.
* A status check never creates work, so transient failures (429, 5xx, network) are retried
  with exponential backoff and jitter, honouring Retry-After.
* 402 means the account is out of credits. That stops the batch instead of failing every
  remaining job one by one.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
SUBMIT_RETRIES = 4
POLL_RETRIES = 3
RETRY_BASE = 2.0
RETRY_CAP = 30.0
USER_AGENT = "ScreenwriterStudio/1.0"

# Statuses that mean "try again later" for a call that creates nothing.
TRANSIENT_CODES = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})
# Statuses that prove a submit was refused before any task existed, so it is safe to resend.
RESEND_CODES = frozenset({408, 429})

sleep = time.sleep


class ProviderHTTPError(RuntimeError):
    """The provider answered with an HTTP error status."""

    def __init__(self, code: int, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class JobFailed(RuntimeError):
    """Provider reports a terminal failure; an explicit resume may submit a retry."""


class CreditsExhausted(JobFailed):
    """The account has no credits left. Nothing was created; top up, then resume."""


_client: httpx.Client | None = None
_client_lock = threading.Lock()


def client() -> httpx.Client:
    """One pooled client for the process. Timeouts are per phase, so a hung socket ends itself."""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        return _client


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return max(0.0, min(float(response.headers.get("Retry-After", "")), RETRY_CAP * 4))
    except ValueError:
        return None


def request(url: str, api_key: str, payload: dict | None = None, query: dict | None = None) -> dict:
    """One JSON call to KIE. Raises ProviderHTTPError for an HTTP error status."""
    response = client().request(
        "POST" if payload is not None else "GET", url,
        params=query, json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    if response.status_code >= 400:
        raise ProviderHTTPError(
            response.status_code,
            f"HTTP {response.status_code}: {response.text[:400]}",
            _retry_after(response),
        )
    try:
        reply = response.json()
    except ValueError as exc:
        raise RuntimeError("The provider answered with something that is not JSON.") from exc
    if not isinstance(reply, dict):
        raise RuntimeError("The provider answered with an unexpected reply.")
    return reply


def within(seconds: float, fn: Callable):
    """Bound a call by wall-clock time. Per-phase HTTP timeouts do not stop a slow trickle."""
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise TimeoutError(f"Provider request timed out after {seconds}s.")
    if "error" in box:
        raise box["error"]
    return box["value"]


def transient(error: BaseException) -> bool:
    """A failure of a call that creates nothing, where trying again can work."""
    if isinstance(error, ProviderHTTPError):
        return error.code in TRANSIENT_CODES
    return isinstance(error, (httpx.TransportError, TimeoutError, ConnectionError))


def backoff(attempt: int, error: BaseException | None = None) -> float:
    """Seconds to wait before retry number attempt+1: Retry-After if given, else jittered doubling."""
    hinted = getattr(error, "retry_after", None)
    if hinted:
        return float(hinted)
    return min(RETRY_CAP, RETRY_BASE * 2 ** attempt) * random.uniform(0.5, 1.0)


def pause(seconds: float, cancel: threading.Event | None = None) -> bool:
    """Wait; True when cancel was set meanwhile."""
    if seconds <= 0:
        return bool(cancel is not None and cancel.is_set())
    if cancel is not None:
        return cancel.wait(seconds)
    sleep(seconds)
    return False


def submit_task(url: str, api_key: str, payload: dict, *, send: Callable, timeout: float,
                cancel: threading.Event | None = None) -> dict:
    """createTask, sorting failures into "certainly not created" and "unknown".

    A 4xx answer or a failed connection means no task exists, so the job is failed and an
    explicit resume may submit it again. 429 and 408 are retried here with backoff.
    A timeout, a reset after sending, or a 5xx may have created a billable task, so those
    propagate unchanged and the caller keeps the job "unknown".
    """
    for attempt in range(SUBMIT_RETRIES + 1):
        last = attempt == SUBMIT_RETRIES
        try:
            reply = within(timeout, lambda: send(url, api_key, payload=payload))
        except ProviderHTTPError as exc:
            if exc.code == 402:
                raise CreditsExhausted(f"KIE credits are exhausted ({exc}). Add credits, then resume.") from exc
            if exc.code in RESEND_CODES and not last:
                if pause(backoff(attempt, exc), cancel):
                    raise JobFailed("Stopped while waiting out a rate limit; nothing was submitted.") from exc
                continue
            if 400 <= exc.code < 500:
                raise JobFailed(f"submit rejected: {exc}") from exc
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise JobFailed(f"not sent, the provider could not be reached: {exc}") from exc
        code = reply.get("code") if isinstance(reply, dict) else None
        if code == 402:
            raise CreditsExhausted(
                f"KIE credits are exhausted ({reply.get('msg') or 'insufficient credits'}). Add credits, then resume.")
        if code == 429 and not last:
            if pause(backoff(attempt), cancel):
                raise JobFailed("Stopped while waiting out a rate limit; nothing was submitted.")
            continue
        return reply
    raise AssertionError("unreachable")  # pragma: no cover


def poll_reply(fetch: Callable[[], dict], *, cancel: threading.Event | None = None,
               retries: int | None = None) -> dict:
    """One status check, retried on transient failures. Other non-200 replies are returned to the caller."""
    limit = POLL_RETRIES if retries is None else retries
    for attempt in range(limit + 1):
        try:
            reply = fetch()
            code = reply.get("code")
            if code in TRANSIENT_CODES:
                raise ProviderHTTPError(int(code), f"HTTP {code}: {reply.get('msg') or 'provider error'}")
            return reply
        except Exception as exc:
            if attempt >= limit or not transient(exc):
                raise
            if pause(backoff(attempt, exc), cancel):
                raise RuntimeError("Generation cancelled; provider task saved for resume.") from exc
    raise AssertionError("unreachable")  # pragma: no cover


def first_url(result_json) -> str:
    if isinstance(result_json, str) and result_json.strip():
        try:
            result_json = json.loads(result_json)
        except json.JSONDecodeError:
            return ""
    if not isinstance(result_json, dict):
        return ""
    urls = result_json.get("resultUrls") or result_json.get("result_urls") or []
    if isinstance(urls, str):
        return urls
    if urls:
        return str(urls[0])
    for key in ("resultUrl", "url", "image_url"):
        if result_json.get(key):
            return str(result_json[key])
    return ""


def fetch_bytes(url: str, *, timeout: float = 90) -> bytes:
    """A small file (an image), bounded by wall-clock time."""
    def get() -> bytes:
        response = client().get(url)
        response.raise_for_status()
        return response.content
    return within(timeout, get)


def stream_to_file(url: str, path: Path, *, cancel: threading.Event | None = None,
                   check: Callable[[threading.Event | None], None] | None = None,
                   deadline: float = 600, limit: int = 2_000_000_000) -> int:
    """Stream a large file (a video) to path. Returns the byte count. The caller picks the temp name."""
    size = 0
    stop = time.monotonic() + deadline
    with client().stream("GET", url) as response:
        response.raise_for_status()
        with path.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                if check is not None:
                    check(cancel)
                if time.monotonic() > stop:
                    raise TimeoutError("Download timed out; resume to retry the saved URL.")
                size += len(chunk)
                if size > limit:
                    raise ValueError("Provider file exceeds the download limit.")
                handle.write(chunk)
    return size
