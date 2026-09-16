"""政策文件归集系统 CLI。

用法示例：
  python -m policy_collector.cli init-db
  python -m policy_collector.cli demo                 # 离线样例跑通全流程（推荐先跑）
  python -m policy_collector.cli sources              # 查看来源
  python -m policy_collector.cli run --source ndrc_zcwj --prefer rule
  python -m policy_collector.cli ingest --url <政策页URL> --source ndrc_zcwj
  python -m policy_collector.cli query --category 保障类 --keyword 投资
  python -m policy_collector.cli query --review pending
  python -m policy_collector.cli audit --id 3 --action confirm
  python -m policy_collector.cli export --format csv --out policies.csv
  python -m policy_collector.cli stats
  python -m policy_collector.cli schedule --source ndrc_zcwj --interval 60
  python -m policy_collector.cli web --open        # 启动本地 Web 管理界面
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import __version__
from .config import AppConfig, DEFAULT_CLASSIFY, DEFAULT_CONFIG, DEFAULT_SOURCES
from .db import Database
from .pipeline import Pipeline
from .scheduler import Scheduler


def _cfg(args: argparse.Namespace) -> AppConfig:
    return AppConfig.load(
        config_path=args.config,
        sources_path=args.sources,
        classify_path=args.classify,
    )


def cmd_init_db(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    pipe = Pipeline(cfg)
    n = pipe.sync_sources()
    print(f"数据库就绪: {cfg.db_path}")
    print(f"已同步来源 {n} 个：{', '.join(cfg.sources.keys())}")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    db = Database(cfg.db_path)
    print(f"{'名称':<16}{'站点':<18}{'地区':<6}{'栏目分类':<10}{'启用':<4}最近检查")
    for s in db.list_sources():
        print(f"{s['name']:<16}{s['site']:<18}{s['region']:<6}{s['category']:<10}"
              f"{'是' if s['enabled'] else '否':<4}{s['last_checked_at'] or '-'}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    pipe = Pipeline(cfg)
    stats = pipe.run_demo()
    print("离线样例演示完成（采集→解析→分类→去重→入库）：")
    print(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
    print("\n查询结果：")
    from .todo import NONE, TODO_META
    for p in pipe.db.query_policies(limit=20):
        tt = p.get("todo_type") or NONE
        # 标注"该谁处理"，与待办清单同口径（原来只标「待复核」，看不出责任方）
        mark = f"【{TODO_META[tt][0]}·{TODO_META[tt][1]}】" if tt != NONE else ""
        cat = (p["category_names"] or "-")[:24]
        print(f"  #{p['id']} v{p['version']} [{cat}] {p['title'][:40]}{mark} ({p['region']})")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    names = [n for n,s in cfg.sources.items() if s.enabled and not s.list_url.startswith('file:')] if args.source == 'all' else args.source.split(',')
    failed = False
    for name in names:
        pipe = Pipeline(cfg)
        try:
            stats = pipe.run_source(name, prefer=args.prefer, limit=args.limit,
                                    retry_only=args.retry_only, reclassify=args.reclassify)
            print(json.dumps({'source':name, **stats.to_dict()}, ensure_ascii=False, indent=2))
            failed = failed or stats.has_errors
        finally:
            pipe.close()
    return 1 if failed else 0


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    src = cfg.sources.get(args.source)
    if src is None:
        print(f"来源不存在: {args.source}（先运行 init-db / 检查 config/sources.yaml）")
        return 2
    pipe = Pipeline(cfg)
    stats = pipe.ingest_url(src, args.url, prefer=args.prefer)
    print(f"入库结果：{json.dumps(stats.to_dict(), ensure_ascii=False)}")
    pipe.close()
    return 1 if stats.has_errors else 0


def cmd_query(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    db = Database(cfg.db_path)
    rows = db.query_policies(
        region=args.region or "",
        category=args.category or "",
        keyword=args.keyword or "",
        review_status=args.review or "",
        limit=args.limit,
    )
    if not rows:
        print("（无匹配记录）")
        return 0
    print(f"共 {len(rows)} 条：")
    from .todo import NONE, TODO_META
    for p in rows:
        # 与待办清单同口径：标出"该谁处理"，而不是笼统的"待复核/已入库"
        tt = p.get("todo_type") or NONE
        flag = TODO_META[tt][0] if tt != NONE else "无需处理"
        cat = (p["category_names"] or "-")[:24]
        print(f"  #{p['id']} v{p['version']} [{flag}] {cat} | {p['title'][:50]}（{p['region'] or '-'}）")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """人工复核确认：confirm(确认采纳)/adjust(调整分类)/reject(剔除)。"""
    cfg = _cfg(args)
    db = Database(cfg.db_path)
    try:
        db.audit(args.id, args.action, [c.strip() for c in args.category.split(',') if c.strip()], args.note)
        print(f"政策 #{args.id} 已执行 {args.action}，已记录复核前后变化。")
    finally:
        db.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    db = Database(cfg.db_path)
    rows = db.query_policies(limit=args.limit, review_status=args.review)
    for row in rows:
        row['attachments'] = db.list_attachments(row['id'])
        row['sources'] = db.policy_sources(row['policy_key'])
    db.close()
    out = args.out or str(cfg.data_dir / ('export_policies.' + args.format))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    if args.format == "csv":
        fieldnames = ["id", "version", "title", "wenhao", "issuing_authority", "page_date",
                      "doc_date", "region", "site", "doc_type", "category", "category_names",
                      "is_investment_policy", "need_review", "review_status", "model_version", "classification_method", "fallback_reason", "page_url", "content", "attachments", "sources"]
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                row = {k: json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in r.items()}
                # 防止导出的外部网页文字在表格软件中被解释为公式。
                row = {k: "'" + v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for k,v in row.items()}
                w.writerow(row)
    elif args.format == "json":
        Path(out).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        print("--format 仅支持 csv/json")
        return 2
    print(f"已导出 {len(rows)} 条 → {out}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    db = Database(cfg.db_path)
    total = db.query_policies(limit=100000)
    print(f"政策总数: {len(total)}（当前版本，不含已剔除）")
    # 待办按"谁能解决"分开报，读的是与 Web 待办清单**同一个字段**（policies.todo_type）。
    # 原实现报的是旧的 need_review 计数——那个布尔把材料缺件、模型故障、口径待定
    # 混在一起，于是 CLI 报一个数、页面报另一组数，两边对不上账。
    from .todo import NONE, TODO_META, TODO_ORDER
    ov = db.todo_overview()
    counts = ov["counts"]
    print(f"待办分布（在库 {len(total)} 条 · 需你逐条处理 {ov['human']} 条）:")
    for key in TODO_ORDER:
        name, owner, _ = TODO_META[key]
        print(f"  {name}（{owner}）: {counts.get(key, 0)}")
    print(f"  {TODO_META[NONE][0]}: {counts.get(NONE, 0)}")
    from collections import Counter
    by_cat = Counter(p["category_names"] or "未分类" for p in total)
    print("\n按类别：")
    for k, v in by_cat.most_common():
        print(f"  {k}: {v}")
    print("\n最近运行日志：")
    for r in db.list_runs(limit=5):
        print(f"  [{r['started_at']}] {r['run_id']} {r['kind']}/{r['status']} "
              f"summary={r['summary'][:120]}")
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    sched = Scheduler(cfg, interval_minutes=args.interval)
    return sched.run_forever(source_names=args.source.split(",") if args.source else None, prefer=args.prefer, cycles=args.cycles, limit=args.limit)


def cmd_configure_llm(args):
    from .model_settings import save_model_settings
    import getpass
    cfg=_cfg(args)
    key='' if args.env_only else getpass.getpass('模型 API Key（不回显，仅保存在本机数据目录）：')
    if not args.env_only and not key.strip():raise ValueError('密钥不能为空；如使用环境变量请加 --env-only')
    save_model_settings(cfg,args.provider,args.base_url,args.model,key.strip(),args.key_env)
    print('模型配置已保存至本机；可执行 llm-check 检查连接。')
    return 0

def cmd_llm_check(args):
    from .model_settings import connection_check
    result=connection_check(_cfg(args))
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['ok'] else 1


def cmd_doctor(args):
    cfg = _cfg(args)
    cfg.data_dir.mkdir(parents=True,exist_ok=True)
    from .llm_client import LLMClient
    report = {'database':str(cfg.db_path), 'llm_ready':LLMClient(cfg.llm).available,
              'model_configured':bool(cfg.llm.effective_model), 'key_configured':bool(cfg.llm.api_key),
              'sources':[{'name':s.name,'enabled':s.enabled,'list_url':s.list_url,'note':s.note} for s in cfg.sources.values()]}
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['llm_ready'] else 1


def cmd_web(args: argparse.Namespace) -> int:
    """启动本地 Web 管理界面（需 flask 依赖）。"""
    try:
        from .webapp import create_app
    except ImportError:
        print("缺少 Flask：先执行 pip install -r requirements.txt")
        return 2
    cfg = _cfg(args)
    app = create_app(cfg)
    url = f"http://{args.host}:{args.port}"
    if args.open:
        import threading
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url + "/")).start()
    print(f"政策文件归集系统 Web 界面: {url}  (Ctrl+C 退出)")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="policy-collector", description="政策文件归集系统 v" + __version__)
    parser.add_argument("--config", default=None, help=f"运行配置 (默认 {DEFAULT_CONFIG})")
    parser.add_argument("--sources", default=None, help=f"来源配置 (默认 {DEFAULT_SOURCES})")
    parser.add_argument("--classify", default=None, help=f"分类口径配置 (默认 {DEFAULT_CLASSIFY})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="初始化数据库并同步来源")
    sub.add_parser("sources", help="列出采集来源")
    sub.add_parser("demo", help="离线样例跑通全流程（无需联网/Key）")

    p = sub.add_parser("run", help="运行指定来源的增量采集入库")
    p.add_argument("--source", required=True)
    p.add_argument("--prefer", default="llm", choices=["llm", "rule"])
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--retry-only", action="store_true")
    p.add_argument("--reclassify", action="store_true", help="重新分析未人工确认的重复文件")

    p = sub.add_parser("ingest", help="手工入库单篇 URL")
    p.add_argument("--url", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--prefer", default="llm", choices=["llm", "rule"])

    p = sub.add_parser("query", help="按条件查询政策")
    p.add_argument("--region", default="")
    p.add_argument("--category", default="")
    p.add_argument("--keyword", default="")
    p.add_argument("--review", default="", help="pending/confirmed/rejected")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("audit", help="人工复核：confirm/adjust/reject")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--action", choices=["confirm", "adjust", "reject"], required=True)
    p.add_argument("--category", default="", help="adjust 时的新分类，如 guarantee,incentive")
    p.add_argument("--note", default="")

    p = sub.add_parser("export", help="导出政策数据")
    p.add_argument("--format", choices=["csv", "json"], default="csv")
    p.add_argument("--out", default="")
    p.add_argument("--limit", type=int, default=100000)
    p.add_argument("--review", default="", help="可限定 confirmed/adjusted/pending")

    p=sub.add_parser('configure-llm',help='交互配置本机真实模型服务，密钥不回显')
    from .model_settings import PROVIDER_ORDER   # 与「模型配置」页同一份预设清单
    p.add_argument('--provider',choices=PROVIDER_ORDER,default='dashscope')
    p.add_argument('--base-url',default='')
    p.add_argument('--model',default='')
    p.add_argument('--key-env',default='')
    p.add_argument('--env-only',action='store_true',help='只存服务参数，使用环境变量密钥')
    sub.add_parser('llm-check',help='一次最小JSON连接检查，不做分类评测')
    sub.add_parser("doctor", help="检查模型配置与来源状态（不打印密钥）")
    sub.add_parser("stats", help="统计与最近运行日志")

    p = sub.add_parser("schedule", help="按间隔循环运行来源")
    p.add_argument("--source", default="", help="逗号分隔；缺省跑全部启用来源")
    p.add_argument("--cycles", type=int, default=None, help="运行轮数；缺省持续运行")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--interval", type=float, default=0, help="间隔分钟（缺省用配置）")
    p.add_argument("--prefer", default="llm", choices=["llm", "rule"])

    p = sub.add_parser("web", help="启动本地 Web 管理界面（需 flask）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = {
        "init-db": cmd_init_db,
        "sources": cmd_sources,
        "demo": cmd_demo,
        "run": cmd_run,
        "ingest": cmd_ingest,
        "query": cmd_query,
        "audit": cmd_audit,
        "export": cmd_export,
        "stats": cmd_stats,
        "doctor": cmd_doctor,
        "configure-llm":cmd_configure_llm,
        "llm-check":cmd_llm_check,
        "schedule": cmd_schedule,
        "web": cmd_web,
    }[args.cmd]
    try:
        return handler(args)
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
