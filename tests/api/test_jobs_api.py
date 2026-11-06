"""
File:   test_jobs_api.py
Brief:  Jobs API endpoint tests (CRUD, run, cancel, runtime status).
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.5.0
"""

from __future__ import annotations

from fastapi.testclient import TestClient

JOB_YAML = """\
name: apijob
sources:
  - path: "D:/Photos"
    patterns: ["**/*.jpg"]
options:
  mode: append
  verify: none
  continue_on_error: true
schedule:
  kind: manual
"""


def test_job_crud_cycle(client: TestClient) -> None:
    """Create, read, update, delete a job over the API."""
    # create
    response = client.post("/api/jobs", json={"content": JOB_YAML})
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "apijob"

    # duplicate create -> 409
    response = client.post("/api/jobs", json={"content": JOB_YAML})
    assert response.status_code == 409

    # list
    response = client.get("/api/jobs")
    assert response.status_code == 200
    names = [job["name"] for job in response.json()]
    assert names == ["apijob"]

    # read one
    response = client.get("/api/jobs/apijob")
    assert response.status_code == 200
    assert "apijob" in response.json()["yaml"]

    # update with matching name
    response = client.put("/api/jobs/apijob", json={"content": JOB_YAML})
    assert response.status_code == 200

    # update with mismatched name -> 422
    bad = JOB_YAML.replace("name: apijob", "name: other")
    response = client.put("/api/jobs/apijob", json={"content": bad})
    assert response.status_code == 422

    # delete
    response = client.delete("/api/jobs/apijob")
    assert response.status_code == 200
    assert client.get("/api/jobs/apijob").status_code == 404


def test_job_create_validation_error(client: TestClient) -> None:
    """Invalid YAML returns 422 with a readable detail."""
    response = client.post("/api/jobs", json={"content": "name: [broken"})
    assert response.status_code == 422
    assert "detail" in response.json()


def test_run_unknown_job_404(client: TestClient) -> None:
    """Running a missing job returns 404."""
    response = client.post("/api/jobs/missing/run")
    assert response.status_code == 404


def test_run_and_runtime_status(client: TestClient) -> None:
    """A queued job appears in runtime status and completes on the fake."""
    import time

    client.post("/api/jobs", json={"content": JOB_YAML})
    response = client.post("/api/jobs/apijob/run")
    assert response.status_code == 200
    assert response.json()["accepted"] is True

    # The runtime worker completes the (empty) job quickly on the fake.
    for _ in range(50):
        status = client.get("/api/jobs/runtime/status").json()
        if status["active"] is None and status["queue_length"] == 0:
            break
        time.sleep(0.05)
    assert status["active"] is None


def test_cancel_endpoint(client: TestClient) -> None:
    """The cancel endpoint returns a boolean payload."""
    response = client.post("/api/jobs/cancel")
    assert response.status_code == 200
    assert response.json() == {"cancelled": False}
