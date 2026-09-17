"""用真实浏览器换取会话 cookie——过掉瑞数（Riversafe）这类动态防护。

背景：湖北 `fgw.hubei.gov.cn` 对**每一个**请求都返回 412，且实测
"伪造完整浏览器头 / 换 Googlebot UA / 手动三跳握手 / 深层路径"**全部无效**——
它的判定点在 TLS 指纹层，纯 HTTP 客户端（requests / urllib / curl）过不去。
甘肃 `www.gansu.gov.cn` 更严：JS 挑战能跑（JS 文件 200），但重放被拒 400。

但**真实浏览器能过**（湖北已实测）。于是采用"一次握手 + 全速抓取"：

    浏览器过挑战拿到 cookie  →  交给 requests 复用  →  列表/详情全走纯 HTTP

每站只开一次浏览器，而不是每页都开——否则采集 30 条就要开 30 次 Chrome。
cookie 有时效（瑞数通常几十分钟），过期表现为重新 412，届时再握一次。
"""
from __future__ import annotations

import re
import time
import urllib.parse

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 瑞数挑战页的两个特征：<meta content="..." r="m"> 与 r='m' 的内联脚本
_CHALLENGE = re.compile(r"""r=['"]m['"]""")

# headless 下最容易被识别的几处指纹
_STEALTH = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh','en']});
window.chrome={runtime:{},loadTimes:()=>({}),csi:()=>({})};
"""


class BrowserUnavailable(RuntimeError):
    """没能起浏览器——通常是没装 playwright。"""


def browser_cookies(entry_url: str, timeout_seconds: int = 45,
                    min_bytes: int = 4000, headless: bool = True) -> dict:
    """打开 entry_url、等动态防护放行，返回该站 cookie。

    返回 `{}` 表示**没拿到**——调用方应据此判定"该站接不通"并如实上报，
    而不是当成一次普通失败反复重试。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 取决于本机环境
        raise BrowserUnavailable(
            "该来源需要过动态防护，请先安装 playwright：pip install playwright"
            "（本机已有 Chrome 时无需再下载浏览器）") from exc

    deadline = time.monotonic() + timeout_seconds
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=headless,
                                     args=["--disable-blink-features=AutomationControlled"])
        try:
            ctx = browser.new_context(locale="zh-CN", user_agent=UA,
                                      viewport={"width": 1440, "height": 900})
            ctx.add_init_script(_STEALTH)
            page = ctx.new_page()
            try:
                page.goto(entry_url, wait_until="domcontentloaded",
                          timeout=timeout_seconds * 1000)
            except Exception:
                # 挑战页会自行重载，导航异常不代表失败——下面只看内容有没有放行
                pass
            while time.monotonic() < deadline:
                try:
                    html = page.content()
                except Exception:
                    html = ""
                if len(html) >= min_bytes and not _CHALLENGE.search(html[:3000]):
                    break
                page.wait_for_timeout(1500)
            return {c["name"]: c["value"] for c in ctx.cookies()}
        finally:
            browser.close()


