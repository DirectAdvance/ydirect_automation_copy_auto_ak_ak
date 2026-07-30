"""content-retry-cap-fix: depth cap on the agent-retry chain + atomic insert+mark.

Covers the two codex-auditor findings on the already-deployed content-agent-retry
feature (commit 253dfc0c):

1. content_retry_depth caps the error -> agent-board-task -> done -> retry chain
   (content_jobs_ready_for_agent_retry's max_depth gate), instead of only guarding a
   single row against being retried twice.
2. _content_retry_insert_from_failed() does INSERT (new retry row) + UPDATE (mark the
   failed row's content_retry_job_id) as ONE statement/transaction via a writable CTE,
   so a crash between them cannot leave an unmarked failed row next to an orphan retry
   row (which used to cause a duplicate retry on the next poll).

Uses a minimal in-memory fake jobs_exec (no real Postgres) — same style as
test_content_worker_blocked_skip.py's fake_jobs_exec, extended just enough to
interpret the handful of fixed queries these two functions issue.
"""
from __future__ import annotations

from direct import agent_board_bridge
from direct import content_worker


class FakeCEJobsDB:
    """Interprets exactly the queries content_jobs_ready_for_agent_retry() and
    _content_retry_insert_from_failed() issue against an in-memory dict of rows."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self._order = 0

    def add(self, job_id: str, **fields) -> None:
        self._order += 1
        row = {
            "job_id": job_id,
            "username": "u", "login": "porg-test", "agency": "ag",
            "type": "ad_href", "old_text": "/old", "new_text": "/new", "mode": "exact",
            "campaign_count": 1, "access_directologists": None,
            "status": "error", "agent_board_task_id": 1,
            "content_retry_job_id": None, "content_retry_depth": 0,
            "finished_at": self._order,
        }
        row.update(fields)
        self.rows[job_id] = row

    def exec(self, query: str, params: tuple = (), fetch: str | None = None):
        q = query.strip()
        if q.startswith("ALTER TABLE"):
            return None
        if q.startswith("WITH ins AS"):
            (new_job_id, _username, login, agency, type_, old_text, new_text, mode,
             campaign_count, access, depth, failed_job_id) = params
            self.add(
                new_job_id, username="agent-board-auto", login=login, agency=agency,
                type=type_, old_text=old_text, new_text=new_text, mode=mode,
                campaign_count=campaign_count, access_directologists=access,
                content_retry_depth=depth,
            )
            failed = self.rows.get(failed_job_id)
            if failed and failed["status"] == "error" and failed["content_retry_job_id"] is None:
                failed["content_retry_job_id"] = new_job_id
                failed["content_retry_started_at"] = "now"
                return {"retry_job_id": new_job_id}
            return None
        if q.startswith("SELECT * FROM") and "content_retry_depth" in q:
            max_depth, _limit = params
            rows = [
                dict(r) for r in self.rows.values()
                if r["status"] == "error"
                and r["agent_board_task_id"] is not None
                and r["content_retry_job_id"] is None
                and (r.get("content_retry_depth") or 0) < max_depth
            ]
            rows.sort(key=lambda r: r["finished_at"])
            return rows
        if q.startswith("SELECT 1 AS x FROM"):
            return None
        raise AssertionError(f"FakeCEJobsDB got unexpected query: {q[:80]!r}")


def test_ready_for_retry_filters_on_max_depth(monkeypatch):
    db = FakeCEJobsDB()
    db.add("ce_depth0", content_retry_depth=0)
    db.add("ce_depth2", content_retry_depth=2)
    db.add("ce_depth3", content_retry_depth=3)  # at cap for max_depth=3, must be excluded
    monkeypatch.setattr(agent_board_bridge, "_agent_board_done_task_meta", lambda ids: {1: {}})

    ready = agent_board_bridge.content_jobs_ready_for_agent_retry(
        db.exec, "content_jobs", limit=5, max_depth=3
    )

    ready_ids = {r["job_id"] for r in ready}
    assert ready_ids == {"ce_depth0", "ce_depth2"}
    assert "ce_depth3" not in ready_ids


def test_retry_chain_stops_at_max_depth(monkeypatch):
    """Simulate 4 consecutive failures of the SAME lineage: original + 3 retries.
    With CONTENT_AGENT_RETRY_MAX_DEPTH=3, the retry created at depth 3 must be the
    LAST one — a 5th retry (depth 4) must never be created."""
    db = FakeCEJobsDB()
    db.add("ce_job0", content_retry_depth=0)
    monkeypatch.setattr(agent_board_bridge, "_agent_board_done_task_meta", lambda ids: {1: {}})

    created_chain = ["ce_job0"]
    for _ in range(5):  # more iterations than the chain can possibly reach
        ready = agent_board_bridge.content_jobs_ready_for_agent_retry(
            db.exec, "content_jobs", limit=5, max_depth=3
        )
        if not ready:
            break
        row = ready[0]
        retry_jid = content_worker._content_retry_insert_from_failed(db.exec, "content_jobs", row)
        assert retry_jid is not None, "insert+mark must succeed for a fresh failed row"
        created_chain.append(retry_jid)
        # the new retry row itself fails and gets an agent-board task assigned, so the
        # next poll can see it (mirrors the real error -> task -> done -> retry cycle)
        db.rows[retry_jid]["agent_board_task_id"] = 1

    # original + 3 retries reaching depth 1,2,3 = 4 rows total; the depth-3 row is
    # never retried again because 3 < 3 is False.
    assert len(created_chain) == 4
    depths = [db.rows[jid]["content_retry_depth"] for jid in created_chain]
    assert depths == [0, 1, 2, 3]
    last = db.rows[created_chain[-1]]
    assert last["content_retry_job_id"] is None  # never got its own retry


def test_insert_from_failed_marks_original_atomically_in_one_call():
    """The INSERT (new row) + UPDATE (mark original) must be ONE _jobs_exec call, not
    two — that is what makes it atomic (single connection/transaction in _jobs_exec)."""
    db = FakeCEJobsDB()
    db.add("ce_failed", content_retry_depth=1)
    calls = []
    orig_exec = db.exec

    def counting_exec(query, params=(), fetch=None):
        calls.append(query)
        return orig_exec(query, params, fetch)

    retry_jid = content_worker._content_retry_insert_from_failed(counting_exec, "content_jobs", db.rows["ce_failed"])

    assert retry_jid is not None
    assert len(calls) == 1  # single statement covers both insert and mark
    assert db.rows["ce_failed"]["content_retry_job_id"] == retry_jid
    assert db.rows[retry_jid]["content_retry_depth"] == 2  # failed row's depth(1) + 1
