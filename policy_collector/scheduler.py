"""进程常驻定时采集；生产可由 systemd/cron 运行 CLI 单次任务。"""
from __future__ import annotations
import logging
import signal
import threading
from .config import AppConfig
from .pipeline import Pipeline
logger = logging.getLogger('policy.scheduler')

class Scheduler:
    def __init__(self, cfg: AppConfig, interval_minutes=None):
        self.cfg = cfg
        self.interval = cfg.schedule_interval_minutes if interval_minutes in (None, 0) else interval_minutes
        if self.interval <= 0:
            raise ValueError('运行间隔必须大于零')
        self.stop = threading.Event()
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._on_signal)
            signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, *_):
        self.stop.set()

    def run_forever(self, source_names=None, prefer='llm', cycles=None, limit=50):
        sources = source_names or [n for n,s in self.cfg.sources.items() if s.enabled and not s.list_url.startswith('file:')]
        if not sources or (cycles is not None and cycles < 1):
            raise ValueError('需要至少一个启用来源，cycles 必须为正数')
        for n in sources:
            if n not in self.cfg.sources or not self.cfg.sources[n].enabled:
                raise ValueError(f'来源不存在或已停用: {n}')
        completed = 0
        errors = False
        while not self.stop.is_set():
            for name in sources:
                if self.stop.is_set(): break
                pipe = Pipeline(self.cfg)
                try:
                    stats = pipe.run_source(name, prefer=prefer, limit=limit, kind='scheduled')
                    errors = errors or stats.has_errors
                    logger.info('%s %s', name, stats.to_dict())
                except Exception as exc:
                    errors = True
                    logger.error('%s: %s', name, exc)
                finally:
                    pipe.close()
            completed += 1
            if cycles is not None and completed >= cycles: break
            self.stop.wait(self.interval * 60)
        return 1 if errors else 0
