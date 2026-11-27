"""Run the two Settings examples on an explicitly selected local test dashboard."""
import argparse
import json
from pathlib import Path
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4


def verify(port, rig):
    base = f"http://127.0.0.1:{port}"

    def request(path, payload=None):
        data = json.dumps(payload).encode() if payload else None
        req = Request(base + path, data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=30) as response:
            return response.read()

    targets = json.loads(request("/api/platform/targets"))
    if targets != {"mode": "local", "targets": [{"rig_id": rig, "status": "local"}]}:
        raise ValueError(f"Unexpected local target: {targets}")

    for case, expected in (("R1", "passed"), ("R2", "failed")):
        run_id = f"settings-{case.lower()}-{uuid4().hex[:12]}"
        submission = json.loads(request("/runs", {
            "schema_version": "v1", "run_id": run_id, "type": "regression",
            "app": "ios_settings_smoke", "device": rig, "requested_by": "acceptance",
            "case_selector": {"ids": [case]},
        }))
        print(run_id, submission["run_dir"], flush=True)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            detail = json.loads(request(f"/api/runs/{run_id}"))
            if detail.get("result"):
                break
            time.sleep(1)
        else:
            raise TimeoutError(run_id)

        result = detail["result"]
        rows = result["cases"]
        if len(rows) != 1 or rows[0]["id"] != case:
            raise ValueError(f"{run_id}: unexpected cases")
        row = rows[0]
        report = Path(detail["artifacts"]["report_html"]).name
        html = request(f"/api/runs/{run_id}/artifact/{quote(report)}").decode()
        if result["status"] != expected or row["status"] != expected or case not in html:
            raise ValueError(f"{run_id}: {row}")
        if case == "R2" and (
            row.get("failure_stage") != "assertion"
            or "AUTOPHONE_NEGATIVE_SENTINEL_404" not in row["planes"]["ui"].get("detail", "")
            or "AUTOPHONE_NEGATIVE_SENTINEL_404" not in html
        ):
            raise ValueError(f"{run_id}: unexpected negative control: {row}")
        print(case, expected, report, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--rig", required=True)
    args = parser.parse_args()
    verify(args.port, args.rig)
