#!/usr/bin/env python3
"""Reject common secrets and personal deployment identifiers from tracked blobs."""

import argparse
import ipaddress
from pathlib import Path
import re
import subprocess
import sys

PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:OPENSSH |RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    "SSH public key": re.compile(rb"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+) [A-Za-z0-9+/=]{40,}"),
    "access token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16})"),
    "credential in URL": re.compile(rb"[a-z]+://[^\s/:]+:[^\s/@]+@"),
    "FRP token": re.compile(rb"(?:auth\.token\s*=|frp_token\s*:)\s*[\"']?[a-fA-F0-9]{32,}"),
    "personal filesystem path": re.compile(rb"(?:[A-Za-z]:[\\/]Users[\\/][^\s<\"']+|/home/(?!<|\$)[a-zA-Z0-9_.-]+/)"),
}
IP_PATTERN = re.compile(rb"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
EMAIL_PATTERN = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SAFE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "0.0.0.0/32", "127.0.0.0/8", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
))


def findings(data):
    found = []
    for category, pattern in PATTERNS.items():
        for match in pattern.finditer(data):
            found.append((data.count(b"\n", 0, match.start()) + 1, category))
    for match in IP_PATTERN.finditer(data):
        try:
            address = ipaddress.ip_address(match.group().decode())
        except ValueError:
            continue
        if not any(address in network for network in SAFE_NETWORKS):
            found.append((data.count(b"\n", 0, match.start()) + 1, "deployment IP address"))
    for match in EMAIL_PATTERN.finditer(data):
        domain = match.group().split(b"@", 1)[1].lower()
        # RFC 2606 reserves these TLDs for examples and isolated test identities.
        if not domain.endswith((b".invalid", b".example", b".test")):
            found.append((data.count(b"\n", 0, match.start()) + 1, "personal email address"))
    return sorted(set(found))


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def check(staged=False, history=False, anonymous=False):
    failures = 0
    names = git("ls-files", "-z").split(b"\0")
    for raw_name in names:
        if not raw_name:
            continue
        name = raw_name.decode("utf-8")
        data = git("show", ":" + name) if staged else Path(name).read_bytes()
        for line, category in findings(data):
            print(f"{name}:{line}: prohibited {category}; value withheld")
            failures += 1
    if history:
        # Scan all historical blobs, including deleted files and abandoned local refs.
        objects = git("rev-list", "--objects", "--all").splitlines()
        for entry in objects:
            object_id = entry.split(b" ", 1)[0].decode()
            if git("cat-file", "-t", object_id).strip() != b"blob":
                continue
            if findings(git("cat-file", "blob", object_id)):
                print(f"history blob {object_id[:12]}: prohibited content; value withheld")
                failures += 1
    if anonymous:
        metadata = git("log", "--all", "--format=%an%x00%ae%x00%cn%x00%ce").splitlines()
        expected = [b"FRP Tunnel contributors", b"contributors@example.invalid"] * 2
        if any(line.split(b"\0") != expected for line in metadata):
            print("history: author or committer metadata is not the generic project identity")
            failures += 1
    if not failures:
        print("Privacy checks passed for tracked files" + (" and history." if history else "."))
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Read the index rather than working files")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--anonymous-history", action="store_true")
    args = parser.parse_args()
    try:
        return bool(check(args.staged, args.history, args.anonymous_history))
    except (OSError, subprocess.SubprocessError):
        print("Privacy check could not read the repository; refusing to pass.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
