"""「材料质量」页的原件校验缓存：**只准省时间，不准改结论**。

背景：`attachment_report()` 每次加载都要判断全部原件与登记的 sha256 是否一致，
每次判断都要把文件整读一遍。本库 808 个附件、449MB，实测单次 13.0 秒
（同机其余页面 0.02–0.03 秒），用户点「材料质量」后十几秒页面毫无反应。
`FileVerifyCache` 以 (路径, 大小, 修改时间) 为键复用结论，把这一页压到 0.03 秒。

这个优化**动了验收口径**，所以必须钉住三件事：
一致（缓存命中与逐字节重算结论相同）、失效（原件变了必须重算）、
以及那个**已知的残留窗口**（大小与修改时间都不变时看不出来）。
"""
import hashlib
import os
from pathlib import Path

from policy_collector import quality
from policy_collector.db import Database


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _db(tmp_path) -> Database:
    return Database(Path(tmp_path) / 'policy.db')


def _counting(monkeypatch) -> list:
    """拦下真正读文件的那一步，用来证明"缓存命中时确实没读"。"""
    calls: list = []
    real = quality._sha_matches

    def wrapper(path, expected):
        calls.append(str(path))
        return real(path, expected)

    monkeypatch.setattr(quality, '_sha_matches', wrapper)
    return calls


def test_cached_verdict_equals_uncached(tmp_path, monkeypatch):
    """三种输入（一致 / 不一致 / 没登记 sha）下，缓存结论必须与直接重算相同。"""
    f = tmp_path / 'a.pdf'
    f.write_bytes(b'policy-original')
    good = _sha(f.read_bytes())
    db = _db(tmp_path)
    try:
        cache = quality.FileVerifyCache(db)
        for expected in (good, '0' * 64, ''):
            assert cache.ok(f, expected) == quality._sha_matches(f, expected), expected
    finally:
        db.close()


def test_unchanged_file_is_hashed_only_once(tmp_path, monkeypatch):
    """同一文件连问三次只真读一次——这就是 13 秒变 0.03 秒的来源。"""
    f = tmp_path / 'b.pdf'
    f.write_bytes(b'x' * 2048)
    db = _db(tmp_path)
    calls = _counting(monkeypatch)
    try:
        cache = quality.FileVerifyCache(db)
        sha = _sha(f.read_bytes())
        assert [cache.ok(f, sha) for _ in range(3)] == [True, True, True]
        assert len(calls) == 1
    finally:
        db.close()


def test_verdict_survives_process_restart(tmp_path, monkeypatch):
    """结论要落库：下一次请求是新的 cache 实例（真实情况是新进程），必须直接命中。"""
    f = tmp_path / 'c.pdf'
    f.write_bytes(b'y' * 512)
    sha = _sha(f.read_bytes())
    db = _db(tmp_path)
    try:
        first = quality.FileVerifyCache(db)
        assert first.ok(f, sha)
        assert first.flush() == 1

        calls = _counting(monkeypatch)
        second = quality.FileVerifyCache(db)          # 模拟下一次页面加载
        assert second.ok(f, sha) is True
        assert calls == []                            # 没再读文件
        assert second.flush() == 0                    # 命中就不该再写库
    finally:
        db.close()


def test_missing_file_is_always_bad(tmp_path):
    """文件不在（被清理/移到回收站）不能沿用旧结论。"""
    f = tmp_path / 'gone.pdf'
    f.write_bytes(b'z' * 256)
    sha = _sha(f.read_bytes())
    db = _db(tmp_path)
    try:
        cache = quality.FileVerifyCache(db)
        assert cache.ok(f, sha) is True
        cache.flush()
        f.unlink()
        assert quality.FileVerifyCache(db).ok(f, sha) is False
    finally:
        db.close()


def test_file_replaced_with_another_length_is_reverified(tmp_path):
    """换成一个长度不同的文件：旧的"完好"结论必须立刻失效。"""
    f = tmp_path / 'd.pdf'
    f.write_bytes(b'z' * 4096)
    sha = _sha(b'z' * 4096)
    db = _db(tmp_path)
    try:
        cache = quality.FileVerifyCache(db)
        assert cache.ok(f, sha)
        cache.flush()
        f.write_bytes(b'corrupted')
        assert quality.FileVerifyCache(db).ok(f, sha) is False
    finally:
        db.close()


def test_same_length_rewrite_is_caught_by_mtime(tmp_path):
    """同长度改写：大小判据失效，必须靠修改时间兜住。"""
    f = tmp_path / 'e.pdf'
    f.write_bytes(b'A' * 4096)
    sha = _sha(b'A' * 4096)
    db = _db(tmp_path)
    try:
        cache = quality.FileVerifyCache(db)
        assert cache.ok(f, sha)
        cache.flush()
        before = f.stat()
        f.write_bytes(b'B' * 4096)                    # 同长度、不同内容
        os.utime(f, ns=(before.st_atime_ns, before.st_mtime_ns + 10 ** 9))
        assert f.stat().st_size == 4096
        assert quality.FileVerifyCache(db).ok(f, sha) is False
    finally:
        db.close()


def test_same_length_and_same_mtime_is_the_documented_blind_spot(tmp_path):
    """**已知残留窗口**：内容变了但大小与修改时间都不变——缓存看不出来。

    这条不是在验收"能查到"，而是把这个边界钉在测试里：缓存是"复用结论"，
    不是"持续监控"。有人把 sha256 当成"每次都会重新校验"的保证时，
    这个用例就是那个说明。
    """
    f = tmp_path / 'f.pdf'
    f.write_bytes(b'A' * 4096)
    sha = _sha(b'A' * 4096)
    db = _db(tmp_path)
    try:
        cache = quality.FileVerifyCache(db)
        assert cache.ok(f, sha)
        cache.flush()
        stamp = f.stat()
        f.write_bytes(b'B' * 4096)
        os.utime(f, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))   # 连时间都还原

        assert quality.FileVerifyCache(db).ok(f, sha) is True      # 沿用旧结论
        assert quality._sha_matches(f, sha) is False               # 而真相是不一致
    finally:
        db.close()
