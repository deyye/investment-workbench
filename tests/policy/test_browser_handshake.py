"""动态防护握手（瑞数等）与浏览器列表发现的契约测试。

这些用例**不联网**：浏览器行为用注入的函数替代，只锁住我们自己的逻辑——

- 握手结果要**缓存**（否则每抓一条详情就开一次浏览器），force 才重取
- cookie 只能挂到**目标 host**，不能是"发往所有站点"的无域 cookie
- 抓取遇 412 时自动重握一次并重试，但**只重试一次**
- 浏览器返回的链接要按 include 过滤、去重（含 # 片段）、丢掉 javascript:
"""
import pytest
import requests

from policy_collector.collector import Collector, discover_browser_links
from policy_collector.config import AppConfig, SourceConfig


class FakeResp:
    def __init__(self, status, content=b""):
        self.status_code = status
        self.url = "https://fgw.hubei.gov.cn/fbjd/zc/zcwj/"
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self._content = content

    def iter_content(self, size):
        yield self._content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    """按给定状态码序列依次响应，记录每次实际带上的 Cookie。"""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.headers = {}
        self.sent_cookies = []
        self.cookies = requests.cookies.RequestsCookieJar()
        self.calls = 0

    def _resp(self):
        self.calls += 1
        st = self.statuses.pop(0) if self.statuses else 200
        self.sent_cookies.append(dict(self.cookies))
        return FakeResp(st, b"<html><body>" + b"x" * 100 + b"</body></html>")

    def get(self, url, **kw):
        return self._resp()

    def post(self, url, **kw):
        return self._resp()

    def close(self):
        pass


@pytest.fixture
def col(tmp_path):
    cfg = AppConfig.load()
    cfg.data_dir = tmp_path
    cfg.fetch.request_interval_seconds = 0
    cfg.fetch.retries = 2
    c = Collector(cfg)
    yield c
    c.close()


# ---------------- 握手 ----------------

def test_handshake_caches_and_force_refetches(col):
    calls = []
    col.handshake_fn = lambda url: (calls.append(url), {"S": "1"})[1]
    assert col.ensure_handshake("https://fgw.hubei.gov.cn/") is True
    assert col.ensure_handshake("https://fgw.hubei.gov.cn/") is True
    assert len(calls) == 1, "第二次应命中缓存，不该再开浏览器"
    assert col.ensure_handshake("https://fgw.hubei.gov.cn/", force=True) is True
    assert len(calls) == 2
    assert col.handshaked_hosts == ["fgw.hubei.gov.cn"]


def test_handshake_returns_false_when_no_cookie(col):
    col.handshake_fn = lambda url: {}
    assert col.ensure_handshake("https://fgw.hubei.gov.cn/") is False
    assert col.handshaked_hosts == []


def test_cookie_is_scoped_to_target_host(col):
    col.handshake_fn = lambda url: {"SESSION": "abc"}
    col.ensure_handshake("https://fgw.hubei.gov.cn/")
    jar = col.session.cookies
    mine = [c for c in jar if c.domain == "fgw.hubei.gov.cn"]
    assert any(c.name == "SESSION" and c.value == "abc" for c in mine)
    # 关键：不能存在无域 cookie —— 那会被发往所有站点，等于把凭据送给别的站
    assert not [c for c in jar if not c.domain], "不应出现无域 cookie"


def test_missing_playwright_gives_actionable_error(col):
    """没有浏览器可用时要给出可执行的提示，而不是一个裸 ImportError。"""
    from policy_collector.browser_session import BrowserUnavailable
    def boom(url):
        raise BrowserUnavailable("pip install playwright")
    col.handshake_fn = boom
    with pytest.raises(BrowserUnavailable):
        col.ensure_handshake("https://fgw.hubei.gov.cn/")


# ---------------- 412 自动重握 ----------------

