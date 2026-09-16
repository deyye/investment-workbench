"""Local single-user HTTP application. Bind loopback unless secured by a reverse proxy."""
import argparse,base64,hashlib,json,os,re,secrets,shutil,threading,time,uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse,unquote,parse_qs
import fitz
from .extract import extract,FIELDS,STAGES,numeric
from .compare import rows_for,export_xlsx
from .review import update as apply_review
from .queue import project_progress
from . import agent_loop
from .progress import observe
from .model_client import public_config, probe, save_config, clear_config, set_config_path, ModelError

ROOT=Path(__file__).resolve().parent.parent

# 任务表（s.jobs）是纯内存结构，不清会随运行时间单调增长，重启又会全丢。
# 只保留最近这么多条已结束的任务。
MAX_KEPT_JOBS=50

def diagnose(result,path,name,doc_id):
    """按需启动自检循环，并保证它失败时不影响主流程。

    只在自检发现异常时才跑：实测 6 份真实批复主干全部正常、自检零信号，
    循环一次都不会启动；若每份都跑，放宽形态带来的认错风险会摊到全部文档。
    """
    try:
        if agent_loop.should_run(result):
            result=agent_loop.run_agent(path,name,doc_id,result=result)
    except Exception as exc:
        result.setdefault('warnings',[]).append('自检循环执行失败，已保留本地提取结果：'+type(exc).__name__)
    return result

def load_env():
    p=ROOT/'.env'
    if p.exists():
        for line in p.read_text(encoding='utf-8').splitlines():
            line=line.strip()
            if line and not line.startswith('#') and '=' in line:
                k,v=line.split('=',1)
                if re.fullmatch(r'[A-Z][A-Z0-9_]*',k):os.environ.setdefault(k,v.strip().strip('"').strip("'"))

