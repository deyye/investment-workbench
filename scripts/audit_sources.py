"""Repeatable source acceptance: list discovery -> detail -> attachment -> DB.

python scripts/audit_sources.py --source yn_policies,jl_policies --pages 2 --details 2
Reports are evidence for the sampled window, never a claim of province-wide recall.
Uses an isolated temporary database, never edits an existing policy database.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from policy_collector.config import AppConfig
from policy_collector.pipeline import Pipeline


# 中央文件转载的常见子目录标记：省级栏目的这类子栏目若被一并收录，
# 会把中央文件的 region 错记成该省（实测：福建 /zwgk/fgzd/ 下的 gjfgwwj）。
NATIONAL_REPOST = re.compile(r'gjfgw|gjwj|gwywj|guowuyuan|zgzy|zygwy|zhengcewenjian/gj', re.I)


# 年月类路径段（TRS 常见的 /栏目/202601/t20260109_123.html）不是子栏目，
# 统计时必须排除，否则每个常规来源都会被误报成"聚合栏目"。
_DATE_SEG = re.compile(r'^(?:19|20)\d{2}(?:\d{2})?(?:\d{2})?$|^\d{1,2}$')
# 子栏目名应当是有语义的字母词。**纯数字段一律不算子栏目**：
#   · /栏目/202601/t20260109_1.html        —— 年月（TRS）
#   · /栏目/2026090912183160373/index.shtml —— 文章 ID（辽宁慧点）
# 若只排除年月形态，辽宁那类"长数字串即文章 ID"的站会被误报成"聚合栏目"
# （实测：辽宁 36 条详情被拆成 36 个"子栏目"）。聚合栏目检测依赖这个判据，
# 误报会让人开始怀疑检查本身——那时候真问题也就一起被忽略了。
_NUMERIC_SEG = re.compile(r'^\d+$')


def sub_column_distribution(urls, list_url):
    """统计详情链接落在 list_url 下的哪些**直接子目录**（排除年月段）。

    聚合型栏目（一个 list_url 下挂多个子栏目）是"归属错记"的温床：
    列表看着正常、机制检查全过，但收进来的文件可能来自别的辖区。
    把分布打出来，人一眼就能看出"前 15 条全在某个子目录里"。
    """
    base = urlsplit(list_url).path
    if not base.endswith('/'):
        base = base.rsplit('/', 1)[0] + '/'
    counts = {}
    for u in urls:
        p = urlsplit(u).path
        if not p.startswith(base):
            key = '(栏目路径之外)'
        else:
            rest = p[len(base):].strip('/')
            seg = rest.split('/')[0] if '/' in rest else ''
            if not seg or _NUMERIC_SEG.match(seg):
                continue          # 栏目根、年月段或文章 ID 段，都不算子栏目
            key = seg
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def audit_source(name, pages, details):
    cfg = AppConfig.load()
    cfg.fetch.timeout_seconds = 12
    cfg.fetch.retries = 0
    src = replace(cfg.sources[name], max_pages=pages)
    with tempfile.TemporaryDirectory(prefix='policy-audit-') as folder:
        cfg.data_dir = Path(folder)
        cfg.downloads_dir = cfg.data_dir / 'originals'
        cfg.db_path = cfg.data_dir / 'audit.db'
        cfg.sources = {name: src}
        pipe = Pipeline(cfg)
        try:
            pipe.sync_sources()
            discovered = pipe.discover(src)
            source_row = pipe.db.get_source(name)
            all_urls = [r['page_url'] for r in pipe.db._conn.execute(
                'SELECT page_url FROM fetch_records WHERE source_id=?', (source_row['id'],))]
            # 归属检查：聚合型栏目 + 中央文件转载子目录 —— 机制全过但归属会错记
            sub_columns = sub_column_distribution(all_urls, src.list_url)
            national_reposts = [k for k in sub_columns if NATIONAL_REPOST.search(k)]
            rows = pipe.db._conn.execute('SELECT * FROM fetch_records WHERE source_id=? ORDER BY id LIMIT ?', (source_row['id'], details)).fetchall()
            samples = []
            for row in rows:
                stat = pipe.ingest_url(src, row['page_url'], prefer='rule')
                record = pipe.db.get_fetch(source_row['id'], row['page_url'])
                doc = json.loads(record.get('document_json') or '{}')
                samples.append({'url': row['page_url'], 'title': doc.get('title',row['title']),
                    'stats': stat.to_dict(), 'fetch_status': record['status'],
                    'metadata': {k: doc.get(k,'') for k in ('page_date','doc_date','wenhao')},
                    'body_chars': len(doc.get('content','')), 'parse_error':doc.get('parse_error',''),
                    'attachments': [{k:a.get(k,'') for k in ('name','url','parse_status','error')} for a in doc.get('attachments',[])],
                    'error':record.get('error','')})
            return {'source':name,'region':src.region,'column':src.category,'url':src.list_url,
                'discovered':discovered.discovered,'discovery':pipe.collector.discovery_status.get(name,{}),
                'sub_columns':sub_columns,'national_reposts':national_reposts,
                'errors':pipe.discovery_errors,'sample_count':len(samples),'samples':samples,
                'classification_validation':'规则模式仅验证流程，未验证真实大模型准确率',
                'accepted_sample':bool(samples) and not pipe.discovery_errors and not national_reposts and all(
                    s['body_chars'] > 0 and not any(s['stats'][k] for k in
                    ('failed','documents_incomplete','attachments_failed','attachments_unparsed')) for s in samples)}
        finally:
            pipe.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',default='')
    parser.add_argument('--pages',type=int,default=2)
    parser.add_argument('--details',type=int,default=2)
    parser.add_argument('--workers',type=int,default=3)
    parser.add_argument('--output',default='docs/source-audit.json')
    args=parser.parse_args()
    if min(args.pages,args.details,args.workers)<1:parser.error('pages/details/workers must be positive')
    cfg=AppConfig.load()
    names=args.source.split(',') if args.source else [n for n,s in cfg.sources.items() if s.enabled]
    for name in names:
        if name not in cfg.sources:parser.error(f'unknown source: {name}')
    def run(name):
        try:out=audit_source(name,args.pages,args.details)
        except Exception as exc:out={'source':name,'accepted_sample':False,'error':f'{type(exc).__name__}: {exc}'}
        reason = ''
        if not out['accepted_sample']:
            if out.get('national_reposts'):
                reason = '⚠ 疑似中央文件转载子栏目: ' + ','.join(out['national_reposts'])
            elif out.get('error'):
                reason = out['error'][:70]
            elif out.get('errors'):
                reason = str(out['errors'][0])[:70]
            else:
                bad = [(s['url'], s['body_chars'], {k: v for k, v in s['stats'].items()
                        if k in ('failed', 'documents_incomplete', 'attachments_failed', 'attachments_unparsed') and v})
                       for s in (out.get('samples') or [])]
                bad = [b for b in bad if b[1] == 0 or b[2]]
                if not out.get('samples'):
                    reason = '未取到样本'
                elif bad:
                    reason = '样本异常: ' + (bad[0][0][-40:] + f' 正文{bad[0][1]}字 ' + json.dumps(bad[0][2], ensure_ascii=False))[:80]
            if not reason and out.get('sub_columns') and len(out['sub_columns']) > 1:
                reason = '（提示）聚合栏目多子目录: ' + json.dumps(out['sub_columns'], ensure_ascii=False)[:70]
        print(name, 'sample_pass' if out['accepted_sample'] else 'needs_attention', reason, flush=True)
        return out
    with ThreadPoolExecutor(max_workers=min(args.workers,6)) as pool:results=list(pool.map(run,names))
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps({'checked_at':datetime.now(timezone.utc).isoformat(),
        'pages_limit':args.pages,'details_limit':args.details,
        'scope':'指定栏目的抽样接入验收；非全省政策覆盖率，非业务分类准确率。含归属检查：聚合型栏目与中央文件转载子栏目会被判不通过（实测：福建 /zwgk/fgzd/ 下 gjfgwwj 会把国家发改委文件的归属错记成福建）',
        'results':results},ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if all(r['accepted_sample'] for r in results) else 1

if __name__=='__main__':raise SystemExit(main())
