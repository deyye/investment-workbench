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
