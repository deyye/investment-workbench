"""省级发改委政策栏目探测：从官网首页定位"能持续发现政策文件"的栏目。

python scripts/discover_provinces.py [--output docs/province-discovery.json] [--deep]

设计要点（都是实测教训）：
1. **不强制 HTTPS**。实测 31 省里有 4 省（江西/山东/广西/青海）HTTPS 直接 SSLError，
   走 HTTP 正常；强制升级会把"环境问题"误判成"站点不可达"。实测可达数 23/31 → 28/31。
2. **超时 25 秒、重试 1 次**。政府站慢，10 秒 / 0 重试会大面积 ReadTimeout。
3. **按结构判栏目，不靠栏目名白名单**。各站叫法差异极大；改为抓候选页并统计
   其中的"文章型链接"数量，命中多的才是真列表页。
4. 只探测，**不自动启用**。启用前必须跑 audit_sources.py 验证列表/正文/附件/入库。
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bs4 import BeautifulSoup

from policy_collector.collector import Collector, anchor_target
from policy_collector.config import AppConfig

# 栏目名候选：宽松匹配（只要沾边就纳入候选，最终由结构评分决定）
COLUMN_HINT = re.compile(
    r"规范性文件|政策文件|政策法规|法规文件|政策发布|通知公告|其他文件|制度文件|"
    r"政策汇编|文件公告|政策公告|行政规范性|地方性法规|规章|政策|文件|公告|法规|"
    r"决策部署|规划|意见|办法")
# 明显不是政策栏目的导航噪音
NOISE = re.compile(
    r"解读|问答|一图读懂|图片|视频|新闻|动态|要闻|时政|下载|登录|注册|无障碍|"
    r"english|网站地图|联系我们|网站声明|友情链接|关于我们|版权|微博|微信|"
    r"智能问答|办事|服务|互动|访谈|征集|调查|检索|搜索|首页|返回")
# 文章型详情链接（政府站常见若干种；用结构特征而非站点定制）
ARTICLE_PATTERNS = (
    re.compile(r"/art_[0-9a-z_]+\.html?$", re.I),          # 浙江/江苏 art_xxx.html
    re.compile(r"/\d{6}/t\d{8}_\d+\.html?$", re.I),        # TRS：/202401/t20240115_123.html
    re.compile(r"/(20\d{2})[-/]?(\d{2})/t(20\d{2})\d{4}_\d+", re.I),
    re.compile(r"/content_\d+", re.I),                     # 政府网 content_123.htm
    re.compile(r"/html/20\d{2}/[a-z_0-9]+/\d+\.html?$", re.I),
    re.compile(r"/(20\d{2})/(\d{1,2})/(\d{1,2})/", re.I),
    re.compile(r"\d{8,}\.s?html?$", re.I),
    re.compile(r"/[a-z_]+\d{6,}\.s?html?$", re.I),
    # 2026-09-13 接入实测补充（这两类形态此前认不出，导致 3 个可用省份被误判为"需适配"）：
    re.compile(r"/(20\d{2})/(\d{1,2})[-/](\d{1,2})/", re.I),   # 河南 /2026/09-11/3418623.html
    # 上海 /fgw_jggl/20260828/hash.html：注意 20260828 是 **8** 位，
    # 写成 20\d{8}（10 位）会一条也匹配不上——日期段正则必须拿真实样本验过。
    re.compile(r"/20\d{6}/[0-9a-f]{8,}\.s?html?$", re.I),
    # 2026-09-14 实测补充：辽宁 /fgw/zc/zxzc/2026090912183160373/index.shtml
    # （长数字串目录 + index.shtml）。此前认不出，辽宁被误判为"列表 JS 渲染"。
    re.compile(r"/\d{10,}/index\.s?html?$", re.I),
    # 2026-09-14 深夜补充：`/YYYYMM/hash.shtml` —— **6 位**年月这一族。
    # 表里原有 `/20\d{6}/`（8 位 YYYYMMDD），于是 6 位的年月**一条都认不出**：
    #   新疆 /xjfgw/c108361/202509/<32位hash>.shtml
    #   海南 /sfgw/0400/202609/<32位hash>.shtml
    # 两家都被判成"0 篇文章型"，海南因此在 9/13 被记成"列表走 AJAX 的壳"。
    # ⚠️ 这是**同一类错误第二次**（上面那条注释记着一次"把 8 位写成 10 位"）：
    #    **日期段正则必须拿真实样本验过再写进判据**，不要凭印象写位数。
    re.compile(r"/20\d{4}/[0-9a-f]{8,}\.s?html?$", re.I),
)
DATE_RE = re.compile(r"20\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2}")
# 栏目页可能是"跳转桩"：整页只有一行 location.href 指向真正的栏目。
# 实测山东 /col/col91475/index.html 全文 1823 字节、零链接，就是这种占位页；
# 不跟跳会在"这个栏目是空的"上得出错误结论。
_JS_REDIRECT = re.compile(r"location\.href\s*=\s*[\"']([^\"']+)[\"']", re.I)


def js_redirect_target(html: str, base: str) -> str:
    """返回页面声明的 JS 跳转目标（相对地址按 base 解析）；无则空串。"""
    m = _JS_REDIRECT.search(html or "")
    if not m:
        return ""
    target = m.group(1).strip()
    if not target or target.lower().startswith(("javascript", "#")):
        return ""
    return urljoin(base, target)


def looks_like_article(url: str) -> bool:
    return any(p.search(url) for p in ARTICLE_PATTERNS)


def same_host(a: str, b: str) -> bool:
    return urlsplit(a).hostname == urlsplit(b).hostname


def make_collector(timeout: int = 25, retries: int = 1) -> Collector:
    cfg = AppConfig()
    cfg.fetch.timeout_seconds = timeout
    cfg.fetch.retries = retries
    cfg.fetch.request_interval_seconds = 0.3
    return Collector(cfg)


def candidate_columns(html: str, base: str) -> list[dict]:
    """首页里"像政策栏目"的同源链接，按提示词强度排序。"""
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        href = (a.get("href") or "").strip()
        if not text or len(text) > 30 or href.lower().startswith(("javascript", "mailto:")):
            continue
        url = urljoin(base, href)
        if urlsplit(url).scheme not in ("http", "https") or not same_host(url, base):
            continue
        if url.rstrip("/") == base.rstrip("/") or url in seen:
            continue
        if NOISE.search(text) or not COLUMN_HINT.search(text):
            continue
        seen.add(url)
        out.append({"title": text, "url": url})
    return out


def score_list_page(html: str, base: str) -> dict:
    """判断一个页面是不是"政策列表页"：统计其中的文章型同源链接。

    取链接必须走 anchor_target（href 优先、回退 onclick）：部分 CMS 不给 <a> 写 href，
    链接在 onclick 里（实测天津）。只按 href 统计会得到"零文章链接"，
    进而把这类页面误判成"列表由 JS 渲染"——实测天津就是这样被误判的。
    """
    soup = BeautifulSoup(html, "lxml")
    articles, dated = set(), 0
    for a in soup.find_all("a"):
        raw = anchor_target(a)
        if not raw:
            continue
        url = urljoin(base, raw)
        if urlsplit(url).scheme not in ("http", "https") or not same_host(url, base):
            continue
        if not looks_like_article(url):
            continue
        articles.add(url)
    text = soup.get_text(" ", strip=True)
    dated = len(DATE_RE.findall(text))
    next_page = bool(re.search(r"下一页|下页|next", text, re.I))
    return {"article_links": len(articles), "dates_on_page": dated, "has_next": next_page}


def probe(entry: dict, deep: bool, max_cols: int = 6) -> dict:
    collector = make_collector()
    try:
        home = entry["home_url"]            # 保持原始协议，不强制 https
        fetched = collector.fetch(home)
        if not fetched.ok:
            return {**entry, "status": "blocked", "error": fetched.error}
        final = fetched.final_url or home
        cols = candidate_columns(fetched.content.decode("utf-8", "ignore"), final)
        if not cols:
            return {**entry, "status": "needs_adapter", "final_url": final,
                    "home_sha256": fetched.sha256, "columns": [],
                    "error": "首页未发现政策类栏目链接（栏目可能在二级页或需渲染）"}
        if not deep:
            return {**entry, "status": "columns_found", "final_url": final,
                    "home_sha256": fetched.sha256, "columns": cols[:max_cols]}
        scored = []
        for c in cols[:max_cols]:
            got = collector.fetch(c["url"])
            if not got.ok:
                scored.append({**c, "article_links": 0, "error": got.error})
                continue
            page = got.content.decode("utf-8", "ignore")
            page_url = got.final_url or c["url"]
            # 跳转桩：跟一次，否则会把"占位页"误判成"空栏目"
            hop = js_redirect_target(page, page_url)
            if hop and not same_host(hop, final):
                hop = ""
            if hop:
                again = collector.fetch(hop)
                if again.ok:
                    page = again.content.decode("utf-8", "ignore")
                    page_url = again.final_url or hop
                    c = {**c, "redirected_to": page_url}
            s = score_list_page(page, page_url)
            scored.append({**c, **s})
        scored.sort(key=lambda x: (-x.get("article_links", 0), -x.get("dates_on_page", 0)))
        best = scored[0] if scored else {}
        n = best.get("article_links", 0)
        status = "connected_candidate" if n >= 8 else ("weak_candidate" if n >= 3 else "needs_adapter")
        return {**entry, "status": status, "final_url": final, "home_sha256": fetched.sha256,
                "columns": scored, "best_column": best.get("url", ""),
                "best_article_links": n}
    finally:
        collector.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--registry", default="config/provinces.json")
    ap.add_argument("--output", default="docs/province-discovery.json")
    ap.add_argument("--deep", action="store_true", default=True,
                    help="抓取候选栏目页并按结构评分（默认开）")
    ap.add_argument("--shallow", dest="deep", action="store_false")
    ap.add_argument("--workers", type=int, default=4)
    ns = ap.parse_args()

    entries = json.loads(Path(ns.registry).read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[1]
    registry = entries

    def run(e):
        try:
            r = probe(e, ns.deep)
        except Exception as exc:                                    # noqa: BLE001
            r = {**e, "status": "blocked", "error": f"{type(exc).__name__}: {exc}"}
        print(f"{e['region']:<12}{r['status']:<20}{r.get('best_article_links','')}", flush=True)
        return r

    with ThreadPoolExecutor(max_workers=ns.workers) as pool:
        results = list(pool.map(run, registry))
    out = Path(ns.output)
    if not out.is_absolute():
        out = root / out
    out.parent.mkdir(parents=True, exist_ok=True)
    from collections import Counter
    summary = dict(Counter(r["status"] for r in results))
    out.write_text(json.dumps({
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "probe": "保持原始协议、超时25s、重试1次、超时并发4；按结构（文章型链接数）判栏目，非栏目名白名单",
        "status_summary": summary,
        "note": "探测只用于定位候选栏目，不代表已接入；启用前须跑 audit_sources.py 验证列表/正文/附件/入库。",
        "regions": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n状态分布:", summary)
    print("写入", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
