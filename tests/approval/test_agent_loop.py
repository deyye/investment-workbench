"""agent 循环原型（app/agent_loop.py）的回归测试。

重点覆盖三件事：
1. 自检能不能**发现**主干静默失败（有建设内容章节却 0 条指标）；
2. 规划器是否按 放宽章节 → 备用形态 → 转人工 的次序推进，且**必然终止**；
3. 循环是否只增加指标与告警，不动已抽出的字段值。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz

from app import agent_loop as A
from app.extract import extract, mark_conflicts
from app.model_client import ModelError, set_config_path

# 主干模式要求「≥2 字 + 度量后缀 + 数值」，因此「工作内容名 + 数值 + 单位」抽不到；
# 这正是那 4 份公开批文指标为空的形态（滨海植被修复22.85hm2）。
ALT_FORM_BODY = [
    '你单位《关于要求审批瑞安市历史围填海项目生态修复工程初步设计的申请报告》收悉。',
    '一、建设内容：本工程完成植被修复4702平方米。',
]

# 建设内容章节存在，但全文没有任何数值指标 —— 主干会静默返回 0 条。
NO_METRIC_BODY = [
    '你单位《关于要求审批某项目初步设计的申请报告》收悉。',
    '一、建设内容：本工程按批复要求实施相关工程内容。',
]

PLAIN_BODY = [
    '你单位《关于要求审批某项目初步设计的申请报告》收悉。',
    '一、建设内容：新建污水管网1501m，截污纳管管道1250米，污水检查井58座。',
]


def _pdf(td, lines, name='doc.pdf'):
    # 字号 10：insert_text 超出页宽的部分会被静默裁掉，
    # 夹具用 12pt 时 44 字的中文行（≈528pt）加 x=80 会越界，导致断言看起来像代码 bug。
    path = Path(td) / name
    doc = fitz.open()
    page = doc.new_page()
    y = 60
    page.insert_text((80, y), '瑞发改投〔2026〕87号', fontname='china-s', fontsize=10)
    y += 26
    for text in lines:
        page.insert_text((80, y), text, fontname='china-s', fontsize=10)
        y += 26
    doc.save(path)
    return path


def _run(td, lines, **kw):
    p = _pdf(td, lines)
    return A.run_agent(p, p.name, '0' * 32, **kw)


class SnapshotTests(unittest.TestCase):
    """agent 的「读原文」工具：从行重建章节。"""

    def test_snapshot_rebuilds_sections_and_text(self):
        with tempfile.TemporaryDirectory() as td:
            r = extract(_pdf(td, PLAIN_BODY), 'd.pdf', '0' * 32, False)
        ctx = A.snapshot(r['lines'])
        self.assertTrue(ctx['text'])
        self.assertEqual(len(ctx['text']), len(''.join(l['text'] for l in r['lines'])))
        self.assertTrue(any('建设内容' in s['heading'] for s in ctx['sections']))
        self.assertTrue(any('建设内容' in s['heading'] for s in A.pick(ctx, A.SECTION_WORDS)))

    def test_widened_words_cover_engineering_headings(self):
        # 主干判据漏掉「工程任务和规模」；放宽词表应覆盖它。
        self.assertFalse(any(w in '二、工程任务和规模' for w in A.SECTION_WORDS))
        self.assertTrue(any(w in '二、工程任务和规模' for w in A.SECTION_WORDS_WIDE))


class SelfCheckTests(unittest.TestCase):
    """自检必须能区分「确实没有该要素」与「有但没抽到」。"""

    def test_metric_empty_detected_when_body_section_yields_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            r = extract(_pdf(td, NO_METRIC_BODY), 'd.pdf', '0' * 32, False)
        self.assertEqual(len(r['metrics']), 0)
        codes = {i['code'] for i in A.self_check(r, A.snapshot(r['lines']))}
        self.assertIn('METRIC_EMPTY', codes)

    def test_metric_empty_not_raised_when_metrics_present(self):
        with tempfile.TemporaryDirectory() as td:
            r = extract(_pdf(td, PLAIN_BODY), 'd.pdf', '0' * 32, False)
        self.assertTrue(r['metrics'])
        codes = {i['code'] for i in A.self_check(r, A.snapshot(r['lines']))}
        self.assertNotIn('METRIC_EMPTY', codes)

    def test_field_empty_hint_uses_cue_not_absence(self):
        # 字段为空但原文有线索 -> 报 hint；原文无线索 -> 不报（否则任何缺失字段都会误报）。
        with tempfile.TemporaryDirectory() as td:
            r = extract(_pdf(td, PLAIN_BODY), 'd.pdf', '0' * 32, False)
        ctx = A.snapshot(r['lines'])
        r['fields']['建设周期'] = {'value': None, 'status': 'missing', 'evidence': [], 'method': 'rule'}
        codes = {i['code'] for i in A.self_check(r, ctx)}
        self.assertNotIn('FIELD_EMPTY_HINT', codes)

        ctx2 = dict(ctx, text=ctx['text'] + '项目建设工期为24个月')
        codes2 = {i['code'] for i in A.self_check(r, ctx2)}
        self.assertIn('FIELD_EMPTY_HINT', codes2)


class RulePlannerTests(unittest.TestCase):
    """确定性规划器：动作次序固定，且用尽后返回 None（保证终止）。"""

    def setUp(self):
        self.planner = A.RulePlanner()
        self.issue = [{'code': 'METRIC_EMPTY', 'severity': 'high', 'detail': 'x'}]

    def test_sequence_widen_then_alt_form_then_escalate(self):
        tried = set()
        picks = []
        for _ in range(4):
            d = self.planner.decide(self.issue, {}, {}, tried)
            if not d:
                break
            picks.append(d['action'])
            tried.add(d['action'])
        self.assertEqual(picks, ['widen_sections', 'alt_form_metrics', 'escalate'])
        self.assertIsNone(self.planner.decide(self.issue, {}, {}, tried))

    def test_no_action_when_no_issues(self):
        self.assertIsNone(self.planner.decide([], {}, {}, set()))


class AgentLoopTests(unittest.TestCase):
    """循环端到端：补救、升级、终止、不改字段。"""

    def test_alt_form_action_recovers_metric_main_pipeline_misses(self):
        with tempfile.TemporaryDirectory() as td:
            base = extract(_pdf(td, ALT_FORM_BODY), 'd.pdf', '0' * 32, False)
        self.assertEqual(len(base['metrics']), 0)
        with tempfile.TemporaryDirectory() as td:
            r = _run(td, ALT_FORM_BODY)
        self.assertEqual(len(r['metrics']), 1)
        self.assertEqual(r['metrics'][0]['value'], '4702平方米')
        self.assertEqual(r['metrics'][0]['method'], 'agent-alt-form')
        self.assertTrue(r['metrics'][0]['normalized'])
        self.assertTrue(r['metrics'][0]['evidence'])
        self.assertEqual(r['agent']['status'], 'ok')

    def test_escalates_and_warns_when_unrecoverable(self):
        with tempfile.TemporaryDirectory() as td:
            r = _run(td, NO_METRIC_BODY)
        self.assertEqual(r['agent']['status'], 'escalated')
        self.assertTrue(any('agent/METRIC_EMPTY' in w for w in r['warnings']))
        # 升级必须是最后一步，不能继续空转。
        self.assertEqual(r['agent']['trace'][-1]['action'], 'escalate')

    def test_trace_is_bounded_by_max_attempts(self):
        with tempfile.TemporaryDirectory() as td:
            r = _run(td, NO_METRIC_BODY, max_attempts=2)
        self.assertLessEqual(len(r['agent']['trace']), 2)

    def test_healthy_document_exits_without_actions(self):
        with tempfile.TemporaryDirectory() as td:
            r = _run(td, PLAIN_BODY)
        self.assertEqual(r['agent']['status'], 'ok')
        self.assertEqual(r['agent']['trace'], [])
        self.assertEqual(r['agent']['steps'], 0)

    def test_agent_does_not_overwrite_extracted_fields(self):
        with tempfile.TemporaryDirectory() as td:
            base = extract(_pdf(td, ALT_FORM_BODY), 'd.pdf', '0' * 32, False)
        with tempfile.TemporaryDirectory() as td:
            r = _run(td, ALT_FORM_BODY)
        for k, c in base['fields'].items():
            self.assertEqual(r['fields'][k]['value'], c['value'], '字段 %s 被 agent 改动了' % k)


class PlannerIsolationTests(unittest.TestCase):
    """循环与大脑解耦：没有模型密钥时，规则规划器仍可工作。"""

    def setUp(self):
        # 关掉配置文件：这组测试验证「没有可用配置」时的行为，
        # 不能因为本机界面上配过模型就跟着漂移。
        set_config_path(None);self.addCleanup(set_config_path,None)

    def test_llm_planner_refuses_without_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ModelError):
                A.LLMPlanner()

    def test_rule_planner_needs_no_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(A.RulePlanner().name, 'rule')

    def test_audit_reports_baseline_silent_issues(self):
        with tempfile.TemporaryDirectory() as td:
            p = _pdf(td, ALT_FORM_BODY)
            rep = A.audit(p, p.name, '0' * 32)
        self.assertEqual(rep['baseline']['metrics'], 0)
        self.assertEqual(rep['baseline']['warnings'], 0)
        self.assertTrue(rep['baseline']['silent_issues'])
        self.assertEqual(rep['agent']['metrics'], 1)


class ConflictJoinTests(unittest.TestCase):
    """agent 追加的指标必须并入同名冲突检测（extract 内部的检测跑在追加之前）。"""

    @staticmethod
    def _metric(name, value):
        return {'name': name, 'value': value, 'status': 'extracted', 'evidence': [],
                'method': 'agent-alt-form', 'normalized': {'number': value}, 'scope': 'construction'}

    def test_mark_conflicts_marks_same_name_different_values(self):
        r = {'metrics': [self._metric('总面积', '22.85hm2'), self._metric('总面积', '5.40hm2')],
             'warnings': []}
        mark_conflicts(r)
        self.assertTrue(all(m['status'] == 'conflict' for m in r['metrics']))
        self.assertTrue(any('总面积' in w for w in r['warnings']))

    def test_mark_conflicts_leaves_single_value_alone(self):
        r = {'metrics': [self._metric('总面积', '22.85hm2')], 'warnings': []}
        mark_conflicts(r)
        self.assertEqual(r['metrics'][0]['status'], 'extracted')
        self.assertEqual(r['warnings'], [])

    def test_mark_conflicts_is_idempotent(self):
        # agent 循环会对同一 result 调两次（extract 内一次 + agent 一次），不能重复告警。
        r = {'metrics': [self._metric('总面积', '22.85hm2'), self._metric('总面积', '5.40hm2')],
             'warnings': []}
        mark_conflicts(r)
        mark_conflicts(r)
        self.assertEqual(len([w for w in r['warnings'] if '总面积' in w]), 1)

    def test_run_agent_joins_conflict_detection_after_adding_metrics(self):
        # extract() 内部那次调用发生在 agent 追加指标之前，拦不到也没关系；
        # 这里要确认的是 agent_loop 自己**在追加之后**又跑了一次，且作用在返回的 result 上。
        with tempfile.TemporaryDirectory() as td:
            with patch.object(A, 'mark_conflicts') as spy:
                r = _run(td, ALT_FORM_BODY)
        spy.assert_called_once()
        self.assertIs(spy.call_args[0][0], r)


# 主干投资词表要求「总投资/投资概算/概算总投资/投资估算/估算总投资/投资匡算」，
# 而地方批复也写「投资总概算」——语序完全不同，主干 5 个变体一个都不命中。
INVEST_BODY = [
    '你单位《关于要求审批瑞安市马屿镇公路改造提升工程初步设计的申请报告》收悉。',
    '四、项目投资概算及资金来源该工程投资总概算2671万元，其中建筑安装工程费1800万元。',
]

# 「资金来源」出现在**章节标题**里，后面跟的是投资句而不是资金来源内容 ——
# 纯关键词判据会在这里误报，必须要求「（全部）为/由」。
FUNDING_TITLE_ONLY = [
    '你单位《关于要求审批某工程初步设计的申请报告》收悉。',
    '三、项目投资概算及资金来源该工程投资概算100万元。',
]

FUNDING_COPULA = [
    '你单位《关于要求审批某工程初步设计的申请报告》收悉。',
    '四、工程概算及资金筹措该项目投资概算11399万元，资金来源为中央生态环境资金和地方配套资金。',
]


class ReadSectionTests(unittest.TestCase):
    """定点回读：比主干词表多容许一两种语序，但必须有位置约束。"""

    def test_recovers_investment_with_uncovered_word_order(self):
        with tempfile.TemporaryDirectory() as td:
            base = extract(_pdf(td, INVEST_BODY), 'd.pdf', '0' * 32, False)
        self.assertIsNone(base['fields']['总投资/匡算/估算/概算']['value'])
        with tempfile.TemporaryDirectory() as td:
            r = _run(td, INVEST_BODY)
        c = r['fields']['总投资/匡算/估算/概算']
        self.assertEqual(c['value'], '工程投资总概算2671万元')
        self.assertEqual(c['method'], 'agent-read')
        self.assertEqual(c['status'], 'needs_review')
        self.assertTrue(c['evidence'], '补回的字段必须带原文证据')

    def test_recovers_funding_source_with_copula(self):
        with tempfile.TemporaryDirectory() as td:
            base = extract(_pdf(td, FUNDING_COPULA), 'd.pdf', '0' * 32, False)
        self.assertIsNone(base['fields']['资金来源']['value'])
        with tempfile.TemporaryDirectory() as td:
            r = _run(td, FUNDING_COPULA)
        self.assertEqual(r['fields']['资金来源']['value'], '资金来源为中央生态环境资金和地方配套资金')

    def test_ignores_funding_source_appearing_only_in_section_title(self):
        # 标题里的「…及资金来源」不是资金来源内容：既不该报 hint，也不该被回读填充。
        with tempfile.TemporaryDirectory() as td:
            r = _run(td, FUNDING_TITLE_ONLY)
        self.assertIsNone(r['fields']['资金来源']['value'])
        self.assertEqual(r['agent']['trace'], [])

    def test_planner_prefers_read_section_then_escalates(self):
        issues = [{'code': 'FIELD_EMPTY_HINT', 'severity': 'medium',
                   'field': '资金来源', 'detail': 'x'}]
        p = A.RulePlanner()
        self.assertEqual(p.decide(issues, {}, {}, set())['action'], 'read_section')
        self.assertEqual(p.decide(issues, {}, {}, {'read_section'})['action'], 'escalate')
        self.assertIsNone(p.decide(issues, {}, {}, {'read_section', 'escalate'}))


class BlankBeforeOcrTests(unittest.TestCase):
    """空白判定必须优先于 OCR 输出：近乎无墨迹的页面上，OCR 只会读出噪声。

    实测来源：公开语料 seq=27 第 5 页仅对角水印+页码，OCR 吐出一行乱码
    「咨左线亚台"一程亩非五」，墨迹占比 0.00296 本应判空白，却因 OCR 先返回
    而被判成 ocr-vision——既产生误报告警，乱码还混进 lines，并连带把空字段
    降级为 uncertain。
    """

    def _textless_pdf(self, td):
        # 整页无文本层（正文是图片），且图片几乎不占墨迹 -> 走整页 OCR 回退分支。
        import io
        from PIL import Image
        path = Path(td) / 'scan.pdf'
        doc = fitz.open()
        page = doc.new_page()
        buf = io.BytesIO()
        Image.new('RGB', (40, 40), (255, 255, 255)).save(buf, 'PNG')
        page.insert_image(fitz.Rect(100, 100, 140, 140), stream=buf.getvalue())
        doc.save(path)
        return path

    def test_near_blank_page_is_blank_and_discards_ocr_noise(self):
        import app.extract as E
        with tempfile.TemporaryDirectory() as td:
            p = self._textless_pdf(td)
            with patch.object(E, 'ocr_page_lines', return_value=(['咨左线亚台乱码输出'], 0.001)):
                pages, lines, _text, _off, warnings = E.parse_pdf(p)
        self.assertEqual(pages[0]['text_state'], 'blank')
        self.assertFalse(any('乱码' in l['text'] for l in lines), 'OCR 噪声不得进入 lines')
        self.assertFalse(any('需复核' in w for w in warnings))

    def test_page_with_ink_keeps_ocr_result(self):
        import app.extract as E
        # 文本须 ≥20 字，否则会被判 unreadable（可读性阈值），与墨迹判定无关。
        recognised = '识别出的正文内容若干字，项目总投资2998万元，建设工期24个月。'
        with tempfile.TemporaryDirectory() as td:
            p = self._textless_pdf(td)
            with patch.object(E, 'ocr_page_lines', return_value=([recognised], 0.03)):
                pages, lines, _text, _off, warnings = E.parse_pdf(p)
        self.assertEqual(pages[0]['text_method'], 'ocr-vision')
        self.assertEqual(pages[0]['text_state'], 'readable')
        self.assertTrue(any('识别出的正文' in l['text'] for l in lines))
        self.assertFalse(any('需复核' in w for w in warnings))

    def test_unrecognisable_page_with_ink_still_warns(self):
        import app.extract as E
        with tempfile.TemporaryDirectory() as td:
            p = self._textless_pdf(td)
            with patch.object(E, 'ocr_page_lines', return_value=([], 0.03)):
                pages, _lines, _text, _off, warnings = E.parse_pdf(p)
        self.assertEqual(pages[0]['text_state'], 'unreadable')
        self.assertTrue(any('需复核' in w for w in warnings))


if __name__ == '__main__':
    unittest.main()
