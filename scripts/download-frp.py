#!/usr/bin/env python3
"""Download only the checksum-pinned upstream binaries for development tests."""

import argparse
import hashlib
import io
from pathlib import Path
import platform
import tarfile
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]


def unpack(data, destination, version, arch, checksum):
    if hashlib.sha256(data).hexdigest() != checksum:
        raise ValueError("Upstream archive SHA256 does not match the pinned checksum.")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for name in ("frps", "frpc"):
            member = archive.getmember(f"frp_{version}_linux_{arch}/{name}")
            if not member.isfile() or member.size > 64 * 1024 * 1024:
                raise ValueError("Unexpected upstream archive member.")
            with archive.extractfile(member) as source:
                (destination / name).write_bytes(source.read())
            (destination / name).chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    defaults = yaml.safe_load((ROOT / "ansible/roles/frp_common/defaults/main.yml").read_text())
    version = defaults["frp_version"]
    arch = defaults["frp_architectures"][platform.machine()]
    url = f"https://github.com/fatedier/frp/releases/download/v{version}/frp_{version}_linux_{arch}.tar.gz"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=60) as response:
        data = response.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise ValueError("Unexpected archive size.")
    unpack(data, args.destination, version, arch, defaults["frp_checksums"][version][arch])
    print("Upstream FRP binaries downloaded and checksums verified.")


if __name__ == "__main__":
    main()
