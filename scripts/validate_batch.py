"""Small real-model batch on a frozen audit corpus; no source database mutations."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import sys
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from policy_collector.config import AppConfig
from policy_collector.classifier import Classifier
from policy_collector.models import Document,Classification
from policy_collector.sampling import read_jsonl,score_review
from policy_collector.evaluation import evaluate_predictions


def run_batch(sample_dir, out, classifier, *, split='development',limit=8,dry_run=False,
              allow_provisional=False,input_rate=None,output_rate=None,sample_ids=None):
    if out.exists():raise ValueError('评测目录已存在，拒绝覆盖')
    if limit<1 or limit>50:raise ValueError('小批量样本数必须为1至50')
    for rate in (input_rate,output_rate):
        if rate is not None and (not math.isfinite(rate) or rate<0):raise ValueError('单价必须为非负有限数')
    score_review(sample_dir/'sample.jsonl')  # validate immutable snapshot/group and human label provenance
    labels=read_jsonl(sample_dir/'sample.jsonl')
    labels=[g for g in labels if g['split']==split]
    if sample_ids:
        if len(sample_ids)!=len(set(sample_ids)) or not set(sample_ids).issubset({g['sample_id'] for g in labels}):
            raise ValueError('指定样本重复、不存在或不属于当前分组')
        labels=[g for g in labels if g['sample_id'] in sample_ids]
        if len(labels)>limit:raise ValueError('指定样本超过limit')
    else:labels=labels[:limit]
    if not labels:raise ValueError('该分组没有样本；不能把已用于开发的样本临时改为独立验收集')
    corpus={r['sample_id']:r for r in read_jsonl(sample_dir/'corpus.jsonl')}
    cfg=classifier.cfg
    report={'split':split,'selected':len(labels),'dry_run':dry_run,'model':cfg.llm.effective_model,
        'rules_sha256':hashlib.sha256(json.dumps(cfg.classification,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
        'prompt_sha256':hashlib.sha256(classifier.llm.SYSTEM_PROMPT_TMPL.encode()).hexdigest(),
        'label_file_sha256':hashlib.sha256((sample_dir/'sample.jsonl').read_bytes()).hexdigest(),
        'input_rate_per_million':input_rate,'output_rate_per_million':output_rate,'currency':'CNY',
        'status':'preflight','blockers':[],'readiness':[]}
    docs=[]
    for g in labels:
        raw=corpus[g['sample_id']]['document'];doc=Document(**raw);docs.append(doc)
        if hashlib.sha256(doc.analysis_text.encode()).hexdigest()!=g['analysis_sha256']:raise ValueError('分析文本指纹不符')
        complete=bool(doc.content.strip()) and not doc.parse_error and all(a.get('parse_status')=='ok' and a.get('parsed_text') for a in doc.attachments)
        fits=len(doc.analysis_text)<=cfg.llm.chunk_chars*cfg.llm.max_chunks
        report['readiness'].append({'sample_id':g['sample_id'],'material_complete':complete,'fits_context':fits,'analysis_chars':len(doc.analysis_text)})
        if not complete or not fits:report['blockers'].append({'sample_id':g['sample_id'],'reason':'材料不完整或超过分析上限'})
    reviewed=all(g['label_status']=='human_reviewed' for g in labels)
    if not reviewed and (split=='holdout' or not allow_provisional):report['blockers'].append({'reason':'需完整业务终审；开发集探索调用可显式allow-provisional'})
    if not classifier.llm.available:report['blockers'].append({'reason':'未配置真实模型密钥或服务'})
    out.mkdir(parents=True)
    if dry_run or report['blockers']:
        report['status']='blocked' if report['blockers'] else 'ready'
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));return report
    predictions=[];timings=[];errors=Counter();tokens_in=tokens_out=0;known_usage=True;failures=0
    with (out/'predictions.jsonl').open('x') as log:
        for g,doc in zip(labels,docs):
            if failures>=3:break
            started=time.monotonic()
            try:p=asdict(classifier.classify(doc,'llm'))
            except Exception as e:
                p=asdict(Classification(method='error',need_review=True,fallback_reason=type(e).__name__))
            elapsed=time.monotonic()-started
            timings.append(elapsed);predictions.append(p)
            tokens_in+=p['input_tokens'];tokens_out+=p['output_tokens']
            known_usage=known_usage and p.get('usage_reported',False) and p['method']=='llm'
            failures=failures+1 if p['method']!='llm' else 0
            row_errors=[]
            if p['method']!='llm':row_errors.append('service_failure_or_fallback')
            if p['is_investment_policy']=='pending':row_errors.append('pending')
            if g['label_status']=='human_reviewed':
                pred=set(filter(None,p['category'].split(','))) if p['is_investment_policy']=='yes' else set()
                if g['relevant'] and p['is_investment_policy']=='no':row_errors.append('false_exclusion')
                if not g['relevant'] and p['is_investment_policy']=='yes':row_errors.append('false_inclusion')
                if set(g['categories'])-pred:row_errors.append('missing_categories')
                if pred-set(g['categories']):row_errors.append('extra_categories')
            errors.update(row_errors)
            log.write(json.dumps({'sample_id':g['sample_id'],'analysis_sha256':g['analysis_sha256'],
                'prediction':p,'elapsed_seconds':round(elapsed,3),'error_types':row_errors},ensure_ascii=False)+'\n');log.flush()
    complete=len(predictions)==len(labels) and all(p['method']=='llm' for p in predictions)
    report.update(status=('human_reviewed' if reviewed else 'exploratory') if complete else 'incomplete',
        completed=len(predictions),not_attempted=len(labels)-len(predictions),error_types=dict(errors),
        pending_review_ratio=sum(p['need_review'] for p in predictions)/len(predictions) if predictions else None,
        elapsed_seconds=round(sum(timings),3),input_tokens=tokens_in,output_tokens=tokens_out,usage_complete=known_usage,
        estimated_cost_cny=(tokens_in*input_rate+tokens_out*output_rate)/1_000_000 if known_usage and input_rate is not None and output_rate is not None else None,
        cost_note='仅按用户提供的每百万token单价估算；未完整返回usage时费用未知，以服务商账单为准')
    if reviewed and len(predictions)==len(labels):
        gold=[{'id':i+1,'relevant':g['relevant'],'categories':g['categories'],'label_status':'human_reviewed'} for i,g in enumerate(labels)]
        metrics=evaluate_predictions(gold,predictions)
        metrics['scope']='冻结的'+split+'候选样本；不是全站发现召回率'
        report['metrics']=metrics
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));return report


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--sample-dir',type=Path,required=True);ap.add_argument('--out-dir',type=Path,required=True)
    ap.add_argument('--split',choices=['development','holdout'],default='development');ap.add_argument('--limit',type=int,default=8)
    ap.add_argument('--dry-run',action='store_true');ap.add_argument('--allow-provisional',action='store_true')
    ap.add_argument('--input-rate',type=float);ap.add_argument('--output-rate',type=float)
    ap.add_argument('--sample-ids',nargs='+',help='显式选择当前分组样本；不修改冻结分组')
    args=ap.parse_args(argv);cfg=AppConfig.load();cfg.llm.max_chunks=min(cfg.llm.max_chunks,6)
    report=run_batch(args.sample_dir,args.out_dir,Classifier(cfg),split=args.split,limit=args.limit,dry_run=args.dry_run,
        allow_provisional=args.allow_provisional,input_rate=args.input_rate,output_rate=args.output_rate,sample_ids=args.sample_ids)
    print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['status'] in ('ready','human_reviewed','exploratory') else 2

if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError) as e:print(str(e),file=sys.stderr);raise SystemExit(2)
