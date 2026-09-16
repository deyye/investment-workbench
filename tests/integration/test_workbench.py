import base64
import io
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fitz
import pytest
from bs4 import BeautifulSoup
from core import llm
from policy_collector.llm_client import LLMClient
from policy_collector.pipeline import Pipeline
from workbench.server import create_app

HEADERS = {'X-Requested-With': 'ApprovalAgent'}


@pytest.fixture
def suite(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(('LLM_', 'VISION_', 'POLICY_')):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    app = create_app(tmp_path)
    yield app, app.test_client(), tmp_path
    app.extensions['approval_store'].executor.shutdown(wait=True)
    llm.set_config_path(None)


def post(client, path, body):
    return client.post(path, json=body, headers=HEADERS)


def test_pages_and_namespaced_links(suite):
    app, client, _ = suite
    for url in ['/', '/approval/', '/approval/app.js', '/approval/style.css',
                '/approval/api/config', '/policy/', '/policy/policies', '/policy/sources',
                '/policy/todos', '/policy/runs', '/policy/quality', '/policy/maintenance',
                '/settings/model', '/tasks', '/api/health']:
        response = client.get(url)
        assert response.status_code == 200, url
    doc = BeautifulSoup(client.get('/policy/').data, 'html.parser')
    for node in doc.select('a[href],link[href],script[src]'):
        href = node.get('href') or node.get('src')
        if href.startswith('/'):
            assert client.get(href).status_code in (200, 302, 303, 308), href
    assert client.get('/policy/settings/model').location == '/settings/model'
    assert b'id="modelBase"' not in client.get('/approval/').data
    assert post(client, '/approval/api/model/config', {}).status_code == 410


def test_write_origin_and_policy_csrf(suite):
    _, client, _ = suite
    assert client.post('/api/model/config', json={'values': {}}).status_code == 403
    assert client.post('/api/model/config', json={'values': {}}, headers={**HEADERS, 'Origin': 'https://other.invalid'}).status_code == 403
    assert client.post('/policy/sources/run-all', data={}).status_code == 400
    assert client.post('/approval/api/upload', json={'files': []}).status_code == 403


class Wire(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append(body)
        response = self.server.reply
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())


@pytest.fixture
def wire():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Wire)
    server.calls = []
    server.reply = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 11, 'completion_tokens': 3}}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def configure(client, wire):
    response = post(client, '/api/model/config', {'values': {
        'LLM_BASE_URL': f'http://127.0.0.1:{wire.server_port}/v1', 'LLM_API_KEY': 'test-secret',
        'LLM_MODEL': 'shared-text', 'VISION_MODEL': 'shared-vision', 'LLM_MAX_RETRIES': '0'}})
    assert response.status_code == 200
    return response


def test_both_businesses_use_shared_wire_and_hot_reload(suite, wire):
    app, client, data = suite
    configure(client, wire)
    from app.model_client import chat
    policy_client = LLMClient(app.extensions['policy_config'].llm)
    assert chat([{'role': 'user', 'content': 'test'}], llm.effective('LLM_MODEL'))['ok']
    assert policy_client.chat_json('test', 'test')['ok']
    assert policy_client.usage == {'input_tokens': 11, 'output_tokens': 3}
    assert policy_client.usage_reported
    assert [c['model'] for c in wire.calls] == ['shared-text', 'shared-text']
    assert post(client, '/api/model/test', {'vision': True}).status_code == 200
    assert wire.calls[-1]['model'] == 'shared-vision'
    assert wire.calls[-1]['messages'][0]['content'][1]['type'] == 'image_url'
    assert post(client, '/api/model/config', {'values': {'LLM_MODEL': 'updated'}}).status_code == 200
    assert client.get('/approval/api/config').json['model'] == 'updated'
    policy_client.chat_json('test', 'test')
    assert wire.calls[-1]['model'] == 'updated'
    assert (data / 'model_config.json').stat().st_mode & 0o777 == 0o600
    assert not (data / 'policy/llm.local.json').exists()
    assert not (data / 'approval/model_config.json').exists()
    assert 'test-secret' not in client.get('/api/model/config').text
    assert 'test-secret' not in client.get('/approval/api/config').text
    assert post(client, '/api/model/config', {'values': {'LLM_ENABLED': 'false'}}).status_code == 200
    assert not policy_client.available
    assert not client.get('/approval/api/config').json['llm_ready']
    count = len(wire.calls)
    assert policy_client.chat_json('test', 'test') is None
    assert post(client, '/api/model/test', {}).status_code == 502
    assert len(wire.calls) == count


