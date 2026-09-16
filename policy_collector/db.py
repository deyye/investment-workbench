"""数据落地层：SQLite 五类记录。

表结构（与任务口径一致）：
    source_configs  来源配置（站点/栏目/规则/最近检查时间）
    fetch_records   采集记录（页面URL/原始文件位置/状态/错误/哈希）
    policies        政策及版本（标题/文号/机关/日期/分类/审核状态/判断理由）
    attachments     附件记录（所属政策/URL/保存位置/校验值/解析状态）
    run_logs        运行与审核记录（批次/模型版本/人工纠正/耗时）

数据库访问全部经本层集中封装，便于后续换 PostgreSQL 等目标库。
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .models import now
import threading

# 政策库列表的**可排序列白名单**：列表页的表头 key -> 真实 SQL 表达式。
# 只在这里定义一次，排序、表头渲染、默认方向都从它派生，
# 避免"表头加了列、排序还是老的"这种两边不同步。
POLICY_SORTABLE = {
    "id":       "p.id",
    "date":     "p.page_date",
    "title":    "p.title",
    "wenhao":   "p.wenhao",
    "category": "p.category_names",
    "region":   "p.region",
    "todo":     "p.todo_type",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_configs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT UNIQUE NOT NULL,
    site          TEXT DEFAULT '',
    region        TEXT DEFAULT '',
    category      TEXT DEFAULT '',
    enabled       INTEGER DEFAULT 1,
    list_url      TEXT DEFAULT '',
    link_selector TEXT DEFAULT '',
    include       TEXT DEFAULT '[]',      -- JSON list
    exclude       TEXT DEFAULT '[]',      -- JSON list
    max_pages     INTEGER DEFAULT 3,
    last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS fetch_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER,
    page_url      TEXT,
    title         TEXT DEFAULT '',
    status        TEXT DEFAULT 'discovered', -- discovered/downloaded/failed/excluded/processed
    error         TEXT DEFAULT '',
    content_sha256 TEXT DEFAULT '',
    raw_path      TEXT DEFAULT '',
    discovered_at TEXT,
    downloaded_at TEXT,
    processed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_url ON fetch_records(page_url);
CREATE INDEX IF NOT EXISTS idx_fetch_src ON fetch_records(source_id);

CREATE TABLE IF NOT EXISTS policies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_key    TEXT NOT NULL,           -- 归一化标识（文号或标题指纹）
    version       INTEGER DEFAULT 1,
    title         TEXT DEFAULT '',
    wenhao        TEXT DEFAULT '',
    issuing_authority TEXT DEFAULT '',
    page_date     TEXT DEFAULT '',
    doc_date      TEXT DEFAULT '',
    region        TEXT DEFAULT '',
    site          TEXT DEFAULT '',
    page_url      TEXT DEFAULT '',
    doc_type      TEXT DEFAULT '其他',
    category      TEXT DEFAULT '',          -- guide/access/guarantee/incentive（,分隔）
    category_names TEXT DEFAULT '',
    is_investment_policy TEXT DEFAULT 'pending',
    need_review   INTEGER DEFAULT 0,
    reason        TEXT DEFAULT '',
    evidence      TEXT DEFAULT '',
    confidence    REAL,
    model_version TEXT DEFAULT '',
    review_status TEXT DEFAULT 'pending',   -- pending/confirmed/adjusted/rejected
    content       TEXT DEFAULT '',
    content_sha256 TEXT DEFAULT '',
    source_fetch_id INTEGER,
    created_at    TEXT,
    updated_at    TEXT,
    UNIQUE(policy_key, version)
);
CREATE INDEX IF NOT EXISTS idx_policy_key ON policies(policy_key);

CREATE TABLE IF NOT EXISTS attachments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id     INTEGER NOT NULL,
    name          TEXT DEFAULT '',
    url           TEXT DEFAULT '',
    local_path    TEXT DEFAULT '',
    fmt           TEXT DEFAULT '',
    sha256        TEXT DEFAULT '',
    parse_status  TEXT DEFAULT 'not_parsed',
    parsed_text   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_att_policy ON attachments(policy_id);

CREATE TABLE IF NOT EXISTS run_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    source_id     INTEGER,
    kind          TEXT DEFAULT 'manual',    -- manual/scheduled
    status        TEXT DEFAULT 'ok',        -- ok/partial/failed
    summary       TEXT DEFAULT '{}',        -- JSON: discovered/downloaded/ingested/dup/failed/review
    model_version TEXT DEFAULT '',
    started_at    TEXT,
    finished_at   TEXT,
    note          TEXT DEFAULT ''           -- 人工纠正等记录
);
CREATE INDEX IF NOT EXISTS idx_run_id ON run_logs(run_id);
"""


