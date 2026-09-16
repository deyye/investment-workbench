"""重算 policies.parse_requires_review：让"材料待核对"标记与真实材料状态一致。

    python scripts/refresh_material_flags.py            # 只报告，不改库
    python scripts/refresh_material_flags.py --apply    # 实际写库

**这个标记是什么**

`parse_requires_review=1` 的含义是「**材料有解析缺口**，人工确认也不代表材料已完整」，
详情页据此显示提示横幅。它包含两类情况：

- 附件拿不到正文（下载失败、需要 OCR 等）
- 附件为 `partial`：正文已提取，但个别页是图形/模板或转换保真度存疑

**为什么需要重算**

该标记在入库时写入，之后材料状态可能变化（补采成功、重新解析、人工复核），
标记不会自动跟着变。实测存在"材料早已修好、标记仍为 1"的记录，
会让复核人看到一个不成立的警告。判据直接用 `db.material_gaps()`——
与补采选择、人工复核用的是同一口径，本脚本不引入新规则。

**注意**：它与 `todo.derive` 的"材料待补"**不是一回事**。待办那一格只认
"有没有正文"，责任方是机器（补采/重解析）；而 `partial` 这类缺口机器重试也修不掉
（要靠渲染 + OCR），所以归"结论待确认"由人判断。不要把两者混用。
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from policy_collector.config import AppConfig
from policy_collector.db import Database
from policy_collector.models import now


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='实际写库（默认只报告）')
    parser.add_argument('--limit', type=int, default=0, help='最多处理多少条（0=全部）')
    args = parser.parse_args()

    cfg = AppConfig.load()
    db = Database(cfg.db_path)
    rows = [dict(r) for r in db._conn.execute(
        """SELECT id, title, parse_error, parse_requires_review FROM policies p
           WHERE p.version=(SELECT MAX(version) FROM policies v WHERE v.policy_key=p.policy_key)
             AND p.review_status != 'rejected'
           ORDER BY p.id""")]
    if args.limit:
        rows = rows[:args.limit]

    will_clear, will_set, unchanged = [], [], 0
    for r in rows:
        want = 1 if db.material_gaps(r['id']) else 0
        have = int(r['parse_requires_review'] or 0)
        if want == have:
            unchanged += 1
        elif want == 0:
            will_clear.append(r)
        else:
            will_set.append(r)

    print(f'扫描 {len(rows)} 条 · 无变化 {unchanged} · 待清除 {len(will_clear)} · 待置位 {len(will_set)}')
    for tag, items in (('清除', will_clear), ('置位', will_set)):
        for r in items[:8]:
            print(f'  [{tag}] #{r["id"]} {(r["title"] or "")[:44]}')
        if len(items) > 8:
            print(f'  [{tag}] …另有 {len(items) - 8} 条')

    if not args.apply:
        print('\n（未写库。确认无误后加 --apply）')
        return 0
    with db.tx() as c:
        for r in will_clear:
            c.execute('UPDATE policies SET parse_requires_review=0, updated_at=? WHERE id=?', (now(), r['id']))
        for r in will_set:
            c.execute('UPDATE policies SET parse_requires_review=1, updated_at=? WHERE id=?', (now(), r['id']))
    print(f'\n已写库：清除 {len(will_clear)} 条、置位 {len(will_set)} 条')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
