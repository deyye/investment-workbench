import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from policy_collector.config import AppConfig, SourceConfig
from policy_collector.pipeline import Pipeline
from policy_collector.models import Document, Classification
from policy_collector.classifier import Classifier, LLMClassifier
from policy_collector.evaluation import load_gold, resolve_labels, evaluate_predictions, readonly_db
from scripts.eval_llm_compare import snapshot_database, reclassify_with_llm
from scripts.zj_full_ingest_check import prepare_directory, build_verdict, db_stats


@pytest.fixture
def sample(tmp_path):
    cfg = AppConfig.load()
    cfg.data_dir=tmp_path/'source';cfg.db_path=cfg.data_dir/'policy.db';cfg.downloads_dir=cfg.data_dir/'downloads'
    cfg.sources={'zjfgw_gsgg':SourceConfig(name='zjfgw_gsgg',list_url='https://agency.gov.cn/policy/',max_pages=1)}
    p=Pipeline(cfg)
    raw='<meta charset="utf-8"><h1>项目投资管理办法</h1><article><p>发改投资〔2026〕1号</p><p>投资项目给予资金支持和信贷支持，明确监督管理规则。</p></article>'.encode()
    p.ingest_url(cfg.sources['zjfgw_gsgg'],'https://agency.gov.cn/policy/1.html',raw=raw,prefer='rule')
    row=p.db.query_policies()[0]
    g={'id':row['id'],'relevant':True,'categories':['guarantee'],'title':row['title'],
       'wenhao':row['wenhao'],'page_url':row['page_url'],'content_sha256':row['content_sha256'],'label_status':'ai_draft'}
    yield p,cfg,row,g
    p.close()


def test_snapshot_includes_wal_and_refuses_original(sample,tmp_path):
    p,cfg,row,g=sample
    assert Path(str(cfg.db_path)+'-wal').exists()
    original=p.db.get_policy(row['id'])
    with pytest.raises(ValueError):snapshot_database(p.db._conn,cfg.db_path)
    dest=tmp_path/'copy.db';snapshot_database(p.db._conn,dest)
    with readonly_db(dest) as con:
        assert resolve_labels(con,[g])[0][1]['title']==row['title']
    with pytest.raises(ValueError):snapshot_database(p.db._conn,dest)
    assert p.db.get_policy(row['id'])==original


def test_gold_source_binding_beats_ids_and_rejects_changed_content(sample):
    p,cfg,row,g=sample
    assert resolve_labels(p.db._conn,[{**g,'id':999}])[0][1]['id']==row['id']
    with pytest.raises(ValueError,match='来源版本'):resolve_labels(p.db._conn,[{**g,'content_sha256':'0'*64}])
    with pytest.raises(ValueError,match='title'):resolve_labels(p.db._conn,[{**g,'title':'其他文件'}])


def test_comparison_includes_attachments_and_does_not_modify_predictions(sample,tmp_path):
    p,cfg,row,g=sample
    p.db.add_attachment(row['id'],{'name':'办法','url':'https://agency.gov.cn/a.docx','fmt':'docx','parse_status':'ok','parsed_text':'附件规定资金支持。'})
    seen=[]
    def classify(doc,prefer):
        seen.append((prefer,doc.analysis_text))
        return Classification(is_investment_policy='yes',category='guarantee',method='llm' if prefer=='llm' else 'rule')
    fake=SimpleNamespace(classify=classify,llm=SimpleNamespace(available=True))
    dest=tmp_path/'result';dest.mkdir()
    report=reclassify_with_llm(p.db._conn,dest/'copy.db',[g],fake,dest)
    assert seen[0][1]==seen[1][1] and '附件规定资金支持' in seen[1][1]
    assert report['llm_reclassify']['llm_classified']==1 and report['evaluation_status']=='exploratory'
    assert p.db.get_policy(row['id'])['classification_method']==row['classification_method']
    assert len((dest/'predictions.jsonl').read_text().splitlines())==1


def test_model_outage_counts_fallback_not_success(sample,tmp_path):
    p,cfg,row,g=sample
    fake=SimpleNamespace(llm=SimpleNamespace(available=True),classify=lambda doc,prefer:
        Classification(method='rule_fallback' if prefer=='llm' else 'rule',need_review=True))
    dest=tmp_path/'result';dest.mkdir()
    report=reclassify_with_llm(p.db._conn,dest/'copy.db',[g],fake,dest)
    assert report['llm_reclassify']['llm_classified']==0
    assert report['llm_reclassify']['fallback']==1 and not report['run_complete']
    assert report['evaluation_status']=='incomplete' and report['llm']['rule_fallback']==1


def test_dry_run_does_not_call_llm(sample,tmp_path):
    p,cfg,row,g=sample
    def classify(doc,prefer):
        assert prefer=='rule'
        return Classification(need_review=True)
    fake=SimpleNamespace(classify=classify,llm=SimpleNamespace(available=False))
    dest=tmp_path/'result';dest.mkdir()
    report=reclassify_with_llm(p.db._conn,dest/'copy.db',[g],fake,dest,dry_run=True)
    assert report['evaluation_status']=='preflight_only' and 'llm' not in report


