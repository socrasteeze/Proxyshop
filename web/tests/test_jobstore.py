"""
* Job Store Tests — claim/lease/requeue semantics, idempotency, retries.
"""
# Local Imports
from web.server.db import JobStore
from web.shared.schema import JobStatus


class TestSubmitAndClaim:

    def test_submit_defaults(self, jobstore):
        job = jobstore.submit(card_name='Lightning Bolt')
        assert job.status == JobStatus.QUEUED
        assert job.attempts == 0
        assert job.game == 'mtg'

    def test_submit_pokemon_game(self, jobstore):
        job = jobstore.submit(card_name='Pikachu', game='pokemon')
        assert job.game == 'pokemon'
        assert jobstore.get(job.id).game == 'pokemon'

    def test_claim_oldest_first(self, jobstore):
        first = jobstore.submit(card_name='First')
        jobstore.submit(card_name='Second')
        claimed = jobstore.claim_next('w1')
        assert claimed.id == first.id
        assert claimed.status == JobStatus.CLAIMED
        assert claimed.attempts == 1

    def test_claim_empty_queue(self, jobstore):
        assert jobstore.claim_next('w1') is None

    def test_claim_skips_taken_jobs(self, jobstore):
        jobstore.submit(card_name='Only')
        assert jobstore.claim_next('w1') is not None
        assert jobstore.claim_next('w2') is None


class TestIdempotency:

    def test_duplicate_key_returns_existing(self, jobstore):
        a = jobstore.submit(card_name='Bolt', idempotency_key='k-1')
        b = jobstore.submit(card_name='Bolt', idempotency_key='k-1')
        assert a.id == b.id
        assert len(jobstore.list_jobs()) == 1

    def test_different_keys_create_jobs(self, jobstore):
        jobstore.submit(card_name='Bolt', idempotency_key='k-1')
        jobstore.submit(card_name='Bolt', idempotency_key='k-2')
        assert len(jobstore.list_jobs()) == 2


class TestFinishAndRetry:

    def test_success(self, jobstore):
        job = jobstore.submit(card_name='Bolt')
        jobstore.claim_next('w1')
        jobstore.finish(job.id, ok=True, result_filename='result.png', log='ok')
        done = jobstore.get(job.id)
        assert done.status == JobStatus.DONE
        assert done.result_filename == 'result.png'
        assert done.finished_at

    def test_failure_requeues_below_max_attempts(self, jobstore):
        job = jobstore.submit(card_name='Bolt')
        jobstore.claim_next('w1')  # attempts -> 1
        jobstore.finish(job.id, ok=False, error='boom')
        assert jobstore.get(job.id).status == JobStatus.QUEUED

    def test_failure_at_max_attempts_fails(self, jobstore):
        job = jobstore.submit(card_name='Bolt')
        jobstore.claim_next('w1')  # attempts -> 1
        jobstore.finish(job.id, ok=False, error='boom 1')
        jobstore.claim_next('w1')  # attempts -> 2
        jobstore.finish(job.id, ok=False, error='boom 2')
        failed = jobstore.get(job.id)
        assert failed.status == JobStatus.FAILED
        assert failed.error == 'boom 2'


class TestLeaseRequeue:

    def _expire_lease(self, jobstore, job_id):
        con = jobstore._conn()
        con.execute(
            "UPDATE jobs SET claimed_at = datetime('now', '-30 minutes') WHERE id=?",
            (job_id,))
        con.commit()

    def test_stale_claim_requeues(self, jobstore):
        job = jobstore.submit(card_name='Bolt')
        jobstore.claim_next('w1')
        self._expire_lease(jobstore, job.id)
        assert jobstore.requeue_stale() == 1
        assert jobstore.get(job.id).status == JobStatus.QUEUED

    def test_fresh_claim_not_requeued(self, jobstore):
        jobstore.submit(card_name='Bolt')
        jobstore.claim_next('w1')
        assert jobstore.requeue_stale() == 0

    def test_stale_claim_at_max_attempts_fails(self, jobstore):
        job = jobstore.submit(card_name='Bolt')
        jobstore.claim_next('w1')
        jobstore.finish(job.id, ok=False, error='first failure')  # requeued, attempts=1
        jobstore.claim_next('w1')  # attempts=2
        self._expire_lease(jobstore, job.id)
        jobstore.requeue_stale()
        failed = jobstore.get(job.id)
        assert failed.status == JobStatus.FAILED
        assert failed.error == 'Worker lease expired'


class TestWorkers:

    def test_heartbeat_and_capabilities(self, jobstore):
        jobstore.touch_worker('w1', capabilities='{"templates": {}}')
        jobstore.touch_worker('w2')
        workers = jobstore.get_workers()
        assert {w['name'] for w in workers} == {'w1', 'w2'}
        assert all(w['online'] for w in workers)
        assert jobstore.get_capabilities() == '{"templates": {}}'
