import base64
import io
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fitz
import pytest
from bs4 import BeautifulSoup
from bs4.element import Comment, NavigableString, Tag
from core import llm
from policy_collector.llm_client import LLMClient
from policy_collector.pipeline import Pipeline
from workbench.server import create_app

HEADERS = {'X-Requested-With': 'ApprovalAgent'}


@pytest.fixture
def suite(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(('LLM_', 'VISION_', 'POLICY_')):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    app = create_app(tmp_path)
    yield app, app.test_client(), tmp_path
    app.extensions['approval_store'].executor.shutdown(wait=True)
    llm.set_config_path(None)


def post(client, path, body):
    return client.post(path, json=body, headers=HEADERS)


def test_pages_and_namespaced_links(suite):
    app, client, _ = suite
    for url in ['/', '/approval/', '/approval/app.js', '/approval/style.css',
                '/approval/api/config', '/policy/', '/policy/policies', '/policy/sources',
                '/policy/todos', '/policy/runs', '/policy/quality', '/policy/maintenance',
                '/settings/model', '/tasks', '/api/health']:
        response = client.get(url)
        assert response.status_code == 200, url
    doc = BeautifulSoup(client.get('/policy/').data, 'html.parser')
    for node in doc.select('a[href],link[href],script[src]'):
        href = node.get('href') or node.get('src')
        if href.startswith('/'):
            assert client.get(href).status_code in (200, 302, 303, 308), href
    assert client.get('/policy/settings/model').location == '/settings/model'
    assert b'id="modelBase"' not in client.get('/approval/').data
    assert post(client, '/approval/api/model/config', {}).status_code == 410


def test_write_origin_and_policy_csrf(suite):
    _, client, _ = suite
    assert client.post('/api/model/config', json={'values': {}}).status_code == 403
    assert client.post('/api/model/config', json={'values': {}}, headers={**HEADERS, 'Origin': 'https://other.invalid'}).status_code == 403
    assert client.post('/policy/sources/run-all', data={}).status_code == 400
    assert client.post('/approval/api/upload', json={'files': []}).status_code == 403


class Wire(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append(body)
        response = self.server.reply
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())


@pytest.fixture
def wire():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Wire)
    server.calls = []
    server.reply = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 11, 'completion_tokens': 3}}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def configure(client, wire):
    response = post(client, '/api/model/config', {'values': {
        'LLM_BASE_URL': f'http://127.0.0.1:{wire.server_port}/v1', 'LLM_API_KEY': 'test-secret',
        'LLM_MODEL': 'shared-text', 'VISION_MODEL': 'shared-vision', 'LLM_MAX_RETRIES': '0'}})
    assert response.status_code == 200
    return response


def test_both_businesses_use_shared_wire_and_hot_reload(suite, wire):
    app, client, data = suite
    configure(client, wire)
    from app.model_client import chat
    policy_client = LLMClient(app.extensions['policy_config'].llm)
    assert chat([{'role': 'user', 'content': 'test'}], llm.effective('LLM_MODEL'))['ok']
    assert policy_client.chat_json('test', 'test')['ok']
    assert policy_client.usage == {'input_tokens': 11, 'output_tokens': 3}
    assert policy_client.usage_reported
    assert [c['model'] for c in wire.calls] == ['shared-text', 'shared-text']
    assert post(client, '/api/model/test', {'vision': True}).status_code == 200
    assert wire.calls[-1]['model'] == 'shared-vision'
    assert wire.calls[-1]['messages'][0]['content'][1]['type'] == 'image_url'
    assert post(client, '/api/model/config', {'values': {'LLM_MODEL': 'updated'}}).status_code == 200
    assert client.get('/approval/api/config').json['model'] == 'updated'
    policy_client.chat_json('test', 'test')
    assert wire.calls[-1]['model'] == 'updated'
    assert (data / 'model_config.json').stat().st_mode & 0o777 == 0o600
    assert not (data / 'policy/llm.local.json').exists()
    assert not (data / 'approval/model_config.json').exists()
    assert 'test-secret' not in client.get('/api/model/config').text
    assert 'test-secret' not in client.get('/approval/api/config').text
    assert post(client, '/api/model/config', {'values': {'LLM_ENABLED': 'false'}}).status_code == 200
    assert not policy_client.available
    assert not client.get('/approval/api/config').json['llm_ready']
    count = len(wire.calls)
    assert policy_client.chat_json('test', 'test') is None
    assert post(client, '/api/model/test', {}).status_code == 502
    assert len(wire.calls) == count


