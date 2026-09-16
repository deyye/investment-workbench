"""数据维护：备份与清空的契约测试。

这是**破坏性**能力，所以测的重点不是"能不能清掉"，而是四件事：

1. 清空范围精确——不该动的表一行都不能少。`source_configs` 是**配置**不是数据，
   清掉就得把 34 个来源重新配一遍；`fetch_records` 是采集台账，重采时的处理队列就读它。
2. 清空前一定先备份，且备份**可读、内容与清空前一致**（WAL 下直接拷文件会丢数据）。
3. 口令里带条数：对不上就**拒绝**，而不是照旧范围多清（范围漂移守卫）。
4. 采集运行中拒绝——否则会把正在写入的记录清掉。
"""
import sqlite3
from pathlib import Path

import pytest

from policy_collector.config import AppConfig, SourceConfig
from policy_collector.db import Database
from policy_collector.pipeline import Pipeline
from policy_collector.webapp import create_app

URL = 'https://agency.gov.cn/policy/1.html'


@pytest.fixture
def pipe(tmp_path):
    cfg = AppConfig.load()
    cfg.data_dir = tmp_path
    cfg.db_path = tmp_path / 'policy.db'
    cfg.downloads_dir = tmp_path / 'raw'
    cfg.llm.enabled = False
    cfg.sources = {'test': SourceConfig(name='test', list_url='https://agency.gov.cn/policy/')}
    p = Pipeline(cfg)
    yield p
    p.close()


def seed(p) -> None:
    """灌入一次"采集 + 复核"留下的各类记录，覆盖所有会被清 / 不该被清的表。"""
    db = p.db
    db.upsert_source(name='test', site='测试站', region='浙江', list_url='https://agency.gov.cn/policy/')
    src = db.get_source('test')
    fid = db.add_fetch(src['id'], URL, title='投资项目支持办法')
    pid = db.insert_policy({'policy_key': 'k1', 'version': 1, 'title': '投资项目支持办法',
                            'content': '正文', 'region': '浙江', 'is_investment_policy': 'yes',
                            'category': 'guide', 'review_status': 'pending', 'todo_type': 'review'})
    db.add_attachment(pid, {'name': 'a.pdf', 'url': 'https://agency.gov.cn/a.pdf',
                            'parse_status': 'ok', 'parsed_text': '正文'})
    db.link_source(pid, fid, src['id'], URL)
    db.record_attachment_attempts(fid, [{'url': 'https://agency.gov.cn/a.pdf',
                                         'download_status': 'ok', 'parse_status': 'ok'}])
    db.start_run('run-1', src['id'])
    db.agent_event('run-1', fid, 'download', 'running', '正在获取原文')
    db.audit(pid, 'confirm', note='测试复核')
    db.finish_run('run-1', {'downloaded': 1}, status='ok')


def counts(db) -> dict:
    return db.storage_stats()['counts']


# ---------------- 数据层 ----------------

def test_clear_library_keeps_config_and_ledger(pipe):
    seed(pipe)
    before = counts(pipe.db)
    assert before['policies'] == 1 and before['source_configs'] >= 1

    result = pipe.db.clear('library')
    after = counts(pipe.db)

    for t in ('policies', 'attachments', 'policy_sources', 'review_events', 'attachment_attempts'):
        assert after[t] == 0, f'{t} 应被清空'
        assert result['deleted'][t] == before[t], f'{t} 的条数应与清空前一致'
    # 配置与采集台账必须原样保留
    assert after['source_configs'] == before['source_configs']
    assert after['fetch_records'] == before['fetch_records']
    assert after['discovery_observations'] == before['discovery_observations']
    # 运行日志不在本范围内
    assert after['run_logs'] == before['run_logs']


def test_clear_library_unlinks_fetch_records(pipe):
    """清掉政策后，台账里指向它的 policy_id 必须置空。

    留着悬空 id 会让 quality.py 的 JOIN 静默少行——正是本项目反复出现的"静默丢数据"。
    """
    seed(pipe)
    assert any(r['policy_id'] for r in pipe.db.list_fetch())
    pipe.db.clear('library')
    assert all(not r['policy_id'] for r in pipe.db.list_fetch())


def test_clear_logs_only_touches_logs(pipe):
    seed(pipe)
    before = counts(pipe.db)
    pipe.db.clear('logs')
    after = counts(pipe.db)
    assert after['run_logs'] == 0 and after['agent_events'] == 0
    assert after['policies'] == before['policies']
    assert after['attachments'] == before['attachments']
    assert after['source_configs'] == before['source_configs']


def test_clear_all(pipe):
    seed(pipe)
    before = counts(pipe.db)
    pipe.db.clear('all')
    after = counts(pipe.db)
    assert after['policies'] == 0 and after['run_logs'] == 0
    assert after['source_configs'] == before['source_configs']


