"""来源适配与模型配置回归；不执行分类效果评测。"""
import json
from pathlib import Path
import pytest
from policy_collector.config import AppConfig,SourceConfig
from policy_collector.collector import Collector,FetchResult
from policy_collector.parser import Parser
from policy_collector.model_settings import save_model_settings,load_model_settings,connection_check

@pytest.fixture
def cfg(tmp_path):
    c=AppConfig.load();c.data_dir=tmp_path;c.downloads_dir=tmp_path/'downloads';c.db_path=tmp_path/'db.sqlite'
    return c

def test_zj_download_attribute_query_and_outside_body_number():
    html='''<meta charset="utf-8"><h1>项目投资办法</h1><span>浙发改能源〔2026〕116号</span><article>通知正文：具体要求详见附件。<a download="办法.wps" href="/api/download?fileUrl=public-id&amp;fileName=test.wps">附件</a></article>'''
    doc=Parser().parse(html.encode(),'html','https://fzggw.zj.gov.cn/a.html')
    assert doc.wenhao=='浙发改能源〔2026〕116号' and doc.attachments[0]['fmt']=='wps'
    assert 'fileName=test.wps' in doc.attachments[0]['url']
    no_attr=html.replace('download="办法.wps"','')
    assert Parser().parse(no_attr.encode(),'html','https://fzggw.zj.gov.cn/a.html').attachments[0]['fmt']=='wps'

def test_model_settings_round_trip_secret_not_in_repr(cfg,monkeypatch):
    for k in ['LLM_API_KEY','LLM_BASE_URL','LLM_MODEL','DASHSCOPE_API_KEY']:monkeypatch.delenv(k,raising=False)
    path=save_model_settings(cfg,api_key='test-secret')
    assert path.parent==cfg.data_dir and cfg.llm.api_key=='test-secret'
    assert 'test-secret' not in repr(cfg.llm)
    assert cfg.llm.model=='qwen-plus' and cfg.llm.enable_thinking is False
    save_model_settings(cfg,api_key='')
    assert cfg.llm.api_key=='test-secret'
    with pytest.raises(ValueError,match='重新填写'):
        save_model_settings(cfg,'custom','https://other.example/v1','other')
    monkeypatch.setenv('LLM_API_KEY','env-secret');monkeypatch.setenv('LLM_MODEL','env-model')
    load_model_settings(cfg)
    assert cfg.llm.api_key=='env-secret' and cfg.llm.effective_model=='env-model'


def test_model_page_does_not_echo_key_and_save_works(cfg,monkeypatch):
    from policy_collector.webapp import create_app
    for k in ['LLM_API_KEY','DASHSCOPE_API_KEY']:monkeypatch.delenv(k,raising=False)
    client=create_app(cfg).test_client()
    assert client.get('/settings/model').status_code==200
    with client.session_transaction() as sess:token=sess['csrf']
    r=client.post('/settings/model',data={'csrf_token':token,'provider':'dashscope','api_key':'test-secret'},follow_redirects=True)
    assert r.status_code==200 and b'test-secret' not in r.data and cfg.llm.api_key=='test-secret'
    (cfg.data_dir/'llm.local.json').unlink();cfg.llm.local_api_key=''
    assert connection_check(cfg)['ok'] is False


def test_zhejiang_partial_pages_remain_queued_and_mark_failure(cfg, monkeypatch):
    from policy_collector.pipeline import Pipeline
    cfg.fetch.retries = 0
    cfg.sources = {'zj': SourceConfig(name='zj', list_format='zj_unit', max_pages=3,
        list_url='https://fzggw.zj.gov.cn/col/list.html', include=['/art/'])}
    page = b'''<script src="/AuthorizedRead/unitbuild.js" url="/api-gateway/build" queryData="{'tagId':'information'}"></script>'''
    requested = []
    def fetch(url, params=None):
        if params is None:
            return FetchResult(content=page)
        requested.append(json.loads(params['paramJson'])['pageNo'])
        if len(requested) == 2:
            return FetchResult(ok=False, error='HTTP 503')
        return FetchResult(content=json.dumps({'success':True, 'data':{'html':
            '<a href="/art/1.html" title="完整标题">完整...</a>'}}).encode())
    with_pipe = Pipeline(cfg)
    try:
        monkeypatch.setattr(with_pipe.collector, 'fetch', fetch)
        with_pipe.sync_sources()
        stats = with_pipe.discover(cfg.sources['zj'])
        assert stats.discovered == 1 and stats.failed == 1
        row = with_pipe.db.get_source('zj')
        assert '第2页' in row['last_error'] and not row['last_success_at']
        assert with_pipe.db.list_fetch()[0]['title'] == '完整标题'
        assert requested == [1, 2]
    finally:
        with_pipe.close()