def test_config_survives_restart_and_prevents_key_reuse(suite, wire, monkeypatch):
    _, client, data = suite
    configure(client, wire)
    assert post(client, '/api/model/config', {'values': {'LLM_BASE_URL': 'https://another.invalid/v1'}}).status_code == 400
    monkeypatch.setenv('LLM_MODEL', 'old-environment-value')
    llm.set_config_path(data / 'model_config.json')
    assert llm.public_config()['model'] == 'shared-text'
    assert post(client, '/api/model/config', {'values': {'LLM_BASE_URL': 'http://127.0.0.1:9999/v1', 'LLM_ALLOW_NO_KEY': 'true'}, 'clear_key': True}).status_code == 200
    assert llm.effective('LLM_API_KEY') == ''


@pytest.mark.parametrize('content,finish,refusal', [('{"a":1,"a":2}', 'stop', None), ('{}','length',None), ('{}','stop','refused')])
def test_shared_invalid_output_falls_back(suite, wire, content, finish, refusal):
    app, client, _ = suite
    configure(client, wire)
    wire.reply['choices'][0] = {'message': {'content': content, 'refusal': refusal}, 'finish_reason': finish}
    pc = LLMClient(app.extensions['policy_config'].llm)
    assert pc.chat_json('s', 'u') is None
    assert pc.last_error
    assert post(client, '/api/model/test', {}).status_code == 502


def test_upload_review_evidence_export_and_persistence(suite):
    app, client, data = suite
    with fitz.open() as pdf:
        page = pdf.new_page()
        for y, text in [(60, '关于测试项目初步设计的批复'), (90, '一、建设内容：总建筑面积100平方米。'),
                        (120, '二、建设地点：测试园区。'), (150, '项目代码：2609-330100-04-01-100001')]:
            page.insert_text((40,y), text, fontname='china-s', fontsize=12)
        blob = pdf.tobytes()
    response = post(client, '/approval/api/upload', {'files': [{'name':'测试.pdf','data':base64.b64encode(blob).decode()}], 'use_llm':False})
    assert response.status_code == 202
    jid = response.json['job_id']
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get('/approval/api/jobs/'+jid).json
        if job['status'] != 'running':
            break
        time.sleep(.05)
    assert job['status'] == 'completed' and not job['errors']
    groups = client.get('/approval/api/documents').json['groups']
    doc = groups[0]['documents'][0]
    assert doc['fields']['建设地点']['evidence']
    did = doc['id']
    assert client.get(f'/approval/api/documents/{did}/pages/1.png').data.startswith(b'\x89PNG')
    response = post(client, f'/approval/api/documents/{did}/review', {'kind':'fixed','name':'建设地点','value':'人工核对测试园区','revision':doc['revision'],'reason':'集成测试'})
    assert response.status_code == 200, response.text
    assert client.get(f'/approval/api/documents/{did}').json['fields']['建设地点']['value'] == '人工核对测试园区'
    assert client.get('/approval/api/export.xlsx').data.startswith(b'PK')
    assert client.get('/tasks').status_code == 200
    from app.server import Store
    reopened = Store(data / 'approval')
    try:
        assert reopened.get(did)['fields']['建设地点']['value'] == '人工核对测试园区'
    finally:
        reopened.executor.shutdown()


def test_policy_pipeline_review_and_mounted_post_redirect(suite):
    app, client, _ = suite
    pipe = Pipeline(app.extensions['policy_config'])
    try:
        stats = pipe.run_demo(samples_dir=Path('samples/policies'))
        assert stats.ingested == 4
        assert pipe.run_demo(samples_dir=Path('samples/policies')).ingested == 0
        rows = pipe.db.query_policies(limit=10)
        pid = rows[0]['id']
        html = client.get(f'/policy/policies/{pid}').text
        soup = BeautifulSoup(html, 'html.parser')
        token = soup.select_one('input[name=csrf_token]')['value']
        response = client.post(f'/policy/policies/{pid}/review', data={'csrf_token':token,'action':'adjust','categories':['guide']})
        assert response.status_code == 302
        assert response.location.startswith('/policy/')
        assert pipe.db.get_policy(pid)['review_status'] == 'adjusted'
        assert client.get(response.location).status_code == 200
        assert client.get('/tasks').status_code == 200
    finally:
        pipe.close()


# --- 模型设置页的「从哪来、回哪去」 -------------------------------------------
# 起因：设置页入口是散的（工作台顶栏、首页模型胶囊、政策侧导航的「模型配置」），
# 却一个回退路径都没有。用户从政策资料库第 3 页点进来配完模型，只能自己重找。

