'use strict';
const $ = id => document.getElementById(id);
const state = {groups: [], key: null, kind: 'fixed', docId: '', selection: null, page: 1, stages: [], evidenceIds: new Set(), modelReady: false, trash: []};
const labels = {same:'一致',different:'内容变化',equivalent:'表述差异',missing:'未载明',review:'待核对',align:'疑似同一指标',unextracted:'原文有线索未提取到'};
// 侧栏胶囊用短名，完整阶段名放进 title——侧栏只有 230px，写全名会把三个阶段挤成两行。
const STAGE_SHORT = {'建议书/立项':'建议书','可行性研究':'可研','初步设计':'初设','核准/备案':'核准'};
let toastTimer;
let latestJobs=[];
let savingReview=false;
let watchingJob=false;
let progressDisconnected=false;
// 说明每个值的来源与可信度，让复核者知道"为什么要确认"，而不是只看到一个红色状态。
// 后端产出的每一种来源都必须在这里有解释：漏掉一种，那个值在用户眼里就是"来历不明"
// ——大模型给的、自检循环补救的，会和规则直接抽出的一模一样，这与本系统
// 「每个值都要有出处」的定位直接冲突。
const methodNotes = {
  human: c => c.evidence_binding === 'reviewed' ? '此值及证据经人工复核。' : '此值经人工修订，以下仍为原提取证据。',
  'macos-vision': () => '本值由版头图片经 OCR 识别得到，可能存在识别误差，请对照原文版头核对。',
  tesseract: () => '本值由版头图片经 OCR 识别得到，可能存在识别误差，请对照原文版头核对。',
  'vision-required': () => '该要素位于图片层且未能识别出内容，请人工查看原文版头后再填。',
  'vision-model': () => '本值由视觉模型复核印章候选得到，不鉴定印章真实性，请对照原文核对。',
  filename: () => '正文无该文本，值取自附件名推断，请核对。',
  'title-inference': () => '该值由标题推断而来，请核对事项口径。',
  addressee: () => '该值由主送机关（收件人）推断为项目单位，请核对角色。',
  'visual-heuristic': () => '红色近圆形区域检测结果，请核对图像后确认；不鉴定真实性。',
  llm: () => '本值由大模型辅助抽取，其引用已通过原文校验，请按需核对。',
  'agent-widen': () => '本值由自检循环放宽章节判据后补回，可信度低于主干直取，请核对。',
  'agent-alt-form': () => '本值由自检循环换用备用识别形态补回，可信度低于主干直取，请核对。',
  'agent-read': () => '本值由自检循环定点回读原文补回，可信度低于主干直取，请核对。'
};
// 自检循环的动作名，翻成业务语言，让用户看得懂它到底做了什么。
const actionLabels = {
  widen_sections: '放宽章节判据后重扫',
  alt_form_metrics: '换用备用识别形态重抽',
  read_section: '定点回读原文补字段',
  escalate: '放弃自动修复并上报'
};
function methodNote(row, c) {
  if (row.name === '印章' && !methodNotes[c.method]) return '';
  const note = methodNotes[c.method];
  return note ? note(c) : '';
}
function toast(msg) {
  $('toast').textContent = msg; $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 10000);
}
async function api(path, body) {
  const response = await fetch(path, body ? {method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'ApprovalAgent'},body:JSON.stringify(body)} : {});
  const result = await response.json();
  if (!response.ok) {const error=Error(result.error || '请求失败');error.status=response.status;throw error;}
  return result;
}
function node(tag, text, cls) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (cls) element.className = cls;
  return element;
}
function group() { return state.groups.find(g => g.key === state.key); }
function option(value, text) { const o = node('option', text); o.value = value; return o; }
async function refresh() {
  const [documents,jobs,trash]=await Promise.all([api('/approval/api/documents'),api('/approval/api/jobs'),api('/approval/api/trash')]);
  state.groups=documents.groups;latestJobs=jobs;state.trash=trash.items||[];
  if (!group()) state.key = state.groups[0]?.key || null;
  if (state.docId && !group()?.documents.some(d => d.id === state.docId)) state.docId = '';
  render();
}
function render() {
  const g = group();
  renderSummary();renderJobPanel();
  $('exportCurrent').disabled=!g;$('exportJson').disabled=!g;$('exportAll').disabled=!state.groups.length;
  $('docCount').textContent = state.groups.reduce((a,b) => a+b.documents.length,0) + ' 份';
  $('clearAll').disabled = !state.groups.length;
  $('projects').replaceChildren();
  // 一个项目一个框：名称、可点的阶段胶囊、进度条三层分开，
  // 这样「哪个项目缺哪个阶段」「还剩多少没核对」都不用去读一行挤在一起的文字。
  for (const item of state.groups) {
    const card = node('div', undefined, 'project-item' + (item.key === state.key ? ' active':''));
    const head = node('div', undefined, 'project-head');
    const name = node('button', item.name, 'project-name');
    name.title = item.name + '（' + item.documents.length + ' 份批文）';
    name.onclick = () => {state.key=item.key;state.docId='';state.selection=null;render();resetEvidence();};
    const remove = node('button','移出','project-delete');
    remove.title = '把本项目的批文全部移入回收站（可恢复）';
    remove.onclick = () => deleteProject(item);
    head.append(name,remove);card.append(head);

    const chips = node('div', undefined, 'stage-chips');
    // 固定三个阶段的胶囊是用来提示「这个项目还缺哪一关」的，所以只在项目确实走
    // 三阶段审批轨道（含有建议书/可研/初设之一的批文）时才显示这三个占位。
    // 核准/备案类项目本来就没有这三关，给它们挂三个「未上传」等于谎报缺失。
    // 实际出现过的阶段一律补上：只列固定三个的话，一份「待确认」批文会显示成
    // 三个「未上传」，看着像这个项目没有文件。
    const detected = [...new Set(item.documents.map(d => d.stage))];
    const canonical = (item.review?.stages || []).map(s => s.name);
    const base = detected.some(s => canonical.includes(s)) ? canonical : [];
    const stages = base.concat(detected.filter(n => !base.includes(n)))
      .map(name => ({name, count: item.documents.filter(d => d.stage === name).length}));
    for (const s of stages) {
      const doc = item.documents.find(d => d.stage === s.name);
      const selected = !!doc && state.docId === doc.id;
      const chip = node('button', undefined, 'stage-chip' + (s.count ? ' filled' : '') + (selected ? ' sel' : ''));
      chip.append(node('span', STAGE_SHORT[s.name] || s.name), node('b', s.count ? String(s.count) : '未上传'));
      if (doc) {
        // 点胶囊直接跳到该阶段的单文档视图；再点一次回到阶段对照。
        chip.title = '查看 ' + s.name + ' · ' + doc.filename;
        chip.onclick = () => {state.key=item.key;state.docId = selected ? '' : doc.id;state.selection=null;render();resetEvidence();};
      } else {
        chip.disabled = true;chip.title = s.name + ' 尚未上传';
      }
      chips.append(chip);
    }
    card.append(chips);

    const rv = item.review;
    if (rv) {
      const done = Math.max(0, rv.touched || 0);
      const total = done + (rv.pending || 0);
      const bar = node('div', undefined, 'project-progress');
      const fill = node('span', undefined, rv.pending === 0 ? 'done' : '');
      fill.style.width = (total ? Math.round(done / total * 100) : (rv.pending === 0 ? 100 : 0)) + '%';
      bar.append(fill);card.append(bar);
      const meta = node('div', undefined, 'project-meta');
      meta.append(node('span', item.documents.length + ' 份 · ' + new Set(item.documents.map(d=>d.stage)).size + ' 个阶段'));
      meta.append(node('span', rv.pending === 0 ? '待核对项已处理' : '还剩 ' + rv.pending + ' 项待核对',
        rv.pending === 0 ? 'done' : 'warn'));
      card.append(meta);
    }
    $('projects').append(card);
  }
  renderTrash();
  $('empty').hidden=!!g; $('tableWrap').hidden=!g; $('warnings').hidden=true;
  $('documentSelect').replaceChildren(option('','项目阶段对照'));
  if (!g) {resetEvidence();renderReview(null);renderAgent(null);return;}
  renderReview(g);
  for (const d of g.documents) $('documentSelect').append(option(d.id,'单文档 · '+d.stage+' · '+d.filename));
  $('documentSelect').value=state.docId; $('singleTools').hidden=!state.docId;
  if (state.docId) $('stageSelect').value=g.documents.find(d=>d.id===state.docId).stage;
  $('projectTitle').textContent=g.name;
  $('projectCode').textContent=g.key.startsWith('unassigned:')?'项目代码未载明，暂不自动合并':'项目代码 '+g.key;
  $('metricCount').textContent=g.rows.filter(r=>r.kind==='metric').length;
  const indices=g.documents.map((d,i)=>i).filter(i=>!state.docId||g.documents[i].id===state.docId);
  const table=node('table'), thead=node('thead'), header=node('tr');
  header.append(node('th','提取字段'));
  for (const i of indices) {
    const d=g.documents[i],th=node('th',d.stage);
    th.append(node('small',d.fields['发文字号'].value||'文号待确认'),node('small',d.filename)); header.append(th);
  }
  thead.append(header); table.append(thead);
  const tbody=node('tbody');
  const visibleRows=g.rows.filter(r=>r.kind===state.kind&&matchesFilter(r));
  for (const row of visibleRows) {
    const tr=node('tr'),name=node('td',row.name);
    name.append(node('span',state.docId?'单文档提取':labels[row.status],'row-note'));
    tr.append(name);
    for (const i of indices) {
      const c=row.cells[i];
      // 「原文确实没有」与「系统没抽到」必须分开显示：混在一起时，正确的缺失
      // 看起来像系统故障（实测庆元三份的印发机关连开三个空格子），而真正的漏抽
      // 又淹没在同样的灰格里没人处理。
      const status=['needs_review','conflict','uncertain'].includes(c.status)?'review'
        :(c.value==null&&c.reason==='unextracted')?'unextracted'
        :c.value==null?'missing':state.docId?'same':row.status;
      const td=node('td',undefined,status),b=node('button',undefined,'cell-button');
      const blank=c.status==='uncertain'?'识别不确定':(c.reason==='unextracted'?'未提取到（原文有线索）':'原文未载明');
      b.append(node('span',c.value??blank,'cell-value'));
      b.append(node('small',c.evidence.length?'查看原文 · 第 '+[...new Set(c.evidence.map(e=>e.page))].join('、')+' 页':'无可定位证据'));
      b.dataset.documentId=g.documents[i].id;b.dataset.field=row.name;
      b.setAttribute('aria-pressed',String(state.selection?.doc.id===g.documents[i].id&&state.selection?.row.name===row.name));
      b.onclick=()=>selectCell(g.documents[i],row,c);td.append(b);tr.append(td);
    }
    tbody.append(tr);
    if(row.note&&!state.docId){const noteRow=node('tr'),noteCell=node('td',row.note,'comparison-note');noteCell.colSpan=indices.length+1;noteRow.append(noteCell);tbody.append(noteRow);}
  }
  if(!visibleRows.length){const tr=node('tr'),td=node('td','当前范围没有符合筛选条件的结果。可切换基本信息 / 建设指标，或选择全部结果。','filter-empty');td.colSpan=indices.length+1;tr.append(td);tbody.append(tr);}
  table.append(tbody);$('tableWrap').replaceChildren(table);
  const warnings=g.documents.flatMap(d=>d.warnings.map(w=>d.filename+'：'+w)),counts={};
  g.documents.forEach(d=>counts[d.stage]=(counts[d.stage]||0)+1);
  Object.entries(counts).forEach(([k,n])=>{if(n>1)warnings.push(k+'有多份批复，已保留独立列，请在单文档视图确认阶段。');});
  if (warnings.length) {
    const details=node('details'),ul=node('ul');details.append(node('summary','处理提示 · '+warnings.length+' 项'));
    warnings.forEach(w=>ul.append(node('li',w)));details.append(ul);$('warnings').replaceChildren(details);$('warnings').hidden=false;
  }
  renderAgent(g);
}
// 待办总览：把「还需要人工判断」的值收成一份可逐条确认的清单，并给出项目级进度。
// 这是本系统最缺的一条主线——原先只有单个格子的颜色，用户永远不知道什么时候能收工。
function renderReview(g) {
  const box=$('reviewPanel');
  if (!g || !g.review) {box.hidden=true;return;}
  const r=g.review;
  box.replaceChildren();box.hidden=false;
  const head=node('div',undefined,'review-head');
  head.append(node('span','核对进度'),
    node('span',r.pending===0?'待核对项已处理':r.status+' · '+r.label,'progress'+(r.pending===0?' done':'')));
  box.append(head);
  if (r.pending===0) {
    box.append(node('p','当前待核对项已处理。材料是否齐全请看阶段标记，导出前仍可抽查结果。','review-empty'));
  } else {
    const start=node('button','开始 / 继续核对','primary');start.onclick=()=>nextReview();box.append(start);
    const detail=node('details');detail.append(node('summary','查看待核对清单（'+r.pending+'项）'));
    const ul=node('ul',undefined,'review-list');
    for (const a of (r.alignments||[])) {
      const li=node('li');
      const names=a.names||[];
      li.append(node('span','指标对齐','doc'),
                node('span',names.join(' ↔ ')+'（均为 '+(a.value||'同一数值')+'）','val'));
      // 合并/不合并必须都能落地：只在表里标黄而不给处理入口，这一项会永远
      // 留在待办里，项目也就永远到不了「已审完」。
      for (const target of names) {
        const other=names.find(n=>n!==target);
        const holder=(a.holders||[]).find(h=>h.name===other);
        if (!holder) continue;
        const b=node('button','统一为「'+target+'」');
        b.onclick=()=>resolveAlign(holder,other,target,'merge');
        li.append(b);
      }
      const keepHolder=(a.holders||[])[0];
      if (keepHolder) {
        const n=keepHolder.name, other=names.find(x=>x!==n)||'';
        const k=node('button','确认不合并');
        k.onclick=()=>resolveAlign(keepHolder,n,other,'keep');
        li.append(k);
      }
      li.append(node('span',a.why,'why'));
      ul.append(li);
    }
    for (const d of r.documents) for (const it of d.items) {
      const li=node('li');
      const btn=node('button','去核对');btn.onclick=()=>gotoItem(g,d.id,it);
      li.append(node('span',d.stage,'doc'),node('span',it.name+'：'+(it.value??'（空）'),'val'),btn,
                node('span',it.why,'why'));
      ul.append(li);
    }
    detail.append(ul);box.append(detail);
  }
  const absent=r.documents.reduce((n,d)=>n+d.absent.length,0);
  if (absent) box.append(node('p','另有 '+absent+' 个空值未检出相关线索，暂不计入待办；这不等于人工确认原文未载明，交付前请对照原文抽查。','review-note'));
}
function gotoItem(g,docId,it) {
  state.docId=docId;
  if(it.kind==='stage'){render();const doc=g.documents.find(d=>d.id===docId),row=g.rows.find(r=>r.name==='标题');if(doc&&row)selectCell(doc,row,row.cells[g.documents.indexOf(doc)]);else resetEvidence();$('stageSelect').focus();toast('请选择审批阶段，再点击确认阶段。');return;}
  state.kind=it.kind;$('rowFilter').value='all';
  $('fixedTab').setAttribute('aria-selected',String(it.kind==='fixed'));$('metricsTab').setAttribute('aria-selected',String(it.kind==='metric'));render();
  const doc=g.documents.find(d=>d.id===docId);
  const row=g.rows.find(r=>r.kind===it.kind&&r.name===it.name);
  if (!doc||!row) return;
  selectCell(doc,row,it.kind==='metric'?doc.metrics[it.index]:row.cells[g.documents.indexOf(doc)],it.index);
}
// 处理「疑似同一指标」：merge 把本文件里的指标改名为对方名称（两行合并），
// keep 只记录「确实不是同一指标」的判断。两者都会让该项目少一项待办。
async function resolveAlign(holder,name,other,decision) {
  try {
    const doc=await api('/approval/api/documents/'+holder.doc);
    await api('/approval/api/documents/'+holder.doc+'/review',{kind:'align',name,other,decision,value:'',
      reason:decision==='merge'?'跨阶段指标名称对齐：合并为同一指标':'跨阶段指标名称对齐：确认不是同一指标',
      revision:doc.revision??0});
    await refresh();
    toast(decision==='merge'
      ? '已把「'+name+'」统一为「'+other+'」，比对已重新计算。'
      : '已确认两者不是同一指标，该项不再计入待办。');
  } catch(e) {toast(e.message);}
}
// 自检循环的记录：用户能看见系统在想什么，「流程不直观」才有解。
function renderAgent(g) {
  const box=$('agentPanel');
  const doc=g&&state.docId?g.documents.find(d=>d.id===state.docId):null;
  const agent=doc&&doc.agent;
  $('agentDetails').hidden=!agent;
  if (!agent) {box.hidden=true;return;}
  box.replaceChildren();box.hidden=false;
  const h=node('h4','自检循环记录');
  const words={ok:'已收敛',noted:'有提示',escalated:'转人工复核'};
  h.append(node('span',words[agent.status]||agent.status,'agent-status '+(agent.status||'noted')));
  box.append(h);
  if (!(agent.trace||[]).length) {
    box.append(node('p','本次未发现异常，未执行任何补救动作。','muted'));
  } else {
    const ol=node('ol');
    for (const t of agent.trace) {
      const li=node('li');
      li.append(node('span','发现「'+(t.issues||[]).join('、')+'」→ 执行「'+(actionLabels[t.action]||t.action)
        +'」（'+(t.decided_by==='llm'?'模型决策':'规则决策')+'）→ '+t.note));
      if (t.reason) li.append(node('span','；'+t.reason,'why'));
      ol.append(li);
    }
    box.append(ol);
  }
  const left=(agent.issues||[]);
  if (left.length) box.append(node('p','仍需人工处理：'+left.map(i=>i.detail).join('；'),'why'));
}
function resetEvidence() {state.selection=null;document.querySelectorAll('.cell-button').forEach(b=>b.setAttribute('aria-pressed','false'));$('evidenceEmpty').hidden=false;$('evidenceContent').hidden=true;$('evidenceStatus').textContent='选择表格中的字段';}
function selectCell(doc,row,c,index=null) {
  state.selection={doc,row,c,index:index??(row.kind==='metric'?doc.metrics.findIndex(m=>m.name===row.name):null)};
  document.querySelectorAll('.cell-button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.documentId===doc.id&&b.dataset.field===row.name)));
  $('evidenceEmpty').hidden=true;$('evidenceContent').hidden=false;
  $('selectedField').textContent=row.name+' / '+doc.stage;$('selectedValue').textContent=c.value??(c.status==='uncertain'?'识别不确定':'原文未载明');
  $('evidenceStatus').textContent=['needs_review','conflict','uncertain'].includes(c.status)?'需要人工确认':c.status==='reviewed'?'人工已复核':'原文证据';
  $('humanNote').textContent=methodNote(row,c);
  $('variants').replaceChildren();$('editButton').disabled=false;
  $('confirmNext').disabled=!c.value||!c.evidence.length||['conflict','uncertain'].includes(c.status);
  $('confirmNext').title=$('confirmNext').disabled?'缺失、冲突或无证据时请使用修改核对':'';
  if (row.kind==='metric') {
    const matches=doc.metrics.map((m,i)=>({m,i})).filter(({m})=>m.name===row.name);
    if (matches.length>1) {
      $('editButton').disabled=index===null;
      if(index===null)$('confirmNext').disabled=true;
      for (const {m,i} of matches) {const b=node('button','选择值：'+m.value,'source-link');b.onclick=()=>selectCell(doc,row,m,i);$('variants').append(b);}
    }
  }
  $('historyList').replaceChildren();
  const history=(doc.history||[]).filter(h=>h.name===row.name||h.after_name===row.name);
  if (!history.length) $('historyList').append(node('p','暂无修订记录','muted'));
  for(const h of history.slice().reverse()) {
    const value=typeof h.before==='object'?h.before.value:h.before;
    $('historyList').append(node('p',new Date(h.time*1000).toLocaleString()+'：'+(value??'未载明')+' → '+h.after+(h.reason?'；'+h.reason:''),'muted'));
  }
  $('sourceLinks').replaceChildren();
  const pages=[...new Set(c.evidence.map(e=>e.page))];
  pages.forEach(p=>{const b=node('button','第 '+p+' 页','source-link');b.onclick=()=>showPage(p);$('sourceLinks').append(b);});
  showPage(pages[0]||1);
  if(window.matchMedia?.('(max-width:1200px)').matches)$('evidenceContent').scrollIntoView({block:'start',behavior:'smooth'});
}
function showPage(n) {
  const sel=state.selection;if(!sel)return;
  state.page=Math.max(1,Math.min(n,sel.doc.pages.length));
  const p=sel.doc.pages[state.page-1],ev=sel.c.evidence.filter(e=>e.page===state.page);
  $('pageLabel').textContent='第 '+state.page+' / '+sel.doc.pages.length+' 页';
  $('prevPage').disabled=state.page===1;$('nextPage').disabled=state.page===sel.doc.pages.length;
  $('pageImage').src='/approval/api/documents/'+sel.doc.id+'/pages/'+state.page+'.png';
  $('pageImage').onerror=()=>toast('页面加载失败，请重试。');$('highlights').replaceChildren();
  for (const e of ev) {
    const [x0,y0,x1,y1]=e.bbox,h=node('div',undefined,'highlight');
    Object.assign(h.style,{left:(x0/p.width*100)+'%',top:(y0/p.height*100)+'%',width:((x1-x0)/p.width*100)+'%',height:((y1-y0)/p.height*100)+'%'});
    $('highlights').append(h);
  }
  $('sourceQuote').textContent=ev.length?ev.map(e=>e.quote).join(''):'本页无该字段证据。缺失信息不会从其他批文补填。';
}
function toBase64(file) {return new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(file);});}
function selectResult(docid){
  const g=state.groups.find(g=>g.documents.some(d=>d.id===docid));if(!g)return;
  state.key=g.key;state.docId=docid;$('rowFilter').value='all';resetEvidence();render();
  $('projectTitle').scrollIntoView({block:'nearest'});
}
function renderJobPanel(){
  const panel=$('jobPanel'),job=latestJobs[latestJobs.length-1];
  panel.hidden=!job;if(!job)return;
  const opened=panel.querySelector('details')?.open;
  panel.replaceChildren();const details=node('details');details.open=opened??job.status==='running';
  const counts={success:0,duplicate:0,failed:0};
  for(const file of job.files||[])if(file.status in counts)counts[file.status]++;
  details.append(node('summary','最近批次 · '+(job.status==='running'?'处理中':job.status==='interrupted'?'已中断':'处理结束')+' · 新增/更新 '+counts.success+' · 重复 '+counts.duplicate+' · 失败 '+counts.failed));
  const list=node('ol','', 'job-files');
  for(const file of job.files||[]){
    const li=node('li',undefined,'job-file '+file.status);
    li.append(node('strong',file.filename),node('span',file.step));
    if(file.detail)li.append(node('small',file.detail));
    if(file.document_id){const view=node('button','查看结果');view.onclick=()=>selectResult(file.document_id);li.append(view);}
    if(['failed','interrupted'].includes(file.status)){const retry=node('button','重新选择文件');retry.onclick=()=>$('fileInput').click();li.append(retry);}
    list.append(li);
  }
  details.append(list);
  if(job.status==='interrupted')details.append(node('p','服务曾重启。已保存的结果仍可使用，请重新选择未完成文件；重复文件会自动跳过。','muted'));
  panel.append(details);
}
async function watchJob(jid) {
  if(watchingJob)return;
  watchingJob=true;sessionStorage.setItem('activeJob',jid);$('resumeJob').hidden=true;progressDisconnected=false;
  $('progress').hidden=false;let job;
  try{
    do{
      job=await api('/approval/api/jobs/'+jid);
      const i=latestJobs.findIndex(j=>j.id===jid);if(i>=0)latestJobs[i]=job;else latestJobs.push(job);
      $('progress').textContent=(job.status==='running'?'处理中 ':'已处理 ')+job.done+' / '+job.total+' 份 · '+(job.step||'等待处理')+' · '+(job.current_file||'');
      renderSummary();renderJobPanel();
      if(job.status==='running')await new Promise(r=>setTimeout(r,1000));
    }while(job.status==='running');
    await refresh();
    if(job.errors.length)toast('部分文件未完成。展开最近批次可查看原因并重新选择文件。');
    else if(job.status==='interrupted')toast('任务因服务重启中断，请查看最近批次。');
    else toast('文件处理结束，请继续核对提示项。');
    // 释放守卫与清除 activeJob 要相邻且不可有 await：外部一旦看到 activeJob 已清，
    // 就说明本次轮询已彻底结束，这时点「重新连接」必须能生效。
    watchingJob=false;sessionStorage.removeItem('activeJob');
  }catch(e){
    // 断线时保留 activeJob——它就是「重新连接」的依据；只有记录确实过期才清掉。
    if(e.status===404){sessionStorage.removeItem('activeJob');$('progress').textContent='该批次记录已过期，请刷新查看已保存结果。';}
    else if(job&&job.status!=='running'){
      // 任务其实已经跑完，失败的只是收尾刷新。这时还说「后台任务可能仍在继续」
      // 会把人引去重传文件，而实际只要刷新一下页面。
      $('progress').textContent='任务已完成，但界面刷新失败，请手动刷新页面查看结果。';
    }
    else{progressDisconnected=true;$('progress').textContent='进度连接中断，后台任务可能仍在继续。请重新连接，不必重复上传。';$('resumeJob').hidden=false;}
    toast(e.message);
  }finally{watchingJob=false;}
  const next=latestJobs.find(j=>j.status==='running'&&j.id!==jid);if(next&&!progressDisconnected)await watchJob(next.id);
}
$('resumeJob').onclick=async()=>{const jid=sessionStorage.getItem('activeJob');if(jid)await watchJob(jid);else await refresh();};
async function upload() {
  const files=[...$('fileInput').files];if(!files.length)return;
  if(files.some(f=>!f.name.toLowerCase().endsWith('.pdf'))){toast('请选择 PDF 文件。');return;}
  if(files.length>10||files.some(f=>f.size>20*1024*1024)||files.reduce((n,f)=>n+f.size,0)>23*1024*1024){toast('每批最多10份，单份20MB，批次总计23MB以内。');return;}
  $('uploadButton').disabled=true;$('emptyUpload').disabled=true;$('progress').hidden=false;$('progress').textContent='正在读取文件…';
  try {
    const payload=[];for(const f of files)payload.push({name:f.name,data:await toBase64(f)});
    const r=await api('/approval/api/upload',{files:payload,use_llm:$('useModel').checked});
    await watchJob(r.job_id);
  } catch(e) {toast(e.message);$('progress').textContent='未能确认上传结果，请刷新查看最近批次后再决定是否重传。';}
  finally {$('uploadButton').disabled=false;$('emptyUpload').disabled=false;$('fileInput').value='';}
}
// ── 回收站与破坏性操作 ────────────────────────────────────
// 删除一律只做「移入回收站」：人工核对结果（修订值、证据绑定、修订历史）都只存在
// 服务端的 <id>.json 里，没有第二处可查。彻底删除是回收站里的第二个动作，且不可逆。
function timeAgo(ts) {
  const s = Math.max(0, Date.now()/1000 - (ts || 0));
  if (s < 60) return '刚刚';
  if (s < 3600) return Math.round(s/60) + ' 分钟前';
  if (s < 86400) return Math.round(s/3600) + ' 小时前';
  return Math.round(s/86400) + ' 天前';
}
function renderTrash() {
  const list = $('trashList');list.replaceChildren();
  $('trashCount').textContent = state.trash.length;
  $('restoreAll').disabled = !state.trash.length;
  $('purgeTrash').disabled = !state.trash.length;
  if (!state.trash.length) {list.append(node('p','回收站是空的','muted'));return;}
  for (const item of state.trash) {
    const box = node('div', undefined, 'trash-item' + (item.intact ? '' : ' broken'));
    const meta = node('div', undefined, 'trash-meta');
    meta.append(node('strong', item.filename || item.id.slice(0,8)));
    const bits = [];
    if (item.project_name) bits.push(item.project_name);
    if (item.stage) bits.push(item.stage);
    if (item.revisions) bits.push('已核对 ' + item.revisions + ' 处');
    bits.push('删除于 ' + timeAgo(item.deleted_at));
    // 回收站里的 json 是唯一副本，缺文件要明说，不能让人以为还能恢复出完整结果。
    if (!item.intact) bits.push('文件不完整，恢复后需重新提取');
    meta.append(node('small', bits.join(' · ')));
    const acts = node('div', undefined, 'trash-actions');
    const back = node('button','恢复');back.onclick = () => restoreTrash([item.id]);
    const gone = node('button','彻底删除','danger-ghost');gone.onclick = () => purgeTrash([item.id]);
    acts.append(back,gone);box.append(meta,acts);list.append(box);
  }
}
function confirmAction(title,text,note,okLabel) {
  return new Promise(resolve => {
    const dlg = $('confirmDialog');
    $('confirmTitle').textContent = title;
    $('confirmText').textContent = text;
    const noteEl = $('confirmNote');noteEl.textContent = note || '';noteEl.hidden = !note;
    const ok = $('confirmOk'), cancel = $('confirmCancel');
    ok.textContent = okLabel || '确认';
    const finish = value => {ok.onclick = null;cancel.onclick = null;dlg.close();resolve(value);};
    ok.onclick = () => finish(true);
    cancel.onclick = () => finish(false);
    dlg.showModal();
  });
}
async function deleteDocuments(ids, scope, label, extra) {
  const r = await api('/approval/api/documents/delete', {ids, scope, ...(extra || {})});
  await refresh();
  toast((label || '已移入回收站') + '：' + r.moved + ' 份。可在回收站恢复。');
  return r;
}
async function deleteProject(g) {
  const ids = g.documents.map(d => d.id);
  const ok = await confirmAction('移出这个项目？', '「' + g.name + '」的 ' + ids.length + ' 份批文将移入回收站。',
    '可随时从回收站恢复，人工核对结果会一并保留。','移入回收站');
  if (!ok) return;
  try {await deleteDocuments(ids,'project','已移出「' + g.name + '」');} catch(e) {toast(e.message);}
}
async function deleteSelectedDoc() {
  const doc = group()?.documents.find(d => d.id === state.docId);
  if (!doc) return;
  const ok = await confirmAction('移入回收站？', doc.stage + ' · ' + doc.filename + ' 将移入回收站。',
    '可随时从回收站恢复，人工核对结果会一并保留。','移入回收站');
  if (!ok) return;
  state.docId = '';
  try {await deleteDocuments([doc.id],'document');} catch(e) {toast(e.message);}
}
async function clearAllDocuments() {
  const total = state.groups.reduce((a,g) => a + g.documents.length, 0);
  if (!total) return;
  const ok = await confirmAction('清空全部文件？',
    '当前 ' + state.groups.length + ' 个项目的 ' + total + ' 份批文将全部移入回收站。',
    '移入回收站即可恢复；只有在回收站里点「彻底删除」才会真正删掉。','全部移入回收站');
  if (!ok) return;
  try {
    // 口令里带着份数，服务端会核对：若确认期间又有文件传进来，它会拒绝而不是多删。
    await deleteDocuments(state.groups.flatMap(g => g.documents.map(d => d.id)), 'all', '已清空',
      {confirm: 'DELETE-' + total});
  } catch(e) {
    await refresh();toast(e.message + '（已刷新，请重试）');
  }
}
async function restoreTrash(ids) {
  try {
    const r = await api('/approval/api/trash/restore', {ids});
    await refresh();toast('已恢复 ' + r.restored + ' 份。');
  } catch(e) {toast(e.message);}
}
async function purgeTrash(ids) {
  const n = ids ? ids.length : state.trash.length;
  if (!n) return;
  const ok = await confirmAction('彻底删除？',
    (ids ? '选中的 ' + n + ' 份' : '回收站里的 ' + n + ' 份') + '将被永久删除，无法恢复。',
    '人工核对结果会一起消失。如果只想让它从列表里消失，用「移入回收站」就够了。','永久删除');
  if (!ok) return;
  try {
    const r = await api('/approval/api/trash/purge', {confirm:'PURGE', ...(ids ? {ids} : {})});
    await refresh();toast('已永久删除 ' + r.purged + ' 份。');
  } catch(e) {toast(e.message);}
}
function renderEvidencePicker() {
  const doc=state.selection.doc,page=Number($('evidencePageSelect').value);$('evidencePicker').replaceChildren();
  const lines=doc.lines.filter(l=>l.page===page).map(l=>({id:l.id,text:l.text}));
  for(const e of doc.fields['印章'].evidence) if(e.page===page)lines.push({id:e.id,text:e.quote});
  for(const l of lines) {
    const label=node('label'),check=node('input');check.type='checkbox';check.checked=state.evidenceIds.has(l.id);
    check.onchange=()=>check.checked?state.evidenceIds.add(l.id):state.evidenceIds.delete(l.id);
    label.append(check,node('span',l.text));$('evidencePicker').append(label);
  }
}
$('uploadButton').onclick=$('emptyUpload').onclick=()=>$('fileInput').click();$('fileInput').onchange=upload;
$('clearAll').onclick=clearAllDocuments;
$('deleteDoc').onclick=deleteSelectedDoc;
$('restoreAll').onclick=()=>restoreTrash(null);
$('purgeTrash').onclick=()=>purgeTrash(null);
$('documentSelect').onchange=()=>{state.docId=$('documentSelect').value;render();resetEvidence();};
$('saveStage').onclick=async()=>{try{const doc=group().documents.find(d=>d.id===state.docId);await api('/approval/api/documents/'+doc.id+'/review',{kind:'stage',name:'审批阶段',value:$('stageSelect').value,revision:doc.revision??0});await refresh();toast('审批阶段已确认。');nextReview();}catch(e){toast(e.message);}};
$('reprocess').onclick=async()=>{const b=$('reprocess');b.disabled=true;try{const r=await api('/approval/api/documents/'+state.docId+'/reprocess',{use_llm:$('useModel').checked});await watchJob(r.job_id);resetEvidence();}catch(e){toast(e.message);}finally{b.disabled=false;}};
$('fixedTab').onclick=()=>{state.kind='fixed';$('fixedTab').setAttribute('aria-selected','true');$('metricsTab').setAttribute('aria-selected','false');render();};
$('metricsTab').onclick=()=>{state.kind='metric';$('fixedTab').setAttribute('aria-selected','false');$('metricsTab').setAttribute('aria-selected','true');render();};
$('rowFilter').onchange=render;$('prevPage').onclick=()=>showPage(state.page-1);$('nextPage').onclick=()=>showPage(state.page+1);
$('editButton').onclick=()=>{
  if(!state.selection)return;
  $('metricNameLabel').hidden=state.selection.row.kind!=='metric';$('metricName').value=state.selection.row.name;
  $('editLabel').textContent=state.selection.row.name;$('editValue').value=state.selection.c.value??'';$('editReason').value='';
  state.evidenceIds=new Set(state.selection.c.evidence.map(e=>e.id));$('evidencePageSelect').replaceChildren();
  state.selection.doc.pages.forEach(p=>$('evidencePageSelect').append(option(p.number,'第 '+p.number+' 页')));
  $('evidencePageSelect').value=state.page;renderEvidencePicker();$('editDialog').showModal();
};
$('evidencePageSelect').onchange=renderEvidencePicker;$('cancelEdit').onclick=()=>$('editDialog').close();
async function saveReview(quick=false) {
  if(savingReview||!state.selection)return;
  savingReview=true;$('confirmNext').disabled=true;
  const sel=state.selection;
  try {
    await api('/approval/api/documents/'+sel.doc.id+'/review',{kind:sel.row.kind,name:sel.row.name,index:sel.index,
      metric_name:quick?sel.row.name:$('metricName').value,
      value:quick?sel.c.value:$('editValue').value,reason:quick?'对照原文确认无误':$('editReason').value,
      evidence_ids:quick?sel.c.evidence.map(e=>e.id):[...state.evidenceIds],revision:sel.doc.revision??0});
    if(!quick)$('editDialog').close();
    await refresh();const destination=state.groups.find(g=>g.documents.some(d=>d.id===sel.doc.id));if(destination)state.key=destination.key;state.selection=null;resetEvidence();nextReview();toast('已保存，比较结果和待办已更新。');
  } catch(err){toast(err.message);if(state.selection)selectCell(sel.doc,sel.row,sel.c,sel.index);}
  finally{savingReview=false;}
}
$('editForm').onsubmit=e=>{e.preventDefault();saveReview(false);};
$('confirmNext').onclick=()=>saveReview(true);
function nextReview(){
  const g=group();if(!g)return;
  const d=g.review.documents.find(d=>d.items.length);
  if(d){gotoItem(g,d.id,d.items[0]);return;}
  if(g.review.alignments.length){
    $('reviewPanel').querySelector('details').open=true;
    $('reviewPanel').scrollIntoView({block:'nearest'});toast('请在待核对清单中处理指标名称对齐。');return;
  }
  state.docId='';render();resetEvidence();toast('当前项目待核对项已处理，可查看对照表并导出。');
}
function matchesFilter(row){
  const filter=$('rowFilter').value;
  if(filter==='all')return true;
  if(filter==='review'){
    const g=group();const index=state.docId?g.documents.findIndex(d=>d.id===state.docId):-1;
    const cells=index<0?row.cells:[row.cells[index]];
    return (!state.docId&&row.status==='align')||cells.some(c=>c&&(['needs_review','conflict','uncertain'].includes(c.status)||(c.status==='missing'&&c.reason==='unextracted')));
  }
  return row.status===filter;
}
function renderSummary(){
  const box=$('workSummary');box.replaceChildren();
  const total=state.groups.reduce((n,g)=>n+g.documents.length,0);
  const pending=state.groups.reduce((n,g)=>n+(g.review?.pending||0),0);
  box.append(node('strong','工作进展'),node('span','已入库 '+total+' 份 · '+state.groups.length+' 个项目'),node('span','待核对 '+pending+' 项'));
  const job=latestJobs.find(j=>j.status==='running')||latestJobs[latestJobs.length-1];
  const g=group();
  const flow=node('ol',undefined,'workflow');
  const labels=['上传批文','自动处理','核对提示','查看差异 / 导出'];
  const active=!total?0:g?.review?.pending?2:3;
  labels.forEach((label,index)=>{const li=node('li',(index+1)+' '+label,index===(job?.status==='running'?1:active)?'current':'');if(li.className)li.setAttribute('aria-current','step');flow.append(li);});
  box.append(flow);
  if(job){
    box.append(node('span','最近批次：'+job.done+'/'+job.total+' 份已处理 · 新增/更新 '+job.results.filter(r=>!r.duplicate).length+' · 重复 '+job.results.filter(r=>r.duplicate).length+' · 失败 '+job.errors.length));
    const bar=node('progress');bar.max=job.total||1;bar.value=job.done;bar.setAttribute('aria-label','批次处理进度');box.append(bar);
    if(job.status==='running')box.append(node('span',(job.step||'处理中')+'：'+(job.current_file||'')));
    if(job.errors.length){const details=node('details');details.append(node('summary','查看失败原因'));job.errors.forEach(e=>details.append(node('p',e.filename+'：'+e.message)));box.append(details);}
  }
  if(!total)box.append(node('p','先上传同一项目的批复；无需配置模型也可开始。'));
  else if(g)box.append(node('p',g.review?.pending?'下一步：点击“开始 / 继续核对”，按原文逐项确认。':'下一步：查看阶段差异并导出。待办清零不代表所有审批材料已齐全。'));
}
function downloadExport(format,all=false){
  const g=group();if(!all&&!g)return;
  const a=node('a');a.href='/approval/api/export.'+format+(all?'':'?project='+encodeURIComponent(g.key));a.download='';document.body.append(a);a.click();a.remove();
}
$('exportCurrent').onclick=()=>downloadExport('xlsx');
$('exportAll').onclick=()=>downloadExport('xlsx',true);
$('exportJson').onclick=()=>downloadExport('json');
// Model configuration is managed once, at /settings/model.
function applyModelConfig(config) {
  state.modelReady=!!config.llm_ready;
  $('useModel').disabled=!state.modelReady;
  if(!state.modelReady)$('useModel').checked=false;
  $('engineBadge').textContent=config.llm_ready?'大模型辅助已配置':'本地解析';
  $('modelHelp').textContent=config.llm_ready?'当前文字模型：'+config.model+'。勾选后辅助抽取。':((config.model_error||'尚未配置模型')+'；可在上方「模型设置」中填写保存，也可先使用本地解析。');
}
window.addEventListener('focus',()=>api('/approval/api/config').then(applyModelConfig).catch(()=>{}));
(async()=>{
  try {
    const config=await api('/approval/api/config');state.stages=config.stages;$('stageSelect').replaceChildren(...config.stages.map(s=>option(s,s)));
    applyModelConfig(config);
    await refresh();const jid=latestJobs.find(j=>j.status==='running')?.id||sessionStorage.getItem('activeJob');if(jid)await watchJob(jid);
  }catch(e){toast(e.message);}
})();
