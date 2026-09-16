"""政策库列表的排序与分页。

这个功能的"看起来没问题但用起来不对"特别多，所以断言分四层：

  1. **数据层**：排序列白名单、空日期沉底、同日期的稳定次序
  2. **参数层**：越界页码、越界每页条数、非法排序列——手改 URL 不该 500，也不该看到空白
  3. **页面层**：表头箭头/aria-sort、跳页表单、翻页与筛选互不丢参数
  4. **回归层**：待办标签曾经因为变量名写错而**整列不显示**（详见 `test_status_column_*`）

本地实测背景（2026-09-16）：库里 1070 条政策、37 条运行记录。旧实现固定每页 20 条、
只能逐页翻、固定按 id 倒序，于是"看最近发布的政策"这件事在界面上做不到；
`?page=99` 则返回一页空白，而库里其实有一千多条。
"""
from __future__ import annotations

import pytest

from policy_collector.config import AppConfig
from policy_collector.db import POLICY_SORTABLE, Database
from policy_collector.webapp import PAGE_SIZES, POLICY_SORT_COLUMNS, create_app


@pytest.fixture
def cfg(tmp_path):
    c = AppConfig.load()
    c.data_dir = tmp_path
    c.downloads_dir = tmp_path / 'downloads'
    c.db_path = tmp_path / 'db.sqlite'
    return c


def _client(cfg):
    return create_app(cfg).test_client()


def _seed(cfg, rows):
    """写入若干政策；rows 为 (policy_key, 标题, 日期, 地区, 待办, 复核状态)。"""
    db = Database(cfg.db_path)
    for key, title, date, region, todo, review in rows:
        db.add_policy_version(key, {
            'title': title, 'page_date': date, 'region': region,
            'todo_type': todo, 'review_status': review, 'category_names': '准入类',
        })
    db.close()


# 注意日期刻意乱序，且含两条空日期——空日期是"按日期排序"最容易翻车的地方：
# SQLite 升序时 NULL/空串排最前，第一页会变成一堆没有日期的记录。
SEED = [
    ('k1', '甲：2019年政策', '2019-05-01', '浙江', 'none',      'confirmed'),
    ('k2', '乙：2024年政策', '2024-03-08', '江苏', 'review',    'pending'),
    ('k3', '丙：2022年政策', '2022-11-20', '浙江', 'scope',     'pending'),
    ('k4', '丁：无日期政策', '',           '安徽', 'candidate', 'pending'),
    ('k5', '戊：2022年政策（同一天）', '2022-11-20', '广东', 'material', 'pending'),
]


# ── 1. 数据层 ────────────────────────────────────────────────

def test_sort_columns_and_db_whitelist_stay_in_sync():
    """表头能点的列与数据层允许排的列必须一一对应。

    两处各写一份的话，会出现"表头点了没反应"或"排序参数被忽略"这类静默不一致。
    """
    header_keys = {key for key, _label, _dir in POLICY_SORT_COLUMNS}
    assert header_keys == set(POLICY_SORTABLE), '表头排序键与可排序列白名单必须一一对应'
    assert 'date' in header_keys, '日期列必须可排——这是最常被点的一列'


def test_date_sort_puts_empty_dates_last_in_both_directions(cfg):
    """按日期升/降序，没有日期的记录都排在最后。"""
    _seed(cfg, SEED)
    db = Database(cfg.db_path)
    asc = [p['page_date'] for p in db.query_policies(sort='date', order='asc')]
    desc = [p['page_date'] for p in db.query_policies(sort='date', order='desc')]
    db.close()
    non_empty_asc = [d for d in asc if d]
    assert non_empty_asc == sorted(non_empty_asc), f'升序没排好：{asc}'
    assert asc[-1] == '', f'升序时无日期的应沉底：{asc}'
    assert desc[-1] == '', f'降序时无日期的也应沉底：{desc}'
    assert desc[0] == '2024-03-08', f'降序第一条应是最新：{desc}'


def test_same_date_rows_keep_a_stable_order(cfg):
    """同一天发布的多条记录要有固定次序（靠 id 兜底）。

    没有这一层，翻页时会出现"第 2 页看过的条目又出现在第 3 页"。
    """
    _seed(cfg, SEED)
    db = Database(cfg.db_path)
    asc = db.query_policies(sort='date', order='asc')
    desc = db.query_policies(sort='date', order='desc')
    print_date = {p['id']: p['page_date'] for p in db.query_policies()}
    db.close()

    # 同一天的两条（丙、戊）在两种方向下都保持 id 降序
    ties = [p['id'] for p in asc if print_date[p['id']] == '2022-11-20']
    assert len(ties) == 2, f'样例里应有两条同日期记录，实际 {ties}'
    assert ties == sorted(ties, reverse=True), f'同日期次序应为 id 降序：{ties}'
    # 升序与降序都是同一批记录的整体反转关系，不能各排各的
    assert sorted(p['id'] for p in asc) == sorted(p['id'] for p in desc)
    assert [p['id'] for p in asc] != [p['id'] for p in desc]


