#!/usr/bin/env python3
"""Test real FRP/SSH processes on loopback; optionally apply roles on a disposable VM."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import sys
import tempfile
import time

from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=120, **kwargs)
    if result.returncode:
        tasks = [line for line in result.stdout.decode(errors="replace").splitlines()
                 if line.startswith("TASK [")]
        detail = tasks[-1] if tasks else "integration subprocess"
        raise RuntimeError(f"Failed at {detail}; detailed output withheld to protect test keys.")
    return result


def ports(count=9):
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def wait_port(port, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("Integration listener did not become ready.")


def render(role, name, values):
    environment = Environment(loader=FileSystemLoader(ROOT / f"ansible/roles/{role}/templates"),
                              undefined=StrictUndefined, keep_trailing_newline=True)
    environment.filters["to_json"] = json.dumps
    return environment.get_template(name).render(values)


def check_tunnel(folder, public_port, user):
    ssh = ["ssh", "-F", "/dev/null", "-T", "-i", str(folder / "login.key"),
           "-p", str(public_port), "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes",
           "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o",
           f"UserKnownHostsFile={folder / 'login_known_hosts'}", "-o", "ProxyCommand=none",
           "-o", "ProxyJump=none", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=2",
           "-o", "ServerAliveCountMax=2", f"{user}@127.0.0.1"]
    payload = bytes(1024 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    deadline = time.monotonic() + 35
    while True:
        probe = subprocess.run(ssh + ["printf integration-ok"], capture_output=True, timeout=15)
        if probe.returncode == 0 and probe.stdout == b"integration-ok":
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Key-only tunnel login did not recover.")
        time.sleep(0.5)
    download = run(ssh + ["head -c 1048576 /dev/zero"])
    assert hashlib.sha256(download.stdout).hexdigest() == digest
    upload = run(ssh + ["sha256sum"], input=payload)
    assert upload.stdout.decode().split()[0] == digest
    rejected = subprocess.run(ssh[:-1] + ["-o", "PubkeyAuthentication=no", "-o",
                              "PreferredAuthentications=password,keyboard-interactive", ssh[-1],
                              "true"], capture_output=True, timeout=15)
    assert rejected.returncode != 0 and b"Permission denied (publickey)" in rejected.stderr
    print("Verified key login, 1 MiB in both directions, and password rejection.")


def prepare_keys(folder):
    for name in ("host", "relay", "login"):
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "rbridge test",
             "-f", str(folder / f"{name}.key")])


def process_test(args, folder, user, values):
    # Only child processes and files in this temporary directory are managed.
    if not Path("/run/sshd").is_dir():
        raise RuntimeError("OpenSSH runtime directory /run/sshd must already exist.")
    prepare_keys(folder)
    (server_port, public_port, forward_port, backend_port, relay_port,
     second_relay_port, second_public_port, client_admin_port, server_admin_port) = ports()
    spec = importlib.util.spec_from_file_location("frp_config", ROOT / "ansible/filter_plugins/frp_config.py")
    model = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(model)
    services = [{"name": name, "protocol": "tcp", "backend": f"127.0.0.1:{backend_port}",
                 "relay_port": relay, "public_port": public}
                for name, relay, public in (("ssh", relay_port, public_port),
                                           ("second", second_relay_port, second_public_port))]
    values.update(frp_server_port=server_port, frp_services_normalized=model.normalize_services(services),
                  frp_forward_port=forward_port, frp_backend_ssh_port=backend_port,
                  frp_login_user=user, frp_client_admin_port=client_admin_port,
                  frp_server_admin_port=server_admin_port, frp_admin_user="frp-doctor",
                  frp_admin_password=os.urandom(32).hex())
    host_key = (folder / "host.key.pub").read_text().split()
    for filename, port in (("relay_known_hosts", backend_port), ("login_known_hosts", public_port)):
        (folder / filename).write_text(f"[127.0.0.1]:{port} {host_key[0]} {host_key[1]}\n")
    with (folder / "login_known_hosts").open("a") as known:
        known.write(f"[127.0.0.1]:{second_public_port} {host_key[0]} {host_key[1]}\n")
    login_pub = (folder / "login.key.pub").read_text()
    relay_pub = (folder / "relay.key.pub").read_text()
    (folder / "login_authorized_keys").write_text(
        f'restrict,port-forwarding,permitopen="127.0.0.1:{server_port}",command="/bin/false" '
        + relay_pub + login_pub
    )
    (folder / "login_authorized_keys").chmod(0o644)
    (folder / "token").write_text(os.urandom(32).hex() + "\n")
    for role, name in (("frp_server", "frps.toml"), ("frp_client", "frpc.toml"),
                       ("frp_client", "sshd_config")):
        content = render(role, name + ".j2", values).replace("/etc/rbridge/", str(folder) + "/")
        content = content.replace("/etc/ssh/ssh_host_ed25519_key", str(folder / "host.key"))
        content = content.replace("/run/rbridge-sshd/sshd.pid", str(folder / "sshd.pid"))
        (folder / name).write_text(content)
    run([args.frps, "verify", "-c", str(folder / "frps.toml")])
    run([args.frpc, "verify", "-c", str(folder / "frpc.toml")])
    run(["/usr/sbin/sshd", "-t", "-f", str(folder / "sshd_config")])
    children = []
    log = (folder / "process.log").open("wb")

    def start(command):
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        children.append(child)
        return child

    transport = ["ssh", "-N", "-T", "-F", "/dev/null", "-i", str(folder / "relay.key"),
                 "-p", str(backend_port), "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes",
                 "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o",
                 f"UserKnownHostsFile={folder / 'relay_known_hosts'}", "-o", "ProxyCommand=none",
                 "-o", "ProxyJump=none", "-o", "ExitOnForwardFailure=yes", "-L",
                 f"127.0.0.1:{forward_port}:127.0.0.1:{server_port}", f"{user}@127.0.0.1"]
    try:
        start(["/usr/sbin/sshd", "-D", "-e", "-f", str(folder / "sshd_config")])
        start([args.frps, "-c", str(folder / "frps.toml")])
        wait_port(backend_port)
        wait_port(server_port)
        relay = start(transport)
        wait_port(forward_port)
        start([args.frpc, "-c", str(folder / "frpc.toml")])
        proxy = next((path for path in ("/usr/lib/systemd/systemd-socket-proxyd",
                                       "/lib/systemd/systemd-socket-proxyd") if Path(path).is_file()), None)
        if not proxy:
            raise RuntimeError("systemd-socket-proxyd is required for the public TCP ingress.")
        for public, private in ((public_port, relay_port), (second_public_port, second_relay_port)):
            wait_port(private)
            start(["systemd-socket-activate", "-l", f"127.0.0.1:{public}", proxy, f"127.0.0.1:{private}"])
        wait_port(public_port)
        check_tunnel(folder, public_port, user)
        check_tunnel(folder, second_public_port, user)
        relay.terminate()
        relay.wait(timeout=5)
        start(transport)
        wait_port(forward_port)
        check_tunnel(folder, public_port, user)
        check_tunnel(folder, second_public_port, user)
        print("Transport reconnection verified with real FRP and OpenSSH processes.")
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        log.close()




def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frps")
    parser.add_argument("--frpc")
    parser.add_argument("--user", default=os.environ.get("SUDO_USER"))
    parser.add_argument("--apply-to-disposable-host", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.user or args.user == "root":
        parser.error("Run with sudo and specify an existing non-root --user.")
    pwd.getpwnam(args.user)
    if not args.apply_to_disposable_host and (not args.frps or not args.frpc):
        parser.error("Process tests require --frps and --frpc binary paths.")
    os.umask(0o077)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(name, None)
    if args.apply_to_disposable_host:
        result = subprocess.run([sys.executable, str(ROOT / "scripts/field-test.py"),
                                 "--local", "--user", args.user], check=False)
        raise SystemExit(result.returncode)
    # OpenSSH StrictModes rejects authorized keys below world-writable /tmp.
    with tempfile.TemporaryDirectory(prefix="rbridge-test-", dir="/run") as temporary:
        folder = Path(temporary)
        folder.chmod(0o755)  # sshd reads the public authorized key as the login user.
        values = {"frp_service_user": "rbridge", "frp_relay_user": "frp-relay"}
        process_test(args, folder, args.user, values)


if __name__ == "__main__":
    main()
