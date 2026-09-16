"""Expose the existing approval service through Flask, without another server.

Handler.get/post are business dispatchers. The adapter translates requests and
responses only, retaining document/review/export semantics and the task store.
"""
import io
import json
from types import SimpleNamespace
from flask import Blueprint, Response, request
from app.server import Handler


class FlaskHandler(Handler):
    def __init__(self, store, path):
        self.server = SimpleNamespace(store=store)
        self.path = path
        self.headers = request.headers
        self.rfile = io.BytesIO(request.get_data())
        self.response = None

    def send(self, data, status=200, ctype='application/json; charset=utf-8', download=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False)
        self.response = Response(data, status=status, content_type=ctype)
        self.response.headers['Cache-Control'] = 'no-store'
        self.response.headers['X-Content-Type-Options'] = 'nosniff'
        self.response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; frame-ancestors 'none'"
        if download:
            self.response.headers['Content-Disposition'] = f'attachment; filename="{download}"'
        return self.response


def blueprint(store):
    bp = Blueprint('approval', __name__)

    @bp.route('/', defaults={'path': ''}, methods=['GET', 'POST'])
    @bp.route('/<path:path>', methods=['GET', 'POST'])
    def dispatch(path):
        # Legacy model mutation routes cannot create a second settings surface.
        if path.startswith('api/model/'):
            return {'error': '请使用统一模型设置 /settings/model'}, 410
        query = request.query_string.decode('latin-1')
        handler = FlaskHandler(store, '/' + path + ('?' + query if query else ''))
        if request.method == 'POST':
            handler.do_POST()
        else:
            handler.do_GET()
        return handler.response

    return bp
