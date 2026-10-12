"""Shared throttled HTTP client.

ALL fetches go through here (named nethttp, not http, to avoid shadowing stdlib).

- Thread-safe per-host minimum spacing: one gate per host. Each request reserves
  the next free slot at least min_interval after the previous request to that host.
  sec.gov and its subdomains share one gate, floored at 0.15s (under SEC's
  10 requests/second cap). asx.com.au and its subdomains share one gate, floored at 2s.
- Retries 403/429/5xx and network errors with exponential backoff (base*2^n, capped),
  honouring Retry-After (delta-seconds or HTTP date).
- Every SEC request sends an identifying User-Agent that includes the contact email
  from the SEC_CONTACT_EMAIL environment variable. The client refuses to send when
  that variable is missing or the User-Agent does not include it.
"""
import email.utils
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
RETRY_CODES = {403, 429, 500, 502, 503, 504}


def _retryable(code):
    return code in RETRY_CODES or 500 <= code <= 599


# Under SEC's 10 requests/second fair-access cap.
SEC_MIN_INTERVAL = 0.15
ASX_MIN_INTERVAL = 2.0

_HOST_FLOORS = {
    "sec.gov": SEC_MIN_INTERVAL,
    "asx.com.au": ASX_MIN_INTERVAL,
}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_gates = {}
_gates_guard = threading.Lock()


class EndpointError(RuntimeError):
    pass


def _host_key(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "sec.gov" or host.endswith(".sec.gov"):
        return "sec.gov"
    if host == "asx.com.au" or host.endswith(".asx.com.au"):
        return "asx.com.au"
    # ASX company announcements are served from this host. It shares the ASX gate.
    if host == "asx.api.markitdigital.com":
        return "asx.com.au"
    return host


def _gate(host):
    with _gates_guard:
        gate = _gates.get(host)
        if gate is None:
            gate = {"lock": threading.Lock(), "next": 0.0}
            _gates[host] = gate
        return gate


def sec_contact_email():
    """Contact address for SEC fair access. Read from the environment only."""
    return os.environ.get("SEC_CONTACT_EMAIL", "").strip()


def _sec_user_agent():
    contact = sec_contact_email()
    if not contact or not _EMAIL_RE.fullmatch(contact):
        return ""
    return f"cause-n-effect ({contact})"


def _require_sec_identity(ua):
    contact = sec_contact_email()
    if not contact or not _EMAIL_RE.fullmatch(contact) or contact not in (ua or ""):
        raise EndpointError(
            "SEC_CONTACT_EMAIL must be set and included in the User-Agent; refusing to send"
        )


class Client:
    def __init__(self, ua, min_interval, require_contact=False, retries=4, backoff_base=2.0, backoff_cap=60.0):
        if require_contact and not _EMAIL_RE.search(ua or ""):
            raise EndpointError("User-Agent must include a contact email; refusing to send")
        self.ua = ua
        self.min_interval = min_interval
        self.retries = retries
        self.base = backoff_base
        self.cap = backoff_cap
        self.sent = []  # monotonic send times, for tests

    def _interval_for(self, host):
        return max(self.min_interval, _HOST_FLOORS.get(host, 0.0))

    def _throttle(self, url):
        host = _host_key(url)
        gate = _gate(host)
        interval = self._interval_for(host)
        with gate["lock"]:
            now = time.monotonic()
            slot = max(now, gate["next"])
            gate["next"] = slot + interval
        if slot > now:
            time.sleep(slot - now)
        self.sent.append(time.monotonic())

    @staticmethod
    def _retry_after(header):
        if not header:
            return None
        try:
            return max(0.0, float(header))
        except ValueError:
            try:
                return max(0.0, email.utils.parsedate_to_datetime(header).timestamp() - time.time())
            except Exception:
                return None

    def get(self, url, accept="application/json"):
        if _host_key(url) == "sec.gov":
            _require_sec_identity(self.ua)
        for n in range(self.retries + 1):
            self._throttle(url)
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.ua,
                    "Accept": accept,
                    "Accept-Encoding": "identity",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    return response.read(), response.headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                if not _retryable(exc.code) or n == self.retries:
                    raise EndpointError(f"HTTP {exc.code} for {url}") from None
                wait = self._retry_after(exc.headers.get("Retry-After"))
                if wait is None:
                    wait = min(self.cap, self.base * 2 ** n)
            except urllib.error.URLError as exc:
                if n == self.retries:
                    raise EndpointError(f"network error {exc.reason} for {url}") from None
                wait = min(self.cap, self.base * 2 ** n)
            time.sleep(wait)

    def json(self, url):
        body, content_type = self.get(url)
        try:
            return json.loads(body)
        except ValueError:
            raise EndpointError(f"non-JSON ({content_type}) from {url}: {body[:120]!r}") from None


def sec_client(retries=4, backoff_base=2.0, backoff_cap=60.0):
    """SEC client: 0.15s spacing and a User-Agent that includes SEC_CONTACT_EMAIL."""
    ua = _sec_user_agent()
    if not ua:
        raise EndpointError(
            "SEC_CONTACT_EMAIL must be set to a contact email; refusing to send"
        )
    return Client(
        ua,
        SEC_MIN_INTERVAL,
        require_contact=True,
        retries=retries,
        backoff_base=backoff_base,
        backoff_cap=backoff_cap,
    )


def asx_client(retries=4, backoff_base=2.0, backoff_cap=60.0):
    """ASX client: 2s spacing between requests."""
    return Client(
        "cause-n-effect (ASX research)",
        ASX_MIN_INTERVAL,
        require_contact=False,
        retries=retries,
        backoff_base=backoff_base,
        backoff_cap=backoff_cap,
    )
