from threading import Event

from runtime.job_queue import InProcessJobQueue


def test_worker_survives_a_failed_job(monkeypatch):
    finished = Event()

    def dispatch(job):
        if job["payload"]["fail"]:
            raise RuntimeError("database temporarily unavailable")
        finished.set()

    monkeypatch.setattr("runtime.job_queue.dispatch_job", dispatch)
    queue = InProcessJobQueue(max_workers=1)
    queue.submit("plan", "bad", "bad", {"fail": True})
    queue.submit("plan", "good", "good", {"fail": False})
    assert finished.wait(3)
