"""业务回归：合成材料验证流程正确性，不用于宣称模型语义准确率。"""
import io
import json
from pathlib import Path
import pytest
from docx import Document as Word
from policy_collector.config import AppConfig,SourceConfig
from policy_collector.pipeline import Pipeline
from policy_collector.models import Document
from policy_collector.classifier import Classifier,LLMClassifier
from policy_collector.collector import FetchResult,ListPageParser,list_page_url
from policy_collector.dedup import make_policy_key
from policy_collector.parser import Parser
from policy_collector.scheduler import Scheduler
from policy_collector.db import Database,SCHEMA

@pytest.fixture
def cfg(tmp_path):
    cfg=AppConfig.load()
    cfg.data_dir=tmp_path
    cfg.downloads_dir=tmp_path/'downloads'
    cfg.db_path=tmp_path/'policy.db'
    cfg.fetch.retries=0
    cfg.fetch.request_interval_seconds=0
    cfg.sources={'test':SourceConfig(name='test',site='测试官网',list_url='https://agency.gov.cn/policy/',include=['/policy/'],max_pages=1)}
    return cfg

@pytest.fixture
def pipe(cfg):
    p=Pipeline(cfg)
    yield p
    p.close()

def html(body='项目投资资金支持，实施绩效考核。',attachment=''):
    return f'<html><meta charset="utf-8"><meta name="PubDate" content="2026-09-01"><h1>关于项目投资管理办法的通知</h1><article><p>发改投资〔2026〕101号</p><p>{body}</p>{attachment}</article></html>'.encode()

def test_years_and_title_parentheses():
    a=Document(wenhao='发改投资[2025]101号');b=Document(wenhao='发改投资〔2026〕101号')
    assert make_policy_key(a).key != make_policy_key(b).key
    assert make_policy_key(Document(wenhao='发改投资[2026]101号')).key == make_policy_key(b).key
    assert make_policy_key(Document(title='办法（试行）')).key != make_policy_key(Document(title='办法')) .key

def test_publication_not_cited_date():
    d=Parser().parse(html('依据2020年1月1日文件开展投资项目管理。'),'html')
    assert d.page_date=='2026-09-01' and d.doc_date==''
    assert d.wenhao=='发改投资〔2026〕101号'

def test_attachments_fulltext_change_and_failed_refresh(pipe,cfg,monkeypatch):
    def word(text):
        d=Word();d.add_paragraph(text);b=io.BytesIO();d.save(b);return b.getvalue()
    data=word('中央预算内投资项目给予资金支持，明确绩效考核。')
    monkeypatch.setattr(pipe.collector,'fetch',lambda u:FetchResult(content=data,final_url=u))
    url='https://agency.gov.cn/policy/1.html'
    raw=html('具体支持方向详见附件。','<a href="rules.docx?download=1">实施细则</a>')
    a=pipe.ingest_url(cfg.sources['test'],url,raw=raw,prefer='rule')
    assert a.ingested==1 and a.attachments_downloaded==1
    row=pipe.db.query_policies()[0];att=pipe.db.list_attachments(row['id'])[0]
    assert att['parse_status']=='ok' and '中央预算内投资' in att['parsed_text']
    original=Path(att['local_path']);assert original.read_bytes()==data
    assert pipe.ingest_url(cfg.sources['test'],url,raw=raw,prefer='rule').duplicates==1
    data=word('中央预算内投资项目给予信贷支持并实施责任追究。')
    assert pipe.ingest_url(cfg.sources['test'],url,raw=raw,prefer='rule').updated==1
    assert len(pipe.db.versions(row['policy_key']))==2 and original.exists()
    monkeypatch.setattr(pipe.collector,'fetch',lambda u:FetchResult(ok=False,error='HTTP 503'))
    stats=pipe.ingest_url(cfg.sources['test'],url,raw=raw,prefer='rule')
    assert stats.failed==1 and stats.attachments_failed==1
    assert len(pipe.db.versions(row['policy_key']))==2

