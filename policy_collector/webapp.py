"""政策文件归集系统 - 本地 Web 管理界面。

提供比命令行直观的操作入口：
  仪表盘 / 政策库(检索·分页·详情) / 人工复核(确认·调整·剔除) / 来源管理与运行 / 运行日志

启动（默认仅本机访问）：
    python -m policy_collector.cli web --port 8000 --open
或：
    python -m policy_collector.webapp --port 8000

依赖 Flask（requirements.txt 已含）。所有写操作仅作用于本机 SQLite 库。
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import threading
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from flask import (Flask, Response, abort, flash, redirect, render_template, request,
                   url_for, g, session, send_file)

from .config import AppConfig
from .db import Database, POLICY_SORTABLE
from .pipeline import Pipeline
from .todo import TODO_META, TODO_ORDER

CAT_CODES = {"guide": "引导类", "access": "准入类", "guarantee": "保障类", "incentive": "激励约束类"}

# 每页条数的候选值。给范围而不是自由输入：既挡住 `per=100000` 这类拖垮页面的
# 取值，也挡住 `per=0`（会算出除零与"共 0 页"）。
PAGE_SIZES = (20, 50, 100)

#: 单次导出的上限。导出是"当前筛选结果"，正常用法会先收窄条件；
#: 给个上限是为了避免误点"全部导出"时一次性拉出十几万行把浏览器拖住。
EXPORT_LIMIT = 5000

# 政策库表头：(排序 key, 显示名, 点第一次时的方向)。
# 方向按列的性质给默认：日期想先看最新的，标题/文号/地区想先看首字靠前的。
POLICY_SORT_COLUMNS = (
    ("id", "ID", "desc"),
    ("title", "标题", "asc"),
    ("wenhao", "文号", "asc"),
    ("category", "类别", "asc"),
    ("region", "地区", "asc"),
    ("date", "日期", "desc"),
    ("todo", "状态 / 待办", "asc"),
)
POLICY_SORT_DEFAULT_DIR = {key: direction for key, _label, direction in POLICY_SORT_COLUMNS}
POLICY_SORT_LABELS = {key: label for key, label, _direction in POLICY_SORT_COLUMNS}


def _form_prefer(cfg) -> str:
    """采集实际使用的判定方式：**停用大模型后一律回落本地规则**。

    这是「启用/停用」开关在采集链路上的落点。若这里不生效，开关就只是个显示项：
    页面写着"已停用"，采集却照样调用模型——既花钱，又因为两边说法不一致而难以自查。
    """
    requested = request.form.get("prefer", "llm")
    if requested not in ("llm", "rule"):
        abort(400)
    if requested == "llm" and not cfg.llm.enabled:
        flash("当前未启用大模型，本次采集使用本地规则。可在「模型配置」中启用。", "warn")
        return "rule"
    return requested


def row_todo(policy: dict) -> str:
    """行上的待办类型：**直接读列**（判定环节派生后落库）。

    合并前这里是用 CASE 临时推导的（两条分支各写一套）；合并后统一读
    `policies.todo_type`，查询层不再重复实现派生逻辑。
    """
    return policy.get("todo_type") or "none"

# 串行化"运行采集"，避免同一时刻多线程重复抓取同一来源
_RUN_LOCK = threading.Lock()
#: 复核状态 -> 中文名。筛选下拉与列表标签**共用这一处**，避免两处各列一份后慢慢对不上。
REVIEW_LABELS = {
    "pending": "待复核",
    "confirmed": "人工确认",
    "confirmed_auto": "模型通过",
    "adjusted": "已调整",
    "rejected": "已剔除",
}
_REVIEW_STYLE = {
    "pending": "warn", "confirmed": "ok", "confirmed_auto": "ok",
    "adjusted": "ok", "rejected": "bad",
}


def _cat_label(code: str) -> str:
    return CAT_CODES.get(code, code)


def _policy_category_names(p: dict) -> list[str]:
    return [c for c in (p.get("category") or "").split(",") if c]


def create_app(cfg: AppConfig | None = None) -> Flask:
    cfg = cfg or AppConfig.load()
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)
    app.cfg = cfg

    def db() -> Database:
        if 'database' not in g:
            g.database = Database(cfg.db_path)
        return g.database

    @app.teardown_appcontext
    def close_db(error=None):
        d = g.pop('database', None)
        if d is not None: d.close()

    @app.before_request
    def protect_forms():
        session.setdefault('csrf', secrets.token_hex(24))
        if request.method == 'POST' and not secrets.compare_digest(request.form.get('csrf_token', ''), session['csrf']):
            abort(400, '表单已失效，请刷新页面重试')

    @app.context_processor
    def form_token():
        return {'csrf_token': session.get('csrf','')}

    boot = Pipeline(cfg)
    try:
        boot.sync_sources()
    finally:
        boot.close()

    # ---------------- 仪表盘 ----------------
    @app.route("/")
    def index():
        d = db()
        stats = d.dashboard()
        recent = d.query_policies(limit=8)
        runs = d.list_runs(limit=5)
        return render_template("index.html", stats=stats, recent=recent, runs=runs,
                               regions=[r for r in d.region_overview() if r["region"] != "样例"],
                               todos=d.todo_overview())

    # ---------------- 按省份浏览 ----------------
    @app.route("/provinces")
    def provinces():
        """把"全都在一个列表里"拆成"按地区分开"。

        用户反馈：政策全部堆在一个列表里看不出各省分布。这里按 region 分组，
        并把四类小计一起给出——一眼能看出某省是"只收了准入类"还是四类齐全。
        """
        d = db()
        rows = d.region_overview()
        # 中央与地方分开呈现：把"国家"混在省里，会让人误以为它是一个省。
        central = [r for r in rows if r["region"] in ("国家", "中央", "全国")]
        local = [r for r in rows if r["region"] and r["region"] not in ("国家", "中央", "全国", "样例")]
        samples = [r for r in rows if r["region"] == "样例"]
        unlabeled = [r for r in rows if not r["region"]]
        return render_template("provinces.html", central=central, local=local,
                               samples=samples, unlabeled=unlabeled,
                               local_count=len(local),
                               local_total=sum(r["total"] for r in local))

    # ---------------- 政策库 ----------------
    @app.route("/policies")
    def policies():
        q = request.args.get("q", "").strip()
        category = request.args.get("category", "").strip()
        region = request.args.get("region", "").strip()
        review = request.args.get("review", "").strip()
        todo = request.args.get("todo", "").strip()

        # 排序：列名走白名单（认不出就用默认），方向也只在 asc/desc 里取值。
        # 手改错的 URL 会安静地退回默认视图，而不是 500 或报错。
        sort = request.args.get("sort", "").strip()
        if sort not in POLICY_SORTABLE:
            sort = "id"
        default_dir = POLICY_SORT_DEFAULT_DIR[sort]
        order = request.args.get("order", "").strip().lower()
        if order not in ("asc", "desc"):
            order = default_dir

        per = request.args.get("per", 0, type=int)
        if per not in PAGE_SIZES:
            per = PAGE_SIZES[0]

        d = db()
        total = d.count_policies(region=region, category=category, keyword=q,
                                review_status=review, todo=todo)
        # 总页数由**总数**算出，不再用"本页是否取满"倒推。
        # 旧写法 `has_more = len(rows) == per` 在"总数恰好是每页整数倍"时
        # 会在最后一页多给一个指向空页的"下一页"，点进去是空白列表。
        pages = max(1, (total + per - 1) // per)
        page_requested = max(request.args.get("page", 1, type=int), 1)
        # 页码越界就地夹住：否则手改 page=99 或筛选后页码残留会看到一片空白，
        # 而"库里其实有几百条"这件事完全看不出来。
        # 一条都没有时不提示"超出范围"——那时候该说的是"没有匹配结果"。
        page = min(page_requested, pages)
        clamped = page_requested > pages and total > 0

        rows = d.query_policies(region=region, category=category, keyword=q,
                                review_status=review, todo=todo,
                                limit=per, offset=(page - 1) * per, sort=sort, order=order)
        regions = sorted({r["region"] for r in d.query_policies(limit=2000) if r["region"]})
        # 把待办类型附到每行，列表里就能直接看出"这条该谁处理"
        for r in rows:
            r["todo"] = row_todo(r)

        # 当前查询状态（不含 page）。页面里所有翻页/换排序/换每页条数的链接
        # 都从它派生，于是**筛选与排序、页码与每页条数互不丢失**；
        # 各写一套 href 是列表页最常见的退化点（改了搜索词翻页就丢排序）。
        state = {"q": q, "region": region, "category": category, "review": review,
                 "todo": todo, "sort": sort, "order": order, "per": per}
        defaults = {"sort": "id", "order": POLICY_SORT_DEFAULT_DIR["id"], "per": PAGE_SIZES[0], "page": 1}
        base = {k: v for k, v in state.items() if str(v) != str(defaults.get(k, ""))}

        def qs(**overrides):
            """在 base 之上换掉若干参数，生成可点的查询串；等于默认值的参数不写进地址。"""
            merged = {**base, **{k: str(v) for k, v in overrides.items() if v is not None}}
            for key, value in overrides.items():
                if value is None:
                    merged.pop(key, None)
            merged = {k: v for k, v in merged.items() if str(v) != str(defaults.get(k, ""))}
            return ("?" + urlencode(merged)) if merged else ""

        return render_template(
            "policies.html", rows=rows, q=q, category=category, region=region, review=review, todo=todo,
            page=page, pages=pages, total=total, per=per, page_sizes=PAGE_SIZES,
            clamped=clamped, page_requested=page_requested,
            sort=sort, order=order, sort_columns=POLICY_SORT_COLUMNS, sort_labels=POLICY_SORT_LABELS,
            base=base, qs=qs, export_limit=EXPORT_LIMIT,
            regions=regions, cat_codes=CAT_CODES, _cat_label=_cat_label,
            review_labels=REVIEW_LABELS, review_styles=_REVIEW_STYLE,
            todo_meta=TODO_META, todo_order=TODO_ORDER,
        )

    @app.get("/policies/export.csv")
    def policies_export():
        """把**当前筛选与排序**的结果导成 CSV。

        为什么要它：政策库是拿来用的资料，不是只在浏览器里看的。研究时要给同事
        一份"浙江的准入类政策清单"，此前只能一条条复制。

        三个细节是刻意的：
          1) 带 UTF-8 BOM。Excel（Windows 中文版）读没有 BOM 的 UTF-8 会把中文
             显示成乱码——政务材料在 Excel 里打开是常态，这个坑必踩。
          2) 与列表页**共用同一套筛选与排序**（`Database._policy_filters` /
             `policy_order_by`），所以"页面上筛出多少条，导出来就是多少条"，
             不会出现导出比页面多几条、口径对不上。
          3) 文件名保持 ASCII。中文文件名要按 RFC 5987 编码，各浏览器与
             Excel 的处理并不一致，反而容易导出成乱码名，不值当。
        """
        q = request.args.get("q", "").strip()
        region = request.args.get("region", "").strip()
        category = request.args.get("category", "").strip()
        review = request.args.get("review", "").strip()
        todo = request.args.get("todo", "").strip()
        sort = request.args.get("sort", "").strip()
        if sort not in POLICY_SORTABLE:
            sort = "id"
        order = request.args.get("order", "").strip().lower()
        if order not in ("asc", "desc"):
            order = POLICY_SORT_DEFAULT_DIR[sort]

        d = db()
        rows = d.query_policies(region=region, category=category, keyword=q,
                                review_status=review, todo=todo,
                                limit=EXPORT_LIMIT, sort=sort, order=order)
        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.writer(buf)
        writer.writerow(["ID", "标题", "文号", "类别", "地区", "发布日期",
                         "复核状态", "待办类型", "责任方", "来源站点", "来源链接"])
        for p in rows:
            tt = p.get("todo_type") or "none"
            meta = TODO_META.get(tt, (tt, "-", ""))
            writer.writerow([
                p.get("id", ""), p.get("title", ""), p.get("wenhao", ""),
                p.get("category_names") or "未分类", p.get("region", ""),
                (p.get("page_date") or "")[:10], REVIEW_LABELS.get(p.get("review_status"), ""),
                "" if tt == "none" else meta[0], "" if tt == "none" else meta[1],
                p.get("site", ""), p.get("page_url", ""),
            ])
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
        return Response(
            buf.getvalue(), mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=policies-{stamp}.csv"},
        )

    @app.route("/todos")
    def todos():
        """待办清单：按"谁能解决"分三格，而不是给一个混装的"待复核 N 条"。

        实际待办数取决于分类模式：规则模式下一律转人工（规则只能召回候选），
        所以用规则模式跑出来的库，这一页会几乎全是"结论待确认"——那不是清单没做好，
        而是"拿规则当生产判别器"的固有代价。模型模式才可能自动确认。
        """
        d = db()
        overview = d.todo_overview()
        lists = {key: d.query_policies(todo=key, limit=6) for key in TODO_ORDER}
        for key, rows in lists.items():
            for r in rows:
                r["todo"] = key
        return render_template("todos.html", todos=overview, lists=lists,
                               todo_meta=TODO_META, todo_order=TODO_ORDER)

    @app.route("/policies/<int:pid>")
    def policy_detail(pid: int):
        d = db()
        p = d.get_policy(pid)
        if p is None:
            abort(404)
        versions = d.versions(p["policy_key"])
        attachments = d.list_attachments(pid)
        from .quality import attachment_quality
        for a in attachments:a.update(attachment_quality(a))
        material_issues=[dict(r) for r in d._conn.execute("SELECT t.* FROM attachment_attempts t WHERE t.fetch_id IN (SELECT fetch_id FROM policy_sources WHERE policy_id=?) AND t.id=(SELECT MAX(t2.id) FROM attachment_attempts t2 WHERE t2.fetch_id=t.fetch_id AND t2.url=t.url) AND (t.download_status!='ok' OR t.parse_status!='ok')",(pid,))]
        cats = _policy_category_names(p)
        return render_template(
            "policy.html", p=p, versions=versions, attachments=attachments, cats=cats, material_issues=material_issues,
            provenance=d.policy_sources(p["policy_key"]), history=d.review_history(pid), agent_events=d.policy_agent_events(pid),
            cat_codes=CAT_CODES, _cat_label=_cat_label, review_style=_REVIEW_STYLE.get(p["review_status"], ""),
            todo_meta=TODO_META, todo_order=TODO_ORDER,
        )

    @app.get('/quality')
    def material_quality():
        from .quality import attachment_report
        return render_template('quality.html',report=attachment_report(db()))

    # ---------------- 人工复核 ----------------
    @app.route("/policies/<int:pid>/review", methods=["POST"])
    def policy_review(pid: int):
        d = db()
        p = d.get_policy(pid)
        if p is None:
            abort(404)
        action = request.form.get("action", "")
        try:
            d.audit(pid, action, request.form.getlist('categories'), request.form.get('note',''))
            flash(f"政策 #{pid} 已完成复核，变更已留痕", 'ok')
        except ValueError as exc:
            flash(str(exc), 'warn')
        return redirect(url_for("policy_detail", pid=pid))

    # ---------------- 来源与运行 ----------------
    @app.route("/sources")
    def sources():
        d = db()
        rows = d.list_sources()
        for s in rows:
            s["in_config"] = s["name"] in cfg.sources
            s["note"] = cfg.sources[s["name"]].note if s["in_config"] else ""
        from .llm_client import LLMClient
        return render_template("sources.html", rows=rows, model_ready=LLMClient(cfg.llm).available,
                               model_enabled=bool(cfg.llm.enabled))

    def _run_worker(source_name: str, prefer: str, retry_only=False) -> None:
        pipe = None
        try:
            pipe = Pipeline(cfg)
            pipe.run_source(source_name, prefer=prefer, limit=200, retry_only=retry_only)
        finally:
            if pipe: pipe.close()
            _RUN_LOCK.release()

    def _run_all_worker(run_id: str, prefer: str, retry_only=False) -> None:
        pipe = None
        try:
            pipe = Pipeline(cfg)
            # create_run=False：批次行已由路由预先登记，保证跳转到进度页时它已存在
            pipe.run_all_sources(prefer=prefer, limit=200, retry_only=retry_only,
                                 batch_run_id=run_id, create_run=False)
        except Exception as exc:                                   # noqa: BLE001
            try:
                d = Database(cfg.db_path)
                try:
                    d.finish_run(run_id, {}, status='failed', note=f'批次异常：{exc}')
                finally:
                    d.close()
            except Exception:                                      # noqa: BLE001
                pass
        finally:
            if pipe: pipe.close()
            _RUN_LOCK.release()

    @app.route("/sources/run-all", methods=["POST"])
    def source_run_all():
        """一键全国采集：不必逐个来源点「开始采集」。"""
        prefer = _form_prefer(cfg)
        names = [n for n, s in cfg.sources.items()
                 if s.enabled and not s.list_url.startswith('file:')]
        if not names:
            flash('没有已启用的来源，请先在来源配置中启用并完成验收。', 'warn')
            return redirect(url_for('sources'))
        if not _RUN_LOCK.acquire(blocking=False):
            flash('已有采集任务运行，请等待完成', 'warn')
            return redirect(url_for('runs'))
        run_id = f"batch-{secrets.token_hex(8)}"
        # 先登记批次行再起线程：否则跳转到进度页时会因记录尚未写入而 404
        try:
            d = Database(cfg.db_path)
            try:
                d.start_run(run_id, None, kind='batch')
                d.run_progress(run_id, total=len(names), completed=0, stage='starting',
                               message=f'准备采集 {len(names)} 个来源', results=[])
            finally:
                d.close()
        except Exception:
            _RUN_LOCK.release()
            raise
        t = threading.Thread(target=_run_all_worker,
                             args=(run_id, prefer, request.form.get('retry_only') == '1'),
                             daemon=True)
        try: t.start()
        except Exception:
            _RUN_LOCK.release()
            raise
        flash(f"已启动一键全国采集：{len(names)} 个来源将依次执行，可离开此页。", "ok")
        return redirect(url_for("run_detail", run_id=run_id))

    @app.route("/sources/<name>/run", methods=["POST"])
    def source_run(name: str):
        src = cfg.sources.get(name)
        if src is None or not src.enabled:
            flash(f"来源 {name} 不在当前 sources.yaml 中", "warn")
            return redirect(url_for("sources"))
        prefer = _form_prefer(cfg)
        if not _RUN_LOCK.acquire(blocking=False):
            flash('已有采集任务运行，请等待完成', 'warn')
            return redirect(url_for('runs'))
        t = threading.Thread(target=_run_worker, args=(name, prefer, request.form.get('retry_only') == '1'), daemon=True)
        try: t.start()
        except Exception:
            _RUN_LOCK.release()
            raise
        flash("任务已启动，下方会自动显示处理进度。", "ok")
        return redirect(url_for("runs"))

    # ---------------- 运行日志 ----------------
    @app.route("/runs")
    def runs():
        d = db()
        # 以前固定取最近 50 条、且没有翻页：一次"一键全国采集"就会留下 37 条记录，
        # 跑两轮之后更早的批次在界面上等于不存在。这里补上与政策库同一套分页。
        per = request.args.get("per", 0, type=int)
        if per not in PAGE_SIZES:
            per = PAGE_SIZES[0]
        total = d.count_runs()
        pages = max(1, (total + per - 1) // per)
        page_requested = max(request.args.get("page", 1, type=int), 1)
        page = min(page_requested, pages)
        clamped = page_requested > pages and total > 0
        rows = d.list_runs(limit=per, offset=(page - 1) * per)
        smap = {s["id"]: (s["site"] or s["name"]) for s in d.list_sources()}
        parsed = []
        for r in rows:
            try:
                r["summary_obj"] = json.loads(r["summary"] or "{}")
                r["progress_obj"] = json.loads(r.get("progress") or "{}")
            except Exception:  # noqa: BLE001
                r["summary_obj"] = {}
                r["progress_obj"] = {}
            parsed.append(r)
        # 是否还在跑要**全库判断**，不能只看当前这一页：换到第 2 页时，
        # 正在运行的批次落在第 1 页，只看本页会让自动刷新悄悄停掉。
        running = _RUN_LOCK.locked() or d.count_runs(status="running") > 0

        def qs(**overrides):
            merged = {k: str(v) for k, v in overrides.items() if v is not None}
            merged = {k: v for k, v in merged.items() if v != str(PAGE_SIZES[0]) or k != "per"}
            if merged.get("page") == "1":
                merged.pop("page")
            return ("?" + urlencode(merged)) if merged else ""

        return render_template("runs.html", rows=parsed, running=running, smap=smap,
                               page=page, pages=pages, total=total, per=per,
                               page_sizes=PAGE_SIZES, clamped=clamped,
                               page_requested=page_requested, qs=qs)

    @app.get('/runs/<run_id>')
    def run_detail(run_id):
        d=db()
        row=d._conn.execute('SELECT * FROM run_logs WHERE run_id=?',(run_id,)).fetchone()
        if row is None: abort(404)
        r=dict(row)
        r['summary_obj']=json.loads(r['summary'] or '{}')
        r['progress_obj']=json.loads(r.get('progress') or '{}')
        source=next((s for s in d.list_sources() if s['id']==r['source_id']),{})
        return render_template('run_detail.html',r=r,source=source,events=d.run_events(run_id),running=r['status']=='running')

    @app.get('/attachments/<int:aid>/download')
    def attachment_download(aid):
        a = db().get_attachment(aid)
        if not a or not a['local_path']: abort(404)
        path = Path(a['local_path']).resolve()
        if not path.is_relative_to(cfg.downloads_dir.resolve()) or not path.is_file(): abort(404)
        return send_file(path, as_attachment=True, download_name=path.name)

    @app.route('/settings/model', methods=['GET','POST'])
    def model_settings():
        from .model_settings import (provider_options, describe_settings,
                                     save_model_settings, set_enabled, connection_check)
        result=None
        if request.method=='POST':
            if _RUN_LOCK.locked():
                flash('采集运行中，请结束后再修改或检查模型配置','warn')
                return redirect(url_for('model_settings'))
            action=request.form.get('action','save')
            try:
                if action=='check':
                    result=connection_check(cfg)
                elif action=='toggle':
                    want=request.form.get('enabled')=='1'
                    set_enabled(cfg,want)
                    flash('已启用大模型：后续采集使用大模型判断。' if want else
                          '已停用大模型：后续采集一律使用本地规则，不产生模型调用费用。','ok')
                    return redirect(url_for('model_settings'))
                else:
                    # enabled=None = 保留当前开关状态：保存配置不改变启用与否
                    save_model_settings(cfg,request.form.get('provider','dashscope'),
                        request.form.get('base_url',''),request.form.get('model',''),
                        request.form.get('api_key','').strip(),request.form.get('api_key_env',''),
                        enabled=None)
                    flash('配置已保存在本机（密钥不回显）。','ok')
                    return redirect(url_for('model_settings'))
            except ValueError as exc:
                flash(str(exc),'warn')
        return render_template('model_settings.html',providers=provider_options(),
            state=describe_settings(cfg),result=result)

    # ---------------- 数据维护：备份 / 清空 ----------------
    def _backup_dir() -> Path:
        return Path(cfg.db_path).parent / "backups"

    def _list_backups(limit: int = 8) -> list:
        d = _backup_dir()
        if not d.exists():
            return []
        out = []
        for p in sorted(d.glob("policy-*.db"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
            st = p.stat()
            out.append({"name": p.name, "mb": round(st.st_size / 1048576, 1),
                        "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
        return out

    def _clear_token(d: Database, scope: str) -> str:
        """口令 = CLEAR-<本次将被清掉的总条数>。

        数字进口令是一道**范围漂移守卫**：页面渲染时算出的清空范围，若在提交前
        又采进了新数据，服务端会因口令不符而**拒绝**，而不是照着旧范围把新数据一并清掉。
        """
        counts = d.storage_stats()["counts"]
        groups = ("library", "logs") if scope == "all" else (scope,)
        n = sum(counts.get(t, 0) for g in groups for t in Database.CLEARABLE_TABLES[g])
        return f"CLEAR-{n}"

    @app.route("/maintenance")
    def maintenance():
        d = db()
        return render_template("maintenance.html", stats=d.storage_stats(),
                               backups=_list_backups(), running=_RUN_LOCK.locked(),
                               tokens={s: _clear_token(d, s) for s in ("library", "logs", "all")})

    @app.route("/maintenance/clear", methods=["POST"])
    def maintenance_clear():
        d = db()
        if _RUN_LOCK.locked():
            flash("采集任务正在运行，已拒绝清空——请等它跑完再操作。", "warn")
            return redirect(url_for("maintenance"))
        scope = (request.form.get("scope") or "").strip()
        if scope not in ("library", "logs", "all"):
            abort(400)
        expect = _clear_token(d, scope)
        if not secrets.compare_digest((request.form.get("confirm") or "").strip(), expect):
            flash(f"确认口令已过期（当前应为 {expect}）。多半是页面打开后又采进了新数据，请刷新后重试。", "warn")
            return redirect(url_for("maintenance"))
        backup = d.backup(label="before-clear")
        result = d.clear(scope)
        label = {"library": "政策库", "logs": "运行日志", "all": "政策库与运行日志"}[scope]
        flash(f"已清空{label}，共 {result['total']} 条记录。清空前已自动备份：{Path(backup).name}", "ok")
        return redirect(url_for("maintenance"))

    @app.get("/health")
    def health():
        return {"ok": True, "db": str(cfg.db_path)}

    return app


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="政策文件归集系统 Web 界面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args(argv)

    app = create_app()
    if args.open:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{args.host}:{args.port}/")).start()
    print(f"政策文件归集系统 Web 界面: http://{args.host}:{args.port}  (Ctrl+C 退出)")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
