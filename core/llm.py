"""Bounded OpenAI-compatible transport; no document or credential logging."""
import json
import os
import socket
import threading
from dataclasses import dataclass
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


class ModelError(ValueError):
    """A user-safe error; never include provider response bodies or URLs."""


# 界面上可保存的键。保存后立即生效，不需要重启服务：
# settings() 每次都现读一次，而不是在启动时固化到 os.environ。
EDITABLE = ('LLM_ENABLED', 'LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL', 'VISION_MODEL', 'LLM_TIMEOUT_SECONDS',
            'LLM_MAX_RETRIES', 'LLM_MAX_TOKENS', 'LLM_JSON_MODE', 'LLM_EXTRA_BODY',
            'LLM_HTTP_HOSTS', 'LLM_ALLOW_NO_KEY')
DEFAULTS = {'LLM_ENABLED': 'true', 'LLM_TIMEOUT_SECONDS': '90', 'LLM_MAX_RETRIES': '2', 'LLM_MAX_TOKENS': '8192',
            'LLM_JSON_MODE': 'true', 'LLM_EXTRA_BODY': '{}', 'LLM_ALLOW_NO_KEY': 'false'}
# 默认指向仓库 data/：命令行脚本（scripts/check_model.py 等）不走服务启动流程，
# 也必须能读到界面上保存的配置，否则会出现「界面配好了、脚本却说没配」。
_CONFIG_PATH = Path(__file__).resolve().parents[1] / 'data' / 'model_config.json'
_CACHE = {'mtime': None, 'data': {}}
_CONFIG_LOCK = threading.RLock()

def synchronized(fn):
    def wrapped(*args, **kwargs):
        with _CONFIG_LOCK:
            return fn(*args, **kwargs)
    return wrapped


@synchronized
def set_config_path(path):
    """服务启动时指向实际 DATA_DIR；传 None 表示禁用配置文件（测试隔离用）。"""
    global _CONFIG_PATH
    _CONFIG_PATH = Path(path) if path is not None else None
    _CACHE.update(mtime=None, data={})


@synchronized
def runtime_config():
    """界面保存的配置；按 mtime 缓存，避免每次请求都读盘。"""
    path = _CONFIG_PATH
    if path is None:
        return {}
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        _CACHE.update(mtime=None, data={})
        return {}
    if _CACHE['mtime'] != mtime:
        try:
            loaded = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            loaded = {}
        _CACHE.update(mtime=mtime, data=loaded if isinstance(loaded, dict) else {})
    return _CACHE['data']


@synchronized
def effective(name, default=''):
    """键存在就用它（哪怕是空串），否则回落环境变量。

    「存在即生效」是刻意设计：界面上把密钥清空后必须真的清掉，
    而不是悄悄回落成 .env 里的旧值——否则用户会以为换了模型其实没换。
    """
    runtime = runtime_config()
    value = runtime[name] if name in runtime else os.getenv(name)
    if value is None:
        value = default
    return value if isinstance(value, str) else str(value)


def _shape(base, allowed_http, timeout, retries, tokens, extra, json_mode):
    """地址与参数的格式校验。

    刻意不检查密钥是否存在：缺密钥只是「未就绪」，由 public_config 表达；
    若在这里拦下，用户就没法先把地址和模型名存好、之后再来补密钥。
    """
    base = base.strip().rstrip('/')
    parsed = urlsplit(base)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelError('模型地址无效，请填写 API 基础地址')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'} | allowed_http):
        raise ModelError('模型地址需 HTTPS；内网 HTTP 主机需明确配置 LLM_HTTP_HOSTS')
    try:
        timeout = float(timeout)
        retries = int(retries)
        tokens = int(tokens)
        if not 1 <= timeout <= 300 or not 0 <= retries <= 3 or not 128 <= tokens <= 32768:
            raise ValueError()
        extra = json.loads(extra or '{}')
        if not isinstance(extra, dict) or set(extra) - {'enable_thinking', 'reasoning_effort'}:
            raise ValueError()
    except (ValueError, TypeError):
        raise ModelError('模型参数无效，请检查超时、重试、输出长度或附加参数') from None
    if json_mode not in {'true', 'false'}:
        raise ModelError('LLM_JSON_MODE 只能为 true 或 false')
    return base, parsed, timeout, retries, tokens, extra, json_mode


def _check_key(key):
    """密钥必须能放进 HTTP 请求头。

    中文和全角标点不是合法的头字符，请求会在发出之前就抛 UnicodeEncodeError。
    而它是 ValueError 的子类，会被下游那句 except 一并吞掉、误报成「模型响应
    结构或 JSON 无效」——把排查方向指到模型侧，实际错在密钥本身。
    """
    if not key:
        return key
    try:
        key.encode('latin-1')
    except UnicodeEncodeError:
        raise ModelError('密钥含中文或全角字符，请重新复制粘贴（常见原因是误把界面提示文字粘进了密钥框）') from None
    return key