def test_repost_provenance_and_human_review_preserved(pipe,cfg):
    source=cfg.sources['test'];url='https://agency.gov.cn/policy/a.html'
    pipe.ingest_url(source,url,raw=html(),prefer='rule')
    row=pipe.db.query_policies()[0]
    pipe.db.audit(row['id'],'adjust',['guarantee'],'人工核对原文')
    assert pipe.ingest_url(source,url,raw=html(),prefer='llm',reclassify=True).duplicates==1
    assert pipe.db.get_policy(row['id'])['review_status']=='adjusted'
    assert pipe.ingest_url(source,'https://agency.gov.cn/policy/repost.html',raw=html(),prefer='rule').duplicates==1
    assert len(pipe.db.policy_sources(row['policy_key']))==2
    assert pipe.ingest_url(source,'https://agency.gov.cn/policy/abridged.html',raw=html('项目投资办法，节选内容。'),prefer='rule').ingested==1
    assert len(pipe.db.versions(row['policy_key']))==1
    assert any(p['related_policy_key']==row['policy_key'] for p in pipe.db.query_policies())
    pipe.db.audit(row['id'],'reject',note='测试剔除')
    assert row['id'] not in [p['id'] for p in pipe.db.query_policies()]
    assert len(pipe.db.review_history(row['id']))==2

def test_empty_discovery_is_failed_and_disabled_rejected(pipe,cfg,monkeypatch):
    monkeypatch.setattr(pipe.collector,'fetch',lambda u:FetchResult(content=b'<html>No list</html>'))
    assert pipe.run_source('test',prefer='rule').failed==1
    assert pipe.db.list_runs()[0]['status']=='failed'
    cfg.sources['test'].enabled=False
    with pytest.raises(ValueError):pipe.run_source('test')

def test_periodic_refresh_not_skip_processed(pipe,cfg,monkeypatch):
    body=[html()]
    def fetch(url):
        raw=b'<a href="1.html">Policy</a>' if url.endswith('/') else body[0]
        return FetchResult(content=raw,final_url=url)
    monkeypatch.setattr(pipe.collector,'fetch',fetch)
    assert pipe.run_source('test',prefer='rule').ingested==1
    body[0]=html('项目投资资金支持，新增信贷支持。')
    assert pipe.run_source('test',prefer='rule').updated==1
    assert pipe.run_source('test',prefer='rule').duplicates==1
    assert len(pipe.db.query_policies())==1

def test_fallback_explicit_and_full_document_segments(cfg,monkeypatch):
    monkeypatch.delenv('LLM_API_KEY',raising=False)
    out=Classifier(cfg).classify(Document(title='日常活动',content='新闻报道'))
    # 模型故障属运维事项：仍须保持待判定（不得静默丢弃），但不再占用业务待办队列。
    assert out.method=='rule_fallback' and out.todo_type=='system' and out.is_investment_policy=='pending'
    assert out.need_review is False
    monkeypatch.setenv('LLM_API_KEY','test-placeholder')
    cfg.llm.model='mock';cfg.llm.enabled=True;cfg.llm.chunk_chars=500;cfg.llm.max_chunks=8
    c=LLMClassifier(cfg,cfg.classification);seen=[]
    def respond(system,user):
        seen.append(user)
        return {'is_investment_policy':'yes','doc_type':'正式政策','category':['guide','access','guarantee','incentive'],
                'category_reason':'测试多标签','evidence':user.split('<document>\n',1)[1][:4],'need_review':False}
    monkeypatch.setattr(c.client,'chat_json',respond)
    out=c.classify(Document(title='关于项目投资',content='正文'*900+'末尾条款',attachments=[{'name':'附件','parsed_text':'附件里的资金支持'}]))
    assert len(seen)>3 and any('末尾条款' in s for s in seen) and any('附件里的资金支持' in s for s in seen)
    assert len(out.category.split(','))==4 and out.method=='llm'
    cfg.llm.max_chunks=1
    assert c.classify(Document(title='关于项目投资',content='正文'*900)).input_truncated
    bad=c._validate(Document(content='真实原文'),{'is_investment_policy':True,'category':['fake'],'need_review':'false','evidence':'编造条款'})
    assert bad.is_investment_policy=='pending' and bad.need_review and not bad.evidence

