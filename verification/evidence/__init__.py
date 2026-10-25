"""Build a self-contained evidence package from a verification run directory."""
from __future__ import annotations

import hashlib
import html
import importlib.metadata
import json
import platform
import re
import subprocess
from pathlib import Path

from verification.common import RunContext, canonical, git_dirty, git_sha

GATE_NAMES = ("behavioural", "state_ordering", "contract", "privacy")
PRIVACY_STATEMENT = (
    "This automated result does not approve cutover or production release and does not discharge "
    "privacy-review obligations. Passing checks are not proof of production safety. "
    "The ReadyForReview -> ApprovedForCutover transition is a human decision."
)
GUARANTEE_BOUNDARIES = (
    "This package covers fast-tier replay. The contract provides per-key ordering only; "
    "restart behavior is at-least-once. No exactly-once, global ordering or atomic cutover claim is made."
)
PACKAGE_HASH_METHOD = (
    "SHA-256 of canonical JSON (sorted keys, compact separators, UTF-8) for the complete "
    "evidence.json object with integrity.package_sha256 set to null."
)


class RunReader:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.file_hashes: dict[str, str] = {}
        self.file_statuses: dict[str, str] = {}

    def read_bytes(self, path: Path) -> tuple[bytes | None, str]:
        relative = path.relative_to(self.run_dir).as_posix()
        try:
            within_run_dir = path.resolve().is_relative_to(self.run_dir.resolve())
        except (OSError, RuntimeError):
            self.file_statuses[relative] = "unreadable"
            return None, "unreadable"
        if not within_run_dir:
            self.file_statuses[relative] = "unreadable"
            return None, "unreadable"
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            self.file_statuses[relative] = "missing"
            return None, "missing"
        except OSError:
            self.file_statuses[relative] = "unreadable"
            return None, "unreadable"
        self.file_hashes[relative] = hashlib.sha256(payload).hexdigest()
        self.file_statuses[relative] = "present"
        return payload, "present"

    def read_json(self, path: Path) -> tuple[dict | None, str]:
        payload, status = self.read_bytes(path)
        if status != "present":
            return None, status
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            self.file_statuses[path.relative_to(self.run_dir).as_posix()] = "unreadable"
            return None, "unreadable"
        if not isinstance(value, dict):
            self.file_statuses[path.relative_to(self.run_dir).as_posix()] = "unreadable"
            return None, "unreadable"
        return value, "present"


def _sanitize_text(value):
    if isinstance(value, str):
        return value.replace("\u2014", "-")
    if isinstance(value, list):
        return [_sanitize_text(item) for item in value]
    if isinstance(value, dict):
        return {_sanitize_text(key): _sanitize_text(item) for key, item in value.items()}
    return value


def _normalize_repo_origin(remote: str) -> str:
    if "://" in remote:
        authority, _, path = remote.split("://", 1)[1].partition("/")
        authority = authority.rsplit("@", 1)[-1]
        remote = f"{authority}/{path}" if path else authority
    else:
        remote = remote.rsplit("@", 1)[-1]
        scp_match = re.match(r"^([^/:]+):(.+)$", remote)
        if scp_match:
            remote = f"{scp_match.group(1)}/{scp_match.group(2)}"
    remote = remote.strip("/")
    remote = re.sub(r"^[^/]+/proxy/", "", remote, count=1)
    return remote.removesuffix(".git")


def _repo_origin(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=repo_root,
            capture_output=True, text=True, check=True, timeout=10,
        )
        remote = _normalize_repo_origin(result.stdout.strip())
        return remote or repo_root.name
    except (OSError, subprocess.SubprocessError):
        return repo_root.name


def _tool_version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or not output:
        return "unavailable"
    return output.splitlines()[0]


def _package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


