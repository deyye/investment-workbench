"""把已有条目的待办类型补上（合并两条分支后的一次性迁移）。

**为什么需要**
合并前本分支没有 `todo_type` 列——待办类型是查询时临时推导的
（还会把"附件 partial 但正文完整"误算成材料缺失）。合并后待办类型由分类环节
落库，于是**新旧数据都要有一次回填**，否则界面上的待办清单会是空的。

**两种模式（默认干跑，不写库）**

`--mode todo`（默认，保守）
    只看库里**已有的字段**（parse_error / 附件正文 / fallback_reason /
    is_investment_policy / need_review）推断待办类型，**不改任何判定结论**。
    适合"我只要让待办清单能用"。

`--mode rejudge`（重判）
    对 `classification_method='rule'` 的条目，用**合并后的规则**在已存的
    标题+正文上重跑一遍分类，结论与待办一起更新。
    判定结论与当前代码不一致时用这个——库里的结论本来就是旧规则产出的。
    **不动 llm / rule_fallback 的条目**：重启规则不会得到同样的结果，
    也拿不到当时的证据上下文。

用法：
    python scripts/backfill_todo_type.py                      # 干跑，看分布
    python scripts/backfill_todo_type.py --apply              # 应用保守回填
    python scripts/backfill_todo_type.py --mode rejudge       # 干看重判影响
    python scripts/backfill_todo_type.py --mode rejudge --apply
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from policy_collector.classifier import RuleClassifier  # noqa: E402
from policy_collector.config import AppConfig  # noqa: E402
from policy_collector.db import Database  # noqa: E402
from policy_collector.models import Document  # noqa: E402
from policy_collector.todo import (  # noqa: E402
    MATERIAL, NONE, REVIEW, SCOPE, SYSTEM,
)


def stored_verdict(row: dict, attachments: list[dict]) -> str:
    """只用库里已有字段推断待办类型（不改结论）。

    顺序即优先级，与 classifier 的口径保持一致：
    材料（有没有正文）→ 模型故障 → 相关性判不出 → 需人确认 → 明确。
    """
    if (row.get("parse_error") or "").strip():
        return MATERIAL
    if any(not (a.get("parsed_text") or "").strip() for a in attachments):
        return MATERIAL
    if (row.get("fallback_reason") or "").strip():
        return SYSTEM
    if row.get("is_investment_policy") == "pending":
        return SCOPE
    if row.get("need_review"):
        return REVIEW
    return NONE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="todo", choices=["todo", "rejudge"])
    ap.add_argument("--apply", action="store_true", help="真正写库（缺省只干跑）")
    args = ap.parse_args()

    cfg = AppConfig.load()
    db = Database(cfg.db_path)          # 打开时自动迁移出 todo_type 列
    rule = RuleClassifier(cfg.classification)

    rows = [dict(r) for r in db._conn.execute("SELECT * FROM policies ORDER BY id")]
    before = Counter(r.get("todo_type") or "none" for r in rows)
    after: Counter = Counter()
    updates: list[tuple[str, int, dict]] = []   # (todo, id, 结论字段)

    for r in rows:
        atts = db.list_attachments(r["id"])
        if args.mode == "todo" or r.get("classification_method") != "rule":
            todo, fields = stored_verdict(r, atts), {}
        else:
            doc = Document(title=r.get("title") or "", content=r.get("content") or "",
                           parse_error=r.get("parse_error") or "",
                           attachments=[{"name": a.get("name"), "parsed_text": a.get("parsed_text"),
                                         "parse_status": a.get("parse_status"), "error": a.get("error")}
                                        for a in atts])
            cls = rule.classify(doc)
            todo = cls.todo_type
            # material 由材料完整性决定：规则分类器不看附件，这里补上。
            # 判据必须用 quality 里那**一个**（含打包件豁免）——这里原先自己写了一遍
            # "任何附件没正文就算材料待补"，会把湖北那种 <id>.zip 打包件误判成缺口。
            from policy_collector.quality import count_attachment_gaps
            if (r.get("parse_error") or "").strip() or count_attachment_gaps(atts) > 0:
                todo = MATERIAL
            fields = {"is_investment_policy": cls.is_investment_policy, "category": cls.category,
                      "category_names": cls.category_names, "doc_type": cls.doc_type,
                      "need_review": int(cls.need_review), "reason": cls.reason,
                      "reviewer_hint": cls.reviewer_hint, "model_version": cls.model_version}
        after[todo] += 1
        # 重判过的条目**一律写回**，不只在待办类型变化时写。
        # 原因：结论字段（is_investment_policy / category / reason / model_version）
        # 本身也可能变，而"待办类型没变"不等于"结论没变"。只在变化时写，
        # 会让 model_version 停在旧值上——"这条究竟用哪版规则判的"就查不出来了。
        # `--mode todo` 的 fields 为空，行为保持不变（只写变化项）。
        if todo != (r.get("todo_type") or "none") or fields:
            updates.append((todo, r["id"], fields))

    print(f"模式 {args.mode}｜{'写库' if args.apply else '干跑（不写库）'}｜库内 {len(rows)} 条")
    keys = [MATERIAL, SYSTEM, "candidate", REVIEW, SCOPE, NONE]
    print("  迁移前 " + "　".join(f"{k}={before.get(k, 0)}" for k in keys))
    print("  迁移后 " + "　".join(f"{k}={after.get(k, 0)}" for k in keys))
    print(f"  将更新 {len(updates)} 条" + ("（含判定结论）" if args.mode == "rejudge" else "（仅待办类型）"))

    if not args.apply:
        print("\n干跑结束。确认无误后加 --apply 应用。")
        return 0

    with db.tx() as c:
        for todo, pid, fields in updates:
            sets = {"todo_type": todo, **fields}
            cols = ",".join(f"{k}=?" for k in sets)
            c.execute(f"UPDATE policies SET {cols} WHERE id=?", (*sets.values(), pid))
    print(f"\n已写入 {len(updates)} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
