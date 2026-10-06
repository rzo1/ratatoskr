import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ratatoskr import source
from ratatoskr.source import SourceError, SourceSession

BASE = "https://source.example.org/api/v4"


class Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status, self.ok, self._payload = status, status < 400, payload
        self.headers = headers or {}

    def json(self):
        return self._payload

    def text(self):
        return str(self._payload)


@pytest.fixture
def session(monkeypatch):
    """A SourceSession whose browser requests are answered from ``session.answers``."""
    s = SourceSession("source.example.org", retries=3)
    s.answers = {}
    s.requested = []

    def get(url, params=None, fail_on_status_code=False, timeout=None):
        assert timeout == 120_000
        s.requested.append(url)
        answer = s.answers[url].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    s.ctx = type("Ctx", (), {"request": type("Req", (), {"get": staticmethod(get)})})()
    s.sleeps = []
    monkeypatch.setattr(source.time, "sleep", s.sleeps.append)
    return s


def test_pagination_and_relogin_on_expired_session(session, monkeypatch):
    session.answers = {
        f"{BASE}/things": [
            Resp(401),
            Resp(200, [1, 2], {"link": f'<{BASE}/things?p=2>; rel="next"'}),
        ],
        f"{BASE}/things?p=2": [Resp(200, [3])],
    }
    logins = []
    monkeypatch.setattr(session, "login", lambda: logins.append(1))
    assert list(session.paginate("/things")) == [1, 2, 3]
    assert logins == [1]


def test_slow_server_is_retried_with_backoff(session):
    session.answers = {
        f"{BASE}/things": [
            PlaywrightTimeout("Timeout 120000ms exceeded"),
            Resp(502),
            Resp(429, headers={"retry-after": "30"}),
            Resp(200, [1]),
        ]
    }
    assert list(session.paginate("/things")) == [1]
    assert session.sleeps == [5, 10, 30]  # exponential backoff, Retry-After wins


def test_gives_up_after_retries(session):
    session.answers = {f"{BASE}/things": [PlaywrightTimeout("Timeout") for _ in range(4)]}
    with pytest.raises(SourceError, match="failed after 4 attempts: Timeout"):
        list(session.paginate("/things"))
    assert len(session.sleeps) == 3


def test_persistent_server_error_is_reported(session):
    session.answers = {f"{BASE}/things": [Resp(503, "down") for _ in range(4)]}
    with pytest.raises(SourceError) as exc:
        list(session.paginate("/things"))
    assert exc.value.status == 503


def test_client_errors_are_not_retried(session):
    session.answers = {f"{BASE}/things": [Resp(404, "nope")]}
    with pytest.raises(SourceError) as exc:
        list(session.paginate("/things"))
    assert exc.value.status == 404
    assert session.sleeps == []


def test_download_skips_html_and_errors(session):
    url = "https://source.example.org/g/p/uploads/x/f.png"
    session.answers = {url: [Resp(200, headers={"content-type": "text/html"}), Resp(404)]}
    assert session.download(url) is None
    assert session.download(url) is None
