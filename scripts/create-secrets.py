#!/usr/bin/env python3
"""Create private deployment inputs outside a Git checkout (Linux/WSL only)."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys


def validate_destination(path):
    resolved = Path(path).expanduser().resolve()
    if os.name != "posix":
        raise ValueError("Generate secrets inside Linux or WSL, on a Linux filesystem.")
    if str(resolved).startswith("/mnt/"):
        raise ValueError("Use the WSL Linux filesystem, not a shared /mnt directory.")
    if resolved.exists():
        raise ValueError("Output directory already exists; refusing to overwrite keys.")
    for ancestor in (resolved, *resolved.parents):
        if (ancestor / ".git").exists():
            raise ValueError("Private deployment inputs must be outside every Git checkout.")
    return resolved


def validate_host(host):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
        raise ValueError("Use an IPv4 address or DNS name without SSH options or whitespace.")
    return host


def pinned_hosts(text, host, port):
    expected = host if port == 22 else f"[{host}]:{port}"
    result = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3 and expected in fields[0].split(","):
            if fields[1] != "ssh-ed25519" or not re.fullmatch(r"[A-Za-z0-9+/=]+", fields[2]):
                raise ValueError("Supply verified, unhashed Ed25519 relay host keys.")
            result.append(f"{expected} {fields[1]} {fields[2]}")
    if not result:
        raise ValueError("No verified Ed25519 host key matches the selected relay endpoint.")
    return "\n".join(result) + "\n"


def generate(args):
    destination = validate_destination(args.output)
    host = validate_host(args.server_host)
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", args.client_user) or args.client_user == "root":
        raise ValueError("Client login must be an existing non-root Linux account.")
    if not 1 <= args.relay_port <= 65535:
        raise ValueError("Invalid relay SSH port.")
    known_hosts = pinned_hosts(Path(args.known_hosts).read_text(), host, args.relay_port)
    keygen = shutil.which("ssh-keygen")
    if not keygen:
        raise ValueError("Install OpenSSH client tools first.")
    os.umask(0o077)
    destination.mkdir(mode=0o700, parents=True)
    destination.chmod(0o700)
    for name in ("relay", "login"):
        subprocess.run(
            [keygen, "-q", "-t", "ed25519", "-N", "", "-C", f"frp-tunnel {name}",
             "-f", str(destination / f"{name}_id_ed25519")],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    # JSON strings are valid YAML scalars and cannot inject additional YAML keys.
    values = {
        "frp_token": secrets.token_hex(32),
        "frp_relay_public_key": (destination / "relay_id_ed25519.pub").read_text().strip(),
        "frp_relay_private_key": (destination / "relay_id_ed25519").read_text(),
        "frp_login_public_key": (destination / "login_id_ed25519.pub").read_text().strip(),
        "frp_relay_known_hosts": known_hosts,
    }
    (destination / "secrets.yml").write_text(
        "---\n" + "".join(f"{name}: {json.dumps(value)}\n" for name, value in values.items())
    )
    inventory = {
        "all": {
            "vars": {"frp_server_address": host, "frp_relay_ssh_port": args.relay_port,
                     "frp_login_user": args.client_user},
            "children": {
                "frp_servers": {"hosts": {"relay": {"ansible_host": host,
                                                    "ansible_user": args.admin_user}}},
                "frp_clients": {"hosts": {"wsl": {"ansible_connection": "local",
                                                  "ansible_host": "localhost"}}},
            },
        }
    }
    (destination / "inventory.yml").write_text(json.dumps(inventory, indent=2) + "\n")
    (destination / "known_hosts").write_text(known_hosts)
    for child in destination.iterdir():
        child.chmod(0o600)
    print("Private deployment inputs created. No key or token values were printed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--server-host", required=True)
    parser.add_argument("--client-user", required=True)
    parser.add_argument("--known-hosts", required=True,
                        help="Previously verified, unhashed Ed25519 relay host keys")
    parser.add_argument("--relay-port", type=int, default=22)
    parser.add_argument("--admin-user", default="root")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", args.admin_user):
        parser.error("Invalid administrative SSH account.")
    try:
        generate(args)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Do not echo exception filenames or subprocess arguments containing identities.
        if isinstance(error, ValueError):
            parser.error(str(error))
        parser.error("Secret generation failed; inspect private inputs and local OpenSSH tools.")


if __name__ == "__main__":
    main()
