"""Real provider probe using synthetic text/image only. No credentials in output."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.server import load_env
from app.model_client import probe, ModelError

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--vision', action='store_true')
    args = parser.parse_args()
    load_env()
    try:
        print(json.dumps(probe(args.vision), ensure_ascii=False))
    except ModelError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)
