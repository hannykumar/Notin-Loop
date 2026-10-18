import os
from pathlib import Path
import subprocess


def test_fault_script_selects_kind_explicitly_from_any_directory(tmp_path):
    binary = tmp_path / "kubectl"
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    binary.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / "lab/scripts/kind_lab_fault.sh"
    result = subprocess.run(["bash", str(script), "oom"], cwd=tmp_path,
                            env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "SRE_LAB_CLUSTER_NAME": "review"},
                            capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["--context", "kind-review", "apply", "-f", "lab/k8s/fault-oom.yaml"]
