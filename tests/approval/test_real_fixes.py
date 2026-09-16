"""真实批复实测驱动的修复：书写精度宽容、空值原因、跨阶段指标对齐。

这些改动都是在 6 份真实批复（庆元 / 龙泉，各三阶段）上跑出来的，本题用
最小夹具把每条结论钉住——真实材料不进仓库，但结论必须可回归。

覆盖：
1. 只差书写精度/约数的值不再报成「内容变化」，而真实变化不能被吞掉；
2. 空值区分「原文未载明」与「原文有线索未提取到」；
3. 数值相同、名称不同、且分处不同阶段的指标要被标出「疑似同一指标」，
   而同文档内数值相同的两个指标不能被误标；
4. 疑似同一指标**必须能被人工处理掉**——否则项目永远到不了「已审完」；
5. 导出保留「来源」与「缺失原因」，自检循环补回的值降级为待复核。
"""
import os
import tempfile
import unittest
from pathlib import Path

import fitz

from app.extract import FIELDS, mark_missing_reason
from app.compare import rows_for, export_xlsx
from app.review import update as apply_review
from app.queue import project_progress, align_pair_key
from app import agent_loop as A


def cell(value, status='extracted', method='rule', reason=None):
    c = {'value': value, 'status': status, 'evidence': [], 'method': method}
    if reason:
        c['reason'] = reason
    return c


def make_doc(doc_id, filename, stage, fields=None, metrics=None):
    fs = {k: cell(None, 'missing') for k in FIELDS}
    fs.update(fields or {})
    return {'id': doc_id, 'filename': filename, 'stage': stage, 'fields': fs,
            'metrics': metrics or [], 'warnings': [], 'history': [],
            'project_key': 'p1', 'project_name': '测试项目', 'revision': 0}


def metric(name, value, status='extracted'):
    return {'name': name, 'value': value, 'status': status, 'method': 'rule',
            'evidence': [], 'normalized': None}


def row_of(rows, name):
    return next(r for r in rows if r['kind'] == 'metric' and r['name'] == name)


class RoundingToleranceTests(unittest.TestCase):
    """书写精度差异是噪音，真实变化是信息——两者必须分得开。"""

    def _pair(self, a, b):
        docs = [make_doc('d1', 'a.pdf', '可行性研究', metrics=[metric('总用地面积', a)]),
                make_doc('d2', 'b.pdf', '初步设计', metrics=[metric('总用地面积', b)])]
        return rows_for(docs)[1]

    def test_unit_conversion_rounding_is_equivalent(self):
        # 13.693 公顷 = 136930 平方米，与 136929.6 平方米只差 0.4，属换算精度。
        rows = self._pair('13.693公顷', '136929.6平方米')
        r = row_of(rows, '总用地面积')
        self.assertEqual(r['status'], 'equivalent')
        self.assertIn('精度', r['note'])

    def test_decimal_rounding_is_equivalent(self):
        rows = self._pair('6736平方米', '6736.11平方米')
        self.assertEqual(row_of(rows, '总用地面积')['status'], 'equivalent')

    def test_approximate_value_rounding_is_equivalent(self):
        rows = self._pair('约2.31公里', '2.308千米')
        r = row_of(rows, '总用地面积')
        self.assertEqual(r['status'], 'equivalent')

    def test_real_change_is_never_swallowed(self):
        # 可研 109.789 万立方米 → 初设 993194 立方米，差 9.5%，必须报变化。
        rows = self._pair('109.789万立方米', '993194立方米')
        self.assertEqual(row_of(rows, '总用地面积')['status'], 'different')

    def test_coarse_stage_estimate_still_reported(self):
        # 建议书「约200000平方米」与可研「13.693公顷」差 31.5%，是真变化。
        rows = self._pair('约200000平方米', '13.693公顷')
        self.assertEqual(row_of(rows, '总用地面积')['status'], 'different')

    def test_interval_values_are_not_tolerated(self):
        # 区间值不适用精度宽容：起止都变了就必须报差异。
        rows = self._pair('24.5-36米', '25-37米')
        self.assertEqual(row_of(rows, '总用地面积')['status'], 'different')


