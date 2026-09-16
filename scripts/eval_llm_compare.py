"""Compare fresh rule/LLM predictions on the same full documents and parsed attachments.

The source database is opened read-only. SQLite backup captures WAL contents into a new
snapshot. --dry-run validates labels, versions and material readiness without model calls.
Unreviewed labels require --allow-provisional; results remain exploratory.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from policy_collector.config import AppConfig
from policy_collector.classifier import Classifier
from policy_collector.models import Classification
from policy_collector.evaluation import (load_gold, readonly_db, resolve_labels, document_from_row,
    evaluate_predictions, evaluate_db, label_summary)


def snapshot_database(src_con, copy_db):
    source = Path(src_con.execute('PRAGMA database_list').fetchone()[2]).resolve()
    copy_db = Path(copy_db).resolve()
    if source == copy_db or copy_db.exists():
        raise ValueError('评测副本必须是新文件，不能覆盖原库或已有结果')
    copy_db.parent.mkdir(parents=True, exist_ok=True)
    dest = sqlite3.connect(copy_db)
    try:
        src_con.backup(dest)
    finally:
        dest.close()


def reclassify_with_llm(src_con, copy_db, labels, classifier, out_dir, *, dry_run=False):
    snapshot_database(src_con, copy_db)
    con = readonly_db(copy_db)
    try:
        resolved = resolve_labels(con, labels)  # 全部预检完成后才可调用模型
        documents = [document_from_row(con, row) for _, row in resolved]
        result_path = Path(out_dir) / 'predictions.jsonl'
        readiness = [{'id': g['id'], 'database_id': row['id'], 'page_url': row['page_url'],
            'content_sha256': row['content_sha256'],
            'analysis_sha256': hashlib.sha256(doc.analysis_text.encode()).hexdigest(),
            'body_chars': len(doc.content), 'analysis_chars': len(doc.analysis_text),
            'attachments': len(doc.attachments),
            'attachments_incomplete': sum(a.get('parse_status') != 'ok' for a in doc.attachments)}
            for (g, row), doc in zip(resolved, documents)]
        baseline = [asdict(classifier.classify(doc, prefer='rule')) for doc in documents]
        report = {'labeled': len(labels), 'labels': label_summary(labels), 'material_readiness': readiness,
            'stored_predictions': evaluate_predictions(labels, [row for _, row in resolved]),
            'rule_baseline': evaluate_predictions(labels, baseline), 'dry_run': dry_run}
        if dry_run:
            report['evaluation_status'] = 'preflight_only'
            return report
        if not classifier.llm.available:
            raise ValueError('未配置可用真实模型；请配置后检查连接，或先使用 --dry-run')
        predictions = []
        counters = {'llm_classified': 0, 'fallback': 0, 'failed': 0, 'not_attempted': 0,
                    'input_tokens': 0, 'output_tokens': 0}
        consecutive_failures = 0
        with result_path.open('x', encoding='utf-8') as log:
            for ((g, row), doc, rule, material) in zip(resolved, documents, baseline, readiness):
                start = time.monotonic()
                if consecutive_failures >= 3:
                    cls = Classification(method='not_attempted', need_review=True, reason='连续三次服务失败，停止后续付费调用')
                    counters['not_attempted'] += 1
                else:
                    try:
                        cls = classifier.classify(doc, prefer='llm')
                    except Exception as e:
                        # 不输出可能含请求地址/密钥的异常正文；失败不能沿用原库旧预测。
                        cls = Classification(method='error', need_review=True, reason=type(e).__name__)
                    if cls.method == 'llm':
                        counters['llm_classified'] += 1
                        consecutive_failures = 0
                    else:
                        counters['fallback' if cls.method == 'rule_fallback' else 'failed'] += 1
                        consecutive_failures += 1
                prediction = asdict(cls)
                predictions.append(prediction)
                counters['input_tokens'] += cls.input_tokens
                counters['output_tokens'] += cls.output_tokens
                log.write(json.dumps({**material, 'rule': rule, 'llm': prediction,
                    'elapsed_seconds': round(time.monotonic()-start, 3)}, ensure_ascii=False) + '\n')
                log.flush()  # 中断仍保留已完成结果，副本数据库始终不改写预测
        report['llm'] = evaluate_predictions(labels, predictions)
        report['llm_reclassify'] = counters
        report['run_complete'] = counters['llm_classified'] == len(labels)
        reviewed = report['labels']['human_reviewed'] == len(labels)
        report['evaluation_status'] = ('human_reviewed' if reviewed else 'exploratory') if report['run_complete'] else 'incomplete'
        return report
    finally:
        con.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--src-db', required=True)
    ap.add_argument('--gold', required=True)
    ap.add_argument('--out-dir', help='新结果目录，默认创建独立临时目录')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--allow-provisional', action='store_true')
    args = ap.parse_args(argv)
    try:
        labels = load_gold(Path(args.gold))
        if not args.dry_run and not args.allow_provisional and label_summary(labels)['human_reviewed'] != len(labels):
            raise ValueError('标签未经全部人工终审；探索性比较请加 --allow-provisional')
        cfg = AppConfig.load()
        classifier = Classifier(cfg)
        out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else Path(tempfile.mkdtemp(prefix='pc_llm_eval_'))
        if out_dir.exists() and any(out_dir.iterdir()):
            raise ValueError('结果目录非空，请使用新目录；不会覆盖既有文件')
        con = readonly_db(Path(args.src_db))
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            report = reclassify_with_llm(con, out_dir/'policy_snapshot.db', labels, classifier, out_dir, dry_run=args.dry_run)
        finally:
            con.close()
        report.update(model=cfg.llm.effective_model,
            gold_sha256=hashlib.sha256(Path(args.gold).read_bytes()).hexdigest(),
            rules_sha256=hashlib.sha256(json.dumps(cfg.classification, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            prompt_sha256=hashlib.sha256(classifier.llm.SYSTEM_PROMPT_TMPL.encode()).hexdigest(),
            chunk_chars=cfg.llm.chunk_chars, max_chunks=cfg.llm.max_chunks)
        (out_dir/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if args.dry_run or report.get('run_complete') else 1
    except (ValueError, OSError, sqlite3.Error) as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
