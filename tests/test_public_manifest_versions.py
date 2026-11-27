import json

import pytest

from autophone.application.ai import build_ai_context
from autophone.contracts import ContractError
from autophone.observation.artifacts import Artifact, append_artifacts, load_manifest, write_artifact_manifest


def test_current_forbidden_is_preserved(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "private.txt").write_text("private")
    path = run / "artifact-manifest.json"
    item = dict(path="private.txt", kind="log", sensitivity="internal", redaction_status="redacted",
                retention="local_only", share_policy="forbidden")
    raw = json.dumps(dict(schema_version="v2", run_id="run", artifacts=[item]))
    path.write_text(raw)
    bundle = tmp_path / "bundle"
    build_ai_context(run, dest=bundle)
    assert not (bundle / "private.txt").exists()
    assert path.read_text() == raw
    (run / "new.txt").write_text("new")
    append_artifacts(run, [Artifact("new.txt", "log")])
    stored = json.loads(path.read_text())
    assert stored["schema_version"] == "v2"
    assert stored["artifacts"][0] == item
    assert load_manifest(path)["artifacts"][1]["share_policy"] == "summary_only"


def test_current_manifest_round_trip_and_invalid_field(tmp_path):
    path = write_artifact_manifest(tmp_path, "run", [Artifact("log.txt", "log")])
    data = json.loads(path.read_text())
    assert load_manifest(path) == data
    assert data["schema_version"] == "v2"
    data["artifacts"][0]["unknown_policy"] = "forbidden"
    path.write_text(json.dumps(data))
    with pytest.raises(ContractError):
        load_manifest(path)


def test_old_version_is_rejected_without_modifying_the_manifest(tmp_path):
    path = tmp_path / "artifact-manifest.json"
    raw = json.dumps({"schema_version": "v1", "run_id": "run", "artifacts": []})
    path.write_text(raw)
    with pytest.raises(ContractError):
        load_manifest(path)
    with pytest.raises(ContractError):
        append_artifacts(tmp_path, [])
    assert path.read_text() == raw
