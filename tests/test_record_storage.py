"""Disk stalls must not stall acquisition or silently become complete episodes."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import numpy as np
import pytest

import oglo._record as module
from oglo._frame import Frame, ImuSample, MagSample
from oglo._device import SampleBatch
from oglo._record import Recorder, RecordError
from oglo import replay
from test_record_replay import glove


def add(rec, seq):
    rec.add_tactile(Frame(seq=seq, t_us=seq * 4000, host_t=seq / 250,
                          counts=np.full((5, 4, 4), seq, dtype=np.uint16)))


def block_chunks(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = module._write_chunk

    def write(path, rows):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test did not release the disk")
        return original(path, rows)

    monkeypatch.setattr(module, "_write_chunk", write)
    return entered, release


def test_blocked_disk_does_not_block_sample_ingestion_and_owns_copied_rows(tmp_path, monkeypatch):
    entered, release = block_chunks(monkeypatch)
    g = glove(n=0)
    rec = Recorder(g, tmp_path / "ep_0001", chunk_samples=2)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            def ingest():
                for seq in range(7):
                    add(rec, seq)
            done = pool.submit(ingest)
            try:
                assert entered.wait(2)
                # Disk is still blocked. Acquisition must already have returned.
                done.result(timeout=2)
                assert rec._t.n == 7
                assert rec._t.live_samples == 1
            finally:
                release.set()
        rec.write(complete=False, error="fixture", stop_reason="test")
        z = replay(rec.dir).arrays("tactile")
        assert z['seq'].tolist() == list(range(7))
        assert z['counts'][:, 0, 0, 0].tolist() == list(range(7))
        if hasattr(rec, "_writer"):
            assert not rec._writer._thread.is_alive()
    finally:
        release.set()
        if hasattr(rec, "_writer"):
            rec._writer.close(check=False)
        g.close()


def test_backlog_is_bounded_fails_promptly_and_preserves_accepted_rows(tmp_path, monkeypatch):
    entered, release = block_chunks(monkeypatch)
    g = glove(n=0)
    rec = Recorder(g, tmp_path / "ep_0001", chunk_samples=1)
    try:
        add(rec, 0)
        add(rec, 1)
        assert entered.wait(2)  # One active block no longer occupies a queue slot.
        for seq in range(2, 10):
            add(rec, seq)
        with pytest.raises(RecordError, match="storage backlog is full"):
            add(rec, 10)
        assert rec._writer._queue.qsize() == 8
        assert rec._t.n == 10 and rec._t.live_samples == 1
        release.set()
        rec.write(complete=False, error="storage backlog is full", stop_reason="error")
        meta = json.loads((rec.dir / "meta.json").read_text())
        assert meta['complete'] is False
        assert meta['counts']['tactile'] == 10
        z = replay(rec.dir).arrays("tactile")
        assert z['seq'].tolist() == list(range(10))
    finally:
        release.set()
        rec._writer.close(check=False)
        g.close()


def test_worker_io_failure_cannot_publish_a_complete_episode(tmp_path, monkeypatch):
    g = glove(n=0)
    rec = Recorder(g, tmp_path / "ep_0001", chunk_samples=1)
    failed = threading.Event()

    def fail(path, columns):
        failed.set()
        raise OSError("simulated disk full")

    monkeypatch.setattr(module, "_write_chunk", fail)
    try:
        add(rec, 0)
        add(rec, 1)
        assert failed.wait(2)
        rec._writer._queue.join()
        with pytest.raises(RecordError, match="storage failed: simulated disk full"):
            add(rec, 2)
        with pytest.raises(RecordError, match="could not finalize") as caught:
            rec.write()
        assert caught.value.partial_episode == rec.dir
        meta = json.loads((rec.dir / "meta.json").read_text())
        assert meta['complete'] is False and meta['stop_reason'] == 'write_error'
        assert 'simulated disk full' in meta['error']
        assert not list(rec.dir.glob('*.jsonl'))
        assert not rec._writer._thread.is_alive()
    finally:
        rec._writer.close(check=False)
        g.close()


def test_publication_waits_for_pending_chunks(tmp_path, monkeypatch):
    entered, release = block_chunks(monkeypatch)
    g = glove(n=0)
    rec = Recorder(g, tmp_path / "ep_0001", chunk_samples=1)
    try:
        add(rec, 0)
        add(rec, 1)
        assert entered.wait(2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            finishing = pool.submit(rec.write)
            try:
                assert not finishing.done()
                assert json.loads((rec.dir / "meta.json").read_text())['complete'] is False
            finally:
                release.set()
            finishing.result(timeout=5)
        assert json.loads((rec.dir / "meta.json").read_text())['complete'] is True
        rows = [json.loads(line) for line in (rec.dir / "tactile_left.jsonl").read_text().splitlines()]
        assert [r["oglo"]["seq"] for r in rows] == [0, 1]
    finally:
        release.set()
        rec._writer.close(check=False)
        g.close()


def test_record_keeps_polling_while_chunks_are_blocked(tmp_path, monkeypatch):
    entered, release = block_chunks(monkeypatch)
    polled_again, stop = threading.Event(), threading.Event()
    baseline = glove(n=0)
    info, status = baseline.info, baseline.status()
    baseline.close()
    original_recorder = module.Recorder
    monkeypatch.setattr(module, 'Recorder', lambda g, path, **kw: original_recorder(g, path, chunk_samples=2, **kw))

    class Device:
        dropped = {}

        def __init__(self):
            self.info = info
            self.calls = 0

        def status(self):
            return status

        def read_batch(self):
            self.calls += 1
            if self.calls == 4:
                # The third batch fills the first set of chunks. The next poll
                # must happen without needing the storage call to return.
                polled_again.set()
                stop.set()
            ns = time.monotonic_ns()
            common = dict(seq=self.calls, t_us=self.calls * 4000,
                          host_t=ns/1e9, host_t_ns=ns, host_received_ns=ns)
            return SampleBatch(
                tactile=(Frame(**common, counts=np.zeros((5,4,4),dtype=np.uint16)),),
                imu=(ImuSample(**common, accel=(0,0,1), gyro=(0,0,0), raw=(0,0,4096,0,0,0)),),
                mag=(MagSample(**common, field=(0,0,1), raw=(0,0,1)),),
            )

    device = Device()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(module.record, tmp_path, seconds=30, glove=device, stop_event=stop)
        try:
            assert entered.wait(2)
            assert polled_again.wait(2), 'record stopped reading while a chunk write was blocked'
        finally:
            stop.set()
            release.set()
        episode = future.result(timeout=5)
    meta = json.loads((episode/'meta.json').read_text())
    assert meta['complete'] is True and meta['stop_reason'] == 'cancelled'
    assert meta['counts'] == dict(tactile=5, imu=5, mag=5)
