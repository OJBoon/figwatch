"""Tests for BoundedSet and processed-comment persistence."""

import json
import os
import tempfile
from unittest import mock

from figwatch.watcher import BoundedSet, load_processed, save_processed


class TestBoundedSet:
    def test_add_and_contains(self):
        s = BoundedSet(maxlen=5)
        s.add('a')
        s.add('b')
        assert 'a' in s
        assert 'b' in s
        assert 'c' not in s

    def test_evicts_oldest_on_overflow(self):
        s = BoundedSet(maxlen=3)
        s.add('a')
        s.add('b')
        s.add('c')
        s.add('d')  # evicts 'a'
        assert 'a' not in s
        assert 'b' in s
        assert 'd' in s
        assert len(s) == 3

    def test_add_existing_refreshes(self):
        s = BoundedSet(maxlen=3)
        s.add('a')
        s.add('b')
        s.add('c')
        s.add('a')  # refresh — 'a' moves to end, 'b' is now oldest
        s.add('d')  # evicts 'b'
        assert 'a' in s
        assert 'b' not in s
        assert len(s) == 3

    def test_iter_preserves_order(self):
        s = BoundedSet(maxlen=5)
        s.add('x')
        s.add('y')
        s.add('z')
        assert list(s) == ['x', 'y', 'z']

    def test_update(self):
        s = BoundedSet(maxlen=3)
        s.update(['a', 'b', 'c', 'd'])
        assert 'a' not in s
        assert list(s) == ['b', 'c', 'd']

    def test_clear(self):
        s = BoundedSet(maxlen=5)
        s.update(['a', 'b'])
        s.clear()
        assert len(s) == 0
        assert 'a' not in s


class TestPersistence:
    def test_load_save_roundtrip(self, tmp_path):
        proc_file = tmp_path / '.processed-comments.json'
        with mock.patch('figwatch.watcher._processed_path', return_value=str(proc_file)):
            ids = BoundedSet()
            ids.update(['1', '2', '3'])
            save_processed(ids)

            loaded = load_processed()
            assert '1' in loaded
            assert '2' in loaded
            assert '3' in loaded
            assert len(loaded) == 3

    def test_load_missing_file(self, tmp_path):
        missing = str(tmp_path / 'nope.json')
        with mock.patch('figwatch.watcher._processed_path', return_value=missing):
            loaded = load_processed()
            assert len(loaded) == 0
            assert isinstance(loaded, BoundedSet)

    def test_save_respects_maxlen(self, tmp_path):
        proc_file = tmp_path / '.processed-comments.json'
        with mock.patch('figwatch.watcher._processed_path', return_value=str(proc_file)):
            ids = BoundedSet(maxlen=3)
            ids.update(['a', 'b', 'c', 'd', 'e'])
            save_processed(ids)

            with open(proc_file) as f:
                on_disk = json.load(f)
            assert len(on_disk) == 3
            assert on_disk == ['c', 'd', 'e']
