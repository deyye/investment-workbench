"""Conservative identity: preserve years/parentheses and all historical snapshots."""
from __future__ import annotations
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from .db import Database
from .models import IngestResult

@dataclass
class PolicyKey:
    key: str
    source: str
    title_norm: str


def _norm_title(t):
    return re.sub(r'[《》"“”\s]', '', unicodedata.normalize('NFKC',t or '')).lower()


def make_policy_key(doc):
    title=_norm_title(doc.title)
    if doc.wenhao:
        # Brackets differ between sites, but the issuing year must never disappear.
        value=re.sub(r'\s+', '', unicodedata.normalize('NFKC',doc.wenhao))
        value=re.sub(r'[\[【(]', '〔', value)
        value=re.sub(r'[\]】)]', '〕', value)
        if re.match(r'^(公告|通知|令)〔',value):
            value=(doc.issuing_authority or doc.source_name)+':'+value
        return PolicyKey(value,'wenhao',title)
    # Without an issuing number, cross-site title matches are not enough to merge.
    value=(doc.issuing_authority or doc.source_name)+':'+title
    if not title:
        value=hashlib.sha256(doc.page_url.encode()).hexdigest()
    return PolicyKey(value,'title',title)


def content_hash(doc):
    # Compare text and attachment bytes, not page counters/navigation or attachment order/URLs.
    content=re.sub(r'\s+','',doc.content or '')
    attachments=sorted(set(a.get('sha256') or ('missing:'+a['url']) for a in doc.attachments))
    return hashlib.sha256(json.dumps([content,attachments],ensure_ascii=False).encode()).hexdigest()


class Deduplicator:
    def __init__(self,db:Database):self.db=db

    def check(self,doc):
        key=make_policy_key(doc).key
        origin=self.db.policy_for_url(doc.page_url)
        if origin:
            key=origin['policy_key']
        # Reposts of older versions cannot roll back or generate a new current version.
        same=self.db.matching_version(key,content_hash(doc))
        if same:
            return IngestResult(same['id'],'duplicate_skip','已有相同版本，保留来源',same['version'])
        prev=self.db.find_policy(key)
        if prev and not origin:
            # Equal issuing numbers with different content might be abridged reposts/collisions.
            return IngestResult(prev['id'],'suspected_duplicate','同文号内容不同，保留独立记录待复核',1)
        if prev:
            return IngestResult(prev['id'],'updated','原来源正文或附件变化，追加版本',prev['version']+1,True)
        return IngestResult(decision='ingested',detail='新政策')
