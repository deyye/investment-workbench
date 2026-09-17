"""Material inventory and repair share the production ingestion path."""
from __future__ import annotations
import hashlib
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from .models import now
from .attachment_parsers import PARSER_VERSION, is_permanent_download_error
from .locking import ingestion_lock


def _sha_matches(path: Path, expected: str) -> bool:
    """文件内容是否与登记的 sha256 一致——**要把整个文件读一遍**。"""
    if not (expected and path.is_file()):
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == expected


class FileVerifyCache:
    """复用"原件与登记的 sha256 是否一致"的结论，键是 (路径, 大小, 修改时间)。

    为什么必须有：材料质量页每次加载都要判断全部原件是否完好，而每次判断都要
    把文件完整读一遍。本库 808 个附件共 449MB，线上实测单次 **13.5 秒**（其余页面
    0.02–0.03 秒），用户点「材料质量」后 14 秒内页面毫无反应——表现就是"点不动"。

    语义没有放宽：大小与修改时间一致就认为还是同一个文件，跳过重算哈希；
    文件被重新下载/替换/截断（大小或时间变了）会立刻重算。**故障窗口只剩
    "内容被改但大小与修改时间都不变"**，那需要有人刻意伪造，不是日常风险。
    需要无条件重算时删掉 attachment_verify 表里的行即可。
    """

    def __init__(self, db):
        self._db = db
        self._rows = {r[0]: (r[1], r[2], r[3], r[4]) for r in db._conn.execute(
            "SELECT local_path, size, mtime_ns, sha256, ok FROM attachment_verify")}
        self._dirty: dict[str, tuple] = {}

    def ok(self, path: Path, expected: str) -> bool:
        if not expected:
            return False
        try:
            st = path.stat()
        except OSError:
            return False            # 文件不在（或不可读）——与逐字节校验同结论
        if not path.is_file():
            return False
        size, mtime = st.st_size, st.st_mtime_ns
        key = str(path)
        hit = self._rows.get(key)
        if hit and hit[0] == size and hit[1] == mtime and hit[2] == expected:
            return bool(hit[3])
        ok = _sha_matches(path, expected)
        self._rows[key] = (size, mtime, expected, int(ok))
        self._dirty[key] = self._rows[key]
        return ok

    def flush(self) -> int:
        """把本次新算出来的结论写回库。没有新结论时不碰库。"""
        if not self._dirty:
            return 0
        rows = [(k, *v, now()) for k, v in self._dirty.items()]
        with self._db.tx() as c:
            c.executemany(
                "INSERT INTO attachment_verify(local_path,size,mtime_ns,sha256,ok,checked_at)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(local_path) DO UPDATE SET size=excluded.size,"
                " mtime_ns=excluded.mtime_ns, sha256=excluded.sha256,"
                " ok=excluded.ok, checked_at=excluded.checked_at", rows)
        n = len(self._dirty)
        self._dirty.clear()
        return n


def attachment_quality(a, cache=None):
    """单个附件的质量判定。`cache` 见 `FileVerifyCache`，不传就每次真算。"""
    path=Path(a.get('local_path') or '/nonexistent-policy-original')
    downloaded = cache.ok(path, a.get('sha256') or '') if cache is not None \
        else _sha_matches(path, a.get('sha256') or '')
    # 注意：这里要求 parse_status 恰好为 'ok'，**是刻意的**——`partial` 表示
    # "正文已提取，但个别页是图形/模板或转换保真度存疑"（实测 OFD 报
    # "第2页含图形/模板或缺少文本，需渲染核对"），这是真实的材料缺口，
    # 详情页要照常提示"材料待核对"。
    #
    # 它与待办类型 `material`（待补材料）**不是一回事**：后者只认"有没有正文"，
    # 因为那一格的责任方是机器（补采/重解析）。partial 这类缺口机器重试也修不掉
    # （要靠渲染+OCR 能力），所以归"结论待确认"由人判断，不能记到机器账上。
    parsed=bool(a.get('parsed_text','').strip()) and a.get('parse_status')=='ok'
    return {'download_ok':downloaded,'parse_complete':downloaded and parsed,
            'parsed_pages':a.get('parsed_pages',0),'total_pages':a.get('total_pages',0)}


