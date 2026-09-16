"""「模型配置」页：服务预设、启用开关、密钥来源与优先级。

这个功能最容易做假的地方是**开关与配置只落在界面上、没作用到采集**。
所以断言分三层：
  1. 数据层：预设清单、保存与开关的语义
  2. Web 层：页面不回显密钥、toggle 只改开关、保存不改开关
  3. 采集层：停用后请求"自动处理"会被强制回落为本地规则
"""
import json

import pytest

from policy_collector.config import AppConfig, LLMConfig
from policy_collector.model_settings import (PROVIDERS, PROVIDER_ORDER, describe_settings,
                                             detect_provider, provider_options,
                                             save_model_settings, set_enabled, settings_path)


@pytest.fixture
def cfg(tmp_path):
    c = AppConfig.load()
    c.data_dir = tmp_path
    c.downloads_dir = tmp_path / 'downloads'
    c.db_path = tmp_path / 'db.sqlite'
    return c


def _client(cfg):
    from policy_collector.webapp import create_app
    return create_app(cfg).test_client()


def _token(client):
    client.get('/settings/model')          # before_request 会在 session 里种下 csrf
    with client.session_transaction() as sess:
        return sess['csrf']


# ── 1. 服务预设 ─────────────────────────────────────────────

def test_preset_list_is_complete_and_valid():
    assert list(PROVIDERS) == PROVIDER_ORDER, '下拉顺序表与预设表必须一一对应'
    for name, p in PROVIDERS.items():
        assert p['label'] and p['note'], f'{name} 缺少中文名或说明'
        assert p['api_key_env'], f'{name} 缺少密钥环境变量名'
        if p['base_url']:                  # custom 留空由使用者填写
            assert p['base_url'].startswith(('https://', 'http://')), name
            if p['base_url'].startswith('http://'):
                assert p.get('local'), f'{name} 用 http 却不是本机服务'
        else:
            assert name == 'custom'
        # 模型名由使用者填写的那几家，说明里必须写清
        if not p['model']:
            assert p['note'], f'{name} 没有默认模型名，说明里必须写清要填什么'
    # 本机服务必须允许无密钥
    assert {n for n, p in PROVIDERS.items() if p.get('local')} == {'vllm', 'ollama'}


def test_provider_options_do_not_leak_secrets():
    opts = provider_options()
    assert len(opts) == len(PROVIDERS)
    assert all('api_key' not in o for o in opts), '预设里不得出现密钥字段'
    assert opts[0]['name'] == PROVIDER_ORDER[0]


@pytest.mark.parametrize('url,expect', [
    ('https://api.deepseek.com', 'deepseek'),
    ('https://api.deepseek.com/v1', 'deepseek'),          # 带不带 /v1 都认
    ('http://127.0.0.1:11434/v1', 'ollama'),
    ('https://api.moonshot.cn/v1', 'moonshot'),
    ('https://unknown-vendor.example/v1', 'custom'),
    ('', 'custom'),
])
def test_detect_provider_by_host(url, expect):
    assert detect_provider(url) == expect


# ── 2. 保存与开关的语义 ─────────────────────────────────────

def test_save_writes_private_file_and_keeps_enabled(cfg):
    path = save_model_settings(cfg, 'deepseek', 'https://api.deepseek.com/v1',
                               'deepseek-chat', 'secret-abc', '')
    assert path == settings_path(cfg) and path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600, '密钥文件必须是 0600'
    assert cfg.llm.api_key == 'secret-abc' and cfg.llm.enabled is True

    # 「先停用、再改配置」不能被保存动作悄悄重新启用
    set_enabled(cfg, False)
    save_model_settings(cfg, 'deepseek', 'https://api.deepseek.com/v1',
                        'deepseek-reasoner', '', '')
    assert cfg.llm.enabled is False, '保存配置不得改变启用状态'
    assert cfg.llm.effective_model == 'deepseek-reasoner'


def test_set_enabled_only_touches_the_flag(cfg):
    save_model_settings(cfg, 'moonshot', 'https://api.moonshot.cn/v1',
                        'moonshot-v1-8k', 'key-1', '')
    set_enabled(cfg, False)
    assert cfg.llm.enabled is False
    assert cfg.llm.base_url == 'https://api.moonshot.cn/v1'
    assert cfg.llm.effective_model == 'moonshot-v1-8k'
    assert cfg.llm.api_key == 'key-1'


