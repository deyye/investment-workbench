"""Actual local HTTP wire tests; no external provider credentials."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from app.model_client import chat, probe, public_config, settings, set_config_path, ModelError

class WireHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append((self.path,self.headers.get('Authorization'),body))
        status,reply=self.server.replies.pop(0)
        self.send_response(status)
        if status==302:self.send_header('Location','http://127.0.0.1:1/stolen')
        self.end_headers()
        self.wfile.write(json.dumps(reply).encode())


def completion(content='{"ok":true}',finish='stop'):
    return {'choices':[{'message':{'content':content},'finish_reason':finish}]}

class ModelClientTests(unittest.TestCase):
    def setUp(self):
        # 禁用配置文件：否则默认路径下的 data/model_config.json 一旦被界面上保存过，
        # 「未配置时 llm_ready 为假」这类断言就会跟着环境漂移。
        set_config_path(None);self.addCleanup(set_config_path,None)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),WireHandler)
        self.server.calls=[];self.server.replies=[]
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.env=patch.dict(os.environ,{'LLM_BASE_URL':f'http://127.0.0.1:{self.server.server_port}/v1','NO_PROXY':'127.0.0.1,localhost','LLM_API_KEY':'test-secret','LLM_MODEL':'test-model','VISION_MODEL':'test-vision','LLM_MAX_RETRIES':'0'},clear=True)
        self.env.start()
    def tearDown(self):
        self.env.stop();self.server.shutdown();self.server.server_close();self.thread.join()
    def test_text_probe_wire(self):
        self.server.replies=[(200,completion())]
        self.assertTrue(probe()['ok'])
        path,key,body=self.server.calls[0]
        self.assertEqual(path,'/v1/chat/completions');self.assertEqual(key,'Bearer test-secret')
        self.assertEqual(body['response_format'],{'type':'json_object'})
        self.assertNotIn('test-secret',json.dumps(public_config()))
    def test_vision_probe_sends_synthetic_image(self):
        self.server.replies=[(200,completion())];self.assertTrue(probe(True)['vision'])
        body=self.server.calls[0][2];self.assertEqual(body['model'],'test-vision')
        self.assertTrue(body['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))
    def test_retry_transient_only(self):
        os.environ['LLM_MAX_RETRIES']='2';self.server.replies=[(429,{'error':'secret detail'}),(200,completion())]
        with patch('app.model_client.time.sleep'):self.assertTrue(probe()['ok'])
        self.assertEqual(len(self.server.calls),2)
    def test_auth_failure_redacted_and_not_retried(self):
        os.environ['LLM_MAX_RETRIES']='2';self.server.replies=[(401,{'error':'test-secret private content'})]
        with self.assertRaisesRegex(ModelError,'鉴权失败') as err:probe()
        self.assertNotIn('test-secret',str(err.exception));self.assertEqual(len(self.server.calls),1)
    def test_redirect_not_followed(self):
        self.server.replies=[(302,{})]
        with self.assertRaisesRegex(ModelError,'302'):probe()
    def test_truncated_invalid_and_wrong_shape(self):
        for response in [completion('{}','length'),completion('not json'),completion('[]'),{}]:
            with self.subTest(response=response):
                self.server.replies=[(200,response)]
                with self.assertRaises(ModelError):probe()
    def test_no_json_mode_and_fenced_response(self):
        os.environ['LLM_JSON_MODE']='false';os.environ['LLM_EXTRA_BODY']='{"enable_thinking":false}'
        self.server.replies=[(200,completion('```json\n{"ok":true}\n```'))]
        self.assertTrue(probe()['ok']);body=self.server.calls[0][2]
        self.assertNotIn('response_format',body);self.assertFalse(body['enable_thinking'])
    def test_complete_endpoint_not_appended_twice(self):
        os.environ['LLM_BASE_URL']+='/chat/completions';self.server.replies=[(200,completion())]
        probe();self.assertEqual(self.server.calls[0][0],'/v1/chat/completions')
    def test_secret_file(self):
        with tempfile.NamedTemporaryFile(mode='w') as f:
            f.write('file-secret\n');f.flush();os.environ['LLM_API_KEY_FILE']=f.name
            self.server.replies=[(200,completion())];probe();self.assertEqual(self.server.calls[0][1],'Bearer file-secret')
    def test_config_validation(self):
        for key,value in [('LLM_BASE_URL','http://untrusted/v1'),('LLM_BASE_URL','https://user:password@host/v1'),('LLM_MAX_RETRIES','5'),('LLM_TIMEOUT_SECONDS','nan'),('LLM_EXTRA_BODY','{"messages":[]}'),('LLM_JSON_MODE','auto'),('LLM_API_KEY','')]:
            with self.subTest(key=key,value=value),patch.dict(os.environ,{key:value}):
                with self.assertRaises(ModelError):settings()
    def test_explicit_local_gateway(self):
        with patch.dict(os.environ,{'LLM_BASE_URL':'http://model-gateway:8000/v1','LLM_HTTP_HOSTS':'model-gateway','LLM_ALLOW_NO_KEY':'true','LLM_API_KEY':''}):
            self.assertTrue(public_config()['llm_ready'])
    def test_wrong_probe_result(self):
        self.server.replies=[(200,completion('{"ok":false}'))]
        with self.assertRaisesRegex(ModelError,'指令验证'):probe()
    def test_diagnostic_api_and_missing_config(self):
        import urllib.request
        import urllib.error
        from app.server import Handler,Store
        with tempfile.TemporaryDirectory() as directory:
            service=ThreadingHTTPServer(('127.0.0.1',0),Handler);service.store=Store(directory)
            worker=threading.Thread(target=service.serve_forever,daemon=True);worker.start()
            base=f'http://127.0.0.1:{service.server_port}'
            try:
                self.server.replies=[(200,completion())]
                request=urllib.request.Request(base+'/api/model/test',data=b'{}',headers={'X-Requested-With':'ApprovalAgent','Content-Type':'application/json'})
                with urllib.request.urlopen(request) as r:self.assertTrue(json.load(r)['ok'])
                with patch.dict(os.environ,{'LLM_API_KEY':''}):
                    with urllib.request.urlopen(base+'/api/config') as r:self.assertFalse(json.load(r)['llm_ready'])
                    with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
                    self.assertEqual(error.exception.code,502)
                bad=urllib.request.Request(base+'/api/model/test',data=b'{}')
                with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(bad)
                self.assertEqual(error.exception.code,403)
            finally:
                service.shutdown();service.server_close();service.store.executor.shutdown();worker.join()
