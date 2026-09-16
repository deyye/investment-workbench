"""Current review state; material coverage is independent of review completion."""

PENDING_FIELD_STATUS = ('needs_review', 'conflict', 'uncertain')
PENDING_METRIC_STATUS = ('needs_review', 'conflict', 'uncertain')

_WHY = {
    'needs_review': '系统给出的是推断值或图形候选，需人工确认',
    'conflict': '同一文档内出现多个值，需人工选定统计口径',
    'uncertain': '页面不可读，无法证明该要素确实缺失',
    'unextracted': '原文有该要素的线索，但自动提取未取到',
}
# 自检循环补回来的值可信度低于主干直取，理由要说清楚，不能让用户以为
# 它和正常抽出来的一样可靠。
_AGENT_WHY = '由自检循环补回（采用了放宽的识别办法），可信度低于主干直取，需人工核对'


def _why(key, method):
    """给出「为什么这一项需要人工判断」。自检循环的补救值优先说明其来源。"""
    if (method or '').startswith('agent-'):
        return _AGENT_WHY
    return _WHY.get(key, '需人工确认')


def _page(cell):
    return next((e['page'] for e in cell.get('evidence', []) if 'page' in e), None)


def pending_items(doc):
    """单份文档里还需要人工处理的项。"""
    items = []
    for name, c in doc['fields'].items():
        if c.get('method') == 'human' and c.get('status') not in PENDING_FIELD_STATUS:
            continue
        status = c.get('status')
        unextracted = status == 'missing' and c.get('reason') == 'unextracted'
        if status in PENDING_FIELD_STATUS or unextracted:
            items.append({
                'kind': 'fixed', 'index': None, 'name': name,
                'value': c.get('value'), 'status': status,
                'method': c.get('method'), 'page': _page(c),
                'why': _why('unextracted' if unextracted else status, c.get('method')),
            })
    for i, m in enumerate(doc['metrics']):
        peers = [x for x in doc['metrics'] if x['name'] == m['name'] and x.get('value')]
        conflict = len({x['value'] for x in peers}) > 1
        if conflict or m.get('status') in PENDING_METRIC_STATUS:
            items.append({
                'kind': 'metric', 'index': i, 'name': m['name'],
                'value': m.get('value'), 'status': 'conflict' if conflict else m.get('status'),
                'method': m.get('method'), 'page': _page(m),
                'why': _why('conflict' if conflict else m.get('status'), m.get('method')),
            })
    # 「待确认」才要人看：标题里连阶段词都没有，系统确实判不出来。
    # 「核准/备案」是已定论的归类（标题明写核准/备案，属另一条轨道），不必再占用待办。
    if doc.get('stage') not in ('建议书/立项', '可行性研究', '初步设计', '核准/备案'):
        items.insert(0, {'kind':'stage','name':'审批阶段','index':None,'value':doc.get('stage'),
                        'why':'请按批复标题确认审批阶段','status':'needs_review','page':1})
    return items


def absent_items(doc):
    """未检出线索的空字段，保留供用户抽查。"""
    return [{'name': k, 'why': '未检出相关线索，建议抽查原文'}
            for k, c in doc['fields'].items()
            if not c.get('value') and c.get('reason') == 'absent']


def align_pair_key(a, b):
    """一对疑似同一指标的稳定键。顺序无关，用于判断这对是否已被人工处理过。"""
    return '｜'.join(sorted((a, b)))


def _align_decided(docs):
    """已被人工处理过的对齐对。判据是修订历史里出现过 kind='align' 的记录。

    必须单独记这种处理：合并会把指标改名、不合并则什么都不改，两者都不会让
    指标状态发生变化，只能靠历史记录区分「还没人看过」与「已确认过、本来就
    不是同一指标」。
    """
    decided = set()
    for d in docs:
        for h in d.get('history', []):
            if h.get('kind') == 'align' and isinstance(h.get('name'), str):
                decided.add(h['name'])
    return decided


def alignment_items(rows, docs=None, decided=None):
    """跨阶段疑似同一指标的提示，同样需要人工决定是否合并。

    一对疑似指标会在两行上各标一次（比对表两行都要变色提示），但待办清单里
    只应出现一条——否则同一件事数两遍，进度永远清不了零。

    `holders` 指明每个名称分别落在哪份文件、第几条指标，界面据此发起处理动作；
    已由人工处理过的对不再出现。
    """
    decided = decided or set()
    docs = docs or []
    seen = set()
    out = []
    for r in rows:
        if r['kind'] != 'metric' or not r.get('align_with'):
            continue
        for other in r['align_with']:
            pair = tuple(sorted((r['name'], other)))
            key = align_pair_key(*pair)
            if pair in seen or key in decided:
                continue
            seen.add(pair)
            item = {'names': list(pair), 'name': pair[0], 'align_with': [pair[1]],
                    'key': key, 'value': '', 'holders': [],
                    'why': '数值相同但各阶段名称不同，疑似同一指标，请确认是否合并'}
            for n in pair:
                for d in docs:
                    for i, m in enumerate(d.get('metrics', [])):
                        if m['name'] == n:
                            item['holders'].append({'name': n, 'doc': d['id'], 'stage': d['stage'],
                                                    'filename': d['filename'], 'index': i})
                            if not item['value'] and m.get('value'):
                                item['value'] = m['value']
            out.append(item)
    return out


def project_progress(docs, rows=None):
    """项目级完成度：待办数、已处理动作数、三档状态。"""
    per_doc = []
    total = 0
    for d in docs:
        items = pending_items(d)
        total += len(items)
        per_doc.append({
            'id': d['id'], 'filename': d['filename'], 'stage': d['stage'],
            'pending': len(items), 'items': items,
            'absent': absent_items(d),
        })
    touched = sum(1 for d in docs for h in d.get('history', [])
                  if h.get('kind') in ('fixed', 'metric', 'stage', 'align'))
    align = alignment_items(rows or [], docs, _align_decided(docs))
    if total == 0 and not align:
        status = '待核对项已处理'
    elif touched == 0:
        status = '未开始'
    else:
        status = '进行中'
    pending = total + len(align)
    return {
        'pending': pending, 'field_pending': total, 'alignment_pending': len(align),
        'touched': touched, 'status': status,
        'label': '待核对项已处理' if pending == 0 else '还剩 %d 项待核对' % pending,
        'documents': per_doc, 'alignments': align,
        'stages': [{'name':stage,'count':sum(d.get('stage')==stage for d in docs)}
                   for stage in ('建议书/立项','可行性研究','初步设计')],
    }