def _resolve(base, allowed_http, key, key_file, allow_no_key, timeout, retries, tokens, extra, json_mode):
    """把一组原始值校验成可用的连接参数；发起请求前的最后一道关。"""
    base, parsed, timeout, retries, tokens, extra, json_mode = _shape(
        base, allowed_http, timeout, retries, tokens, extra, json_mode)
    if key_file:
        try:
            key = Path(key_file).read_text(encoding='utf-8').strip()
        except OSError:
            raise ModelError('无法读取模型密钥文件') from None
    if not key and not allow_no_key:
        raise ModelError('请配置 LLM_API_KEY 或 LLM_API_KEY_FILE')
    _check_key(key)
    endpoint = base if parsed.path.endswith('/chat/completions') else base + '/chat/completions'
    return endpoint, key, timeout, retries, tokens, extra, json_mode == 'true'


def _resolve_mapping(get):
    return _resolve(get('LLM_BASE_URL'),
                    {x.strip() for x in get('LLM_HTTP_HOSTS').split(',') if x.strip()},
                    get('LLM_API_KEY'),
                    get('LLM_API_KEY_FILE'),
                    get('LLM_ALLOW_NO_KEY', 'false').lower() == 'true',
                    get('LLM_TIMEOUT_SECONDS', '90'),
                    get('LLM_MAX_RETRIES', '2'),
                    get('LLM_MAX_TOKENS', '8192'),
                    get('LLM_EXTRA_BODY', '{}'),
                    get('LLM_JSON_MODE', 'true').lower())


@synchronized
def settings():
    return _resolve_mapping(lambda name, default='': effective(name, default))


def _key_hint(value):
    """只回报「已配置」或末四位；明文永不出后端。"""
    if not value:
        return ''
    return '····' + value[-4:] if len(value) >= 12 else '已配置'


@synchronized
def public_config():
    """对外只暴露是否就绪、模型名与密钥存在性，密钥明文不下发到浏览器。"""
    model = effective('LLM_MODEL')
    out = {'llm_ready': False, 'model': model, 'vision_model': effective('VISION_MODEL'),
           'base_url': effective('LLM_BASE_URL'), 'has_key': False, 'key_hint': '',
           'enabled': effective('LLM_ENABLED', 'true').lower() == 'true',
           'source': 'runtime' if runtime_config() else 'env', 'model_error': ''}
    try:
        settings()
        if not out['enabled']:
            raise ModelError('大模型已停用，两个业务将使用本地规则')
        if not model.strip():
            raise ModelError('请配置 LLM_MODEL')
    except (ModelError, ValueError) as exc:
        reason = str(exc).strip() or '请检查服务地址、密钥和模型名'
        out['model_error'] = '模型配置未完成：' + reason + '（可在上方「模型设置」中填写保存，立即生效）'
        return out
    key = effective('LLM_API_KEY').strip()
    from_file = not key and bool(effective('LLM_API_KEY_FILE').strip())
    out['has_key'] = bool(key) or from_file or effective('LLM_ALLOW_NO_KEY', 'false').lower() == 'true'
    out['key_hint'] = '由密钥文件提供' if from_file else _key_hint(key)
    out['llm_ready'] = True
    return out


@synchronized
def save_config(values, clear_key=False):
    """保存界面提交的配置；密钥留空表示保持原值，避免回填时误清空。

    先校验再落盘——不合法的地址不会覆盖掉原本可用的配置。
    """
    current = dict(runtime_config())
    new_base = str(values.get('LLM_BASE_URL', effective('LLM_BASE_URL'))).strip().rstrip('/')
    if new_base != effective('LLM_BASE_URL').rstrip('/') and not str(values.get('LLM_API_KEY', '')).strip() and not clear_key and (effective('LLM_API_KEY') or effective('LLM_API_KEY_FILE')):
        raise ModelError('更换服务地址时请重新填写密钥，或明确清除旧密钥')
    if clear_key:
        current['LLM_API_KEY_FILE'] = ''
    if str(values.get('LLM_API_KEY', '')).strip():
        current['LLM_API_KEY_FILE'] = ''
    if 'LLM_ENABLED' in values and str(values['LLM_ENABLED']).lower() not in ('true', 'false'):
        raise ModelError('启用状态必须为 true 或 false')
    for name in EDITABLE:
        if name not in values:
            continue
        value = values[name]
        if name == 'LLM_API_KEY' and not str(value or '').strip():
            continue
        if value is None:
            continue
        current[name] = str(value).strip()
    if clear_key:
        current['LLM_API_KEY'] = ''
    candidate = {name: (current[name] if name in current else effective(name, DEFAULTS.get(name, ''))) for name in EDITABLE}
    _shape(candidate['LLM_BASE_URL'],
           {x.strip() for x in candidate['LLM_HTTP_HOSTS'].split(',') if x.strip()},
           candidate['LLM_TIMEOUT_SECONDS'], candidate['LLM_MAX_RETRIES'], candidate['LLM_MAX_TOKENS'],
           candidate['LLM_EXTRA_BODY'], candidate['LLM_JSON_MODE'].lower())
    # 密钥的字符集在保存时就拦下，别等到发请求时才炸出一个看不懂的错误。
    _check_key(candidate.get('LLM_API_KEY') or '')
    if _CONFIG_PATH is None:
        raise ModelError('当前运行方式不支持界面保存，请改 .env 后重启服务')
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = _CONFIG_PATH.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(current, stream, ensure_ascii=False, indent=2)
    temp.chmod(0o600)
    temp.replace(_CONFIG_PATH)
    _CACHE.update(mtime=None, data={})
    return public_config()