def browser_list_and_cookies(entry_url: str, list_url: str, include: list | None = None,
                             timeout_seconds: int = 60) -> tuple:
    """一次浏览器会话：过动态防护 + 渲染列表页 + 提取条目链接。

    返回 `(cookies, [(url, title), ...])`。

    为什么列表也要用浏览器：湖北 `/fbjd/zc/zcwj/` 的纯 HTTP 响应里只有 1 条文章链接，
    浏览器渲染后是 1833 条——条目由 JS 异步填充，HTTP 客户端拿不到。
    而详情页本身是静态的，所以拿到 cookie 后一律走 requests，不必每页都开浏览器。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 取决于本机环境
        raise BrowserUnavailable(
            "该来源需要过动态防护，请先安装 playwright：pip install playwright"
            "（本机已有 Chrome 时无需再下载浏览器）") from exc

    deadline = time.monotonic() + timeout_seconds
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True,
                                     args=["--disable-blink-features=AutomationControlled"])
        try:
            ctx = browser.new_context(locale="zh-CN", user_agent=UA,
                                      viewport={"width": 1440, "height": 900})
            ctx.add_init_script(_STEALTH)
            page = ctx.new_page()
            for target in (entry_url, list_url):      # 先过防护，再进列表页
                try:
                    page.goto(target, wait_until="domcontentloaded",
                              timeout=timeout_seconds * 1000)
                except Exception:
                    pass
                page.wait_for_timeout(2500)
            # 列表由 JS 填充：轮询到条目数连续两次相同（或超时）为止
            last, stable = -1, 0
            while time.monotonic() < deadline:
                n = page.eval_on_selector_all("a", "els=>els.length")
                if n == last:
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    stable, last = 0, n
                page.wait_for_timeout(1200)
            raw = page.eval_on_selector_all(
                "a", "els=>els.map(e=>[e.href, (e.textContent||'').trim()])")
            return {c["name"]: c["value"] for c in ctx.cookies()}, raw
        finally:
            browser.close()


class BrowserGateway:
    """常驻的"浏览器通道"：过一次挑战，之后列表/详情/附件都用它的请求上下文取。

    什么时候需要它——**cookie 交给纯 HTTP 客户端会被拒**的站。

    甘肃实测（2026-09-17）：
        - 直接请求            → 412（挑战页）
        - 握手拿到的 cookie 交给 requests → **400**（挑战令牌与浏览器指纹绑定，
          补齐 Accept / Sec-Fetch-* / br 编码等请求头也无效）
        - 同一 cookie 交给浏览器上下文的 request 通道 → **200**
        - 且 headless 会被识别（挑战后重放 400），必须 `headless=False`

    所以这类站不能走"握手 + requests"，只能整站走浏览器。
    好消息是列表页原始 HTML 是静态的（甘肃通知公告 21 条、政策 19 条都在源码里），
    不必用 JS 渲染——通道只负责过防护，解析仍交给通用的 ListPageParser。

    只开一次浏览器：一次运行里列表、详情、附件共用同一个上下文，
    而不是每页开一次 Chrome（启动一次约 2 秒，20 条就多花 40 秒）。
    ⚠️ 本通道**不注入 `_STEALTH`**，这是实测结论、不是遗漏。

    三组对照（2026-09-17，同一台机、同一个浏览器版本，只改一个变量）：

    | 条件 | 首页渲染 | 请求通道 |
    | --- | --- | --- |
    | headless | 39 字节 | 400 |
    | headless + stealth | 39 字节 | 400 |
    | **非 headless，不注入 stealth** | **69464 字节 / 415 个链接** | **200** |
    | 非 headless + stealth | 39 字节 | 400 |

    也就是说**伪装脚本本身就是指纹**：`Object.defineProperty(navigator,'webdriver',…)`
    与手写的 `window.chrome={runtime:{},loadTimes:()=>({}),csi:()=>({})}`
    恰恰是自动化工具的典型特征，反而比"什么都不改"更容易被认出来。
    `--disable-blink-features=AutomationControlled` 已经用官方方式让 webdriver 消失，
    不需要再补一层。

    （湖北那边 `browser_cookies` 仍带 `_STEALTH` 且能用——**不要跟着一起改**，
    没坏的东西不动。两站防护规则不同是常态。）

    ⚠️ 浏览器会**中途死掉**：非 headless 的 Chrome 窗口被系统回收、被误关、
    或被同机另一个 Chrome 实例挤掉时，`self._ctx` 引用还在但每个调用都抛
    `TargetClosedError`。所以存活判定一律走 `_alive()`（查 `page.is_closed()`），
    不用 `is not None`；`open()` / `get()` 各自"重建 + 重试一次"。

    ⚠️ 但**不能把所有 `content()` 异常都当死亡**（2026-09-17 踩过）：
    挑战页自我重载的瞬间会抛 `Unable to retrieve content because the page is
    navigating and changing the content`，实测 0.2s 抛、1.4s 后正常返回 69464 字节。
    "一律不吞"会把这种瞬时重载变成整轮采集直接 failed。现在只吞这类瞬时异常，
    真死亡由 `_alive()` 和 `wait_for_timeout` 抛出。

    ⚠️ 就绪判据**只看页面长度**（挑战页 39 字节 / 真实页 34~69KB）。
    早先叠加了"且不含 `r='m'`"，而 `_CHALLENGE` 会在真实首页前 3000 字符的内联 JS 上
    误命中 → 每站白等满 30 秒超时才放行（症状是"慢"而不是"错"，最容易放过）。
    """

    def __init__(self, headless: bool = True, timeout_seconds: int = 30,
                 min_bytes: int = 4000):
        self._headless = headless
        self._timeout = timeout_seconds
        self._min_bytes = min_bytes
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._passed: set = set()

    # ---- 浏览器存活 ------------------------------------------------

    def _alive(self) -> bool:
        """上下文是否还能用。**不能靠 `self._ctx is not None` 判断**——
        浏览器被外部关掉 / 崩掉时引用还在，但每次调用都会抛 TargetClosedError。"""
        if self._ctx is None or self._page is None:
            return False
        try:
            return not self._page.is_closed()
        except Exception:
            return False

    def _reset(self) -> None:
        """丢弃当前浏览器，下次 `_ensure()` 会重建。已过挑战的 host 记录一并清掉
        ——新浏览器的 cookie 是新的，必须重新过挑战。"""
        self.close()
        self._passed.clear()

    def _ensure(self):
        if self._ctx is not None and self._alive():
            return
        if self._ctx is not None:     # 引用在、进程没了：先清干净再重建
            self._reset()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 取决于本机环境
            raise BrowserUnavailable(
                "该来源需要过动态防护，请先安装 playwright：pip install playwright"
                "（本机已有 Chrome 时无需再下载浏览器）") from exc
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            channel="chrome", headless=self._headless,
            args=["--disable-blink-features=AutomationControlled"])
        self._ctx = self._browser.new_context(locale="zh-CN", user_agent=UA,
                                              viewport={"width": 1440, "height": 900})
        # 刻意不 add_init_script(_STEALTH)：理由见类注释里的对照表。
        self._page = self._ctx.new_page()

    # ---- 过挑战 ----------------------------------------------------

    def open(self, entry_url: str, force: bool = False) -> bool:
        """过一次挑战。同一 host 只做一次；返回是否看起来放行了。

        浏览器中途死掉（长采集时 Chrome 被系统回收/窗口被关）会重建并重试一次，
        不让一次瞬时的浏览器死亡把整轮采集打断——但**重试后仍失败要如实抛错**，
        不能静默返回 False 冒充"这个站接不通"。
        """
        host = urllib.parse.urlsplit(entry_url).hostname or ""
        if not host:
            return False
        if host in self._passed and not force:
            return True
        last: Exception | None = None
        for attempt in range(2):
            try:
                self._ensure()
                return self._open_once(entry_url, host)
            except BrowserUnavailable:
                raise
            except Exception as exc:      # 浏览器/上下文已死
                last = exc
                self._reset()
        raise RuntimeError(
            f"浏览器通道连续两次不可用（{type(last).__name__}: {last}）")

    def _open_once(self, entry_url: str, host: str) -> bool:
        deadline = time.monotonic() + self._timeout
        try:
            self._page.goto(entry_url, wait_until="domcontentloaded",
                            timeout=self._timeout * 1000)
        except Exception:
            pass                      # 挑战页会自行重载，导航异常不代表失败
        while time.monotonic() < deadline:
            if not self._alive():
                raise RuntimeError("页面在过挑战途中被关闭")
            try:
                html = self._page.content()
            except Exception as exc:
                # `Page.content: Unable to retrieve content because the page is
                # navigating and changing the content` 是**瞬时**的（实测 0.2s 抛、
                # 1.4s 后正常返回 69464 字节）——挑战页此刻正在自我重载。
                # 所以这里必须吞掉、等下一轮，不能当失败。
                # 真正的死亡（TargetClosedError）由上面 `_alive()` 与
                # `wait_for_timeout` 抛出，走 `open()` 的重建重试。
                html = ""
            # 就绪判据**只看长度**：挑战页 39 字节，真实页 34~69KB（实测）。
            # 早先还叠加了"不含 r='m' "，结果真实首页前 3000 字符里正好有这段
            # 内联 JS → 每站白等满 30 秒超时才继续（看着像慢，其实是误判）。
            if len(html) >= self._min_bytes:
                self._passed.add(host)
                return True
            self._page.wait_for_timeout(1200)
        return False

    def get(self, url: str, entry_url: str = "") -> tuple:
        """取一个 URL，返回 `(status, bytes, content_type)`。二进制安全（附件也走这里）。

        同样对"浏览器死了"做一次重建重试（附件多的站一次运行可能几分钟）。
        """
        host = urllib.parse.urlsplit(url).hostname or ""
        last: Exception | None = None
        for attempt in range(2):
            try:
                self._ensure()
                if host and host not in self._passed:
                    self.open(entry_url or url)
                resp = self._ctx.request.get(url, timeout=self._timeout * 1000)
                return resp.status, resp.body(), (resp.headers or {}).get("content-type", "")
            except BrowserUnavailable:
                raise
            except Exception as exc:
                last = exc
                self._reset()
        raise RuntimeError(
            f"浏览器通道请求连续两次失败（{type(last).__name__}: {last}）")

    def cookies(self) -> dict:
        return {c["name"]: c["value"] for c in self._ctx.cookies()} if self._ctx else {}

    def close(self) -> None:
        for closer in (lambda: self._browser and self._browser.close(),
                       lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        self._pw = self._browser = self._ctx = self._page = None
