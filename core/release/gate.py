"""Release Guardian (Reliability Addendum Module 6).

A repeatable release gate. Each check reports one honest status:

    PASS | FAIL | BLOCKED | SKIPPED | NOT_RUN | NOT_CONFIGURED

Rules that make this trustworthy rather than decorative:
- A check that was not actually run is never reported as PASS.
- A configured-but-unavailable tool is ``NOT_CONFIGURED``, never green.
- Critical checks (tests, lint, secret scan, security regression) block the
  deploy when they fail. ``NOT_CONFIGURED`` never blocks on its own, but it is
  always visible.
- The gate only observes: it never deploys, never mutates production, and never
  prints a secret value (only the file path where a pattern was found).
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from config.settings import PROJECT_ROOT

# ----------------------------------------------------------------------
# Result model
# ----------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
SKIPPED = "SKIPPED"
NOT_RUN = "NOT_RUN"
NOT_CONFIGURED = "NOT_CONFIGURED"

Runner = Callable[[list[str], Path, float], tuple[int, str]]


@dataclass
class Check:
    id: str
    name: str
    critical: bool
    fn: Callable[["GateContext"], dict[str, Any]]


@dataclass
class GateContext:
    repo_root: Path
    runner: Runner
    timeout: float = 900.0


def _default_runner(command: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, output[-4000:]
    except FileNotFoundError as exc:
        return 127, f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"


def _result(status: str, detail: str, *, critical: bool, command: str | None = None) -> dict[str, Any]:
    return {"status": status, "detail": detail, "critical": critical, "command": command}


# ----------------------------------------------------------------------
# Individual checks
# ----------------------------------------------------------------------

_EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
    "data",
    "logs",
    ".idea",
    ".vscode",
}

_SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
]
# A keyword assignment only counts when the value looks like a real secret —
# placeholders and obvious examples are ignored to avoid false alarms.
_KEYWORD_ASSIGN = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b\s*[=:]\s*[\"']([^\"']{12,})[\"']"
)
_PLACEHOLDER = re.compile(
    r"(?i)(example|placeholder|changeme|change_me|your[_-]?|test|dummy|sample|xxxx|<[^>]+>|\$\{|\.\.\.)"
)


def _iter_text_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _EXCLUDE_DIRS for part in path.parts):
            continue
        # The local .env is an intentional, gitignored secret store — it is not
        # release content. .env.example IS scanned.
        if path.name == ".env":
            continue
        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2", ".zip", ".exe", ".dll", ".pyc"}:
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
        except OSError:
            continue
        yield path


# Keyword assignments are noisy in source code (e.g. `token[192];`), so the
# heuristic only runs for configuration-like files where a real secret would
# actually be committed. High-confidence provider patterns are scanned in every
# file regardless.
_CONFIG_SUFFIXES = {".env", ".ini", ".cfg", ".yaml", ".yml", ".toml", ".json", ".properties", ".conf"}


def _is_config_like(path: Path) -> bool:
    if path.suffix.lower() in _CONFIG_SUFFIXES:
        return True
    name = path.name.lower()
    return name.endswith(".env") or "credential" in name or "secret" in name


def _secret_scan(root: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in _iter_text_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = str(path.relative_to(root))
        for kind, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                findings.append({"file": rel, "kind": kind})
        if not _is_config_like(path):
            continue
        for match in _KEYWORD_ASSIGN.finditer(text):
            value = match.group(2)
            if _PLACEHOLDER.search(value) or _PLACEHOLDER.search(match.group(1)):
                continue
            findings.append({"file": rel, "kind": f"keyword:{match.group(1).lower()}"})
    return findings


def check_secret_scan(ctx: GateContext) -> dict[str, Any]:
    findings = _secret_scan(ctx.repo_root)
    if not findings:
        return _result(PASS, "No secret patterns found in tracked source.", critical=True)
    locations = ", ".join(sorted({f["file"] for f in findings})[:10])
    # Never echo the matched value — only where it was seen.
    return _result(
        FAIL,
        f"{len(findings)} potential secret(s) found (values redacted): {locations}",
        critical=True,
    )


def _migration_heads(root: Path) -> tuple[bool, list[str], str]:
    versions = root / "database" / "migrations" / "versions"
    if not versions.is_dir():
        return False, [], "no Alembic versions directory"
    revision_re = re.compile(r"^\s*revision(?:\s*:\s*[\w\[\], ]+)?\s*=\s*[\"']([^\"']+)[\"']", re.M)
    down_re = re.compile(
        r"^\s*down_revision(?:\s*:\s*[\w\[\], |]+)?\s*=\s*(?:[\"']([^\"']+)[\"']|None)", re.M
    )
    revisions: set[str] = set()
    referenced: set[str] = set()
    for path in versions.glob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        rev = revision_re.search(text)
        if rev:
            revisions.add(rev.group(1))
        down = down_re.search(text)
        if down and down.group(1):
            referenced.add(down.group(1))
    if not revisions:
        return False, [], "no revisions found"
    heads = sorted(revisions - referenced)
    return True, heads, ""


def check_migration_heads(ctx: GateContext) -> dict[str, Any]:
    configured, heads, reason = _migration_heads(ctx.repo_root)
    if not configured:
        return _result(NOT_CONFIGURED, f"Migration head check unavailable: {reason}.", critical=False)
    if len(heads) == 1:
        return _result(PASS, f"Single migration head: {heads[0]}.", critical=False)
    return _result(FAIL, f"Expected one migration head, found {len(heads)}: {heads}", critical=False)


def _module_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


def check_unit_tests(ctx: GateContext) -> dict[str, Any]:
    command = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
    rc, output = ctx.runner(command, ctx.repo_root, ctx.timeout)
    if rc == 127:
        return _result(NOT_CONFIGURED, output, critical=True)
    if rc == 124:
        return _result(BLOCKED, output, critical=True, command=" ".join(command))
    if rc == 0:
        return _result(PASS, "Full test suite passed.", critical=True, command=" ".join(command))
    tail = output.strip().splitlines()[-1] if output.strip() else "no output"
    return _result(FAIL, f"pytest exited {rc}: {tail}", critical=True, command=" ".join(command))


def check_security_regression(ctx: GateContext) -> dict[str, Any]:
    files = [
        "tests/test_security.py",
        "tests/test_rbac.py",
        "tests/test_module3_audit.py",
        "tests/test_missions.py",
    ]
    present = [f for f in files if (ctx.repo_root / f).is_file()]
    if not present:
        return _result(NOT_CONFIGURED, "No security regression test files present.", critical=True)
    command = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *present]
    rc, output = ctx.runner(command, ctx.repo_root, ctx.timeout)
    if rc == 124:
        return _result(BLOCKED, output, critical=True, command=" ".join(command))
    if rc == 0:
        return _result(PASS, f"Security regression set passed ({len(present)} files).",
                       critical=True, command=" ".join(command))
    tail = output.strip().splitlines()[-1] if output.strip() else "no output"
    return _result(FAIL, f"security regression pytest exited {rc}: {tail}",
                   critical=True, command=" ".join(command))


def check_lint(ctx: GateContext) -> dict[str, Any]:
    if not _module_available("ruff"):
        return _result(NOT_CONFIGURED, "ruff is not installed.", critical=True)
    command = [sys.executable, "-m", "ruff", "check", "."]
    rc, output = ctx.runner(command, ctx.repo_root, ctx.timeout)
    if rc == 0:
        return _result(PASS, "Lint clean.", critical=True, command=" ".join(command))
    return _result(FAIL, f"ruff exited {rc}: {output.strip().splitlines()[-1] if output.strip() else ''}",
                   critical=True, command=" ".join(command))


def check_type_check(ctx: GateContext) -> dict[str, Any]:
    if not _module_available("mypy"):
        return _result(NOT_CONFIGURED, "mypy is not installed; static typing is not enforced here.",
                       critical=False)
    command = [sys.executable, "-m", "mypy", "."]
    rc, output = ctx.runner(command, ctx.repo_root, ctx.timeout)
    if rc == 0:
        return _result(PASS, "Type check clean.", critical=False, command=" ".join(command))
    return _result(FAIL, f"mypy exited {rc}", critical=False, command=" ".join(command))


def check_dependency_audit(ctx: GateContext) -> dict[str, Any]:
    if not _module_available("pip_audit"):
        return _result(NOT_CONFIGURED, "pip-audit is not installed; no vulnerability scan was run.",
                       critical=False)
    command = [sys.executable, "-m", "pip_audit", "--progress-spinner", "off"]
    rc, output = ctx.runner(command, ctx.repo_root, ctx.timeout)
    if rc == 0:
        return _result(PASS, "No known vulnerable dependencies.", critical=False, command=" ".join(command))
    return _result(FAIL, f"pip-audit exited {rc}", critical=False, command=" ".join(command))


def check_build(ctx: GateContext) -> dict[str, Any]:
    desktop = ctx.repo_root / "apps" / "desktop"
    package_json = desktop / "package.json"
    node_modules = desktop / "node_modules"
    if not package_json.is_file():
        return _result(NOT_CONFIGURED, "No desktop package.json to build.", critical=False)
    if not node_modules.is_dir():
        return _result(NOT_CONFIGURED, "Desktop dependencies not installed (npm ci required).",
                       critical=False)
    command = ["npm", "run", "build"]
    rc, output = ctx.runner(command, desktop, ctx.timeout)
    if rc == 127:
        return _result(NOT_CONFIGURED, "npm is not available on PATH.", critical=False)
    if rc == 0:
        return _result(PASS, "Desktop build succeeded.", critical=False, command=" ".join(command))
    return _result(FAIL, f"desktop build exited {rc}", critical=False, command=" ".join(command))


CHECKS: tuple[Check, ...] = (
    Check("unit_tests", "Full unit/integration test suite", True, check_unit_tests),
    Check("lint", "Static lint", True, check_lint),
    Check("secret_scan", "Secret scan", True, check_secret_scan),
    Check("security_regression", "Security regression tests", True, check_security_regression),
    Check("migration_heads", "Migration head consistency", False, check_migration_heads),
    Check("type_check", "Static type check", False, check_type_check),
    Check("dependency_audit", "Dependency vulnerability audit", False, check_dependency_audit),
    Check("build", "Front-end build", False, check_build),
)

CHECK_IDS = tuple(c.id for c in CHECKS)


# ----------------------------------------------------------------------
# Gate
# ----------------------------------------------------------------------

def run_gate(
    *,
    only: Iterable[str] | None = None,
    skip: Iterable[str] | None = None,
    runner: Runner | None = None,
    repo_root: Path | None = None,
    timeout: float = 900.0,
) -> dict[str, Any]:
    root = repo_root or PROJECT_ROOT
    ctx = GateContext(repo_root=root, runner=runner or _default_runner, timeout=timeout)
    only_set = set(only) if only else None
    skip_set = set(skip or ())

    items: list[dict[str, Any]] = []
    started = time.time()
    for check in CHECKS:
        if only_set is not None and check.id not in only_set:
            continue
        if check.id in skip_set:
            items.append({"id": check.id, "name": check.name, "critical": check.critical,
                          "status": SKIPPED, "detail": "Skipped by request.", "duration_ms": 0})
            continue
        t0 = time.time()
        try:
            outcome = check.fn(ctx)
        except Exception as exc:  # a broken check must not silently pass
            outcome = _result(FAIL, f"check raised {type(exc).__name__}: {exc}", critical=check.critical)
        outcome.update({
            "id": check.id,
            "name": check.name,
            "critical": check.critical,
            "duration_ms": int((time.time() - t0) * 1000),
        })
        items.append(outcome)

    blockers = [
        i["id"] for i in items
        if i["critical"] and i["status"] in (FAIL, BLOCKED)
    ]
    failed_non_critical = [
        i["id"] for i in items
        if not i["critical"] and i["status"] in (FAIL, BLOCKED)
    ]
    deploy_blocked = bool(blockers)
    if deploy_blocked or failed_non_critical:
        overall = FAIL
    else:
        overall = PASS

    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1

    return {
        "gate": "release_guardian",
        "status": overall,
        "deploy_blocked": deploy_blocked,
        "blockers": blockers,
        "non_critical_failures": failed_non_critical,
        "counts": counts,
        "duration_ms": int((time.time() - started) * 1000),
        "items": items,
        "note": (
            "A release is blocked while any critical check fails. Checks that "
            "are NOT_CONFIGURED or NOT_RUN are reported honestly and never "
            "counted as passing."
        ),
    }


def summarize(report: dict[str, Any]) -> str:
    lines = [f"Release gate: {report['status']} (deploy_blocked={report['deploy_blocked']})"]
    for item in report["items"]:
        lines.append(f"  [{item['status']:<14}] {item['id']}: {item['detail'][:120]}")
    return "\n".join(lines)
