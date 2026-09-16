"""浙江发改「公告公示」真实源全量翻页验证（只读列表 + 1 条详情，不入库）。

用法: .venv/bin/python scripts/zj_live_check.py [max_pages]
默认 max_pages=40（与 config/sources.yaml 的 zjfgw_gsgg 一致），空页自动停。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from policy_collector.collector import Collector, discover_zj_unit_links
from policy_collector.config import AppConfig
from policy_collector.parser import Parser
from policy_collector.site_adapters import get_detail_adapter

max_pages = int(sys.argv[1]) if len(sys.argv) > 1 else 40
cfg = AppConfig.load()
src = next(s for s in cfg.sources.values() if s.name == 'zjfgw_gsgg')
# 原件落盘到数据目录下的 _live_check，避免混入正式下载目录
cfg.downloads_dir = Path(cfg.downloads_dir).parent / '_live_check'

collector = Collector(cfg)
page_no_of = []
real_fetch = collector.fetch

def logged_fetch(url, params=None, **kw):
    if 'paramJson' in (params or {}):
        page_no_of.append(json.loads(params['paramJson'])['pageNo'])
    return real_fetch(url, params=params, **kw)

collector.fetch = logged_fetch
try:
    t0 = time.monotonic()
    res = collector.fetch(src.list_url)
    assert res.ok, f'栏目页失败: {res.error}'
    links = discover_zj_unit_links(collector, src, res.content, page_url=res.final_url or src.list_url,
                                   max_pages=max_pages, page_size=15)
    dt = time.monotonic() - t0

    newest, oldest = links[0], links[-1]
    print(f'[翻页] 请求页序(至空页止): {page_no_of}')
    print(f'[翻页] 请求 {len(page_no_of)} 页 / 去重后详情链接 {len(links)} 条 / 耗时 {dt:.1f}s')
    print(f'[首条] {newest.url}\n        {newest.title}')
    print(f'[末条] {oldest.url}\n        {oldest.title}')
    new_path = sum(1 for l in links if '/col/' in l.url)
    print(f'[路径] 新 /col/.../art/ 结构 {new_path} 条，旧 /art/ 结构 {len(links) - new_path} 条')

    # 末条(最旧)真实详情页解析：走注册适配器，验证翻到的历史记录仍可完整入库
    raw = collector.fetch(oldest.url)
    if raw.ok:
        doc = Parser().parse(raw.content, 'html', page_url=oldest.url)
        adapter = get_detail_adapter(oldest.url)
        print(f'[详情] 适配器={adapter.__name__ if adapter else "通用回退"}')
        print(f'[详情] 标题: {doc.title[:80]}')
        print(f'[详情] 文号: {doc.wenhao or "无" } | 成文 {doc.doc_date or "无"} | 发布 {doc.page_date or "无"}')
        print(f'[详情] 附件 {len(doc.attachments)} 个: ' +
              ', '.join(f"{a['name'][:40]}({a['fmt']})" for a in doc.attachments[:3]))
        print(f'[详情] 正文 {len(doc.content)} 字符, 正文含"印发"={ "印发" in doc.content }')
    else:
        print(f'[详情] 末条抓取失败: {raw.error}')
finally:
    collector.close()
