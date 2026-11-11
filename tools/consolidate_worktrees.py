"""Archive and remove the explicitly inventoried legacy Factorio worktrees.

One-off maintenance for the user's 2026-11-11 cleanup request. Default is read-only.
No branch deletion happens here; verified archives precede worktree removal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


LEGACY = Path(r"C:\Users\NEC\Documents\Factorio").resolve()
WORKTREES = LEGACY / "runtime" / "deterministic" / "worktrees"
DESTINATION = Path(__file__).resolve().parents[1] / "runtime" / "housekeeping"


def git(*args: str, cwd: Path = LEGACY) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def inventory() -> list[dict]:
    records = []
    for block in git("worktree", "list", "--porcelain").split("\n\n"):
        info = dict(line.split(" ", 1) if " " in line else (line, True)
                    for line in block.splitlines())
        path = Path(info["worktree"]).resolve()
        if path == LEGACY:
            continue
        # Validate the final absolute target before any archive/removal operation.
        if path.parent != WORKTREES.resolve() or path.is_symlink() or info.get("locked"):
            raise RuntimeError(f"unexpected or locked worktree: {path}")
        records.append({"path": str(path), "head": info["HEAD"],
                        "branch": info.get("branch"),
                        "changes": git("status", "--porcelain=v1", "--untracked-files=all", cwd=path).splitlines()})
    return records


def archive(record: dict) -> dict:
    path = Path(record["path"])
    target = DESTINATION / "worktrees" / (path.name + ".zip")
    target.parent.mkdir(parents=True, exist_ok=True)
    entries = list(path.rglob("*"))
    if any(p.is_symlink() or not p.resolve().is_relative_to(path) for p in entries):
        raise RuntimeError(f"worktree contains an external file: {path}")
    files = sorted(p for p in entries if p.is_file() and p.name != ".git")
    if any(p.relative_to(path).as_posix() == "ARCHIVE_MANIFEST.json" for p in files):
        raise RuntimeError("reserved manifest filename already exists")
    hashes = {}
    if target.exists():
        with zipfile.ZipFile(target) as backup:
            manifest = json.loads(backup.read("ARCHIVE_MANIFEST.json"))
            if any(manifest.get(k) != v for k, v in record.items()):
                raise RuntimeError("existing backup belongs to another worktree state")
            hashes = manifest["sha256"]
            if set(hashes) != {p.relative_to(path).as_posix() for p in files}:
                raise RuntimeError("file set changed after interrupted backup")
    else:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as output:
            for source in files:
                name = source.relative_to(path).as_posix()
                data = source.read_bytes()
                hashes[name] = hashlib.sha256(data).hexdigest()
                output.writestr(name, data)
            output.writestr("ARCHIVE_MANIFEST.json", json.dumps({**record, "sha256": hashes}, indent=2))
    # Verify the actual archived bytes AND that no source changed during backup.
    with zipfile.ZipFile(target) as backup:
        if backup.testzip() is not None:
            raise RuntimeError(f"archive CRC failure: {target}")
        for name, digest in hashes.items():
            if (hashlib.sha256(backup.read(name)).hexdigest() != digest
                    or hashlib.sha256((path / name).read_bytes()).hexdigest() != digest):
                raise RuntimeError(f"archive/source changed: {path / name}")
    return {**record, "archive": str(target), "files": len(files),
            "archive_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    records = inventory()
    if not args.apply:
        print(json.dumps({"worktrees": len(records), "dirty": sum(bool(r["changes"]) for r in records),
                          "action": "archive all files, verify, then remove; preserve main worktree"}))
        return
    DESTINATION.mkdir(parents=True, exist_ok=True)
    bundle = DESTINATION / "legacy-all-refs.bundle"
    if not bundle.exists():
        git("bundle", "create", str(bundle), "--all")
    git("bundle", "verify", str(bundle))
    report_path = DESTINATION / "cleanup-report.json"
    report = (json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists()
              else {"legacy_repo": str(LEGACY), "bundle": str(bundle), "worktrees": []})
    for record in records:
        saved = archive(record)
        # Recheck registration, HEAD and dirty paths before removing this exact target.
        path = Path(record["path"])
        if (git("rev-parse", "HEAD", cwd=path) != record["head"]
                or git("status", "--porcelain=v1", "--untracked-files=all", cwd=path).splitlines() != record["changes"]):
            raise RuntimeError(f"worktree changed during cleanup: {record['path']}")
        git("worktree", "remove", "--force", record["path"])
        saved["removed"] = not Path(record["path"]).exists()
        if not saved["removed"]:
            raise RuntimeError("worktree removal did not remove the directory")
        report["worktrees"].append(saved)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"removed": len(report["worktrees"]), "name": Path(record["path"]).name,
                          "archived_files": saved["files"]}), flush=True)
    print(json.dumps({"remaining": git("worktree", "list", "--porcelain"), "report": str(report_path)}))


if __name__ == "__main__":
    main()