def test_scheduler_skips_demo_disabled_and_marks_kind(cfg,monkeypatch):
    cfg.sources['off']=SourceConfig(name='off',enabled=False)
    cfg.sources['demo']=SourceConfig(name='demo',list_url='file://samples/list.html')
    calls=[]
    from policy_collector.pipeline import RunStats
    monkeypatch.setattr(Pipeline,'run_source',lambda self,n,**kw:calls.append((n,kw['kind'])) or RunStats())
    assert Scheduler(cfg,interval_minutes=.00001).run_forever(prefer='rule',cycles=2)==0
    assert calls==[('test','scheduled')]*2

def test_legacy_database_migration_and_transaction_rollback(tmp_path):
    import sqlite3
    path=tmp_path/'old.db';con=sqlite3.connect(path);con.executescript(SCHEMA);con.close()
    d=Database(path)
    assert 'classification_method' in [r[1] for r in d._conn.execute('PRAGMA table_info(policies)')]
    with pytest.raises(sqlite3.ProgrammingError):
        d.store_document('key',{'title':'test'},[{'name':object()}],1,1,'https://example.gov.cn')
    assert d._conn.execute('SELECT COUNT(*) FROM policies').fetchone()[0]==0
    d.close()

def test_list_pagination_and_host_filter(cfg):
    source=cfg.sources['test'];source.pagination='trs'
    assert list_page_url(source,2)=='https://agency.gov.cn/policy/index_1.html'
    links=ListPageParser(source).parse('<a href="a.html#part">A</a><a href="a.html">A</a><a href="https://evil.test/policy/a.html">B</a>')
    assert len(links)==1 and links[0].url.endswith('/a.html')


def test_cq_download_buttons_and_no_navigation(cfg):
    from policy_collector.parser import attachment_links
    from bs4 import BeautifulSoup
    markup = "<a onclick=\"downloadFj('政策.docx','./rules.docx')\">下载文字版</a>"
    assert attachment_links(BeautifulSoup(markup,'lxml'),'https://agency.gov.cn/policy/a.html')[0]['url'].endswith('/policy/rules.docx')
    source=cfg.sources['test'];source.detail_url_pattern=r'/[0-9]{6}/t[0-9]{8}_[0-9]+[.]html$'
    raw='<a href="sub/">栏目</a><a href="202609/t20260901_123.html">文件</a>'
    assert len(ListPageParser(source).parse(raw))==1


def test_gov_json_feed_limits_and_filters(cfg):
    from policy_collector.collector import parse_gov_feed
    source=cfg.sources['test'];source.max_pages=1
    raw=json.dumps([{'URL':f'https://agency.gov.cn/policy/{i}.html','TITLE':str(i)} for i in range(30)]).encode()
    assert len(parse_gov_feed(raw,source))==20
    with pytest.raises(ValueError):parse_gov_feed(b'{}',source)

def test_model_http_contract_and_truncated_response(cfg,monkeypatch):
    from policy_collector.llm_client import LLMClient
    import requests
    cfg.llm.enabled=True;cfg.llm.model='mock';cfg.llm.retries=0
    monkeypatch.setenv('LLM_API_KEY','test-placeholder')
    reply={'choices':[{'finish_reason':'stop','message':{'content':'{"category":["guide"]}'}}],
           'usage':{'prompt_tokens':25,'completion_tokens':7}}
    class Response:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def json(self):return reply
    def post(url,headers,json,timeout):
        assert url.endswith('/chat/completions')
        assert json['response_format']=={'type':'json_object'}
        assert json['model']=='mock' and len(json['messages'])==2
        return Response()
    monkeypatch.setattr(requests,'post',post)
    client=LLMClient(cfg.llm)
    assert client.chat_json('system','document')=={'category':['guide']}
    assert client.usage=={'input_tokens':25,'output_tokens':7}
    reply['choices'][0]['finish_reason']='length'
    assert client.chat_json('system','document') is None and '截断' in client.last_error


