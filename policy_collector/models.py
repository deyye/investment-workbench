"""领域模型：文档、政策、分类结果、入库记录。

字段命名遵循任务要求（标题/文号/发文机关/发布日期/成文日期/正文/附件），
不额外堆砌概念。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Document:
    """一篇待处理的原始文档（网页正文或附件解析结果）。"""

    source_name: str = ""
    page_url: str = ""            # 网页原文地址
    title: str = ""
    wenhao: str = ""              # 文号（原文无则空）
    issuing_authority: str = ""   # 发文机关
    page_date: str = ""           # 网页发布日期（YYYY-MM-DD，未知为空）
    doc_date: str = ""            # 成文日期（落款日期）
    content: str = ""             # 正文文本
    doc_type: str = ""            # 文件类型：正式政策/申报通知/解读/征求意见稿/项目批复/其他
    origin_path: str = ""         # 若是本地样例/附件，原始文件路径
    raw_bytes_sha256: str = ""    # 原文件哈希（去重用）
    attachments: list = field(default_factory=list)
    parse_error: str = ""
    parser_version: str = ""
    parse_method: str = ""
    total_pages: int = 0
    parsed_pages: int = 0

    @property
    def analysis_text(self) -> str:
        parts = ["[正文]\n" + self.content]
        parts.extend("[附件：" + a.get("name", "") + "]\n" + a["parsed_text"]
                     for a in self.attachments if a.get("parsed_text"))
        return "\n\n".join(parts)


@dataclass
class Classification:
    """一份文件的识别分类结果。"""

    is_investment_policy: str = "pending"  # yes / no / pending（待判定）
    category: str = ""                     # guide/access/guarantee/incentive（可逗号多标签）
    category_names: str = ""               # 中文名，多标签用","分隔
    doc_type: str = "其他"
    need_review: bool = False              # 是否需人处理（candidate/scope/review 为真）
    todo_type: str = "none"                # none/material/system/candidate/review/scope，见 classification.yaml
    reason: str = ""                       # 判断理由
    evidence: str = ""                     # 原文证据位置/片段
    confidence: Optional[float] = None     # LLM 自评置信度（仅供参考，不作"已确认"）
    model_version: str = ""
    reviewer_hint: str = ""                # 待办原因
    method: str = "rule"
    fallback_reason: str = ""
    input_truncated: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    usage_reported: bool = False

    @property
    def todo_name(self) -> str:
        return {"none": "", "material": "待补材料", "candidate": "高置信候选",
                "scope": "待定口径", "review": "待复核结论",
                "system": "待修故障"}.get(self.todo_type, "")


@dataclass
class IngestResult:
    """单篇入库结果。"""

    policy_id: Optional[int] = None
    decision: str = "ingested"   # ingested / duplicate_skip / updated / linked / excluded / error
    detail: str = ""
    version: int = 1
    new_version_created: bool = False


@dataclass
class AttachmentRecord:
    name: str = ""
    url: str = ""
    local_path: str = ""
    fmt: str = ""
    sha256: str = ""
    parse_status: str = "not_parsed"  # ok / failed / not_parsed
    parsed_text: str = ""