class Database:
    """SQLite 封装：连接管理 + 基础 DAO。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    # ---------- 来源 ----------
    def upsert_source(self, name: str, **fields: Any) -> int:
        with self.tx() as cur:
            cur.execute("SELECT id FROM source_configs WHERE name=?", (name,))
            row = cur.fetchone()
            if row:
                sets = ", ".join(f"{k}=?" for k in fields)
                cur.execute(f"UPDATE source_configs SET {sets} WHERE id=?", (*fields.values(), row["id"]))
                return int(row["id"])
            cols = ",".join(["name"] + list(fields.keys()))
            marks = ",".join(["?"] * (len(fields) + 1))
            cur.execute(f"INSERT INTO source_configs ({cols}) VALUES ({marks})", (name, *fields.values()))
            return int(cur.lastrowid)

    def list_sources(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM source_configs"
        if enabled_only:
            sql += " WHERE enabled=1"
        with self._conn:
            return [dict(r) for r in self._conn.execute(sql + " ORDER BY id")]

    def get_source(self, name: str) -> Optional[dict]:
        with self._conn:
            r = self._conn.execute("SELECT * FROM source_configs WHERE name=?", (name,)).fetchone()
            return dict(r) if r else None

    def get_source_by_id(self, sid: int) -> Optional[dict]:
        with self._conn:
            r = self._conn.execute("SELECT * FROM source_configs WHERE id=?", (sid,)).fetchone()
            return dict(r) if r else None

    def touch_source(self, sid: int) -> None:
        with self.tx() as cur:
            cur.execute("UPDATE source_configs SET last_checked_at=? WHERE id=?", (now(), sid))

    # ---------- 采集记录 ----------
    def add_fetch(self, source_id: int, page_url: str, status: str = "discovered",
                  title: str = "", error: str = "", sha256: str = "", raw_path: str = "") -> int:
        with self.tx() as cur:
            existing = cur.execute("SELECT id FROM fetch_records WHERE source_id=? AND page_url=?", (source_id,page_url)).fetchone()
            if existing:
                return int(existing["id"])
            cur.execute(
                "INSERT INTO fetch_records(source_id,page_url,status,title,error,content_sha256,raw_path,discovered_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (source_id, page_url, status, title, error, sha256, raw_path, now()),
            )
            return int(cur.lastrowid)

    def update_fetch(self, fetch_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as cur:
            cur.execute(f"UPDATE fetch_records SET {sets} WHERE id=?", (*fields.values(), fetch_id))

    def list_fetch(self, source_id: Optional[int] = None, status: Optional[str] = None,
                   limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM fetch_records WHERE 1=1", []
        if source_id:
            sql += " AND source_id=?"
            args.append(source_id)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn:
            return [dict(r) for r in self._conn.execute(sql, args)]

    # ---------- 政策及版本 ----------
    def find_policy(self, policy_key: str) -> Optional[dict]:
        with self._conn:
            r = self._conn.execute(
                "SELECT * FROM policies WHERE policy_key=? ORDER BY version DESC LIMIT 1", (policy_key,)
            ).fetchone()
            return dict(r) if r else None

    def insert_policy(self, p: dict) -> int:
        cols = list(p.keys())
        marks = ",".join(["?"] * len(cols))
        with self.tx() as cur:
            cur.execute(
                f"INSERT INTO policies ({','.join(cols)}) VALUES ({marks})", [p[c] for c in cols]
            )
            return int(cur.lastrowid)

    def add_policy_version(self, policy_key: str, data: dict) -> tuple[int, int]:
        """同一 policy_key 已有记录则 version+1 追加新版本，返回 (policy_id, version)。"""
        prev = self.find_policy(policy_key)
        version = (prev["version"] + 1) if prev else 1
        data["policy_key"] = policy_key
        data["version"] = version
        data["created_at"] = now()
        data["updated_at"] = now()
        pid = self.insert_policy(data)
        return pid, version

    def update_policy(self, pid: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as cur:
            cur.execute(f"UPDATE policies SET {sets} WHERE id=?", (*fields.values(), pid))

    def get_policy(self, pid: int) -> Optional[dict]:
        with self._conn:
            r = self._conn.execute("SELECT * FROM policies WHERE id=?", (pid,)).fetchone()
            return dict(r) if r else None

    @staticmethod
    def _policy_filters(region: str = "", category: str = "", keyword: str = "",
                        review_status: str = "", todo: str = "") -> tuple[str, list]:
        """政策库的**唯一**筛选口径，返回 (WHERE 片段, 参数)。

        为什么单独抽出来：列表页要同时给出「当前页数据」和「符合条件的总数」，
        两者必须用同一套条件，否则会出现"共 137 条，翻到第 7 页是空的"这类
        自相矛盾的界面。这类"同一判据写两遍、然后慢慢分叉"的坑，
        在本项目里已经因为别的原因踩过好几次，所以这里只留一处定义。
        """
        sql = " WHERE version=(SELECT MAX(version) FROM policies v WHERE v.policy_key=p.policy_key)"
        args: list = []
        if todo:
            # 待办类型由**分类环节派生后落库**（policies.todo_type），筛选直接读列。
            # 早期实现是用 CASE 表达式临时推导（两条并行分支各写一套），
            # 合并后统一为"存一次、各处读"，避免派生口径与统计口径漂移。
            sql += " AND todo_type=?"
            args.append(todo)
        elif not review_status:
            sql += " AND review_status != 'rejected'"
        if region:
            sql += " AND region=?"
            args.append(region)
        if category:
            sql += " AND (category LIKE ? OR category_names LIKE ?)"
            args += [f"%{category}%", f"%{category}%"]
        if keyword:
            sql += " AND (title LIKE ? OR content LIKE ? OR wenhao LIKE ? OR EXISTS (SELECT 1 FROM attachments a WHERE a.policy_id=p.id AND a.parsed_text LIKE ?))"
            args += [f"%{keyword}%"] * 4
        if review_status == 'pending':
            sql += " AND (review_status='pending' OR parse_requires_review=1)"
        elif review_status:
            sql += " AND review_status=?"
            args.append(review_status)
        return sql, args

    @staticmethod
    def policy_order_by(sort: str = "", order: str = "") -> str:
        """把列表页的排序参数翻成 ORDER BY，**只认白名单**。

        两点是刻意的：

        1) **不拼接用户输入**。排序列名走 `POLICY_SORTABLE` 映射，参数认不出就用默认，
           既避免注入，也避免一个手改错的 URL 让整页 500。
        2) **日期为空的行恒排最后**。SQLite 里 NULL（以及我们写入的空串）
           在升序时排最前，于是"按日期升序"的第一页会是一堆没有日期的记录——
           那恰恰是最没用的一屏。加上 `IS NULL OR =''` 这一级排序键后，
           无论升序降序，没有日期的都沉到最后。
        3) 末尾固定追加 `p.id DESC` 作**稳定分页**的兜底键。否则同一天发布的
           几十份文件在翻页时顺序不稳定，会出现"某条在第 2 页看过、第 3 页又出现"。
        """
        expr = POLICY_SORTABLE.get(sort, POLICY_SORTABLE['id'])
        direction = 'ASC' if str(order).lower() == 'asc' else 'DESC'
        parts = []
        if sort and sort != 'id':
            parts.append(f"({expr} IS NULL OR {expr}='') ASC")
        parts.append(f"{expr} {direction}")
        if expr != POLICY_SORTABLE['id']:
            parts.append("p.id DESC")
        return " ORDER BY " + ", ".join(parts)

    def count_policies(self, region: str = "", category: str = "", keyword: str = "",
                       review_status: str = "", todo: str = "") -> int:
        sql, args = self._policy_filters(region, category, keyword, review_status, todo)
        with self._conn:
            return int(self._conn.execute("SELECT COUNT(*) FROM policies p" + sql, args).fetchone()[0])

    def query_policies(self, region: str = "", category: str = "", keyword: str = "",
                       review_status: str = "", todo: str = "", limit: int = 100,
                       offset: int = 0, sort: str = "", order: str = "") -> list[dict]:
        sql, args = self._policy_filters(region, category, keyword, review_status, todo)
        sql = ("SELECT p.* FROM policies p" + sql + self.policy_order_by(sort, order)
               + " LIMIT ? OFFSET ?")
        args = [*args, limit, offset]
        with self._conn:
            rows = [dict(r) for r in self._conn.execute(sql, args)]
            if keyword and rows:
                by_id = {r['id']: r for r in rows}
                marks = ','.join('?' for _ in rows)
                matches = self._conn.execute(
                    f"SELECT policy_id,name,parsed_text FROM attachments WHERE policy_id IN ({marks}) AND parsed_text LIKE ? ORDER BY id",
                    [*by_id, f'%{keyword}%'])
                for match in matches:
                    row = by_id[match['policy_id']]
                    if 'matched_attachment' in row:
                        continue
                    text = match['parsed_text']
                    position = max(0, text.lower().find(keyword.lower()))
                    row['matched_attachment'] = match['name']
                    row['attachment_excerpt'] = text[max(0, position-30):position+len(keyword)+70]
            return rows

    # ---------- 附件 ----------
    def add_attachment(self, policy_id: int, att: dict) -> int:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO attachments(policy_id,name,url,local_path,fmt,sha256,parse_status,parsed_text,error)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (policy_id, att.get("name", ""), att.get("url", ""), att.get("local_path", ""),
                 att.get("fmt", ""), att.get("sha256", ""), att.get("parse_status", "not_parsed"),
                 att.get("parsed_text", ""), att.get("error", "")),
            )
            return int(cur.lastrowid)

    def list_attachments(self, policy_id: int) -> list[dict]:
        with self._conn:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM attachments WHERE policy_id=? ORDER BY id", (policy_id,))]

    # ---------- 运行日志 ----------
    def start_run(self, run_id: str, source_id: Optional[int], kind: str = "manual") -> int:
        with self.tx() as cur:
            cur.execute("INSERT INTO run_logs(run_id,source_id,kind,status,started_at,summary)"
                        " VALUES(?,?,?,?,?,?)", (run_id, source_id, kind, "running", now(), "{}"))
            return int(cur.lastrowid)

    def finish_run(self, run_id: str, summary: dict, status: str = "ok",
                   model_version: str = "", note: str = "") -> None:
        with self.tx() as cur:
            cur.execute(
                "UPDATE run_logs SET status=?,summary=?,finished_at=?,model_version=?,note=? WHERE run_id=?",
                (status, json.dumps(summary, ensure_ascii=False), now(), model_version, note, run_id),
            )

    def list_runs(self, limit: int = 20, offset: int = 0) -> list[dict]:
        with self._conn:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM run_logs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))]

    def count_runs(self, status: str = "") -> int:
        """运行记录总数（可按状态过滤）。

        有了它，处理进度页才能给出"第 x/y 页"并允许跳页——此前固定只取最近 50 条，
        更早的批次在界面上等于不存在（一次全国采集会留下 37 条记录，很快就超）。
        状态过滤则用于"整库是否还有任务在跑"，避免只看当前页导致自动刷新停掉。
        """
        sql, args = "SELECT COUNT(*) FROM run_logs", []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        with self._conn:
            return int(self._conn.execute(sql, args).fetchone()[0])

    def agent_event(self, run_id, fetch_id, action, status, message):
        with self.tx() as cur:
            cur.execute('INSERT INTO agent_events(run_id,fetch_id,action,status,message,created_at) VALUES(?,?,?,?,?,?)',
                        (run_id,fetch_id,action,status,message,now()))

    def run_progress(self, run_id, **fields):
        if not run_id: return
        with self.tx() as cur:
            row=cur.execute('SELECT progress FROM run_logs WHERE run_id=?',(run_id,)).fetchone()
            data=json.loads(row['progress'] or '{}') if row else {}
            data.update(fields)
            cur.execute('UPDATE run_logs SET progress=? WHERE run_id=?',(json.dumps(data,ensure_ascii=False),run_id))

    def run_events(self, run_id, limit=100):
        return [dict(r) for r in self._conn.execute('SELECT * FROM agent_events WHERE run_id=? ORDER BY id DESC LIMIT ?', (run_id,limit))][::-1]

    def policy_agent_events(self, pid):
        return [dict(r) for r in self._conn.execute("""SELECT * FROM agent_events WHERE fetch_id IN
            (SELECT fetch_id FROM policy_sources WHERE policy_id=?) ORDER BY id DESC LIMIT 30""",(pid,))][::-1]

    def get_run_summary(self, run_id: str) -> Optional[dict]:
        with self._conn:
            r = self._conn.execute("SELECT * FROM run_logs WHERE run_id=?", (run_id,)).fetchone()
            return dict(r) if r else None

    def _migrate(self):
        self._conn.executescript("""CREATE TABLE IF NOT EXISTS agent_events (
            id INTEGER PRIMARY KEY, run_id TEXT NOT NULL DEFAULT '', fetch_id INTEGER,
            action TEXT, status TEXT, message TEXT, created_at TEXT);
            CREATE INDEX IF NOT EXISTS idx_agent_run ON agent_events(run_id,id);
            CREATE INDEX IF NOT EXISTS idx_agent_fetch ON agent_events(fetch_id,id);""")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_attachment_policy ON attachments(policy_id)")
        additions = {
            "run_logs": {"progress":"TEXT DEFAULT '{}'"},
            "source_configs": {"last_success_at": "TEXT", "last_error": "TEXT DEFAULT ''"},
            "fetch_records": {"last_checked_at": "TEXT", "policy_id": "INTEGER", "classification_json": "TEXT DEFAULT ''", "document_json":"TEXT DEFAULT ''"},
            "policies": {"reviewer_hint": "TEXT DEFAULT ''", "classification_method": "TEXT DEFAULT ''",
                         "fallback_reason": "TEXT DEFAULT ''", "input_truncated": "INTEGER DEFAULT 0",
                         "input_tokens": "INTEGER DEFAULT 0", "output_tokens": "INTEGER DEFAULT 0",
                         "related_policy_key": "TEXT DEFAULT ''", "raw_page_sha256":"TEXT DEFAULT ''",
                         "analysis_sha256":"TEXT DEFAULT ''", "parse_error":"TEXT DEFAULT ''",
                         "parse_requires_review":"INTEGER DEFAULT 0",
                         "todo_type":"TEXT DEFAULT 'none'"},
            "attachments": {"error": "TEXT DEFAULT ''", "parser_version":"TEXT DEFAULT ''",
                "parse_method":"TEXT DEFAULT ''", "total_pages":"INTEGER DEFAULT 0", "parsed_pages":"INTEGER DEFAULT 0"},
        }
        for table, fields in additions.items():
            existing = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, declaration in fields.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS attachment_attempts (
                id INTEGER PRIMARY KEY, fetch_id INTEGER, url TEXT, fmt TEXT, download_status TEXT,
                parse_status TEXT, error TEXT, checked_at TEXT);
            CREATE INDEX IF NOT EXISTS idx_attachment_attempts ON attachment_attempts(fetch_id,url,id);
            CREATE TABLE IF NOT EXISTS discovery_observations (
                id INTEGER PRIMARY KEY, source_id INTEGER, page_url TEXT, title TEXT,
                admitted INTEGER, reason TEXT, observed_at TEXT, UNIQUE(source_id,page_url));
            CREATE TABLE IF NOT EXISTS policy_sources (
                id INTEGER PRIMARY KEY, policy_id INTEGER NOT NULL, fetch_id INTEGER,
                source_id INTEGER, page_url TEXT NOT NULL, last_seen_at TEXT,
                UNIQUE(policy_id, source_id, page_url));
            CREATE TABLE IF NOT EXISTS review_events (
                id INTEGER PRIMARY KEY, policy_id INTEGER NOT NULL, action TEXT,
                before_json TEXT, after_json TEXT, note TEXT, created_at TEXT);
        """)
        self._conn.execute("""INSERT OR IGNORE INTO policy_sources
            (policy_id,fetch_id,source_id,page_url,last_seen_at)
            SELECT p.id,p.source_fetch_id,f.source_id,p.page_url,p.created_at
            FROM policies p JOIN fetch_records f ON f.id=p.source_fetch_id""")

    def store_document(self, key, fields, attachments, fid, sid, url):
        """Policy, attachments and provenance commit together or roll back together."""
        with self.tx() as c:
            c.execute("BEGIN IMMEDIATE")
            prev=c.execute("SELECT MAX(version) FROM policies WHERE policy_key=?",(key,)).fetchone()[0]
            version=(prev or 0)+1
            data={**fields,"policy_key":key,"version":version,"created_at":now(),"updated_at":now()}
            cols=list(data)
            c.execute(f"INSERT INTO policies ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",[data[k] for k in cols])
            pid=c.lastrowid
            for a in attachments:
                keys=('name','url','local_path','fmt','sha256','parse_status','parsed_text','error','parser_version','parse_method','total_pages','parsed_pages')
                c.execute("INSERT INTO attachments(policy_id,"+','.join(keys)+") VALUES("+','.join('?' for _ in range(len(keys)+1))+")",
                          [pid]+[a.get(k,0 if k.endswith('pages') else '') for k in keys])
            c.execute("INSERT INTO policy_sources(policy_id,fetch_id,source_id,page_url,last_seen_at) VALUES(?,?,?,?,?)",
                      (pid,fid,sid,url,now()))
            c.execute("UPDATE fetch_records SET policy_id=? WHERE id=?",(pid,fid))
            return pid,version

    def get_fetch(self, source_id: int, url: str):
        r = self._conn.execute("SELECT * FROM fetch_records WHERE source_id=? AND page_url=? ORDER BY id LIMIT 1",
                               (source_id,url)).fetchone()
        return dict(r) if r else None

    def link_source(self, pid, fid, sid, url):
        with self.tx() as c:
            c.execute("""INSERT INTO policy_sources(policy_id,fetch_id,source_id,page_url,last_seen_at)
                VALUES(?,?,?,?,?) ON CONFLICT(policy_id,source_id,page_url)
                DO UPDATE SET last_seen_at=excluded.last_seen_at,fetch_id=excluded.fetch_id""",
                (pid,fid,sid,url,now()))
            c.execute("UPDATE fetch_records SET policy_id=? WHERE id=?", (pid,fid))

    def policy_for_url(self, url):
        r = self._conn.execute("""SELECT p.* FROM policies p JOIN policy_sources s ON s.policy_id=p.id
            WHERE s.page_url=? ORDER BY p.id DESC LIMIT 1""", (url,)).fetchone()
        return dict(r) if r else None

    def matching_version(self, key, digest):
        r = self._conn.execute("SELECT * FROM policies WHERE policy_key=? AND content_sha256=? ORDER BY version DESC LIMIT 1",
                               (key,digest)).fetchone()
        return dict(r) if r else None

    def versions(self, key):
        return [dict(r) for r in self._conn.execute("SELECT * FROM policies WHERE policy_key=? ORDER BY version DESC", (key,))]

    def policy_sources(self, key):
        return [dict(r) for r in self._conn.execute("""SELECT DISTINCT s.page_url,c.site,c.region,s.last_seen_at
            FROM policy_sources s JOIN policies p ON p.id=s.policy_id
            LEFT JOIN source_configs c ON c.id=s.source_id WHERE p.policy_key=?""", (key,))]

    def get_attachment(self, aid):
        r=self._conn.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
        return dict(r) if r else None

    def material_gaps(self, pid):
        """材料是否存在**解析缺口**——用于 `parse_requires_review`（详情页"材料待核对"提示）。

        ⚠️ **这不是"待办类型 material"的判据，别混用。** 两者是不同的问题：

        | | 问的是 | 判据 | 用途 |
        |---|---|---|---|
        | 本方法 `material_gaps` | 材料**有没有解析缺口**（含 `partial`：正文已提取、个别页是图形/模板） | 要求 `parse_status` 恰好为 `ok` | `parse_requires_review`、详情页警告 |
        | `todo.document_incomplete` | 材料**有没有拿到正文** | 正文为空才算缺 | `todo_type='material'`（机器自修） |

        区别的后果很实际：实测 116 条同时发了 PDF 与 OFD 两个版本，
        PDF 解析 `ok`、OFD `partial`——**正文其实拿到了两遍**。
        按本方法判会出现"材料没齐"，按 `document_incomplete` 判则是"材料是好的"。
        所以**待办派生必须用后者**（`partial` 这类缺口机器重试修不掉，
        归"结论待确认"由人判断，不该记到机器账上）。

        方法名原先叫 `material_pending`，与"待办里的材料待补"极易混淆
        （合并时就因此被误用进了 `audit()`），故改名。
        """
        row = self.get_policy(pid)
        if row and row.get('parse_error'):
            return True
        from .quality import attachment_quality
        if any(not attachment_quality(a)['parse_complete'] for a in self.list_attachments(pid)):
            return True
        return bool(self._conn.execute("""SELECT 1 FROM attachment_attempts t
            WHERE t.fetch_id IN (SELECT fetch_id FROM policy_sources WHERE policy_id=?)
            AND t.id=(SELECT MAX(t2.id) FROM attachment_attempts t2
                      WHERE t2.fetch_id=t.fetch_id AND t2.url=t.url)
            AND (t.download_status!='ok' OR t.parse_status!='ok') LIMIT 1""", (pid,)).fetchone())

    def audit(self, pid, action, categories=None, note=""):
        before=self.get_policy(pid)
        if not before:
            raise ValueError("政策不存在")
        if action not in ("confirm","adjust","reject"):
            raise ValueError("未知审核动作")
        from .classifier import CATEGORY_CN
        cats=list(dict.fromkeys(categories or []))
        if action == "adjust" and (not cats or any(c not in CATEGORY_CN for c in cats)):
            raise ValueError("请选择有效的政策分类")
        if action == "confirm" and not before["category"]:
            raise ValueError("未分类政策请先调整分类后采纳")
        gaps = self.material_gaps(pid)
        # 待办类型用 `todo.document_incomplete`（材料有没有**拿到正文**），
        # **不是** `material_gaps`（材料有没有解析缺口，含 partial）。
        # 两者混用会让"PDF 解析成功、OFD 只给 partial"这类条目被判成"材料待补"
        # ——正文其实拿到了两遍（实测 116 条属于这种双份形态）。
        # 必须**同时**更新 todo_type：页面统计与列表筛选读的都是 todo_type，
        # 只清 need_review 的话，人复核完的这一条会继续挂在待办队列里出不去。
        #   · 剔除            → 结论已定，出队
        #   · 材料没拿到正文  → 出人工队列，改挂"材料待补"（机器/运维的活）
        #   · 其余            → 人的判断已经做完，出队
        from types import SimpleNamespace
        from .todo import HUMAN_QUEUES, MATERIAL, NONE, document_incomplete
        doc = SimpleNamespace(parse_error=(before or {}).get('parse_error') or '',
                              attachments=self.list_attachments(pid))
        if action == "reject":
            todo_type = NONE
        elif document_incomplete(doc):
            todo_type = MATERIAL
        else:
            todo_type = NONE
        fields={"review_status": {"confirm":"confirmed","adjust":"adjusted","reject":"rejected"}[action],
                "need_review":int(todo_type in HUMAN_QUEUES), "todo_type":todo_type,
                "parse_requires_review":int(gaps), "is_investment_policy":"no" if action=="reject" else "yes", "updated_at":now()}
        if action=="adjust":
            fields.update(category=",".join(cats),category_names=",".join(CATEGORY_CN[c] for c in cats))
        with self.tx() as c:
            sets=", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE policies SET {sets} WHERE id=?", (*fields.values(),pid))
            after={**before,**fields}
            c.execute("INSERT INTO review_events(policy_id,action,before_json,after_json,note,created_at) VALUES(?,?,?,?,?,?)",
                      (pid,action,json.dumps(before,ensure_ascii=False),json.dumps(after,ensure_ascii=False),note,now()))

    def review_history(self, pid):
        return [dict(r) for r in self._conn.execute("SELECT * FROM review_events WHERE policy_id=? ORDER BY id DESC", (pid,))]

    # ---------- 仪表盘统计 ----------
    # ---------- 维护：备份 / 清空 ----------
    # 清空是**破坏性**动作，因此固定三件事：
    #   ① 先备份库文件（可回滚）  ② 口令里带当前条数（范围漂移守卫）  ③ 采集运行中拒绝
    # `source_configs`（来源配置）**永不参与清空**——它是配置不是数据，清掉就得重配 34 个来源。
    CLEARABLE_TABLES = {
        "library": ("review_events", "attachment_attempts", "attachments",
                    "policy_sources", "policies"),
        "logs": ("run_logs", "agent_events"),
    }
    PROTECTED_TABLES = ("source_configs",)

    def _table_names(self) -> set:
        return {r[0] for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

    def storage_stats(self) -> dict:
        """各表行数与库文件体积——清空前先让人看清要清掉多少。"""
        counts = {t: self._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in sorted(self._table_names())}
        p = Path(self.db_path)
        return {"counts": counts, "db_bytes": p.stat().st_size if p.exists() else 0,
                "protected": list(self.PROTECTED_TABLES)}

    def backup(self, label: str = "manual") -> str:
        """在线备份一份库文件。

        用 SQLite 的 backup API，而不是直接拷贝文件：库跑在 WAL 模式下，
        已提交但尚未 checkpoint 的内容还在 -wal 里，`cp policy.db` 会丢这部分。
        """
        dest_dir = Path(self.db_path).parent / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"policy-{time.strftime('%Y%m%d-%H%M%S')}-{label}.db"
        with self._lock:
            dst = sqlite3.connect(str(dest))
            try:
                self._conn.backup(dst)
            finally:
                dst.close()
        return str(dest)

    def clear(self, scope: str = "library") -> dict:
        """清空业务数据。scope: library（政策库）/ logs（运行日志）/ all。"""
        if scope not in ("library", "logs", "all"):
            raise ValueError(f"未知的清空范围：{scope}")
        groups = ("library", "logs") if scope == "all" else (scope,)
        existing = self._table_names()
        deleted: dict[str, int] = {}
        with self._lock, self.tx() as cur:
            for g in groups:
                for t in self.CLEARABLE_TABLES[g]:
                    if t not in existing:
                        continue
                    cur.execute(f"SELECT COUNT(*) FROM {t}")
                    deleted[t] = cur.fetchone()[0]
                    cur.execute(f"DELETE FROM {t}")
                    # 自增游标一并归零，清空后编号从 1 开始（旧链接本就已失效）
                    cur.execute("DELETE FROM sqlite_sequence WHERE name=?", (t,))
            if "library" in groups and "fetch_records" in existing:
                # fetch_records 保留（它是采集台账，重采时的处理队列就读它），
                # 但**必须解开指向已删政策的引用**：quality.py 靠这个字段 JOIN 取来源名，
                # 留着悬空 id 会让统计静默少行——这正是本项目反复出现的"静默丢数据"。
                cur.execute("UPDATE fetch_records SET policy_id=NULL WHERE policy_id IS NOT NULL")
        return {"scope": scope, "deleted": deleted, "total": sum(deleted.values())}

    def dashboard(self) -> dict:
        with self._conn:
            def one(sql: str, *a: Any) -> int:
                return int(self._conn.execute(sql, a).fetchone()[0])

            latest = "version=(SELECT MAX(version) FROM policies v WHERE v.policy_key=p.policy_key)"

            def todo(kind: str) -> int:
                """按待办类型计数。todo_type 由判定环节自动推导，不含手工置位。"""
                return one(f"SELECT COUNT(*) FROM policies p WHERE todo_type=? AND {latest}", kind)

            return {
                "sources": one("SELECT COUNT(*) FROM source_configs"),
                "sources_enabled": one("SELECT COUNT(*) FROM source_configs WHERE enabled=1"),
                "policies": len(self.query_policies(limit=1000000)),
                "pending_review": one("SELECT COUNT(*) FROM policies p WHERE need_review=1 AND " + latest),
                "confirmed": one("SELECT COUNT(*) FROM policies p WHERE review_status IN ('confirmed','adjusted') AND " + latest),
                "rejected": len(self.query_policies(review_status='rejected', limit=1000000)),
                "fetches": one("SELECT COUNT(*) FROM fetch_records"),
                "runs": one("SELECT COUNT(*) FROM run_logs"),
                "attachments": one("SELECT COUNT(*) FROM attachments"),
                # 待办三分法：机器自修的、要业务方看的、要运维处理的，分开计数
                "todo_material": todo("material"),
                "todo_review": todo("review"),
                "todo_scope": todo("scope"),
                "todo_system": todo("system"),
                "auto_confirmed": one(f"SELECT COUNT(*) FROM policies p WHERE todo_type='none' AND review_status='confirmed_auto' AND {latest}"),
            }

    def region_overview(self) -> list[dict]:
        """按地区汇总政策数量与四类分布，供"按省份浏览"使用。

        地区取 policies.region（由来源的 region 落库）。一个地区可能对应多个来源
        （如"国家"下有发改委、中国政府网），这里按地区合并——用户要的是
        "哪个省收了多少、都是哪几类"，不是来源台账。

        只统计当前版本、未被剔除的条目，与列表页口径保持一致；
        否则页面上各省之和会大于"政策记录"总数，看起来像数据错了。

        待办列读 `need_review`（"需要人看一眼"的条目），不是全部待办——
        材料待补与系统待修属于机器/运维，混进来会让这一列失去意义。
        """
        with self._conn:
            rows = self._conn.execute(
                """
                SELECT region,
                       COUNT(*) AS total,
                       MAX(page_date) AS latest_date,
                       SUM(CASE WHEN category LIKE '%guide%'     THEN 1 ELSE 0 END) AS guide,
                       SUM(CASE WHEN category LIKE '%access%'    THEN 1 ELSE 0 END) AS access,
                       SUM(CASE WHEN category LIKE '%guarantee%' THEN 1 ELSE 0 END) AS guarantee,
                       SUM(CASE WHEN category LIKE '%incentive%' THEN 1 ELSE 0 END) AS incentive,
                       SUM(CASE WHEN need_review=1 THEN 1 ELSE 0 END) AS todo
                FROM policies p
                WHERE version=(SELECT MAX(version) FROM policies v WHERE v.policy_key=p.policy_key)
                  AND review_status != 'rejected'
                GROUP BY region
                ORDER BY total DESC, region
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def todo_overview(self) -> dict:
        """各待办类型的条目数（直接读 `policies.todo_type`）。

        这是"待办清单"的数据源：把待办拆成机器能修的（material）、运维要配的
        （system）、业务要看的（candidate/review/scope），让页面回答
        "现在该谁做什么"，而不是给一个没有行动指向的总数。

        类型由判定环节派生后落库，这里只负责计数——**不再在查询层临时推导**：
        两条并行分支曾各写一套派生逻辑（一为 CASE 表达式、一为列），合并后
        统一为"存一次、各处读"，否则两套口径一旦漂移，页面上对不上账。
        """
        from .todo import HUMAN_QUEUES, MATERIAL, NONE, SYSTEM, TODO_META, TODO_ORDER
        with self._conn:
            counted = {r["todo"]: int(r["n"]) for r in self._conn.execute(
                """SELECT p.todo_type AS todo, COUNT(*) AS n
                   FROM policies p
                   WHERE p.version=(SELECT MAX(version) FROM policies v WHERE v.policy_key=p.policy_key)
                     AND p.review_status != 'rejected'
                   GROUP BY todo"""
            )}
        # 单条样本：让人点进去之前先看到"典型长什么样"
        samples = {}
        for key in TODO_ORDER:
            row = self._conn.execute(
                """SELECT p.id, p.title FROM policies p
                   WHERE p.version=(SELECT MAX(version) FROM policies v WHERE v.policy_key=p.policy_key)
                     AND p.review_status != 'rejected' AND p.todo_type = ?
                   ORDER BY p.id DESC LIMIT 1""", (key,)).fetchone()
            samples[key] = dict(row) if row else None
        cells = []
        for key in TODO_ORDER:
            name, owner, note = TODO_META[key]
            cells.append({"key": key, "name": name, "owner": owner, "note": note,
                          "count": counted.get(key, 0), "sample": samples.get(key)})
        return {
            "cells": cells,
            "counts": counted,
            "total": sum(counted.values()),
            # "需你处理"只算需要人**逐条**判断的；
            # candidate 是抽检（不逐条）、material/system 是机器与运维的活，都不计
            "human": sum(counted.get(k, 0) for k in HUMAN_QUEUES),
            "machine": counted.get(MATERIAL, 0),
            "system": counted.get(SYSTEM, 0),
            "clear": counted.get(NONE, 0),
        }

    def record_attachment_attempts(self, fid, attachments):
        with self.tx() as c:
            for a in attachments:
                c.execute("INSERT INTO attachment_attempts(fetch_id,url,fmt,download_status,parse_status,error,checked_at) VALUES(?,?,?,?,?,?,?)",
                    (fid,a['url'],a.get('fmt',''), 'ok' if a.get('sha256') and a.get('local_path') else 'failed',
                     a.get('parse_status',''),a.get('error',''),now()))

    def refresh_analysis(self, pid, fields, attachments, changed):
        """Same original, newer extraction. Human conclusions survive, with an explicit recheck flag."""
        import hashlib
        with self.tx() as c:
            c.execute('BEGIN IMMEDIATE')
            before=dict(c.execute('SELECT * FROM policies WHERE id=?',(pid,)).fetchone())
            metadata={k:v for k,v in fields.items() if k in ('wenhao','page_date','doc_date','issuing_authority') and not before.get(k)}
            if before['review_status'] in ('confirmed','adjusted','rejected'):
                fields={k:v for k,v in fields.items() if k in ('content','content_sha256','analysis_sha256','parse_error','raw_page_sha256')}
                fields.update(metadata)
                if changed: fields.update(parse_requires_review=1,need_review=1)
            fields['updated_at']=now()
            c.execute('UPDATE policies SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),pid))
            if metadata:
                c.execute('INSERT INTO review_events(policy_id,action,before_json,after_json,note,created_at) VALUES(?,?,?,?,?,?)',
                    (pid,'metadata_backfill',json.dumps({k:before.get(k) for k in metadata},ensure_ascii=False),
                     json.dumps(metadata,ensure_ascii=False),'同一来源重采补齐空字段',now()))
            keys=('local_path','fmt','sha256','parse_status','parsed_text','error','parser_version','parse_method','total_pages','parsed_pages')
            for a in attachments:
                c.execute('UPDATE attachments SET '+','.join(k+'=?' for k in keys)+' WHERE policy_id=? AND url=?',
                    (*[a.get(k,0 if k.endswith('pages') else '') for k in keys],pid,a['url']))
            if changed:
                oldtext=before.get('analysis_sha256') or hashlib.sha256(before['content'].encode()).hexdigest()
                c.execute('INSERT INTO review_events(policy_id,action,before_json,after_json,note,created_at) VALUES(?,?,?,?,?,?)',
                    (pid,'reparse',json.dumps({'analysis_sha256':oldtext,'review_status':before['review_status']}),
                     json.dumps({'analysis_sha256':fields.get('analysis_sha256'),'human_decision_preserved':before['review_status'] in ('confirmed','adjusted','rejected')}),
                     '原件未变，更新解析结果；原人工结论保留，新增材料请复核',now()))
