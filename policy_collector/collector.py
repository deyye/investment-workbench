"""Bounded HTTP downloads, immutable originals and configured list discovery."""
from __future__ import annotations

import hashlib
import ast
import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup
from .config import AppConfig, SourceConfig


@dataclass
class FetchResult:
    ok: bool = True
    url: str = ""
    final_url: str = ""
    content: bytes = b""
    content_type: str = ""
    error: str = ""
    sha256: str = ""


@dataclass
class CandidateLink:
    url: str
    title: str = ""


def normalized_url(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))


def url_from_onclick(value: str) -> str:
    """从 onclick 属性里提取 URL。

    部分政府站 CMS 不给 <a> 写 href，把链接放进 onclick。实测天津：
        <a onclick="isDownLoad(this,'https://fzgg.tj.gov.cn/xxfb/tzggx/202609/t20260911_7372816.html')">
    这类页面的列表**是静态存在的**，但按 href 提取会得到"零文章链接"，
    从而被误判成"列表由 JS 渲染"（需要另写数据接口）——实测天津、以及同类的
    苏迪/托普系 CMS 都能被这个函数直接救回来。

    只认看起来像链接的引号串（含路径分隔，或以 html/jsp/php/do 结尾），
    避免把 `void(0)`、`this`、`true` 之类的参数当成链接。
    """
    if not value:
        return ""
    for quoted in re.findall(r"""['"]([^'"]+)['"]""", value):
        s = quoted.strip()
        if not s or s.startswith(("#", "javascript:", "mailto:", "void")):
            continue
        if re.search(r"(?:^https?://|/|\.s?html?$|\.jsp$|\.aspx?$|\.do$|\.php$)", s, re.I):
            return s
    return ""


def anchor_target(a) -> str:
    """取一个 <a> 的跳转目标：优先 href，href 缺失或是 javascript: 伪链接时回退 onclick。"""
    raw = (a.get("href") or "").strip()
    if not raw or raw.startswith(("#", "javascript:", "mailto:")):
        raw = url_from_onclick(a.get("onclick") or "")
    return raw


def allowed_url(url: str, src: SourceConfig) -> bool:
    p = urllib.parse.urlsplit(url)
    origin = urllib.parse.urlsplit(src.list_url)
    if p.scheme == "file":
        return origin.scheme == "file"
    if p.scheme not in ("http", "https") or not p.hostname or p.username:
        return False
    hosts = src.allowed_hosts or ([origin.hostname] if origin.hostname else [])
    if hosts and p.hostname not in hosts:
        return False
    if src.detail_url_pattern and not re.search(src.detail_url_pattern, url):
        return False
    if src.include and not any(k in url for k in src.include):
        return False
    if src.exclude and any(k in url for k in src.exclude):
        return False
    return not re.search(r"\.(jpg|jpeg|png|gif|css|js|svg)$", p.path, re.I)


def list_page_url(src: SourceConfig, page: int) -> str:
    if src.pagination == "trs":
        base = urllib.parse.urljoin(src.list_url, "./")
        return src.list_url if page == 1 else urllib.parse.urljoin(base, f"index_{page-1}.html")
    if "{page}" in src.list_url:
        return src.list_url.format(page=page, page0=page-1)
    if "{page0}" in src.list_url:
        return src.list_url.format(page0=page-1)
    return src.list_url


