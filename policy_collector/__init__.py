"""政策文件归集系统 (policy-collector)。

面向发改/投资条线政策文件"定期采集-识别分类-入库落地"的轻量工程化初版。
模块划分（与 docs/ARCHITECTURE 对应）：

    config      配置加载（运行参数 / 来源 / 分类口径）
    models      领域对象（文档、政策、分类结果）
    db          数据落地（SQLite，五类记录：来源/采集/政策及版本/附件/运行审核）
    collector   来源与网页、附件采集
    parser      正文解析（HTML/PDF/Word）
    classifier  政策识别与四类分类（规则 + LLM 双轨）
    llm         LLM 客户端（OpenAI 兼容接口）
    dedup       去重与版本管理
    pipeline    串起 采集->解析->分类->去重->入库 的流程编排
    scheduler   定时运行
    cli         命令行入口

演示：python -m policy_collector.cli demo --source demo_local
"""

__version__ = "0.5.0"