class MissingReasonTests(unittest.TestCase):
    """「原文确实没有」与「有线索却没抽到」是两件事，不能同色显示。"""

    def test_absent_when_no_cue(self):
        r = {'fields': {'建设周期': cell(None, 'missing')}}
        mark_missing_reason(r, '一、建设内容：新建道路1500米。')
        self.assertEqual(r['fields']['建设周期']['reason'], 'absent')

    def test_unextracted_when_cue_present(self):
        r = {'fields': {'建设周期': cell(None, 'missing')}}
        mark_missing_reason(r, '六、项目工期：项目建设工期为14个月。')
        self.assertEqual(r['fields']['建设周期']['reason'], 'unextracted')

    def test_imprint_never_reports_unextracted(self):
        # 印发机关由版记结构定位，没有候选即确属版记未署机关（实测庆元三份），
        # 不应因为正文出现疑似线索就报「有线索未抽到」。
        r = {'fields': {'印发机关': cell(None, 'missing')}}
        mark_missing_reason(r, '庆元县发展和改革局文件　2025年9月30日印发')
        self.assertEqual(r['fields']['印发机关']['reason'], 'absent')

    def test_filled_fields_get_no_reason(self):
        r = {'fields': {'建设周期': cell('14个月')}}
        mark_missing_reason(r, '建设工期为14个月')
        self.assertNotIn('reason', r['fields']['建设周期'])

    def test_compare_surfaces_unextracted(self):
        docs = [make_doc('d1', 'a.pdf', '初步设计',
                         fields={'建设周期': cell(None, 'missing', reason='unextracted')}),
                make_doc('d2', 'b.pdf', '可行性研究', fields={'建设周期': cell('14个月')})]
        r = next(x for x in rows_for(docs)[1] if x['name'] == '建设周期')
        self.assertEqual(r['status'], 'unextracted')


class AlignmentTests(unittest.TestCase):
    """跨阶段指标名不同导致错位——这是真实材料上最误导人的问题。"""

    def _docs(self):
        return [make_doc('d1', '可研.pdf', '可行性研究',
                         metrics=[metric('道路路面面积', '60637平方米')]),
                make_doc('d2', '初设.pdf', '初步设计',
                         metrics=[metric('行车道、路缘带及硬路肩总面积', '60637平方米')])]

    def test_align_flagged_across_disjoint_stages(self):
        docs, rows = rows_for(self._docs())
        prog = project_progress(docs, rows)
        self.assertEqual(prog['alignment_pending'], 1)
        item = prog['alignments'][0]
        self.assertEqual(sorted(item['names']),
                         sorted(['道路路面面积', '行车道、路缘带及硬路肩总面积']))
        self.assertEqual(item['value'], '60637平方米')
        # 两个名称各落在哪份文件必须给出来，界面才能发起处理。
        self.assertEqual({h['name'] for h in item['holders']},
                         {'道路路面面积', '行车道、路缘带及硬路肩总面积'})
        self.assertEqual(row_of(rows, '道路路面面积')['status'], 'align')
        self.assertEqual(row_of(rows, '行车道、路缘带及硬路肩总面积')['status'], 'align')

    def test_same_document_equal_values_not_flagged(self):
        # 「涵洞7处」与「平交口7处」在同一份文件里，不是跨阶段错位，不得标 align。
        docs = [make_doc('d1', '可研.pdf', '可行性研究',
                         metrics=[metric('涵洞数量', '7处'), metric('平交口数量', '7处')])]
        docs, rows = rows_for(docs)
        self.assertEqual(project_progress(docs, rows)['alignment_pending'], 0)
        for name in ['涵洞数量', '平交口数量']:
            self.assertNotEqual(row_of(rows, name)['status'], 'align')
            self.assertNotIn('align_with', row_of(rows, name))

    def test_merge_renames_and_leaves_one_row(self):
        docs = self._docs()
        docs, rows = rows_for(docs)
        target = next(d for d in docs if d['id'] == 'd2')
        apply_review(target, {'kind': 'align', 'name': '行车道、路缘带及硬路肩总面积',
                              'other': '道路路面面积', 'decision': 'merge', 'value': ''})
        docs, rows = rows_for(docs)
        names = [r['name'] for r in rows if r['kind'] == 'metric']
        self.assertEqual(names, ['道路路面面积'])
        r = row_of(rows, '道路路面面积')
        self.assertEqual([c['value'] for c in r['cells']], ['60637平方米', '60637平方米'])
        self.assertEqual(project_progress(docs, rows)['alignment_pending'], 0)

    def test_keep_records_decision_without_changing_data(self):
        docs = self._docs()
        docs, rows = rows_for(docs)
        target = next(d for d in docs if d['id'] == 'd1')
        apply_review(target, {'kind': 'align', 'name': '道路路面面积',
                              'other': '行车道、路缘带及硬路肩总面积',
                              'decision': 'keep', 'value': ''})
        self.assertEqual(target['metrics'][0]['name'], '道路路面面积')
        docs, rows = rows_for(docs)
        self.assertEqual(project_progress(docs, rows)['alignment_pending'], 0)
        self.assertEqual(target['history'][-1]['name'],
                         align_pair_key('道路路面面积', '行车道、路缘带及硬路肩总面积'))

    def test_invalid_decision_rejected(self):
        docs = self._docs()
        target = docs[0]
        for bad in [{'decision': 'x'}, {'decision': 'merge', 'other': ''},
                    {'decision': 'merge', 'other': '道路路面面积'}]:
            data = {'kind': 'align', 'name': '道路路面面积', 'value': ''}
            data.update(bad)
            with self.assertRaises(ValueError):
                apply_review(target, data)

    def test_project_reaches_done_after_alignment_resolved(self):
        """回归：对齐项必须能被处理掉，否则项目永远到不了「已审完」。

        这一条是这套改动里最容易漏的：比对表标了黄、待办数计了一条，
        却没有任何入口能把它消掉，进度就会卡在「还剩 1 项」不动。
        """
        docs = self._docs()
        docs, rows = rows_for(docs)
        self.assertEqual(project_progress(docs, rows)['status'], '未开始')
        target = next(d for d in docs if d['id'] == 'd2')
        apply_review(target, {'kind': 'align', 'name': '行车道、路缘带及硬路肩总面积',
                              'other': '道路路面面积', 'decision': 'merge', 'value': ''})
        docs, rows = rows_for(docs)
        prog = project_progress(docs, rows)
        self.assertEqual(prog['pending'], 0)
        self.assertEqual(prog['status'], '待核对项已处理')
        self.assertEqual(prog['label'], '待核对项已处理')


