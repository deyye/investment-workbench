import base64
import json
from pathlib import Path
from urllib.parse import quote_plus
import pytest
from policy_collector.classifier import RuleClassifier, LLMClassifier
from policy_collector.collector import (Collector, FetchResult, DiscoveryError,
    discover_jpage_links, next_page_link)
from policy_collector.config import AppConfig, SourceConfig
from policy_collector.models import Document
from policy_collector.pipeline import Pipeline


def config(tmp_path):
    cfg=AppConfig.load();cfg.data_dir=tmp_path;cfg.downloads_dir=tmp_path/'raw';cfg.db_path=tmp_path/'db.sqlite'
    return cfg


@pytest.mark.parametrize('title,verdict,why',[
    # —— 事务性公告：出现即判否，**反误杀豁免不适用**（这正是它们此前漏网的原因）
    ('重庆市粮食和物资储备局关于比选《成渝区域粮食应急保障中心项目实施方案》研究承担单位的公告', 'no',
     '比选公告。标题里的"方案"旧规则会触发豁免、照样判收'),
    ('2026年重点用能单位节能降碳诊断支撑服务项目中选结果公告', 'no', '中选结果'),
    ('江西省优化营商环境工作领导小组办公室关于招募第三届省优化营商环境咨询专家及社会监督员的公告', 'no', '招募公告'),
    ('四川省发展和改革委员会机关服务中心办公家具采购项目（第二次）比选公告', 'no', '采购比选'),
    ('关于2026年“数据要素×”大赛江西分赛决赛项目获奖名单的公示', 'no', '获奖公示'),
    ('重庆市“十五五”可再生能源发展规划（2026-2030年）生态环境影响评价公众参与第一次信息公示', 'no',
     '环评公众参与公示'),
    # —— 同样含这些词的**真政策**，不能被误杀
    ('市发展改革委 市工业和信息化局关于印发市级零碳园区培育建设名单（第一批）的通知', 'yes',
     '园区建设名单属引导类。⚠️ 所以「名单」绝不能进排除词表'),
    ('关于印发《国家级零碳园区建设名单(第一批)》的通知', 'yes', '同上'),
    ('关于印发《XX市投资项目联席会议制度》的通知', 'yes', '含"会议"但是政策（反误杀豁免的意义）'),
    ('关于印发《政府投资项目采购管理办法》的通知', 'yes', '含"采购"但是政策'),
])
def test_transactional_notices_are_excluded_without_killing_real_policies(tmp_path,title,verdict,why):
    """事务性公告判否、同词真政策判收——两类排除词分开这件事的回归锁。

    2026-09-14 实测：库里 54 条标题含"比选/中选/招募/获奖/公示/采购"等词，
    16 条被判「收」，其中 14 条是事务公告。根因不是词表缺词，而是**豁免范围太宽**：
    `POLICY_FORM_EXEMPT` 见到"方案/办法/规划"就放行，于是
    《关于比选〈…项目实施方案〉…公告》因"方案"二字被放过。
    现在拆成 soft（可豁免）/ hard（不豁免）两组；本例两个方向都要守住。
    """
    rule = RuleClassifier(config(tmp_path).classification)
    out = rule.classify(Document(title=title,
                                 content='企业投资项目实行核准管理，项目单位应当办理用地预审手续，并开展节能审查。'))
    assert out.is_investment_policy == verdict, f'{why} → 实际 {out.is_investment_policy}｜{out.reason[:60]}'


def test_two_generic_keywords_are_not_a_policy(tmp_path):
    """两个通用词（投资/项目）不足以判「收」——这是本库的底线。

    合并后判据来自另一条分支的分档设计：弱词不单独构成理由，正文弱词需达到
    `weak_pending_threshold`（默认 4）才转待判定；正式文件若同时命中某类特征条款，
    走兜底转待判定。本例两者都不满足 → 判否（不收）。

    ⚠️ 已知残留风险（不要在没想清楚前"顺手修掉"）：若一份**事务性通知**通篇只在
    顺带提及处出现 1–3 个通用词、且不命中任何类别特征条款，它会被直接判否，
    属**漏收**方向。这是用"低误收"换来的代价——分档阈值调低会立刻放大人工量。
    业务验收样本集（validation/acceptance_cases.json）持续跟踪该风险。
    """
    rule=RuleClassifier(config(tmp_path).classification)
    out=rule.classify(Document(title='关于报送工作总结的通知',content='今年投资增长较快，项目推进顺利。'))
    assert out.is_investment_policy != 'yes', '通用词不能构成"收"的理由'
    assert out.todo_type == 'none' and not out.evidence


