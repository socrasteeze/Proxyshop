"""
* Per-game download queue tests (pure, file-backed, offline).
"""
from web.shared import download_queue as dq


class TestQueue:

    def test_enqueue_and_head(self, tmp_path):
        dq.enqueue(tmp_path, 'mtg', {'tags': 'art:dragon'})
        dq.enqueue(tmp_path, 'mtg', {'tags': 'art:angel'})
        items = dq.load_queue(tmp_path, 'mtg')
        assert [i['label'] for i in items] == ['art:dragon', 'art:angel']
        assert dq.head(tmp_path, 'mtg')['label'] == 'art:dragon'

    def test_dedup_identical_spec(self, tmp_path):
        a = dq.enqueue(tmp_path, 'mtg', {'tags': 'art:dragon'})
        b = dq.enqueue(tmp_path, 'mtg', {'tags': 'art:dragon'})
        assert a['id'] == b['id']
        assert len(dq.load_queue(tmp_path, 'mtg')) == 1

    def test_pop_head_guarded(self, tmp_path):
        h = dq.enqueue(tmp_path, 'mtg', {'set': 'mh3'})
        dq.enqueue(tmp_path, 'mtg', {'set': 'war'})
        # Popping with the wrong id is a no-op.
        assert dq.pop_head(tmp_path, 'mtg', 'nope') is None
        assert len(dq.load_queue(tmp_path, 'mtg')) == 2
        popped = dq.pop_head(tmp_path, 'mtg', h['id'])
        assert popped['id'] == h['id']
        assert dq.head(tmp_path, 'mtg')['label'] == 'Set WAR'

    def test_remove_pending(self, tmp_path):
        dq.enqueue(tmp_path, 'mtg', {'set': 'mh3'})
        b = dq.enqueue(tmp_path, 'mtg', {'set': 'war'})
        assert dq.remove(tmp_path, 'mtg', b['id'])['id'] == b['id']
        assert [i['label'] for i in dq.load_queue(tmp_path, 'mtg')] == ['Set MH3']
        assert dq.remove(tmp_path, 'mtg', 'missing') is None

    def test_is_head(self, tmp_path):
        a = dq.enqueue(tmp_path, 'mtg', {'set': 'mh3'})
        b = dq.enqueue(tmp_path, 'mtg', {'set': 'war'})
        assert dq.is_head(tmp_path, 'mtg', a['id']) is True
        assert dq.is_head(tmp_path, 'mtg', b['id']) is False

    def test_clear_pending_keeps_head(self, tmp_path):
        dq.enqueue(tmp_path, 'mtg', {'set': 'mh3'})
        dq.enqueue(tmp_path, 'mtg', {'set': 'war'})
        dq.enqueue(tmp_path, 'mtg', {'set': 'dom'})
        removed = dq.clear_pending(tmp_path, 'mtg')
        assert removed == 2
        assert [i['label'] for i in dq.load_queue(tmp_path, 'mtg')] == ['Set MH3']

    def test_empty_and_missing(self, tmp_path):
        assert dq.load_queue(tmp_path, 'mtg') == []
        assert dq.head(tmp_path, 'mtg') is None
        assert dq.pop_head(tmp_path, 'mtg') is None
        assert dq.clear_pending(tmp_path, 'mtg') == 0