def _gate_summary(document: dict | None, status: str) -> dict:
    checks = document.get("checks", []) if document else []
    if not isinstance(checks, list):
        checks = []
    checks = [item for item in checks if isinstance(item, dict)]

    def basis(item):
        evidence = item.get("evidence")
        return evidence.get("basis", "unspecified") if isinstance(evidence, dict) else "unspecified"

    counts = {
        state: sum(item.get("status") == state for item in checks)
        for state in ("pass", "fail", "skip")
    }
    return {
        "status": status,
        "summary_counts": {
            "checks": len(checks),
            "passed": counts["pass"],
            "failed": counts["fail"],
            "skipped": counts["skip"],
        },
        "skipped_count": counts["skip"],
        "by_verification_kind": {
            kind: sum(item.get("verification") == kind for item in checks)
            for kind in ("runtime", "static")
        },
        "by_basis": {
            basis_name: sum(basis(item) == basis_name for item in checks)
            for basis_name in sorted({basis(item) for item in checks})
        },
        "failing_checks": [
            {key: item.get(key) for key in ("id", "scenario", "category", "detail", "verification")}
            for item in checks if item.get("status") == "fail"
        ],
    }


def _read_gate(reader: RunReader, run_dir: Path, name: str) -> tuple[dict | None, str]:
    path = run_dir / "gates" / f"{name}.json"
    document, parse_status = reader.read_json(path)
    if parse_status != "present":
        return None, parse_status
    status = document.get("status")
    if (not isinstance(status, str) or status not in {"pass", "fail", "error"}
            or not isinstance(document.get("checks"), list)):
        reader.file_statuses[path.relative_to(run_dir).as_posix()] = "unreadable"
        return None, "unreadable"
    return document, status


def _output_runs(reader: RunReader, run_dir: Path) -> list[dict]:
    root = run_dir / "outputs"
    try:
        if not root.resolve().is_relative_to(run_dir.resolve()):
            return []
    except (OSError, RuntimeError):
        return []
    if not root.is_dir():
        return []
    records = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        payload, file_status = reader.read_bytes(path)
        if path.name != "run.json":
            continue
        relative = path.relative_to(root)
        processor = relative.parts[0] if len(relative.parts) > 0 else None
        scenario = relative.parts[1] if len(relative.parts) > 1 else None
        record = {"processor": processor, "scenario": scenario, "status": file_status}
        if file_status == "present":
            try:
                run = json.loads(payload.decode("utf-8"))
                if not isinstance(run, dict):
                    record["status"] = "unreadable"
                    reader.file_statuses[path.relative_to(run_dir).as_posix()] = "unreadable"
                else:
                    record.update({
                        "exit_codes": run.get("exit_codes", []),
                        "duration_ms": run.get("duration_ms"),
                    })
            except (UnicodeError, json.JSONDecodeError):
                record["status"] = "unreadable"
                reader.file_statuses[path.relative_to(run_dir).as_posix()] = "unreadable"
        records.append(record)
    return records


def _inventory_section(privacy: dict | None, privacy_status: str) -> dict:
    if privacy is None:
        status = privacy_status if privacy_status in {"missing", "unreadable"} else "missing"
        return {"status": status, "baseline_approval": None, "dimensions": {}}
    inventory = privacy.get("inventory", {})
    if not isinstance(inventory, dict):
        inventory = {}
    baseline = inventory.get("baseline", {})
    candidate = inventory.get("candidate", {})
    if not isinstance(baseline, dict):
        baseline = {}
    if not isinstance(candidate, dict):
        candidate = {}
    baseline_ids = baseline.get("dimensions", {})
    candidate_ids = candidate.get("dimensions", {})
    if not isinstance(baseline_ids, dict):
        baseline_ids = {}
    if not isinstance(candidate_ids, dict):
        candidate_ids = {}
    diff = inventory.get("diff", {})
    if not isinstance(diff, dict):
        diff = {}
    implementation_changed = inventory.get("implementation_changed")
    if not isinstance(implementation_changed, bool):
        implementation_changed = baseline.get("implementation") != candidate.get("implementation")
    dimensions = {}
    for name in list(baseline_ids) + [key for key in candidate_ids if key not in baseline_ids]:
        delta = diff.get(name)
        status = "unknown"
        if isinstance(delta, dict) and all(key in delta for key in ("added", "removed", "changed")):
            status = "changed" if any(delta[key] for key in ("added", "removed", "changed")) else "unchanged"
        dimensions[name] = {
            "baseline_ids": baseline_ids.get(name, []),
            "candidate_ids": candidate_ids.get(name, []),
            "status": status,
        }
    return {
        "status": "present" if privacy_status in {"pass", "fail", "error"} else privacy_status,
        "baseline_approval": baseline.get("approval"),
        "baseline": {key: baseline.get(key) for key in (
            "path", "processor", "content_sha256_recorded", "content_sha256_computed"
        )},
        "candidate": {key: candidate.get(key) for key in ("path", "processor")},
        "implementation": {
            "baseline": baseline.get("implementation"),
            "candidate": candidate.get("implementation"),
            "changed": implementation_changed,
            "informational": True,
        },
        "dimensions": dimensions,
    }