class Collector:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers["User-Agent"] = cfg.fetch.user_agent
        self._last_request = 0.0
        self.discovery_status = {}
        # 动态防护（瑞数等）的握手结果：host -> {"cookies": {...}, "at": 单调时钟}
        self._handshakes: dict[str, dict] = {}
        self._handshake_entry: dict[str, str] = {}
        # 便于测试替换浏览器实现，默认用 browser_session.browser_cookies
        self.handshake_fn = None
        # 整站只能走浏览器通道的主机（见 ensure_browser）：cookie 交给 requests
        # 会被拒的站，列表/详情/附件都必须经浏览器网络栈取。
        self.browser_fetch_hosts: set[str] = set()
        self.browser_headless = True
        self._gateway = None

    def close(self):
        self.session.close()
        if self._gateway is not None:
            self._gateway.close()
            self._gateway = None

    # ---------- 整站浏览器通道（cookie 无法复用的站） ----------
    def ensure_browser(self, entry_url: str, headless: bool = True):
        """把某主机登记为"必须走浏览器"，并就地过一次挑战。

        与 `ensure_handshake` 的区别：握手是"浏览器换 cookie，之后 requests 复用"；
        本通道是"cookie 只在浏览器网络栈里有效"，所以**不提 cookie**，
        所有请求都交给同一个浏览器上下文（见 BrowserGateway 的实测记录）。
        """
        host = urllib.parse.urlsplit(entry_url).hostname or ""
        if not host:
            return False
        self.browser_fetch_hosts.add(host)
        self.browser_headless = headless
        # 记下挑战入口：浏览器通道也要先用它过一次防护，再取具体页面。
        self._handshake_entry[host] = entry_url
        gateway = self._browser_gateway()
        return gateway.open(entry_url)

    def _browser_gateway(self):
        if self._gateway is None:
            from .browser_session import BrowserGateway
            self._gateway = BrowserGateway(headless=self.browser_headless)
        return self._gateway

    def _fetch_via_browser(self, url: str, target: str) -> FetchResult:
        """经浏览器通道取内容；非 2xx 一律如实回报，不重试——重试也过不去。"""
        try:
            status, body, ctype = self._browser_gateway().get(
                target, entry_url=self._handshake_entry.get(
                    urllib.parse.urlsplit(target).hostname or "", target))
        except Exception as e:
            if type(e).__name__ == "BrowserUnavailable":
                raise
            return FetchResult(ok=False, url=url, error=f"{type(e).__name__}: {str(e)[:160]}")
        if status != 200:
            return FetchResult(ok=False, url=url, error=f"HTTP {status}")
        if len(body) > self.cfg.fetch.max_page_bytes:
            return FetchResult(ok=False, url=url, error="超过单文件大小上限")
        return FetchResult(ok=True, url=url, final_url=target, content=body,
                           content_type=ctype, sha256=hashlib.sha256(body).hexdigest())

    # ---------- 动态防护（瑞数等）握手 ----------
    def ensure_handshake(self, entry_url: str, force: bool = False) -> bool:
        """过掉目标站的动态防护，把 cookie 装进 session；返回是否拿到。

        湖北 `fgw.hubei.gov.cn` 这类站对每个请求都返回 412，且伪造完整浏览器头、
        换 UA、手动三跳握手都无效——判定点在 TLS 指纹层。只有真实浏览器拿得到 cookie。
        所以这里**只开一次浏览器**，之后列表页与详情页都走纯 HTTP，
        而不是每页都开一次 Chrome。
        """
        host = urllib.parse.urlsplit(entry_url).hostname or ""
        if not host:
            return False
        self._handshake_entry[host] = entry_url
        cached = self._handshakes.get(host)
        ttl = getattr(self.cfg.fetch, "handshake_ttl_seconds", 1200)
        if cached and not force and (time.monotonic() - cached["at"]) < ttl:
            self._apply_cookies(host, cached["cookies"])
            return True
        fn = self.handshake_fn
        if fn is None:
            from .browser_session import browser_cookies
            fn = browser_cookies
        cookies = fn(entry_url) or {}
        if not cookies:
            return False
        self._handshakes[host] = {"cookies": cookies, "at": time.monotonic()}
        self._apply_cookies(host, cookies)
        return True

    def _apply_cookies(self, host: str, cookies: dict) -> None:
        """只把 cookie 挂到该 host 上。

        不能写成 `jar.set(k, v)`：那样建出的是**无域** cookie，会被发往所有站点——
        不但可能被目标站判为异常，也等于把 A 站的会话凭据送给 B 站。
        """
        jar = self.session.cookies
        try:
            jar.clear(domain=host)
        except KeyError:
            pass
        for k, v in cookies.items():
            jar.set(k, v, domain=host, path="/")

    def _https_if_required(self, url: str) -> str:
        """已握手的站若走 https，就把同源 http 链接升级过去。

        湖北列表页里的 href 写死 `http://fgw.hubei.gov.cn/...`，而该站只在 https 上放行：
        同一个 URL 换成 https 是 200（93KB），保持 http 就是 400。
        不升级的表现很隐蔽——**列表能发现 1219 条，详情却一条也抓不回来**。
        只在"握手入口本身就是 https"时升级，避免把仅有 http 的站改坏。
        """
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "http" or parts.hostname not in self._handshakes:
            return url
        entry = self._handshake_entry.get(parts.hostname) or ""
        if urllib.parse.urlsplit(entry).scheme != "https":
            return url
        return urllib.parse.urlunsplit(
            ("https", parts.netloc, parts.path, parts.query, parts.fragment))

    def adopt_cookies(self, entry_url: str, cookies: dict) -> None:
        """接收一次**已经发生过**的握手结果。

        列表页本身要浏览器渲染时，同一次会话已经过了防护，cookie 顺手带回来即可，
        不必为了详情页再开一次浏览器。
        """
        host = urllib.parse.urlsplit(entry_url).hostname or ""
        if not host or not cookies:
            return
        self._handshake_entry[host] = entry_url
        self._handshakes[host] = {"cookies": cookies, "at": time.monotonic()}
        self._apply_cookies(host, cookies)

    @property
    def handshaked_hosts(self) -> list:
        return sorted(self._handshakes)

    def fetch(self, url: str, timeout: Optional[int] = None, params: Optional[dict] = None,
              form: Optional[dict] = None) -> FetchResult:
        """取一个 URL。`form` 非空则改用 POST（表单编码）——部分政府站把列表放在
        POST 接口后面（如江西 CMS 的 /queryList），GET 会 404。"""
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            return FetchResult(ok=False, url=url, error="仅允许 HTTP/HTTPS 下载")
        target = self._https_if_required(url)
        host = urllib.parse.urlsplit(target).hostname or ""
        # 整站走浏览器的站：**不走下面的 requests 重试逻辑**——失败不是超时或抖动，
        # 而是被防护拒了，重试只会白等。间隔仍按"需要客气对待的站"处理。
        if host in self.browser_fetch_hosts:
            wait = max(self.cfg.fetch.request_interval_seconds, 2.5) - (
                time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            return self._fetch_via_browser(url, target)
        last_err = ""
        for attempt in range(self.cfg.fetch.retries + 1):
            # 需要过动态防护的站，请求间隔**自动放慢**。实测湖北：按 0.5 秒连抓 1200+
            # 条后触发风控——全站返回 412（300 条重试全军覆没、0.5 秒/条即失败），
            # 而间隔 3 秒时 8/8 立刻恢复。这类站本来就是"需要客气对待"的站，
            # 所以不必单独配置：只要过了握手，就按更保守的间隔走。
            interval = self.cfg.fetch.request_interval_seconds
            if host in self._handshakes:
                interval = max(interval, 2.5)
            wait = interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                send = self.session.post if form else self.session.get
                kwargs = {"data": form} if form else {"params": params}
                with send(target, timeout=timeout or self.cfg.fetch.timeout_seconds,
                          verify=self.cfg.fetch.verify_ssl, stream=True, **kwargs) as resp:
                    # 412 = 动态防护挑战，多半是握手 cookie 过期了：重握一次再试。
                    # 只在本轮**第一次**尝试时刷新，免得把"站点真的不可达"变成无限重试。
                    if (resp.status_code == 412 and attempt == 0
                            and host in self._handshake_entry
                            and self.ensure_handshake(self._handshake_entry[host], force=True)):
                        continue
                    # Authentication / blocking / missing pages require source maintenance, not aggressive retries.
                    if resp.status_code in (401, 403, 404, 412):
                        return FetchResult(ok=False, url=url, error=f"HTTP {resp.status_code}")
                    resp.raise_for_status()
                    chunks, size = [], 0
                    for chunk in resp.iter_content(65536):
                        size += len(chunk)
                        if size > self.cfg.fetch.max_page_bytes:
                            return FetchResult(ok=False, url=url, error="超过单文件大小上限")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    return FetchResult(ok=True, url=url, final_url=resp.url, content=data,
                                       content_type=resp.headers.get("Content-Type", ""),
                                       sha256=hashlib.sha256(data).hexdigest())
            except requests.RequestException as e:
                # 记**完整信息**，不只异常类型名。湖北那次 293 条失败只留下 "ProxyError"
                # 三个字，复现时手工请求却是 200——查不出到底是超时、连接重置还是代理。
                last_err = f"{type(e).__name__}: {str(e)[:180]}"
                if attempt < self.cfg.fetch.retries:
                    time.sleep(min(10, self.cfg.fetch.retry_backoff_seconds * (attempt + 1)))
        return FetchResult(ok=False, url=url, error=last_err)

    def save(self, source_name: str, url: str, content: bytes, ext: str = "html") -> str:
        folder = self.cfg.downloads_dir / re.sub(r"[^\w.-]", "_", source_name)
        folder.mkdir(parents=True, exist_ok=True)
        suffix = re.sub(r"[^a-z0-9]", "", ext.lower())[:10] or "bin"
        path = folder / f"{hashlib.sha256(content).hexdigest()}.{suffix}"
        if not path.exists():
            path.write_bytes(content)
        # Absolute paths work with POLICY_DATA_DIR outside the checkout, too.
        return str(path.resolve())

    def is_policy_url(self, url: str, src: SourceConfig) -> bool:
        return allowed_url(url, src)


class ListPageParser:
    def __init__(self, src: SourceConfig):
        self.src = src

    def parse(self, html: str | bytes, base_url: str = "") -> list[CandidateLink]:
        soup = BeautifulSoup(html, "lxml")
        # Some public CMS list fragments are embedded as inert CDATA/comments
        # (e.g. Jiangsu jpage: HTML carries the first page as XML <record><![CDATA[…]]>).
        from bs4 import CData, Comment
        for block in soup.find_all(string=lambda x: isinstance(x, (Comment, CData))):
            if "<a " in str(block):
                block.replace_with(BeautifulSoup(str(block), "lxml"))
        selector = self.src.link_selector
        anchors = soup.select(selector) if selector else soup.find_all("a")
        # TRS jpage (Jiangsu/…): 首屏列表以 <script> 内嵌 <datastore><recordset> XML 序列化，
        # CDATA 记录内是 <li><a …>；剥掉 CDATA 标记后与页面 <a> 合并解析。
        for tag in soup.find_all("script"):
            txt = tag.string or ""
            if "<recordset>" not in txt or "<a " not in txt:
                continue
            frag = BeautifulSoup(re.sub(r"<!\[CDATA\[|\]\]>", "", txt), "lxml")
            anchors += frag.select(selector) if selector else frag.find_all("a")
        links, seen = [], set()
        for a in anchors:
            href = anchor_target(a)
            if not href:
                continue
            href = normalized_url(urllib.parse.urljoin(base_url or self.src.list_url, href))
            if href in seen or not self._match(href):
                continue
            seen.add(href)
            title = a.get("title") or " ".join(a.get_text(" ", strip=True).split())
            links.append(CandidateLink(href, title[:300]))
        return links

    def _match(self, url: str) -> bool:
        return allowed_url(url, self.src)


def parse_gov_feed(raw: bytes, src: SourceConfig) -> list[CandidateLink]:
    """中国政府网公开栏目 JSON；字段与栏目网页自身脚本一致。"""
    import json
    rows = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(rows, list):
        raise ValueError('政府网公开列表结构变化：预期 JSON 数组')
    links, seen = [], set()
    for row in rows[:src.max_pages * 20]:
        if not isinstance(row, dict): continue
        url = normalized_url(urllib.parse.urljoin(src.list_url, str(row.get('URL',''))))
        if url not in seen and allowed_url(url, src):
            seen.add(url)
            links.append(CandidateLink(url, str(row.get('TITLE',''))))
    return links


# ---- 浙江系政府站"页面构建单元"动态列表（hanweb AuthorizedRead/unitbuild.js）----
# 栏目页不含列表，通过 <script ... unitbuild.js url="..." queryData="..."> 声明构建参数；
# 前端 GET url?queryData 得到 {"data":{"html":"<ul>…政策列表…</ul>"}}。不执行网页脚本。

def extract_unitbuild_spec(page_html: str) -> tuple[str, dict] | None:
    """从栏目页提取 (接口相对/绝对 URL, queryData 参数 dict)。未命中返回 None。"""
    candidates = []
    for tag in BeautifulSoup(page_html, "lxml").find_all("script"):
        if "AuthorizedRead/unitbuild.js" not in tag.get("src", ""):
            continue
        value = tag.get("querydata", "")
        try:
            try:
                params = json.loads(value)
            except json.JSONDecodeError:
                params = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            continue
        if isinstance(params, dict) and tag.get("url"):
            candidates.append((tag["url"], params))
    # 同页可能还有导航/页脚构建单元，优先信息列表。
    return next((x for x in candidates if x[1].get("tagId") == "信息列表"),
                candidates[0] if len(candidates) == 1 else None)


class DiscoveryError(ValueError):
    """列表未完整采完；此前成功页的候选仍可入队。"""

    def __init__(self, message: str, links: list[CandidateLink]):
        super().__init__(message)
        self.links = list(links)


def unit_list_links(list_html: str, src: SourceConfig, base_url: str = "") -> list[CandidateLink]:
    """从单元构建返回的 data.html 提取详情链接；标题优先取 <a title> 完整属性。"""
    soup = BeautifulSoup(list_html, "lxml")
    links, seen = [], set()
    for a in soup.find_all("a"):
        href = anchor_target(a)
        if not href:
            continue
        href = normalized_url(urllib.parse.urljoin(base_url or src.list_url, href))
        if href in seen or not allowed_url(href, src):
            continue
        seen.add(href)
        title = a.get("title") or a.get_text(" ", strip=True) or ""
        links.append(CandidateLink(href, " ".join(title.split())[:300]))
    return links


def discover_zj_unit_links(collector: Collector, src: SourceConfig, page_raw: bytes,
                           page_url: str, max_pages: int = 1, page_size: int = 15) -> list[CandidateLink]:
    """浙江系 unitbuild 两跳发现：栏目页 → 单元构建接口(data.html) → 详情链接。

    翻页：每页带 paramJson={"pageNo":N,"pageSize":15,"search":""}（与 unitbuild.js 的
    paramsMap 分支同构），空页自动停止；接口 JSON 原件逐页落盘留证。
    """
    collector.discovery_status[src.name] = {'pages_fetched': 0, 'end_reached': False, 'stop_reason': 'page_limit'}
    spec = extract_unitbuild_spec(page_raw.decode("utf-8", "ignore"))
    if spec is None:
        raise ValueError("栏目页未发现 unitbuild 构建参数（结构变化或反爬页）")
    api_url, base_params = spec
    api_url = urllib.parse.urljoin(page_url, api_url)
    endpoint, origin = urllib.parse.urlsplit(api_url), urllib.parse.urlsplit(page_url)
    if (endpoint.scheme, endpoint.netloc) != (origin.scheme, origin.netloc) or not endpoint.path.startswith("/api-gateway/"):
        raise ValueError("列表构建接口不在官网同源公开网关内")
    links, seen = [], set()
    for page_no in range(1, max_pages + 1):
        params = dict(base_params)
        params["paramJson"] = json.dumps(
            {"pageNo": page_no, "pageSize": page_size, "search": ""},
            ensure_ascii=False)
        result = collector.fetch(api_url, params=params)
        if not result.ok:
            raise DiscoveryError(f"单元构建接口第{page_no}页请求失败: {result.error}", links)
        collector.save(src.name, api_url, result.content, "json")
        collector.discovery_status[src.name]['pages_fetched'] += 1
        try:
            payload = json.loads(result.content.decode("utf-8", "ignore"))
        except json.JSONDecodeError as e:
            raise DiscoveryError(f"单元构建接口第{page_no}页返回非 JSON", links) from e
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise DiscoveryError(f"单元构建接口第{page_no}页返回失败或结构变化", links)
        data = payload.get("data") or {}
        list_html = data.get("html", "") if isinstance(data, dict) else ""
        if not isinstance(list_html, str) or not list_html:
            raise DiscoveryError("单元构建返回为空（列表未生成）", links)
        page_links = unit_list_links(list_html, src, base_url=page_url)
        fresh = [l for l in page_links if l.url not in seen]
        for l in fresh:
            seen.add(l.url)
        links.extend(fresh)
        if not page_links:      # 空页：翻页到底
            collector.discovery_status[src.name]['end_reached'] = True
            collector.discovery_status[src.name]['stop_reason'] = 'empty_page'
            break
        if not fresh:           # 重复页不等于已采完，提示翻页参数/接口需维护。
            raise DiscoveryError(f"第{page_no}页完全重复，未确认历史列表采完", links)
    return links


# ---- 江西系政府站 CMS：栏目页声明 channelId，列表由 POST /queryList 返回 ----
# 栏目页静态 HTML 里没有一条文章链接（页面自报 articleCount:200、childCount:0），
# 列表与**正文**都在同一份 JSON 里：data.results[].source.{title,pubDate,content.content,id}。
# 因此这是"接口型列表"，不是"需要 JS 渲染"——渲染能拿到链接，但接口给得更完整。
_JX_CHANNEL_RE = re.compile(r"channelId\s*=\s*['\"](\d+)")
_JX_WEBSITE_RE = re.compile(r"websiteId\s*=\s*['\"](\d+)")
_JX_SITE_RE = re.compile(r"siteId\s*=\s*['\"](\d+)")
_JX_CODE_RE = re.compile(r"codeName\s*=\s*['\"](\w+)")


def extract_jx_query_spec(page_html: str, page_url: str) -> dict | None:
    """从栏目页内联脚本里取 queryList 的调用参数（取不到就说明结构变了）。"""
    ch = _JX_CHANNEL_RE.search(page_html)
    if not ch:
        return None
    site = _JX_SITE_RE.search(page_html)
    web = _JX_WEBSITE_RE.search(page_html)
    code = _JX_CODE_RE.search(page_html)
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(page_url))
    return {
        "endpoint": origin + "/queryList",
        "column_path": code.group(1) if code else "",
        "channelId": ch.group(1),
        "siteId": site.group(1) if site else "",
        "websiteId": web.group(1) if web else "",
    }


