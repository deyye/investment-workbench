"""江西 CMS 接口型列表（POST /queryList）的离线契约测试。

锁三件事，任一条坏了都说明"这个源静默失效"：
1. 能从栏目页内联脚本里取到 channelId 等参数（结构一变就取不到）
2. 必须用 **POST** 调 /queryList（GET 该端点 404——这是接入时最反直觉的一点）
3. 详情 URL 由 栏目名 + 文章 id 拼出，且不越出官网同源

用假 Collector 驱动，不联网。
"""
import json

from policy_collector.collector import (CandidateLink, FetchResult, discover_jx_query_links,
                                        extract_jx_query_spec)
from policy_collector.config import SourceConfig

PAGE = """
<script>
  var channelId = '1754390189518807040', codeName = 'col14590'
  var websiteId = '1753254619659046912', siteId = '1753254619659046912'
</script>
"""
PAGE_URL = "http://drc.jiangxi.gov.cn/jxsfzhggwyh/col/col14590/index.html"
SRC = SourceConfig(
    name="jx_tzgg", site="江西省发展和改革委员会", region="江西",
    list_url=PAGE_URL,
    include=["/jxsfzhggwyh/col/col14590/content/"],
)

PAYLOAD = json.dumps({
    "data": {"page": 1, "rows": 2, "total": 2, "results": [
        {"source": {"id": "2098331399481798656",
                    "title": "  江西省发展改革委关于成品油价格调整的通告  "}},
        {"source": {"id": "2097628395954843648", "title": "某项公告"}},
    ]},
}, ensure_ascii=False).encode("utf-8")


class FakeCollector:
    """记录调用方式；只回一页数据。"""

    def __init__(self, payload=PAYLOAD):
        self.payload = payload
        self.calls = []
        self.saved = []
        self.discovery_status = {}

    def fetch(self, url, timeout=None, params=None, form=None):
        self.calls.append({"url": url, "form": form, "params": params})
        return FetchResult(ok=True, url=url, final_url=url, content=self.payload,
                           content_type="application/json", sha256="x")

    def save(self, source, url, raw, fmt):
        self.saved.append((source, url, fmt))


def test_spec_is_read_from_the_column_page():
    spec = extract_jx_query_spec(PAGE, PAGE_URL)
    assert spec is not None
    # 接口在**站点根**，不是栏目目录下（实测 /jxsfzhggwyh/queryList 是 404）
    assert spec["endpoint"] == "http://drc.jiangxi.gov.cn/queryList"
    assert spec["channelId"] == "1754390189518807040"
    assert spec["column_path"] == "col14590"


def test_spec_missing_channel_id_is_reported_not_guessed():
    assert extract_jx_query_spec("<html>变了</html>", PAGE_URL) is None


def test_discovery_uses_post_and_derives_detail_urls():
    c = FakeCollector()
    links = discover_jx_query_links(c, SRC, PAGE.encode(), PAGE_URL, max_pages=1)
    assert len(c.calls) == 1
    call = c.calls[0]
    assert call["form"], "必须用 POST 表单调用（GET 该端点直接 404）"
    assert call["form"]["channelId"] == "1754390189518807040"
    assert call["form"]["pageNo"] == 1
    assert isinstance(links[0], CandidateLink)
    assert links[0].url == ("http://drc.jiangxi.gov.cn/jxsfzhggwyh/col/col14590/"
                            "content/content_2098331399481798656.html")
    # 标题里的多余空白要折叠掉（接口原样带前后空格）
    assert links[0].title == "江西省发展改革委关于成品油价格调整的通告"
    # JSON 原件要落盘留证
    assert c.saved and c.saved[0][2] == "json"


def test_empty_page_stops_cleanly():
    c = FakeCollector(payload=b'{"data":{"rows":0,"results":[]}}')
    links = discover_jx_query_links(c, SRC, PAGE.encode(), PAGE_URL, max_pages=3)
    assert links == []
    assert c.discovery_status["jx_tzgg"]["end_reached"] is True