class Store:
    def __init__(self,path):
        self.path=Path(path);self.path.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.model_probe_lock=threading.Lock();self.jobs={};self.executor=ThreadPoolExecutor(max_workers=1)
        self.job_path=self.path/"jobs"/"state.json"
        if self.job_path.exists():
            try:self.jobs=json.loads(self.job_path.read_text(encoding="utf-8"))
            except (OSError,ValueError):self.jobs={}
            for job in self.jobs.values():
                if job.get("status")=="running":
                    job.update(status="interrupted",step="服务重启，任务已中断",current_file="")
                    job["done"]=sum(f["status"] in ("success","duplicate","failed") for f in job.get("files",[]))
                    for item in job.get("files",[]):
                        if item["status"] in ("queued","running"):item.update(status="interrupted",step="请重新上传该文件",detail="已保存的文件和人工修订仍保留")
            self.persist_jobs()
    def persist_jobs(self):
        with self.lock:
            self.job_path.parent.mkdir(parents=True,exist_ok=True)
            temp=self.job_path.with_suffix('.tmp')
            temp.write_text(json.dumps(self.jobs,ensure_ascii=False),encoding='utf-8')
            temp.replace(self.job_path)
    def new_job(self,names):
        with self.lock:
            jid=uuid.uuid4().hex
            self.jobs[jid]={'id':jid,'status':'running','step':'等待处理','total':len(names),'done':0,
                'created_at':time.time(),'updated_at':time.time(),'results':[],'errors':[],
                'files':[{'filename':n,'status':'queued','step':'等待处理','detail':''} for n in names]}
            self.prune_jobs();self.persist_jobs()
            return jid
    def progress(self,job,index,step,detail=''):
        with self.lock:
            job.update(step=step,updated_at=time.time())
            if job.get('files'):
                item=job['files'][index];item.update(step=step,detail=detail,status='running')
                job['current_file']=item['filename']
            # Persist at file boundaries, not every OCR page.
    def extract_job(self,job,index,path,name,docid,llm):
        with observe(lambda step,detail:self.progress(job,index,step,detail)):
            result=extract(path,name,docid,llm)
        self.progress(job,index,'校验与自检','核验原文证据，必要时定点重读')
        result=diagnose(result,path,name,docid)
        self.progress(job,index,'保存结果','保存后即可查看原文并人工核对')
        return result
    def finish_file(self,job,index,status,detail='',docid=None):
        with self.lock:
            if job.get('files'):
                job['files'][index].update(status=status,step={'success':'处理完成','duplicate':'已存在，未重复解析','failed':'处理失败'}[status],detail=detail,document_id=docid)
            job['updated_at']=time.time()
            self.persist_jobs()
    def snapshot_jobs(self,jid=None):
        with self.lock:
            value=self.jobs[jid] if jid else list(self.jobs.values())
            return json.loads(json.dumps(value))
    def list(self):
        # 只认文档文件。data/ 里还住着 model_config.json 等其它 json，
        # 用 glob('*.json') 一网打尽会把配置当文档读进来，/api/documents
        # 随即在取 project_key 时 KeyError，界面上只看到「请求参数无效」。
        # 文档文件名恒为 32 位十六进制，与 get()/save() 的判据保持一致。
        with self.lock:
            return [json.loads(p.read_text(encoding='utf-8'))
                    for p in sorted(self.path.glob('*.json'))
                    if re.fullmatch(r'[0-9a-f]{32}\.json',p.name)]
    def get(self,i):
        if not re.fullmatch(r'[0-9a-f]{32}',i):raise ValueError('文件编号无效')
        with self.lock:return json.loads((self.path/(i+'.json')).read_text(encoding='utf-8'))
    def save(self,d):
        with self.lock:
            p=self.path/(d['id']+'.json');temp=p.with_suffix('.tmp')
            temp.write_text(json.dumps(d,ensure_ascii=False),encoding='utf-8');temp.replace(p)

    # ── 回收站 ────────────────────────────────────────────────
    # 删除不做硬删。核验场景的核心资产是人工核对结果（修订值、证据绑定、修订历史），
    # 它们只在 <id>.json 里，删掉这份 json 就没有第二处可查。所以删除一律改成
    # 「移入 data/.trash/<时间戳>-<id>/」，先可恢复，再由用户显式彻底删除。
    TRASH_DIR='.trash'

    def trash_root(self):
        return self.path/self.TRASH_DIR

    def list_trash(self):
        with self.lock:
            root=self.trash_root()
            if not root.exists():return []
            out=[]
            for d in sorted(root.iterdir(),reverse=True):
                m=re.fullmatch(r'(\d+(?:\.\d+)?)-([0-9a-f]{32})',d.name) if d.is_dir() else None
                if not m:continue
                at,i=float(m.group(1)),m.group(2)
                item={'id':i,'dir':d.name,'deleted_at':at}
                src=d/(i+'.json')
                if src.exists():
                    try:
                        doc=json.loads(src.read_text(encoding='utf-8'))
                        item.update(filename=doc.get('filename'),project_name=doc.get('project_name'),
                                    project_key=doc.get('project_key'),stage=doc.get('stage'),
                                    name=doc.get('fields',{}).get('项目名称',{}).get('value'),
                                    revisions=len(doc.get('history') or []))
                    except (OSError,ValueError):pass
                # 回收站里的 json 是唯一副本，损坏或缺失都要显式标出，不能装作没这条。
                item['intact']=src.exists() and (d/(i+'.pdf')).exists()
                out.append(item)
            return out

    def trash(self,ids):
        """把文档移入回收站。返回实际移走的编号（已在回收站或不存在的不算）。"""
        moved=[]
        with self.lock:
            for i in ids:
                if not re.fullmatch(r'[0-9a-f]{32}',i):raise ValueError('文件编号无效')
                src=self.path/(i+'.json')
                if not src.exists():continue
                dest=self.trash_root()/f'{time.time():.6f}-{i}'
                dest.mkdir(parents=True,exist_ok=True)
                src.replace(dest/(i+'.json'))
                pdf=self.path/(i+'.pdf')
                if pdf.exists():pdf.replace(dest/(i+'.pdf'))
                moved.append(i)
            return moved

    def restore(self,ids=None):
        """把回收站里的文档放回原位。ids 为空表示全部恢复。"""
        moved=[]
        with self.lock:
            root=self.trash_root()
            if not root.exists():return moved
            for d in sorted(root.iterdir()):
                m=re.fullmatch(r'\d+(?:\.\d+)?-([0-9a-f]{32})',d.name) if d.is_dir() else None
                if not m:continue
                i=m.group(1)
                if ids and i not in ids:continue
                src=d/(i+'.json')
                if not src.exists():continue
                new_id=i
                target=self.path/(i+'.json')
                if target.exists():
                    # 原编号已被占用时给恢复的这份换一个新编号。
                    # 不能叫 <旧编号>.restored-xxxx.json：文档编号必须恒为 32 位十六进制，
                    # 否则 Store.get() 的校验过不去，恢复出来的文件会变成谁也读不到。
                    new_id=uuid.uuid4().hex
                    target=self.path/(new_id+'.json')
                    try:
                        doc=json.loads(src.read_text(encoding='utf-8'))
                        doc['id']=new_id
                        target.write_text(json.dumps(doc,ensure_ascii=False),encoding='utf-8')
                        src.unlink()
                    except (OSError,ValueError):
                        target.unlink(missing_ok=True);continue
                else:
                    src.replace(target)
                pdf=d/(i+'.pdf')
                if pdf.exists():pdf.replace(self.path/(new_id+'.pdf'))
                moved.append(new_id)
                try:d.rmdir()
                except OSError:pass
            return moved

    def purge(self,ids=None):
        """彻底删除。ids 为空表示清空整个回收站。只动回收站，不碰其它任何文件。"""
        count=0
        with self.lock:
            root=self.trash_root()
            if not root.exists():return 0
            for d in sorted(root.iterdir()):
                m=re.fullmatch(r'\d+(?:\.\d+)?-([0-9a-f]{32})',d.name) if d.is_dir() else None
                if not m:continue
                if ids and m.group(1) not in ids:continue
                shutil.rmtree(d,ignore_errors=True);count+=1
            return count

    def busy(self):
        with self.lock:return any(j.get('status')=='running' for j in self.jobs.values())

    def prune_jobs(self):
        """只保留最近 MAX_KEPT_JOBS 条已结束的任务，未结束的一律保留。"""
        with self.lock:
            if len(self.jobs)<=MAX_KEPT_JOBS:return
            for jid in list(self.jobs):
                if len(self.jobs)<=MAX_KEPT_JOBS:break
                if self.jobs[jid].get('status')!='running':self.jobs.pop(jid,None)
    def reprocess(self,jid,docid,llm):
        job=self.jobs[jid]
        try:
            original=self.get(docid)
            pdf=self.path/(docid+'.pdf')
            self.progress(job,0,'读取页面','开始重新提取，保留人工修订')
            fresh=self.extract_job(job,0,pdf,original['filename'],docid,llm)
            with self.lock:
                current=self.get(docid)
                for k,old in current['fields'].items():
                    if old.get('method')=='human':fresh['fields'][k]=old
                # Keep the full reviewed metric set: renames and unresolved siblings must survive.
                if any(h.get('kind') in ('metric','align') for h in current.get('history',[])):
                    fresh['metrics']=current['metrics']
                    fresh.setdefault('warnings',[]).append('建设指标已有人工作业，本次重新提取保留完整指标集合；请按需人工更新。')
                if any(h['kind']=='stage' for h in current.get('history',[])):fresh['stage']=current['stage']
                fresh.update(history=current.get('history',[]),revision=current.get('revision',0)+1,sha256=current.get('sha256'),created_at=current.get('created_at'))
                fresh['project_key']=fresh['fields']['项目代码']['value'] or 'unassigned:'+docid
                fresh['project_name']=fresh['fields']['项目名称']['value'] or fresh['filename']
                self.save(fresh)
            job['results'].append({'id':docid,'filename':fresh['filename']})
            self.finish_file(job,0,'success','人工修订已保留',docid)
        except Exception as exc:
            message='重新提取失败，请检查 PDF 或重试：'+type(exc).__name__
            job['errors'].append({'filename':docid,'message':message})
            self.finish_file(job,0,'failed',message)
        finally:
            job.update(done=1,status='completed',current_file='',step='处理结束');self.persist_jobs()

    def run(self,jid,items,llm):
        job=self.jobs[jid]
        known={d.get('sha256'):d for d in self.list() if d.get('sha256')}
        for index,(name,blob) in enumerate(items):
            self.progress(job,index,'检查文件','检查是否已上传')
            try:
                digest=hashlib.sha256(blob).hexdigest()
                existing=known.get(digest)
                if existing:
                    job['results'].append({'id':existing['id'],'filename':name,'duplicate':True})
                    self.finish_file(job,index,'duplicate','如需更新，请在单文档视图选择重新提取',existing['id']);continue
                i=uuid.uuid4().hex;p=self.path/(i+'.pdf');p.write_bytes(blob)
                try:d=self.extract_job(job,index,p,name,i,llm)
                except Exception:
                    p.unlink(missing_ok=True);raise
                d.update(sha256=digest,created_at=time.time(),history=[],revision=0)
                self.save(d);known[digest]=d;job['results'].append({'id':i,'filename':name})
                self.finish_file(job,index,'success','请核对系统提示项后查看阶段差异',i)
            except Exception as e:
                message=str(e) if isinstance(e,ValueError) else '无法解析 PDF，请确认文件能正常打开后重新上传'
                job['errors'].append({'filename':name,'message':message})
                self.finish_file(job,index,'failed',message)
            finally:job['done']+=1
        job['status']='completed';job['step']='处理完成';job['current_file']=''
        self.prune_jobs();self.persist_jobs()

