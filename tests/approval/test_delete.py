"""删除、回收站与一键清空。

删除不做硬删：人工核对成果（修订值、证据绑定、修订历史）只存在于 <id>.json，
删掉就没有第二处可查。所以删除一律先移入 data/.trash/，可恢复，彻底删除要独立口令。
本文件重点守住两件事：删除范围不外溢（配置与任务文件绝不波及），以及任何一步都可回退。
"""
import json,tempfile,threading,unittest,urllib.error,urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from app.server import Handler,Store
# 两种运行方式都要能用，理由同 test_usability.py：discover -s tests 与 -m unittest tests.x
try:
    from tests.approval.test_v2 import document
except ImportError:
    from tests.approval.test_v2 import document

KEY='2508-331126-04-01-309363'

def seed(store,ids=('a','b'),key=KEY,history=2):
    """直接在存储目录里放几份文档，并配上同名 PDF 与干扰项。"""
    for i in ids:
        d=document(i)
        d.update(project_key=key,project_name='演示项目',history=[{'kind':'fixed'}]*(history or 0))
        store.save(d)
        (store.path/(d['id']+'.pdf')).write_bytes(b'%PDF-1.4 fixture')
    (store.path/'model_config.json').write_text('{"LLM_API_KEY":"placeholder-not-a-real-key"}',encoding='utf-8')
    (store.path/'jobs').mkdir(exist_ok=True)
    (store.path/'jobs'/'state.json').write_text('{}',encoding='utf-8')

def idof(ch):return ch*32

class StoreTests(unittest.TestCase):
    def test_list_ignores_config_and_jobs(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);seed(s)
            self.assertEqual(len(s.list()),2)

    def test_trash_moves_both_files_and_leaves_config_alone(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);seed(s)
            self.assertEqual(s.trash([idof('a')]),[idof('a')])
            self.assertEqual(len(s.list()),1)
            self.assertFalse((Path(td)/(idof('a')+'.json')).exists())
            self.assertFalse((Path(td)/(idof('a')+'.pdf')).exists())
            self.assertEqual(len(list((Path(td)/'.trash').glob('*'))),1)
            # 配置与任务状态不属于文档，任何删除动作都不该碰它们。
            self.assertTrue((Path(td)/'model_config.json').exists())
            self.assertTrue((Path(td)/'jobs'/'state.json').exists())

    def test_trash_rejects_malformed_id(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);seed(s)
            with self.assertRaises(ValueError):s.trash(['../../etc/passwd'])
            with self.assertRaises(ValueError):s.trash(['A'*32])
            self.assertEqual(len(s.list()),2)

    def test_trash_skips_unknown_id(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);seed(s)
            self.assertEqual(s.trash([idof('f')]),[])

    def test_list_trash_keeps_enough_context_to_decide(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);seed(s,history=3)
            s.trash([idof('a')])
            item=s.list_trash()[0]
            self.assertEqual(item['id'],idof('a'))
            self.assertEqual(item['stage'],'初步设计')
            self.assertEqual(item['project_key'],KEY)
            self.assertEqual(item['project_name'],'演示项目')
            self.assertEqual(item['revisions'],3)
            self.assertTrue(item['intact'])
            self.assertGreater(item['deleted_at'],0)

    def test_intact_is_false_when_the_pdf_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);seed(s)
            (Path(td)/(idof('a')+'.pdf')).unlink()
            s.trash([idof('a')])
            self.assertFalse(s.list_trash()[0]['intact'])

    def test_restore_puts_the_document_back(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);seed(s)
            s.trash([idof('a')])
            self.assertEqual(s.restore([idof('a')]),[idof('a')])
            self.assertEqual(len(s.list()),2)
            self.assertEqual(s.list_trash(),[])
            self.assertEqual(s.get(idof('a'))['id'],idof('a'))

    def test_restore_all(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);seed(s)
            s.trash([idof('a'),idof('b')])
            self.assertEqual(len(s.restore()),2)
            self.assertEqual(len(s.list()),2)

    def test_restore_never_overwrites_a_live_document(self):
        """原编号被占用时给新编号，且新编号必须仍是 Store 认得的 32 位十六进制。

        写成 <旧编号>.restored-xxxx.json 之类的名字会让 get() 校验不过，
        恢复出来的文件会变成谁也读不到——比直接失败更糟。
        """
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);seed(s)
            s.trash([idof('a')])
            (Path(td)/(idof('a')+'.json')).write_text(json.dumps(document('a')),encoding='utf-8')
            (Path(td)/(idof('a')+'.pdf')).write_bytes(b'%PDF-1.4 other')
            moved=s.restore([idof('a')])
            self.assertEqual(len(moved),1)
            self.assertNotEqual(moved[0],idof('a'))
            self.assertEqual(len(moved[0]),32)
            self.assertEqual(s.get(moved[0])['id'],moved[0])
            self.assertEqual(s.get(idof('a'))['id'],idof('a'))
            self.assertEqual(len(s.list()),3)

    def test_purge_only_touches_the_trash(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);seed(s)
            s.trash([idof('a')])
            self.assertEqual(s.purge(),1)
            self.assertEqual(s.list_trash(),[])
            self.assertEqual(len(s.list()),1)
            self.assertTrue((Path(td)/'model_config.json').exists())

    def test_purge_can_target_one_item(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);seed(s)
            s.trash([idof('a'),idof('b')])
            self.assertEqual(s.purge([idof('a')]),1)
            self.assertEqual([x['id'] for x in s.list_trash()],[idof('b')])

    def test_busy_reports_a_running_job(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);seed(s)
            self.assertFalse(s.busy())
            s.jobs['j']={'status':'running'}
            self.assertTrue(s.busy())

