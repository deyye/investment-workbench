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

from core import shell

from .config import AppConfig
from .db import Database, POLICY_SORTABLE
from .pipeline import Pipeline
from .todo import TODO_META, TODO_ORDER

CAT_CODES = {"guide": "引导类", "access": "准入类", "guarantee": "保障类", "incentive": "激励约束类"}

# 每页条数的候选值。给范围而不是自由输入：既挡住 `per=100000` 这类拖垮页面的
# 取值，也挡住 `per=0`（会算出除零与"共 0 页"）。
PAGE_SIZES = (20, 50, 100)
# 步骤明细一屏通常看得完 50 条，所以时间线的档位比列表大一档。
EVENT_PAGE_SIZES = (50, 100, 200)

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


def resolve_policy_sort(raw_sort: str = "", raw_order: str = "") -> tuple[str, str]:
    """把地址里的 `sort` / `order` 解析成真正生效的 `(sort, order)`——**唯一一处**。

    列表页与导出页共用。两边各解析一次是这个功能的经典翻车点：只要其中一处
    对"`order` 缺失时算哪个方向"的理解不同，同一份地址导出的顺序就和页面上
    看到的不一样，而两边都是 200，没人会先怀疑排序。

    认不出的值安静回落（手改错的 URL 不该 500），方向一律取该列的默认方向。
    """
    sort = (raw_sort or "").strip()
    if sort not in POLICY_SORTABLE:
        sort = "id"
    order = (raw_order or "").strip().lower()
    if order not in ("asc", "desc"):
        order = POLICY_SORT_DEFAULT_DIR[sort]
    return sort, order


def policy_order_default(view: dict) -> str:
    """`order` 的默认方向：**随 `sort` 是哪一列而变**。

    这是 `pager_qs` 规则 ④ 唯一的用例，也是"表头只能点一次"那个缺陷的解药：
    交给它按"这一份地址里最终生效的 sort"现场求默认值，而不是拿一个固定字符串
    （写死成 `id` 列的 `desc` 的话，凡默认方向是 `asc` 的列都永远点不到降序）。
    """
    key = (view or {}).get("sort") or "id"
    return POLICY_SORT_DEFAULT_DIR.get(key, POLICY_SORT_DEFAULT_DIR["id"])