def test_full_validation_cannot_pass_empty_partial_or_unfinished_pages(sample):
    p,cfg,row,g=sample
    base={'ingested':0,'updated':0,'duplicates':1,'discovery':{'end_reached':True}}
    final={'fetch_total':1,'fetch_by_status':{'processed':1},'attachments':{'incomplete':0},'runs':[{'status':'ok'}]}
    assert build_verdict([base,base],final)['overall_ok']
    assert not build_verdict([base,base],{**final,'fetch_total':0})['overall_ok']
    assert not build_verdict([base,base],{**final,'fetch_by_status':{'discovered':1}})['overall_ok']
    assert not build_verdict([base,base],{**final,'attachments':{'incomplete':1}})['overall_ok']
    assert not build_verdict([base,{**base,'discovery':{'end_reached':False}}],final)['overall_ok']
    assert not build_verdict([base],final)['overall_ok']
    assert db_stats(cfg.db_path)['policy_current']==1


def test_full_check_never_deletes_nonempty_directory(tmp_path):
    protected=tmp_path/'production';protected.mkdir()
    folder=tmp_path/'results';folder.mkdir();(folder/'policy.db').write_text('keep me')
    with pytest.raises(ValueError):prepare_directory(folder,protected)
    assert (folder/'policy.db').read_text()=='keep me'
    assert prepare_directory(folder,protected,keep=True)==folder
    with pytest.raises(ValueError):prepare_directory(tmp_path,protected,keep=True)


def test_pending_not_correct_negative_and_invalid_gold_rejected():
    label={'id':1,'relevant':False,'categories':[]}
    result=evaluate_predictions([label],[asdict(Classification())])
    assert result['exact_match']==0 and result['decision_coverage']==0 and result['pending']==1
    for bad in [{**label,'relevant':'false'},{**label,'categories':['incentive']},{**label,'id':True}]:
        with pytest.raises(ValueError):evaluate_predictions([bad],[asdict(Classification())])


def test_dataset_is_provisional_and_fully_version_bound():
    labels=load_gold(Path('gold/zj_v1_20260909/gold.jsonl'))
    assert len(labels)==56
    assert all(g['label_status']=='ai_draft' and g['page_url'] and len(g['content_sha256'])==64 for g in labels)


def test_llm_evidence_cannot_be_title_only_or_no_with_categories(sample,monkeypatch):
    _,cfg,_,_=sample
    c=LLMClassifier(cfg,cfg.classification)
    data={'is_investment_policy':'yes','doc_type':'正式政策','category':['guarantee'],
          'need_review':False,'evidence':'标题含资金支持','confidence':.95}
    out=c._validate(Document(title='标题含资金支持',content='内容另见附件'),data,evidence_source='内容另见附件')
    assert out.is_investment_policy=='pending' and not out.category
    out=c._validate(Document(content='不属于项目投资'),{**data,'is_investment_policy':'no','evidence':'不属于项目投资'})
    assert out.is_investment_policy=='pending'


def test_incomplete_material_guard_also_applies_outside_pipeline(sample,monkeypatch):
    _,cfg,_,_=sample
    c=Classifier(cfg)
    monkeypatch.setattr(c,'_classify',lambda *a:Classification(is_investment_policy='no',method='llm'))
    out=c.classify(Document(content='通知详见附件',attachments=[{'parse_status':'failed'}]))
    # 材料不完整：结论必须挂起（不得算已确认），但归入「待补材料」由机器自修，不计入业务待办。
    assert out.todo_type=='material' and out.is_investment_policy=='pending'
    assert out.need_review is False


def test_partial_llm_failure_keeps_known_token_usage(sample,monkeypatch):
    _,cfg,_,_=sample
    monkeypatch.setenv('LLM_API_KEY','test-key')
    cfg.llm.enabled=True;cfg.llm.model='mock';cfg.llm.chunk_chars=500
    c=Classifier(cfg);calls=[]
    def reply(system,user):
        calls.append(user)
        c.llm.client.usage={'input_tokens':10,'output_tokens':3}
        if len(calls)>1:
            c.llm.client.last_error='HTTP 503'
            return None
        return {'is_investment_policy':'yes','category':['guarantee'],'need_review':True,'evidence':'正文正文','confidence':.9}
    monkeypatch.setattr(c.llm.client,'chat_json',reply)
    out=c.classify(Document(content='正文'*800))
    assert out.method=='rule_fallback' and out.input_tokens==20 and out.output_tokens==6


def test_api_rejects_refusal_duplicate_json_and_keeps_retry_usage(sample,monkeypatch):
    import requests
    from policy_collector.llm_client import LLMClient
    _,cfg,_,_=sample
    monkeypatch.setenv('LLM_API_KEY','test-key');cfg.llm.retries=0
    response={'choices':[{'finish_reason':'content_filter','message':{'content':'{}'}}],
              'usage':{'prompt_tokens':10,'completion_tokens':2}}
    class Reply:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def json(self):return response
    monkeypatch.setattr(requests,'post',lambda *a,**kw:Reply())
    monkeypatch.setattr('policy_collector.llm_client.time.sleep',lambda *a:None)
    client=LLMClient(cfg.llm)
    assert client.chat_json('s','u') is None
    response['choices'][0]={'finish_reason':'stop','message':{'content':'{}','refusal':'refused'}}
    assert client.chat_json('s','u') is None and '拒绝' in client.last_error
    response['choices'][0]={'finish_reason':'stop','message':{'content':'{"ok":true,"ok":false}'}}
    cfg.llm.retries=1
    assert client.chat_json('s','u') is None
    assert client.usage=={'input_tokens':20,'output_tokens':4}
