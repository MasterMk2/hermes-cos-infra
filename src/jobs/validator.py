#!/usr/bin/env python3
"""Deterministic acceptance validator (independent from the coding agent).

Never asks the agent whether the work is done. Evaluates each acceptance
criterion with fixed verification kinds:

  file_exists      params: {"path": rel}            file present in repo
  function_exists  params: {"file": rel, "name": n}  top-level def/class in py file
  test_command     params: {} (uses job.tests_command, must be allowlisted)
  git_scope        params: {} (changed files within repo allowed_paths)
  artifact_contains params: {"path": rel, "text": s}

Only allowlisted test commands are executed. Arbitrary shell is never run.
"""

import ast
import subprocess
from pathlib import Path


class VerificationError(Exception):
    pass


def _safe_repo_path(repo_path, rel):
    if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise VerificationError(f"unsafe path: {rel!r}")
    p = (Path(repo_path) / rel).resolve()
    if not str(p).startswith(str(Path(repo_path).resolve())):
        raise VerificationError(f"path escapes repo: {rel!r}")
    return p


def _check_file_exists(repo_path, params):
    p = _safe_repo_path(repo_path, params.get("path"))
    return p.is_file(), f"exists={p.is_file()}"


def _check_function_exists(repo_path, params):
    rel = params.get("file")
    name = params.get("name")
    p = _safe_repo_path(repo_path, rel)
    if not p.is_file():
        return False, f"file missing: {rel}"
    tree = ast.parse(p.read_text(encoding="utf-8"))
    names = {
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    return name in names, f"{'found' if name in names else 'missing'}: {name}"


def _check_git_scope(repo_path, params, changed_files):
    allowed = params.get("allowed_paths") or []
    extra = [f for f in changed_files if not any(f.startswith(a) for a in allowed)]
    return (not extra), (f"changed={len(changed_files)} extra={extra}" if extra
                         else f"changed={len(changed_files)} all in scope")


def _check_artifact_contains(repo_path, params):
    p = _safe_repo_path(repo_path, params.get("path"))
    if not p.is_file():
        return False, f"file missing: {params.get('path')}"
    hit = params.get("text", "") in p.read_text(encoding="utf-8", errors="replace")
    return hit, f"contains={hit}"


def run_tests(repo_path, command, allowlist, timeout_s):
    if not command or command not in (allowlist or []):
        return "BLOCKED", "test command not in repository allowlist"
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(repo_path), capture_output=True,
            text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT", f"tests exceeded {timeout_s}s"
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [l for l in out.splitlines() if l.strip()]
    summary = "\n".join(lines[-12:])[:1500]
    return ("PASS" if proc.returncode == 0 else "FAIL"), summary


def evaluate(job, repo_cfg, repo_path, changed_files, test_timeout_s=600):
    """Returns (acceptance_results, tests_status, tests_summary)."""
    allowlist = repo_cfg.get("test_commands", [])
    results = []
    tests_status, tests_summary = "NOT_RUN", None
    for ac in job.get("acceptance_criteria", []):
        kind = ac.get("verification", {}).get("kind")
        params = ac.get("verification", {}).get("params", {})
        try:
            if kind == "test_command":
                cmd = job.get("tests_command")
                tests_status, tests_summary = run_tests(
                    repo_path, cmd, allowlist, test_timeout_s)
                ok, detail = (tests_status == "PASS"), f"tests={tests_status}"
            elif kind == "file_exists":
                ok, detail = _check_file_exists(repo_path, params)
            elif kind == "function_exists":
                ok, detail = _check_function_exists(repo_path, params)
            elif kind == "git_scope":
                ok, detail = _check_git_scope(
                    repo_path, {"allowed_paths": repo_cfg.get("allowed_paths", [])},
                    changed_files)
            elif kind == "artifact_contains":
                ok, detail = _check_artifact_contains(repo_path, params)
            else:
                ok, detail = False, f"unknown verification kind: {kind}"
        except (VerificationError, SyntaxError, OSError) as exc:
            ok, detail = False, f"error: {exc}"
        results.append({
            "id": ac.get("id"),
            "description": ac.get("description"),
            "verification": kind,
            "status": "PASS" if ok else "FAIL",
            "detail": detail,
        })
    return results, tests_status, tests_summary
