import copy,io,json,tempfile,unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import fitz
from app.extract import extract,FIELDS,parse_pdf,numeric
from app.quantities import quantity_key
from app.compare import rows_for,compare_cells
from app.review import update

def cell(v):return {'value':v,'status':'extracted','evidence':[]}
def document(i='0',stage='初步设计',date='2025年1月1日'):
    return {'id':i*32,'filename':i+'.pdf','project_key':'x','project_name':'测试','stage':stage,'fields':{k:cell(date if k=='印发日期' else None) for k in FIELDS},'metrics':[],'lines':[{'id':'p1-l1','text':'项目建设工期为24个月。','page':1,'bbox':[10,10,200,30]}],'pages':[{'number':1,'width':595,'height':842}],'warnings':[],'revision':0}
class V2Tests(unittest.TestCase):
    def test_decimal_equivalence(self):self.assertEqual(quantity_key(numeric('1公里')),quantity_key(numeric('1000.0米')))
    def test_qualifiers_not_erased(self):self.assertNotEqual(quantity_key(numeric('约1公里')),quantity_key(numeric('1000米')))
    def test_suffix_limit(self):self.assertEqual(numeric('100平方米以上')['qualifier'],'以上')
    def test_mixed_unit_range(self):self.assertEqual(numeric('1公里-1500米')['number'],'1000')
    def test_incompatible_range(self):self.assertIsNone(numeric('1平方米-2米'))
    def test_reversed_range(self):self.assertIsNone(numeric('36米-24米'))
    def test_ten_thousand_units(self):self.assertEqual(Decimal(numeric('1万千瓦')['number']),10000)
    def test_area_and_duration(self):
        self.assertEqual(Decimal(numeric('1平方千米')['number']),1000000)
        self.assertEqual(Decimal(numeric('2年')['number']),24)
    def test_commas(self):self.assertEqual(Decimal(numeric('1,234.5万元')['number']),Decimal('1234.5'))
    def test_real_date_order(self):
        ds=[document('a',date='2025年12月1日'),document('b',date='2025年9月1日')]
        self.assertEqual(rows_for(ds)[0][0]['id'],'b'*32)
    def test_negative_index_rejected(self):
        d=document();d['metrics']=[dict(cell('1米'),name='长度')]
        with self.assertRaises(ValueError):update(d,{'kind':'metric','name':'长度','index':-1,'value':'2米'})
    def test_stale_review_rejected(self):
        with self.assertRaises(ValueError):update(document(),{'kind':'fixed','name':'建设周期','value':'24个月','revision':3})
    def test_binding_and_history(self):
        d=update(document(),{'kind':'fixed','name':'建设周期','value':'24个月','evidence_ids':['p1-l1'],'reason':'原文确认'})
        self.assertEqual(d['fields']['建设周期']['evidence'][0]['page'],1);self.assertEqual(d['revision'],1);self.assertEqual(d['history'][0]['reason'],'原文确认')
    def test_invalid_binding_rejected(self):
        with self.assertRaises(ValueError):update(document(),{'kind':'fixed','name':'建设周期','value':'24个月','evidence_ids':['p9-l9']})
    def test_invalid_code_rejected(self):
        with self.assertRaises(ValueError):update(document(),{'kind':'fixed','name':'项目代码','value':'123'})
    def test_manual_group_and_stage(self):
        d=update(document(),{'kind':'fixed','name':'项目代码','value':'2609-330100-04-01-100001'})
        d=update(d,{'kind':'stage','name':'审批阶段','value':'可行性研究'})
        self.assertEqual(d['project_key'],'2609-330100-04-01-100001');self.assertEqual(d['stage'],'可行性研究')
    def test_rotated_page_coordinates(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'rotated.pdf';doc=fitz.open();page=doc.new_page();page.insert_text((50,70),'Text sufficiently long to avoid invoking OCR on rotated pages.');page.set_rotation(90);doc.save(p)
            pages,lines,*_=parse_pdf(p)
            self.assertEqual(pages[0]['width'],595);self.assertTrue(all(0<=l['bbox'][0]<l['bbox'][2]<=595 for l in lines))
    def test_same_line_section_text(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'sections.pdf';doc=fitz.open();page=doc.new_page()
            page.insert_text((40,60),'关于演示项目可行性研究报告的批复',fontname='china-s',fontsize=12)
            page.insert_text((40,100),'一、建设内容：总建筑面积100平方米。',fontname='china-s',fontsize=12)
            page.insert_text((40,140),'二、建设地点：项目位于测试园区。',fontname='china-s',fontsize=12);doc.save(p)
            d=extract(p,p.name,'0'*32);self.assertIn('100平方米',d['fields']['建设内容']['value'])
    def test_local_fallback_on_model_error(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'example.pdf';doc=fitz.open();doc.new_page().insert_text((40,60),'A sufficiently long fixture document for failure fallback.');doc.save(p)
            with patch('app.extract.chat',side_effect=TimeoutError):d=extract(p,p.name,'0'*32,True)
            self.assertEqual(d['engine'],'local');self.assertTrue(any('大模型抽取失败' in w for w in d['warnings']))

class MoreAcceptanceTests(unittest.TestCase):
    def test_fixed_duration_normalisation(self):
        a,b=document('a'),document('b');a['fields']['建设周期']=cell('2年');b['fields']['建设周期']=cell('24个月')
        row=next(r for r in rows_for([a,b])[1] if r['name']=='建设周期');self.assertEqual(row['status'],'equivalent')
    def test_estimate_basis_retained(self):
        a,b=document('a'),document('b');k='总投资/匡算/估算/概算';a['fields'][k]=cell('估算总投资1亿元');b['fields'][k]=cell('概算总投资10000万元')
        row=next(r for r in rows_for([a,b])[1] if r['name']==k);self.assertEqual(row['status'],'different');self.assertIn('口径',row['note'])
    def test_same_basis_currency_equivalence(self):
        a,b=document('a'),document('b');k='总投资/匡算/估算/概算';a['fields'][k]=cell('估算总投资1亿元');b['fields'][k]=cell('估算总投资10000万元')
        row=next(r for r in rows_for([a,b])[1] if r['name']==k);self.assertEqual(row['status'],'equivalent')
    def test_preserve_concurrent_human_review_on_reprocess(self):
        from app.server import Store
        with tempfile.TemporaryDirectory() as td:
            store=Store(td);d=document('a');store.save(d);jobid='job';store.jobs[jobid]={'results':[],'errors':[],'status':'running'}
            def during_extract(*args):
                latest=store.get(d['id']);latest=update(latest,{'kind':'fixed','name':'建设周期','value':'25个月'});store.save(latest)
                return copy.deepcopy(d)
            try:
                with patch('app.server.extract',side_effect=during_extract):store.reprocess(jobid,d['id'],False)
                self.assertEqual(store.get(d['id'])['fields']['建设周期']['value'],'25个月');self.assertFalse(store.jobs[jobid]['errors'])
            finally:store.executor.shutdown(wait=True)
