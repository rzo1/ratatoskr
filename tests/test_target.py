import pytest

from ratatoskr import target
from ratatoskr.target import TargetApi, retry_delay


class Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}
        self.ok = status < 400

    def json(self):
        return {"ok": True}


@pytest.fixture
def api(monkeypatch):
    a = TargetApi("https://ee.example.org", "glpat-x", retries=3)
    a.answers, a.sleeps = [], []
    a.session.request = lambda method, url, **kw: a.answers.pop(0)
    monkeypatch.setattr(target.time, "sleep", a.sleeps.append)
    monkeypatch.setattr(target.time, "time", lambda: 1000)
    return a


def test_wiki_rate_limit_is_retried(api, capsys):
    api.answers = [
        Resp(400, '{"message":{"base":["rate limited"]}}'),
        Resp(429, headers={"Retry-After": "30"}),
        Resp(429, headers={"RateLimit-Reset": "1005"}),
        Resp(201),
    ]
    assert api.write("POST", "/projects/1/wikis", {"title": "x"}) == {"ok": True}
    assert api.sleeps == [10, 31, 6]
    assert capsys.readouterr().out.count("target rate limit on POST /projects/1/wikis") == 3


def test_gives_up_after_retries(api):
    api.answers = [Resp(429) for _ in range(4)]
    with pytest.raises(RuntimeError, match="-> 429"):
        api.write("POST", "/projects/1/wikis", {})
    assert api.sleeps == [10, 20, 40]


def test_other_errors_are_not_retried(api):
    api.answers = [Resp(400, '{"message":{"namespace":["is not valid"]}}')]
    with pytest.raises(RuntimeError, match="namespace"):
        api.write("POST", "/projects", {})
    assert api.sleeps == []


def test_write_delay_paces_writes_only(api):
    api.write_delay = 1.5
    api.answers = [Resp(200), Resp(201)]
    api.get("/projects/1")
    api.write("POST", "/projects/1/wikis", {})
    assert api.sleeps == [1.5]


def test_backoff_is_capped():
    assert retry_delay(Resp(429), 10) == target.MAX_BACKOFF
    assert retry_delay(Resp(429, headers={"retry-after": "9999"}), 0) == target.MAX_BACKOFF
