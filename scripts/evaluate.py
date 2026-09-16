"""Reproducible six-PDF evaluation against independently transcribed gold."""
import argparse,hashlib,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.extract import extract,clean
from app.compare import export_xlsx

def run(sample_dir,out,gold_path=None):
    root=Path(__file__).resolve().parents[1];gold=json.loads((Path(gold_path) if gold_path else root/'evaluation/gold.json').read_text(encoding='utf-8'))
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    failures=[];docs=[];fixed=correct=present=located=tp=predicted=expected=review=0;timings=[];hashes={}
    for i,g in enumerate(gold['documents']):
        p=Path(sample_dir)/g['filename']
        if not p.is_file():raise FileNotFoundError(f'缺少样例：{p.name}')
        start=time.perf_counter();d=extract(p,p.name,f'{i:032x}');timings.append(round(time.perf_counter()-start,3));docs.append(d)
        hashes[p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
        for k,want in g['fields'].items():
            actual=d['fields'][k];fixed+=1
            if (clean(actual['value']) if actual['value'] is not None else None)==(clean(want['value']) if want['value'] is not None else None):correct+=1
            else:failures.append({'file':p.name,'field':k,'expected':want['value'],'actual':actual['value']})
            if actual['status'] in ['needs_review','uncertain','conflict']:review+=1
            if want['value'] is not None:
                present+=1
                pages={e['page'] for e in actual['evidence']}
                # For values repeated later, gold lists the accepted source page(s).
                if set(want['pages']).issubset(pages):located+=1
                else:failures.append({'file':p.name,'field':k,'expected_pages':want['pages'],'actual_pages':sorted(pages)})
        a={(m['name'],clean(m['value'])) for m in d['metrics'] if m.get('scope')=='construction'}
        b={(m['name'],clean(m['value'])) for m in g['construction_metrics']}
        tp+=len(a&b);predicted+=len(a);expected+=len(b)
        for x in sorted(a-b):failures.append({'file':p.name,'extra_metric':x})
        for x in sorted(b-a):failures.append({'file':p.name,'missing_metric':x})
    report={'documents':len(docs),'fixed_fields':{'correct':correct,'total':fixed,'accuracy':round(correct/fixed,4)},'evidence_page_recall':{'correct':located,'total':present,'recall':round(located/present,4)},'construction_metrics':{'true_positive':tp,'predicted':predicted,'expected':expected,'precision':round(tp/predicted,4) if predicted else 0,'recall':round(tp/expected,4)},'manual_review_fields':review,'seconds_per_document':timings,'sample_sha256':hashes,'failures':failures,'scope':gold['scope'],'limitations':['开发样例回归，不代表未见文档泛化准确率','页码命中不等于文字框像素级准确率；图像高亮另行浏览器核对','本地印章候选仍需要人工或视觉模型确认']}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'predictions.json').write_text(json.dumps(docs,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'comparison.xlsx').write_bytes(export_xlsx(docs))
    print(json.dumps({k:v for k,v in report.items() if k not in ['sample_sha256']},ensure_ascii=False,indent=2))
    return report
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--samples',required=True);p.add_argument('--gold',help='本地私有标注文件；不设置时使用仓库内虚构演示标注');p.add_argument('--out',default='evaluation/results');a=p.parse_args();r=run(a.samples,a.out,a.gold);sys.exit(bool(r['failures']))
