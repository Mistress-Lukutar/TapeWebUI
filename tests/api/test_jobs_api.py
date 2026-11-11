"""
File:   test_jobs_api.py
Brief:  Jobs API endpoint tests (CRUD, run, cancel, runtime status).
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
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


def test_job_excludes_roundtrip(client: TestClient) -> None:
    """Excludes survive create/read over the API."""
    yaml_with_excludes = JOB_YAML.replace(
        '    patterns: ["**/*.jpg"]',
        '    patterns: ["**/*.jpg"]\n    excludes: ["temp/", "*.tmp"]',
    )
    response = client.post("/api/jobs", json={"content": yaml_with_excludes})
    assert response.status_code == 201, response.text

    stored = client.get("/api/jobs/apijob")
    assert stored.status_code == 200
    assert "temp/" in stored.json()["yaml"]
    assert "*.tmp" in stored.json()["yaml"]


def test_job_backslash_exclude_422(client: TestClient) -> None:
    """An ignore pattern with a backslash separator is rejected."""
    bad = JOB_YAML.replace(
        '    patterns: ["**/*.jpg"]', '    excludes: ["temp\\\\"]'
    )
    response = client.post("/api/jobs", json={"content": bad})
    assert response.status_code == 422
    assert "path separator" in response.json()["detail"]


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


def test_preview_sources_counts(client, tmp_path) -> None:
    """Preview counts files/bytes per source with pattern pruning."""
    source = tmp_path / "src_tree"
    source.mkdir()
    (source / "a.txt").write_text("hello")
    (source / "b.log").write_text("x" * 10)
    sub = source / "temp"
    sub.mkdir()
    (sub / "c.txt").write_text("ignored")

    payload = {
        "sources": [
            {"path": str(source), "excludes": ["temp/", "*.log"]},
            {"path": str(tmp_path / "missing")},
        ]
    }
    response = client.post("/api/jobs/preview-sources", json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result) == 2
    first = result[0]
    assert first["exists"] is True
    assert first["files"] == 1
    assert first["bytes"] == 5
    assert result[1] == {
        "path": str(tmp_path / "missing"),
        "exists": False,
        "files": 0,
        "bytes": 0,
    }
