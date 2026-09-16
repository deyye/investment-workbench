"""Acceptance regressions using fictional PDFs, including evidence and export colours."""
import io
import tempfile
import unittest
from pathlib import Path

import fitz
from openpyxl import load_workbook

from app.extract import extract, FIELDS
from app.compare import rows_for, export_xlsx, COLORS


def document(value, name='建设周期'):
    fields = {k: {'value': None, 'status': 'missing', 'evidence': []} for k in FIELDS}
    fields[name] = {'value': value, 'status': 'extracted', 'evidence': []}
    return dict(filename=value, stage='初步设计', fields=fields, metrics=[],
                project_key='fictional', project_name='测试项目', warnings=[])


class RequirementsTests(unittest.TestCase):
    def test_distinct_equal_metrics_on_same_line_and_range_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fictional.pdf'
            with fitz.open() as pdf:
                page = pdf.new_page()
                for y, text in [(60, '关于测试项目初步设计的批复'),
                                (100, '一、建设内容：总建筑面积100平方米，配套广场面积100平方米。'),
                                (140, '检修通道长度不少于10米，附属围墙高度2-3米。'),
                                (180, '二、建设地点：项目位于测试园区。')]:
                    page.insert_text((40, y), text, fontname='china-s', fontsize=11)
                pdf.save(path)
            result = extract(path, path.name, 'f' * 32)
        metrics = {m['name']: m for m in result['metrics']}
        for name, value in [('总建筑面积', '100平方米'), ('配套广场面积', '100平方米'),
                            ('检修通道长度', '不少于10米'), ('附属围墙高度', '2-3米')]:
            with self.subTest(name=name):
                self.assertEqual(metrics[name]['value'], value)
                ev = metrics[name]['evidence']
                self.assertTrue(ev)
                self.assertIn(value, ''.join(e['quote'] for e in ev))
                for e in ev:
                    self.assertEqual(e['page'], 1)
                    x0, y0, x1, y1 = e['bbox']
                    self.assertTrue(0 <= x0 < x1 <= 595)
                    self.assertTrue(0 <= y0 < y1 <= 842)

    def test_bounds_and_investment_basis_stay_different_in_excel(self):
        for name, a, b in [
            ('建设周期', '不超过24个月', '不少于24.1个月'),
            ('建设周期', '不超过24个月', '24.1个月'),
            ('总投资/匡算/估算/概算', '估算总投资1亿元', '概算总投资10001万元'),
        ]:
            with self.subTest(a=a, b=b):
                docs = [document(a, name), document(b, name)]
                row = next(r for r in rows_for(docs)[1] if r['name'] == name)
                self.assertEqual(row['status'], 'different')
                wb = load_workbook(io.BytesIO(export_xlsx(docs)))
                cells = next(r for r in wb['1-固定字段'] if r[0].value == name)
                self.assertEqual(cells[1].fill.fgColor.rgb, '00' + COLORS['different'])

    def test_approximate_counts_do_not_hide_one_unit_change(self):
        docs = [document(None), document(None)]
        for d, value in zip(docs, ['约10座', '11座']):
            d['metrics'] = [dict(name='泵站数量', value=value, status='extracted', evidence=[])]
        row = next(r for r in rows_for(docs)[1] if r['name'] == '泵站数量')
        self.assertEqual(row['status'], 'different')
