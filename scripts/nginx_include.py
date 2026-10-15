#!/usr/bin/env python3
"""Add one `include` line to the HTTPS server block of a domain in existing nginx config files.

    python3 nginx_include.py --domain didrah.com --include "snippets/assistant-dashboard.conf" [--dirs /etc/nginx/sites-enabled /etc/nginx/conf.d]

Exit codes: 0 = added or already present, 2 = no matching HTTPS server block, 3 = several matches (nothing changed).
The file is backed up first, OUTSIDE the nginx directories (nginx would load a backup left in sites-enabled):
--backup-dir/NAME.bak. The caller must still run `nginx -t` and restore that backup on failure.
"""

import argparse
import pathlib
import re
import shutil
import sys

DEFAULT_DIRS = ["/etc/nginx/sites-enabled", "/etc/nginx/conf.d"]
DEFAULT_BACKUP_DIR = "/var/backups/assistant-nginx"


def mask_comments(text: str) -> str:
    """Blank out # comments (same length, so offsets stay valid)."""
    return re.sub(r"#[^\n]*", lambda m: " " * len(m.group(0)), text)


def server_blocks(text: str) -> list[tuple[int, int]]:
    """(start, end) offsets of each `server { ... }` block; `end` is the index of the closing brace."""
    masked = mask_comments(text)
    blocks = []
    for m in re.finditer(r"(?<![\w$.-])server\s*\{", masked):
        depth, i = 0, m.end() - 1
        while i < len(masked):
            if masked[i] == "{":
                depth += 1
            elif masked[i] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append((m.start(), i))
                    break
            i += 1
    return blocks


def matches(block: str, domain: str) -> bool:
    masked = mask_comments(block)
    names = [t for m in re.finditer(r"server_name\s+([^;]+);", masked) for t in m.group(1).split()]
    return domain in names and re.search(r"\blisten\s+[^;]*\b443\b", masked) is not None


def add_include(path: pathlib.Path, domain: str, include: str, backup_dir: pathlib.Path) -> str:
    """Returns 'added', 'present' or 'nomatch' for one file."""
    text = path.read_text(encoding="utf-8")
    hits = [(s, e) for s, e in server_blocks(text) if matches(text[s : e + 1], domain)]
    if not hits:
        return "nomatch"
    if len(hits) > 1:
        return "multiple"
    start, end = hits[0]
    if include in text[start : end + 1]:
        return "present"
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_dir / (path.name + ".bak"))
    line = f"\n    include {include};  # added by the assistant's server_setup.sh\n"
    path.write_text(text[:end] + line + text[end:], encoding="utf-8")
    return "added"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--include", required=True)
    ap.add_argument("--dirs", nargs="*", default=DEFAULT_DIRS)
    ap.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR)
    args = ap.parse_args(argv)

    files = [p for d in args.dirs for p in sorted(pathlib.Path(d).glob("*")) if p.is_file() and not p.name.endswith((".bak-assistant", ".bak", "~"))]
    candidates = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        n = sum(1 for s, e in server_blocks(text) if matches(text[s : e + 1], args.domain))
        if n:
            candidates.append((f, n))
    if not candidates:
        print(f"no HTTPS server block for {args.domain} found in {', '.join(args.dirs)}")
        return 2
    if len(candidates) > 1 or candidates[0][1] > 1:
        print("several HTTPS server blocks match; edit by hand: " + ", ".join(str(f) for f, _ in candidates))
        return 3
    f = candidates[0][0]
    result = add_include(f, args.domain, args.include, pathlib.Path(args.backup_dir))
    print(f"{result}: {f}")
    return 0 if result in ("added", "present") else 2


if __name__ == "__main__":
    sys.exit(main())