class ExportTests(unittest.TestCase):
    """导出必须能分辨每个值的出处，否则「每个值都要有出处」落不了地。"""

    def test_single_sheet_keeps_source_and_missing_reason(self):
        docs = [make_doc('d1', 'a.pdf', '初步设计', fields={
                    '项目代码': cell('2509-331126-04-01-309362'),
                    '印发机关': cell(None, 'missing', reason='absent'),
                    '建设周期': cell(None, 'missing', reason='unextracted'),
                }, metrics=[metric('道路长度', '2.308千米')])]
        import io
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(export_xlsx(docs)))
        sheet = wb['单文档结构化']
        head = [c.value for c in sheet[1]]
        for col in ['来源', '状态', '缺失原因']:
            self.assertIn(col, head)
        i_reason = head.index('缺失原因')
        reasons = [r[i_reason].value for r in sheet.iter_rows(min_row=2)]
        joined = ' '.join(str(v) for v in reasons)
        self.assertIn('原文未载明', joined)
        self.assertIn('未提取到', joined)


class AgentIntegrationTests(unittest.TestCase):
    """自检循环接进产品后，补救值的等级必须低于主干直取。"""

    def _pdf(self, td, lines):
        path = Path(td) / 'x.pdf'
        doc = fitz.open()
        page = doc.new_page()
        y = 60
        page.insert_text((80, y), '演发改投〔2026〕88号', fontname='china-s', fontsize=10)
        y += 26
        for text in lines:
            page.insert_text((80, y), text, fontname='china-s', fontsize=10)
            y += 26
        doc.save(path)
        return path

    def test_agent_recovered_metrics_are_needs_review(self):
        lines = ['你单位《关于要求审批某工程初步设计的申请报告》收悉。',
                 '一、建设内容：本工程完成滨海植被修复22.85hm2。']
        with tempfile.TemporaryDirectory() as td:
            r = A.run_agent(self._pdf(td, lines), 'x.pdf', '0' * 32)
        added = [m for m in r['metrics'] if (m.get('method') or '').startswith('agent-')]
        self.assertTrue(added, '应补回指标')
        for m in added:
            self.assertEqual(m['status'], 'needs_review',
                             '放宽形态补回的指标必须降级，不能与主干直取同等级')

    def test_should_run_is_false_on_healthy_document(self):
        lines = ['你单位《关于要求审批某工程初步设计的申请报告》收悉。',
                 '一、建设内容：新建污水管网1501m，截污纳管管道1250米。',
                 '三、项目估算总投资为4800万元。',
                 '四、项目建设工期为14个月。']
        with tempfile.TemporaryDirectory() as td:
            from app.extract import extract
            r = extract(self._pdf(td, lines), 'x.pdf', '0' * 32, False)
        self.assertFalse(A.should_run(r), '主干正常时不应启动自检循环')


if __name__ == '__main__':
    unittest.main()
