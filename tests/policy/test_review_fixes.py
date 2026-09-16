from pathlib import Path
from types import SimpleNamespace
import pytest
from policy_collector.config import AppConfig, SourceConfig
from policy_collector.pipeline import Pipeline
from policy_collector.models import Classification
from policy_collector.webapp import create_app

BODY='投资项目给予资金支持，明确监督管理规则。项目资金用于基础设施建设，申报单位应当提供完整材料，严格履行审批程序，接受全过程监督和绩效评价，确保项目规范实施。'
RAW=('<meta charset="utf-8"><h1>投资项目支持办法</h1><article>'+BODY+'</article>').encode()
URL='https://agency.gov.cn/policy/1.html'

@pytest.fixture
def pipe(tmp_path):
    cfg=AppConfig.load();cfg.data_dir=tmp_path;cfg.db_path=tmp_path/'db';cfg.downloads_dir=tmp_path/'raw';cfg.llm.enabled=False
    cfg.sources={'test':SourceConfig(name='test',list_url='https://agency.gov.cn/policy/')}
    p=Pipeline(cfg)
    yield p
    p.close()


def ingest(p):return p.ingest_url(p.cfg.sources['test'],URL,raw=RAW)

@pytest.mark.parametrize('review,expected',[(True,'pending'),(False,'excluded')])
def test_negative_review_is_retained(pipe,review,expected):
    pipe.classifier.classify=lambda *a:Classification(is_investment_policy='no',need_review=review,method='llm')
    stats=ingest(pipe)
    assert stats.excluded==int(not review)
    if review:assert pipe.db.query_policies(review_status=expected)[0]['is_investment_policy']=='pending'
    else:assert pipe.db.list_fetch()[0]['status']=='excluded'

@pytest.mark.parametrize('human',[False,True])
def test_model_recovers_once_without_overwriting_human(pipe,human):
    ingest(pipe);row=pipe.db.query_policies()[0]
    if human:pipe.db.audit(row['id'],'adjust',['guarantee'])
    calls=[]
    pipe.classifier.llm=SimpleNamespace(available=True)
    def classify(*a):
        calls.append(1)
        return Classification(is_investment_policy='yes',category='guarantee',method='llm',need_review=False)
    pipe.classifier.classify=classify
    first=ingest(pipe);ingest(pipe)
    assert len(calls)==(0 if human else 1)
    assert first.reclassified==int(not human)
    if human:assert pipe.db.get_policy(row['id'])['review_status']=='adjusted'


def test_missing_model_does_not_retry_unchanged_document(pipe):
    ingest(pipe)
    pipe.classifier.classify=lambda *a:pytest.fail('Unavailable model should not reclassify unchanged records')
    assert ingest(pipe).duplicates==1


def test_search_attachment_and_web_excerpt(pipe):
    ingest(pipe);row=pipe.db.query_policies()[0]
    pipe.db.add_attachment(row['id'],dict(name='资金支持附件',url='https://agency.gov.cn/a.txt',parsed_text='符合条件的项目给予贴息支持。',parse_status='ok'))
    found=pipe.db.query_policies(keyword='贴息')
    assert len(found)==1 and found[0]['matched_attachment']=='资金支持附件'
    client=create_app(pipe.cfg).test_client()
    html=client.get('/policies?q=贴息').get_data(as_text=True)
    assert '命中附件：资金支持附件' in html and '给予贴息支持' in html
    assert not pipe.db.query_policies(keyword='不存在的关键词')