def _back_button(client, referer=None):
    """取出设置页返回按钮的 (目标地址, 文案)。referer=None 表示模拟书签直达。

    箭头是装饰，必须标了 aria-hidden 才不代表实际内容——这里顺手当成断言：
    读屏软件念出来的应该只有"返回政策资料库"。
    """
    headers = {'Referer': referer} if referer else {}
    html = client.get('/settings/model', headers=headers).get_data(as_text=True)
    soup = BeautifulSoup(html, 'html.parser')
    node = soup.select_one('a.back')
    assert node is not None, f'设置页没有返回按钮（referer={referer!r}）'
    for deco in node.select('[aria-hidden="true"]'):
        deco.decompose()
    return node['href'], node.get_text(strip=True)


@pytest.mark.parametrize('referer,expect_url,expect_label', [
    ('/policy/policies?sort=date&order=desc&per=50',
     '/policy/policies?sort=date&order=desc&per=50', '返回政策资料库'),
    ('/policy/quality', '/policy/quality', '返回材料质量'),
    ('/policy/runs/batch-abc', '/policy/runs/batch-abc', '返回处理进度'),
    ('/policy/todos', '/policy/todos', '返回待办清单'),
    ('/policy/', '/policy/', '返回政策归集'),
    ('/tasks', '/tasks', '返回任务进度'),
    ('/', '/', '返回首页'),
    ('/approval/xyz', '/approval/xyz', '返回审批文件'),
])
def test_settings_back_button_follows_referer(suite, referer, expect_url, expect_label):
    _, client, _ = suite
    assert _back_button(client, referer) == (expect_url, expect_label)


@pytest.mark.parametrize('referer,why', [
    (None, '书签/地址栏直达'),
    ('https://evil.example/x', '跨站来路不能成为跳转目标'),
    ('//evil.example/x', '协议相对 URL 同样要挡住'),
    ('/policy/settings/model', '旧设置页会被 303 弹回来，等于按钮没反应'),
    ('/settings/model', '从设置页自身刷新'),
    ('/api/model/config', '来路不是页面'),
])
def test_settings_back_button_falls_back_home(suite, referer, why):
    _, client, _ = suite
    url, label = _back_button(client, referer)
    assert (url, label) == ('/', '返回首页'), why


def test_settings_back_keeps_query_but_drops_unsafe_prefix(suite):
    """查询串要带回去，路径本身不能带协议或主机——Referer 是外部输入。"""
    _, client, _ = suite
    url, _ = _back_button(client, 'http://127.0.0.1/fake/path')
    assert url == '/', '同源但未登记的路径应退首页'
    url, _ = _back_button(client, '/policy/provinces?region=%E6%B1%9F%E8%8B%8F')
    assert url == '/policy/provinces?region=%E6%B1%9F%E8%8B%8F'


def test_workbench_nav_marks_current_page(suite):
    """两处导航各管一段，以前都长得一模一样。

    L0 套件条回答"在哪个模块"，L1 左栏回答"在模块里的哪一页"。
    高亮是"我在哪"的唯一提示，每处都必须**恰好一项**（或本模块没有当前页时为零）。
    """
    _, client, _ = suite
    for path, suite_expect, side_expect in [
            ('/', '/', '/'),
            ('/tasks', '/', '/tasks'),
            ('/settings/model', '/settings/model', None)]:
        soup = BeautifulSoup(client.get(path).data, 'html.parser')

        items = soup.select('header.suite-bar .suite-nav a')
        assert len(items) == 4, f'{path} 套件条应有 4 项跨模块入口'
        marked = [a['href'] for a in items if a.get('aria-current') == 'page']
        assert marked == [suite_expect], f'{path} 套件条高亮到了 {marked}'
        # 高亮只写在 aria-current 上，样式靠 [aria-current=page] 选择器接。
        # 若哪天有人又加一个 class，CSS 与模板就成两处要同步的状态了。
        assert not any('active' in (a.get('class') or []) for a in items), '高亮不应另加 class'

        # 左栏只放工作台自己的页，跨模块的入口一律上移——所以这里固定两项
        side = soup.select('aside.sidebar .side-nav a')
        assert [a['href'] for a in side] == ['/', '/tasks'], path
        side_marked = [a['href'] for a in side if a.get('aria-current') == 'page']
        assert side_marked == ([side_expect] if side_expect else []), \
            f'{path} 左栏高亮到了 {side_marked}'