def attachment_report(db, source=''):
    # 统计口径要覆盖全部附件，所以这里**不能只算当前页**——但判定结论可以缓存：
    # 否则分页只分掉了渲染，没分掉真正昂贵的整文件校验。
    cache = FileVerifyCache(db)
    rows=db._conn.execute('''SELECT a.*,p.title,p.page_url,p.region,p.source_fetch_id,p.parse_error,
        p.parse_requires_review,s.name AS source_name FROM attachments a JOIN policies p ON a.policy_id=p.id
        LEFT JOIN fetch_records f ON f.id=p.source_fetch_id LEFT JOIN source_configs s ON f.source_id=s.id
        WHERE p.version=(SELECT MAX(version) FROM policies v WHERE v.policy_key=p.policy_key)
        ORDER BY a.id''').fetchall()
    details=[];formats=defaultdict(Counter)
    for row in rows:
        a=dict(row)
        if source and a['source_name']!=source:continue
        q=attachment_quality(a, cache)
        attempt=db._conn.execute('''SELECT * FROM attachment_attempts WHERE url=? AND fetch_id IN
            (SELECT fetch_id FROM policy_sources WHERE policy_id=?) ORDER BY id DESC LIMIT 1''',(a['url'],a['policy_id'])).fetchone()
        latest=dict(attempt) if attempt else None
        current_issue=bool(latest and (latest['download_status']!='ok' or latest['parse_status']!='ok'))
        item={k:a[k] for k in ('id','policy_id','title','page_url','region','source_name','name','url','fmt','parse_status','error','parser_version')}
        item.update(q,latest_attempt=latest,needs_attention=not q['parse_complete'] or current_issue)
        details.append(item)
        counts=formats[a['fmt'] or 'unknown'];counts['total']+=1
        counts['download_ok']+=q['download_ok'];counts['parse_complete']+=q['parse_complete']
        counts['needs_attention']+=item['needs_attention']
    cache.flush()
    return {'scope':'当前版本附件，不含历史验证库；解析完整仅指程序检查通过，仍需业务核验；'
                    '原件校验结论按文件大小与修改时间复用，文件一旦变动会自动重算',
        'attachments':len(details),'download_ok':sum(r['download_ok'] for r in details),
        'parse_complete':sum(r['parse_complete'] for r in details),
        'affected_policies':len({r['policy_id'] for r in details if r['needs_attention']}),
        'by_format':{k:dict(v) for k,v in sorted(formats.items(),key=lambda kv:-kv[1]['needs_attention'])}, 'details':details}


