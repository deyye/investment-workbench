#!/usr/bin/env python3
"""浙江全量发现、入库及双轮检查。默认新建隔离目录，绝不自动删除已有目录。

--data-dir 指定的目录须为空，已有检查库只能通过 --keep 续跑。
验收分别展示发现到底、候选处理、材料完整性和稳定输入幂等情况。
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from policy_collector.evaluation import readonly_db
SOURCE = 'zjfgw_gsgg'


def prepare_directory(data_dir, protected, keep=False):
    data_dir = Path(data_dir).expanduser().resolve()
    protected = Path(protected).expanduser().resolve()
    if data_dir == protected or data_dir in protected.parents or protected in data_dir.parents:
        raise ValueError('隔离目录不能位于正式数据目录内或包含正式数据目录')
    if data_dir.exists() and any(data_dir.iterdir()):
        if not keep or not (data_dir/'policy.db').is_file():
            raise ValueError('隔离目录非空；请换新目录，已有检查库可显式 --keep，程序不会删除文件')
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def db_stats(db_path):
    con = readonly_db(Path(db_path))
    try:
        src = con.execute('SELECT * FROM source_configs WHERE name=?', (SOURCE,)).fetchone()
        out = {'source_found': bool(src), 'fetch_total': 0, 'fetch_by_status': {},
               'source_error': src['last_error'] if src else '来源不存在'}
        if not src:
            return out
        out['fetch_by_status'] = {r['status']:r['n'] for r in con.execute(
            'SELECT status,COUNT(*) n FROM fetch_records WHERE source_id=? GROUP BY status', (src['id'],))}
        out['fetch_total'] = sum(out['fetch_by_status'].values())
        rows = [dict(r) for r in con.execute('''SELECT p.* FROM policies p WHERE
            p.version=(SELECT MAX(v.version) FROM policies v WHERE v.policy_key=p.policy_key)
            AND p.review_status!='rejected' AND EXISTS
            (SELECT 1 FROM policy_sources s WHERE s.policy_id=p.id AND s.source_id=?)''', (src['id'],))]
        out['policy_current'] = len(rows)
        out['wenhao_missing'] = sum(not r['wenhao'] for r in rows)
        out['wenhao_missing_rate'] = out['wenhao_missing']/len(rows) if rows else None
        attachments = [dict(a) for r in rows for a in con.execute('SELECT * FROM attachments WHERE policy_id=?',(r['id'],))]
        out['attachments'] = {'total':len(attachments),
            'downloaded':sum(bool(a['local_path']) for a in attachments),
            'parsed':sum(a['parse_status']=='ok' for a in attachments),
            'incomplete':sum(a['parse_status']!='ok' for a in attachments)}
        out['runs'] = [{'id':r['run_id'],'status':r['status'],'summary':json.loads(r['summary'] or '{}'),'note':r['note']}
            for r in con.execute('SELECT * FROM run_logs WHERE source_id=? ORDER BY id', (src['id'],))]
        return out
    finally:
        con.close()


def build_verdict(rounds, final):
    statuses = final.get('fetch_by_status', {})
    unresolved = sum(n for status,n in statuses.items() if status not in ('processed','excluded'))
    last = rounds[-1] if rounds else {}
    r2 = rounds[1] if len(rounds) >= 2 else {}
    verdict = {
        'discovery_reached_end': bool(last.get('discovery',{}).get('end_reached')) and not final.get('source_error'),
        'candidates_total':final.get('fetch_total',0), 'unresolved_candidates':unresolved,
        'candidate_processing_complete':final.get('fetch_total',0)>0 and unresolved==0,
        'material_complete':final.get('attachments',{}).get('incomplete',0)==0 and not last.get('documents_incomplete',0),
        'last_run_ok':bool(final.get('runs')) and final['runs'][-1]['status']=='ok',
        'two_rounds_completed':len(rounds)>=2,
        'stable_input_idempotence_verified':bool(r2) and r2.get('ingested',0)==0 and r2.get('updated',0)==0 and r2.get('duplicates',0)>0,
    }
    verdict['overall_ok'] = all(verdict[k] for k in ('discovery_reached_end','candidate_processing_complete',
        'material_complete','last_run_ok','two_rounds_completed','stable_input_idempotence_verified'))
    return verdict


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data-dir', help='新隔离目录；默认创建独立临时目录')
    ap.add_argument('--rounds',type=int,default=2)
    ap.add_argument('--limit',type=int,default=600)
    ap.add_argument('--keep',action='store_true')
    args=ap.parse_args(argv)
    try:
        if args.rounds<1 or args.limit<1:
            raise ValueError('rounds 和 limit 必须为正整数')
        from policy_collector.config import AppConfig, PROJECT_ROOT
        from policy_collector.pipeline import Pipeline
        protected=AppConfig.load().data_dir
        candidate=Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix='pc_zj_full_'))
        data_dir=prepare_directory(candidate,protected,args.keep)
        # 同时保护项目默认目录，避免环境变量改变后误用。
        prepare_directory(data_dir,PROJECT_ROOT/'data',args.keep)
        os.environ['POLICY_DATA_DIR']=str(data_dir)
        cfg=AppConfig.load()
        if SOURCE not in cfg.sources:
            raise ValueError('来源未配置')
        overall={'source':SOURCE,'data_dir':str(data_dir),'rounds':[],'final':{}}
        pipe=Pipeline(cfg)
        try:
            for i in range(1,args.rounds+1):
                stats=pipe.run_source(SOURCE,prefer='rule',limit=args.limit)
                overall['rounds'].append({'round':i,**stats.to_dict(),
                    'discovery':dict(pipe.collector.discovery_status.get(SOURCE,{}))})
                overall['final']=db_stats(cfg.db_path)
                overall['verdict']=build_verdict(overall['rounds'],overall['final'])
                (data_dir/'report.json').write_text(json.dumps(overall,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(overall['rounds'][-1],ensure_ascii=False),flush=True)
        finally:
            pipe.close()
        print(json.dumps(overall['verdict'],ensure_ascii=False,indent=2))
        print(f"报告：{data_dir/'report.json'}")
        return 0 if overall['verdict']['overall_ok'] else 1
    except (ValueError,OSError,sqlite3.Error) as e:
        print(str(e),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
