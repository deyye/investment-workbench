"""本机模型配置；只保存到被忽略的数据目录，不进入源码。

「模型配置」页与 CLI `configure-llm` 共用这一份实现：预设主流大模型服务、
保存密钥、启用/停用开关、连接检查。

两件事按实测写死在语义里，改之前先想清楚：

1. **密钥只落本机文件**（`<数据目录>/llm.local.json`，权限 0600），页面不回显。
2. **环境变量优先于本机文件**。所以「填了密钥」不等于「用的是这个密钥」——
   `describe_settings` 会把真实来源一并报出来，界面按它显示。
"""
import json
import os
from urllib.parse import urlsplit

# 主流大模型服务预设。选服务 = 自动带出地址、示例模型名与密钥环境变量名。
#
# ⚠️ 模型名是**通用默认值**，不是"某账号一定可用"的承诺：各家的模型清单随账号、
# 地域、开通时间变化。填错的表现是连接检查报「模型接口 HTTP 400/404」，
# 照报错改成账号实际可用的模型名即可。
#
# `local: True` 的本机服务不需要密钥（LLMClient 对本机地址放行）。
PROVIDERS = {
    'deepseek': {
        'label': 'DeepSeek（深度求索）',
        'base_url': 'https://api.deepseek.com/v1',
        'model': 'deepseek-chat',
        'api_key_env': 'DEEPSEEK_API_KEY',
        'note': '国内直连，性价比高；模型名可改为账号开通的其它版本',
    },
    'dashscope': {
        'label': '阿里云百炼（通义千问）',
        'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'model': 'qwen-plus',
        'api_key_env': 'DASHSCOPE_API_KEY',
        'note': '通义千问系列；该服务需关闭思考模式，已自动设置',
        'enable_thinking': False,
    },
    'moonshot': {
        'label': '月之暗面 Kimi',
        'base_url': 'https://api.moonshot.cn/v1',
        'model': 'moonshot-v1-8k',
        'api_key_env': 'MOONSHOT_API_KEY',
        'note': '长文本处理见长，适合整篇政策判定',
    },
    'zhipu': {
        'label': '智谱 AI（GLM）',
        'base_url': 'https://open.bigmodel.cn/api/paas/v4',
        'model': 'glm-4-plus',
        'api_key_env': 'ZHIPUAI_API_KEY',
        'note': 'GLM 系列',
    },
    'siliconflow': {
        'label': '硅基流动 SiliconFlow',
        'base_url': 'https://api.siliconflow.cn/v1',
        'model': 'Qwen/Qwen2.5-7B-Instruct',
        'api_key_env': 'SILICONFLOW_API_KEY',
        'note': '聚合多家开源模型',
    },
    'minimax': {
        'label': 'MiniMax',
        'base_url': 'https://api.minimax.chat/v1',
        'model': 'abab6.5s-chat',
        'api_key_env': 'MINIMAX_API_KEY',
        'note': '模型名请按账号实际开通的填写',
    },
    'ark': {
        'label': '火山方舟（豆包）',
        'base_url': 'https://ark.cn-beijing.volces.com/api/v3',
        'model': '',
        'api_key_env': 'ARK_API_KEY',
        'note': '模型名需填推理接入点 ID（形如 ep-2024xxxx），不是模型中文名',
    },
    'openai': {
        'label': 'OpenAI',
        'base_url': 'https://api.openai.com/v1',
        'model': 'gpt-4o-mini',
        'api_key_env': 'OPENAI_API_KEY',
        'note': '需可访问的网络环境',
    },
    'vllm': {
        'label': '本机 vLLM',
        'base_url': 'http://127.0.0.1:8000/v1',
        'model': '',
        'api_key_env': 'LLM_API_KEY',
        'note': '本机部署，无需密钥；模型名填启动 vLLM 时指定的名称',
        'local': True,
    },
    'ollama': {
        'label': '本机 Ollama',
        'base_url': 'http://127.0.0.1:11434/v1',
        'model': 'qwen2.5:14b',
        'api_key_env': 'LLM_API_KEY',
        'note': '本机部署，无需密钥',
        'local': True,
    },
    'custom': {
        'label': '其他兼容接口',
        'base_url': '',
        'model': '',
        'api_key_env': 'LLM_API_KEY',
        'note': '任何 OpenAI 兼容接口：填服务地址与模型名',
    },
}

# 下拉框顺序：常用国产服务在前，本机与自定义在后。
PROVIDER_ORDER = ['deepseek', 'dashscope', 'moonshot', 'zhipu', 'siliconflow',
                  'minimax', 'ark', 'openai', 'vllm', 'ollama', 'custom']

# 本机地址：不需要密钥，且允许 http。
LOCAL_HOSTS = ('localhost', '127.0.0.1', '::1')


def settings_path(cfg):
    return cfg.data_dir / 'llm.local.json'