def test_count_matches_query_for_every_filter(cfg):
    """总数与列表必须用同一套筛选条件。

    否则会出现"共 137 条，翻到第 7 页是空的"——用户只能自己猜哪里不对。
    """
    _seed(cfg, SEED)
    db = Database(cfg.db_path)
    cases = [
        ({}, None),
        ({'region': '浙江'}, '浙江'),
        ({'todo': 'review'}, None),
        ({'keyword': '2022年政策'}, None),
        ({'review_status': 'pending'}, None),
    ]
    for kwargs, region in cases:
        count = db.count_policies(**kwargs)
        rows = db.query_policies(limit=999, **kwargs)
        assert count == len(rows), f'{kwargs} 总数 {count} 与实际 {len(rows)} 不一致'
        if region:
            assert all(r['region'] == region for r in rows)
    db.close()


# ── 2. 参数层 ────────────────────────────────────────────────

def test_page_beyond_range_clamps_instead_of_showing_blank(cfg):
    """页码越界要夹到最后一页并说明，而不是给一页空白。"""
    _seed(cfg, SEED)
    c = _client(cfg)
    body = c.get('/policies?page=999').get_data(as_text=True)
    assert '超出范围' in body, '越界时应有说明，否则用户以为库里没东西'
    assert '丙：2022年政策' in body or '甲：2019年政策' in body, '越界后应显示最后一页的真实数据'
    assert '共 <b>5</b> 条' in body


def test_unknown_sort_and_order_fall_back_without_error(cfg):
    """手改错的排序参数安静退回默认视图，不报错、也不 500。"""
    _seed(cfg, SEED)
    c = _client(cfg)
    default = c.get('/policies').get_data(as_text=True)
    for bad in ['/policies?sort=坏值&order=乱七八糟', '/policies?sort=1;DROP TABLE policies;--',
                '/policies?sort=title%20--&order=asc']:
        r = c.get(bad)
        assert r.status_code == 200, bad
    assert c.get('/policies?sort=坏值&order=xx').get_data(as_text=True) == default
    # 注入尝试不该影响数据
    db = Database(cfg.db_path)
    assert db.count_policies() == 5
    db.close()


def test_per_page_only_accepts_whitelisted_sizes(cfg):
    """每页条数只认白名单：`per=0`（除零）、`per=100000`（拖垮页面）都要被挡回默认。"""
    _seed(cfg, SEED)
    c = _client(cfg)
    for bad in ('0', '-5', '100000', 'abc'):
        body = c.get(f'/policies?per={bad}').get_data(as_text=True)
        assert f'value="{PAGE_SIZES[0]}" selected' in body, f'per={bad} 应回落到默认 {PAGE_SIZES[0]}'
    body = c.get(f'/policies?per={PAGE_SIZES[-1]}').get_data(as_text=True)
    assert f'value="{PAGE_SIZES[-1]}" selected' in body
    assert '共 <b>5</b> 条 · 第 1 / 1 页' in body


# ── 3. 页面层 ────────────────────────────────────────────────

def test_sort_headers_expose_state_and_arrows(cfg):
    """表头要能看出"现在按什么排、点了会怎样"，并给读屏器 aria-sort。"""
    _seed(cfg, SEED)
    c = _client(cfg)
    body = c.get('/policies?sort=date&order=asc').get_data(as_text=True)
    assert 'aria-sort="ascending"' in body, '当前排序列要标出方向'
    assert '排序：日期升序' in body
    assert 'class="th-sort on"' in body
    # 点当前列应翻转方向；点未排序列用该列默认方向
    assert 'sort=date&amp;order=desc' in body or 'sort=date' in body
    assert 'sort=title&amp;order=asc' in body
    # 换排序时应回到第 1 页
    assert 'sort=date&amp;order=desc&amp;page' not in body


def test_pager_preserves_filters_and_resets_page_on_filter_change(cfg):
    """翻页/跳页带着筛选与排序；改筛选条件则回到第 1 页。"""
    _seed(cfg, SEED)
    c = _client(cfg)
    body = c.get('/policies?q=政策&per=50&sort=title&order=asc&page=2').get_data(as_text=True)
    # 跳页表单把当前条件都带成隐藏域
    jump = body.split('class="pg-jump"')[1].split('</form>')[0]
    assert 'name="q"' in jump and 'name="sort"' in jump and 'name="per"' in jump
    assert 'name="page"' in jump and 'max="1"' in jump
    # 筛选表单带排序与每页条数，但**不带 page**（改条件要看第 1 页）
    filt = body.split('<form class="filter"')[1].split('</form>')[0]
    assert 'name="sort" value="title"' in filt and 'name="per" value="50"' in filt
    assert 'name="page"' not in filt


