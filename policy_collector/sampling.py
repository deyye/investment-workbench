"""Auditable candidate sampling, including excluded and undiscovered reference-list items."""
from __future__ import annotations
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from .config import PROJECT_ROOT
from .evaluation import document_from_row
from .models import Document
from .parser import Parser


def digest(value): return hashlib.sha256(value.encode()).hexdigest()
def read_jsonl(path): return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]
def normalized(value):
    return re.sub(r'[\s《》〔〕\[\]【】（）()“”"：:]', '', unicodedata.normalize('NFKC',value or ''))


def prepare_sample(db, out, limit=20, seed='investment-audit-v1', reference_list=None, source=''):
    if limit<1:raise ValueError('样本数量必须大于0')
    if out.exists():raise ValueError('样本目录已存在')
    known=read_jsonl(PROJECT_ROOT/'gold/zj_v1_20260909/gold.jsonl')
    known_urls={g.get('page_url') for g in known}
    known_numbers={normalized(g['wenhao']) for g in known if g.get('wenhao')}
    known_titles={normalized(g['title']) for g in known if g.get('title')}
    rows=[dict(r) for r in db._conn.execute('SELECT f.*,s.name AS source_name FROM fetch_records f JOIN source_configs s ON s.id=f.source_id ORDER BY f.id')]
    rows=[r for r in rows if (not source or r['source_name']==source) and r['page_url'].startswith('https://')]
    unique={r['page_url']:r for r in rows}
    for row in db._conn.execute('''SELECT o.*,s.name AS source_name FROM discovery_observations o
            JOIN source_configs s ON s.id=o.source_id WHERE o.admitted=0'''):
        row=dict(row)
        if row['page_url'].startswith('https://') and (not source or row['source_name']==source):
            unique.setdefault(row['page_url'],{**row,'status':'list_filtered'})
    reference_count=0
    if reference_list:
        for r in read_jsonl(reference_list):
            if not isinstance(r.get('page_url'),str) or not r['page_url'].startswith('https://'):
                raise ValueError('独立列表必须有官网HTTPS page_url')
            reference_count+=1
            if r['page_url'] not in unique:
                unique[r['page_url']]={'page_url':r['page_url'],'source_name':r.get('source_name','reference'),
                    'title':r.get('title',''),'status':'not_discovered','id':None,'reference_url':r.get('list_url','')}
    buckets=defaultdict(list)
    for row in unique.values():buckets[row['status']].append(row)
    for stage,bucket in buckets.items():bucket.sort(key=lambda r:digest(seed+r['page_url']))
    selected=[]
    while len(selected)<limit and any(buckets.values()):
        for stage in sorted(buckets):
            if buckets[stage] and len(selected)<limit:selected.append(buckets[stage].pop(0))
    samples=[];corpus=[]
    for row in selected:
        p=db.policy_for_url(row['page_url'])
        if p:doc=document_from_row(db._conn,p)
        elif row.get('document_json'):
            raw=json.loads(row['document_json']);doc=Document(**{k:v for k,v in raw.items() if k in Document.__dataclass_fields__})
        elif row.get('raw_path') and Path(row['raw_path']).is_file():
            raw=Path(row['raw_path']).read_bytes();doc=Parser().parse(raw,Path(row['raw_path']).suffix,page_url=row['page_url'])
        else:doc=Document(title=row.get('title',''),page_url=row['page_url'],parse_error='未取得完整材料')
        uid=digest(row['page_url'])[:20]
        family=normalized(doc.wenhao) or normalized(doc.title) or row['page_url']
        historical=(row['page_url'] in known_urls or normalized(doc.wenhao) in known_numbers
                    or normalized(doc.title) in known_titles)
        split='development' if historical or int(digest(seed+family)[:8],16)%5 else 'holdout'
        material_complete=bool(doc.content.strip()) and not doc.parse_error and all(
            a.get('parse_status')=='ok' and a.get('parsed_text','').strip() for a in doc.attachments)
        material_hash=digest(doc.analysis_text)
        try:prediction=json.loads(row.get('classification_json') or '{}')
        except json.JSONDecodeError:prediction={}
        if p:prediction={k:p.get(k) for k in ('is_investment_policy','category','classification_method','need_review')}
        item={'sample_id':uid,'family':family,'split':split,'historical_development':historical,
            'page_url':row['page_url'],'title':doc.title or row.get('title',''),'source_name':row['source_name'],
            'pipeline_stage':row['status'],'material_complete':material_complete,'analysis_sha256':material_hash,
            'prediction':prediction,'relevant':None,'categories':[],'label_status':'unreviewed',
            'reviewer':'','reviewed_at':'','note':''}
        samples.append(item);corpus.append({'sample_id':uid,'analysis_sha256':material_hash,'document':asdict(doc)})
    out.mkdir(parents=True)
    for name,items in [('sample.jsonl',samples),('corpus.jsonl',corpus)]:
        (out/name).write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in items))
    manifest={'seed':seed,'sampled':len(samples),'reference_list_rows':reference_count,
        'scope':'按采集阶段分层抽样，仅报告样本情况；没有独立官网列表不能测列表发现召回率',
        'stage_counts':dict(Counter(s['pipeline_stage'] for s in samples)),
        'split_counts':dict(Counter(s['split'] for s in samples)),
        'complete_materials':sum(s['material_complete'] for s in samples),
        'corpus_sha256':hashlib.sha256((out/'corpus.jsonl').read_bytes()).hexdigest(),
        'immutable_samples':{s['sample_id']:{k:v for k,v in s.items() if k not in ('relevant','categories','label_status','reviewer','reviewed_at','note')} for s in samples}}
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    return {k:v for k,v in manifest.items() if k!='immutable_samples'}


