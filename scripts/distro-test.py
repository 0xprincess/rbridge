#!/usr/bin/env python3
"""Deploy the real roles into a disposable systemd container of one distribution.

Docker runs the image's systemd as PID 1 in a privileged container that acts as
both relay and origin, reached over SSH exactly like a remote host. The test
deploys twice (the second run must change nothing), then checks native SSH
(1 MiB round trip and SFTP) through the public TCP publication and HTTPS through
Caddy's internal CA. Only the container and a temporary directory are created;
both are removed afterward unless --keep is given.

  python scripts/distro-test.py --image fedora:latest

Requires Docker, OpenSSH, curl and requirements-dev.txt (ansible-playbook on PATH).
SELinux and AppArmor are inactive inside containers; exercise them on real hosts.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CA_PATH = "/var/lib/rbridge-web/data/caddy/pki/authorities/local/root.crt"
# Minimal bootable systemd, sshd and Python for Ansible, by package manager.
BOOTSTRAP = """
set -e
if command -v apt-get >/dev/null; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq systemd systemd-sysv dbus openssh-server python3 >/dev/null
elif command -v dnf >/dev/null; then dnf -y -q install systemd openssh-server python3 >/dev/null
elif command -v zypper >/dev/null; then zypper -n -q install systemd openssh-server python3 >/dev/null
elif command -v pacman >/dev/null; then pacman -Syu --noconfirm --noprogressbar systemd openssh python >/dev/null
else echo 'unsupported package manager' >&2; exit 1
fi
"""
UNITS = ("rbridge-server", "rbridge-web", "rbridge-publish-ssh.socket", "rbridge-sshd",
         "rbridge-transport", "rbridge-client")


class Failure(RuntimeError):
    pass


def run(argv, *, check=True, data=None, timeout=900, env=None):
    result = subprocess.run([str(arg) for arg in argv], input=data, capture_output=True,
                            timeout=timeout, env=env)
    if check and result.returncode:
        raise Failure(f"{Path(str(argv[0])).name} failed ({result.returncode}):\n"
                      + (result.stdout + result.stderr).decode(errors="replace")[-4000:])
    return result


def eventually(action, label, timeout=90):
    deadline = time.monotonic() + timeout
    while True:
        if action():
            return
        if time.monotonic() >= deadline:
            raise Failure(label)
        time.sleep(2)


class Container:
    def __init__(self, image, name):
        self.image, self.name = image, name

    def exec(self, script, *, data=None, check=True):
        return run(["docker", "exec", "-i", self.name, "sh", "-c", script], data=data, check=check)

    def start(self):
        builder = self.name + "-build"
        run(["docker", "rm", "-f", builder, self.name], check=False)
        run(["docker", "run", "--name", builder, self.image, "sh", "-c", BOOTSTRAP], timeout=1800)
        run(["docker", "commit", builder, self.name + ":systemd"])
        run(["docker", "rm", builder])
        run(["docker", "run", "-d", "--name", self.name, "--privileged", "--cgroupns=private",
             "--tmpfs", "/run", "--tmpfs", "/tmp", "-e", "container=docker",
             self.name + ":systemd", "/usr/lib/systemd/systemd"])
        eventually(lambda: self.exec("systemctl is-system-running", check=False).stdout.strip()
                   in (b"running", b"degraded"), "systemd did not finish booting")
        return run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                    self.name]).stdout.decode().strip()

    def remove(self):
        run(["docker", "rm", "-f", self.name], check=False)
        run(["docker", "rmi", self.name + ":systemd"], check=False)


def recap(output):
    """Per-host changed counts from an Ansible PLAY RECAP."""
    counts = {}
    for line in output.decode(errors="replace").splitlines():
        fields = line.split()
        if len(fields) > 2 and fields[1] == ":" and any(item.startswith("changed=") for item in fields):
            counts[fields[0]] = int(next(item for item in fields if item.startswith("changed="))[8:])
    return counts


def test(container, folder):
    address = container.start()
    for name in ("admin", "relay", "login"):
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "distro-test", "-f", folder / name])
    container.exec("ssh-keygen -A >/dev/null && install -d -m 700 /root/.ssh && "
                   "cat > /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys && "
                   "(systemctl enable --now ssh.service 2>/dev/null || systemctl enable --now sshd.service) && "
                   "useradd -m -s /bin/sh tester && "
                   "printf '[Service]\\nExecStart=/usr/bin/python3 -m http.server --bind 127.0.0.1 18080 --directory /etc\\n' "
                   "> /etc/systemd/system/distro-origin.service && systemctl daemon-reload && "
                   "systemctl start distro-origin", data=(folder / "admin.pub").read_bytes())
    key = container.exec("cat /etc/ssh/ssh_host_ed25519_key.pub").stdout.decode().split()
    known = f"{address} {key[0]} {key[1]}\n"
    (folder / "known_hosts").write_text(known)
    (folder / "login_known_hosts").write_text(f"[{address}]:2222 {key[0]} {key[1]}\n")
    host = {"ansible_host": address, "ansible_user": "root"}
    (folder / "inventory.json").write_text(json.dumps({"all": {"children": {
        "frp_servers": {"hosts": {"relay": host}}, "frp_clients": {"hosts": {"client": host}}}}}))
    (folder / "values.json").write_text(json.dumps({
        "frp_server_address": address, "frp_managed_ssh": True, "frp_login_user": "tester",
        "frp_token": os.urandom(32).hex(), "frp_admin_password": os.urandom(32).hex(),
        "frp_relay_public_key": (folder / "relay.pub").read_text().strip(),
        "frp_relay_private_key": (folder / "relay").read_text(),
        "frp_login_public_key": (folder / "login.pub").read_text().strip(),
        "frp_relay_known_hosts": known, "frp_backend_ssh_port": 2224, "frp_web_tls": "internal",
        "frp_services": [
            {"name": "ssh", "protocol": "tcp", "backend": "127.0.0.1:2224", "relay_port": 12222, "public_port": 2222},
            {"name": "app", "protocol": "http", "backend": "http://127.0.0.1:18080", "relay_port": 12300,
             "hostname": "app.distro.test", "access": {"mode": "public"}}],
    }))
    environment = dict(os.environ, ANSIBLE_CONFIG=str(ROOT / "ansible/ansible.cfg"), ANSIBLE_NOCOLOR="1")
    playbook = ["ansible-playbook", "-i", folder / "inventory.json", "-e", "@" + str(folder / "values.json"),
                ROOT / "ansible/deploy.yml", "--private-key", folder / "admin", "--ssh-common-args",
                f"-o UserKnownHostsFile={folder / 'known_hosts'} -o IdentitiesOnly=yes"]
    run(playbook, env=environment, timeout=1800)
    second = recap(run(playbook, env=environment, timeout=1800).stdout)
    if set(second) != {"relay", "client"} or any(second.values()):
        raise Failure(f"Second deployment was not idempotent: {second}")
    active = container.exec("systemctl is-active " + " ".join(UNITS), check=False).stdout.decode().split()
    if active != ["active"] * len(UNITS):
        raise Failure(f"Units not active: {dict(zip(UNITS, active))}")
    ssh = ["ssh", "-F", "/dev/null", "-T", "-i", folder / "login", "-p", "2222", "-o", "IdentityAgent=none",
           "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", f"UserKnownHostsFile={folder / 'login_known_hosts'}", "-o", "ConnectTimeout=5", f"tester@{address}"]
    eventually(lambda: run(ssh + ["printf distro-ok"], check=False, timeout=20).stdout == b"distro-ok",
               "SSH through the public TCP publication failed")
    payload = os.urandom(1024 * 1024)
    if run(ssh + ["cat"], data=payload, timeout=60).stdout != payload:
        raise Failure("1 MiB SSH round trip changed the payload")
    run(["sftp", "-F", "/dev/null", "-i", folder / "login", "-P", "2222", "-o", "IdentityAgent=none",
         "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", f"UserKnownHostsFile={folder / 'login_known_hosts'}",
         "-b", "-", f"tester@{address}"], data=b"pwd\n", timeout=60)
    (folder / "caddy.crt").write_bytes(container.exec("cat " + CA_PATH).stdout)
    curl = ["curl", "--silent", "--noproxy", "*", "--max-time", "10", "--cacert", folder / "caddy.crt",
            "--resolve", f"app.distro.test:443:{address}", "--output", "/dev/null", "--write-out", "%{http_code}",
            "https://app.distro.test/hostname"]
    eventually(lambda: run(curl, check=False).stdout == b"200", "HTTPS publication failed")
    release = container.exec(". /etc/os-release && printf %s \"$PRETTY_NAME\"").stdout.decode()
    systemd = container.exec("systemctl --version").stdout.decode().splitlines()[0]
    print(f"PASS {release} ({systemd}): idempotent deployment, native SSH/SFTP, HTTPS", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", required=True, help="Distribution container image, e.g. debian:stable")
    parser.add_argument("--keep", action="store_true", help="Keep the container and private inputs for inspection")
    args = parser.parse_args()
    for tool in ("docker", "ssh", "ssh-keygen", "sftp", "curl", "ansible-playbook"):
        if not shutil.which(tool):
            parser.error(f"{tool} is required")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(name, None)
    name = "rbridge-distro-" + "".join(c if c.isalnum() else "-" for c in args.image).strip("-")
    container = Container(args.image, name)
    folder = Path(tempfile.mkdtemp(prefix="rbridge-distro-"))
    try:
        test(container, folder)
    except Failure as error:
        print(f"FAIL {args.image}: {error}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        if args.keep:
            print(f"Kept container {name} and private inputs in {folder}", file=sys.stderr)
        else:
            container.remove()
            shutil.rmtree(folder)


if __name__ == "__main__":
    main()