def test_fetch_retries_once_after_412(col):
    col.session = FakeSession([412, 200])
    col.handshake_fn = lambda url: {"S": "fresh"}
    assert col.ensure_handshake("https://fgw.hubei.gov.cn/")
    result = col.fetch("https://fgw.hubei.gov.cn/fbjd/zc/zcwj/")
    assert result.ok is True
    assert col.session.calls == 2, "412 之后应重握并重试一次"


def test_fetch_does_not_loop_forever_on_challenge(col):
    """站点一直挑战时只刷新一次，然后如实报 412，不能变成无限重试。"""
    col.session = FakeSession([412, 412, 412, 412])
    col.handshake_fn = lambda url: {"S": "still-blocked"}
    col.ensure_handshake("https://fgw.hubei.gov.cn/")
    result = col.fetch("https://fgw.hubei.gov.cn/fbjd/zc/zcwj/")
    assert result.ok is False
    assert result.error == "HTTP 412"


def test_fetch_without_handshake_records_412_plainly(col):
    col.session = FakeSession([412])
    result = col.fetch("https://fgw.hubei.gov.cn/fbjd/zc/zcwj/")
    assert result.ok is False and result.error == "HTTP 412"
    assert col.session.calls == 1


# ---------------- 浏览器列表发现 ----------------

def test_discover_browser_links_normalizes_and_dedupes(monkeypatch, col):
    from policy_collector import browser_session
    monkeypatch.setattr(browser_session, "browser_list_and_cookies",
                        lambda entry, lst, include=None, **kw: ({"S": "1"}, [
                            ("https://fgw.hubei.gov.cn/a/t1.shtml", "  标题一  "),
                            ("https://fgw.hubei.gov.cn/a/t1.shtml#frag", "重复项"),
                            ("https://fgw.hubei.gov.cn/b/t2.shtml", "标题二"),
                            ("javascript:void(0)", "点我"),
                            ("", "空链接"),
                        ]))
    src = SourceConfig(name="hb", list_url="https://fgw.hubei.gov.cn/fbjd/zc/zcwj/",
                       handshake="https://fgw.hubei.gov.cn/")
    found = discover_browser_links(col, src, src.list_url)
    assert [c.url for c in found] == ["https://fgw.hubei.gov.cn/a/t1.shtml",
                                      "https://fgw.hubei.gov.cn/b/t2.shtml"]
    assert found[0].title == "标题一"


def test_discover_browser_links_adopts_session_cookie(monkeypatch, col):
    """列表会话里已经过完防护，cookie 要顺手接管，详情页就不必再开浏览器。"""
    from policy_collector import browser_session
    monkeypatch.setattr(browser_session, "browser_list_and_cookies",
                        lambda entry, lst, include=None, **kw: ({"SESSION": "from-list"}, []))
    src = SourceConfig(name="hb", list_url="https://fgw.hubei.gov.cn/fbjd/zc/zcwj/",
                       handshake="https://fgw.hubei.gov.cn/")
    discover_browser_links(col, src, src.list_url)
    assert col.handshaked_hosts == ["fgw.hubei.gov.cn"]
    assert any(c.name == "SESSION" for c in col.session.cookies)


def test_discover_browser_links_passes_include_through(monkeypatch, col):
    from policy_collector import browser_session
    seen = {}
    def fake(entry, lst, include=None, **kw):
        seen["entry"], seen["list"], seen["include"] = entry, lst, include
        return {}, []
    monkeypatch.setattr(browser_session, "browser_list_and_cookies", fake)
    src = SourceConfig(name="hb", list_url="https://fgw.hubei.gov.cn/fbjd/zc/zcwj/",
                       handshake="https://fgw.hubei.gov.cn/",
                       include=["/fbjd/zc/zcwj/tz/"])
    discover_browser_links(col, src, src.list_url)
    assert seen["entry"] == "https://fgw.hubei.gov.cn/"      # 握手入口取 handshake
    assert seen["include"] == ["/fbjd/zc/zcwj/tz/"]