def test_set_enabled_works_before_any_save(cfg):
    """只用 .env 配密钥、从没在界面保存过的用户，也必须能关闭大模型。

    实测踩过：配置文件不存在时原先直接抛错，他明明在用大模型却点不动"停用"。
    """
    cfg.llm.base_url = 'https://api.deepseek.com/v1'
    cfg.llm.model = 'deepseek-chat'
    assert not settings_path(cfg).exists()
    set_enabled(cfg, False)
    assert cfg.llm.enabled is False
    # 首次停用要把当前生效的地址与模型快照下来，否则下次加载会丢失
    assert cfg.llm.base_url == 'https://api.deepseek.com/v1'
    assert cfg.llm.model == 'deepseek-chat'
    saved = json.loads(settings_path(cfg).read_text(encoding='utf-8'))
    assert saved['enabled'] is False and saved['base_url'] == 'https://api.deepseek.com/v1'
    assert saved['api_key'] == '', '不该把 .env 里的密钥复制进新文件'
    # 变量名跟服务走，不能留下 YAML 的 DASHSCOPE_API_KEY（与实际服务对不上）
    assert saved['api_key_env'] == 'DEEPSEEK_API_KEY'
    assert saved['provider'] == 'deepseek'
    set_enabled(cfg, True)
    assert cfg.llm.enabled is True and cfg.llm.base_url == 'https://api.deepseek.com/v1'


def test_changing_host_requires_a_new_key(cfg):
    save_model_settings(cfg, 'deepseek', 'https://api.deepseek.com/v1', 'm', 'old-key', '')
    with pytest.raises(ValueError, match='重新填写'):
        save_model_settings(cfg, 'moonshot', 'https://api.moonshot.cn/v1', 'm2')
    with pytest.raises(ValueError, match='模型名称'):
        save_model_settings(cfg, 'ark', '', '', 'k', '')   # ark 无默认模型名
    with pytest.raises(ValueError, match='HTTPS'):
        save_model_settings(cfg, 'custom', 'http://remote.example/v1', 'm', 'k', '')


# ── 3. 密钥来源与三层优先级 ─────────────────────────────────

def test_three_layer_key_priority(monkeypatch):
    """shell 显式 export > 界面保存的本机配置 > 项目 .env。

    中间这层是本次新增的：不区分 `.env` 与 shell，界面上改密钥就永远无效。
    """
    from policy_collector import config as C
    had = set(C._DOTENV_KEYS)
    try:
        monkeypatch.setenv('LLM_API_KEY', 'env-value')
        monkeypatch.setenv('LLM_MODEL', 'env-model')
        llm = LLMConfig(model='file-model', local_api_key='file-key', api_key_env='DEEPSEEK_API_KEY')

        # ① 同样的两个键，若来自 .env：本机界面配置赢（本次新增的一层）
        C._DOTENV_KEYS.update({'LLM_API_KEY', 'LLM_MODEL'})
        assert llm.api_key == 'file-key'
        assert llm.effective_model == 'file-model'
        # 此时本机文件为空，则轮到 .env 兜底
        empty = LLMConfig(model='', local_api_key='', api_key_env='DEEPSEEK_API_KEY')
        assert empty.api_key == 'env-value'

        # ② 换成 shell 显式 export：最高优先，压过本机配置
        C._DOTENV_KEYS.difference_update({'LLM_API_KEY', 'LLM_MODEL'})
        assert llm.api_key == 'env-value', 'shell 显式 export 必须最高优先'
        assert llm.effective_model == 'env-model'
    finally:
        C._DOTENV_KEYS.clear()
        C._DOTENV_KEYS.update(had)


def test_describe_settings_reports_the_true_source(cfg, monkeypatch):
    from policy_collector import config as C
    had = set(C._DOTENV_KEYS)
    try:
        cfg.llm.local_api_key = ''
        monkeypatch.setenv('LLM_API_KEY', 'dotenv-value')
        C._DOTENV_KEYS.add('LLM_API_KEY')
        st = describe_settings(cfg)
        assert (st['key_source'], st['key_source_label']) == ('dotenv', '项目 .env 文件')

        C._DOTENV_KEYS.discard('LLM_API_KEY')                 # 同一变量改为 shell 显式
        st = describe_settings(cfg)
        assert st['key_source'] == 'env:LLM_API_KEY'
        assert st['env_overrides'] == []                      # LLM_MODEL 未设，不算覆盖

        cfg.llm.local_api_key = 'file-key'
        C._DOTENV_KEYS.add('LLM_API_KEY')
        assert describe_settings(cfg)['key_source'] == 'file'
    finally:
        C._DOTENV_KEYS.clear()
        C._DOTENV_KEYS.update(had)