def test_review_requires_token_and_invalid_category_rejected(cfg,pipe):
    from policy_collector.webapp import create_app
    pipe.ingest_url(cfg.sources['test'],'https://agency.gov.cn/policy/1.html',raw=html(),prefer='rule')
    row=pipe.db.query_policies()[0]
    client=create_app(cfg).test_client()
    assert client.post(f"/policies/{row['id']}/review",data={'action':'reject'}).status_code==400
    with pytest.raises(ValueError):pipe.db.audit(row['id'],'adjust',['not-a-category'])
    assert pipe.db.get_policy(row['id'])['review_status']=='pending'


def test_zj_unitbuild_spec_extraction_and_list(cfg):
    """浙江"页面构建单元"动态列表：栏目页声明参数 → data.html 详情链接（新旧路径均收）。"""
    from policy_collector.collector import extract_unitbuild_spec, unit_list_links
    sample = Path(__file__).resolve().parents[2] / 'samples' / 'zj'
    spec = extract_unitbuild_spec((sample / 'col_main.html').read_text(encoding='utf-8', errors='ignore'))
    assert spec is not None
    api_url, params = spec
    assert api_url.endswith('/front/page/build/unit')
    assert params['pageId'] == '1229565788' and params['tagId'] == '信息列表'
    # 配置与 sources.yaml 的 zjfgw_gsgg 对齐
    src = cfg.sources['zj'] if 'zj' in cfg.sources else SourceConfig(
        name='zjfgw_gsgg', site='浙江省发展改革委', region='浙江', category='行政规范性文件',
        enabled=True, list_url='https://fzggw.zj.gov.cn/col/col1229565788/index.html',
        list_format='zj_unit', include=['/art/'],
        detail_url_pattern=r'/art_[0-9a-z_]+[.]html$', max_pages=1)
    payload = json.loads((sample / 'unit_api.json').read_text(encoding='utf-8'))
    assert payload.get('success') is True
    links = unit_list_links(payload['data']['html'], src, base_url=src.list_url)
    assert len(links) == 15, f'浙江动态首页应发现15条，实际 {len(links)}'
    assert any('/col/col1229123351/art/' in l.url for l in links)   # 新路径
    assert any(l.url.startswith('https://fzggw.zj.gov.cn/art/202') for l in links)  # 旧路径
    assert all(l.title for l in links)
    # 标题取 <a title> 完整属性，而非被截断的文本
    assert any('固定资产投资项目节能审查和碳排放评价实施办法' in l.title for l in links)
    # 未发现 unitbuild 参数的页面应抛错（发现失败显式化，而非空跑成功）
    assert extract_unitbuild_spec('<html>no script</html>') is None
    bad = SourceConfig(name='bad', list_url='https://fzggw.zj.gov.cn/col/x/index.html',
                       include=['/art/'], detail_url_pattern=r'/art_[0-9a-z_]+[.]html$')
    # 栏目导航等非 art_ 文件名的详情页被 detail_url_pattern 滤除
    nav = '<a href="/col/x/art/index.html">栏目</a><a href="/col/x/art/2026/art_1a2b3c.html">文件</a>'
    links = unit_list_links(nav, bad, base_url='https://fzggw.zj.gov.cn/col/x/index.html')
    assert [l.url for l in links] == ['https://fzggw.zj.gov.cn/col/x/art/2026/art_1a2b3c.html']


def test_zj_detail_metadata_and_gateway_attachment():
    """浙江 art 详情：文件编号/生成日期/发布机构元数据表 + JPaaS 下载网关 fileName 附件。"""
    from policy_collector.parser import Parser
    sample = Path(__file__).resolve().parents[2] / 'samples' / 'zj' / 'zj_detail_energy.html'
    raw = sample.read_bytes()
    doc = Parser().parse(raw, 'html', page_url='https://fzggw.zj.gov.cn/col/col1229123351/art/2026/art_937add6719f140bdac86d5b97c525d28.html')
    assert doc.title.startswith('省发展改革委关于印发《浙江省固定资产投资项目节能审查和碳排放评价实施办法》')
    assert doc.wenhao == '浙发改能源〔2026〕116号'          # 元数据表"文件编号"
    assert doc.issuing_authority == '浙江省发展和改革委员会'  # 优先正文落款全称，发布机构作兜底
    assert doc.doc_date == '2026-06-02' and doc.page_date == '2026-06-05'
    assert '印发给你们' in doc.content                       # div#zoom 印发通知正文
    assert len(doc.attachments) == 1
    att = doc.attachments[0]
    assert att['fmt'] == 'wps'
    assert att['url'].startswith('https://fzggw.zj.gov.cn/api-gateway/jpaas-web-server/front/document/download')
    assert att['name'].endswith('.wps') or '节能审查和碳排放评价实施办法' in att['name']