@pytest.mark.parametrize('title,body,category',[
 ('政府投资项目管理办法','政府投资项目应当开展后评价并追究违规责任。','incentive'),
 ('重大项目用地保障办法','优先保障重大项目新增建设用地，落实用地保障。','guarantee'),
 ('固定资产投资项目节能审查办法','固定资产投资项目必须开展节能审查。','access'),
])
def test_object_action_and_original_evidence(tmp_path,title,body,category):
    out=RuleClassifier(config(tmp_path).classification).classify(Document(title=title,content=body))
    assert out.is_investment_policy=='yes' and category in out.category
    assert out.evidence in body and out.need_review  # still a provisional rule result


def test_multi_label_needs_evidence_for_each_category(tmp_path):
    llm=LLMClassifier(config(tmp_path),{})
    text='优先保障重大项目用地。对政府投资项目开展后评价。'
    data=dict(is_investment_policy='yes',category=['guarantee','incentive'],doc_type='正式政策',
              evidence='优先保障重大项目用地',need_review=False,confidence=.95)
    out=llm._validate(Document(content=text),data)
    assert out.need_review and '独立证据' in out.reviewer_hint
    data['category_evidence']={'guarantee':'优先保障重大项目用地','incentive':'对政府投资项目开展后评价'}
    out=llm._validate(Document(content=text),data)
    assert not out.need_review and '分类证据' in out.reason
    data['category_evidence']['incentive']='编造证据'
    assert llm._validate(Document(content=text),data).need_review


def test_yunnan_encoded_pager_and_same_origin():
    page='<a href="/policies/2.html">下一页</a>'
    encoded=base64.b64encode(quote_plus(page).encode()).decode()
    raw=f'<div id="pages">{encoded}</div><script>window.atob(x)</script>'.encode()
    assert next_page_link(raw,'https://example.gov.cn/policies/')=='https://example.gov.cn/policies/2.html'
    assert not next_page_link('<a href="https://evil.test/">下一页</a>'.encode(),'https://example.gov.cn/')


def group(url, next_url=''):
    record=f'<record><![CDATA[<a href="{url}">政策通知</a>]]></record>' if url else ''
    return f'<datastore><nextgroup><![CDATA[<a href="{next_url}"></a>]]></nextgroup><recordset>{record}</recordset></datastore>'.encode()


def test_jpage_groups_and_partial_failure(tmp_path,monkeypatch):
    cfg=config(tmp_path);collector=Collector(cfg)
    src=SourceConfig(name='test',list_url='https://example.gov.cn/col/',include=['/art/'])
    proxy='/module/web/jpage/dataproxy.jsp?page=1'
    monkeypatch.setattr(collector,'fetch',lambda u:FetchResult(content=group('/art/2.html')))
    found=discover_jpage_links(collector,src,group('/art/1.html',proxy),src.list_url,3)
    assert len(found)==2 and collector.discovery_status['test']['stop_reason']=='no_next_group'
    assert not collector.discovery_status['test']['end_reached']
    monkeypatch.setattr(collector,'fetch',lambda u:FetchResult(ok=False,error='HTTP 403'))
    with pytest.raises(DiscoveryError) as e:discover_jpage_links(collector,src,group('/art/1.html',proxy),src.list_url,3)
    assert len(e.value.links)==1
    with pytest.raises(DiscoveryError,match='同源'):
        discover_jpage_links(collector,src,group('/art/1.html','https://evil.test/module/web/jpage/dataproxy.jsp'),src.list_url,3)
    collector.close()


