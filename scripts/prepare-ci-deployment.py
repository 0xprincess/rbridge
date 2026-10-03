#!/usr/bin/env python3
"""Materialize environment secrets into a private ephemeral Actions directory."""

import importlib.util
import os
from pathlib import Path
import sys

import yaml

spec = importlib.util.spec_from_file_location("tunnel_cli", Path(__file__).with_name("tunnel.py"))
tunnel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tunnel)


def mask(value):
    escaped = str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if escaped:
        print("::add-mask::" + escaped, flush=True)


def mask_tree(value):
    if isinstance(value, dict):
        for child in value.values():
            mask_tree(child)
    elif isinstance(value, list):
        for child in value:
            mask_tree(child)
    elif value is not None:
        mask(value)
        # Mask key fragments and identifiers even if an error only prints one line.
        for line in str(value).splitlines():
            mask(line)


def main():
    os.umask(0o077)
    required = {
        "DEPLOY_PRIVATE_KEY": "admin.key",
        "DEPLOY_KNOWN_HOSTS": "known_hosts",
        "DEPLOY_INVENTORY": "inventory.yml",
        "TUNNEL_SECRETS": "secrets.yml",
        "TUNNEL_SERVICES": "services.yml",
    }
    if any(not os.environ.get(name, "").strip() for name in required):
        raise ValueError("Missing deployment environment secrets.")
    for name in required:
        mask(os.environ[name])
    parsed = {}
    for name in ("DEPLOY_INVENTORY", "TUNNEL_SERVICES", "TUNNEL_SECRETS"):
        value = yaml.safe_load(os.environ[name])
        if not isinstance(value, dict):
            raise ValueError("Deployment YAML inputs must be mappings.")
        mask_tree(value)
        parsed[name] = value
    tunnel.validate_config({**parsed["TUNNEL_SERVICES"], **parsed["TUNNEL_SECRETS"]})
    destination = Path(os.environ["RUNNER_TEMP"]) / "frp-tunnel"
    destination.mkdir(mode=0o700, exist_ok=False)
    for name, filename in required.items():
        path = destination / filename
        path.write_text(os.environ[name].strip() + "\n")
        path.chmod(0o600)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # YAML parser exceptions can contain the original secret text.
        print("Deployment inputs are missing or invalid; details withheld.", file=sys.stderr)
        sys.exit(1)
