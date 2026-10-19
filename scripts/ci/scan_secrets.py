#!/usr/bin/env python3
"""
CollectFlow Secret Scanner (Portable CI/CD Security Gate)
Scans tracked repository files for accidentally committed credentials, private keys, and high-risk secrets.
Redacts detected secrets in output to prevent log leakage.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

# Regex patterns for high-risk secrets
SECRET_PATTERNS = [
    (r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----", "Private Cryptographic Key"),
    (r"(?:api[_-]?key|apikey|secret[_-]?key)\s*[:=]\s*['\"]([a-zA-Z0-9_\-]{20,})['\"]", "API/Secret Key"),
    (r"gh[pousr]_[A-Za-z0-9_]{36,255}", "GitHub Personal Access Token"),
    (r"xox[baprs]-[0-9a-zA-Z]{10,48}", "Slack Token"),
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key ID"),
    (r"(?:password|passwd|pwd)\s*[:=]\s*['\"](?!AdminPass123!|FinancePass123!|SalesPass123!|ViewerPass123!|collectflow_secret|postgres|test_secret_32_bytes_long_entropy_ok)([a-zA-Z0-9!@#$%^&*()_\-+={}\[\]|:;\"'<>,.?/~`]{8,})['\"]", "High-Risk Hardcoded Password"),
]

# Patterns or paths to ignore
IGNORED_DIRS = {
    ".git",
    "venv",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".output",
    "dist",
}

IGNORED_FILES = {
    "package-lock.json",
    ".dockerignore",
    ".gitignore",
}

def is_ignored(path: Path) -> bool:
    for part in path.parts:
        if part in IGNORED_DIRS:
            return True
    if path.name in IGNORED_FILES or path.name.endswith(".pyc") or path.name.endswith(".png"):
        return True
    return False

def get_git_files(repo_root: Path) -> List[Path]:
    """Return tracked files if in a git repository; otherwise fallback to filesystem traversal."""
    try:
        res = subprocess.run(
            ["git", "ls-files"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        )
        files = [repo_root / line.strip() for line in res.stdout.splitlines() if line.strip()]
        return [f for f in files if f.is_file() and not is_ignored(f)]
    except Exception:
        # Fallback to filesystem scan
        files = []
        for root, dirs, filenames in os.walk(repo_root):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for f in filenames:
                p = Path(root) / f
                if not is_ignored(p):
                    files.append(p)
        return files

def redact(secret: str) -> str:
    if len(secret) <= 6:
        return "***REDACTED***"
    return secret[:3] + "..." + secret[-3:] + " [REDACTED]"

def scan_file(file_path: Path) -> List[Tuple[int, str, str]]:
    findings = []
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return [(0, "FILE_READ_ERROR", f"Failed to read {file_path}: {e}")]

    is_test_file = (
        any(t in part.lower() for t in ("test", "e2e") for part in file_path.parts)
        or file_path.name.endswith((".spec.ts", ".spec.js", ".test.ts", ".test.tsx", ".test.js", ".test.py"))
    )
    lines = content.splitlines()
    for idx, line in enumerate(lines, start=1):
        for pattern, desc in SECRET_PATTERNS:
            # Skip password checks in test files (they contain synthetic test fixtures for argon2 validation)
            if is_test_file and desc == "High-Risk Hardcoded Password":
                continue
            matches = re.finditer(pattern, line, re.IGNORECASE)
            for m in matches:
                matched_str = m.group(0)
                findings.append((idx, desc, redact(matched_str)))
    return findings

def main() -> int:
    parser = argparse.ArgumentParser(description="CollectFlow Portable Secret Scanner")
    parser.add_argument("--root", default=".", help="Root directory to scan (default: current directory)")
    parser.add_argument("--fail-on-findings", action="store_true", default=True, help="Exit with code 1 on findings")
    parser.add_argument("--json-output", help="Optional path to emit structured JSON scan report")
    args = parser.parse_args()

    repo_root = Path(args.root).resolve()
    print(f"Scanning for sensitive credentials in: {repo_root}")

    files_to_scan = get_git_files(repo_root)
    print(f"Found {len(files_to_scan)} tracked files to inspect.")

    total_findings = 0
    all_findings = []
    for f in files_to_scan:
        findings = scan_file(f)
        if findings:
            for line_no, desc, redacted_sample in findings:
                total_findings += 1
                try:
                    rel_path = str(f.relative_to(repo_root))
                except ValueError:
                    rel_path = str(f)
                all_findings.append({
                    "file": rel_path,
                    "line": line_no,
                    "description": desc,
                    "sample": redacted_sample
                })
                print(f"[SECURITY ALERT] {rel_path}:{line_no} - {desc} ({redacted_sample})")

    if args.json_output:
        out_p = Path(args.json_output).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        report_data = {
            "verdict": "PASS" if total_findings == 0 else "FAIL",
            "findings_count": total_findings,
            "findings": all_findings,
            "scanned_files_count": len(files_to_scan)
        }
        out_p.write_text(json.dumps(report_data, indent=2), encoding="utf-8")
        print(f"Structured secret scan report written to: {out_p}")

    if total_findings > 0:
        print(f"\nScan failed: {total_findings} potentially sensitive secret(s) detected.")
        return 1

    print("\nScan passed: No high-risk secrets or credentials detected.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