def _assessment_section(privacy: dict | None, privacy_status: str) -> dict:
    if privacy is None:
        status = privacy_status if privacy_status in {"missing", "unreadable"} else "missing"
        return {"status": status, "diff": {}, "policies": []}
    inventory = privacy.get("inventory", {})
    if not isinstance(inventory, dict):
        inventory = {}
    return {
        "status": "present" if privacy_status in {"pass", "fail", "error"} else privacy_status,
        "diff": inventory.get("diff", {}),
        "implementation_changed": inventory.get("implementation_changed"),
        "policies": privacy.get("policies", []),
    }


def _disposition_section(privacy: dict | None, privacy_status: str,
                         gates: dict[str, str]) -> dict:
    if privacy is None:
        reason = "privacy gate missing" if privacy_status == "missing" else "privacy gate unreadable"
        disposition = {
            "recommendation": "needs-clarification",
            "reasons": {"violation": [], "review": [], "clarification": [reason]},
            "statement": "Automated recommendation for a human reviewer, not a legal or compliance determination.",
            "required_human_actions": ["A privacy reviewer reviews this package and decides what action is appropriate."],
        }
    else:
        stored_disposition = privacy.get("disposition")
        disposition = stored_disposition if isinstance(stored_disposition, dict) else {
            "recommendation": "needs-clarification",
            "reasons": {"violation": [], "review": [], "clarification": ["privacy disposition missing"]},
            "statement": "Automated recommendation for a human reviewer, not a legal or compliance determination.",
            "required_human_actions": ["A privacy reviewer reviews this package and decides what action is appropriate."],
        }
    actions = list(disposition.get("required_human_actions", []))
    for name, status in gates.items():
        if status in {"fail", "error", "missing", "unreadable"}:
            action = f"A human reviewer reviews the {name} gate and addresses its {status} status."
            if action not in actions:
                actions.append(action)
    return {
        "disposition": disposition,
        "required_human_actions": actions,
        "statement": PRIVACY_STATEMENT,
        "guarantee_boundaries": GUARANTEE_BOUNDARIES,
    }


SECTION_TITLES = (
    ("A", "Identity"),
    ("B", "Processing inventory"),
    ("C", "Change assessment"),
    ("D", "Validation evidence"),
    ("E", "Review disposition"),
    ("F", "Integrity"),
)
REVIEWER_RECOMMENDATION = (
    "Automated recommendation for a human reviewer, not a legal or compliance determination"
)


def _display(value) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _id_list(value) -> str:
    if not isinstance(value, list):
        return _display(value)
    return ", ".join(_display(item) for item in value) if value else "none"


def _table(headers: list[str], rows: list[list], status_column: int | None = None) -> tuple:
    return ("table", headers, rows, status_column)


