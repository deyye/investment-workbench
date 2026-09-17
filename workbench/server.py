"""One process, one HTTP listener, two business applications."""
import argparse
import atexit
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit
from flask import Flask, jsonify, redirect, render_template, request
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple
from core import llm, shell
from core.policy_model import SharedPolicyConfig
from app.server import Store
from policy_collector.config import AppConfig, _load_dotenv
from policy_collector.db import Database
from policy_collector.webapp import create_app as policy_app
from .approval import blueprint

ROOT = Path(__file__).resolve().parents[1]

# 设置页顶部「返回」要落到哪一页，以及那一页在人话里叫什么。
# 用顶栏和侧栏的叫法（"政策资料库"），不写 URL 片段——用户认的是名字。
# 顺序即优先级：具体路径在前，'/' 兜底放最后。
RETURN_TARGETS = (
    ('/policy/policies', '政策资料库'),
    ('/policy/quality', '材料质量'),
    ('/policy/sources', '采集来源'),
    ('/policy/provinces', '省份浏览'),
    ('/policy/maintenance', '数据维护'),
    ('/policy/runs', '处理进度'),
    ('/policy/todos', '待办清单'),
    ('/policy', '政策归集'),
    ('/approval', '审批文件'),
    ('/tasks', '任务进度'),
    ('/', '首页'),
)

# 本身就是"设置"的路径不能当返回目标：回到它们会被 303 再弹回设置页，
# 在界面上表现为一个按了没反应的按钮。
RETURN_BLOCKED = {'/settings/model', '/policy/settings/model'}


def return_target(referrer, host):
    """算出模型设置页顶部「返回」该指向哪里。

    设置页的入口是散的（工作台顶栏、首页模型胶囊、政策侧导航的「模型配置」），
    给每个入口挂 from 参数必然漏掉后加的那个，所以读浏览器自带的来路。
    判定不出来（书签直达、地址栏输入、跨站来路）就退回首页，不猜。
    """
    fallback = {'url': '/', 'label': '首页'}
    parsed = urlsplit(referrer or '')
    if parsed.netloc and parsed.netloc != host:
        return fallback
    path = parsed.path
    if not path.startswith('/') or path.startswith('//') or path.rstrip('/') in RETURN_BLOCKED:
        return fallback
    for prefix, label in RETURN_TARGETS:
        if path == prefix or (prefix != '/' and path.startswith(prefix + '/')):
            # 查询串要一起带回去：从"政策资料库第 3 页、按日期排序"点进设置，
            # 返回时必须还是那一页，否则等于把人丢回列表开头。
            return {'url': path + (('?' + parsed.query) if parsed.query else ''), 'label': label}
    return fallback


def create_app(data_dir=None, policy_config=None):
    _load_dotenv(ROOT / '.env')
    data = Path(data_dir or os.getenv('WORKBENCH_DATA_DIR', ROOT / 'data')).resolve()
    data.mkdir(parents=True, exist_ok=True)
    llm.set_config_path(data / 'model_config.json')
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
    app.secret_key = secrets.token_hex(32)
    # 套件条高亮判据（与政策侧共用 core/shell.py 一份实现）。本应用里未匹配到
    # `/approval` / `/policy` / `/settings` 前缀的就是工作台自己的页（`/`、`/tasks`）。
    shell.install(app, default='workbench')
    store = Store(data / 'approval')
    app.extensions['approval_store'] = store
    atexit.register(store.executor.shutdown, wait=True)
    app.register_blueprint(blueprint(store), url_prefix='/approval')
    cfg = policy_config or AppConfig.load()
    cfg.data_dir = data / 'policy'
    cfg.downloads_dir = cfg.data_dir / 'downloads'
    cfg.db_path = cfg.data_dir / 'policy.db'
    cfg.llm = SharedPolicyConfig(cfg.llm)
    policy = policy_app(cfg)
    app.extensions['policy_app'] = policy
    app.extensions['policy_config'] = cfg
    # The old policy form is inaccessible; all entry points lead to one form.
    policy.view_functions['model_settings'] = lambda: redirect('/settings/model', code=303)
    probe_lock = threading.Lock()

    @app.before_request
    def protect_write():
        if request.method == 'POST':
            origin = request.headers.get('Origin')
            if request.headers.get('X-Requested-With') != 'ApprovalAgent' or (origin and urlsplit(origin).netloc != request.host):
                return jsonify(error='来源校验失败'), 403

    @app.after_request
    def headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('Cache-Control', 'no-store')
        return response

    @app.get('/')
    def index():
        return render_template('home.html', model=llm.public_config())

    @app.get('/policy')
    def policy_redirect():
        return redirect('/policy/')

    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'businesses': ['approval', 'policy']}

    @app.get('/settings/model')
    def model_page():
        return render_template('model.html', back=return_target(request.referrer, request.host))

    @app.get('/api/model/config')
    def config():
        result = llm.public_config()
        result['options'] = {name: llm.effective(name, llm.DEFAULTS.get(name, '')) for name in
            ('LLM_TIMEOUT_SECONDS', 'LLM_MAX_RETRIES', 'LLM_MAX_TOKENS', 'LLM_JSON_MODE',
             'LLM_EXTRA_BODY', 'LLM_ALLOW_NO_KEY', 'LLM_HTTP_HOSTS')}
        return result

    @app.post('/api/model/config')
    def save():
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get('values'), dict):
            return {'error': '配置格式无效'}, 400
        try:
            return llm.save_config(body['values'], clear_key=body.get('clear_key') is True)
        except llm.ModelError as exc:
            return {'error': str(exc)}, 400

    @app.post('/api/model/test')
    def probe():
        if not probe_lock.acquire(blocking=False):
            return {'error': '正在检查连接，请稍后重试'}, 429
        try:
            body = request.get_json()
            if not isinstance(body, dict):
                return {'error': '请求格式无效'}, 400
            return llm.probe(vision=body.get('vision') is True)
        except llm.ModelError as exc:
            return {'error': str(exc)}, 502
        finally:
            probe_lock.release()

    @app.get('/tasks')
    def tasks():
        db = Database(cfg.db_path)
        try:
            runs = db.list_runs(limit=20)
        finally:
            db.close()
        return render_template('tasks.html', jobs=store.snapshot_jobs(), runs=runs)

    app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {'/policy': policy.wsgi_app})
    return app


def main():
    _load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description='投资项目智能工作台')
    parser.add_argument('--host', default=os.getenv('HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT', '8765')))
    parser.add_argument('--data-dir', default=None)
    args = parser.parse_args()
    app = create_app(args.data_dir)
    print(f'投资项目智能工作台：http://{args.host}:{args.port}', flush=True)
    try:
        run_simple(args.host, args.port, app, threaded=True, use_reloader=False)
    finally:
        app.extensions['approval_store'].executor.shutdown(wait=True)


if __name__ == '__main__':
    main()
