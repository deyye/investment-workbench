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


def attachment_quality(a):
    path=Path(a.get('local_path') or '/nonexistent-policy-original')
    downloaded=path.is_file() and bool(a.get('sha256'))
    if downloaded:
        downloaded=hashlib.sha256(path.read_bytes()).hexdigest()==a['sha256']
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
    rows=db._conn.execute('''SELECT a.*,p.title,p.page_url,p.region,p.source_fetch_id,p.parse_error,
        p.parse_requires_review,s.name AS source_name FROM attachments a JOIN policies p ON a.policy_id=p.id
        LEFT JOIN fetch_records f ON f.id=p.source_fetch_id LEFT JOIN source_configs s ON f.source_id=s.id
        WHERE p.version=(SELECT MAX(version) FROM policies v WHERE v.policy_key=p.policy_key)
        ORDER BY a.id''').fetchall()
    details=[];formats=defaultdict(Counter)
    for row in rows:
        a=dict(row)
        if source and a['source_name']!=source:continue
        q=attachment_quality(a)
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
    return {'scope':'本库当前版本附件，不合并其他历史验证库；解析完整仅指程序检查通过，仍需业务核验',
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
