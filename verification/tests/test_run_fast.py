import json
import os
import shutil
import subprocess
import sys

from verification.common import REPO_ROOT


def test_run_fast_forwards_custom_run_dir_to_evidence(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    python = repo / ".venv" / "bin" / "python"
    scripts.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "scripts" / "_env.sh", scripts / "_env.sh")
    shutil.copyfile(REPO_ROOT / "scripts" / "run-fast.sh", scripts / "run-fast.sh")

    calls_path = tmp_path / "calls.jsonl"
    python.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['RUN_FAST_CALLS'], 'a', encoding='utf-8') as calls:\n"
        "    calls.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    python.chmod(0o755)
    run_dir = str(tmp_path / "custom run")
    env = {
        **os.environ,
        "RUN_FAST_CALLS": str(calls_path),
    }

    result = subprocess.run(
        [
            "bash",
            str(scripts / "run-fast.sh"),
            "--run-dir",
            run_dir,
            "--scenario",
            "normal_session",
            "--baseline",
            "expected",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert calls == [
        [
            "verification/cli.py",
            "verify",
            "--run-dir",
            run_dir,
            "--scenario",
            "normal_session",
            "--baseline",
            "expected",
        ],
        ["verification/cli.py", "evidence", "--run-dir", run_dir],
    ]