def test_repeated_static_page_is_not_success(tmp_path,monkeypatch):
    cfg=config(tmp_path);src=SourceConfig(name='test',list_url='https://example.gov.cn/col/',include=['/art/'],pagination='trs',max_pages=3)
    cfg.sources={'test':src};pipe=Pipeline(cfg)
    try:
        pipe.sync_sources()
        monkeypatch.setattr(pipe.collector,'fetch',lambda u:FetchResult(content='<a href="/art/1.html">政策</a>'.encode()))
        stats=pipe.discover(src)
        assert stats.discovered==1 and stats.failed==1
        assert pipe.collector.discovery_status['test']['stop_reason']=='error'
        assert not pipe.db.get_source('test')['last_success_at']
    finally:pipe.close()


def test_yunnan_body_excludes_navigation():
    from policy_collector.parser import Parser
    raw='<meta charset="utf-8"><meta name="ArticleTitle" content="政策通知"><div>网站首页</div><div class="show-content">固定资产投资项目应当开展节能审查。</div><footer>网站地图</footer>'
    out=Parser().parse(raw.encode(),'html','https://yndrc.yn.gov.cn/html/2026/test/1.html')
    assert not out.parse_error and '网站地图' not in out.content and '网站首页' not in out.content


def test_provincial_registry_has_only_31_provinces():
    entries=json.loads((Path(__file__).resolve().parents[2]/'config/provinces.json').read_text())
    assert len(entries)==31 and len({e['region'] for e in entries})==31
    assert '杭州市' not in {e['region'] for e in entries}


def test_run_all_sources_isolates_single_source_failure(tmp_path, monkeypatch):
    """一键全国采集：单源失败必须被隔离，不能让后面 20 个省一并不采。

    这是这个功能存在的唯一理由。若某个省 403/超时就把整批打断，
    那"一键"反而比逐个点更糟——人得盯着哪一站挂了再手动补跑。
    """
    cfg=config(tmp_path)
    cfg.sources={
        'a_ok': SourceConfig(name='a_ok',site='甲省发改委',region='甲',enabled=True,
                             list_url='https://a.gov.cn/zc/',include=['/zc/']),
        'b_broken': SourceConfig(name='b_broken',site='乙省发改委',region='乙',enabled=True,
                             list_url='https://b.gov.cn/zc/',include=['/zc/']),
        'c_ok': SourceConfig(name='c_ok',site='丙省发改委',region='丙',enabled=True,
                             list_url='https://c.gov.cn/zc/',include=['/zc/']),
    }
    call_order=[]
    def fake_run_source(self, name, prefer='llm', limit=50, retry_only=False, reclassify=False):
        from policy_collector.pipeline import RunStats
        call_order.append(name)
        if name=='b_broken':
            raise RuntimeError('HTTP 403')
        s=RunStats();s.discovered=1;s.ingested=1;return s
    monkeypatch.setattr(Pipeline,'run_source',fake_run_source)
    pipe=Pipeline(cfg)
    try:
        out=pipe.run_all_sources(prefer='rule',limit=10,pause_seconds=0)
        # 关键断言：三个来源都被尝试过，失败的那个没有中断整批
        assert call_order==['a_ok','b_broken','c_ok']
        assert out['total']==3
        by={r['source']:r for r in out['results']}
        assert by['a_ok']['status']=='ok' and by['c_ok']['status']=='ok'
        assert by['b_broken']['status']=='failed' and '403' in by['b_broken']['error']
        assert out['status']=='partial' and out['stats']['ingested']==2
        # 批次记录必须存在且已收尾，进度里能按来源追责
        row=pipe.db.get_run_summary(out['run_id'])
        assert row['kind']=='batch' and row['status']=='partial'
        progress=json.loads(row['progress'])
        assert progress['completed']==3 and len(progress['results'])==3
        assert {r['source'] for r in progress['results']}=={'a_ok','b_broken','c_ok'}
    finally:
        pipe.close()