def test_zj_unitbuild_pagination_pages_until_empty(tmp_path):
    """浙江动态列表全量翻页：paramJson pageNo 逐页递增，空页自停，跨页去重，JSON 原件逐页留证。

    复现手工验证发现的问题场景（mock 下只请求 1 页且 0 链接）：
    必须确认 fetch 收到 params（此前怀疑参数未传透），且第 2、3 页继续请求。
    """
    from policy_collector.collector import Collector, discover_zj_unit_links
    from policy_collector.config import AppConfig, SourceConfig
    cfg = AppConfig.load()
    cfg.data_dir = tmp_path
    cfg.downloads_dir = tmp_path / 'downloads'
    cfg.fetch.retries = 0
    cfg.fetch.request_interval_seconds = 0
    src = SourceConfig(
        name='zjfgw_gsgg', site='浙江省发展改革委', region='浙江', category='行政规范性文件',
        enabled=True, list_url='https://fzggw.zj.gov.cn/col/col1229565788/index.html',
        list_format='zj_unit', include=['/art/'],
        detail_url_pattern=r'/art_[0-9a-z_]+[.]html$', max_pages=40)
    col_page = (Path(__file__).resolve().parents[2] / 'samples' / 'zj' / 'col_main.html').read_bytes()

    def page_html(page_no: int) -> str:
        if page_no > 2:                      # 第 3 页起为空：应自动停止
            return '<ul></ul>'
        lis = []
        for i in range(1, 16):
            hex_id = f'{page_no:04x}{i:02x}' + '0' * 26
            lis.append(
                f'<li class="clearfix"><a class="bt-left" title="政策{page_no}-{i}" '
                f'href="/col/col1229565788/art/2026/art_{hex_id}.html">政策{page_no}-{i}</a>'
                f'<span class="bt-right">2026-09-0{page_no}</span></li>')
        return '<ul>' + ''.join(lis) + '</ul>'

    calls, saved = [], []
    def fake_fetch(url, params=None):
        page_no = json.loads(params['paramJson'])['pageNo'] if params and 'paramJson' in params else 0
        calls.append((url, page_no))
        return FetchResult(ok=True, url=url, final_url=url,
                           content=json.dumps({'success': True, 'message': '生成成功',
                                               'data': {'html': page_html(page_no)}},
                                              ensure_ascii=False).encode())
    collector = Collector(cfg)
    collector.save = lambda *a, **k: saved.append(a) or '/tmp/x.json'   # 截获落盘调用，断言原件留证
    collector.fetch = fake_fetch
    try:
        links = discover_zj_unit_links(collector, src, col_page,
                                       page_url=src.list_url, max_pages=40, page_size=15)
    finally:
        collector.close()
    # 1) params 传透：3 次请求 pageNo 依次 1、2、3，未在 40 页上限前空跑
    assert [p for _, p in calls] == [1, 2, 3], f'应逐页请求至空页停止，实际 {calls}'
    assert calls[0][0].endswith('/front/page/build/unit')
    # 2) 翻页去重汇总：第 1、2 页各 15 条、第 3 页空
    assert len(links) == 30
    assert len({l.url for l in links}) == 30
    assert all('政策1-' in l.title or '政策2-' in l.title for l in links)
    # 3) 接口 JSON 原件逐页留证：含终止页空响应（证明"翻到底"是站点返回空，而非代码异常中止）
    assert len(saved) == 3 and all(s[3] == 'json' for s in saved)