def test_zhejiang_repeated_pages_and_untrusted_gateway(cfg, monkeypatch):
    from policy_collector.collector import discover_zj_unit_links, DiscoveryError
    page = b'''<script src="/AuthorizedRead/unitbuild.js" url="/api-gateway/build" queryData="{'tagId':'information'}"></script>'''
    src = SourceConfig(name='zj', list_url='https://fzggw.zj.gov.cn/col/list.html', include=['/art/'])
    collector = Collector(cfg)
    payload = json.dumps({'success':True,'data':{'html':'<a href="/art/1.html">政策</a>'}}).encode()
    monkeypatch.setattr(collector, 'fetch', lambda *a, **kw: FetchResult(content=payload))
    with pytest.raises(DiscoveryError, match='完全重复') as error:
        discover_zj_unit_links(collector, src, page, src.list_url, max_pages=3)
    assert len(error.value.links) == 1
    with pytest.raises(ValueError, match='同源'):
        discover_zj_unit_links(collector, src, page.replace(b'/api-gateway/', b'https://evil.test/api-gateway/'), src.list_url)
    collector.close()


def test_hunan_inline_number_signature_and_clean_body():
    html = '''<meta charset="utf-8"><h1>关于延长项目管理办法有效期的通知</h1>
        <div>网站首页 信息公开 发布机构：湖南省人民政府</div>
        <div class="tys-main-zt-show"><p><font>湘发改法规规〔</font>2026<font>〕471号</font></p>
        <p>继续执行投资项目管理办法，优化资金支持与监管。</p>
        <p>湖南省发展改革委员会</p><p>湖南省公共资源交易中心</p><p>2026年9月8日</p></div><div>网站地图 联系我们</div>'''
    doc = Parser().parse(html.encode(), 'html', 'https://fgw.hunan.gov.cn/a.html')
    assert doc.wenhao == '湘发改法规规〔2026〕471号'
    assert '网站地图' not in doc.content and '网站首页' not in doc.content
    assert doc.issuing_authority == '湖南省发展改革委员会、湖南省公共资源交易中心'
    assert doc.doc_date == '2026-09-08'


def test_rule_scope_keeps_general_rules_not_individual_approvals(cfg):
    from policy_collector.classifier import RuleClassifier
    from policy_collector.models import Document
    rule = RuleClassifier(cfg.classification)
    for title in ['企业投资项目核准和备案管理办法', '关于建立投资项目联席会议制度的通知']:
        result = rule.classify(Document(title=title, content='适用于投资项目管理，明确审批权限和资金支持。'))
        assert result.doc_type == '正式政策' and result.is_investment_policy == 'yes'
    result = rule.classify(Document(title='关于某市轨道交通项目可行性研究报告的批复', content='同意项目投资。'))
    assert result.doc_type == '项目批复' and result.is_investment_policy == 'no'
    result = rule.classify(Document(title='关于报送机关职工运动会报名表的通知', content='请各处室报名。'))
    assert result.is_investment_policy != 'yes'


def test_dotenv_blank_comments_and_environment_precedence(tmp_path, monkeypatch):
    from policy_collector.config import _load_dotenv
    monkeypatch.delenv('LLM_BASE_URL', raising=False)
    monkeypatch.setenv('LLM_MODEL', 'existing')
    monkeypatch.delenv('LOCAL_TEST_KEY', raising=False)
    path = tmp_path / 'config.env'
    path.write_text('LLM_BASE_URL=\nLLM_MODEL=qwen-plus # comment\nLOCAL_TEST_KEY="abc#123" # comment\n')
    _load_dotenv(path)
    import os
    assert 'LLM_BASE_URL' not in os.environ and os.environ['LLM_MODEL'] == 'existing'
    assert os.environ['LOCAL_TEST_KEY'] == 'abc#123'


def test_fujian_number_outside_body_and_shaanxi_editor():
    html = '''<meta charset="utf-8"><h1>虚拟电厂建设运行管理办法</h1>
        <div class="rules_tit1">闽发改规〔2026〕4号</div>
        <div class="TRS_UEDITOR"><p>投资项目资金支持和监管要求。明确建设运行管理流程。</p></div>
        <div>网站地图</div>'''
    doc = Parser().parse(html.encode(), 'html', 'https://fgw.fujian.gov.cn/example.htm')
    assert doc.wenhao == '闽发改规〔2026〕4号' and not doc.parse_error
    assert '网站地图' not in doc.content


def test_unknown_body_layout_requires_review():
    from policy_collector.pipeline import RunStats
    doc = Parser().parse('<meta charset="utf-8"><h1>政策通知</h1><div>无法定位正文容器的网页内容</div>'.encode(), 'html')
    assert doc.parse_error and RunStats(documents_incomplete=1).has_errors
    assert RunStats(attachments_unparsed=1).has_errors