def repair_materials(pipe, source='', limit=20, local_only=False, prefer='llm', policy_id=None,
                     reclassify=False):
    from .pipeline import RunStats
    from .collector import allowed_url
    if limit<1:raise ValueError('limit必须大于0')
    total=RunStats();outcomes=[]
    with ingestion_lock(pipe.cfg.db_path):
        pipe.sync_sources()
        candidates=pipe.db._conn.execute('''SELECT DISTINCT p.*,f.raw_path,s.name AS source_name
            FROM policies p JOIN fetch_records f ON f.id=p.source_fetch_id JOIN source_configs s ON s.id=f.source_id
            WHERE p.version=(SELECT MAX(version) FROM policies v WHERE v.policy_key=p.policy_key)
            ORDER BY COALESCE(p.updated_at,''),p.id''').fetchall()
        selected=[]
        for row in candidates:
            row=dict(row)
            if source and row['source_name']!=source:continue
            if policy_id and row['id']!=policy_id:continue
            attachments=pipe.db.list_attachments(row['id'])
            # 永久失效的附件（站点 404/410）机器修不了：既不该反复重试刷请求，
            # 也不该让整条政策一直占着"待补材料"队列排不空。
            def _needs_repair(a):
                if is_permanent_download_error(a.get('error')):return False
                return not attachment_quality(a)['parse_complete'] or a.get('parser_version')!=PARSER_VERSION
            if not policy_id and not row.get('parse_error') and not any(
                _needs_repair(a) for a in attachments):continue
            selected.append(row)
            if len(selected)==limit:break
        run_id='repair-'+uuid.uuid4().hex[:16];pipe.db.start_run(run_id,None,'repair');started=time.monotonic()
        for row in selected:
            path=Path(row['raw_path'] or '/nonexistent-policy-original')
            src=pipe.cfg.sources.get(row['source_name'])
            if not src or not allowed_url(row['page_url'],src) or not path.is_file():
                total.failed+=1;outcomes.append({'policy_id':row['id'],'error':'来源不匹配或缺少网页原件，需常规重采'});continue
            # Repair reuses saved originals, downloading only missing attachments unless local_only.
            #
            # 重判（reclassify）的触发条件要精确，否则会破坏 repair 的幂等性
            # （重复跑同一批不应反复重判、反复写 review_events）：
            #
            #   ① 材料确实变了 —— 由 pipeline 里的 `changed` 自动覆盖，不在此处传参。
            #   ② 结论已过期 —— 条目还挂在待补材料队列（todo_type='material'），
            #      但材料早已补齐（例如上一轮 repair 已把正文解析出来了，却没重判）。
            #      这是本函数存在的意义：**材料变了结论必须跟着变**。
            #      一旦重判成功，todo_type 就不再是 material，故天然幂等。
            #
            # 实测教训：14 条被选中、附件全部已解析出正文，却因未传该标志而全部落进
            # duplicates 分支——结论永远停留在"待补材料"，队列就此堵死。
            stale_verdict = (row.get('todo_type') == 'material')
            stats=pipe._ingest_url(src,row['page_url'],raw=path.read_bytes(),prefer=prefer,
                                   reuse_cached=True,local_only=local_only,
                                   reclassify=(reclassify or stale_verdict))
            total+=stats;outcomes.append({'policy_id':row['id'],**stats.to_dict()})
        total.elapsed_seconds=round(time.monotonic()-started,3)
        pipe.db.finish_run(run_id,total.to_dict(),status='partial' if total.has_errors else 'ok',
                           note='原件补采与重解析；'+('仅使用本地原件' if local_only else '允许补采缺失附件'))
    return {'run_id':run_id,'selected':len(selected),'stats':total.to_dict(),'outcomes':outcomes}


# ---------- 附件缺口判据（全仓库唯一口径）----------
# 两件事反复踩过，所以集中到一处，别再各写一份：
#   1) `partial`（已出文本、只是转换过程留了提示）**不算缺口**——按 parse_status != 'ok'
#      直接数，会把附件其实已可判读的站点误判成接入未通过。
#   2) **打包件（zip 等）在同条目其他附件已给出正文时不算缺口**。站点常附一个
#      "全部附件打包下载"（湖北每篇都带 <id>.zip，解开就是同页那些 wps/pdf），
#      我们不解压它，但材料并不缺。不分青红皂白地数，会把"材料齐了"报成"有缺口"。
PACK_SUFFIXES = ('.zip', '.rar', '.7z', '.tar', '.gz', '.tgz', '.bz2', '.xz')


def _attachment_key(att: dict):
    return att.get('url') or att.get('name')


def is_pack(att: dict) -> bool:
    """是否是"打包下载"件（zip 等容器）。"""
    return (att.get('name') or '').lower().endswith(PACK_SUFFIXES)


