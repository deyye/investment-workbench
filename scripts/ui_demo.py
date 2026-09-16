"""Start an isolated, offline UI demo (synthetic policies, no API calls).
python scripts/ui_demo.py --port 8765
"""
import argparse
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from policy_collector.config import AppConfig, SourceConfig
from policy_collector.pipeline import Pipeline
from policy_collector.webapp import create_app


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='policy-ui-demo-') as directory:
        cfg=AppConfig.load();cfg.data_dir=Path(directory);cfg.db_path=cfg.data_dir/'demo.db';cfg.downloads_dir=cfg.data_dir/'originals';cfg.llm.enabled=False
        cfg.sources={'demo_local':SourceConfig(name='demo_local',site='离线样例（合成数据）',region='样例',enabled=True,list_url=(Path(__file__).resolve().parents[1]/'samples/list.html').as_uri(),max_pages=1)}
        p=Pipeline(cfg);p.run_demo();p.close()
        app=create_app(cfg)
        @app.context_processor
        def preview():return {'preview_mode':True}
        app.run(host='0.0.0.0',port=args.port,threaded=True)

if __name__=='__main__':main()
