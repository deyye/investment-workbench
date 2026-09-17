"""待办类型：把"要不要人管"这件事从判定环节一直传到界面。

**为什么需要这个模块**

原来只有一个 `need_review` 布尔——材料缺件、模型故障、口径待定、结论待确认
全塞在一起。页面上就只剩一个没有行动指向的数字（实测 401 条里 345 条 pending）。

现在待办类型由**分类环节派生后落库**（`policies.todo_type`），每个类型对应一个明确责任方：

| 类型 | 责任方 | 含义 |
|---|---|---|
| `material` | **机器自修** | 附件没下全或读不出。补齐后自动重判，**不该占人的时间** |
| `system` | **运维处理** | 模型接口不可用导致的回退。接口配好后会自动补判 |
| `candidate` | **业务抽检** | 规则判正且强判据达标——**不静默进库**，单独成列供抽检翻转 |
| `review` | **业务确认** | 机器判不下去（多类混杂、命中边界样例），需要人看一眼 |
| `scope` | **业务拍板** | 相关性判不出来，要业务方定口径 |
| `none` | 无需处理 | 判定明确 |

**关于 `candidate`（中间档，2026-09-14 业务方裁决）**

规则模式有三种可能的收口方式：一律转人工（人工量接近全量）、
判正即自动确认（把机器建议冒充业务确认，误收会静默进库）、
以及本实现采用的中间档——把"机器很确定"的单独成列，人**抽检翻转**而不是逐条判。
它既不算全人工，也**绝不自动入库**：没被人看过之前，它始终是一条待办。

**两条并行分支的合并口径**

另一条分支（`feat/scope-criteria-and-acceptance`）把待办做成了 `policies.todo_type` 列，
本分支原先是查询时用 CASE 临时推导。合并后统一为**存一次、各处读**——
派生逻辑只此一处（分类器），查询层不再重复实现，避免两套口径漂移。
本模块保留的只是"材料是否完整"这个共用判据与展示元数据。
"""
from __future__ import annotations

MATERIAL = "material"
SYSTEM = "system"
CANDIDATE = "candidate"
REVIEW = "review"
SCOPE = "scope"
NONE = "none"

#: 展示顺序：先机器自修、再运维，最后才是人；业务队列按"确定性从高到低"排
TODO_ORDER = (MATERIAL, SYSTEM, CANDIDATE, REVIEW, SCOPE)

#: 类型 → (名称, 责任方, 一句话说明)
TODO_META = {
    MATERIAL: ("材料待补", "机器自修", "附件没下全或读不出，补齐后自动重判"),
    SYSTEM: ("系统待修", "运维处理", "模型接口不可用，配好后自动补判"),
    CANDIDATE: ("高置信候选", "业务抽检", "规则判正且证据强——抽检翻转，不自动入库"),
    REVIEW: ("结论待确认", "业务确认", "多类混杂或命中边界样例，需人看一眼"),
    SCOPE: ("待定口径", "业务拍板", "相关性判不出来，需业务方定口径"),
    NONE: ("无需处理", "—", "判定明确，不需要人工"),
}

#: 需要人**逐条**看的队列。"待人工判定"只算这几个：
#: candidate 是抽检、material/system 是机器与运维的活，都不该算到人头上。
HUMAN_QUEUES = (REVIEW, SCOPE)


def missing_text(attachments) -> bool:
    """附件里是否存在**拿不到正文**的。

    判定与质量统计共用 `quality.attachment_no_text`，**不要在这里再手写一遍**：
    此前这里写成"任何附件没有正文就算缺"，而湖北**每篇都挂一个 `<id>.zip`**
    （站点提供的"本条内容打包下载"），结果刚采回来的 **913 条政策全部**被判成
    "材料待补"——演示时看着像系统一堆问题，其实材料是齐的。
    """
    from .quality import attachment_no_text
    return any(attachment_no_text(a) for a in attachments or [])


def document_incomplete(doc) -> bool:
    """文档材料是否不完整：正文解析失败，或任一附件拿不到正文。

    **这是全仓库唯一的"材料不完整"判据。** 此前这句话被四处各写一遍，
    且都写成 `parse_status != 'ok'`——于是 `partial`（文本已提取、只是转换过程
    有提示，例如旧版 doc 经 textutil 转换）被当成缺失，可用材料永远堵在待办队列里。
    本项目已因此踩过三次坑，所以收敛到这里，新增判据一律复用，不要再手写一遍。

    注意它与 `attachment_quality.parse_complete` **不是一回事**：后者要求
    `parse_status` 恰好为 `ok`，用于详情页提示"材料待核对"，不参与待办派生。
    """
    return bool(getattr(doc, "parse_error", "")) or missing_text(getattr(doc, "attachments", []))
