"""Bounded observe -> choose tool -> act -> verify loop for a single document.

The model can choose only among offered tools. Complete documents need no extra
planning call. Missing credentials use a deterministic planner and rule classifier.
No generated URL, code, SQL or unbounded retry is executed.
"""
from .models import Classification


class PolicyAgent:
    def __init__(self, classifier, emit):
        self.classifier = classifier
        self.emit = emit
        self.usage = {'input_tokens': 0, 'output_tokens': 0}

    def prepare(self, doc, repair, prefer='llm'):
        self.usage = {'input_tokens': 0, 'output_tokens': 0}
        broken = [a for a in doc.attachments if a.get('parse_status') != 'ok']
        self.emit('inspect', 'done', f'《{doc.title[:100]}》：已检查正文与{len(doc.attachments)}个附件；{len(broken)}个附件待处理')
        # Retry only transient downloads; permanent blocking and unsupported formats
        # need maintenance, not another request or a model guessing the missing text.
        retryable = [a for a in broken if any(k in a.get('error', '') for k in
                     ('Timeout', 'ConnectionError', 'HTTP 429', 'HTTP 500', 'HTTP 502', 'HTTP 503', 'HTTP 504'))]
        if not retryable:
            if broken or doc.parse_error:
                self.emit('materials', 'attention', '材料存在缺口；保留原件和已解析内容，需补采或人工核对')
            return
        action = 'retry_materials'
        client = getattr(getattr(self.classifier, 'llm', None), 'client', None)
        if prefer == 'llm' and client is not None and client.available:
            decision = client.chat_json(
                '你是政策归集Agent的工具选择器。只输出JSON。仅允许action=retry_materials或review_materials。'
                '临时下载失败且有一次重试预算时可补采；不能恢复时交人工。不得返回网址、代码或其他工具。',
                f'观察：{len(retryable)}个附件临时下载失败。剩余补采预算=1。输出{{"action":"retry_materials"}}或{{"action":"review_materials"}}。')
            self.usage = dict(client.usage)
            if isinstance(decision, dict) and decision.get('action') in ('retry_materials','review_materials'):
                action = decision['action']
            else:
                self.emit('plan', 'attention', '模型工具选择不可用，改用本地策略进行一次补采')
        else:
            self.emit('plan', 'done', '本地策略：临时下载失败，选择补采一次')
        if action == 'review_materials':
            self.emit('materials', 'attention', 'Agent选择交人工核对材料，不重复下载')
            return
        self.emit('repair', 'running', 'Agent正在补采临时失败的附件（最多一次）')
        repair({a['url'] for a in retryable})
        remaining = sum(a.get('parse_status') != 'ok' for a in doc.attachments)
        self.emit('repair', 'attention' if remaining else 'done',
                  f'补采已完成；仍有{remaining}个附件待处理' if remaining else '补采校验通过，继续判断政策')

    def classify(self, doc, prefer='llm') -> Classification:
        mode = '大模型' if prefer == 'llm' and getattr(getattr(self.classifier, 'llm', None), 'available', False) else '本地规则'
        self.emit('classify', 'running', f'使用{mode}识别相关性和政策类别')
        result = self.classifier.classify(doc, prefer)
        if result.is_investment_policy == 'no' and result.need_review:
            result.is_investment_policy = 'pending'
        self.emit('verify', 'attention' if result.need_review else 'done',
                  '判断需要复核：' + (result.reviewer_hint or '请确认相关性和分类依据')[:240]
                  if result.need_review else '分类与原文证据检查通过')
        return result
