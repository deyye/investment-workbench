"""Validation and auditable human review, shared by UI and HTTP tests."""
import copy,re,time
from .extract import FIELDS,STAGES,numeric
from .queue import align_pair_key

def validate_evidence(d,ids):
    if not isinstance(ids,list) or len(ids)>100 or any(not isinstance(i,str) for i in ids):raise ValueError('证据编号无效')
    byid={l['id']:l for l in d['lines']}
    for c in d['fields'].values():
        for e in c['evidence']:
            if '-seal-' in e['id']:byid[e['id']]=dict(e,text=e['quote'])
    if any(i not in byid for i in ids):raise ValueError('引用证据不在本文件中')
    return [{'id':i,'page':byid[i]['page'],'bbox':byid[i]['bbox'],'quote':byid[i]['text']} for i in dict.fromkeys(ids)]

def _apply_align(d,data,name,reason):
    """处理「跨阶段疑似同一指标」这一条待办，返回修订历史要记的 (before, after)。

    没有这个动作，对齐项就会永远留在待办里——比对表把两行都标出来，
    待办总览也计入一条，却没有任何入口能把它消掉，结果是**项目永远到不了
    「已审完」**。而「什么时候可以收工」正是待办总览存在的唯一理由。

    merge：把本文件里该指标改名为对方的名称，两行合并为一行。
    keep ：确认两者本就不是同一指标，只记录判断，不改数据。
    """
    other=data.get('other')
    decision=data.get('decision')
    if decision not in ('merge','keep'):raise ValueError('对齐处理方式无效（merge 或 keep）')
    if not isinstance(other,str) or not other.strip() or other==name:raise ValueError('对方指标名称无效')
    other=other.strip()
    if len(other)>80:raise ValueError('指标名称过长')
    hits=[m for m in d['metrics'] if m['name']==name]
    if not hits:raise ValueError('本文件中不存在该指标')
    before={'value':name}
    if decision=='merge':
        for m in hits:m['name']=other
        # 改名后本文件可能同时存在两个同名指标。数值相同的直接合并（无信息损失），
        # 数值不同的保留两条，交给原有的冲突检测提示人工选定口径。
        same=[m for m in d['metrics'] if m['name']==other]
        if len(same)>1:
            keep=next((m for m in same if m.get('method')!='human'),same[0])
            for m in same:
                if m is not keep and m.get('value')==keep.get('value'):d['metrics'].remove(m)
        return before,other
    return before,'不合并'

def update(d,data):
    if 'revision' in data and data['revision']!=d.get('revision',0):raise ValueError('文件已被修改，请刷新后再试')
    kind,name,value=data.get('kind'),data.get('name'),data.get('value')
    if not isinstance(value,str) or len(value)>15000:raise ValueError('字段内容无效')
    value=value.strip()
    reason=data.get('reason','')
    if not isinstance(reason,str) or len(reason)>1000:raise ValueError('说明过长')
    if kind=='stage':
        if value not in STAGES:raise ValueError('审批阶段无效')
        before=d['stage'];d['stage']=value
    elif kind=='align':
        if not isinstance(name,str) or not name:raise ValueError('指标名称无效')
        before,after=_apply_align(d,data,name,reason)
        # 对齐记录用「两名称为一组的稳定键」作 name，便于判断这一对是否已处理过；
        # after 是合并后的名称或「不合并」。
        d.setdefault('history',[]).append({'time':time.time(),'kind':'align','name':align_pair_key(name,after if after!='不合并' else data.get('other')),
                                          'before':before,'after':after,'reason':reason})
        d['revision']=d.get('revision',0)+1
        return d
    else:
        if kind=='fixed':
            if name not in FIELDS:raise ValueError('未知字段')
            if name=='项目代码' and value and not re.fullmatch(r'\d{4}-\d{6}-\d{2}-\d{2}-\d{6}',value):raise ValueError('项目代码须为四位-六位-两位-两位-六位数字')
            if name=='印章' and value not in ['有','无','无法判断','']:raise ValueError('印章值须为有、无或无法判断')
            c=d['fields'][name]
        elif kind=='metric':
            i=data.get('index')
            if type(i) is not int or not 0<=i<len(d['metrics']):raise ValueError('指标索引无效')
            c=d['metrics'][i]
            if c['name']!=name:raise ValueError('指标名称与索引不匹配')
        else:raise ValueError('字段类型无效')
        new_name=data.get('metric_name',name)
        if kind=='metric' and (not isinstance(new_name,str) or not new_name.strip() or len(new_name)>80):raise ValueError('指标名称无效')
        before=copy.deepcopy(c)
        if 'evidence_ids' in data:c['evidence']=validate_evidence(d,data['evidence_ids'])
        c.update(value=value or None,status='reviewed' if value else 'missing',method='human')
        c['evidence_binding']='reviewed' if 'evidence_ids' in data else 'original'
        if kind=='metric':
            c['normalized']=numeric(value);c['name']=new_name.strip()
            # Re-evaluate conflicts after a value or scope/name correction.
            for metric in d['metrics']:
                peers=[x for x in d['metrics'] if x['name']==metric['name'] and x.get('value')]
                if len({x['value'] for x in peers})>1:metric['status']='conflict'
                elif metric.get('status')=='conflict':metric['status']='reviewed' if metric.get('method')=='human' else 'needs_review'
        if kind=='fixed' and name=='项目代码':d['project_key']=value or 'unassigned:'+d['id']
        if kind=='fixed' and name=='项目名称':d['project_name']=value or d['filename']
    d.setdefault('history',[]).append({'time':time.time(),'kind':kind,'name':name,'index':data.get('index'),'after_name':c.get('name') if kind=='metric' else name,'before':before,'after':value,'reason':reason})
    d['revision']=d.get('revision',0)+1
    return d