def discover_jx_query_links(collector: Collector, src: SourceConfig, page_raw: bytes,
                            page_url: str, max_pages: int = 1,
                            page_size: int = 15) -> list[CandidateLink]:
    """江西 CMS 列表发现：栏目页 → POST /queryList（逐页）→ 详情链接。

    与 zj_unit 的区别：这里必须 POST（GET 该端点直接 404），所以用 `collector.fetch(form=…)`。
    详情 URL 由栏目名 + 文章 id 拼出：`/{websiteCode}/col/{codeName}/content/content_{id}.html`。
    """
    collector.discovery_status[src.name] = {'pages_fetched': 0, 'end_reached': False,
                                            'stop_reason': 'page_limit'}
    spec = extract_jx_query_spec(page_raw.decode("utf-8", "ignore"), page_url)
    if spec is None:
        raise ValueError("栏目页未发现 channelId（结构变化或反爬页）")
    endpoint, origin = urllib.parse.urlsplit(spec["endpoint"]), urllib.parse.urlsplit(page_url)
    if (endpoint.scheme, endpoint.netloc) != (origin.scheme, origin.netloc):
        raise ValueError("列表接口不在官网同源内")
    prefix = "/" + (urllib.parse.urlsplit(page_url).path.split("/")[1] or "")
    if spec["column_path"]:
        prefix += "/col/" + spec["column_path"]
    links, seen = [], set()
    for page_no in range(1, max_pages + 1):
        form = {
            "channelId": spec["channelId"], "siteId": spec["siteId"],
            "websiteId": spec["websiteId"], "pageNo": page_no, "pageSize": page_size,
            "perPage": page_size, "showMode": "1", "themeName": "default",
        }
        result = collector.fetch(spec["endpoint"], form=form)
        if not result.ok:
            raise DiscoveryError(f"queryList 第{page_no}页请求失败: {result.error}", links)
        collector.save(src.name, spec["endpoint"], result.content, "json")
        collector.discovery_status[src.name]['pages_fetched'] += 1
        try:
            payload = json.loads(result.content.decode("utf-8", "ignore"))
        except json.JSONDecodeError as e:
            raise DiscoveryError(f"queryList 第{page_no}页返回非 JSON", links) from e
        rows = ((payload or {}).get("data") or {}).get("results") or []
        if not rows:
            collector.discovery_status[src.name]['end_reached'] = True
            collector.discovery_status[src.name]['stop_reason'] = 'empty_page'
            break
        fresh = []
        for row in rows:
            item = row.get("source") if isinstance(row, dict) else None
            if not isinstance(item, dict):
                continue
            aid = str(item.get("id") or "").strip()
            if not aid:
                continue
            href = urllib.parse.urljoin(page_url,
                                        f"{prefix}/content/content_{aid}.html")
            if href in seen or not allowed_url(href, src):
                continue
            seen.add(href)
            title = " ".join(str(item.get("title") or "").split())
            fresh.append(CandidateLink(href, title[:300]))
        links.extend(fresh)
        if not fresh:
            raise DiscoveryError(f"queryList 第{page_no}页无新增（过滤规则或接口结构变化）", links)
    return links


