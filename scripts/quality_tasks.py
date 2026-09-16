"""Inspect missing material, repair it, or prepare/score independently sampled candidates."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from policy_collector.config import AppConfig
from policy_collector.pipeline import Pipeline
from policy_collector.quality import attachment_report, repair_materials


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command',choices=['report','repair','sample','score'])
    ap.add_argument('--source',default='');ap.add_argument('--limit',type=int,default=20)
    ap.add_argument('--policy-id',type=int);ap.add_argument('--local-only',action='store_true')
    ap.add_argument('--prefer',choices=['llm','rule'],default='llm')
    ap.add_argument('--out',required=True,help='新结果文件或sample的新目录')
    ap.add_argument('--reference-list',help='独立人工官网列表JSONL，用于识别列表漏发现')
    ap.add_argument('--labels',help='score时传入完成复核的sample.jsonl')
    ap.add_argument('--seed',default='investment-audit-v1')
    args=ap.parse_args(argv)
    out=Path(args.out)
    if out.exists():raise ValueError('输出已存在，请使用新路径')
    pipe=Pipeline(AppConfig.load())
    try:
        if args.command=='report':result=attachment_report(pipe.db,args.source)
        elif args.command=='repair':result=repair_materials(pipe,args.source,args.limit,args.local_only,args.prefer,args.policy_id)
        elif args.command=='sample':
            from policy_collector.sampling import prepare_sample
            result=prepare_sample(pipe.db,out,args.limit,args.seed,args.reference_list,args.source)
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0
        else:
            from policy_collector.sampling import score_review
            if not args.labels:raise ValueError('score需要--labels')
            result=score_review(Path(args.labels))
        out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 1 if args.command=='repair' and result['stats']['failed'] else 0
    finally:pipe.close()

if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError) as e:print(str(e),file=sys.stderr);raise SystemExit(2)