def test_ready_reflects_whether_the_model_can_actually_be_called(cfg):
    """「已启用」与「真能调用」是两件事，页面据此给出不同提示。"""
    cfg.llm.enabled = True
    cfg.llm.model = 'm'
    cfg.llm.local_api_key = ''
    assert describe_settings(cfg)['ready'] is False, '启用了但没密钥 → 实际会回落规则'
    cfg.llm.local_api_key = 'k'
    assert describe_settings(cfg)['ready'] is True
    cfg.llm.enabled = False
    assert describe_settings(cfg)['ready'] is False


def test_connection_check_refusal_when_disabled(cfg):
    from policy_collector.model_settings import connection_check
    save_model_settings(cfg, 'deepseek', 'https://api.deepseek.com/v1', 'm', 'k', '')
    set_enabled(cfg, False)
    out = connection_check(cfg)
    assert out['ok'] is False and '停用' in out['error'], '停用时不该真的去调模型'


# ── 4. Web 层 ──────────────────────────────────────────────

def test_page_renders_presets_and_never_echoes_the_key(cfg):
    save_model_settings(cfg, 'deepseek', 'https://api.deepseek.com/v1',
                        'deepseek-chat', 'super-secret-key', '')
    html = _client(cfg).get('/settings/model').get_data(as_text=True)
    assert 'super-secret-key' not in html
    assert '大模型已启用' in html and '停用大模型' in html
    for p in PROVIDERS.values():
        assert p['label'] in html
    assert '保存不会改变启用状态' in html


def test_toggle_endpoint_flips_state_without_losing_config(cfg):
    save_model_settings(cfg, 'deepseek', 'https://api.deepseek.com/v1', 'deepseek-chat', 'k-1', '')
    client = _client(cfg)
    token = _token(client)

    r = client.post('/settings/model', data={'csrf_token': token, 'action': 'toggle', 'enabled': '0'},
                    follow_redirects=True)
    assert r.status_code == 200
    assert cfg.llm.enabled is False
    assert cfg.llm.api_key == 'k-1', '切换开关不能丢密钥'
    assert '大模型未启用' in r.get_data(as_text=True)

    r = client.post('/settings/model', data={'csrf_token': token, 'action': 'toggle', 'enabled': '1'},
                    follow_redirects=True)
    assert cfg.llm.enabled is True and '大模型已启用' in r.get_data(as_text=True)


def test_saving_from_the_page_keeps_disabled_state(cfg):
    save_model_settings(cfg, 'deepseek', 'https://api.deepseek.com/v1', 'deepseek-chat', 'k-1', '')
    set_enabled(cfg, False)
    client = _client(cfg)
    r = client.post('/settings/model', data={'csrf_token': _token(client), 'action': 'save',
                                             'provider': 'moonshot',
                                             'base_url': 'https://api.moonshot.cn/v1',
                                             'model': 'moonshot-v1-8k', 'api_key': 'k-2',
                                             'api_key_env': ''}, follow_redirects=True)
    assert r.status_code == 200
    assert cfg.llm.enabled is False, '保存表单不该顺手把停用状态改回来'
    assert cfg.llm.api_key == 'k-2' and cfg.llm.effective_model == 'moonshot-v1-8k'


# ── 5. 采集层：开关必须真的生效 ─────────────────────────────

def test_disabled_model_forces_local_rules_in_collection(cfg):
    from policy_collector.webapp import _form_prefer, create_app
    app = create_app(cfg)

    cfg.llm.enabled = False
    with app.test_request_context('/sources/x/run', method='POST', data={'prefer': 'llm'}):
        assert _form_prefer(cfg) == 'rule', '停用大模型后，请求"自动处理"必须回落本地规则'

    cfg.llm.enabled = True
    with app.test_request_context('/sources/x/run', method='POST', data={'prefer': 'llm'}):
        assert _form_prefer(cfg) == 'llm'
    with app.test_request_context('/sources/x/run', method='POST', data={'prefer': 'rule'}):
        assert _form_prefer(cfg) == 'rule'

    from werkzeug.exceptions import BadRequest
    with app.test_request_context('/sources/x/run', method='POST', data={'prefer': 'anything'}):
        with pytest.raises(BadRequest):
            _form_prefer(cfg)


def test_sources_page_hides_auto_option_when_disabled(cfg):
    # cfg.sources 来自 config/sources.yaml（有多台已启用来源），create_app 会同步进库，
    # 因此页面上确实会渲染出采集分类下拉框——断言才有意义。
    cfg.llm.enabled = False
    html = _client(cfg).get('/sources').get_data(as_text=True)
    assert '大模型已停用' in html
    assert '自动处理（推荐）' not in html, '停用后不该再给出会调用模型的选项'

    cfg.llm.enabled = True
    cfg.llm.local_api_key = 'k'
    cfg.llm.model = 'm'
    html = _client(cfg).get('/sources').get_data(as_text=True)
    assert '自动处理（推荐）' in html