def provider_options():
    """给前端下拉框用的有序列表。**不含密钥**，可安全进模板。"""
    return [{'name': name, 'label': PROVIDERS[name]['label'],
             'base_url': PROVIDERS[name]['base_url'], 'model': PROVIDERS[name]['model'],
             'api_key_env': PROVIDERS[name]['api_key_env'],
             'local': bool(PROVIDERS[name].get('local')), 'note': PROVIDERS[name]['note']}
            for name in PROVIDER_ORDER]


def detect_provider(base_url: str) -> str:
    """按地址反查服务名；认不出就归 custom。

    只在**展示**时用，不参与保存：地址被改过一点（换端口、换地域）时，
    硬套预设反而是错的。

    ⚠️ 本机服务必须连端口一起比：vLLM 与 Ollama 的 host 都是 `127.0.0.1`，
    只比主机名会把两者混成一个（实测踩过）。
    """
    parts = urlsplit(base_url or '')
    host, port = (parts.hostname or '').lower(), parts.port
    if not host:
        return 'custom'
    for name in PROVIDER_ORDER:
        preset = urlsplit(PROVIDERS[name]['base_url'])
        if (preset.hostname or '').lower() != host:
            continue
        if host in LOCAL_HOSTS and preset.port != port:
            continue
        return name
    return 'custom'


def _key_source(cfg) -> str:
    """当前**实际生效**的密钥来自哪里：shell 环境变量 > 界面配置 > 项目 `.env`。

    这不是装饰信息：界面上写「已保存」而实际用的是另一个密钥，是最难自查的一类问题。
    ⚠️ 顺序必须与 `LLMConfig.api_key` 完全一致，两边不一致会给出错误结论。
    """
    from .config import shell_env, dotenv_env
    for name, tag in (('LLM_API_KEY', 'env:LLM_API_KEY'),
                      (cfg.llm.api_key_env, f'env:{cfg.llm.api_key_env}')):
        if shell_env(name):
            return tag
    if (cfg.llm.local_api_key or '').strip():
        return 'file'
    for name in ('LLM_API_KEY', cfg.llm.api_key_env):
        if dotenv_env(name):
            return 'dotenv'
    return ''


def effective_api_key_env_name(cfg, provider: str) -> str:
    """密钥环境变量的**展示/快照**名称：跟随所选服务的预设，而不是 YAML 的默认值。

    背景：`config.yaml` 的默认是 `DASHSCOPE_API_KEY`，一旦服务改用 DeepSeek，
    这个默认值就与实际服务对不上——页面会显示成"服务是 DeepSeek、变量却是
    DASHSCOPE_API_KEY"。

    出现两处（页面展示、首次停用时的快照），故收敛到一处：同一件事写两遍，
    迟早两边不一致。若该变量名**确实有值**（正在被使用），则原样保留。
    """
    env_name = (cfg.llm.api_key_env or '').strip()
    if provider in PROVIDERS and not (os.environ.get(env_name) or '').strip():
        return PROVIDERS[provider]['api_key_env']
    return env_name


def describe_settings(cfg) -> dict:
    """当前模型配置的**如实**状态，供页面显示。"""
    path = settings_path(cfg)
    saved = {}
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            saved = {}
    source = _key_source(cfg)
    from .config import shell_env
    env_overrides = [k for k in ('LLM_BASE_URL', 'LLM_MODEL') if shell_env(k)]
    base_url = cfg.llm.base_url or ''
    local = (urlsplit(base_url).hostname or '') in LOCAL_HOSTS
    provider = saved.get('provider') or detect_provider(base_url)
    if provider not in PROVIDERS:
        provider = 'custom'
    # 「已启用」与「真能调用」是两件事：启用了但没密钥/没模型名，实际仍走本地规则。
    ready = bool(cfg.llm.enabled and cfg.llm.effective_model and (cfg.llm.api_key or local))
    return {
        'enabled': bool(cfg.llm.enabled),
        'ready': ready,
        'provider': provider,
        'provider_label': PROVIDERS[provider]['label'],
        'base_url': base_url,
        'model': cfg.llm.effective_model,
        # 未保存过配置时跟随所选服务的预设名，与首次停用的快照用同一判据
        'api_key_env': (cfg.llm.api_key_env if saved.get('api_key_env')
                        else effective_api_key_env_name(cfg, provider)),
        'has_key': bool(cfg.llm.api_key),
        'key_source': source,
        'key_source_label': ('本机配置文件' if source == 'file'
                             else '项目 .env 文件' if source == 'dotenv'
                             else f'环境变量 {source[4:]}' if source.startswith('env:') else ''),
        'local_service': local,
        'env_overrides': env_overrides,
        'file_exists': path.exists(),
    }


