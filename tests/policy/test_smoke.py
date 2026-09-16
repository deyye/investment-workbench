"""冒烟测试：离线 demo 全链路 + 官网适配器 + Web 界面。

运行：python tests/test_smoke.py  （或 pytest）
说明：全部离线（用 samples/ 内官网 HTML 快照），不依赖网络与 API Key。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from policy_collector.config import AppConfig
from policy_collector.pipeline import Pipeline


PROJ = Path(__file__).resolve().parents[2]


def _tmp_cfg() -> AppConfig:
    td = tempfile.mkdtemp()
    cfg = AppConfig.load()
    cfg.data_dir = Path(td)
    cfg.downloads_dir = Path(td) / "downloads"
    cfg.db_path = Path(td) / "policy.db"
    return cfg


def test_demo_closed_loop() -> None:
    cfg = _tmp_cfg()
    pipe = Pipeline(cfg)
    stats = pipe.run_demo(samples_dir=PROJ / "samples" / "policies")
    assert stats.ingested == 4, f"预期入库4篇，实际 {stats.ingested}"
    assert stats.duplicates >= 1, "应至少1篇转载判重"
    assert stats.excluded >= 1, "应至少1篇非政策(会议通知)被排除"
    # 幂等：重跑不新增
    stats2 = pipe.run_demo(samples_dir=PROJ / "samples" / "policies")
    assert stats2.ingested == 0, "重跑不应新增入库"
    print("smoke ok: demo ingested=4, dup>=1, excluded>=1, rerun-idempotent")


def test_ndrc_detail_adapter() -> None:
    """NDRC 详情页适配（samples/gov 官网快照）：正文/文号/日期/附件登记。"""
    from policy_collector.parser import Parser

    raw = (PROJ / "samples" / "gov" / "ndrc_detail_sample.html").read_bytes()
    url = "https://www.ndrc.gov.cn/xxgk/zcfb/tz/202608/t20260828_1407232.html"
    doc = Parser().parse(raw, "html", page_url=url)
    assert "物流网建设实施方案" in doc.title
    assert "发改经贸〔2026〕1241号" in (doc.wenhao or "")
    assert doc.page_date == "2026-08-27", f"发布日期解析 {doc.page_date}"
    assert len(doc.content) > 100, "正文过短"
    assert doc.attachments, "应登记附件（PDF/OFD）"
    assert all(a["url"].startswith("https://") for a in doc.attachments), "附件 URL 应为绝对地址"
    print("smoke ok: NDRC detail adapter (title/wenhao/date/attachments)")


def test_ndrc_list_adapter() -> None:
    """NDRC 列表页适配（官网快照）：只收本栏目详情，剔除解读等噪音。"""
    from policy_collector.collector import ListPageParser
    from policy_collector.config import SourceConfig

    html = (PROJ / "samples" / "gov" / "ndrc_list_sample.html").read_text(encoding="utf-8", errors="ignore")
    src = SourceConfig(name="ndrc_zcwj", site="国家发展改革委", region="国家", category="政策通知",
                       enabled=True, list_url="https://www.ndrc.gov.cn/xxgk/zcfb/tz/",
                       link_selector="ul.u-list li a", include=["/xxgk/zcfb/tz/202"],
                       exclude=[".pdf", ".doc", ".docx", ".xls"], max_pages=1)
    links = ListPageParser(src).parse(html, base_url="https://www.ndrc.gov.cn/xxgk/zcfb/tz/")
    assert links, "应发现候选详情链接"
    assert all(l.url.startswith("https://www.ndrc.gov.cn/xxgk/zcfb/tz/20") for l in links), "存在越栏链接"
    assert not any("/jd/" in l.url for l in links), "解读链接未被过滤"
    print(f"smoke ok: NDRC list adapter ({len(links)} links, 0 noise)")


def test_webapp_pages() -> None:
    """Web 界面主要路由 + 人工复核操作（test client）。"""
    from policy_collector.webapp import create_app

    cfg = _tmp_cfg()
    pipe = Pipeline(cfg)
    pipe.run_demo(samples_dir=PROJ / "samples" / "policies")
    app = create_app(cfg)
    c = app.test_client()
    for path in ("/", "/policies", "/policies?review=pending", "/sources", "/runs", "/health"):
        assert c.get(path).status_code == 200, f"GET {path} 非200"
    # 找一条待复核记录走审核
    pending = [p for p in pipe.db.query_policies(limit=50) if p["need_review"]]
    assert pending, "demo 应有待复核记录"
    pid = pending[0]["id"]
    assert c.get(f"/policies/{pid}").status_code == 200
    with c.session_transaction() as sess:
        token = sess["csrf"]
    r = c.post(f"/policies/{pid}/review", data={"csrf_token": token, "action": "adjust", "categories": ["guide"]},
               follow_redirects=True)
    assert r.status_code == 200
    p2 = pipe.db.get_policy(pid)
    assert p2["review_status"] == "adjusted" and "引导类" in p2["category_names"], "adjust 未生效"
    assert c.get("/policies/99999").status_code == 404
    print("smoke ok: webapp routes + adjust review")


if __name__ == "__main__":
    test_demo_closed_loop()
    test_ndrc_detail_adapter()
    test_ndrc_list_adapter()
    test_webapp_pages()
    print("\n全部冒烟测试通过 ✔")