@pytest.mark.parametrize('action',['confirm','adjust'])
def test_business_review_keeps_material_warning(pipe,action):
    """人复核完、但材料还没齐 —— 材料提示不能丢，也不能还占着人的队列。

    ⚠️ 这条断言改过一次（2026-09-14）：原来断言 `need_review==1`，
    但合并后引入 `todo_type` 后，`need_review` 的含义收窄为"**业务**要看的"，
    材料问题由 `todo_type='material'` 承载（与分类环节同一口径）。
    所以现在断言更强的两件事：**出人工队列**（todo_type 不是 review/scope）
    **且**材料缺口照常标记（todo_type='material' 且 parse_requires_review=1）。
    """
    ingest(pipe);row=pipe.db.query_policies()[0]
    pipe.db.update_policy(row['id'],parse_error='正文尚不完整')
    pipe.db.audit(row['id'],action,['guarantee'],'已核对分类，等待补采')
    current=pipe.db.get_policy(row['id'])
    assert current['todo_type']=='material', '材料没齐 → 挂"材料待补"，这是机器/运维的活'
    assert current['todo_type'] not in ('review','scope','candidate'), '不能继续占人的队列'
    assert current['parse_requires_review']==1
    assert current['review_status'] in ('confirmed','adjusted')
    assert pipe.db.query_policies(review_status='pending')
    pipe.db.update_policy(row['id'],parse_error='')
    pipe.db.audit(row['id'],'confirm')
    settled=pipe.db.get_policy(row['id'])
    assert settled['parse_requires_review']==0
    assert settled['todo_type']=='none', '材料补齐后人已复核 → 彻底出队，不再出现在待办清单'


def test_attachment_failure_cannot_be_cleared_by_confirm(pipe):
    ingest(pipe);row=pipe.db.query_policies()[0]
    pipe.db.add_attachment(row['id'],dict(name='缺失附件',url='https://agency.gov.cn/missing.pdf',parse_status='failed'))
    pipe.db.audit(row['id'],'confirm')
    assert pipe.db.get_policy(row['id'])['parse_requires_review']==1


def test_audit_judges_material_by_text_not_by_parse_status(pipe):
    """复核后挂不挂"材料待补"，看**有没有拿到正文**，不看 `parse_status` 是否恰好 ok。

    实测形态（116 条）：政务站常把同一个文件同时发 PDF 与 OFD 两个版本，
    PDF 解析 `ok`、OFD `partial`——**正文拿到了两遍**。若按 `parse_status` 判，
    这类条目会被记成"材料待补"丢给机器，而机器根本无从下手（PDF 那份早就读完了）。
    这正是本项目反复踩的那类错（把"拿到了一半"当成"没拿到"）。
    """
    ingest(pipe);row=pipe.db.query_policies()[0];pid=row['id']
    with pipe.db.tx() as c:
        c.execute("DELETE FROM attachments WHERE policy_id=?",(pid,))
        for name,status in (('规划.pdf','ok'),('规划.ofd','partial')):
            c.execute("INSERT INTO attachments(policy_id,name,parse_status,parsed_text) VALUES(?,?,?,?)",
                      (pid,name,status,'正文段落。'*80))
    pipe.db.update_policy(pid,parse_error='')

    assert pipe.db.material_gaps(pid) is True, '有 partial 附件 → "材料待核对"提示该留'
    pipe.db.audit(pid,'confirm')
    cur=pipe.db.get_policy(pid)
    assert cur['todo_type']=='none', '正文已拿到两遍，不该记成"材料待补"丢给机器'
    assert cur['parse_requires_review']==1, '"材料待核对"是另一回事，照常保留'


def test_reopen_confirmed_and_rejected_with_note_and_csrf(pipe):
    ingest(pipe);row=pipe.db.query_policies()[0]
    pipe.db.audit(row['id'],'confirm')
    client=create_app(pipe.cfg).test_client()
    for action in ('reject','adjust'):
        html=client.get(f"/policies/{row['id']}").get_data(as_text=True)
        assert '重新复核 / 修改分类' in html and 'name="note"' in html
        with client.session_transaction() as sess:token=sess['csrf']
        response=client.post(f"/policies/{row['id']}/review",data={'csrf_token':token,'action':action,'categories':['incentive'],'note':'重新核对：应按项目监管条款分类'})
        assert response.status_code==302
        assert pipe.db.review_history(row['id'])[0]['note']=='重新核对：应按项目监管条款分类'
    assert pipe.db.get_policy(row['id'])['category']=='incentive'
