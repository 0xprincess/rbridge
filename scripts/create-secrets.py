#!/usr/bin/env python3
"""Create private deployment inputs outside a Git checkout (Linux only)."""

import argparse
import importlib.util
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile

import yaml

spec = importlib.util.spec_from_file_location("tunnel_cli", Path(__file__).with_name("tunnel.py"))
tunnel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tunnel)


def require_permission_enforcement(path):
    """Refuse filesystems that ignore chmod/ownership, such as Windows drives in WSL."""
    existing = next(ancestor for ancestor in (path, *path.parents) if ancestor.is_dir())
    try:
        descriptor, probe = tempfile.mkstemp(prefix=".rbridge-probe-", dir=existing)
    except OSError:
        return  # Creating the private directory reports its own error.
    try:
        os.close(descriptor)
        os.chmod(probe, 0o600)
        info = os.stat(probe)
    finally:
        os.unlink(probe)
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
        raise ValueError("Use a Linux filesystem that enforces file permissions "
                         "(not a Windows drive such as WSL /mnt/c or a FAT/NTFS/SMB mount).")


def validate_destination(path):
    resolved = Path(path).expanduser().resolve()
    if os.name != "posix":
        raise ValueError("Generate secrets on Linux (including WSL), on a Linux filesystem.")
    if resolved.exists():
        raise ValueError("Output directory already exists; refusing to overwrite keys.")
    for ancestor in (resolved, *resolved.parents):
        if (ancestor / ".git").exists():
            raise ValueError("Private deployment inputs must be outside every Git checkout.")
    require_permission_enforcement(resolved)
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
    managed = not args.no_managed_ssh
    if managed and (not args.client_user or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", args.client_user) or args.client_user == "root"):
        raise ValueError("Managed SSH requires --client-user naming an existing non-root Linux account.")
    if not 1 <= args.relay_port <= 65535:
        raise ValueError("Invalid relay SSH port.")
    known_hosts = pinned_hosts(Path(args.known_hosts).read_text(), host, args.relay_port)
    configuration = tunnel.load_config([args.services_file]) if args.services_file else {}
    if not args.services_file:
        if not managed:
            raise ValueError("--no-managed-ssh requires --services-file with explicit frp_services.")
        configuration["frp_services"] = [{
            "name": "ssh", "protocol": "tcp", "backend": "127.0.0.1:2222",
            "relay_port": 12222, "public_port": 2222,
        }]
    configuration.update(frp_server_address=host, frp_relay_ssh_port=args.relay_port,
                         frp_managed_ssh=managed)
    if managed:
        configuration["frp_login_user"] = args.client_user
    tunnel.validate_config(configuration)
    keygen = shutil.which("ssh-keygen")
    if not keygen:
        raise ValueError("Install OpenSSH client tools first.")
    os.umask(0o077)
    destination.mkdir(mode=0o700, parents=True)
    destination.chmod(0o700)
    for name in (("relay", "login") if managed else ("relay",)):
        subprocess.run(
            [keygen, "-q", "-t", "ed25519", "-N", "", "-C", f"rbridge {name}",
             "-f", str(destination / f"{name}_id_ed25519")],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    # These files remain private, but ordinary block YAML makes services easy to edit.
    values = {
        "frp_token": secrets.token_hex(32),
        "frp_admin_password": secrets.token_hex(32),
        "frp_relay_public_key": (destination / "relay_id_ed25519.pub").read_text().strip(),
        "frp_relay_private_key": (destination / "relay_id_ed25519").read_text(),
        "frp_relay_known_hosts": known_hosts,
    }
    if managed:
        values["frp_login_public_key"] = (destination / "login_id_ed25519.pub").read_text().strip()
    (destination / "secrets.yml").write_text(yaml.safe_dump(values, sort_keys=False))
    (destination / "services.yml").write_text(yaml.safe_dump(configuration, sort_keys=False))
    inventory = {
        "all": {
            "vars": {"frp_server_address": host, "frp_relay_ssh_port": args.relay_port},
            "children": {
                "frp_servers": {"hosts": {"relay": {"ansible_host": host,
                                                    "ansible_user": args.admin_user}}},
                "frp_clients": {"hosts": {"origin": {"ansible_connection": "local",
                                                     "ansible_host": "localhost"}}},
            },
        }
    }
    (destination / "inventory.yml").write_text(yaml.safe_dump(inventory, sort_keys=False))
    (destination / "known_hosts").write_text(known_hosts)
    for child in destination.iterdir():
        child.chmod(0o600)
    print("Private deployment inputs created. No key or token values were printed.")
    print("Edit services.yml, then deploy using -i inventory.yml -e @services.yml -e @secrets.yml.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Requires Python 3.11+, PyYAML and OpenSSH. Run from the repository: "
        "python3 scripts/create-secrets.py --output /private/tunnel --server-host relay.<relay-ip>.rbr.sh "
        "--known-hosts /private/known_hosts --client-user developer. "
        "For non-SSH publishing add --no-managed-ssh --services-file /private/services.yml. "
        "The service file is a flat YAML mapping containing frp_services, and frp_oidc when needed. "
        "Validate edits with python3 scripts/tunnel.py validate --config /private/tunnel/services.yml. "
        "Never commit generated files; use all three generated YAML files when deploying."))
    parser.add_argument("--output", required=True, help="New private directory outside every Git checkout")
    parser.add_argument("--server-host", required=True, help="Relay IPv4 address or DNS name")
    parser.add_argument("--client-user", help="Existing non-root client login account; required with managed SSH")
    parser.add_argument("--no-managed-ssh", action="store_true", help="Do not generate login keys or manage a dedicated SSH backend")
    parser.add_argument("--services-file", help="Private flat YAML service variables to validate and copy; default is the SSH preset")
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
