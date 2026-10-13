import pytest
from playwright.sync_api import Error as PlaywrightError
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


def test_closed_browser_stops_with_a_hint(session):
    closed = PlaywrightError("Target page, context or browser has been closed")
    session.answers = {f"{BASE}/things": [closed]}
    with pytest.raises(SystemExit, match="browser window was closed"):
        list(session.paginate("/things"))
    assert session.sleeps == []


def test_list_projects_shows_progress(capsys):
    class Paged:
        def paginate(self, path, params=None, on_page=None):
            for page in (1, 2):
                on_page(page, page * 100)
                yield from (
                    {"path_with_namespace": f"g/p{page}-{i}", "visibility": "private"}
                    for i in range(100)
                )

    args = type("Args", (), {"scope": "all", "include_archived": False, "visibility": ["private"]})
    assert len(source.list_projects(Paged(), args)) == 200
    out = capsys.readouterr().out
    assert "page 1: 100 projects so far" in out
    assert "page 2: 200 projects so far" in out
    assert "200 projects found" in out


@pytest.mark.parametrize(
    ("visibility", "api_filter", "kept"),
    [
        (["private"], "private", ["g/a"]),  # default: only private pages are requested
        (["private", "internal"], None, ["g/a", "g/b"]),
        (["private", "internal", "public"], None, ["g/a", "g/b", "g/c"]),
    ],
)
def test_list_projects_visibility_filter(visibility, api_filter, kept):
    seen = {}

    class Paged:
        def paginate(self, path, params=None, on_page=None):
            seen.update(params)
            yield from (
                {"path_with_namespace": f"g/{name}", "visibility": vis}
                for name, vis in (("a", "private"), ("b", "internal"), ("c", "public"))
                if api_filter is None or vis == api_filter
            )

    args = type("Args", (), {"scope": "membership", "include_archived": False})()
    args.visibility = visibility
    projects = source.list_projects(Paged(), args)
    assert seen.get("visibility") == api_filter
    assert seen["membership"] == "true" and seen["archived"] == "false"
    assert [p["path_with_namespace"] for p in projects] == kept


def test_requests_are_paced(session, monkeypatch):
    session.min_interval = 0.5
    clock = iter([10.0, 10.2, 10.5, 11.5, 11.5])
    monkeypatch.setattr(source.time, "monotonic", lambda: next(clock))
    session.answers = {f"{BASE}/x": [Resp(200, 1), Resp(200, 2), Resp(200, 3)]}
    assert [session.get("/x") for _ in range(3)] == [1, 2, 3]
    assert session.sleeps == [pytest.approx(0.3)]  # only the second request came too early


def test_unpaced_by_default(session):
    session.answers = {f"{BASE}/x": [Resp(200, 1), Resp(200, 2)]}
    session.get("/x"), session.get("/x")
    assert session.sleeps == []


def test_pauses_when_the_rate_limit_is_almost_used_up(session, monkeypatch, capsys):
    monkeypatch.setattr(source.time, "time", lambda: 1000)
    quota = {"ratelimit-limit": "600", "ratelimit-reset": "1030"}
    session.answers = {
        f"{BASE}/x": [
            Resp(200, 1, {**quota, "ratelimit-remaining": "100"}),
            Resp(200, 2, {**quota, "ratelimit-remaining": "30"}),
        ]
    }
    assert session.get("/x") == 1 and session.sleeps == []
    assert session.get("/x") == 2 and session.sleeps == [30]
    assert "30 of 600 requests left), pausing 30s" in capsys.readouterr().out


def test_max_rate_option(monkeypatch):
    from ratatoskr import cli

    made = []
    monkeypatch.setattr(cli, "SourceSession", lambda *a: made.append(a[-1]))
    args = cli.build_parser().parse_args(["checkout", "--source-url", "s"])
    cli.open_source(args)
    args.protocol = "api"
    cli.open_source(args)
    args.max_rate = 0
    cli.open_source(args)
    args.max_rate = 2
    cli.open_source(args)
    assert made == [None, cli.API_DEFAULT_RATE, None, 2]