def _section_blocks(key: str, sections: dict, integrity: dict) -> list[tuple]:
    if key == "A":
        identity = sections["A"]
        git = identity.get("git", {})
        processors = identity.get("processors", {})
        return [
            _table(["Item", "Value"], [
                ["Repository", identity.get("repo")],
                ["Run git SHA / dirty", git.get("run_time")],
                ["Built-at SHA / mismatch", {
                    "git_sha": git.get("built_at", {}).get("git_sha"),
                    "sha_mismatch": git.get("sha_mismatch"),
                }],
                ["Processors", {
                    "baseline": processors.get("baseline"),
                    "candidate": processors.get("candidate"),
                    "runs": processors.get("runs", []),
                }],
                ["Scenarios", identity.get("scenario_set")],
                ["Mode", identity.get("mode")],
                ["Tool versions", identity.get("tool_versions")],
            ]),
        ]
    if key == "B":
        inventory = sections["B"]
        dimensions = inventory.get("dimensions", {})
        implementation = inventory.get("implementation", {})
        return [
            ("heading", "Baseline approval"),
            _table(["Item", "Value"], [
                ["Approval", inventory.get("baseline_approval")],
                ["Baseline inventory", inventory.get("baseline")],
                ["Candidate inventory", inventory.get("candidate")],
            ]),
            _table(
                ["Dimension", "Baseline IDs", "Candidate IDs", "Status"],
                [[
                    name,
                    _id_list(item.get("baseline_ids")),
                    _id_list(item.get("candidate_ids")),
                    item.get("status", "unknown"),
                ] for name, item in dimensions.items()],
                status_column=3,
            ),
            ("paragraph", "Implementation (informational): baseline "
             f"{_display(implementation.get('baseline'))}, candidate "
             f"{_display(implementation.get('candidate'))}, changed "
             f"{_display(implementation.get('changed'))}."),
        ]
    if key == "C":
        assessment = sections["C"]
        diff = assessment.get("diff", {})
        policies = assessment.get("policies", [])
        return [
            _table(
                ["Dimension", "Added", "Removed", "Changed"],
                [[
                    name,
                    _id_list(delta.get("added")),
                    _id_list(delta.get("removed")),
                    _id_list(delta.get("changed")),
                ] for name, delta in diff.items() if isinstance(delta, dict)],
            ),
            ("heading", "Policy results"),
            _table(
                ["ID", "Severity", "Status"],
                [[item.get("id"), item.get("severity"), item.get("status")]
                 for item in policies if isinstance(item, dict)],
                status_column=2,
            ),
        ]
    if key == "D":
        gates = sections["D"].get("gates", {})
        blocks = []
        for name, gate in gates.items():
            blocks.extend([
                ("heading", f"{name} gate"),
                ("paragraph", f"Status: {_status_label(gate.get('status'))}"),
                ("heading", "Counts by evidence basis"),
                _table(
                    ["Basis", "Checks"],
                    [[basis, count] for basis, count in gate.get("by_basis", {}).items()]
                    or [["None", 0]],
                ),
                ("heading", "Failing checks"),
                _table(
                    ["ID", "Scenario", "Category", "Verification", "Detail"],
                    [[item.get(field) for field in (
                        "id", "scenario", "category", "verification", "detail"
                    )] for item in gate.get("failing_checks", [])]
                    or [["None", "", "", "", ""]],
                ),
            ])
        return blocks
    if key == "E":
        review = sections["E"]
        disposition = review.get("disposition", {})
        reasons = disposition.get("reasons", {})
        return [
            ("paragraph", f"Recommendation: {_display(disposition.get('recommendation'))}"),
            ("heading", "Reasons"),
            _table(
                ["Severity", "Checks or reasons"],
                [[severity, _id_list(reasons.get(severity, []))]
                 for severity in ("violation", "review", "clarification")],
            ),
            ("heading", "Required human actions"),
            ("list", review.get("required_human_actions", [])),
            ("heading", "Guarantee boundaries"),
            ("paragraph", review.get("guarantee_boundaries", GUARANTEE_BOUNDARIES)),
        ]
    files = integrity.get("files", {})
    statuses = integrity.get("file_statuses", {})
    file_names = sorted(set(files) | set(statuses))
    git_shas = integrity.get("git_shas", {})
    return [
        _table(
            ["File", "SHA-256", "Status"],
            [[name, files.get(name), statuses.get(name, "unknown")] for name in file_names],
            status_column=2,
        ),
        ("paragraph", f"Package SHA-256: {integrity.get('package_sha256')}"),
        ("paragraph", f"Package SHA-256 method: {integrity.get('package_sha256_method')}"),
        ("heading", "Git SHAs"),
        _table(["Item", "Value"], [
            ["Run-time", git_shas.get("run_time")],
            ["Built-at", git_shas.get("built_at")],
            ["SHA mismatch", git_shas.get("sha_mismatch")],
        ]),
    ]