def attachment_no_text(att: dict) -> bool:
    """这个附件是否**没给我们正文**。

    打包件不算——它是"本条内容的打包下载"，不是一个独立材料。
    与 `is_attachment_gap` 的区别只有一个：**这里不管下载是否成功**。
    待办派生要的是这个语义（没下下来同样算"材料没拿到"）；
    质量统计把"没下下来"归 `attachments_failed`、不重复计，那边才用 `is_attachment_gap`。
    """
    if (att.get('parsed_text') or '').strip():
        return False
    return not is_pack(att)


def is_attachment_gap(att: dict, siblings=None) -> bool:
    """单个附件是否构成"材料缺口"（**统计口径**）：没拿到正文、且确实下载下来了。

    打包件豁免的实测依据：湖北每篇都挂一个 `<id>.zip`，逐个拆开核对过——
    里面是正文的 PDF 版（成品油调价那条）、同页其他附件的副本（招标文件那条），
    甚至**空包**。原件照旧留存、详情页照旧标注它未展开，只是不记成缺口。
    """
    if (att.get('parsed_text') or '').strip():
        return False
    if not (att.get('sha256') or '').strip():
        return False
    return not is_pack(att)


def count_attachment_gaps(attachments) -> int:
    return sum(1 for a in attachments if is_attachment_gap(a, attachments))


def count_attachment_failures(attachments) -> int:
    return sum(1 for a in attachments
               if a.get('parse_status') != 'ok' and not (a.get('sha256') or '').strip())


# ---------- 正文解析异常：按来源汇总 + 归因 ----------
#
# 这些信息**一直都在库里**（`fetch_records.document_json.parse_error` 与
# `fetch_records.error`），但过去界面上只给一个 run 级的"部分完成"标签。
# 实际后果：要回答"正文不全的 166 篇到底卡在哪"，只能去翻数据库；从批次详情页
# 点进去看到 36 行"有异常"，没有一行说得出该修什么。**有记录 ≠ 有界面。**
#
# 两条判据，都贴在真实输出上（不是另造一套说法）：
#
#   1) `fetch_records.error` 混装了两种语义——"业务判定不收录"（未命中投资项目
#      表述、命中事务性关键词、命中存量企业运行类政策）和"技术失败"（HTTP 404、
#      附件没抓到）。前者是**判定明确、无需人工**的正常结果，把它们一起当异常数，
#      会把"口径已经判完了"报成一屏错误。所以先分流，再归因。
#   2) 归因只用来回答"该谁解决"，不改变任何计数：每个原因映射到一个责任方和一句
#      实话——重试会不会自愈。这一层判断以前只存在于人的脑子里。
BUSINESS_SKIP_MARKER = '不收录'

# 原因关键词 → (责任方标签, 归因类别, 那句实话)
PARSE_ISSUE_RULES = (
    ('未定位正文容器', '需改代码适配来源', 'code',
     '该站正文放在非通用容器里（政务站常见的 TRS / Word 粘贴容器），解析器认不出，'
     '只留了整页文本备用。重试不会自愈，要针对这个来源补一条容器规则。'),
    ('动态防护', '需运维配置', 'ops',
     '站点有动态防护，需要先在运行环境装好浏览器组件再跑。本机缺依赖时会一直卡在这里。'),
    ('playwright', '需运维配置', 'ops',
     '同"动态防护"：缺浏览器组件。装好后重跑该来源即可。'),
    ('附件下载失败', '机器可自修', 'self',
     '附件没抓下来（网络抖动或站点握手问题）。跑一轮"补采与重解析"绝大多数能自动收掉；'
     '若仍失败，再按站点单独查。'),
    ('来源不匹配或缺少网页原件', '机器可自修', 'self',
     '本地没留网页原件，补采一次即可。'),
)
# 永久失效：如实告知不再重试，避免有人反复点下去
PERMANENT_MARKERS = ('HTTP 404', 'HTTP 410', '404', '410')