@synchronized
def clear_config():
    """清除界面保存的配置，回落到 .env / 环境变量。"""
    if _CONFIG_PATH is not None:
        try:
            _CONFIG_PATH.unlink()
        except OSError:
            pass
    _CACHE.update(mtime=None, data={})
    return public_config()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@dataclass
class Completion:
    data: dict
    usage: dict
    usage_reported: bool


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ModelError('模型 JSON 包含重复字段')
        result[key] = value
    return result


def chat(messages, model):
    return complete(messages, model).data


def complete(messages, model, temperature=0):
    if effective('LLM_ENABLED', 'true').lower() != 'true':
        raise ModelError('大模型已停用')
    endpoint, key, timeout, retries, tokens, extra, json_mode = settings()
    if not model or not model.strip():
        raise ModelError('未配置模型名称')
    payload = {'model': model, 'temperature': temperature, 'messages': messages, 'max_tokens': tokens, **extra}
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers=headers)
    opener = urllib.request.build_opener(NoRedirect)
    for attempt in range(retries + 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ModelError('模型响应超过大小限制')
            body = json.loads(raw)
            choice = body['choices'][0]
            if choice.get('finish_reason') == 'length':
                raise ModelError('模型输出被截断，请增加输出长度或拆分文档')
            if choice.get('finish_reason') != 'stop' or choice['message'].get('refusal'):
                raise ModelError('模型拒绝处理或未正常完成输出')
            content = choice['message']['content']
            if not isinstance(content, str):
                raise ModelError('模型未返回文本结果')
            content = content.strip()
            if content.startswith('```') and content.endswith('```'):
                content = content.split('\n', 1)[1].rsplit('```', 1)[0].strip()
            result = json.loads(content, object_pairs_hook=_strict_object)
            if not isinstance(result, dict):
                raise ModelError('模型结果必须为 JSON 对象')
            usage = body.get('usage') or {}
            reported = all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('prompt_tokens', 'completion_tokens'))
            return Completion(result, {'input_tokens': usage['prompt_tokens'] if reported else 0, 'output_tokens': usage['completion_tokens'] if reported else 0}, reported)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            transient = status in {429, 500, 502, 503, 504}
            message = {401: '模型鉴权失败，请核对密钥', 403: '模型访问被拒绝，请检查权限', 404: '模型或接口不存在，请核对地址和模型名', 400: '模型请求参数不兼容，请检查 JSON 模式和附加参数', 429: '模型限流或额度不足'}.get(status, '模型服务返回 HTTP ' + str(status))
            if not transient or attempt == retries:
                raise ModelError(message) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError):
            if attempt == retries:
                raise ModelError('模型连接失败或超时，请检查网络与服务地址') from None
        except UnicodeEncodeError:
            # 必须排在下面那条之前：它是 ValueError 的子类，否则会被吞成「JSON 无效」。
            raise ModelError('密钥含中文或全角字符，请重新复制粘贴') from None
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            if isinstance(exc, ModelError):
                raise
            raise ModelError('模型响应结构或 JSON 无效') from None
        time.sleep(min(2 ** attempt, 4))


def probe(vision=False):
    """Use only synthetic content; never upload existing documents in diagnostics."""
    started = time.monotonic()
    model = effective('VISION_MODEL' if vision else 'LLM_MODEL', '')
    prompt = 'Return a JSON object with exactly this key and value: {"ok":true}.'
    content = prompt
    if vision:
        import base64
        import io
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('RGB', (64, 64), 'white').save(buffer, format='PNG')
        content = [{'type': 'text', 'text': prompt}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()}}]
    out = chat([{'role': 'user', 'content': content}], model)
    if out.get('ok') is not True:
        raise ModelError('服务已响应，但未通过 JSON 指令验证')
    return {'ok': True, 'model': model, 'vision': vision, 'latency_ms': round((time.monotonic() - started) * 1000)}