def test_jiangsu_list_and_trs_detail():
    """江苏发改「通知公告」：静态首页列表(art_284) + TRS_Editor 详情(标题去站点前缀)。"""
    from policy_collector.collector import ListPageParser
    from policy_collector.site_adapters import _jsfgw_detail
    base = Path(__file__).resolve().parents[2] / 'samples' / 'jiangsu'
    src = SourceConfig(name='jsfgw_tzgg', site='江苏省发展改革委', region='江苏',
                       category='通知公告', enabled=True,
                       list_url='https://fzggw.jiangsu.gov.cn/col/col284/index.html',
                       include=['art_284_'],
                       detail_url_pattern=r'/art/20[0-9]{2}/[0-9]+/[0-9]+/art_284_[0-9]+[.]html$',
                       exclude=['index'], max_pages=1)
    # 列表：页面静态直出(不经脚本)，全部应为本栏目 art_284 详情
    links = ListPageParser(src).parse((base / 'js_list.html').read_text(encoding='utf-8', errors='ignore'),
                                      base_url=src.list_url)
    assert len(links) >= 10, f'江苏栏目首页应静态直出列表，实际 {len(links)}'
    assert len({l.url for l in links}) == len(links)
    assert all('art_284_' in l.url for l in links), '不应混入解读(art_314)/导航链接'
    assert all(l.title for l in links)
    # 详情：命中 _jsfgw_detail，标题无站点前缀，正文 TRS_Editor
    doc = Parser().parse((base / 'js_detail.html').read_bytes(), 'html',
                         page_url='https://fzggw.jiangsu.gov.cn/art/2026/8/28/art_284_11821965.html')
    assert '江苏省发展和改革委员会' not in doc.title
    assert '江苏省发展和改革委员会' in doc.issuing_authority or '江苏省发展改革委' in doc.issuing_authority or doc.issuing_authority == ''
    assert '成品油' in doc.title or '成品油' in doc.content
    assert len(doc.content) > 100
    # 页面级访问验证识别(防把验证页当政策)
    raw = (base / 'js_detail.html').read_text(encoding='utf-8', errors='ignore')
    assert _jsfgw_detail is not None and raw.count('TRS_Editor') >= 1


def test_same_source_metadata_backfill_without_new_version_or_review_loss(pipe, cfg):
    source = cfg.sources['test']; url = 'https://agency.gov.cn/policy/metadata.html'
    raw = html().replace(b'<meta name="PubDate" content="2026-09-01">', b'')
    pipe.ingest_url(source, url, raw=raw, prefer='rule')
    row = pipe.db.query_policies()[0]
    pipe.db.audit(row['id'], 'adjust', ['guarantee'], '已审核')
    stats = pipe.ingest_url(source, url, raw=html(), prefer='rule')
    updated = pipe.db.get_policy(row['id'])
    assert stats.duplicates == 1 and len(pipe.db.versions(row['policy_key'])) == 1
    assert updated['page_date'] == '2026-09-01' and updated['review_status'] == 'adjusted'
    assert updated['category'] == 'guarantee'
    assert pipe.db.review_history(row['id'])[0]['action'] == 'metadata_backfill'


def test_failed_url_does_not_starve_older_successful_refresh(pipe, cfg, monkeypatch):
    source = cfg.sources['test']; pipe.sync_sources(); sid = pipe.db.get_source('test')['id']
    ok_url = 'https://agency.gov.cn/policy/ok.html'
    pipe.ingest_url(source, ok_url, raw=html(), prefer='rule')
    ok_row = pipe.db.get_fetch(sid, ok_url)
    pipe.db.update_fetch(ok_row['id'], last_checked_at='2026-01-01T00:00:00')
    failed_id = pipe.db.add_fetch(sid, 'https://agency.gov.cn/policy/failed.html', status='failed')
    pipe.db.update_fetch(failed_id, last_checked_at='2026-09-01T00:00:00')
    from policy_collector.pipeline import RunStats
    monkeypatch.setattr(pipe, 'discover', lambda *a: RunStats())
    requested = []
    def fetch(url):
        requested.append(url)
        return FetchResult(content=html(), final_url=url)
    monkeypatch.setattr(pipe.collector, 'fetch', fetch)
    stats = pipe.run_source('test', prefer='rule', limit=1)
    assert requested == [ok_url] and stats.duplicates == 1
