"""业务验收检查：用 validation/acceptance_cases.json 跑判定规则，输出业务指标。

与 tests/ 的区别（重要）：
  tests/ 验证"代码实现符合写下的规则"；
  本脚本验证"规则是否符合真实业务"——输出静默误收、漏收、待复核等业务指标。

判定结论（收/不收/待判定）是程序性输出；真正决定业务后果的是「是否静默入库」：
只有"判为收且无需人工介入"才算静默入库。因此指标以「静默误收」为核心。

用法：
    python scripts/acceptance_check.py                 # 规则模式
    python scripts/acceptance_check.py --prefer llm    # 模型模式（需配置密钥）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from policy_collector.classifier import Classifier  # noqa: E402
from policy_collector.config import AppConfig  # noqa: E402
from policy_collector.models import Document  # noqa: E402

CASES_PATH = PROJECT_ROOT / "validation" / "acceptance_cases.json"
CN = {"yes": "收", "no": "不收", "pending": "待判定"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefer", default="rule", choices=["rule", "llm"])
    ap.add_argument("--verbose", action="store_true", help="打印每个用例的判定理由")
    args = ap.parse_args()

    cfg = AppConfig.load()
    clf = Classifier(cfg)
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]

    silent_bad, missed, to_review, auto_ok = [], [], [], []
    rows_all = []
    for case in cases:
        cls = clf.classify(Document(title=case["title"], content=case["content"]), prefer=args.prefer)
        relevant = cls.is_investment_policy
        # 「静默入库」= 判为收且**完全不需人过目**。中间档（candidate）不算静默入库：
        # 它虽然没有逐条判，但仍挂在待办清单里等人抽检翻转。
        silent_ingest = (relevant == "yes" and cls.todo_type == "none")
        row = {
            "id": case["id"], "kind": case["kind"], "expect": case["expect"],
            "got": relevant, "todo": cls.todo_type, "reason": cls.reason,
            "cats": cls.category_names, "silent": silent_ingest,
        }
        rows_all.append(row)
        if case["expect"] == "no" and silent_ingest:
            silent_bad.append(row)
        elif case["expect"] == "yes" and relevant == "no":
            missed.append(row)
        elif cls.todo_type == "none" and relevant == case["expect"]:
            auto_ok.append(row)
        else:
            to_review.append(row)

    n_pos = sum(1 for c in cases if c["expect"] == "yes")
    n_neg = sum(1 for c in cases if c["expect"] == "no")
    n_all = len(cases)

    def show(title: str, rows: list, note: str = ""):
        print(f"\n{title}　{len(rows)} 例" + (f"　— {note}" if note else ""))
        for r in rows:
            mark = "收" if r["got"] == "yes" else ("不收" if r["got"] == "no" else "待判定")
            print(f"  [{r['id']}] {r['kind']}")
            print(f"      期望 {r['expect']}　判定 {mark}　待办 {r['todo']}")
            if args.verbose:
                print(f"      理由 {(r['reason'] or '')[:100]}")

    print("=" * 72)
    print(f"业务验收｜样本集 {data['version']}｜共 {n_all} 例（正 {n_pos} / 负 {n_neg}）｜判定模式 {args.prefer}")
    print("=" * 72)

    show("★ 静默误收（判为收且无需人工介入，但期望是不收）—— 最严重", silent_bad,
         "误收会直接进库、再无复核机会")
    show("★ 漏收（期望收但被明确排除）", missed, "重要政策丢失")
    show("待复核（进人工队列，未静默入库）", to_review)
    show("自动确认且正确", auto_ok)

    print("\n" + "=" * 72)
    print("业务指标")
    print("=" * 72)
    print(f"  静默误收率    {len(silent_bad)} / {n_neg} 负样本"
          f" = {len(silent_bad) / n_neg:.1%}" if n_neg else "  静默误收率    n/a")
    print(f"  漏收率        {len(missed)} / {n_pos} 正样本"
          f" = {len(missed) / n_pos:.1%}" if n_pos else "  漏收率        n/a")
    print(f"  明确率（无需人工也无需抽检）  {len(auto_ok)} / {n_all}"
          f" = {len(auto_ok) / n_all:.1%}")
    print(f"  需人工介入率  {(len(to_review) + len(silent_bad) + len(missed))} / {n_all}"
          f" = {(len(to_review) + len(silent_bad) + len(missed)) / n_all:.1%}")

    # 待办分布：区分「高置信候选（抽检）」与「逐条复核」，两者的人工投入完全不同
    dist: dict[str, int] = {}
    for r in rows_all:
        dist[r["todo"]] = dist.get(r["todo"], 0) + 1
    order = ["none", "candidate", "review", "scope", "material", "system"]
    print("  待办分布      " + "　".join(
        f"{k}={dist[k]}" for k in order if k in dist))
    print("\n注：本脚本检验的是「规则是否符合业务」，与 tests/ 互为补充——")
    print("    tests/ 只证明「代码实现符合写下的规则」，不能证明规则正确。")
    return 1 if silent_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