# ── 4. 回归层：待办标签曾经整列不显示 ────────────────────────

def test_status_column_shows_todo_name_and_owner(cfg):
    """「状态 / 待办」列必须显示**中文待办类型与责任方**。

    回归背景：模板里写的是 `todo_names` / `todo_owners` 两个从未传入的变量，
    Jinja 的 Undefined 让 `in` 判断静默为假，于是带待办的行走进了 else 分支，
    整列把原始英文 `review_status`（pending）当成绿色"正常"标签印出来——
    20 行全是 `pending`，而真正的待办信息一个字都没显示。列表页的主信息因此是错的。
    """
    _seed(cfg, SEED)
    c = _client(cfg)
    body = c.get('/policies').get_data(as_text=True)
    assert '>pending<' not in body, '不应把原始 review_status 当标签印在页面上'
    for name, owner in (('结论待确认', '业务确认'), ('待定口径', '业务拍板'),
                        ('材料待补', '机器自修'), ('高置信候选', '业务抽检')):
        assert name in body and owner in body, f'{name} / {owner} 应出现在状态列'
    assert body.count('class="tag warn"') >= 2, '业务队列（review/scope）应是警示色'


def test_rejected_row_shows_rejected_tag(cfg):
    """已剔除的行显式标注，而不是靠"没有待办"暗示。"""
    _seed(cfg, [('k9', '己：已剔除', '2021-01-01', '浙江', 'none', 'rejected')])
    db = Database(cfg.db_path)
    # 默认视图不显示已剔除；按状态筛出来才看得到
    assert db.count_policies() == 0
    assert db.count_policies(review_status='rejected') == 1
    db.close()
    body = _client(cfg).get('/policies?review=rejected').get_data(as_text=True)
    assert '已剔除' in body


# ── 5. 导出当前筛选 ──────────────────────────────────────────

def test_export_csv_follows_filters_and_carries_bom(cfg):
    """导出必须跟着当前筛选与排序走，且带 BOM（否则 Excel 里中文是乱码）。"""
    _seed(cfg, SEED)
    c = _client(cfg)

    r = c.get('/policies/export.csv')
    assert r.status_code == 200
    assert 'text/csv' in r.headers['Content-Type']
    assert 'attachment' in r.headers['Content-Disposition']
    text = r.get_data(as_text=True)
    assert text.startswith('\ufeff'), '缺少 UTF-8 BOM，Excel 打开会乱码'
    body = text.lstrip('\ufeff').strip().splitlines()
    assert len(body) == 1 + 5, f'默认视图应导出 5 条 + 表头：{len(body)}'
    assert 'service' not in r.headers['Content-Disposition'], '文件名保持 ASCII'

    # 筛选后条数与列表页口径一致
    rows_total = Database(cfg.db_path).count_policies(region='浙江')
    only_zj = c.get('/policies/export.csv?region=%E6%B5%99%E6%B1%9F').get_data(as_text=True)
    assert len(only_zj.lstrip('\ufeff').strip().splitlines()) == 1 + rows_total
    assert '江苏' not in only_zj

    # 排序跟着走：降序第一行应是 2024 年那条
    desc = c.get('/policies/export.csv?sort=date&order=desc').get_data(as_text=True)
    first_data_row = desc.lstrip('\ufeff').strip().splitlines()[1]
    assert '乙：2024年政策' in first_data_row

    # 非法排序参数不报错，安静回落
    assert c.get('/policies/export.csv?sort=坏值&order=xx').status_code == 200


def test_export_button_appears_only_when_there_is_something(cfg):
    """没有匹配结果时不摆一个点了会导出空文件的按钮。"""
    _seed(cfg, SEED)
    c = _client(cfg)
    assert 'policies/export.csv' in c.get('/policies').get_data(as_text=True)
    empty = c.get('/policies?q=不存在的关键词').get_data(as_text=True)
    assert 'policies/export.csv' not in empty
    assert '未找到匹配的政策' in empty


# ── 6. 运行记录分页 ──────────────────────────────────────────

def test_runs_page_paginates(cfg):
    """处理进度页以前固定只取最近 50 条且没有翻页，更早的批次等于不存在。"""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path)
    for i in range(45):
        db.start_run(f'run-{i:04d}', None, kind='manual')
        db.finish_run(f'run-{i:04d}', {'ingested': i}, status='ok')
    assert db.count_runs() == 45
    assert db.count_runs(status='running') == 0
    c = _client(cfg)
    body = c.get('/runs').get_data(as_text=True)
    assert '共 <b>45</b> 条 · 第 1 / 3 页' in body
    body = c.get('/runs?page=3').get_data(as_text=True)
    assert '第 3 / 3 页' in body
    assert 'run-0000' in body, '最后一页应能看到最早的记录'
    body = c.get('/runs?page=99').get_data(as_text=True)
    assert '超出范围' in body and '第 3 / 3 页' in body
    db.close()