class DeleteAPITests(unittest.TestCase):
    def setUp(self):
        self.td=tempfile.TemporaryDirectory()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.store=Store(self.td.name)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close()
        self.server.store.executor.shutdown(wait=True);self.td.cleanup()

    def call(self,path,data=None):
        req=urllib.request.Request(self.url+path,data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type':'application/json','X-Requested-With':'ApprovalAgent'})
        with urllib.request.urlopen(req,timeout=20) as r:return json.load(r)

    def status(self,path,data):
        try:self.call(path,data)
        except urllib.error.HTTPError as e:return e.code
        return 200

    def docids(self):
        return [d['id'] for d in self.server.store.list()]

    def test_delete_one_document(self):
        seed(self.server.store)
        out=self.call('/api/documents/delete',{'ids':[idof('a')]})
        self.assertEqual(out['moved'],1)
        self.assertEqual(self.docids(),[idof('b')])
        self.assertEqual(len(self.call('/api/trash')['items']),1)
        with self.assertRaises(urllib.error.HTTPError):self.call('/api/documents/'+idof('a'))

    def test_delete_a_whole_project(self):
        """只有一个项目时，删项目不该被当成「清空全部」而要口令——按意图判定，不按覆盖面。"""
        seed(self.server.store,ids=('a','b','c'))
        docs=[d for d in self.server.store.list() if d['project_key']==KEY]
        self.assertEqual(len(docs),3)
        out=self.call('/api/documents/delete',{'ids':[d['id'] for d in docs],'scope':'project'})
        self.assertEqual(out['moved'],3)
        self.assertEqual(self.call('/api/documents')['groups'],[])

    def test_clear_all_needs_the_token_and_then_works(self):
        seed(self.server.store)
        self.assertEqual(self.status('/api/documents/delete',{'ids':self.docids(),'scope':'all'}),400)
        self.assertEqual(len(self.docids()),2)
        out=self.call('/api/documents/delete',{'ids':self.docids(),'scope':'all','confirm':'DELETE-2'})
        self.assertEqual(out['moved'],2)
        self.assertEqual(self.call('/api/documents')['groups'],[])

    def test_listing_every_document_is_a_plain_delete(self):
        """口令按动作意图判定，不看 id 覆盖了多大范围。

        逐个点名全部文件是一个明确动作（用户看得见自己删了什么），不额外要口令；
        真正的兜底是回收站——删错了能恢复。这样「只有一个项目时删该项目」才不会被
        误判成清空而卡住。
        """
        seed(self.server.store)
        out=self.call('/api/documents/delete',{'ids':self.docids(),'scope':'documents'})
        self.assertEqual(out['moved'],2)
        self.assertEqual(self.call('/api/documents')['groups'],[])
        self.assertEqual(len(self.call('/api/trash')['items']),2)

    def test_stale_token_is_refused(self):
        """口令里的份数对不上，说明删除范围与用户所见不一致，宁可不做。"""
        seed(self.server.store)
        self.assertEqual(self.status('/api/documents/delete',
            {'ids':self.docids(),'scope':'all','confirm':'DELETE-9'}),400)
        self.assertEqual(len(self.docids()),2)

    def test_clear_all_with_a_narrower_range_is_refused(self):
        seed(self.server.store)
        self.assertEqual(self.status('/api/documents/delete',
            {'ids':[idof('a')],'scope':'all','confirm':'DELETE-2'}),400)
        self.assertEqual(len(self.docids()),2)

    def test_clear_all_on_an_empty_store_is_refused(self):
        self.assertEqual(self.status('/api/documents/delete',
            {'ids':[idof('a')],'scope':'all','confirm':'DELETE-0'}),400)

    def test_delete_rejects_bad_input(self):
        seed(self.server.store)
        self.assertEqual(self.status('/api/documents/delete',{}),400)
        self.assertEqual(self.status('/api/documents/delete',{'ids':[]}),400)
        self.assertEqual(self.status('/api/documents/delete',{'ids':['nope']}),400)
        self.assertEqual(self.status('/api/documents/delete',{'ids':'a'*32}),400)
        self.assertEqual(len(self.docids()),2)

    def test_delete_refused_while_a_job_runs(self):
        seed(self.server.store)
        self.server.store.jobs['j']={'status':'running'}
        self.assertEqual(self.status('/api/documents/delete',{'ids':[idof('a')]}),400)
        self.assertEqual(len(self.docids()),2)

    def test_trash_restore_and_purge_roundtrip(self):
        seed(self.server.store)
        self.call('/api/documents/delete',{'ids':[idof('a')]})
        self.assertEqual(len(self.call('/api/trash')['items']),1)
        self.assertEqual(self.call('/api/trash/restore',{'ids':[idof('a')]})['restored'],1)
        self.assertEqual(len(self.docids()),2)
        self.assertEqual(self.call('/api/trash')['items'],[])
        self.call('/api/documents/delete',{'ids':[idof('a')]})
        self.assertEqual(self.status('/api/trash/purge',{}),400)
        self.assertEqual(self.status('/api/trash/purge',{'confirm':'purge'}),400)
        self.assertEqual(self.call('/api/trash/purge',{'confirm':'PURGE'})['purged'],1)
        self.assertEqual(self.call('/api/trash')['items'],[])

    def test_model_config_survives_every_delete_action(self):
        seed(self.server.store)
        self.call('/api/documents/delete',{'ids':[idof('a')]})
        self.call('/api/documents/delete',{'ids':[idof('b')],'scope':'all','confirm':'DELETE-1'})
        self.call('/api/trash/purge',{'confirm':'PURGE'})
        config=Path(self.td.name)/'model_config.json'
        self.assertTrue(config.exists(),'清空文档不得删除模型配置')
        self.assertIn('LLM_API_KEY',config.read_text(encoding='utf-8'))
        self.assertTrue((Path(self.td.name)/'jobs'/'state.json').exists())

if __name__=='__main__':unittest.main()