def pager_qs(state: dict, defaults: dict):
    """分页链接的查询串构造器——**全站唯一一处**。

    参数保留是列表页最容易悄悄退化的地方：改了搜索词翻页丢排序、翻页丢每页条数。
    每一页各写一套 href 必然漂移（运行记录页原来那版就只带覆盖值，
    从 `?per=100` 点"下一页"会退回默认每页条数）。所以这里统一成规则：

      ① 覆盖值盖住当前值；显式传 None 表示"把这个参数去掉"
      ② 与默认值相同的参数不写进地址（链接短、可读、能直接分享）
      ③ 因此 page=1 天然被省略，不需要额外的特判
      ④ **默认值允许是函数**：`lambda view: ...`，按"这一份地址里最终生效的参数"
         现场求默认值。用于那种**默认值本身取决于另一个参数**的字段——
         典型就是 `order`：它的默认方向随 `sort` 是哪一列而变（日期默认降序、
         标题默认升序）。规则 ② 拿一个固定字符串去比，必然错一半。

    ④ 是怎么被发现的（2026-09-17）：政策库表头点一次能排、**再点一次就不动了**，
    而且只有「标题/文号/类别/地区/状态」这几列有病，「日期」正常。原因是
    `order` 的默认值被写死成 `id` 列的 `desc`：
      · 点日期 → 要 `desc` → 与默认值相同 → 从地址里被抹掉 → 路由回落到
        「日期列默认 desc」→ 结果正确，**所以日期看起来是好的**；
      · 点标题 → 要 `desc` → 也被抹掉 → 路由回落到「标题列默认 asc」→
        点来点去永远是升序，箭头写着"降序"却永远点不到。
    一个值（`desc`）同时被当成"id 列的默认方向"和"标题列的降序"，
    两者含义不同却撞在一起，就把一半的列锁死了。

    返回 (qs, base)：`base` 是"当前生效且非默认"的参数，供模板里的
    每页条数 / 跳页两个 GET 表单当隐藏域用。
    """
    def default_of(key: str, view: dict) -> str:
        """view 是"生效参数"字典（含被覆盖后的值），默认值可以从它派生。"""
        d = defaults.get(key, "")
        return d(view) if callable(d) else d

    base = {k: str(v) for k, v in state.items() if str(v) != str(default_of(k, state))}

    def qs(**overrides):
        merged = {k: str(v) for k, v in overrides.items() if v is not None}
        merged = {**base, **merged}
        for key, value in overrides.items():
            if value is None:
                merged.pop(key, None)
        # 注意：判默认值用的是 **merged**（已含覆盖值），不是 base——
        # 换列时才知道该拿哪一列的默认方向去比（见规则 ④）。
        merged = {k: v for k, v in merged.items() if str(v) != str(default_of(k, merged))}
        return ("?" + urlencode(merged)) if merged else ""

    return qs, base


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
    # 套件条高亮判据（与工作台共用一份实现）。默认值给 'policy'：本应用单独跑时
    # `script_root` 为空、`path` 是 `/policies`，按前缀一个都对不上，但它确实是政策模块。
    shell.install(app, default='policy')

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
    def template_globals():
        # csrf_token 与 preview_mode 都放这里。
        #
        # preview_mode 以前**只在 scripts/ui_demo.py 的演示模式里注入**，正式环境下
        # base.html 的 `{% if preview_mode %}` 靠 Jinja 的未定义变量静默为假。结果就是：
        # 页面 200、测试全绿、横幅永远不显示——但"它到底什么时候才会出现"没有任何
        # 代码说得清，改模板的人也无从判断是自己写错了还是本该如此。
        #
        # 这里显式给默认值。演示脚本在 create_app() 之后再注册一个 context processor，
        # 按 Flask 的顺序后者覆盖前者，所以演示模式仍然显示横幅。
        return {'csrf_token': session.get('csrf', ''), 'preview_mode': False}

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

        # 排序参数由 resolve_policy_sort 统一解析（与导出页共用同一份判据）。
        sort, order = resolve_policy_sort(request.args.get("sort", ""),
                                         request.args.get("order", ""))

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
        # `order` 的默认值传**函数**而不是字符串：它的默认方向随 sort 是哪一列而变，
        # 写死成某一个方向就会把"默认方向与之相反的那些列"的第二次点击吃掉
        # （表现：表头点一次能排、再点一次没反应）。详见 pager_qs 规则 ④。
        defaults = {"sort": "id", "order": policy_order_default,
                    "per": PAGE_SIZES[0], "page": 1}
        qs, base = pager_qs(state, defaults)

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
        sort, order = resolve_policy_sort(request.args.get("sort", ""),
                                         request.args.get("order", ""))

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
        from .quality import attachment_report, parse_error_report
        d = db()
        # 附件明细原来是整表一次性渲染（本库 801 条），页面上既没有分页也没有
        # "总共多少条"。清单短的时候看不出来，长了以后"后面还有多少"完全无从判断。
        # 这里与政策库共用同一套分页参数。
        report = attachment_report(d)
        per = request.args.get('per', 0, type=int)
        if per not in PAGE_SIZES:
            per = PAGE_SIZES[0]
        total = len(report['details'])
        pages = max(1, (total + per - 1) // per)
        page_requested = max(request.args.get('page', 1, type=int), 1)
        page = min(page_requested, pages)
        report['details'] = report['details'][(page - 1) * per:page * per]
        qs, base = pager_qs({"per": per}, {"per": PAGE_SIZES[0], "page": 1})
        return render_template('quality.html', report=report, parse_report=parse_error_report(d),
                               page=page, pages=pages, total=total, per=per, qs=qs, base=base,
                               page_sizes=PAGE_SIZES, clamped=page_requested > pages and total > 0,
                               page_requested=page_requested)

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

        # 与政策库共用同一套查询串构造：以前这里只把"覆盖值"拼进地址，
        # 于是从 ?per=100 点"下一页"会把每页条数悄悄退回默认值。
        state = {"per": per}
        defaults = {"per": PAGE_SIZES[0], "page": 1}
        qs, base = pager_qs(state, defaults)

        return render_template("runs.html", rows=parsed, running=running, smap=smap,
                               page=page, pages=pages, total=total, per=per,
                               page_sizes=PAGE_SIZES, clamped=clamped,
                               page_requested=page_requested, qs=qs, base=base)

    @app.get('/runs/<run_id>')
    def run_detail(run_id):
        from .quality import parse_error_report
        d=db()
        row=d._conn.execute('SELECT * FROM run_logs WHERE run_id=?',(run_id,)).fetchone()
        if row is None: abort(404)
        r=dict(row)
        r['summary_obj']=json.loads(r['summary'] or '{}')
        r['progress_obj']=json.loads(r.get('progress') or '{}')
        source=next((s for s in d.list_sources() if s['id']==r['source_id']),{})
        # 步骤明细分页。原来是写死"最近100步"，看上去像设计，其实是硬上限：
        # 实测单个全国批次最多 1635 条事件，第 101 步往前在页面上等于不存在，
        # 而且没有任何提示说"还有更多"。
        per = request.args.get('per', 0, type=int)
        if per not in EVENT_PAGE_SIZES: per = EVENT_PAGE_SIZES[0]
        event_total = d.count_events(run_id)
        pages = max(1, (event_total + per - 1) // per)
        page_requested = max(request.args.get('page', 1, type=int), 1)
        page = min(page_requested, pages)
        events = d.run_events(run_id, limit=per, offset=(page - 1) * per) if event_total else []
        # 正文解析异常归因：这些结论以前只存在于"查库的人"脑子里。批次页和材料质量页
        # 共用同一份报告，避免两处口径各写一遍。
        qs, base = pager_qs({"per": per}, {"per": EVENT_PAGE_SIZES[0], "page": 1})
        return render_template('run_detail.html', r=r, source=source, events=events,
                               running=r['status']=='running',
                               parse_report=parse_error_report(d) if r['kind']=='batch' else None,
                               page=page, pages=pages, total=event_total, per=per, qs=qs, base=base,
                               page_sizes=EVENT_PAGE_SIZES, clamped=page_requested > pages and event_total > 0,
                               page_requested=page_requested)

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