class Handler(BaseHTTPRequestHandler):
    server_version='ApprovalAgent/1.0'
    def log_message(self,*args):pass
    def send(self,data,status=200,ctype='application/json; charset=utf-8',download=None):
        if isinstance(data,(dict,list)):data=json.dumps(data,ensure_ascii=False).encode()
        elif isinstance(data,str):data=data.encode()
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(data)))
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; frame-ancestors 'none'")
        if download:self.send_header('Content-Disposition',f'attachment; filename="{download}"')
        self.end_headers();self.wfile.write(data)
    def do_GET(self):
        try:self.get()
        except (ValueError,KeyError):self.send({'error':'请求参数无效'},400)
        except FileNotFoundError:self.send({'error':'文件不存在'},404)
        except Exception:self.send({'error':'服务处理失败'},500)
    def get(self):
        s=self.server.store;p=urlparse(self.path).path
        if p=='/api/health':return self.send({'status':'ok','version':'2.0'})
        if p=='/api/jobs':return self.send(s.snapshot_jobs())
        if p=='/api/trash':return self.send({'items':s.list_trash()})
        if p=='/api/config':return self.send({**public_config(),'fields':FIELDS,'stages':STAGES})
        if p=='/api/documents':
            ds=s.list();groups={}
            for d in ds:groups.setdefault(d['project_key'],[]).append(d)
            out=[]
            for key,v in groups.items():
                docs,rows=rows_for(v)
                # review 是项目级完成度与待办清单：让用户能回答
                #「这个项目还剩多少没核对完」，而不是只看单个格子的颜色。
                out.append({'key':key,'name':docs[0]['project_name'],'documents':docs,'rows':rows,
                            'review':project_progress(docs,rows)})
            return self.send({'groups':out})
        if p.startswith('/api/jobs/'):
            jid=p.rsplit('/',1)[-1]
            if jid not in s.jobs:return self.send({'error':'任务不存在'},404)
            return self.send(s.snapshot_jobs(jid))
        if p in ('/api/export.xlsx','/api/export.json'):
            docs=s.list(); query=parse_qs(urlparse(self.path).query)
            key=query.get('project',[None])[0]
            if key is not None:
                docs=[d for d in docs if d['project_key']==key]
                if not docs:raise ValueError('未找到该项目，请刷新后重试')
            if not docs:raise ValueError('请先上传批复文件')
            if p.endswith('.xlsx'):return self.send(export_xlsx(docs),ctype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',download='approval-comparison.xlsx')
            return self.send(json.dumps(docs,ensure_ascii=False,indent=2),ctype='application/json',download='approval-evidence.json')
        detail=re.fullmatch(r'/api/documents/([0-9a-f]{32})',p)
        if detail:return self.send(s.get(detail.group(1)))
        m=re.fullmatch(r'/api/documents/([0-9a-f]{32})/pages/(\d+)\.png',p)
        if m:
            i,n=m.groups();d=s.get(i);n=int(n)
            if not 1<=n<=len(d['pages']):raise ValueError()
            with fitz.open(s.path/(i+'.pdf')) as doc:
                page=doc[n-1];page.set_rotation(0)
                return self.send(page.get_pixmap(matrix=fitz.Matrix(1.5,1.5),alpha=False).tobytes('png'),ctype='image/png')
        files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
        if p in files:
            fn=files[p];ct={'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[fn.split('.')[-1]]
            return self.send((ROOT/'app/static'/fn).read_bytes(),ctype=ct)
        self.send({'error':'未找到'},404)
    def do_POST(self):
        # Custom header + same-origin check prevent cross-site form uploads.
        origin=self.headers.get('Origin')
        if self.headers.get('X-Requested-With')!='ApprovalAgent' or (origin and urlparse(origin).netloc!=self.headers.get('Host')):
            return self.send({'error':'来源校验失败'},403)
        try:
            n=int(self.headers.get('Content-Length','0'))
            if n<=0 or n>32*1024*1024:return self.send({'error':'请求大小超限（32MB）'},413)
            data=json.loads(self.rfile.read(n));self.post(urlparse(self.path).path,data)
        except (ValueError,KeyError,TypeError) as e:self.send({'error':str(e)[:180] or '请求无效'},400)
        except FileNotFoundError:self.send({'error':'文件不存在'},404)
        except Exception:self.send({'error':'处理失败，请重试'},500)
    def post(self,p,data):
        s=self.server.store
        if p=='/api/model/config':
            # 密钥留空表示不改动；校验在落盘前完成，非法地址不会覆盖原有可用配置。
            return self.send(save_config(data.get('values') or {},clear_key=data.get('clear_key') is True))
        if p=='/api/model/config/reset':
            return self.send(clear_config())
        if p=='/api/model/test':
            if not self.server.store.model_probe_lock.acquire(blocking=False):
                return self.send({'error':'正在测试模型连接，请稍后重试'},429)
            try:return self.send(probe(vision=data.get('vision') is True))
            except ModelError as exc:return self.send({'error':str(exc)},502)
            finally:self.server.store.model_probe_lock.release()
        if p=='/api/upload':
            fs=data.get('files',[])
            if not isinstance(fs,list) or not 1<=len(fs)<=10:raise ValueError('一次上传1至10份PDF')
            items=[]
            for f in fs:
                # 逐项校验结构：缺字段时直接抛 ValueError 给出可读提示，
                # 否则会漏出「list indices must be integers or slices」这类
                # 只有开发者看得懂的解释器报错。
                if not isinstance(f,dict):raise ValueError('上传内容格式无效：每份文件需包含文件名与内容')
                if not isinstance(f.get('name'),str) or not f['name'].strip():raise ValueError('上传内容格式无效：文件名缺失')
                if not isinstance(f.get('data'),str) or not f['data'].strip():raise ValueError('上传内容格式无效：文件内容缺失')
                name=Path(f['name']).name[:180]
                try:blob=base64.b64decode(f['data'],validate=True)
                except Exception:raise ValueError('上传内容格式无效：文件内容无法解码') from None
                if not name.lower().endswith('.pdf') or not blob.startswith(b'%PDF-'):raise ValueError('仅支持PDF文件')
                if len(blob)>20*1024*1024:raise ValueError('单份文件不得超过20MB')
                items.append((name,blob))
            if sum(len(blob) for _,blob in items)>23*1024*1024:raise ValueError('每批文件总计不得超过23MB，请分批上传')
            if sum(j['status']=='running' for j in s.jobs.values())>=3:return self.send({'error':'任务繁忙，请稍后再试'},429)
            llm=bool(data.get('use_llm'))
            if llm and not public_config()['llm_ready']:raise ValueError('请先在「模型设置」中完成大模型配置')
            jid=s.new_job([name for name,_ in items])
            s.executor.submit(s.run,jid,items,llm)
            return self.send({'job_id':jid},202)
        m=re.fullmatch(r'/api/documents/([0-9a-f]{32})/review',p)
        if m:
            with s.lock:
                d=apply_review(s.get(m.group(1)),data)
                s.save(d)
            return self.send({'ok':True,'revision':d['revision']})
        if p=='/api/documents/delete':
            ids=data.get('ids')
            if not isinstance(ids,list) or not ids:raise ValueError('请选择要移入回收站的文件')
            if len(ids)>500:raise ValueError('一次最多处理 500 份')
            if s.busy():raise ValueError('有文件正在处理，请等待任务结束后再删除')
            want={i for i in ids if isinstance(i,str)}
            if str(data.get('scope') or '')=='all':
                # 口令里带着当前份数：既拦住误触，也挡住「界面渲染之后又有文件传进来」
                # 导致实际清空范围与用户所见不一致。
                # 判据是「用户点的是清空」这个意图，不是「这批 id 恰好覆盖了全部」——
                # 后者会让「只有一个项目时删除该项目」被误判成清空。
                docs=s.list()
                if not docs:raise ValueError('当前没有可清空的文件')
                expected=f'DELETE-{len(docs)}'
                if str(data.get('confirm') or '')!=expected:raise ValueError(f'清空全部需要确认口令 {expected}')
                if want!={d['id'] for d in docs}:raise ValueError('清空范围与当前文件不一致，请刷新后重试')
            moved=s.trash(sorted(want))
            if not moved:raise ValueError('未找到可移入回收站的文件，请刷新后重试')
            return self.send({'ok':True,'moved':len(moved),'ids':moved})
        if p=='/api/trash/restore':
            ids=data.get('ids')
            if ids is not None and not isinstance(ids,list):raise ValueError('恢复参数无效')
            moved=s.restore([i for i in (ids or []) if isinstance(i,str)] or None)
            return self.send({'ok':True,'restored':len(moved),'ids':moved})
        if p=='/api/trash/purge':
            # 彻底删除不可逆，所以要有独立口令，且与「移入回收站」分成两个动作。
            if str(data.get('confirm') or '')!='PURGE':raise ValueError('彻底删除需要确认口令 PURGE')
            ids=data.get('ids')
            if ids is not None and not isinstance(ids,list):raise ValueError('参数无效')
            purged=s.purge([i for i in (ids or []) if isinstance(i,str)] or None)
            return self.send({'ok':True,'purged':purged})
        m=re.fullmatch(r'/api/documents/([0-9a-f]{32})/reprocess',p)
        if m:
            i=m.group(1);s.get(i)
            llm=bool(data.get('use_llm'))
            if llm and not public_config()['llm_ready']:raise ValueError('请先在「模型设置」中完成大模型配置')
            if any(j['status']=='running' for j in s.jobs.values()):raise ValueError('请等待当前任务结束后重试')
            jid=s.new_job([s.get(i)['filename']])
            s.executor.submit(s.reprocess,jid,i,llm)
            return self.send({'job_id':jid},202)
        self.send({'error':'未找到'},404)

def main():
    load_env();parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=int(os.getenv('PORT','8765')));parser.add_argument('--host',default=os.getenv('HOST','127.0.0.1'));args=parser.parse_args()
    http=ThreadingHTTPServer((args.host,args.port),Handler);http.store=Store(os.getenv('DATA_DIR',str(ROOT/'data')))
    # 界面保存的模型配置与数据同目录：换模型不必改 .env，也不必重启。
    set_config_path(http.store.path/'model_config.json')
    print(f'文件结构化智能体：http://{args.host}:{args.port}',flush=True)
    try:http.serve_forever()
    except KeyboardInterrupt:pass
    finally:http.server_close();http.store.executor.shutdown(wait=True)
if __name__=='__main__':main()
