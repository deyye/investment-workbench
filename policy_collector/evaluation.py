"""Shared evaluation: validate labels, bind source snapshots, and score explicit predictions."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from .models import Document

CATEGORIES = ('guide', 'access', 'guarantee', 'incentive')


def load_gold(path: Path) -> list[dict]:
    labels = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    validate_labels(labels)
    return labels


def validate_labels(labels):
    if not labels:
        raise ValueError('标注集不能为空')
    seen, identities = set(), set()
    for g in labels:
        if not isinstance(g, dict) or type(g.get('id')) is not int or g['id'] <= 0:
            raise ValueError('标注 id 必须为正整数')
        if g['id'] in seen:
            raise ValueError('标注 ID 重复')
        seen.add(g['id'])
        if type(g.get('relevant')) is not bool:
            raise ValueError('relevant 必须为布尔值')
        cats = g.get('categories')
        if not isinstance(cats, list) or any(type(c) is not str or c not in CATEGORIES for c in cats):
            raise ValueError('非法分类标签，categories 必须为四类字符串数组')
        if len(cats) != len(set(cats)):
            raise ValueError('分类标签重复')
        if bool(cats) != g['relevant']:
            raise ValueError('相关文件必须有分类；非相关文件应无分类标签')
        if g.get('label_status', 'unspecified') not in ('ai_draft', 'human_reviewed', 'unspecified'):
            raise ValueError('非法 label_status')
        for key in ('title', 'wenhao', 'page_url', 'content_sha256'):
            if key in g and not isinstance(g[key], str):
                raise ValueError(f'{key} 必须为字符串')
        if g.get('content_sha256') and not re.fullmatch('[0-9a-f]{64}', g['content_sha256']):
            raise ValueError('content_sha256 必须为原采集内容指纹')
        if g.get('page_url'):
            identity = (g['page_url'], g.get('content_sha256', ''))
            if identity in identities:
                raise ValueError('标注来源版本重复')
            identities.add(identity)


def label_summary(labels):
    return {'total': len(labels), 'status': dict(Counter(g.get('label_status', 'unspecified') for g in labels)),
            'human_reviewed': sum(g.get('label_status') == 'human_reviewed' for g in labels),
            'snapshot_bound': sum(bool(g.get('page_url') and g.get('content_sha256')) for g in labels)}


def readonly_db(path: Path):
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con


def resolve_labels(con, labels):
    """IDs are local hints; URL + ingestion fingerprint identify a labeled version."""
    validate_labels(labels)
    resolved, seen = [], set()
    for g in labels:
        if g.get('page_url'):
            sql, args = 'SELECT * FROM policies WHERE page_url=?', [g['page_url']]
            if g.get('content_sha256'):
                sql += ' AND content_sha256=?'
                args.append(g['content_sha256'])
            rows = con.execute(sql, args).fetchall()
            if len(rows) != 1:
                raise ValueError(f"标注 {g['id']} 的来源版本缺失或不唯一；请使用原采样库，勿仅依赖 ID")
            row = dict(rows[0])
        else:
            result = con.execute('SELECT * FROM policies WHERE id=?', (g['id'],)).fetchone()
            if result is None:
                raise ValueError(f"政策 ID 不存在: {g['id']}")
            row = dict(result)
        for key in ('title', 'wenhao', 'content_sha256'):
            if g.get(key) and re.sub(r'\s+', '', g[key]) != re.sub(r'\s+', '', row.get(key) or ''):
                raise ValueError(f"标注 {g['id']} 的 {key} 与数据库不匹配")
        if row['id'] in seen:
            raise ValueError('多条标注匹配同一数据库记录')
        seen.add(row['id'])
        resolved.append((g, row))
    return resolved


def document_from_row(con, row):
    fields = ('page_url', 'title', 'wenhao', 'issuing_authority', 'page_date', 'doc_date', 'content')
    doc = Document(**{k: row.get(k) or '' for k in fields})
    doc.attachments = [dict(a) for a in con.execute('SELECT * FROM attachments WHERE policy_id=? ORDER BY id', (row['id'],))]
    doc.parse_error = row.get('parse_error') or ('' if doc.content.strip() else '数据库正文为空')
    return doc


def evaluate_predictions(labels, predictions):
    validate_labels(labels)
    if len(labels) != len(predictions):
        raise ValueError('预测数量与标注数量不一致')
    tp = fp = fn = tn = ct = cp = cn = exact = pending = fallback = reviewed = incomplete = 0
    by_category = {c: {'tp': 0, 'fp': 0, 'fn': 0} for c in CATEGORIES}
    for gold, row in zip(labels, predictions):
        inv = row.get('is_investment_policy')
        if inv not in ('yes', 'no', 'pending'):
            raise ValueError('预测相关性枚举无效')
        cats = set(filter(None, (row.get('category') or '').split(',')))
        if not cats <= set(CATEGORIES):
            raise ValueError('预测类别无效')
        yes, truth = inv == 'yes', set(gold['categories'])
        tp += yes and gold['relevant']; fp += yes and not gold['relevant']
        fn += not yes and gold['relevant']; tn += inv == 'no' and not gold['relevant']
        pending += inv == 'pending'
        fallback += (row.get('classification_method') or row.get('method')) == 'rule_fallback'
        reviewed += bool(row.get('need_review'))
        incomplete += bool(row.get('input_truncated'))
        pred = cats if yes else set()
        ct += len(truth & pred); cp += len(pred - truth); cn += len(truth - pred)
        exact += inv != 'pending' and yes == gold['relevant'] and pred == truth
        for c, counts in by_category.items():
            counts['tp'] += c in pred and c in truth
            counts['fp'] += c in pred and c not in truth
            counts['fn'] += c not in pred and c in truth
    def div(a, b): return round(a / b, 4) if b else None
    return {'labeled_records': len(labels), 'relevance_precision': div(tp, tp+fp),
            'relevance_recall': div(tp, tp+fn), 'relevance_f1': div(2*tp, 2*tp+fp+fn),
            'category_micro_precision': div(ct, ct+cp), 'category_micro_recall': div(ct, ct+cn),
            'category_micro_f1': div(2*ct, 2*ct+cp+cn), 'exact_match': div(exact, len(labels)),
            'confusion': {'tp': tp, 'fp': fp, 'fn_including_pending': fn, 'tn': tn},
            'per_category': by_category, 'pending': pending, 'rule_fallback': fallback,
            'need_review': reviewed, 'input_truncated': incomplete,
            'decision_coverage': div(len(labels)-pending, len(labels)), 'labels': label_summary(labels),
            'scope': '仅标注的已入库候选；pending 不计正确，相关样本 pending 计漏判；不衡量官网发现或被排除文件召回率'}


def evaluate_db(con, labels):
    return evaluate_predictions(labels, [row for _, row in resolve_labels(con, labels)])
