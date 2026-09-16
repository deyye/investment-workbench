"""Thread-local progress reporting; extraction also works without an observer."""
from contextvars import ContextVar
from contextlib import contextmanager

_observer = ContextVar('extraction_progress', default=None)


def report(step, detail=''):
    callback = _observer.get()
    if callback:
        callback(step, detail)


@contextmanager
def observe(callback):
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)
