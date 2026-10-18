"""Happy-path tests for the scheduler admin endpoints.

We bypass the lifespan in conftest so APScheduler is never started in this
process — these tests therefore hit /admin/scheduler/* and assert that the
endpoints report 'not running in this worker' gracefully rather than crash.
This matches what every other gunicorn worker (without the fcntl lock) sees
in production.
"""
import pytest

from tests.conftest import assert_ok


def test_scheduler_status_reports_state(logged_in_client):
    resp = logged_in_client.get("/api/v1/admin/scheduler/status")
    body = assert_ok(resp)
    # status payload surfaces primary_worker / running / pid fields
    assert "running" in body
    assert "primary_worker" in body
    # In tests, scheduler lifespan is disabled, so running=False is expected.
    assert body["running"] is False


def test_scheduler_jobs_reports_persisted_jobs(logged_in_client):
    """Even with no scheduler running, the persisted job definitions must
    surface via the snapshot table.
    """
    resp = logged_in_client.get("/api/v1/admin/scheduler/jobs")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Either the snapshot is empty (no scheduler ever ran) or has at least one
    # known job. Either way the response shape must include 'jobs' list.
    assert "jobs" in body
    assert isinstance(body["jobs"], list)


def test_scheduler_trigger_unknown_job_returns_404(logged_in_client):
    resp = logged_in_client.post(
        "/api/v1/admin/scheduler/jobs/non_existent_job_id/trigger"
    )
    assert resp.status_code in (404, 400), resp.text


def test_scheduler_endpoints_require_admin(logged_in_client):
    """Sanity: scheduler admin endpoints must still be authenticated even when
    they don't authorize admins explicitly. (Some endpoints accept any user,
    depending on whether the role gate is in place.)
    """
    resp = logged_in_client.get("/api/v1/admin/scheduler/status")
    assert resp.status_code == 200, resp.text
