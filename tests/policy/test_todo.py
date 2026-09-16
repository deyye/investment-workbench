"""待办类型：把"要不要人管"这件事从判定环节一直传到界面。

这些测试守四条不变式：
1. **中间档**：规则判正且证据强 → `candidate`（业务抽检），**绝不自动入库**——
   这是业务方 2026-09-14 的裁决，也是本项目最容易"改着改着就退化"的一条。
2. 材料不完整属机器自修，**不占业务待办队列**（材料判据 = 有没有拿到正文）
3. 待办类型是**存进库的字段**，页面统计与列表过滤必须读同一个字段
4. 每个类型都要有中文名与责任方（页面直接渲染，缺了会露出英文键名）
"""
import pytest

from policy_collector.classifier import Classifier
from policy_collector.config import AppConfig, SourceConfig
from policy_collector.models import Document
from policy_collector.pipeline import Pipeline
from policy_collector.todo import (
    CANDIDATE, HUMAN_QUEUES, MATERIAL, NONE, REVIEW, SCOPE, SYSTEM,
    TODO_META, TODO_ORDER, document_incomplete, missing_text,
)


@pytest.fixture
def cfg():
    return AppConfig.load()


@pytest.fixture
def pipe(tmp_path):
    c = AppConfig.load()
    c.db_path = tmp_path / 'policy.db'
    c.data_dir = tmp_path
    c.downloads_dir = tmp_path / 'downloads'
    c.sources = {'test': SourceConfig(name='test', site='合成官网', list_url='https://agency.gov.cn/policy/')}
    c.fetch.retries = 0
    c.fetch.request_interval_seconds = 0
    p = Pipeline(c)
    yield p
    p.close()


def judge(cfg, title, content, attachments=None, prefer='rule'):
    doc = Document(title=title, content=content, attachments=attachments or [])
    return Classifier(cfg).classify(doc, prefer=prefer)


def seed(pipe, rows):
    """直接写库造样本：待办统计只关心 todo_type 列，不必走完整采集链路。"""
    with pipe.db.tx() as c:
        for i, (title, todo) in enumerate(rows, start=1):
            c.execute(
                'INSERT INTO policies(policy_key,version,title,is_investment_policy,need_review,'
                'review_status,todo_type,region) VALUES(?,?,?,?,?,?,?,?)',
                (f'k{i}', 1, title, 'yes', int(todo in ('candidate', 'review', 'scope')),
                 'pending', todo, '甲'))
    return pipe


# ---------- 1. 中间档：高置信候选不静默入库 ----------

def test_strong_evidence_goes_to_candidate_not_auto_confirm(cfg):
    """强判据达标（≥2）→ `candidate`，且 need_review 仍为 True。

    这是"中间档"的核心：机器很确定的那批**不直接入库**，单独成列供抽检。
    若哪天有人把它改回 `none`（=自动确认），这条测试会红——
    那等于把"机器建议"当成"业务确认"，误收会静默进库、事后无法解释。
    """
    # B1 强判据 + B2 生命周期锚点，共 2 个 → 达到 min_strong_for_auto_confirm
    r = judge(cfg, '关于规范企业投资项目管理的通知',
              '企业投资项目实行核准管理，项目单位应当办理用地预审手续。')
    assert r.todo_type == CANDIDATE, r.todo_type
    assert r.need_review is True, '候选也必须算待办：没被人看过之前不能算已确认'


def test_single_thin_hit_goes_to_review(cfg):
    """只命中 1 个强判据、标题又没体现投资主题 → 证据偏薄，转「结论待确认」。

    仍判「收」（业务方"讲到投资的就收"的低门槛），但不能当高置信候选。
    """
    r = judge(cfg, '关于深化重点领域改革的若干意见', '严格执行用地预审制度，加强源头管控。')
    assert r.is_investment_policy == 'yes'
    assert r.todo_type == REVIEW, r.todo_type


def test_undecidable_relevance_goes_to_scope(cfg):
    """相关性判不出来 → `scope`（待定口径，要业务方拍板），不是 review。

    两者都由人处理，但行动不同：review 是"看这条判得对不对"，
    scope 是"定一条口径"。混在一起会让待办清单失去行动指向。
    """
    r = judge(cfg, '关于推进重点领域改革的若干意见',
              '落实投资主体责任，规范项目流程，简化审批环节，完善核准与备案管理。')
    assert r.is_investment_policy == 'pending'
    assert r.todo_type == SCOPE, r.todo_type