def discover_browser_links(collector: Collector, src: SourceConfig, page_url: str,
                           include: Optional[list] = None) -> list:
    """列表数据由 JS 异步加载时，用真实浏览器渲染后取条目。

    顺带把这次会话的 cookie 交给 collector：同一站多半也挂了动态防护，
    详情页随后就能走纯 HTTP（实测湖北正是如此——**列表要渲染，详情只要 cookie**）。
    """
    from .browser_session import browser_list_and_cookies
    entry = src.handshake or page_url
    keys = include if include is not None else (src.include or None)
    cookies, raw = browser_list_and_cookies(entry, page_url, include=keys)
    if cookies:
        collector.adopt_cookies(entry, cookies)
    out, seen = [], set()
    for href, title in raw:
        if not href or href.startswith("javascript"):
            continue
        url = href.split("#")[0]
        if url in seen:
            continue
        seen.add(url)
        out.append(CandidateLink(url=url, title=(title or "").strip()))
    return out


def next_page_link(raw: bytes, page_url: str) -> str:
    """Follow only an explicit same-origin next-page link, never guessed URLs."""
    soup = BeautifulSoup(raw, 'lxml')
    # Yunnan's public template encodes only the pager HTML; decode data, never JS.
    pager = soup.select_one('#pages')
    if pager and not pager.find('a') and 'window.atob' in raw.decode('utf-8', 'ignore'):
        import base64
        try:
            fragment = urllib.parse.unquote_plus(base64.b64decode(pager.get_text(strip=True), validate=True).decode('utf-8'))
            pager.replace_with(BeautifulSoup(fragment, 'lxml'))
        except (ValueError, UnicodeError):
            pass
    for a in soup.find_all('a'):
        label = a.get_text(strip=True)
        title = (a.get('title') or '').strip()
        if ('next' not in a.get('rel', []) and label not in ('下一页', '下页', '下一頁', 'Next', 'next', '>')
                and title not in ('下一页', '下页', '下一頁', 'Next', 'next')):
            continue
        # 链接可能在 onclick 里而不在 href（实测辽宁慧点 CMS 的"下一页"就是这样：
        # `<a title="下一页" onclick="queryArticleByCondition(this,'…-2.shtml')">`）。
        href = anchor_target(a)
        if not href:
            continue
        url = normalized_url(urllib.parse.urljoin(page_url, href))
        p, origin = urllib.parse.urlsplit(url), urllib.parse.urlsplit(page_url)
        if p.scheme in ('http', 'https') and p.netloc == origin.netloc and url != normalized_url(page_url):
            return url
    return ''


