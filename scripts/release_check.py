"""Check a source snapshot before sharing; optionally archive exactly those bytes."""

import argparse
import io
import ipaddress
import re
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IP = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
PRIVATE_NETWORKS = [
    ipaddress.ip_network((10 << 24, 8)),
    ipaddress.ip_network(((172 << 24) + (16 << 16), 12)),
    ipaddress.ip_network(((192 << 24) + (168 << 16), 16)),
]
PATTERNS = {
    "deployment domain": re.compile(r"(?:focus" + r"aim|focus" + r"china|vemic)\.com", re.I),
    "internal storage terminology": re.compile(r"\bF" + r"FS\b"),
    "internal port": re.compile(r":(?:307" + r"75|320" + r"04)\b"),
    "private key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "API credential": re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9]{30,})\b"
    ),
}
LOCAL_DIRS = {".git", ".deepeval", ".claude", ".codex", "logs", "data", ".venv", "__pycache__"}


def findings(name: str, data: bytes) -> list[str]:
    path = Path(name)
    errors = []
    if (
        any(part in LOCAL_DIRS for part in path.parts)
        or (path.name.startswith(".env") and path.name != ".env.example")
        or path.suffix in {".pem", ".key", ".p12", ".pfx"}
    ):
        errors.append(f"{name}: prohibited local/credential file")
    text = data.decode("utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), 1):
        for label, pattern in PATTERNS.items():
            if pattern.search(line):
                errors.append(f"{name}:{number}: {label}")
        for match in IP.finditer(line):
            try:
                address = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            if any(address in network for network in PRIVATE_NETWORKS):
                errors.append(f"{name}:{number}: private network address")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write checked source as tar.gz (outside repository)")
    args = parser.parse_args()
    if args.output and args.output.resolve().is_relative_to(ROOT):
        parser.error("Place the archive outside the repository")
    names = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    snapshot = {}
    errors = []
    for name in sorted(set(names) - {""}):
        path = ROOT / name
        if path.is_symlink():
            errors.append(f"{name}: symlink requires explicit review")
            continue
        if not path.exists():
            continue  # Working-tree deletion is excluded from the snapshot.
        if not path.is_file():
            errors.append(f"{name}: unsupported source entry")
            continue
        data = path.read_bytes()
        snapshot[name] = (data, path.stat().st_mode & 0o777)
        errors.extend(findings(name, data))
    if errors:
        print("Release check failed (values redacted):\n" + "\n".join(errors))
        return 1
    print(f"PASS: {len(snapshot)} source files; formal attribution retained.")
    if args.output:
        with tarfile.open(args.output, "x:gz") as archive:
            for name, (data, mode) in snapshot.items():
                info = tarfile.TarInfo("Mentarc-EvoAgent/" + name)
                info.size, info.mode = len(data), mode
                archive.addfile(info, io.BytesIO(data))
        print(f"Archive: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