# ---------- 2. 材料不完整 = 机器自修，不占业务队列 ----------

def test_incomplete_material_is_machine_work(cfg):
    """附件拿不到正文 → material，且 need_review 必须为 False。

    否则"附件没下全"会混进业务待办，人的清单里全是机器能自己修的东西。
    """
    r = judge(cfg, '关于规范企业投资项目管理的通知', '企业投资项目实行核准管理。',
              attachments=[{'name': 'a.pdf', 'parse_status': 'failed', 'parsed_text': ''}])
    assert r.todo_type == MATERIAL
    assert r.need_review is False


def test_partial_with_text_is_not_material():
    """`partial` 表示文本已提取、只是转换过程有提示，**不算材料缺失**。

    实测：旧版 doc 经 textutil 转换后 parse_status='partial' 但正文完整。
    按 `parse_status != 'ok'` 判定会让这类条目永远挂在"材料待补"里——
    机器反复重试也修不掉，因为材料本来就是好的。
    """
    att = [{'parse_status': 'partial', 'parsed_text': '已成功提取的正文'}]
    assert not missing_text(att)
    assert not document_incomplete(type('D', (), {'parse_error': '', 'attachments': att})())


def test_attachment_without_text_is_material_regardless_of_status():
    """反过来：无论标成什么状态，只要拿不到正文就算材料待补。"""
    for status in ('failed', 'needs_ocr', 'unsupported', 'not_parsed', 'ok', ''):
        assert missing_text([{'parse_status': status, 'parsed_text': ''}]), status


def test_document_incomplete_is_the_single_judgement():
    """正文解析失败也算材料不完整（否则会带着 parse_error 去判结论）。"""
    D = type('D', (), {'parse_error': '旧解析器不支持', 'attachments': []})
    assert document_incomplete(D())
    assert document_incomplete(type('D', (), {'parse_error': '', 'attachments': []})()) is False


# ---------- 3. 统计与过滤读同一个字段 ----------

def test_todo_overview_matches_filtered_list(pipe):
    """格子上的数字，必须等于点进去看到的条数。"""
    seed(pipe, [
        ('A', MATERIAL), ('B', MATERIAL),
        ('C', SYSTEM),
        ('D', CANDIDATE), ('E', CANDIDATE), ('F', CANDIDATE),
        ('G', REVIEW),
        ('H', SCOPE),
        ('I', NONE),
    ])
    ov = pipe.db.todo_overview()
    assert ov['counts'][MATERIAL] == 2
    assert ov['counts'][SYSTEM] == 1
    assert ov['counts'][CANDIDATE] == 3
    assert ov['counts'][REVIEW] == 1
    assert ov['counts'][SCOPE] == 1
    assert ov['counts'][NONE] == 1
    for key, expect in ((MATERIAL, 2), (SYSTEM, 1), (CANDIDATE, 3),
                        (REVIEW, 1), (SCOPE, 1), (NONE, 1)):
        assert len(pipe.db.query_policies(todo=key, limit=100)) == expect, key
    assert [c['key'] for c in ov['cells']] == list(TODO_ORDER)
    assert all('count' in c and 'owner' in c for c in ov['cells'])


def test_human_queue_excludes_machine_and_spotcheck(pipe):
    """"需你处理"只算要人**逐条**判的。

    candidate 是抽检（扫一眼翻转）、material/system 是机器与运维的活，
    都不该算到人头上——否则又变回那个没有行动指向的总数。
    """
    seed(pipe, [
        ('A', MATERIAL), ('B', SYSTEM), ('C', CANDIDATE),
        ('D', REVIEW), ('E', SCOPE),
    ])
    ov = pipe.db.todo_overview()
    assert ov['human'] == 2, ov['human']            # review + scope
    assert ov['machine'] == 1 and ov['system'] == 1
    assert set(HUMAN_QUEUES) == {REVIEW, SCOPE}


# ---------- 4. 元数据完整 ----------

def test_metadata_covers_every_type():
    """每个类型都要有中文名与责任方——页面直接用它渲染。"""
    for key in (MATERIAL, SYSTEM, CANDIDATE, REVIEW, SCOPE, NONE):
        name, owner, note = TODO_META[key]
        assert name and owner and note, key
    # "无需处理"不进待办清单
    assert NONE not in TODO_ORDER
    assert set(TODO_ORDER) == {MATERIAL, SYSTEM, CANDIDATE, REVIEW, SCOPE}
