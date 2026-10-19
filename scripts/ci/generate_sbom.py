#!/usr/bin/env python3
"""
CollectFlow CycloneDX SBOM Generator
Generates CycloneDX 1.4/1.5 JSON SBOM and SHA-256 checksum for backend or frontend.
"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def generate_backend_sbom(repo_root: Path, output_path: Path) -> str:
    req_file = repo_root / "requirements.txt"
    if not req_file.exists():
        raise FileNotFoundError(f"Missing requirements.txt at {req_file}")

    venv_pip_audit = repo_root / "venv" / "bin" / "pip-audit"
    cmd = [str(venv_pip_audit) if venv_pip_audit.exists() else "pip-audit", "-r", str(req_file), "--format", "cyclonedx-json"]
    res = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True)
    if res.returncode != 0 and not res.stdout:
        raise RuntimeError(f"pip-audit failed: {res.stderr}")

    # Extract JSON object (ignoring informational banner lines like 'No known vulnerabilities found')
    stdout = res.stdout.strip()
    json_start = stdout.find("{")
    if json_start == -1:
        raise RuntimeError(f"Could not find valid JSON in pip-audit output: {stdout}")

    raw_json = stdout[json_start:]
    data = json.loads(raw_json)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    checksum = compute_sha256(output_path)
    checksum_file = output_path.with_suffix(output_path.suffix + ".sha256")
    checksum_file.write_text(f"{checksum}  {output_path.name}\n", encoding="utf-8")
    return checksum

def generate_frontend_sbom(repo_root: Path, output_path: Path) -> str:
    pkg_file = repo_root / "package.json"
    if not pkg_file.exists():
        raise FileNotFoundError(f"Missing package.json at {pkg_file}")

    cmd = ["npm", "sbom", "--sbom-format", "cyclonedx"]
    res = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"npm sbom failed: {res.stderr}")

    stdout = res.stdout.strip()
    json_start = stdout.find("{")
    if json_start == -1:
        raise RuntimeError(f"Could not find valid JSON in npm sbom output: {stdout}")

    raw_json = stdout[json_start:]
    data = json.loads(raw_json)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    checksum = compute_sha256(output_path)
    checksum_file = output_path.with_suffix(output_path.suffix + ".sha256")
    checksum_file.write_text(f"{checksum}  {output_path.name}\n", encoding="utf-8")
    return checksum

def main() -> int:
    parser = argparse.ArgumentParser(description="CollectFlow SBOM Generator")
    parser.add_argument("--repo-type", choices=["backend", "frontend"], required=True, help="Repository type")
    parser.add_argument("--root", default=".", help="Repository root path")
    parser.add_argument("--output", required=True, help="Output SBOM JSON path")
    args = parser.parse_args()

    repo_root = Path(args.root).resolve()
    output_path = Path(args.output).resolve()

    print(f"Generating CycloneDX SBOM for {args.repo_type} from {repo_root}...")
    if args.repo_type == "backend":
        checksum = generate_backend_sbom(repo_root, output_path)
    else:
        checksum = generate_frontend_sbom(repo_root, output_path)

    print(f"SBOM written to {output_path}")
    print(f"SHA-256: {checksum}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
