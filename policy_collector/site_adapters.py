"""站点适配器：列表页 + 详情页的站点差异统一收敛点。

两类差异分治：
1. 列表适配（发现候选详情链接）：默认「CSS 选择器 + include/exclude 过滤」即可；
   结构差异大的站点注册专属列表解析函数（见 register_list_parser）。
2. 详情适配（抽取正文/标题/日期/附件）：各官网正文容器、日期格式不一，
   按 URL 特征注册详情解析函数（见 register_detail_parser）。注册后 Parser
   解析 HTML 时自动优先调用，返回 None 则回退通用抽取。

站点接入状态见 config/sources.yaml 和 docs/VALIDATION.md。
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Optional

from bs4 import BeautifulSoup

from .collector import CandidateLink

# ---------------------------------------------------------------------------
# 列表页适配注册
# ---------------------------------------------------------------------------
_LIST_REGISTRY: dict[str, Callable] = {}


def register_list_parser(source_name: str):
    """为指定来源 name 注册专属列表解析函数。f(soup, src) -> list[CandidateLink]"""
    def deco(fn: Callable):
        _LIST_REGISTRY[source_name] = fn
        return fn
    return deco


def get_list_parser(source_name: str) -> Optional[Callable]:
    return _LIST_REGISTRY.get(source_name)


def has_custom_parser(source_name: str) -> bool:
    return source_name in _LIST_REGISTRY


# ---------------------------------------------------------------------------
# 详情页适配注册
# ---------------------------------------------------------------------------
@dataclass
class DetailResult:
    """站点详情页解析结果（None 表示该站无适配，回退通用解析）。"""

    title: str = ""
    content: str = ""
    page_date: str = ""                                    # YYYY-MM-DD（页面发布时间）
    attachments: list = field(default_factory=list)       # [{"name","url","fmt"}]


_DETAIL_REGISTRY: list[tuple[str, Callable]] = []  # (url特征, fn)，先注册先匹配


def register_detail_parser(url_mark: str):
    """按页面 URL 包含的片段注册详情解析器（如 'ndrc.gov.cn'）。"""
    def deco(fn: Callable):
        _DETAIL_REGISTRY.append((url_mark, fn))
        return fn
    return deco


def get_detail_adapter(page_url: str) -> Optional[Callable]:
    for mark, fn in _DETAIL_REGISTRY:
        if mark and mark in (page_url or ""):
            return fn
    return None


# ---------------------------------------------------------------------------
# NDRC 国家发展改革委详情页
# ---------------------------------------------------------------------------
_ATTACH_RE = re.compile(r"\.(pdf|doc|docx|wps|xls|xlsx|zip|rar|ofd)\s*$", re.I)


def _clean_text(s: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", (s or "").replace("\u3000", " ")).strip()


@register_detail_parser("ndrc.gov.cn")
def _ndrc_detail(soup: BeautifulSoup, page_url: str) -> Optional[DetailResult]:
    """正文容器 div.TRS_Editor（与 .article_con 同区）；标题取 <title>【…】内文。"""
    con = soup.select_one("div.TRS_Editor") or soup.select_one(".article_con")
    if con is None:
        return None
    content = _clean_text(con.get_text("\n", strip=True))
    if len(content) < 40:  # 正文过短视为无效页
        return None

    title = ""
    t = soup.title.get_text(strip=True) if soup.title else ""
    m = re.match(r"^【(.+?)】", t)
    title = m.group(1) if m else re.split(r"[_-]", t)[0].strip()

    page_date = ""
    m = re.search(r"发布时间[:：]\s*(20\d{2})/(\d{1,2})/(\d{1,2})",
                  soup.get_text(" ", strip=True))
    if m:
        page_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    attachments = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if href.lower().startswith(("javascript", "mailto:")):
            continue
        if not _ATTACH_RE.search(href):
            continue
        abs_url = urllib.parse.urljoin(page_url, href)
        name = " ".join(a.get_text(strip=True).split()) or abs_url.rsplit("/", 1)[-1]
        attachments.append({
            "name": name[:200],
            "url": abs_url,
            "fmt": _ATTACH_RE.search(href).group(1).lower(),
        })

    return DetailResult(title=title[:300], content=content,
                        page_date=page_date, attachments=attachments)


# ---------------------------------------------------------------------------
# 浙江省发展改革委 art 详情页（新 /col/.../art/ 与旧 /art/ 两种路径同构）
# ---------------------------------------------------------------------------
@register_detail_parser("fzggw.zj.gov.cn")
def _zjfgw_detail(soup: BeautifulSoup, page_url: str) -> Optional[DetailResult]:
    con = soup.select_one("div#zoom") or soup.select_one("div.con")
    if con is None:
        return None
    content = _clean_text(con.get_text("\n", strip=True))
    if len(content) < 20:
        return None

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = " ".join(h1.get_text(" ", strip=True).split())
    if not title and soup.title:
        title = " ".join(soup.title.get_text(strip=True).split())

    page_date = ""
    m = re.search(r"发布[日时间][^0-9]{0,8}(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})",
                  soup.get_text(" ", strip=True))
    if m:
        page_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    attachments = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if href.lower().startswith(("javascript", "mailto:")):
            continue
        decoded = urllib.parse.unquote(href)
        if not _ATTACH_RE.search(decoded):
            continue
        abs_url = urllib.parse.urljoin(page_url, href)
        name = " ".join(a.get_text(strip=True).split()) or abs_url.rsplit("/", 1)[-1]
        attachments.append({
            "name": urllib.parse.unquote(name)[:200],
            "url": abs_url,
            "fmt": _ATTACH_RE.search(decoded).group(1).lower(),
        })

    return DetailResult(title=title[:300], content=content,
                        page_date=page_date, attachments=attachments)


# ---------------------------------------------------------------------------
# 江苏省发展改革委 art 详情页（hanweb/TRS：正文 div.TRS_Editor；
# <title> 带「站点名 栏目名」前缀，需清洗；栏目见 sources.yaml jsfgw_tzgg）
# ---------------------------------------------------------------------------
_JS_TITLE_PREFIX_RE = re.compile(r"^\s*(?:江苏省发展和改革委员会|江苏省发展改革委)\s*(?:通知公告|发改要闻|政策解读|市县动态|图片新闻|时政要闻|要闻动态)?\s*")


@register_detail_parser("fzggw.jiangsu.gov.cn")
def _jsfgw_detail(soup: BeautifulSoup, page_url: str) -> Optional[DetailResult]:
    con = soup.select_one("div.TRS_Editor") or soup.select_one("div#zoom")
    if con is None:
        return None
    content = _clean_text(con.get_text("\n", strip=True))
    if len(content) < 20:
        return None

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = " ".join(h1.get_text(" ", strip=True).split())
    if not title and soup.title:
        title = " ".join(soup.title.get_text(strip=True).split())
    title = _JS_TITLE_PREFIX_RE.sub("", title).strip()

    page_date = ""
    m = re.search(r"(?:发布时间|发布[日时]间|日期)\s*[:：]?\s*(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})",
                  soup.get_text(" ", strip=True))
    if m:
        page_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    attachments = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if href.lower().startswith(("javascript", "mailto:")):
            continue
        decoded = urllib.parse.unquote(href)
        if not _ATTACH_RE.search(decoded):
            continue
        abs_url = urllib.parse.urljoin(page_url, href)
        name = " ".join(a.get_text(strip=True).split()) or abs_url.rsplit("/", 1)[-1]
        attachments.append({
            "name": urllib.parse.unquote(name)[:200],
            "url": abs_url,
            "fmt": _ATTACH_RE.search(decoded).group(1).lower(),
        })

    return DetailResult(title=title[:300], content=content,
                        page_date=page_date, attachments=attachments)


@register_detail_parser("yndrc.yn.gov.cn")
def _yunnan_detail(soup: BeautifulSoup, page_url: str) -> Optional[DetailResult]:
    con = soup.select_one(".show-content")
    if con is None:
        return None
    content = _clean_text(con.get_text("\n", strip=True))
    if not content:
        return None
    title = soup.find('meta', attrs={'name':'ArticleTitle'})
    date = soup.find('meta', attrs={'name':'PubDate'})
    return DetailResult(title=title.get('content','') if title else '', content=content,
                        page_date=date.get('content','')[:10] if date else '')
