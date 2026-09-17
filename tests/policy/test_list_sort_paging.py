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

import re

import pytest

from policy_collector.config import AppConfig
from policy_collector.db import POLICY_SORTABLE, Database
from policy_collector.webapp import (PAGE_SIZES, POLICY_SORT_COLUMNS, create_app,
                                     pager_qs, policy_order_default,
                                     resolve_policy_sort)


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
    """写入若干政策。

    rows 为 `(policy_key, 标题, 日期, 地区, 待办, 复核状态[, 类别[, 文号]])`；
    类别与文号可选，默认分别取 `准入类` 与空串（老的调用点不受影响）。
    """
    db = Database(cfg.db_path)
    for row in rows:
        key, title, date, region, todo, review, *rest = row
        db.add_policy_version(key, {
            'title': title, 'page_date': date, 'region': region,
            'todo_type': todo, 'review_status': review,
            'category_names': (rest[0] if rest else '准入类'),
            'wenhao': (rest[1] if len(rest) > 1 else ''),
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

#: 专门给"表头连点"用的样例：**七列的值两两不同**，所以升序与降序必然
#: 给出不同的首行——这样"方向变了但数据没变"也能被当场抓住。
#: 用 SEED 是不行的：那批数据类别全是「准入类」、文号全空，
#: 按这两列排升降序得到的是同一份结果（并列时都回落到 id 兜底键）。
TOGGLE_SEED = [
    ('t1', '甲：最早',  '2019-01-01', '浙江', 'none',      'confirmed', '引导类',     '浙政1号'),
    ('t2', '乙：第二',  '2021-01-01', '江苏', 'review',    'pending',   '准入类',     '苏政2号'),
    ('t3', '丙：第三',  '2023-01-01', '安徽', 'scope',     'pending',   '保障类',     '皖政3号'),
    ('t4', '丁：第四',  '2025-01-01', '广东', 'candidate', 'pending',   '激励约束类', '粤政4号'),
    ('t5', '戊：最新',  '2027-01-01', '山东', 'material',  'pending',   '其他类',     '鲁政5号'),
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
    # 点当前列应翻转方向。注意这里**不能**只断言"地址里有 order=desc"：
    # 省略 order 是合法的，只要该列自己的默认方向恰好就是 desc（路由会回落过去）。
    # 所以这一层只断言"声明出来的意图"，真实落点交给下面 test_every_header_*
    # 去走完整的点击链路——那才是唯一可信的判据。
    assert '按「日期」降序排列' in body, '当前列的链接应声明翻转后的方向'
    assert 'href="/policies?sort=date"' in body, 'date 默认就是 desc，省略 order 后仍落到 desc'
    assert 'href="/policies?sort=title"' in body, '未排序列用该列默认方向（asc，可省略 order）'
    assert 'href="/policies"' in body, 'id 列默认 desc → 点它回到干净的默认视图'
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


# ── 7. 表头连点：每一列都必须能来回切（回归） ────────────────
#
# 回归背景（2026-09-17，用户报"表头点一下可以，再点一下除了日期之外都不能排序了"）：
# `order` 的默认值在 `pager_qs` 的 defaults 里被写死成 **id 列的 `desc`**，
# 而"与默认值相同的参数不写进地址"（规则②）是拿字符串比的。于是：
#   · 标题列的默认方向是 asc，第二次点击要 desc → 与那个写死的默认值相同
#     → 从地址里被抹掉 → 路由回落到"标题列默认 asc" → **永远升序**；
#   · 日期列的默认方向恰好也是 desc → 抹掉后回落得到 desc → 结果正确，
#     **所以只点日期的开发者会以为一切正常**。
# 一个值（`desc`）同时被当成"id 列的默认方向"和"标题列的降序"，含义不同却撞在一起。
#
# 这组用例走**真实的点击链路**（抓页面上的 href 再请求它），不自己拼 URL——
# 缺陷恰恰在 href 上，自己拼 URL 会绕过它。

_HEADER_LINK = re.compile(
    r'<a class="th-sort[^"]*" href="([^"]+)"[^>]*title="按「([^」]+)」([^"]+)排列"')
_CAPTION = re.compile(r'排序：([^<·]+?)\s*·\s*点表头可换')
_FIRST_TITLE = re.compile(r'<td class="tt"><a[^>]*>([^<]+)</a>')

_DIR_NAME = {'asc': '升序', 'desc': '降序'}


def _header_links(html):
    """表头显示名 -> (点它的地址, 点下去的意图方向)。"""
    return {label: (href.replace('&amp;', '&'), intent)
            for href, label, intent in _HEADER_LINK.findall(html)}


def _click_series(c, start, label, times):
    """从 start 开始连点 label 表头 times 次，返回 (方向序列, 首行标题序列)。"""
    dirs, firsts = [], []
    path = start
    for _ in range(times):
        html = c.get(path).get_data(as_text=True)
        dirs.append(_CAPTION.search(html).group(1).strip().replace(label, ''))
        firsts.append(_FIRST_TITLE.search(html).group(1))
        path = _header_links(html)[label][0]
    return dirs, firsts, path


@pytest.mark.parametrize('key,label,default', POLICY_SORT_COLUMNS)
def test_every_header_toggles_back_and_forth(cfg, key, label, default):
    """切到该列后连点 4 次表头：方向必须严格交替，**且首行数据真的跟着变**。

    只断言"方向文字变了"还不够——地址对、文案对、SQL 没生效是完全可能的；
    所以同时要求升序与降序的首行标题不同（TOGGLE_SEED 保证七列取值两两不同）。
    """
    _seed(cfg, TOGGLE_SEED)
    c = _client(cfg)
    other = 'desc' if default == 'asc' else 'asc'
    dirs, firsts, _ = _click_series(c, f'/policies?sort={key}&order={default}', label, 4)
    expected = [_DIR_NAME[default], _DIR_NAME[other]] * 2
    assert dirs == expected, f'「{label}」表头不能来回切换，实际方向轨迹 {dirs}'
    assert firsts[0] != firsts[1], (
        f'「{label}」方向文字变了但首行数据没变，排序没真正生效：{firsts[:2]}')
    # 箭头必须与方向一致（否则用户看到的还是"点了没反应"）
    html = c.get(f'/policies?sort={key}&order={default}').get_data(as_text=True)
    assert 'aria-sort="' + ('ascending' if default == 'asc' else 'descending') + '"' in html


def test_current_column_link_never_drops_order(cfg):
    """当前排序列的链接必须**显式带上翻转后的方向**。

    这条是上面那个缺陷的最小复现：旧实现在 `?sort=title&order=asc` 上给出的
    是 `?sort=title`（order 被当默认值抹掉），点下去回落成 asc，等于没反应。
    """
    _seed(cfg, TOGGLE_SEED)
    c = _client(cfg)
    for key, label, default in POLICY_SORT_COLUMNS:
        other = 'desc' if default == 'asc' else 'asc'
        html = c.get(f'/policies?sort={key}&order={default}').get_data(as_text=True)
        href = _header_links(html)[label][0]
        assert f'order={other}' in href, (
            f'「{label}」在 {default} 下应给出 order={other}，实际是 {href}')


def test_order_default_is_derived_from_sort_column():
    """`pager_qs` 的 defaults 允许传函数，且按"这一份地址里生效的 sort"求值。

    这是修法的机制本身：`order` 的默认方向随列而变，用固定字符串表达不了。
    """
    defaults = {'sort': 'id', 'order': policy_order_default, 'per': 20, 'page': 1}
    qs, base = pager_qs({'sort': 'title', 'order': 'asc', 'per': 20}, defaults)
    # title 的默认方向就是 asc → base 里不必重复带上 order
    assert base == {'sort': 'title'}
    # 目标列是 date（默认 desc）时，order=desc 属于默认值，可以省
    assert qs(sort='date', order='desc') == '?sort=date'
    # 目标列是 title（默认 asc）时，order=desc 是**非默认**，绝不能省
    assert qs(sort='title', order='desc') == '?sort=title&order=desc'
    # 不换列时按当前列求默认值：title 下 asc 是默认，于是出现干净的 ?sort=title
    assert qs(sort='title', order='asc') == '?sort=title'
    # id 列默认 desc：order=asc 必须保留
    assert qs(sort='id', order='asc') == '?order=asc'
    assert qs(sort='id', order='desc') == ''
    for key, _label, direction in POLICY_SORT_COLUMNS:
        assert resolve_policy_sort(key, '') == (key, direction), f'{key} 缺 order 时应用该列默认方向'


def test_list_and_export_agree_on_order(cfg):
    """同一份地址，页面首行与导出首行必须是同一条。

    列表页与导出页各解析一次 sort/order 是这类缺陷的温床：两边对
    "order 缺失时算哪个方向"的理解一旦不同，导出顺序就和页面不一样，
    而两边都是 200，没人会先怀疑排序。
    """
    _seed(cfg, TOGGLE_SEED)
    c = _client(cfg)
    for query in ('', '?sort=title', '?sort=title&order=desc', '?sort=date&order=asc',
                  '?sort=region&order=desc', '?sort=坏值&order=xx'):
        html = c.get('/policies' + query).get_data(as_text=True)
        page_first = _FIRST_TITLE.search(html).group(1)
        csv_text = c.get('/policies/export.csv' + query).get_data(as_text=True)
        export_first = csv_text.lstrip('\ufeff').strip().splitlines()[1].split(',')[1]
        assert page_first in export_first or export_first in page_first, (
            f'{query or "(默认)"} 页面首行「{page_first}」与导出行首「{export_first}」不一致')