def test_batch_run_is_not_killed_by_stale_run_cleanup(tmp_path, monkeypatch):
    """批次行不能被 run_source 的"清理上次中断"误标为失败。

    每个来源启动时都会把 status='running' 的行标失败（防上次中断留脏）。
    若不排除当前批次自身，批次会在第一个来源启动瞬间变成 failed。
    """
    cfg=config(tmp_path)
    cfg.sources={'a_ok': SourceConfig(name='a_ok',site='甲省发改委',region='甲',enabled=True,
                                      list_url='https://a.gov.cn/zc/',include=['/zc/'])}
    pipe=Pipeline(cfg)
    try:
        captured={}
        real=Pipeline.run_source
        def spy(self, name, prefer='llm', limit=50, retry_only=False, reclassify=False):
            captured['batch_status_during_run']=self.db.get_run_summary(self.active_batch)['status']
            from policy_collector.pipeline import RunStats
            return RunStats()
        monkeypatch.setattr(Pipeline,'run_source',spy)
        out=pipe.run_all_sources(prefer='rule',limit=5,pause_seconds=0)
        assert captured['batch_status_during_run']=='running'
        assert pipe.db.get_run_summary(out['run_id'])['status']=='ok'
    finally:
        pipe.close()



def test_audit_flags_national_repost_and_ignores_date_dirs():
    """验收脚本的归属检查：聚合栏目下的中央转载要能被抓出来，年月目录不能误报。

    背景：福建 /zwgk/fgzd/ 是聚合页，前 15 条全在 gjfgwwj（国家发改委文件转载）子目录下，
    收进来会把中央文件的 region 错记成福建——而机制检查（列表/正文/附件/入库）全过。
    这条检查把"归属错记"从只能人工发现，变成脚本能判。
    """
    from scripts.audit_sources import sub_column_distribution, NATIONAL_REPOST
    # 常规 TRS 站：栏目下第一段是年月，不是子栏目 —— 不能误报
    normal = ['https://x.gov.cn/zcfb/ghxwj/202604/t20260430_1.html',
              'https://x.gov.cn/zcfb/ghxwj/202601/t20260109_2.html',
              'https://x.gov.cn/zcfb/ghxwj/2026/t20260109_3.html']
    assert sub_column_distribution(normal, 'https://x.gov.cn/zcfb/ghxwj/') == {}
    # 聚合页：并列多个子栏目，其中一个是中央转载
    mixed = ['https://x.gov.cn/zwgk/fgzd/gjfgwwj/202609/t1.htm',
             'https://x.gov.cn/zwgk/fgzd/gjfgwwj/202608/t2.htm',
             'https://x.gov.cn/zwgk/fgzd/sfgwgfxwj/202609/t3.htm']
    dist = sub_column_distribution(mixed, 'https://x.gov.cn/zwgk/fgzd/')
    assert dist == {'gjfgwwj': 2, 'sfgwgfxwj': 1}
    assert [k for k in dist if NATIONAL_REPOST.search(k)] == ['gjfgwwj']


def test_audit_sub_column_distribution_marks_out_of_column_links():
    """详情跳到栏目路径之外的也要标出来（跨栏目/跨站混杂的另一种形态）。

    同时锁住一件事：**纯数字段不是子栏目**。除年月外，还有"长数字串即文章 ID"
    的站（辽宁 /fgw/zc/zxzc/2026090912183160373/index.shtml）——只排除年月形态的话，
    辽宁 36 条详情会被拆成 36 个"子栏目"，把聚合栏目检测变成噪音。
    检查一旦开始误报，真问题也会连带被忽略，所以这条必须钉住。
    """
    from scripts.audit_sources import sub_column_distribution
    urls = ['https://x.gov.cn/other/section/t1.html',
            'https://x.gov.cn/zwgk/fgzd/sub/t2.htm']
    dist = sub_column_distribution(urls, 'https://x.gov.cn/zwgk/fgzd/')
    assert dist == {'(栏目路径之外)': 1, 'sub': 1}
    # 辽宁形态：长数字串是文章 ID，不是子栏目 → 不产生任何子栏目
    ln = ['https://fgw.ln.gov.cn/fgw/zc/zxzc/2026090912183160373/index.shtml',
          'https://fgw.ln.gov.cn/fgw/zc/zxzc/2024123113391120820/index.shtml']
    assert sub_column_distribution(ln, 'https://fgw.ln.gov.cn/fgw/zc/zxzc/index.shtml') == {}


