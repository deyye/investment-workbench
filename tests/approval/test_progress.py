"""Progress, recovery and bounded duplicate work without real user documents."""
import tempfile
import unittest
from unittest.mock import patch
from app.server import Store
from app.progress import report
# 两种运行方式都要能用：`unittest discover -s tests` 时 tests/ 在 sys.path 上，
# 而 `-m unittest tests.test_progress` 时需要走包路径，否则单独跑这个文件会
# ModuleNotFoundError，看起来像测试坏了。
try:
    from tests.approval.test_v2 import document
except ImportError:
    from tests.approval.test_v2 import document

class ProgressTests(unittest.TestCase):
    def test_files_report_actual_steps_and_duplicate_once(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);jid=store.new_job(['a.pdf','b.pdf']);steps=[]
            def extract(path,name,docid,llm):
                report('识别扫描文字','第 1 / 2 页')
                steps.append(store.snapshot_jobs(jid)['files'][0]['step'])
                d=document();d['id']=docid;d['filename']=name;return d
            try:
                with patch('app.server.extract',side_effect=extract) as mocked,patch('app.server.diagnose',side_effect=lambda result,*args:result):
                    store.run(jid,[('a.pdf',b'%PDF-test'),('b.pdf',b'%PDF-test')],False)
                self.assertEqual(mocked.call_count,1)
                self.assertEqual(steps,['识别扫描文字'])
                job=store.snapshot_jobs(jid)
                self.assertEqual([f['status'] for f in job['files']],['success','duplicate'])
                self.assertEqual(job['done'],2)
                restored=Store(folder)
                try:
                    self.assertEqual(restored.snapshot_jobs(jid)['status'],'completed')
                    self.assertEqual(len(restored.list()),1,'Job journal must not appear as a document')
                finally:restored.executor.shutdown()
            finally:store.executor.shutdown()
    def test_restart_marks_pending_files_interrupted(self):
        with tempfile.TemporaryDirectory() as folder:
            original=Store(folder);jid=original.new_job(['pending.pdf'])
            original.executor.shutdown()
            restored=Store(folder)
            try:
                job=restored.snapshot_jobs(jid)
                self.assertEqual(job['status'],'interrupted')
                self.assertEqual(job['files'][0]['status'],'interrupted')
                self.assertIn('重新上传',job['files'][0]['step'])
            finally:restored.executor.shutdown()
    def test_failure_persists_with_actionable_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);jid=store.new_job(['broken.pdf'])
            try:
                with patch('app.server.extract',side_effect=ValueError('PDF已加密，请先解除密码保护。')):
                    store.run(jid,[('broken.pdf',b'%PDF-broken')],False)
                job=store.snapshot_jobs(jid)
                self.assertEqual(job['files'][0]['status'],'failed')
                self.assertIn('解除密码',job['files'][0]['detail'])
                self.assertEqual(store.list(),[])
            finally:store.executor.shutdown()
    def test_snapshots_do_not_mutate_live_job(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder)
            try:
                jid=store.new_job(['test.pdf']);copy=store.snapshot_jobs(jid)
                copy['files'][0]['status']='success'
                self.assertEqual(store.jobs[jid]['files'][0]['status'],'queued')
            finally:store.executor.shutdown()
    def test_progress_observer_does_not_leak(self):
        from app.progress import observe
        calls=[]
        with observe(lambda *args:calls.append(args)):report('读取页面','1/1')
        report('not observed')
        self.assertEqual(calls,[('读取页面','1/1')])