def test_clear_rejects_unknown_scope(pipe):
    with pytest.raises(ValueError):
        pipe.db.clear('everything')


def test_clear_resets_autoincrement(pipe):
    """清空后编号从 1 重新开始（旧链接本就已失效，演示时编号也更好看）。"""
    seed(pipe)
    pipe.db.clear('library')
    assert pipe.db.insert_policy({'policy_key': 'k2', 'version': 1, 'title': 'X'}) == 1


def test_backup_is_readable_and_matches_pre_clear_state(pipe):
    seed(pipe)
    path = pipe.db.backup('test')
    assert Path(path).exists()
    con = sqlite3.connect(path)
    try:
        assert con.execute('SELECT COUNT(*) FROM policies').fetchone()[0] == 1
        assert con.execute('SELECT COUNT(*) FROM attachments').fetchone()[0] == 1
        assert con.execute('SELECT COUNT(*) FROM source_configs').fetchone()[0] >= 1
    finally:
        con.close()


def test_clear_leaves_data_dir_files_alone(pipe):
    """安全底线：同目录下的旁路文件（模型密钥、笔记等）不被波及。"""
    (pipe.cfg.data_dir / 'llm.local.json').write_text('{"api_key":"TEST-KEY"}', encoding='utf-8')
    (pipe.cfg.data_dir / 'notes.txt').write_text('keep me', encoding='utf-8')
    seed(pipe)
    pipe.db.clear('all')
    assert (pipe.cfg.data_dir / 'llm.local.json').read_text(encoding='utf-8') == '{"api_key":"TEST-KEY"}'
    assert (pipe.cfg.data_dir / 'notes.txt').read_text(encoding='utf-8') == 'keep me'


# ---------------- HTTP 层 ----------------

@pytest.fixture
def client(pipe):
    app = create_app(pipe.cfg)
    app.config.update(TESTING=True)
    c = app.test_client()
    with c.session_transaction() as s:
        s['csrf'] = 'tok'
    return c


def lib_total(db) -> int:
    n = counts(db)
    return sum(n.get(t, 0) for t in Database.CLEARABLE_TABLES['library'])


def test_maintenance_page_renders(client):
    html = client.get('/maintenance').get_data(as_text=True)
    assert '数据维护' in html
    assert '始终保留' in html
    assert 'source_configs' in html


def test_clear_endpoint_rejects_stale_token(client, pipe):
    """口令对不上（页面打开后又采了新数据）→ 拒绝，且一条都不清。"""
    seed(pipe)
    before = counts(pipe.db)
    html = client.post('/maintenance/clear',
                       data={'csrf_token': 'tok', 'scope': 'library', 'confirm': 'CLEAR-999999'},
                       follow_redirects=True).get_data(as_text=True)
    assert '确认口令已过期' in html
    assert counts(pipe.db)['policies'] == before['policies']


def test_clear_endpoint_works_and_backs_up(client, pipe):
    seed(pipe)
    html = client.post('/maintenance/clear',
                       data={'csrf_token': 'tok', 'scope': 'library',
                             'confirm': f'CLEAR-{lib_total(pipe.db)}'},
                       follow_redirects=True).get_data(as_text=True)
    assert '已清空政策库' in html
    assert counts(pipe.db)['policies'] == 0
    assert list((pipe.cfg.data_dir / 'backups').glob('policy-*.db')), '清空前必须留下备份'


def test_clear_endpoint_rejects_bad_scope(client, pipe):
    r = client.post('/maintenance/clear',
                    data={'csrf_token': 'tok', 'scope': 'drop-everything', 'confirm': 'CLEAR-1'})
    assert r.status_code == 400


def test_clear_endpoint_requires_csrf(client, pipe):
    seed(pipe)
    r = client.post('/maintenance/clear', data={'scope': 'library', 'confirm': 'CLEAR-1'})
    assert r.status_code == 400
    assert counts(pipe.db)['policies'] == 1


def test_clear_endpoint_refuses_while_collecting(client, pipe):
    """采集运行中必须拒绝，否则会把正在写入的记录清掉。"""
    from policy_collector import webapp as webapp_mod
    seed(pipe)
    webapp_mod._RUN_LOCK.acquire()
    try:
        html = client.post('/maintenance/clear',
                           data={'csrf_token': 'tok', 'scope': 'library',
                                 'confirm': f'CLEAR-{lib_total(pipe.db)}'},
                           follow_redirects=True).get_data(as_text=True)
        assert '采集任务正在运行' in html
        assert counts(pipe.db)['policies'] == 1
    finally:
        webapp_mod._RUN_LOCK.release()
