"""Create explicitly fictional PDFs or import supplied PDFs through the real extractor."""
import argparse,hashlib,sys,time,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import fitz
from app.server import Store,main

def synthetic(folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    for project,code,areas in [('演示文体中心项目','2609-330100-04-01-100001',[4700,3990,3580]),('演示连接线工程项目','2609-330100-04-01-100002',[200000,136930,136929.6])]:
        for i,stage in enumerate(['项目建议书','可行性研究报告','初步设计']):
            doc=fitz.open();p=doc.new_page();y=65
            lines=['演示市发展和改革局文件',f'演发改投〔2026〕{101+i}号',f'关于{project}{stage}的批复','演示市建设有限公司：','一、建设地点','项目位于演示园区。','二、主要建设内容及规模',f'项目总用地面积{areas[i]}平方米，总建筑面积{areas[i]}平方米。','主要建设公共服务设施及相关配套工程。','三、项目业主','演示市建设有限公司。','四、建设工期',f'项目建设工期为{60 if i==0 else 24}个月。','五、投资估算和资金筹措',f'项目估算总投资{2998-i*10}万元。建设资金由业主自筹解决。','请据此开展下一阶段工作。']
            for line in lines:
                p.insert_text((45,y),line,fontname='china-s',fontsize=12,color=(.65,.08,.08) if line.endswith('局文件') else (0,0,0));y+=29
            p=doc.new_page()
            p.insert_text((45,70),'本文件为系统功能演示生成的虚构材料，不用于真实审批。',fontname='china-s',fontsize=12)
            p.insert_text((340,385),'演示市发展和改革局',fontname='china-s',fontsize=12)
            p.draw_circle((430,420),36,color=(.8,.05,.05),width=2)
            p.insert_text((406,425),'演示章',fontname='china-s',fontsize=14,color=(.8,.05,.05))
            p.insert_text((45,690),'项目代码：'+code,fontname='china-s',fontsize=12)
            p.insert_text((45,750),'演示市发展和改革局办公室',fontname='china-s',fontsize=11)
            p.insert_text((350,750),f'2026年{i+1}月1日印发',fontname='china-s',fontsize=11)
            doc.save(folder/f'{project}-{stage}.pdf');doc.close()
    return folder

def workflow_fixture(folder):
    """Extra fictional case: unknown stage, missing stages and conflicting metric scope."""
    path=Path(folder)/'演示待核对项目.pdf'
    with fitz.open() as pdf:
        page=pdf.new_page()
        for y,line in [(60,'演示市发展和改革局文件'),(90,'关于演示待核对项目的批复'),
                       (120,'项目代码：2609-330100-04-01-100003'),
                       (160,'一、建设内容：总建筑面积100平方米。'),
                       (190,'其中总建筑面积200平方米。'),
                       (230,'二、建设地点：演示园区。'),
                       (270,'本文件为故意设置矛盾的虚构演示材料，不用于真实审批。')]:
            page.insert_text((45,y),line,fontname='china-s',fontsize=11)
        pdf.save(path)
    return path

def main_demo():
    p=argparse.ArgumentParser();p.add_argument('--samples');p.add_argument('--data',default='data');p.add_argument('--serve',action='store_true');p.add_argument('--review-demo',action='store_true',help='增加阶段待确认和指标冲突的虚构材料');a=p.parse_args()
    directory=Path(a.samples) if a.samples else synthetic(Path(a.data)/'demo-source')
    if a.review_demo:
        if a.samples:raise SystemExit('--review-demo 仅可用于生成的虚构材料')
        workflow_fixture(directory)
    files=sorted(directory.glob('*.pdf'))
    if not files:raise SystemExit('目录中没有PDF文件')
    store=Store(a.data);jid=uuid.uuid4().hex;store.jobs[jid]={'status':'running','total':len(files),'done':0,'results':[],'errors':[]}
    try:
        store.run(jid,[(f.name,f.read_bytes()) for f in files],False)
        result=store.jobs[jid];print(f"导入{len(result['results'])}份，失败{len(result['errors'])}份")
        if result['errors']:print(result['errors']);raise SystemExit(1)
    finally:store.executor.shutdown(wait=True)
    print('材料已通过实际解析流程入库。')
    if a.serve:
        import os
        os.environ['DATA_DIR']=str(Path(a.data).resolve());sys.argv=[sys.argv[0]];main()
if __name__=='__main__':main_demo()