# --- 套件条高亮：政策子应用被挂在 /policy 下，判据必须带挂载前缀 ---------------
# 起因：政策侧 base.html 自己写了一套 `request.path.startswith(...)`。
# WSGI 把 /policy/policies 拆成 SCRIPT_NAME='/policy' + PATH_INFO='/policies'，
# 而 Flask 的 request.path **只有 PATH_INFO**，于是：
#   /policy/        → request.path == '/' → 命中工作台那条 → 高亮停在"工作台"
#   /policy/todos   → request.path == '/todos' → 四条都不中 → 一个高亮都没有
# 症状同样是"页面 200、测试全绿、只有肉眼看得出"。现在判据统一在 core/shell.py。
SUITE_HIGHLIGHT = {
    '/': '/', '/tasks': '/', '/settings/model': '/settings/model', '/approval/': '/approval/',
    '/policy/': '/policy/', '/policy/policies': '/policy/', '/policy/todos': '/policy/',
    '/policy/provinces': '/policy/', '/policy/sources': '/policy/', '/policy/runs': '/policy/',
    '/policy/quality': '/policy/', '/policy/maintenance': '/policy/',
}


@pytest.mark.parametrize('path,expected', sorted(SUITE_HIGHLIGHT.items()))
def test_suite_nav_highlights_exactly_one_module(suite, path, expected):
    """每页的套件条必须**恰好一项**高亮，且就是当前模块——不是 0 项，也不是别家。"""
    _, client, _ = suite
    soup = BeautifulSoup(client.get(path).data, 'html.parser')
    items = soup.select('header.suite-bar .suite-nav a')
    assert len(items) == 4, f'{path} 套件条应有 4 项跨模块入口'
    marked = [a['href'] for a in items if a.get('aria-current') == 'page']
    assert marked == [expected], f'{path} 套件条高亮到了 {marked}，应该是 {[expected]}'


def test_module_detection_uses_script_root():
    """判据本身单测：挂载前缀必须算进去，默认值只在没匹配上前缀时生效。"""
    from core.shell import current_module
    # 挂在 /policy 下：script_root 带前缀、path 是去掉前缀的部分
    assert current_module('/policy', '/') == 'policy'
    assert current_module('/policy', '/policies') == 'policy'
    assert current_module('/policy', '/settings/model') == 'policy'   # 政策侧自己的路由
    assert current_module('', '/approval/x') == 'approval'
    assert current_module('', '/settings/model') == 'settings'
    # 没匹配上前缀 → 走调用方给的默认值（工作台应用=workbench，政策应用=policy）
    assert current_module('', '/') == 'workbench'
    assert current_module('', '/tasks') == 'workbench'
    assert current_module('', '/policies', default='policy') == 'policy'


def test_authored_shell_text_is_gone(suite):
    """页头的英文装饰标签与左栏中英对照小标题，三端都不该再出现。

    这些是纯装饰：`POLICY LIBRARY / 资料管理` 这类 eyebrow 不承载任何信息，
    `工作台 WORKBENCH` 这类 side-caption 只是把模块名写两遍。
    """
    _, client, _ = suite
    banned = ['WORKSPACE', 'WORKBENCH', 'POLICY LIBRARY', 'OVERVIEW', 'REGIONS',
              'MAINTENANCE /', 'MODEL SERVICE', 'MATERIAL QUALITY', 'COLLECTION /',
              'TODO /']
    for path in SUITE_HIGHLIGHT:
        html = client.get(path).get_data(as_text=True)
        hit = [t for t in banned if t in html]
        assert not hit, f'{path} 仍有装饰性英文标签：{hit}'
        assert 'side-caption' not in html, f'{path} 仍有左栏中英对照小标题'


def test_settings_back_button_reachable_from_policy_sidebar(suite):
    """端到端：政策库 → 侧栏「模型配置」→ 设置页 → 返回，应回到原筛选状态。"""
    app, client, _ = suite
    pipe = Pipeline(app.extensions['policy_config'])
    try:
        pipe.run_demo(samples_dir=Path('samples/policies'))
    finally:
        pipe.close()
    source = '/policy/policies?sort=date&order=desc&per=50'
    assert client.get(source).status_code == 200
    doc = BeautifulSoup(client.get('/policy/').data, 'html.parser')
    entry = [a['href'] for a in doc.select('nav.side-nav a') if '模型' in a.get_text()]
    assert entry == ['/policy/settings/model'], entry
    hop = client.get(entry[0])
    assert hop.status_code == 303 and hop.location == '/settings/model'
    url, label = _back_button(client, source)
    assert (url, label) == (source, '返回政策资料库')
    assert client.get(url).status_code == 200