def test_config_survives_restart_and_prevents_key_reuse(suite, wire, monkeypatch):
    _, client, data = suite
    configure(client, wire)
    assert post(client, '/api/model/config', {'values': {'LLM_BASE_URL': 'https://another.invalid/v1'}}).status_code == 400
    monkeypatch.setenv('LLM_MODEL', 'old-environment-value')
    llm.set_config_path(data / 'model_config.json')
    assert llm.public_config()['model'] == 'shared-text'
    assert post(client, '/api/model/config', {'values': {'LLM_BASE_URL': 'http://127.0.0.1:9999/v1', 'LLM_ALLOW_NO_KEY': 'true'}, 'clear_key': True}).status_code == 200
    assert llm.effective('LLM_API_KEY') == ''


@pytest.mark.parametrize('content,finish,refusal', [('{"a":1,"a":2}', 'stop', None), ('{}','length',None), ('{}','stop','refused')])
def test_shared_invalid_output_falls_back(suite, wire, content, finish, refusal):
    app, client, _ = suite
    configure(client, wire)
    wire.reply['choices'][0] = {'message': {'content': content, 'refusal': refusal}, 'finish_reason': finish}
    pc = LLMClient(app.extensions['policy_config'].llm)
    assert pc.chat_json('s', 'u') is None
    assert pc.last_error
    assert post(client, '/api/model/test', {}).status_code == 502


def test_upload_review_evidence_export_and_persistence(suite):
    app, client, data = suite
    with fitz.open() as pdf:
        page = pdf.new_page()
        for y, text in [(60, '关于测试项目初步设计的批复'), (90, '一、建设内容：总建筑面积100平方米。'),
                        (120, '二、建设地点：测试园区。'), (150, '项目代码：2609-330100-04-01-100001')]:
            page.insert_text((40,y), text, fontname='china-s', fontsize=12)
        blob = pdf.tobytes()
    response = post(client, '/approval/api/upload', {'files': [{'name':'测试.pdf','data':base64.b64encode(blob).decode()}], 'use_llm':False})
    assert response.status_code == 202
    jid = response.json['job_id']
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get('/approval/api/jobs/'+jid).json
        if job['status'] != 'running':
            break
        time.sleep(.05)
    assert job['status'] == 'completed' and not job['errors']
    groups = client.get('/approval/api/documents').json['groups']
    doc = groups[0]['documents'][0]
    assert doc['fields']['建设地点']['evidence']
    did = doc['id']
    assert client.get(f'/approval/api/documents/{did}/pages/1.png').data.startswith(b'\x89PNG')
    response = post(client, f'/approval/api/documents/{did}/review', {'kind':'fixed','name':'建设地点','value':'人工核对测试园区','revision':doc['revision'],'reason':'集成测试'})
    assert response.status_code == 200, response.text
    assert client.get(f'/approval/api/documents/{did}').json['fields']['建设地点']['value'] == '人工核对测试园区'
    assert client.get('/approval/api/export.xlsx').data.startswith(b'PK')
    assert client.get('/tasks').status_code == 200
    from app.server import Store
    reopened = Store(data / 'approval')
    try:
        assert reopened.get(did)['fields']['建设地点']['value'] == '人工核对测试园区'
    finally:
        reopened.executor.shutdown()


def test_policy_pipeline_review_and_mounted_post_redirect(suite):
    app, client, _ = suite
    pipe = Pipeline(app.extensions['policy_config'])
    try:
        stats = pipe.run_demo(samples_dir=Path('samples/policies'))
        assert stats.ingested == 4
        assert pipe.run_demo(samples_dir=Path('samples/policies')).ingested == 0
        rows = pipe.db.query_policies(limit=10)
        pid = rows[0]['id']
        html = client.get(f'/policy/policies/{pid}').text
        soup = BeautifulSoup(html, 'html.parser')
        token = soup.select_one('input[name=csrf_token]')['value']
        response = client.post(f'/policy/policies/{pid}/review', data={'csrf_token':token,'action':'adjust','categories':['guide']})
        assert response.status_code == 302
        assert response.location.startswith('/policy/')
        assert pipe.db.get_policy(pid)['review_status'] == 'adjusted'
        assert client.get(response.location).status_code == 200
        assert client.get('/tasks').status_code == 200
    finally:
        pipe.close()