def classify_issue(text: str) -> tuple[str, str, str]:
    """把一条报错文本映射成 (责任方, 类别, 实话)。**归因规则只定义这一处。**"""
    text = (text or '').strip()
    for marker in PERMANENT_MARKERS:
        if marker in text:
            return '无需处理', 'gone', ('链接已永久失效，按设计不重试：'
                                    '反复重跑不会变好，也不会变坏。')
    for key, owner, kind, advice in PARSE_ISSUE_RULES:
        if key in text:
            return owner, kind, advice
    return '待判断', 'unknown', '尚未归类的原因。可以先把这条原文贴出来再定。'


def parse_error_report(db) -> dict:
    """正文解析异常：按来源汇总 + 归因。

    只统计**当前版本**的政策（与其他质量报表同一口径），避免同一篇的历史版本
    被重复计算——两条并行分支合并时踩过"口径一处变、另一处没变"的坑。
    """
    with db._conn:
        site_rows = db._conn.execute('''
            SELECT s.site AS site, s.name AS source_name, s.region AS region,
                   COUNT(*) AS n, MAX(f.page_url) AS sample_url,
                   MAX(COALESCE(json_extract(f.document_json,'$.parse_error'),'')) AS reason
            FROM fetch_records f
            JOIN source_configs s ON s.id = f.source_id
            JOIN policies p ON p.source_fetch_id = f.id
            WHERE p.version = (SELECT MAX(version) FROM policies v WHERE v.policy_key = p.policy_key)
              AND f.document_json IS NOT NULL AND json_valid(f.document_json)
              AND COALESCE(json_extract(f.document_json,'$.parse_error'),'') <> ''
            GROUP BY s.site, s.name, s.region
            ORDER BY n DESC''').fetchall()
        # 技术失败（排除"业务判定不收录"）
        fail_rows = db._conn.execute('''
            SELECT s.site AS site, s.name AS source_name, f.error AS error, COUNT(*) AS n
            FROM fetch_records f
            JOIN source_configs s ON s.id = f.source_id
            WHERE COALESCE(f.error,'') <> '' AND instr(f.error, ?) = 0
            GROUP BY s.site, s.name, f.error
            ORDER BY n DESC''', (BUSINESS_SKIP_MARKER,)).fetchall()
        skipped = int(db._conn.execute(
            '''SELECT COUNT(*) FROM fetch_records
               WHERE instr(COALESCE(error,''), ?) > 0''', (BUSINESS_SKIP_MARKER,)).fetchone()[0])

    sites = [dict(r) for r in site_rows]
    groups: dict[str, dict] = {}
    for s in sites:
        owner, kind, advice = classify_issue(s['reason'])
        g = groups.setdefault(owner, {'owner': owner, 'kind': kind, 'advice': advice,
                                      'count': 0, 'sites': []})
        g['count'] += s['n']
        g['sites'].append(f"{s['site']}（{s['n']} 篇）")
    order = {'code': 0, 'ops': 1, 'self': 2, 'unknown': 3, 'gone': 4}
    group_list = sorted(groups.values(), key=lambda g: (order.get(g['kind'], 9), -g['count']))

    failures = {}
    for r in fail_rows:
        owner, kind, advice = classify_issue(r['error'])
        g = failures.setdefault(owner, {'owner': owner, 'kind': kind, 'advice': advice,
                                        'count': 0, 'sites': [], 'reasons': []})
        g['count'] += r['n']
        g['sites'].append(r['site'])
        g['reasons'].append({'text': r['error'], 'n': r['n'], 'site': r['site']})
    failure_list = sorted(failures.values(), key=lambda g: (order.get(g['kind'], 9), -g['count']))

    return {
        'parse_total': sum(s['n'] for s in sites),
        'sites': sites,
        'groups': group_list,
        'failures': failure_list,
        'failure_total': sum(g['count'] for g in failure_list),
        'business_skipped': skipped,
        'scope': '只统计当前版本政策；"业务判定不收录"属判定明确，不计入异常。',
    }
