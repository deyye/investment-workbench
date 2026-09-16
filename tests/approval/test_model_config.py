"""界面可改模型配置：即时生效、密钥不回显、非法值不落盘。"""
import json,os,tempfile,threading,unittest,urllib.error,urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from app.server import Handler,Store
from app.model_client import ModelError,clear_config,public_config,save_config,set_config_path,settings

# 以一份「环境变量里已经配好」的基线开场：这样才能证明界面保存的值确实盖过了它，
# 而不是因为环境变量本来就是空的才碰巧生效。
BASE_ENV={'NO_PROXY':'127.0.0.1,localhost','LLM_BASE_URL':'https://env.example/v1','LLM_API_KEY':'sk-env-secret-000001','LLM_MODEL':'env-model'}


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.path=Path(directory.name)/'model_config.json'
        set_config_path(self.path);self.addCleanup(set_config_path,None)
        self.env=patch.dict(os.environ,dict(BASE_ENV),clear=True);self.env.start();self.addCleanup(self.env.stop)

    def test_env_is_used_until_something_is_saved(self):
        self.assertEqual(public_config()['source'],'env')
        self.assertTrue(public_config()['llm_ready'])
        endpoint,key,*_=settings()
        self.assertEqual(endpoint,'https://env.example/v1/chat/completions')
        self.assertEqual(key,'sk-env-secret-000001')

    def test_saved_config_overrides_env_and_applies_without_restart(self):
        saved=save_config({'LLM_BASE_URL':'https://dashscope.aliyuncs.com/compatible-mode/v1',
                           'LLM_API_KEY':'sk-dash-abcdefgh','LLM_MODEL':'qwen-plus'})
        self.assertTrue(saved['llm_ready']);self.assertEqual(saved['source'],'runtime')
        self.assertEqual(settings()[0],'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions')
        self.assertEqual(settings()[1],'sk-dash-abcdefgh')
        # 换厂商在同一进程内直接生效，这正是"模型随时可替换"的那条路径
        save_config({'LLM_BASE_URL':'https://api.deepseek.com/v1','LLM_MODEL':'deepseek-chat','LLM_API_KEY':'sk-new-provider-0001'})
        self.assertEqual(public_config()['model'],'deepseek-chat')
        self.assertEqual(settings()[0],'https://api.deepseek.com/v1/chat/completions')

    def test_secret_never_returned_by_public_config(self):
        saved=save_config({'LLM_BASE_URL':'https://api.deepseek.com/v1',
                           'LLM_API_KEY':'sk-verysecretvalue123','LLM_MODEL':'deepseek-chat'})
        self.assertNotIn('sk-verysecretvalue123',json.dumps(saved,ensure_ascii=False))
        self.assertTrue(saved['has_key']);self.assertEqual(saved['key_hint'],'····e123')

    def test_blank_key_keeps_the_stored_one_and_clear_removes_it(self):
        save_config({'LLM_BASE_URL':'https://api.deepseek.com/v1',
                     'LLM_API_KEY':'sk-keepme-abcdefg','LLM_MODEL':'deepseek-chat'})
        saved=save_config({'LLM_MODEL':'deepseek-reasoner','LLM_API_KEY':''})
        self.assertTrue(saved['has_key']);self.assertEqual(saved['model'],'deepseek-reasoner')
        self.assertEqual(settings()[1],'sk-keepme-abcdefg')
        # 显式清除后不再回落 .env：否则用户以为清掉了，实际还在用旧密钥
        cleared=save_config({},clear_key=True)
        self.assertFalse(cleared['has_key'])
        with self.assertRaises(ModelError):settings()

    def test_invalid_config_is_rejected_without_touching_the_working_one(self):
        save_config({'LLM_BASE_URL':'https://api.deepseek.com/v1',
                     'LLM_API_KEY':'sk-good-key-000001','LLM_MODEL':'deepseek-chat'})
        before=self.path.read_text(encoding='utf-8')
        for bad in [{'LLM_BASE_URL':'http://evil.example.com/v1'},
                    {'LLM_BASE_URL':'https://user:pw@host/v1'},
                    {'LLM_BASE_URL':'https://api.deepseek.com/v1?x=1'},
                    {'LLM_JSON_MODE':'auto'}]:
            with self.subTest(bad=bad),self.assertRaises(ModelError):save_config(bad)
        self.assertEqual(self.path.read_text(encoding='utf-8'),before)
        self.assertEqual(public_config()['model'],'deepseek-chat')

    def test_non_ascii_key_is_rejected_with_a_clear_message(self):
        """密钥里混进中文时，绝不能报成「模型响应结构或 JSON 无效」。

        实测踩过：中文塞不进 HTTP 头，会抛 UnicodeEncodeError，而它是 ValueError
        的子类，被 chat() 的兜底 except 吞掉后统一报成 JSON 无效，把排查方向
        指到模型侧，实际错在密钥本身。
        """
        with self.assertRaises(ModelError) as err:
            save_config({'LLM_BASE_URL':'https://api.deepseek.com/v1','LLM_MODEL':'deepseek-chat',
                         'LLM_API_KEY':'sk-进度连接中断，请重新连接，不必重复上传。'})
        self.assertIn('中文或全角',str(err.exception))
        self.assertFalse(self.path.exists(),'校验失败不能落盘')

    def test_non_ascii_key_is_rejected_at_request_time_too(self):
        os.environ['LLM_API_KEY']='sk-中文密钥-abcdefgh'
        with self.assertRaises(ModelError) as err:
            settings()
        self.assertIn('中文或全角',str(err.exception))

    def test_config_file_is_not_read_as_a_document(self):
        """配置文件和文档同住在 data/ 下，不能被当成文档读进去。

        实测踩过：Store.list() 用 glob('*.json') 把 model_config.json 也当文档，
        /api/documents 随即在取 project_key 时 KeyError，界面上只看到
        「请求参数无效」——上传后结果列表完全刷不出来。这个组合此前没被走到过：
        测试都用临时 DATA_DIR，那里没有配置文件。
        """
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            (Path(directory)/'model_config.json').write_text('{"LLM_MODEL":"deepseek-flash"}',encoding='utf-8')
            self.assertEqual(store.list(),[])
            service=ThreadingHTTPServer(('127.0.0.1',0),Handler);service.store=store
            worker=threading.Thread(target=service.serve_forever,daemon=True);worker.start()
            try:
                request=urllib.request.Request('http://127.0.0.1:%d/api/documents'%service.server_port,
                                               headers={'X-Requested-With':'ApprovalAgent'})
                with urllib.request.urlopen(request,timeout=10) as r:self.assertEqual(json.load(r),{'groups':[]})
            finally:
                service.shutdown();service.server_close();store.executor.shutdown();worker.join()

    def test_blank_model_name_keeps_the_panel_disabled(self):
        saved=save_config({'LLM_BASE_URL':'https://api.deepseek.com/v1',
                           'LLM_API_KEY':'sk-abcdefghijklm','LLM_MODEL':''})
        self.assertFalse(saved['llm_ready']);self.assertIn('LLM_MODEL',saved['model_error'])

    def test_partial_config_saves_then_completes(self):
        """先填地址和模型名、密钥后补：不能因为缺密钥就把前半截也存不下。"""
        os.environ.pop('LLM_API_KEY',None)
        partial=save_config({'LLM_BASE_URL':'https://api.deepseek.com/v1','LLM_MODEL':'deepseek-chat'})
        self.assertFalse(partial['llm_ready'])
        self.assertIn('LLM_API_KEY',partial['model_error'])
        self.assertTrue(self.path.exists())
        completed=save_config({'LLM_API_KEY':'sk-later-added-0001'})
        self.assertTrue(completed['llm_ready'])
        self.assertEqual(settings()[1],'sk-later-added-0001')

    def test_clear_config_falls_back_to_env(self):
        save_config({'LLM_BASE_URL':'https://api.deepseek.com/v1',
                     'LLM_API_KEY':'sk-x-abcdefghij','LLM_MODEL':'deepseek-chat'})
        self.assertEqual(public_config()['source'],'runtime')
        restored=clear_config()
        self.assertEqual(restored['source'],'env');self.assertFalse(self.path.exists())
        self.assertEqual(restored['model'],'env-model');self.assertEqual(settings()[1],'sk-env-secret-000001')

    def test_http_endpoints_save_reset_and_reject_bad_input(self):
        with tempfile.TemporaryDirectory() as directory:
            service=ThreadingHTTPServer(('127.0.0.1',0),Handler);service.store=Store(directory)
            worker=threading.Thread(target=service.serve_forever,daemon=True);worker.start()
            base=f'http://127.0.0.1:{service.server_port}'
            def call(path,data=None,header=True):
                headers={'Content-Type':'application/json'}
                if header:headers['X-Requested-With']='ApprovalAgent'
                request=urllib.request.Request(base+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)
                return urllib.request.urlopen(request)
            try:
                with call('/api/config') as r:self.assertEqual(json.load(r)['source'],'env')
                with call('/api/model/config',{'values':{'LLM_BASE_URL':'https://api.deepseek.com/v1',
                        'LLM_API_KEY':'sk-http-secret-0001','LLM_MODEL':'deepseek-chat'}}) as r:saved=json.load(r)
                self.assertTrue(saved['llm_ready'])
                self.assertNotIn('sk-http-secret-0001',json.dumps(saved,ensure_ascii=False))
                with call('/api/config') as r:after=json.load(r)
                self.assertEqual(after['source'],'runtime');self.assertEqual(after['model'],'deepseek-chat')
                with self.assertRaises(urllib.error.HTTPError) as err:
                    call('/api/model/config',{'values':{'LLM_BASE_URL':'http://evil.example.com/v1'}})
                self.assertEqual(err.exception.code,400)
                self.assertEqual(public_config()['model'],'deepseek-chat')
                with call('/api/model/config/reset',{}) as r:restored=json.load(r)
                self.assertEqual(restored['source'],'env')
                with self.assertRaises(urllib.error.HTTPError) as err:call('/api/model/config',{'values':{}},header=False)
                self.assertEqual(err.exception.code,403)
            finally:
                service.shutdown();service.server_close();service.store.executor.shutdown();worker.join()


if __name__=='__main__':unittest.main()