def _jpage_group(raw: bytes, src: SourceConfig, base_url: str):
    """Use the first policy-bearing datastore (desktop/mobile often duplicate it)."""
    text = raw.decode('utf-8-sig', 'replace')
    groups = re.findall(r'<datastore\b[^>]*>.*?</datastore>', text, re.S | re.I)
    if not groups:
        raise ValueError('jpage响应缺少datastore，可能为验证页或结构变化')
    for group in groups:
        links = ListPageParser(src).parse(re.sub(r"<!\[CDATA\[|\]\]>", "", group), base_url)
        if not links:
            continue
        match = re.search(r'<nextgroup\b[^>]*>(.*?)</nextgroup>', group, re.S | re.I)
        next_url = ''
        if match:
            fragment = re.sub(r'<!\[CDATA\[|\]\]>', '', match.group(1))
            anchor = BeautifulSoup(fragment, 'lxml').find('a', href=True)
            if anchor and anchor['href'].strip():
                next_url = urllib.parse.urljoin(base_url, anchor['href'])
        return links, next_url
    # An explicit empty recordset is distinguishable from non-policy records.
    if re.search(r'<record\b', text, re.I):
        raise ValueError('jpage有记录但无符合范围的详情，需检查过滤规则')
    return [], ''


def discover_jpage_links(collector: Collector, src: SourceConfig, raw: bytes,
                         page_url: str, max_pages: int) -> list[CandidateLink]:
    status = {'pages_fetched': 0, 'end_reached': False, 'stop_reason': 'page_limit'}
    collector.discovery_status[src.name] = status
    links, seen, seen_urls = [], set(), {normalized_url(page_url)}
    current = page_url
    for page in range(1, max_pages + 1):
        try:
            found, next_url = _jpage_group(raw, src, current)
        except ValueError as exc:
            raise DiscoveryError(str(exc), links) from exc
        status['pages_fetched'] += 1
        fresh = [x for x in found if x.url not in seen]
        if found and not fresh:
            raise DiscoveryError('jpage分组完全重复，翻页未完成', links)
        links.extend(fresh); seen.update(x.url for x in fresh)
        if not next_url:
            status.update(end_reached=not found, stop_reason='empty_recordset' if not found else 'no_next_group')
            return links
        endpoint, origin = urllib.parse.urlsplit(next_url), urllib.parse.urlsplit(page_url)
        if endpoint.scheme not in ('http','https') or endpoint.netloc != origin.netloc or endpoint.path != '/module/web/jpage/dataproxy.jsp':
            raise DiscoveryError('jpage下一组不在官网同源公开dataproxy接口', links)
        if normalized_url(next_url) in seen_urls:
            raise DiscoveryError('jpage下一组网址循环，未确认采完', links)
        if page == max_pages:
            break
        seen_urls.add(normalized_url(next_url))
        result = collector.fetch(next_url)
        if not result.ok:
            raise DiscoveryError(f'jpage第{page+1}组请求失败: {result.error}', links)
        collector.save(src.name, next_url, result.content, 'xml')
        raw, current = result.content, next_url
    return links
