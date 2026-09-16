import copy,io,json,tempfile,threading,unittest,urllib.request,urllib.error
from http.server import ThreadingHTTPServer
from openpyxl import load_workbook
from app.server import Handler,Store
from app.queue import project_progress
from app.compare import rows_for
from app.review import update
# 两种运行方式都要能用：`discover -s tests` 时 tests/ 在 sys.path 上，
# 而 `-m unittest tests.test_usability` 需要走包路径，否则单独跑这个文件
# 会 ModuleNotFoundError，看起来像测试坏了。
try:
    from tests.approval.test_v2 import document,cell
except ImportError:
    from tests.approval.test_v2 import document,cell

class UsabilityTests(unittest.TestCase):
    def test_confirmation_does_not_clear_other_conflicts(self):
        d=document();d['metrics']=[dict(cell(v),name='面积',status='conflict') for v in ['100平方米','200平方米']]
        update(d,dict(kind='metric',name='面积',index=0,value='100平方米'))
        self.assertEqual(project_progress([d],rows_for([d])[1])['pending'],2)
        update(d,dict(kind='metric',name='面积',index=0,value='100平方米',metric_name='一期面积'))
        self.assertEqual(project_progress([d],rows_for([d])[1])['pending'],1)
        update(d,dict(kind='metric',name='面积',index=1,value='200平方米',metric_name='二期面积'))
        self.assertEqual(project_progress([d],rows_for([d])[1])['pending'],0)

    def test_stage_pending_independent_of_coverage(self):
        d=document();d['stage']='待确认'
        p=project_progress([d],rows_for([d])[1]);self.assertEqual(p['pending'],1)
        self.assertEqual(p['documents'][0]['items'][0]['kind'],'stage')
        update(d,dict(kind='stage',name='审批阶段',value='建议书/立项'))
        p=project_progress([d],rows_for([d])[1]);self.assertEqual(p['pending'],0)
        self.assertEqual([x['count'] for x in p['stages']],[1,0,0])
        self.assertEqual(p['status'],'待核对项已处理')

    def test_historical_edit_does_not_suppress_current_uncertainty(self):
        d=document();d['history']=[dict(kind='fixed',name='建设周期')]
        d['fields']['建设周期']=dict(cell('24个月'),status='needs_review')
        self.assertEqual(project_progress([d])['pending'],1)

    def test_project_export_scope_and_bad_key(self):
        with tempfile.TemporaryDirectory() as folder:
            s=ThreadingHTTPServer(('127.0.0.1',0),Handler);s.store=Store(folder)
            threading.Thread(target=s.serve_forever,daemon=True).start()
            try:
                for i in ['a','b']:
                    d=document(i);d['project_key']=i;s.store.save(d)
                def read(path):
                    with urllib.request.urlopen('http://127.0.0.1:'+str(s.server_port)+path) as r:return r.read()
                self.assertEqual(len(json.loads(read('/api/export.json?project=a'))),1)
                self.assertEqual(len(json.loads(read('/api/export.json'))),2)
                wb=load_workbook(io.BytesIO(read('/api/export.xlsx?project=a')))
                self.assertIn('1-固定字段',wb.sheetnames);self.assertNotIn('2-固定字段',wb.sheetnames)
                with self.assertRaises(urllib.error.HTTPError):read('/api/export.xlsx?project=unknown')
            finally:s.shutdown();s.server_close();s.store.executor.shutdown(wait=True)

    def test_reprocess_preserves_renames_and_conflicting_siblings(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            s=Store(folder);d=document('a');d['metrics']=[dict(cell(v),name='面积',status='conflict') for v in ['100平方米','200平方米']]
            fresh=copy.deepcopy(d)
            update(d,dict(kind='metric',name='面积',index=0,value='100平方米',metric_name='一期面积'))
            s.save(d);s.jobs['j']={'results':[],'errors':[]}
            try:
                with patch('app.server.extract',return_value=fresh),patch('app.server.diagnose',side_effect=lambda x,*args:x):s.reprocess('j',d['id'],False)
                self.assertFalse(s.jobs['j']['errors']);got=s.get(d['id'])
                self.assertEqual([m['name'] for m in got['metrics']],['一期面积','面积'])
                self.assertEqual(project_progress([got])['pending'],1)
            finally:s.executor.shutdown(wait=True)