def test_discover_follows_js_redirect_stub():
    """栏目页可能是"跳转桩"（整页只有一行 location.href），必须跟一次。

    实测山东 /col/col91475/index.html 全文仅 1823 字节、零链接，是占位页；
    不跟跳会得出"这个栏目是空的"这种错误结论（并把可用省份记为需适配）。
    """
    from scripts.discover_provinces import js_redirect_target
    stub = '<html><body><SCRIPT> location.href="/col/col91477/index.html";</SCRIPT></body></html>'
    assert js_redirect_target(stub, 'http://fgw.shandong.gov.cn/col/col91475/index.html') \
        == 'http://fgw.shandong.gov.cn/col/col91477/index.html'
    # 相对地址按 base 解析
    assert js_redirect_target('<script>location.href="list2.html"</script>',
                              'http://x.gov.cn/a/b/index.html') == 'http://x.gov.cn/a/b/list2.html'
    # 无跳转 / 伪跳转 都不能误判
    assert js_redirect_target('<html>正常列表页</html>', 'http://x.gov.cn/') == ''
    assert js_redirect_target('<a onclick="location.href=\'javascript:;\'">x</a>', 'http://x.gov.cn/') == ''


def test_links_in_onclick_are_collected_not_only_href():
    """链接写在 onclick 里（<a> 无 href）时，列表提取必须能看到。

    实测天津 fzgg.tj.gov.cn：
        <a onclick="isDownLoad(this,'https://…/t20260911_7372816.html')" title="…">
    只按 href 提取会得到"60KB 页面零文章链接"，从而把该站误判为"列表由 JS 渲染"、
    归入最贵的一档（需要逆向数据接口）——实际上它完全是静态可采的。
    """
    from policy_collector.collector import url_from_onclick, ListPageParser
    assert url_from_onclick(
        "isDownLoad(this,'https://fzgg.tj.gov.cn/xxfb/tzggx/202609/t20260911_7372816.html')"
    ) == 'https://fzgg.tj.gov.cn/xxfb/tzggx/202609/t20260911_7372816.html'
    assert url_from_onclick("window.open('/a/b/12345.html')") == '/a/b/12345.html'
    # 不能把普通参数当成链接
    assert url_from_onclick('void(0)') == ''
    assert url_from_onclick("jumpTo(this,'7','1')") == ''

    src = SourceConfig(name='t', site='甲', region='甲', enabled=True,
                       list_url='https://fzgg.tj.gov.cn/xxfb/tzggx/',
                       include=['/xxfb/tzggx/'],
                       detail_url_pattern=r'/xxfb/tzggx/\d{6}/t\d{8}_\d+\.html?$')
    page = ('<ul><li><a onclick="isDownLoad(this,'
            "'https://fzgg.tj.gov.cn/xxfb/tzggx/202609/t20260911_7372816.html')"
            '" title="天津市发展改革委关于调整我市成品油价格的公告">公告</a></li></ul>')
    links = ListPageParser(src).parse(page, base_url=src.list_url)
    assert [x.url for x in links] == ['https://fzgg.tj.gov.cn/xxfb/tzggx/202609/t20260911_7372816.html']
    assert links[0].title.startswith('天津市发展改革委')


def test_next_page_link_reads_onclick_and_title():
    """翻页链接也可能只在 onclick 里 + 用 title 标"下一页"。

    实测辽宁慧点 CMS：
        <a title="下一页" onclick="queryArticleByCondition(this,'/fgw/zc/zxzc/be842d3c-2.shtml')">
    只认 href 或只认文本会取不到，next_link 模式在该站直接失效
    （实测表现为"只采到第一页"，而页面明明写着 totalpage="7"）。
    """
    from policy_collector.collector import next_page_link
    html = ('<div class="pageDiv_7_7">'
            '<a style="cursor:pointer" title="下一页" '
            "onclick=\"queryArticleByCondition(this,'/fgw/zc/zxzc/be842d3c-2.shtml')\">下一页</a>"
            '</div>')
    got = next_page_link(html.encode('utf-8'), 'https://fgw.ln.gov.cn/fgw/zc/zxzc/index.shtml')
    assert got == 'https://fgw.ln.gov.cn/fgw/zc/zxzc/be842d3c-2.shtml'
    # 跨域不跟（防被引到站外）
    cross = '<a title="下一页" onclick="go(this,\'https://evil.example.com/x-2.shtml\')">下一页</a>'
    assert not next_page_link(cross.encode('utf-8'), 'https://fgw.ln.gov.cn/a/index.shtml')