def _status_label(status) -> str:
    return "MISSING" if status == "missing" else _display(status)


def _summary_gate_rows(sections: dict) -> list[list]:
    gates = sections.get("D", {}).get("gates", {})
    rows = []
    for name in GATE_NAMES:
        gate = gates.get(name, {})
        counts = gate.get("summary_counts", {})
        kinds = gate.get("by_verification_kind", {})
        rows.append([
            name,
            gate.get("status", "missing"),
            counts.get("checks", 0),
            counts.get("passed", 0),
            counts.get("failed", 0),
            counts.get("skipped", 0),
            kinds.get("runtime", 0),
            kinds.get("static", 0),
        ])
    return rows


def _markdown_cell(value, status: bool = False) -> str:
    text = _status_label(value) if status else _display(value)
    return html.escape(text, quote=False).replace("|", r"\|").replace("\n", " ")


def _markdown_table(headers: list[str], rows: list[list], status_column: int | None = None) -> str:
    lines = [
        "| " + " | ".join(_markdown_cell(item) for item in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        cells = [
            _markdown_cell(value, status=index == status_column)
            for index, value in enumerate(row)
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _markdown_blocks(blocks: list[tuple]) -> list[str]:
    lines = []
    for block in blocks:
        kind = block[0]
        if kind == "heading":
            lines.extend((f"### {block[1]}", ""))
        elif kind == "paragraph":
            lines.extend((html.escape(str(block[1]), quote=False), ""))
        elif kind == "list":
            lines.extend([
                f"- {html.escape(_display(item), quote=False)}" for item in block[1]
            ] or ["- None"])
            lines.append("")
        else:
            lines.extend((_markdown_table(block[1], block[2], block[3]), ""))
    return lines


def _html_table(headers: list[str], rows: list[list], status_column: int | None = None) -> str:
    header_html = "".join(f"<th>{html.escape(_display(item), quote=False)}</th>" for item in headers)
    row_html = []
    for row in rows:
        cells = []
        for index, value in enumerate(row):
            text = _status_label(value) if index == status_column else _display(value)
            if index == status_column:
                status_class = str(value).lower() if value in {
                    "pass", "fail", "skip", "missing", "error"
                } else "neutral"
                if status_class == "missing":
                    status_class = "missing"
                cells.append(
                    f'<td class="status status-{status_class}">{html.escape(text, quote=False)}</td>'
                )
            else:
                cells.append(f"<td>{html.escape(text, quote=False)}</td>")
        row_html.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<table><thead><tr>" + header_html + "</tr></thead><tbody>"
        + "".join(row_html) + "</tbody></table>"
    )


def _html_blocks(blocks: list[tuple]) -> str:
    rendered = []
    for block in blocks:
        kind = block[0]
        if kind == "heading":
            rendered.append(f"<h3>{html.escape(block[1], quote=False)}</h3>")
        elif kind == "paragraph":
            rendered.append(f"<p>{html.escape(str(block[1]), quote=False)}</p>")
        elif kind == "list":
            items = "".join(f"<li>{html.escape(_display(item), quote=False)}</li>" for item in block[1])
            rendered.append("<ul>" + (items or "<li>None</li>") + "</ul>")
        else:
            rendered.append(_html_table(block[1], block[2], block[3]))
    return "".join(rendered)


def _markdown_json(data: dict) -> str:
    raw = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
    longest_run = max((len(run) for run in re.findall(r"`+", raw)), default=0)
    fence = "`" * max(3, longest_run + 1)
    return f"{fence}json\n{raw}\n{fence}"


def _markdown(sections: dict, integrity: dict, evidence_json_sha256: str) -> str:
    disposition = sections["E"].get("disposition", {})
    lines = [
        "# Verification evidence",
        "",
        "## Disposition",
        "",
        f"**Recommendation:** {_display(disposition.get('recommendation'))}",
        "",
        REVIEWER_RECOMMENDATION + ".",
        "",
        PRIVACY_STATEMENT,
        "",
        "## Gate summary",
        "",
        _markdown_table(
            ["Gate", "Status", "Checks", "Passed", "Failed", "Skipped", "Runtime", "Static"],
            _summary_gate_rows(sections),
            status_column=1,
        ),
        "",
        f"Package SHA-256: `{integrity['package_sha256']}`",
        f"evidence.json SHA-256: `{evidence_json_sha256}`",
        "",
    ]
    for key, title in SECTION_TITLES:
        lines.extend((f"## {key}. {title}", ""))
        lines.extend(_markdown_blocks(_section_blocks(key, sections, integrity)))
        lines.extend(("### Raw JSON", "", _markdown_json(sections[key]), ""))
    lines.extend((GUARANTEE_BOUNDARIES, ""))
    return "\n".join(lines)


def _html(sections: dict, integrity: dict, evidence_json_sha256: str) -> str:
    disposition = sections["E"].get("disposition", {})
    html_sections = []
    for key, title in SECTION_TITLES:
        content = html.escape(
            json.dumps(sections[key], indent=2, ensure_ascii=False, sort_keys=True),
            quote=False,
        )
        html_sections.append(
            f"<section><h2>{key}. {html.escape(title, quote=False)}</h2>"
            f"{_html_blocks(_section_blocks(key, sections, integrity))}"
            f"<details><summary>Raw JSON</summary><pre>{content}</pre></details></section>"
        )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Verification evidence</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;"
        "color:#18212f;background:#f7f8fa}section,.summary{background:white;border:1px solid #d8dee8;"
        "border-radius:8px;padding:1rem 1.25rem;margin:1rem 0}table{border-collapse:collapse;width:100%;"
        "margin:.75rem 0 1.25rem}th,td{text-align:left;vertical-align:top;padding:.45rem .6rem;"
        "border:1px solid #d8dee8;overflow-wrap:anywhere}th,td.status{white-space:nowrap}th{background:#eef2f7}.status-pass{color:#176b36}"
        ".status-fail,.status-error{color:#a21d24}.status-skip,.status-missing{color:#8a5600}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:.88rem}details{margin-top:1rem}"
        "summary{cursor:pointer;font-weight:600}.hash{font-family:monospace}</style></head><body>"
        "<h1>Verification evidence</h1>"
        "<div class=\"summary\"><h2>Disposition</h2>"
        f"<p><strong>Recommendation:</strong> {html.escape(_display(disposition.get('recommendation')), quote=False)}</p>"
        f"<p>{html.escape(REVIEWER_RECOMMENDATION + '.', quote=False)}</p>"
        f"<p>{html.escape(PRIVACY_STATEMENT, quote=False)}</p></div>"
        "<section><h2>Gate summary</h2>"
        + _html_table(
            ["Gate", "Status", "Checks", "Passed", "Failed", "Skipped", "Runtime", "Static"],
            _summary_gate_rows(sections),
            status_column=1,
        )
        + "</section>"
        + f"<p class=\"hash\">Package SHA-256: {html.escape(integrity['package_sha256'], quote=False)}</p>"
        + f"<p class=\"hash\">evidence.json SHA-256: {html.escape(evidence_json_sha256, quote=False)}</p>"
        + "".join(html_sections)
        + f"<p>{html.escape(GUARANTEE_BOUNDARIES, quote=False)}</p>"
        + "</body></html>\n"
    )


def build(ctx: RunContext, out_dir: Path | None = None) -> dict:
    run_dir = ctx.run_dir.resolve()
    reader = RunReader(run_dir)
    context, context_status = reader.read_json(run_dir / "context.json")
    gate_documents = {}
    gates = {}
    for name in GATE_NAMES:
        gate_documents[name], gates[name] = _read_gate(reader, run_dir, name)
    output_runs = _output_runs(reader, run_dir)
    current_sha = git_sha(ctx.repo_root)
    current_dirty = git_dirty(ctx.repo_root)
    context_sha = context.get("git_sha") if context else None
    context_dirty = context.get("git_dirty") if context else None
    sha_mismatch = context_sha != current_sha if context_sha and current_sha else None
    identity = {
        "status": "present" if context_status == "present" else context_status,
        "repo": _repo_origin(ctx.repo_root),
        "git": {
            "run_time": {"git_sha": context_sha, "git_dirty": context_dirty},
            "built_at": {"git_sha": current_sha, "git_dirty": current_dirty},
            "sha_mismatch": sha_mismatch,
        },
        "processors": {
            "baseline": context.get("baseline") if context else None,
            "candidate": context.get("candidate") if context else None,
            "runs": output_runs,
        },
        "scenario_set": context.get("scenarios", []) if context else [],
        "mode": {
            "baseline": context.get("baseline") if context else None,
            "strict": context.get("strict") if context else None,
            "tolerance": context.get("tolerance") if context else None,
        },
        "tool_versions": {
            "python": platform.python_version(),
            "jsonschema": _package_version("jsonschema"),
            "PyYAML": _package_version("PyYAML"),
            "rustc": _tool_version(["rustc", "--version"]),
            "cargo": _tool_version(["cargo", "--version"]),
            "java": _tool_version(["java", "-version"]),
        },
        "tool_versions_label": "observed when the evidence package was built",
    }
    privacy = gate_documents.get("privacy")
    privacy_status = gates["privacy"]
    file_hashes = dict(sorted(reader.file_hashes.items()))
    git_info = {
        "run_time": context_sha,
        "built_at": current_sha,
        "sha_mismatch": sha_mismatch,
    }
    sections = {
        "A": identity,
        "B": _inventory_section(privacy, privacy_status),
        "C": _assessment_section(privacy, privacy_status),
        "D": {
            "status": "present",
            "gates": {name: _gate_summary(gate_documents[name], gates[name]) for name in GATE_NAMES},
        },
        "E": _disposition_section(privacy, privacy_status, gates),
        "F": {
            "status": "present",
            "files": file_hashes,
            "file_statuses": dict(sorted(reader.file_statuses.items())),
            "git_shas": git_info,
            "package_sha256_method": PACKAGE_HASH_METHOD,
        },
    }
    integrity = {
        "files": file_hashes,
        "file_statuses": dict(sorted(reader.file_statuses.items())),
        "git_shas": git_info,
        "package_sha256": None,
        "package_sha256_method": PACKAGE_HASH_METHOD,
    }
    evidence = _sanitize_text({"sections": sections, "integrity": integrity})
    integrity_hash = hashlib.sha256(canonical(evidence).encode("utf-8")).hexdigest()
    evidence["integrity"]["package_sha256"] = integrity_hash
    evidence_json = json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    evidence_json_hash = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
    markdown = _markdown(evidence["sections"], evidence["integrity"], evidence_json_hash)
    html_text = _html(evidence["sections"], evidence["integrity"], evidence_json_hash)

    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    content = {
        "evidence.json": evidence_json.encode("utf-8"),
        "evidence.md": markdown.encode("utf-8"),
        "evidence.html": html_text.encode("utf-8"),
    }
    for filename, payload in content.items():
        (evidence_dir / filename).write_bytes(payload)
    checksum_text = "".join(
        f"{hashlib.sha256(content[name]).hexdigest()}  {name}\n"
        for name in ("evidence.json", "evidence.md", "evidence.html")
    )
    content["evidence.sha256"] = checksum_text.encode("utf-8")
    (evidence_dir / "evidence.sha256").write_bytes(content["evidence.sha256"])

    if out_dir is not None:
        out_dir = Path(out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        for filename, payload in content.items():
            (out_dir / filename).write_bytes(payload)

    return {
        "run_dir": str(run_dir),
        "evidence_dir": str(evidence_dir),
        "files": list(content),
        "gates": gates,
        "disposition": evidence["sections"]["E"]["disposition"],
        "package_sha256": integrity_hash,
    }


__all__ = ["build"]
