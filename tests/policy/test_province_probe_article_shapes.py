"""探测脚本的"文章型链接"形态表——回归锁。

为什么专门给它写测试：这张表的历史就是**两次同类误判**。

1. **2026-09-13**：上海 `/fgw_jggl/20260828/hash.html` 的年月日是 **8** 位，
   而表里写的是 `20\\d{8}`（10 位）→ 一条都匹配不上，上海被记成"需适配"。
2. **2026-09-14**：`/YYYYMM/hash.shtml`（**6** 位年月）这一族**整体缺失**——
   新疆、海南都用这个形态，两家都被判成"0 篇文章型"，
   海南因此在 9/13 被写成"列表走 AJAX 的壳"，直到 9/14 深夜才推翻。

两次的根因是同一件事：**凭印象写日期段的位数，没拿真实样本验**。
所以这里把"已接通省份的真实 URL 形态"逐条钉住；
新增省份前先跑一遍 `looks_like_article()`，认不出就别急着下"要 JS"的结论。
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_probe():
    spec = importlib.util.spec_from_file_location("discover_provinces",
                                                  ROOT / "scripts" / "discover_provinces.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


probe = _load_probe()


#: (URL, 来自哪个省/哪种机制, 备注)
ARTICLE_URLS = [
    ("https://xjdrc.xinjiang.gov.cn/xjfgw/c108361/202509/5f995c5b3cbb48599c7bfde0ade3a160.shtml",
     "新疆", "6 位年月 /YYYYMM/ + 32 位 hash ← 曾整族缺失"),
    ("https://plan.hainan.gov.cn/sfgw/0400/202609/c3df3ed14ef84026be48909494b63550.shtml",
     "海南", "同上。海南曾被记成「列表走 AJAX 的壳」"),
    ("http://drc.jiangxi.gov.cn/jxsfzhggwyh/col/col14590/content/content_2098331399481798656.html",
     "江西", "/content_<长数字>.html"),
    ("https://fgw.ln.gov.cn/fgw/zc/zxzc/2026090912183160373/index.shtml",
     "辽宁", "长数字串目录 + index.shtml ← 曾判「需 JS 渲染」"),
    ("https://fzgg.tj.gov.cn/xxfb/tzggx/202609/t20260911_7372816.html",
     "天津", "TRS /YYYYMM/tYYYYMMDD_<n>.html"),
    ("https://www.shanghai.gov.cn/fgw_jggl/20260828/8a3f6c1e.html",
     "上海", "8 位 YYYYMMDD ← 曾因写成 10 位而全不命中"),
    ("https://www.ndrc.gov.cn/xxgk/zcfb/tz/202608/t20260810_1406952.html",
     "国家发改委", "TRS 标准形态"),
    ("https://www.gov.cn/zhengce/content/202608/content_12345678.htm",
     "中国政府网", "/content_<数字>"),
    ("https://fzggw.zj.gov.cn/art/2026/8/12/art_1229123418_59012345.html",
     "浙江", "hanweb /art_<id>.html"),
    ("https://fzggw.cq.gov.cn/zwgk/zfxxgkml/xzgfxwj/202609/t20260905_12345678.html",
     "重庆", "TRS + 中文目录"),
    ("https://henan.example.gov.cn/2026/09-11/3418623.html",
     "河南", "/YYYY/MM-DD/<数字>.html"),
]

#: 栏目页/导航页**不该**被当成文章
NON_ARTICLE_URLS = [
    ("https://xjdrc.xinjiang.gov.cn/xjfgw/c108345/zwgk.shtml", "栏目总览页"),
    ("https://plan.hainan.gov.cn/sfgw/0500/zcwj.shtml", "栏目页"),
    ("https://drc.jiangxi.gov.cn/jxsfzhggwyh/col/col47604/index.html", "栏目页"),
]


@pytest.mark.parametrize("url,origin,why", ARTICLE_URLS)
def test_connected_provinces_urls_are_recognised(url, origin, why):
    assert probe.looks_like_article(url), (
        f"{origin} 的详情链接形态认不出（{why}）：\n  {url}\n"
        f"直接后果：该省会被判成「0 篇文章型」，进而误归为「需 JS 渲染 / 入口待找」。"
    )


@pytest.mark.parametrize("url,why", NON_ARTICLE_URLS)
def test_column_pages_are_not_mistaken_for_articles(url, why):
    """反向也要守：把栏目页当文章，会把"栏目"记成"文章"、统计失真。"""
    assert not probe.looks_like_article(url), f"{why} 被误判成文章型链接：{url}"