def load_model_settings(cfg):
    from .config import shell_env, dotenv_env
    path = settings_path(cfg)
    saved = path.exists()
    if saved:
        data = json.loads(path.read_text(encoding='utf-8'))
        for name in ('base_url', 'model', 'api_key_env', 'enabled', 'enable_thinking'):
            if name in data:
                setattr(cfg.llm, name, data[name])
        cfg.llm.local_api_key = str(data.get('api_key', ''))
    override = shell_env('LLM_BASE_URL')
    if override and override.rstrip('/') != cfg.llm.base_url.rstrip('/'):
        # 更换服务时不把旧服务的本机密钥、专用参数带过去。
        cfg.llm.local_api_key = ''
        cfg.llm.enable_thinking = None
    cfg.llm.base_url = override or cfg.llm.base_url
    if not saved:
        # 没有任何界面配置时才让 `.env` 兜底：否则用户在页面上改地址、改密钥
        # 都会被 `.env` 的旧值盖住，"前端配置"就只是摆设。
        cfg.llm.base_url = dotenv_env('LLM_BASE_URL') or cfg.llm.base_url
        cfg.llm.model = dotenv_env('LLM_MODEL') or cfg.llm.model
    return cfg


def _validate_base_url(base_url: str) -> str:
    base_url = (base_url or '').strip().rstrip('/')
    p = urlsplit(base_url)
    if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password or p.query or p.fragment or '{' in base_url:
        raise ValueError('请输入有效的模型 Base URL，不含密钥、占位符或查询参数')
    if p.scheme == 'http' and p.hostname not in LOCAL_HOSTS:
        raise ValueError('远程模型请使用 HTTPS；本机服务允许 HTTP')
    return base_url


def _write(cfg, data: dict):
    path = settings_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp, path)
    path.chmod(0o600)
    load_model_settings(cfg)
    return path


def save_model_settings(cfg, provider='dashscope', base_url='', model='', api_key='',
                        api_key_env='', enabled=None):
    if provider not in PROVIDERS:
        raise ValueError('未知模型服务类型')
    preset = PROVIDERS[provider]
    base_url = _validate_base_url(base_url or preset['base_url'])
    model = (model or preset['model']).strip()
    if not model:
        raise ValueError('必须填写模型名称')
    path = settings_path(cfg)
    previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if enabled is None:
        # 保存配置**不改变**启用状态：开关由「启用/停用」按钮单独控制。
        # 否则「先停用、再改模型名」会被保存动作悄悄重新启用。
        enabled = previous.get('enabled', True)
    if not api_key and previous.get('api_key'):
        if previous.get('base_url') != base_url:
            raise ValueError('更换服务地址时请重新填写密钥，避免向新服务发送旧密钥')
        api_key = previous['api_key']
    data = {'enabled': bool(enabled), 'provider': provider, 'base_url': base_url, 'model': model,
            'api_key_env': api_key_env or preset['api_key_env'], 'api_key': api_key,
            'enable_thinking': preset.get('enable_thinking')}
    return _write(cfg, data)


def set_enabled(cfg, enabled: bool):
    """只改启用开关，不动地址、模型与密钥——开关要能随手开合。

    ⚠️ 配置文件**不存在也要能停用**。只用 `.env` / YAML 配密钥、从没在界面保存过的
    用户（本机的现状就是如此）点"停用"时，原先会报"尚未配置模型服务"而失败——
    他明明正在用大模型，却被拒绝关闭。截图复核时抓到的就是这个。

    首次停用会把**当前实际生效**的地址与模型名一并快照落盘：否则下次加载时
    `saved=True` 会让 `.env` 不再兜底，地址与模型名凭空消失。
    密钥不快照（`local_api_key` 为空时留空），避免把 `.env` 的密钥复制一份到新文件。
    """
    path = settings_path(cfg)
    if path.exists():
        data = json.loads(path.read_text(encoding='utf-8'))
    else:
        provider = detect_provider(cfg.llm.base_url)
        data = {'provider': provider,
                'base_url': cfg.llm.base_url, 'model': cfg.llm.model,
                'api_key_env': effective_api_key_env_name(cfg, provider),
                'api_key': cfg.llm.local_api_key,
                'enable_thinking': cfg.llm.enable_thinking}
    data['enabled'] = bool(enabled)
    return _write(cfg, data)


def connection_check(cfg):
    from .llm_client import LLMClient
    client = LLMClient(cfg.llm)
    if not cfg.llm.enabled:
        return {'ok': False, 'error': '当前已停用大模型，采集将使用本地规则；如需检查请先启用',
                'model': cfg.llm.effective_model}
    if not client.available:
        return {'ok': False, 'error': '未配置实际 API Key 或模型名称；请在本机配置',
                'model': cfg.llm.effective_model}
    result = client.chat_json('只输出JSON。', '连接检查：请输出 {"ok":true}。')
    return {'ok': bool(result and result.get('ok') is True),
            'error': client.last_error or ('' if result and result.get('ok') is True else '模型未返回预期JSON'),
            'model': cfg.llm.effective_model, 'usage': client.usage}
