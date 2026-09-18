"""One process, one HTTP listener, two business applications."""
import argparse
import atexit
import base64
import hmac
import ipaddress
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit
from flask import Flask, jsonify, redirect, render_template, request
from werkzeug.middleware.dispatcher import DispatcherMiddleware
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

SECURITY_HEADERS = (
    ('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; "
     "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
     "connect-src 'self'; font-src 'self' data:; object-src 'none'; "
     "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"),
    ('X-Content-Type-Options', 'nosniff'),
    ('X-Frame-Options', 'DENY'),
    ('Referrer-Policy', 'same-origin'),
    ('Permissions-Policy', 'camera=(), microphone=(), geolocation=()'),
    ('Cross-Origin-Opener-Policy', 'same-origin'),
    ('X-Permitted-Cross-Domain-Policies', 'none'),
)


class DeploymentMiddleware:
    """Apply browser hardening and optional HTTP Basic authentication.

    This sits outside ``DispatcherMiddleware`` so the same policy protects the
    workbench, approval adapter and mounted policy application.
    """

    def __init__(self, application, auth=None):
        self.application = application
        self.auth = auth

    @staticmethod
    def _secure_start(start_response):
        def wrapped(status, headers, exc_info=None):
            existing = {name.lower() for name, _ in headers}
            headers.extend((name, value) for name, value in SECURITY_HEADERS
                           if name.lower() not in existing)
            return start_response(status, headers, exc_info)
        return wrapped

    def _authenticated(self, environ):
        if self.auth is None:
            return True
        value = environ.get('HTTP_AUTHORIZATION', '')
        if not value.startswith('Basic '):
            return False
        try:
            raw = base64.b64decode(value[6:], validate=True).decode('utf-8')
            username, password = raw.split(':', 1)
        except (ValueError, UnicodeError):
            return False
        expected_user, expected_password = self.auth
        return (hmac.compare_digest(username, expected_user)
                and hmac.compare_digest(password, expected_password))

    def __call__(self, environ, start_response):
        secure_start = self._secure_start(start_response)
        # Health probes contain no business data and must remain usable by
        # container/orchestrator checks even when the UI is protected.
        if environ.get('PATH_INFO') == '/api/health':
            return self.application(environ, secure_start)
        if not self._authenticated(environ):
            body = '需要身份验证'.encode('utf-8')
            secure_start('401 Unauthorized', [
                ('Content-Type', 'text/plain; charset=utf-8'),
                ('Content-Length', str(len(body))),
                ('Cache-Control', 'no-store'),
                ('WWW-Authenticate', 'Basic realm="Investment Workbench", charset="UTF-8"'),
            ])
            return [body]
        return self.application(environ, secure_start)


def is_loopback_host(host):
    """Return whether a bind target is restricted to this machine."""
    if host.lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def deployment_auth(host):
    """Read deployment safeguards after ``.env`` has been loaded."""
    username = os.getenv('WORKBENCH_AUTH_USER', '')
    password = os.getenv('WORKBENCH_AUTH_PASSWORD', '')
    if bool(username) != bool(password):
        raise ValueError('WORKBENCH_AUTH_USER 与 WORKBENCH_AUTH_PASSWORD 必须同时设置')
    if not is_loopback_host(host):
        if os.getenv('WORKBENCH_ALLOW_REMOTE', '').lower() != 'true':
            raise ValueError('非本机监听默认关闭；确认已配置 TLS 反向代理后设置 WORKBENCH_ALLOW_REMOTE=true')
        # The supplied Compose file publishes the container port on the host's
        # loopback interface only.  Inside that container the process still has
        # to bind 0.0.0.0, so allow this narrowly named, explicit assertion.
        # A standalone image does not set it and therefore remains fail-closed.
        container_loopback = os.getenv('WORKBENCH_CONTAINER_LOOPBACK_ONLY', '').lower() == 'true'
        if container_loopback:
            return (username, password) if username else None
        if not username:
            raise ValueError('非本机监听必须设置 WORKBENCH_AUTH_USER 与 WORKBENCH_AUTH_PASSWORD')
        if os.getenv('WORKBENCH_COOKIE_SECURE', '').lower() != 'true':
            raise ValueError('非本机监听必须通过 TLS，并设置 WORKBENCH_COOKIE_SECURE=true')
    return (username, password) if username else None


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


def create_app(data_dir=None, policy_config=None, auth=None):
    _load_dotenv(ROOT / '.env')
    data = Path(data_dir or os.getenv('WORKBENCH_DATA_DIR', ROOT / 'data')).resolve()
    data.mkdir(parents=True, exist_ok=True)
    llm.set_config_path(data / 'model_config.json')
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
    cookie_secure = os.getenv('WORKBENCH_COOKIE_SECURE', '').lower() == 'true'
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_SECURE'] = cookie_secure
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
    # ``policy`` is a separate Flask application mounted below ``/policy``.
    # Cookie settings on the outer workbench app do not propagate through
    # DispatcherMiddleware, so apply the same browser policy explicitly.
    policy.config['SESSION_COOKIE_HTTPONLY'] = True
    policy.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    policy.config['SESSION_COOKIE_SECURE'] = cookie_secure
    policy.config['SESSION_COOKIE_PATH'] = '/policy'
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

    mounted = DispatcherMiddleware(app.wsgi_app, {'/policy': policy.wsgi_app})
    app.wsgi_app = DeploymentMiddleware(mounted, auth=auth)
    return app


def main():
    _load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description='投资项目智能工作台')
    parser.add_argument('--host', default=os.getenv('HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT', '8765')))
    parser.add_argument('--data-dir', default=None)
    parser.add_argument('--dev-server', action='store_true', help='仅开发调试：改用 Werkzeug 开发服务器')
    args = parser.parse_args()
    try:
        auth = deployment_auth(args.host)
    except ValueError as exc:
        parser.error(str(exc))
    app = create_app(args.data_dir, auth=auth)
    print(f'投资项目智能工作台：http://{args.host}:{args.port}', flush=True)
    try:
        if args.dev_server:
            from werkzeug.serving import run_simple
            run_simple(args.host, args.port, app, threaded=True, use_reloader=False)
        else:
            try:
                from waitress import serve
            except ImportError:
                parser.error('缺少生产服务器依赖，请先执行 pip install -r requirements.txt')
            serve(app, host=args.host, port=args.port, threads=8)
    finally:
        app.extensions['approval_store'].executor.shutdown(wait=True)


if __name__ == '__main__':
    main()