def score_review(path):
    labels=read_jsonl(path);manifest=json.loads((path.parent/'manifest.json').read_text())
    if hashlib.sha256((path.parent/'corpus.jsonl').read_bytes()).hexdigest()!=manifest['corpus_sha256']:
        raise ValueError('材料快照被修改，请重新采样，不得沿用旧标签')
    metrics=Counter();reviewed=0;seen=set()
    for s in labels:
        uid=s['sample_id']
        if uid in seen or uid not in manifest['immutable_samples']:raise ValueError('样本重复或不属于此抽样批次')
        seen.add(uid)
        for k,v in manifest['immutable_samples'][uid].items():
            if s.get(k)!=v:raise ValueError('样本输入或分组被修改：'+uid)
        if s['label_status']!='human_reviewed':continue
        if type(s['relevant']) is not bool or not s['reviewer'].strip() or not s['reviewed_at'].strip():
            raise ValueError('终审标签必须有相关性判断、复核人和日期')
        cats=s['categories']
        if not isinstance(cats,list) or any(c not in ('guide','access','guarantee','incentive') for c in cats) or len(cats)!=len(set(cats)) or bool(cats)!=s['relevant']:
            raise ValueError('相关性与分类标签不一致')
        reviewed+=1
        if not s['relevant']:continue
        stage=s['pipeline_stage']
        if stage=='not_discovered':metrics['list_not_discovered']+=1
        elif stage=='list_filtered':metrics['list_filter_false_exclusion']+=1
        elif stage in ('discovered','downloaded'):metrics['queued_or_unfinished']+=1
        elif not s['material_complete']:metrics['material_failure']+=1
        elif stage=='excluded' or s['prediction'].get('is_investment_policy')=='no':metrics['classification_false_exclusion']+=1
        elif s['prediction'].get('is_investment_policy')=='pending':metrics['classification_pending']+=1
        else:metrics['relevant_retained']+=1
    if seen!=set(manifest['immutable_samples']):
        raise ValueError('样本被删除；请保留未复核样本，不能缩小原始分母')
    return {'sampled':len(labels),'human_reviewed':reviewed,'unreviewed':len(labels)-reviewed,
        'relevant_sample_outcomes':dict(metrics),'reference_list_supplied':bool(manifest['reference_list_rows']),
        'list_recall':None,'scope':'仅分层样本漏收定位，不把零条人工复核或缺少独立列表解释为零漏收；未推算全站召回率'}