# --- 外壳层的两类"只有肉眼看才崩"的缺陷 ---------------------------------------
# 起因：给审批页加外壳时写了 Jinja 注释 `{# #}`，而 app/static/index.html 是
# **静态资源**（app/server.py 直接 read_bytes 吐出去，不经 Jinja）。于是注释原样输出，
# 落在 .shell-body 这个 grid 容器里，每个文本节点变成一个匿名栅格项，三列被挤塌。
# 症状：页面 200、全部测试绿、只有打开页面才看得出。

# 三个模块的 .shell-body 有几个直接子元素（列）。这是"布局没被撑坏"的硬指标，
# 比"页面能打开"强得多：多一个文本节点就变成多一列。
SHELL_COLUMNS = {'/': 2, '/tasks': 2, '/settings/model': 2, '/approval/': 3,
                 '/policy/': 2, '/policy/policies': 2}


@pytest.mark.parametrize('path,columns', sorted(SHELL_COLUMNS.items()))
def test_shell_grid_has_only_element_children(suite, path, columns):
    """栅格容器里除元素外不能有非空文本节点——它就是多出来的一列。

    HTML 注释不算：它不生成盒子。Jinja 注释算，因为它根本没被处理。
    """
    _, client, _ = suite
    soup = BeautifulSoup(client.get(path).data, 'html.parser')
    body = soup.select_one('.shell-body')
    assert body is not None, f'{path} 没有 .shell-body——外壳没接上'

    kids = list(body.children)
    elements = [k for k in kids if isinstance(k, Tag)]
    stray = [k for k in kids
             if isinstance(k, NavigableString) and not isinstance(k, Comment) and k.strip()]
    assert not stray, (
        f'{path} 的 .shell-body 里混进了非空文本节点，会各自变成一个匿名栅格项：\n'
        + '\n'.join(f'  {str(s)[:90]!r}' for s in stray)
    )
    assert len(elements) == columns, (
        f'{path} 的 .shell-body 应有 {columns} 列，实际 {[e.name for e in elements]}'
    )


def test_static_page_has_no_template_syntax(suite):
    """审批页是静态文件，模板语法不会被处理，只会原样印在页面上。

    这份文件不走 Jinja——`app/server.py` 里 `read_bytes()` 直接返回。
    在它里面写 `{{ }}` / `{% %}` / `{# #}` 都会成为可见正文。
    """
    _, client, _ = suite
    for path in ['/approval/']:
        html = client.get(path).get_data(as_text=True)
        for token in ('{{', '{%', '{#'):
            assert token not in html, f'{path} 输出了未处理的模板语法 {token!r}'


# --- 外壳的"链接下划线"缺陷 ----------------------------------------------------
# 套件条里是两个 <a>：品牌 `.suite-brand` + 四入口 `.suite-nav a`。
# 统一外壳时删掉了审批页旧的 `.suite-nav a{text-decoration:none}`，新写的外壳段又没有
# `text-decoration`，于是只有审批页出现浏览器默认下划线——另两端各有一条全局 `a` 规则
# 刚好兜住，把缺陷掩盖了。症状依旧是"测试全绿、只有肉眼看得出"。
# 两条硬约束因此钉死：① 外壳自身的链接规则必须自带 text-decoration:none；
# ② 三份 CSS 都要有同款全局 `a` 基线，不然下次再加链接又会漏。

SHELL_CSS = {
    'workbench': 'workbench/static/workbench.css',
    'policy': 'policy_collector/static/style.css',
    'approval': 'app/static/style.css',
}


@pytest.mark.parametrize('module,rel', sorted(SHELL_CSS.items()))
def test_shell_links_declare_no_underline(module, rel):
    """外壳链接的"无下划线"必须写在自身规则里，不能靠别的模块的全局规则兜底。"""
    css = re.sub(r'/\*.*?\*/', '', Path(rel).read_text(encoding='utf-8'), flags=re.S)

    for selector in ('.suite-brand', '.suite-nav a'):
        match = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', css)
        assert match, f'{module}：外壳 CSS 里找不到 {selector} 规则'
        body = match.group(1).replace(' ', '')
        assert 'text-decoration:none' in body, (
            f'{module}：{selector} 没有声明 text-decoration:none，'
            '套件条会出现浏览器默认下划线'
        )

    assert re.search(r'(?<![-\w.#])a\s*\{[^}]*text-decoration\s*:\s*none', css), (
        f'{module}：缺少全局 a{{text-decoration:none}} 基线规则。'
        '三份外壳都要有这一条，否则新加的链接会带下划线'
    )
