"""模板层的两类静默缺陷，靠扫描抓，不靠人盯。

这两类问题的共同点：**页面返回 200、测试全绿、肉眼看不出**，只有拿真实数据
盯着某一列才会发现。实测各踩过一次，所以在这里钉成红灯——

  1. **渲染期未定义变量**：模板引用了、但渲染上下文里根本没有的变量。
     Jinja 默认把它当假值，于是 `{% if todo_names %}` 这种写法永远走 else 分支。
     - 政策库「状态 / 待办」列整列印出原始英文 `review_status`，真正的待办类型
       和责任方一个字都没显示（看起来"每行都正常"）。
     - `base.html` 的"界面预览"横幅只在演示脚本里注入过，正式环境永远不显示，
       而没有任何代码说得清这一点。

  2. **CSS 类名写了没定义**：模板里 `class="list wide"`，CSS 里从来没有 `.wide`。
     看起来生效、其实没写。除了一像素一像素地量，没有别的办法察觉。

扫描方式刻意选成"不改变行为"的：把 Jinja 的 Undefined 换成会记账的子类，
它照样返回假值（页面照常渲染），但每次被访问都记下名字。这样一次跑完所有页面
就能拿到完整清单，而不是修一个暴露一个。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jinja2 import Undefined

from policy_collector.config import AppConfig
from policy_collector.db import Database
from policy_collector.quality import classify_issue, parse_error_report
from policy_collector.webapp import create_app

ROOT = Path(__file__).resolve().parents[2]

# 允许"模板里有、CSS 里查不到"的类名。**不是垃圾桶**：每一项都要有理由，
# 因为新增的未定义类名会立刻让 test_template_class_names_are_defined_in_css 变红。
POLICY_CLASS_WHITELIST = {
    # 仅作结构/语义标记，视觉由父级选择器负责，本身不需要规则
    'latest-panel', 'region-metrics', 'review-card', 'model-state-toggle',
}
WORKBENCH_CLASS_WHITELIST: set[str] = set()


# ── 工具 ──────────────────────────────────────────────────────
def _static_classes(html: str) -> set[str]:
    """模板里写死的 class 名（剥掉 Jinja 表达式）。

    `class="province-cat c-{{ code }}"` 这类拼接：剥掉 `{{ }}` 后会留下 `c-` 残片，
    它不是类名，丢掉。
    """
    html = re.sub(r'\{\{.*?\}\}', ' ', html, flags=re.S)
    html = re.sub(r'\{%.*?%\}', ' ', html, flags=re.S)
    found: set[str] = set()
    for attr in re.findall(r'class="([^"]*)"', html):
        found.update(attr.split())
    return {c for c in found if c and not c.endswith('-')}


def _css_classes(path: Path) -> set[str]:
    return set(re.findall(r'\.([A-Za-z][\w-]*)', path.read_text(encoding='utf-8')))


class _RecordingUndefined(Undefined):
    """记账版 Undefined：行为与默认 Undefined 一致，只多记一笔。"""

    hits: set[str] = set()

    def _note(self):
        _RecordingUndefined.hits.add(self._undefined_name or '<expression>')

    def __str__(self):        # noqa: D105
        self._note(); return ''

    def __iter__(self):
        self._note(); return iter(())

    def __bool__(self):
        self._note(); return False

    def __len__(self):
        self._note(); return 0

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        self._note(); return self

    def __getitem__(self, name):
        self._note(); return self


@pytest.fixture
def cfg(tmp_path):
    c = AppConfig.load()
    c.data_dir = tmp_path
    c.downloads_dir = tmp_path / 'downloads'
    c.db_path = tmp_path / 'db.sqlite'
    return c


SEED_POLICIES = [
    ('k1', '甲：已确认', '2024-03-08', '浙江', 'none',      'confirmed'),
    ('k2', '乙：结论待确认', '2022-11-20', '江苏', 'review', 'pending'),
    ('k3', '丙：待定口径', '2022-11-20', '浙江', 'scope',    'pending'),
    ('k4', '丁：高置信候选', '',           '安徽', 'candidate', 'pending'),
    ('k5', '戊：材料待补', '2019-05-01', '广东', 'material', 'pending'),
    ('k6', '己：已剔除', '2018-01-01', '山东', 'none',       'rejected'),
]


def _seed(cfg, events_per_run=(250, 120)):
    """造出能走到各页面主要分支的最小数据：政策 + 一个批次 run + 一个单来源 run。"""
    # 来源配置是 create_app() 里 Pipeline.sync_sources() 写进库的，
    # 而下面的采集记录要挂到来源上——所以先起一次 app 让它落地。
    create_app(cfg)
    d = Database(cfg.db_path)
    for key, title, date, region, todo, review in SEED_POLICIES:
        d.add_policy_version(key, {
            'title': title, 'page_date': date, 'region': region,
            'todo_type': todo, 'review_status': review, 'category_names': '准入类',
        })
    # 一条带正文解析异常的采集记录，用来验证归因面板有内容可渲染
    src = d.list_sources()
    source_id = src[0]['id'] if src else None
    if source_id is not None:
        fetch_id = d.add_fetch(source_id, 'https://example.gov.cn/a/1', status='processed',
                              title='庚：正文没解析出来')
        d.update_fetch(fetch_id, document_json=json.dumps(
            {'parse_error': '未定位正文容器，整页文本仅供复核，需维护来源适配'}, ensure_ascii=False))
        d.add_policy_version('k7', {
            'title': '庚：正文没解析出来', 'page_date': '2021-06-01', 'region': '江苏',
            'todo_type': 'review', 'review_status': 'pending', 'source_fetch_id': fetch_id,
        })
    d.start_run('run-batch', None, 'batch')
    d.finish_run('run-batch', {'ingested': 10}, status='partial', note='测试用批次')
    d.start_run('run-single', None, 'manual')
    for i in range(events_per_run[0]):
        d.agent_event('run-batch', None, 'discover', 'done', f'批次第 {i + 1} 步')
    for i in range(events_per_run[1]):
        d.agent_event('run-single', None, 'parse', 'done', f'单来源第 {i + 1} 步')
    d.finish_run('run-single', {'ingested': 3}, status='ok')
    d.close()


def _pages(cfg):
    return [
        '/', '/provinces', '/policies', '/policies?todo=review', '/policies?q=甲&sort=date&per=50&page=1',
        '/todos', '/sources', '/runs', '/runs?per=100&page=2', '/quality', '/quality?per=100&page=2',
        '/maintenance', '/policies/1', '/runs/run-batch', '/runs/run-single',
    ]


# ── 1. 渲染期未定义变量 ────────────────────────────────────────
def test_no_undefined_variables_when_rendering_pages(cfg):
    _seed(cfg)
    app = create_app(cfg)
    app.jinja_env.undefined = _RecordingUndefined
    _RecordingUndefined.hits = set()
    client = app.test_client()

    broken = {}
    for path in _pages(cfg):
        before = set(_RecordingUndefined.hits)
        resp = client.get(path)
        assert resp.status_code == 200, f'{path} 渲染失败：{resp.status_code}'
        new = _RecordingUndefined.hits - before
        if new:
            broken[path] = sorted(new)

    assert not broken, (
        '模板引用了渲染上下文里根本没有的变量（Jinja 会静默当假值）：\n'
        + '\n'.join(f'  {p}: {names}' for p, names in broken.items())
        + '\n处理方式：要么在视图里传进去，要么把模板改成不依赖它。'
    )


def test_preview_banner_flag_is_explicit(cfg):
    """`preview_mode` 由 context processor 提供，不再靠"变量不存在"当假值。"""
    app = create_app(cfg)
    with app.test_request_context('/'):
        ctx = {}
        for processor in app.template_context_processors[None]:
            ctx.update(processor())
    assert 'preview_mode' in ctx, 'preview_mode 应由 context processor 显式提供'
    assert ctx['preview_mode'] is False


def test_policy_review_note_is_a_block_field_not_a_hint_callout():
    """复核备注不能套用内联提示框；Safari 会把含块级 textarea 的背景拆成碎片。"""
    template = (ROOT / 'policy_collector/templates/policy.html').read_text(encoding='utf-8')
    css = (ROOT / 'policy_collector/static/style.css').read_text(encoding='utf-8')

    assert '<label class="review-note-field">' in template
    assert not re.search(r'<label[^>]*class="[^"]*\bhint\b[^"]*"[^>]*>\s*复核备注', template)
    rule = re.search(r'\.review-note-field\s*\{([^}]*)\}', css)
    assert rule and 'display:block' in rule.group(1).replace(' ', '')


# ── 2. CSS 类名 ───────────────────────────────────────────────
@pytest.mark.parametrize('template_dir,css_path,whitelist', [
    ('policy_collector/templates', 'policy_collector/static/style.css', POLICY_CLASS_WHITELIST),
    ('workbench/templates', 'workbench/static/workbench.css', WORKBENCH_CLASS_WHITELIST),
])
def test_template_class_names_are_defined_in_css(template_dir, css_path, whitelist):
    tdir, css = ROOT / template_dir, ROOT / css_path
    defined = _css_classes(css)
    used: set[str] = set()
    for f in sorted(tdir.glob('*.html')):
        used |= _static_classes(f.read_text(encoding='utf-8'))
    missing = sorted(c for c in used - defined if c not in whitelist)
    assert not missing, (
        f'{template_dir} 里这些类名在 {css_path} 中没有任何定义：{missing}\n'
        '要么补样式，要么把没用的类名删掉；确实只是结构标记的，加进白名单并写明理由。'
    )


# ── 3. 分页参数保留（第一类问题的回归）────────────────────────
def test_runs_pager_keeps_per_page_when_turning_pages(cfg):
    """运行记录页换页时必须带上每页条数。

    旧实现只把"覆盖值"拼进地址，于是从 `?per=100` 点"下一页"会退回默认每页条数——
    用户看到的是"第 2 页内容比第 1 页多了一倍"。
    """
    _seed(cfg, events_per_run=(1, 1))
    from policy_collector.webapp import PAGE_SIZES
    resp = create_app(cfg).test_client().get(f'/runs?per={PAGE_SIZES[-1]}&page=1')
    html = resp.get_data(as_text=True)
    jump = re.search(r'<form class="pg-jump".*?</form>', html, re.S).group(0)
    assert 'name="per"' in jump, '跳页表单没有携带每页条数'
    assert f'value="{PAGE_SIZES[-1]}"' in jump


def test_run_timeline_is_paginated_not_silently_truncated(cfg):
    """步骤明细原来写死 `LIMIT 100`，而单个全国批次实测最多 1635 条事件。

    表现是"更早的步骤在界面上不存在，也没有任何提示说还有更多"。
    """
    _seed(cfg, events_per_run=(250, 120))
    client = create_app(cfg).test_client()
    html = client.get('/runs/run-single').get_data(as_text=True)
    assert '共 120 步' in html, '页面应报出步骤总数，而不是让人以为只有 100 步'
    assert html.count('<li><time>') == 50, '默认每页 50 步'
    assert 'class="pager"' in html, '时间线应有分页条'

    page2 = client.get('/runs/run-single?per=100&page=2').get_data(as_text=True)
    assert page2.count('<li><time>') == 20, '120 步、每页 100 → 第 2 页剩 20 步'


# ── 4. 归因规则与报告 ─────────────────────────────────────────
def test_classify_issue_maps_reason_to_owner():
    owner, kind, _advice = classify_issue('未定位正文容器，整页文本仅供复核，需维护来源适配')
    assert (owner, kind) == ('需改代码适配来源', 'code')
    assert classify_issue('HTTP 404')[1] == 'gone'
    assert classify_issue('附件下载失败，等待补采')[1] == 'self'
    assert classify_issue('某种没见过的新报错')[1] == 'unknown'


def test_parse_error_report_counts_only_current_version(cfg):
    """正文解析异常只统计当前版本政策，且按原因归并。"""
    _seed(cfg)
    d = Database(cfg.db_path)
    report = parse_error_report(d)
    d.close()
    assert report['parse_total'] == 1, '种子里只有一条带 parse_error 的记录'
    assert [g['kind'] for g in report['groups']] == ['code']
    assert report['sites'] and report['sites'][0]['site']


def test_business_skip_is_not_counted_as_failure(cfg):
    """`fetch_records.error` 里混装了"业务判定不收录"和"技术失败"。

    "不收录"是判定明确的结果，不是异常。把它一起当失败数，
    会把"口径已经判完了"报成一屏错误。
    """
    _seed(cfg)
    d = Database(cfg.db_path)
    src = d.list_sources()[0]
    d.add_fetch(src['id'], 'https://example.gov.cn/skip/1', status='skipped',
                title='辛：事务公告', error='命中事务性关键词「公示」，属事务公告而非政策正文，不收录')
    d.add_fetch(src['id'], 'https://example.gov.cn/gone/1', status='failed',
                title='壬：已失效', error='HTTP 404')
    report = parse_error_report(d)
    d.close()
    assert report['business_skipped'] == 1, '「不收录」应单独计数'
    reasons = [r['text'] for g in report['failures'] for r in g['reasons']]
    assert not any('不收录' in r for r in reasons), '「不收录」不该出现在失败列表里'
    assert any('404' in r for r in reasons), '404 应出现在失败列表里'
