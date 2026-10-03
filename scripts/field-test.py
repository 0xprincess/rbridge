#!/usr/bin/env python3
"""Destructive regression suite for disposable Ubuntu/systemd hosts, never production.

Controller prerequisites: requirements-dev.txt, scripts/fixtures/requirements.txt,
Playwright Chromium (`python -m playwright install --with-deps chromium`), curl,
OpenSSH, OpenSSL and (internal-CA mode only) libnss3-tools (certutil). Inventory must contain frp_servers
and frp_clients. Admin SSH aliases must have pinned host keys and passwordless sudo.

Two hosts:
  python scripts/field-test.py --inventory /private/inventory.yml \
    --ssh-config /private/ssh_config --relay-ssh relay --client-ssh origin \
    --address RELAY_IP --domain test.example.net --user ubuntu --require-dns

Public ACME: add --public-tls (requires real public DNS and open TCP 80/443).
Default: Caddy's internal CA, explicitly trusted by curl/browser and both hosts.
DNS names: public, basic, app, oidc, secure, idp under --domain. SSH TCP 2222 and
TCP echo 2233 must be publicly reachable. All management credentials remain in
private inventory/SSH files; test credentials are ephemeral and never printed.
Leaves the final TCP-only deployment installed; always removes fixture services,
fixture Docker container, fixture CA trust, and fixture hosts-file entries.

--keep-fixtures restores the full deployment after removal checks and preserves
private inputs for visual inspection on success. Its printed directory contains
credentials.json and deployment.json. Destroy both disposable VPSs afterward
and remove that directory; never use this option against shared hosts.
"""
import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import bcrypt
import yaml

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("public", "basic", "app", "oidc", "secure", "idp")
CA_PATH = "/var/lib/frp-tunnel-web/data/caddy/pki/authorities/local/root.crt"
ARTIFACTS = None


class Failure(RuntimeError):
    pass


def require(condition, label):
    if not condition:
        raise Failure(label)


def command(argv, *, data=None, env=None, timeout=900, allow_failure=False):
    # OpenSSH can change inherited stdin flags. Isolate noninteractive commands
    # so a prior SSH probe cannot leave Ansible with non-blocking standard input.
    result = subprocess.run([str(arg) for arg in argv], input=data,
                            stdin=subprocess.DEVNULL if data is None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, timeout=timeout)
    if result.returncode and ARTIFACTS:
        (ARTIFACTS / f"subprocess-{time.time_ns()}.log").write_bytes(result.stdout + b"\nSTDERR:\n" + result.stderr)
    if result.returncode and not allow_failure:
        raise Failure("Subprocess failed; private output withheld (" + Path(str(argv[0])).name + ").")
    return result


def eventually(action, label, timeout=90):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return action()
        except (Failure, OSError, subprocess.TimeoutExpired):
            if time.monotonic() >= deadline:
                raise Failure(label) from None
            time.sleep(1)


class Suite:
    def __init__(self, args, folder):
        self.args, self.folder = args, folder
        self.address = args.address
        self.domain = args.domain
        self.environment = dict(os.environ, ANSIBLE_CONFIG=str(ROOT / "ansible/ansible.cfg"),
                                ANSIBLE_NOCOLOR="1")
        self.values = {}
        self.trust = folder / "trust.pem"
        self.browser_home = folder / "browser-home"
        self.prepared = set()
        self.report = []

    def mark(self, name):
        self.report.append(name)
        print("PASS " + name, flush=True)

    def remote(self, host, script, *, data=None, allow_failure=False):
        if self.args.local:
            argv = ["sh", "-c", script]
        else:
            alias = self.args.relay_ssh if host == "relay" else self.args.client_ssh
            argv = ["ssh", "-F", str(self.args.ssh_config), "-o", "BatchMode=yes", "-o",
                    "StrictHostKeyChecking=yes", alias, "sudo -n sh -c " + shlex.quote(script)]
        return command(argv, data=data, allow_failure=allow_failure)

    def put(self, host, path, content, mode="600"):
        data = content.encode() if isinstance(content, str) else content
        self.remote(host, "umask 077; cat > " + shlex.quote(path) + "; chmod " + mode + " " +
                    shlex.quote(path), data=data)

    def deploy(self, values=None, fail=False, *, limit=None):
        path = self.folder / "deployment.json"
        path.write_text(json.dumps(self.values if values is None else values))
        argv = ["ansible-playbook", "-i", str(self.args.inventory), "-e", "@" + str(path),
                str(ROOT / "ansible/deploy.yml")]
        if limit:
            argv += ["--limit", limit]
        if self.args.ssh_config:
            argv += ["--ssh-common-args", "-F " + shlex.quote(str(self.args.ssh_config)) +
                     " -o StrictHostKeyChecking=yes -o BatchMode=yes"]
        return command(argv, env=self.environment, allow_failure=fail)

    def make_keys(self):
        for name in ("login", "relay"):
            command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "field-test",
                     "-f", self.folder / (name + ".key")])
        host = self.remote("relay", "cat /etc/ssh/ssh_host_ed25519_key.pub").stdout.decode().split()
        prefix = self.address if self.args.relay_port == 22 else f"[{self.address}]:{self.args.relay_port}"
        relay_hosts = f"{prefix} {host[0]} {host[1]}\n"
        (self.folder / "relay_known_hosts").write_text(relay_hosts)
        host = self.remote("client", "cat /etc/ssh/ssh_host_ed25519_key.pub").stdout.decode().split()
        (self.folder / "login_known_hosts").write_text(f"[{self.address}]:2222 {host[0]} {host[1]}\n")
        password = base64.urlsafe_b64encode(os.urandom(24)).decode()
        self.password = password
        self.login_password = base64.urlsafe_b64encode(os.urandom(24)).decode()
        self.denied_password = base64.urlsafe_b64encode(os.urandom(24)).decode()
        self.values = {
            "frp_server_address": self.address, "frp_relay_ssh_port": self.args.relay_port,
            "frp_login_user": self.args.user, "frp_managed_ssh": True,
            "frp_token": os.urandom(32).hex(), "frp_admin_password": os.urandom(32).hex(),
            "frp_relay_public_key": (self.folder / "relay.key.pub").read_text().strip(),
            "frp_relay_private_key": (self.folder / "relay.key").read_text(),
            "frp_login_public_key": (self.folder / "login.key.pub").read_text().strip(),
            "frp_relay_known_hosts": relay_hosts,
            "frp_backend_ssh_port": 2224 if self.args.local else 2222, "frp_server_port": 17000,
            "frp_forward_port": 17001, "frp_client_admin_port": 17400,
            "frp_server_admin_port": 17401,
            "frp_oidc": {"issuer_url": f"https://idp.{self.domain}", "client_id": "field-app",
                         "client_secret": os.urandom(32).hex(),
                         "cookie_secret": base64.urlsafe_b64encode(os.urandom(32)).decode()},
        }
        if not self.args.public_tls:
            self.values["frp_web_tls"] = "internal"
        self.services = [
            {"name": "ssh", "protocol": "tcp",
             "backend": "127.0.0.1:" + str(self.values["frp_backend_ssh_port"]),
             "relay_port": 12222, "public_port": 2222},
            {"name": "echo", "protocol": "tcp", "backend": "127.0.0.1:18081",
             "relay_port": 12223, "public_port": 2233},
        ]
        for index, name in enumerate(NAMES):
            access = {"mode": "public"}
            backend = "http://127.0.0.1:18080"
            if name == "basic":
                access = {"mode": "basic", "users": {"field": bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()}}
            elif name == "app":
                access = {"mode": "application"}
            elif name == "oidc":
                access = {"mode": "oidc", "port": 14180, "allowed_emails": ["allowed@example.test"]}
            elif name == "secure":
                backend = "https://origin.fixture.test:18443"
            elif name == "idp":
                backend = "http://127.0.0.1:15556"
            self.services.append({"name": name, "protocol": "http", "backend": backend,
                                  "relay_port": 12300 + index, "hostname": f"{name}.{self.domain}",
                                  "access": access})
        self.values["frp_services"] = self.services
        (self.folder / "credentials.json").write_text(json.dumps({
            "domain": self.domain, "basic_user": "field", "basic_password": self.password,
            "allowed_email": "allowed@example.test", "allowed_password": self.login_password,
            "denied_email": "denied@example.test", "denied_password": self.denied_password,
        }))

    def certificates(self):
        command(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                 "-subj", "/CN=Disposable FRP field root", "-keyout", self.folder / "ca.key",
                 "-out", self.folder / "ca.crt"])
        command(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=origin.fixture.test",
                 "-keyout", self.folder / "origin.key", "-out", self.folder / "origin.csr"])
        extension = self.folder / "origin.ext"
        extension.write_text("subjectAltName=DNS:origin.fixture.test\nextendedKeyUsage=serverAuth\n")
        command(["openssl", "x509", "-req", "-in", self.folder / "origin.csr", "-CA", self.folder / "ca.crt",
                 "-CAkey", self.folder / "ca.key", "-CAcreateserial", "-days", "2", "-extfile", extension,
                 "-out", self.folder / "origin.crt"])

    def prepare(self):
        hosts = ("relay",) if self.args.local else ("relay", "client")
        # Recover interrupted prior invocations before replacing credentials/fixtures.
        self.prepared.update(hosts)
        self.cleanup()
        for host in hosts:
            self.prepared.add(host)
            self.remote(host, "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
                        "openssh-server python3-aiohttp ca-certificates curl && "
                        "install -d -m 700 /opt/frp-field && systemctl start ssh")
            self.put(host, "/usr/local/share/ca-certificates/frp-field-origin.crt", (self.folder / "ca.crt").read_bytes(), "644")
            entries = self.address + " " + " ".join(f"{name}.{self.domain}" for name in NAMES)
            self.remote(host, "printf '%s\\n' " + shlex.quote(entries + " # frp-field-test") +
                        " '127.0.0.1 origin.fixture.test # frp-field-test' >> /etc/hosts; update-ca-certificates")
        # Go caches system roots within the Caddy process. A recovered run has a
        # newly generated fixture CA; reload alone would retain the old roots.
        self.remote("relay", "if systemctl is-active --quiet frp-tunnel-web.service; "
                    "then systemctl restart frp-tunnel-web.service; fi")
        self.remote("client", "(command -v docker >/dev/null || "
                    "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io) && systemctl start docker")
        for name in ("origin.crt", "origin.key"):
            self.put("client", "/opt/frp-field/" + name, (self.folder / name).read_bytes())
        self.put("client", "/opt/frp-field/origin.py", (ROOT / "scripts/fixtures/field-origin.py").read_bytes())
        unit = "[Unit]\nDescription=Disposable FRP field origin\n[Service]\nExecStart=/usr/bin/python3 /opt/frp-field/origin.py\nRestart=on-failure\n"
        self.put("client", "/etc/systemd/system/frp-field-origin.service", unit, "644")
        dex = {"issuer": f"https://idp.{self.domain}", "storage": {"type": "memory"},
               "web": {"http": "127.0.0.1:15556"}, "oauth2": {"skipApprovalScreen": True},
               "enablePasswordDB": True,
               "staticClients": [{"id": "field-app", "secret": self.values["frp_oidc"]["client_secret"],
                                  "name": "Disposable field app", "redirectURIs": [f"https://oidc.{self.domain}/oauth2/callback"]}],
               "staticPasswords": [{"email": email, "hash": bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
                                    "username": username, "userID": username}
                                   for email, password, username in [("allowed@example.test", self.login_password, "allowed"),
                                                                     ("denied@example.test", self.denied_password, "denied")]]}
        self.put("client", "/opt/frp-field/dex.yml", yaml.safe_dump(dex), "644")
        self.remote("client", "systemctl daemon-reload && systemctl start frp-field-origin && "
                    "docker run -d --name frp-field-dex --network host --read-only "
                    "--mount type=bind,src=/opt/frp-field/dex.yml,dst=/etc/dex/config.yml,readonly "
                    "ghcr.io/dexidp/dex:v2.43.1 dex serve /etc/dex/config.yml")

    def bootstrap(self):
        bootstrap = dict(self.values, frp_services=[s for s in self.services if s["name"] != "oidc"])
        self.deploy(bootstrap)
        roots = (self.folder / "ca.crt").read_bytes()
        if self.args.public_tls:
            roots += Path("/etc/ssl/certs/ca-certificates.crt").read_bytes()
        else:
            caddy_root = self.remote("relay", "cat " + CA_PATH).stdout
            roots += caddy_root
            for host in (("relay",) if self.args.local else ("relay", "client")):
                self.put(host, "/usr/local/share/ca-certificates/frp-field-caddy.crt", caddy_root, "644")
                self.remote(host, "update-ca-certificates")
            (self.folder / "caddy.crt").write_bytes(caddy_root)
        self.trust.write_bytes(roots)
        eventually(lambda: self.http("idp", "/.well-known/openid-configuration", status=200),
                   "Real Dex discovery did not become reachable")
        self.deploy()
        eventually(lambda: self.http("public", status=200), "Published HTTP origin did not become ready")
        self.mark("real-role deployment and verified TLS issuer discovery")

    def http(self, name, path="/", *, status=200, headers=(), credentials=None, scheme="https"):
        port = 443 if scheme == "https" else 80
        hostname = name + "." + self.domain
        argv = ["curl", "--silent", "--show-error", "--noproxy", "*", "--max-time", "12",
                "--cacert", self.trust, "--resolve", f"{hostname}:{port}:{self.address}",
                "--write-out", "\n%{http_code}", f"{scheme}://{hostname}{path}"]
        for header in headers:
            argv += ["-H", header]
        if credentials:
            argv += ["--user", credentials]
        result = command(argv, timeout=20)
        body, code = result.stdout.rsplit(b"\n", 1)
        require(int(code) == status, "Unexpected HTTP status for " + name)
        return body

    def ssh(self, key="login", *, port=2222, user=None):
        return ["ssh", "-F", "/dev/null", "-T", "-i", str(self.folder / (key + ".key")),
                "-p", str(port), "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes", "-o",
                "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o",
                "UserKnownHostsFile=" + str(self.folder / (key + "_known_hosts")), "-o",
                "ConnectTimeout=5", "-o", "ServerAliveInterval=2", "-o", "ServerAliveCountMax=2",
                f"{user or self.args.user}@{self.address}"]

    def tcp(self):
        argv = self.ssh()
        probe = command(argv + ["printf field-ok"], timeout=15)
        require(probe.stdout == b"field-ok", "Native key-only SSH failed")
        payload = os.urandom(1024 * 1024)
        digest = hashlib.sha256(payload).hexdigest()
        uploaded = command(argv + ["sha256sum"], data=payload, timeout=30)
        require(uploaded.stdout.decode().split()[0] == digest, "SSH upload corrupted")
        downloaded = command(argv + ["head -c 1048576 /dev/zero"], timeout=30)
        require(downloaded.stdout == bytes(1024 * 1024), "SSH download corrupted")
        rejected = command(argv[:-1] + ["-o", "PubkeyAuthentication=no", "-o",
                           "PreferredAuthentications=password,keyboard-interactive", argv[-1], "true"],
                           timeout=15, allow_failure=True)
        require(rejected.returncode != 0 and b"Permission denied (publickey)" in rejected.stderr,
                "Password/interactive authentication was not rejected")
        with socket.create_connection((self.address, 2233), timeout=10) as connection:
            # Interleave writes and reads so the fixture never needs unbounded buffering.
            for offset in range(0, len(payload), 32768):
                chunk = payload[offset:offset + 32768]
                connection.sendall(chunk)
                received = b""
                while len(received) < len(chunk):
                    part = connection.recv(len(chunk) - len(received))
                    require(bool(part), "Second TCP service closed early")
                    received += part
                require(received == chunk, "Second TCP service corrupted bytes")

    def restrictions(self):
        argv = self.ssh("relay", port=self.args.relay_port, user="frp-relay")
        denied = command(argv + ["true"], timeout=15, allow_failure=True)
        require(denied.returncode != 0, "Relay key allowed command execution")
        denied = command(argv[:-1] + ["-W", "127.0.0.1:22", argv[-1]], timeout=15, allow_failure=True)
        require(denied.returncode != 0 and b"administratively prohibited" in denied.stderr,
                "Relay allowed unrelated forwarding")
        denied = command(argv[:-1] + ["-N", "-o", "ExitOnForwardFailure=yes", "-R",
                                     "127.0.0.1:0:127.0.0.1:17000", argv[-1]], timeout=15, allow_failure=True)
        require(denied.returncode != 0 and b"remote port forwarding failed" in denied.stderr,
                "Relay allowed remote forwarding")
        relay_ports = {self.values["frp_server_port"], self.values["frp_server_admin_port"], 14180}
        relay_ports.update(service["relay_port"] for service in self.services)
        origin_ports = {self.values["frp_forward_port"], self.values["frp_client_admin_port"],
                        self.values["frp_backend_ssh_port"], 18080, 18081, 18443, 15556}
        self.private_listeners("relay", relay_ports)
        # These listeners live on the origin VPS, not self.address (the relay).
        # Inspect actual binds so even a firewall cannot conceal an auth bypass.
        self.private_listeners("client", origin_ports)
        if not self.args.local:
            for port in relay_ports:
                self.closed(port)
        self.mark("relay forwarding restrictions and no public authentication bypass")

    def private_listeners(self, host, ports):
        listeners = self.remote(host, "ss -lntH").stdout.decode().splitlines()
        found = set()
        for line in listeners:
            address, port = line.split()[3].rsplit(":", 1)
            port = int(port)
            if port in ports:
                found.add(port)
                require(address in ("127.0.0.1", "[::1]", "::1"),
                        host + " private listener escaped loopback")
        require(found == ports, host + " private listener check did not observe every expected service")

    def closed(self, port):
        try:
            with socket.create_connection((self.address, port), timeout=2):
                raise Failure("Removed/private port remains publicly reachable")
        except (ConnectionRefusedError, TimeoutError):
            pass

    def web(self):
        self.http("public", scheme="http", status=308)
        public = json.loads(self.http("public", headers=["X-Auth-Request-Email: forged@example.test",
                            "X-Auth-Request-User: attacker", "X-Forwarded-User: attacker",
                            "X-Forwarded-Email: forged@example.test", "X-Forwarded-For: 192.0.2.199"]))
        require(public["fixture"] == "frp-field-origin", "Wrong public backend")
        headers = {key.lower(): value for key, value in public["headers"].items()}
        require("forged@example.test" not in headers.values() and "attacker" not in headers.values(),
                "Spoofed identity reached origin")
        require("192.0.2.199" not in headers.get("x-forwarded-for", ""), "Spoofed forwarding address survived")
        self.http("basic", status=401)
        self.http("basic", status=401, credentials="field:incorrect")
        require(json.loads(self.http("basic", credentials="field:" + self.password))["fixture"] == "frp-field-origin",
                "Basic credentials did not authorize backend")
        self.http("app", status=401)
        self.http("app", headers=["Authorization: Bearer application-owned"])
        self.http("secure")
        # The same CA signs this deliberately wrong identity; a chain-only check must fail.
        wrong = copy.deepcopy(self.values)
        next(s for s in wrong["frp_services"] if s["name"] == "secure")["backend"] = "https://127.0.0.1:18443"
        self.deploy(wrong)
        try:
            eventually(lambda: self.http("secure", status=502), "HTTPS origin certificate identity was not verified")
        finally:
            self.deploy()
        eventually(lambda: self.http("secure"), "HTTPS backend failed to recover")
        # Unknown Host over an already valid TLS connection must never reach an app.
        result = command(["curl", "--silent", "--noproxy", "*", "--max-time", "10", "--cacert", self.trust,
                          "--resolve", f"public.{self.domain}:443:{self.address}",
                          "-H", f"Host: absent.{self.domain}", f"https://public.{self.domain}/"])
        require(b"frp-field-origin" not in result.stdout, "Unknown hostname routed to backend")
        self.mark("HTTPS redirect, TLS origin identity, access modes and identity-header isolation")

    def browser(self):
        from playwright.sync_api import sync_playwright
        home = self.browser_home
        nss = home / ".pki/nssdb"
        nss.mkdir(parents=True)
        if not self.args.public_tls:
            command(["certutil", "-N", "-d", "sql:" + str(nss), "--empty-password"])
            command(["certutil", "-A", "-d", "sql:" + str(nss), "-n", "Disposable FRP Caddy",
                     "-t", "C,,", "-i", self.folder / "caddy.crt"])
        environment = dict(os.environ, HOME=str(home))
        # Pin the actual installed browser path before changing HOME for NSS isolation.
        with sync_playwright() as playwright:
            executable = playwright.chromium.executable_path
            browser = playwright.chromium.launch(executable_path=executable, env=environment,
                args=["--no-sandbox", "--no-proxy-server",
                      f"--host-resolver-rules=MAP *.{self.domain} {self.address}"])
            try:
                context = browser.new_context()
                page = context.new_page()
                response = page.goto(f"https://public.{self.domain}/")
                require(response.status == 200, "Browser did not trust managed TLS certificate")
                measured = page.evaluate("""async () => {
                  const response = await fetch('/stream');
                  const reader = response.body.getReader(); const times = []; let body = '';
                  const start = performance.now();
                  while (true) { const part = await reader.read(); if (part.done) break;
                    times.push(performance.now() - start); body += new TextDecoder().decode(part.value); }
                  return {times, body};
                }""")
                require(measured["body"] == "data: 0\n\ndata: 1\n\ndata: 2\n\n" and
                        len(measured["times"]) >= 2 and measured["times"][0] < 900 and
                        measured["times"][-1] - measured["times"][0] >= 400,
                        "Streaming response was buffered or corrupted")
                echoed = page.evaluate("""() => new Promise((resolve, reject) => {
                  const ws = new WebSocket(`wss://${location.host}/ws`);
                  const timer = setTimeout(() => { ws.close(); reject('timeout'); }, 10000);
                  ws.onopen = () => ws.send('real-websocket');
                  ws.onerror = () => { clearTimeout(timer); reject('websocket failed'); };
                  ws.onmessage = e => { clearTimeout(timer); resolve(e.data); ws.close(); };
                })""")
                require(echoed == "real-websocket", "WebSocket upgrade/data failed")
                context.close()
                for allowed in (False, True):
                    context = browser.new_context(extra_http_headers={"X-Auth-Request-Email": "allowed@example.test"})
                    page = context.new_page()
                    requests = []
                    page.on("request", lambda request: requests.append(request.url))
                    destination = f"https://oidc.{self.domain}/private?next=%2Freport%3Fa%3D1&view=details"
                    page.goto(destination)
                    login = page.locator('input[name="login"]')
                    if not login.count():
                        page.get_by_text("Log in with Email", exact=False).click()
                    login.fill("allowed@example.test" if allowed else "denied@example.test")
                    page.locator('input[name="password"]').fill(self.login_password if allowed else self.denied_password)
                    page.locator('button[type="submit"]').click()
                    page.wait_for_function("""prefix => location.href.startsWith(prefix) ||
                      Array.from(document.querySelectorAll('button, input[type="submit"]'))
                        .some(button => (button.textContent.trim() || button.value) === 'Grant Access')
                    """, arg=f"https://oidc.{self.domain}/", timeout=30000)
                    consent = page.get_by_role("button", name="Grant Access", exact=True)
                    if consent.count():
                        consent.click()
                    page.wait_for_url(f"https://oidc.{self.domain}/**", timeout=30000)
                    body = page.locator("body").inner_text()
                    require(any("/auth?" in url for url in requests) and
                            any("/oauth2/callback?" in url and "code=" in url for url in requests),
                            "Browser did not complete real authorization-code callback")
                    if allowed:
                        result = json.loads(body)
                        require(page.url == destination, "OIDC callback did not restore the original path and query")
                        headers = {key.lower(): value for key, value in result["headers"].items()}
                        require(headers.get("x-auth-request-email") == "allowed@example.test",
                                "OIDC did not propagate verified identity")
                        cookies = context.cookies()
                        require(any(c["name"] == "__Host-frp-oidc" and c["secure"] and c["httpOnly"]
                                    and c["sameSite"] == "Lax" for c in cookies), "OIDC cookie security flags missing")
                    else:
                        require("frp-field-origin" not in body, "Unauthorized OIDC identity reached backend")
                    context.close()
                # No cookies plus spoofed identity must not reach the application.
                context = browser.new_context(extra_http_headers={"X-Auth-Request-Email": "allowed@example.test"})
                page = context.new_page()
                page.goto(f"https://oidc.{self.domain}/")
                require("frp-field-origin" not in page.locator("body").inner_text() and
                        page.url.startswith(f"https://idp.{self.domain}/"),
                        "OIDC identity spoofing bypassed authorization")
                context.close()
            except Exception:
                if ARTIFACTS:
                    (ARTIFACTS / "browser.html").write_text(page.content())
                    (ARTIFACTS / "browser-url.txt").write_text(page.url)
                    page.screenshot(path=str(ARTIFACTS / "browser.png"), full_page=True)
                raise
            finally:
                browser.close()
        self.mark("trusted browser TLS, WebSockets, incremental streaming and real Dex OIDC allow/deny callbacks")

    def doctor(self, host, stage=None):
        role = "server" if host == "relay" else "client"
        self.put(host, "/opt/frp-field/doctor.json", json.dumps(self.values))
        for relative in ("scripts/tunnel.py", "ansible/filter_plugins/frp_config.py"):
            path = "/opt/frp-field/repo/" + relative
            self.remote(host, "mkdir -p " + shlex.quote(str(Path(path).parent)))
            self.put(host, path, (ROOT / relative).read_bytes())
        result = self.remote(host, "python3 /opt/frp-field/repo/scripts/tunnel.py doctor --config "
                             "/opt/frp-field/doctor.json --role " + role + " --json --timeout 2", allow_failure=True)
        try:
            document = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise Failure("Doctor did not emit valid safe JSON") from None
        private_values = [self.password, self.login_password, self.denied_password,
                          self.values["frp_token"], self.values["frp_admin_password"],
                          self.values["frp_oidc"]["client_secret"], self.values["frp_oidc"]["cookie_secret"]]
        require(all(value.encode() not in result.stdout + result.stderr for value in private_values),
                "Doctor exposed credentials")
        if stage:
            service = {"backend": 2, "public_ingress": 3}.get(stage)
            require(result.returncode == 1 and any(c["stage"] == stage and c["status"] == "fail"
                                                  and (service is None or c.get("service") == service)
                                                  for c in document["checks"]), "Doctor misclassified " + stage)
        else:
            for expected in ("transport", "registration"):
                checks = [c for c in document["checks"] if c["stage"] == expected]
                require(checks and all(c["status"] == "ok" for c in checks),
                        "Doctor missed healthy " + expected)
            if host == "client":
                for expected in ("backend", "public_ingress"):
                    require(any(c["stage"] == expected and c.get("service") == 3 and c["status"] == "ok"
                                for c in document["checks"]), "Doctor missed healthy public application")

    def faults(self):
        self.doctor("client")
        self.doctor("relay")
        self.remote("client", "systemctl stop frp-tunnel-transport")
        try:
            self.doctor("client", "transport")
        finally:
            self.remote("client", "systemctl start frp-tunnel-transport")
        eventually(self.tcp, "SSH transport restart did not recover both TCP services")
        self.remote("client", "systemctl stop frp-tunnel-client")
        try:
            self.doctor("client", "registration")
        finally:
            self.remote("client", "systemctl start frp-tunnel-client")
        eventually(self.tcp, "FRP client restart did not recover both TCP services")
        self.remote("client", "systemctl stop frp-field-origin")
        try:
            self.doctor("client", "backend")
        finally:
            self.remote("client", "systemctl start frp-field-origin")
        eventually(lambda: self.http("public"), "Origin restart did not recover web publication")
        self.remote("relay", "systemctl stop frp-tunnel-web")
        try:
            self.doctor("client", "public_ingress")
        finally:
            self.remote("relay", "systemctl start frp-tunnel-web")
        eventually(lambda: self.http("public"), "Web ingress restart did not recover")
        self.mark("transport/registration/backend/ingress fault diagnostics and restart recovery")

    def validation(self):
        snapshot = '''import hashlib,json
from pathlib import Path
paths = [Path('/etc/frp-tunnel'), Path('/etc/systemd/system')]
result = {}
for root in paths:
 for path in sorted(root.rglob('*')):
  if path.is_file() and (root.name == 'frp-tunnel' or path.name.startswith('frp-tunnel')):
   s = path.stat(); result[str(path)] = [hashlib.sha256(path.read_bytes()).hexdigest(),s.st_mtime_ns,s.st_mode]
print(json.dumps(result,sort_keys=True))
'''
        hosts = ("relay",) if self.args.local else ("relay", "client")
        for host in hosts:
            self.put(host, "/opt/frp-field/snapshot.py", snapshot)
        before = {host: self.remote(host, "python3 /opt/frp-field/snapshot.py").stdout for host in hosts}
        bad_services = []
        collision = copy.deepcopy(self.values)
        collision["frp_services"][1]["relay_port"] = collision["frp_services"][0]["relay_port"]
        bad_services.append(collision)
        invalid = copy.deepcopy(self.values)
        next(s for s in invalid["frp_services"] if s["name"] == "basic")["access"]["users"]["field"] = "not-bcrypt"
        bad_services.append(invalid)
        invalid = copy.deepcopy(self.values)
        next(s for s in invalid["frp_services"] if s["name"] == "public")["hostname"] = "invalid\nroute"
        bad_services.append(invalid)
        for invalid in bad_services:
            config = self.folder / "invalid.json"
            config.write_text(json.dumps(invalid))
            result = command([sys.executable, ROOT / "scripts/tunnel.py", "validate", "--config", config], allow_failure=True)
            require(result.returncode != 0, "CLI accepted invalid service configuration")
            result = self.deploy(invalid, fail=True)
            require(result.returncode != 0, "Ansible accepted invalid service configuration")
            after = {host: self.remote(host, "python3 /opt/frp-field/snapshot.py").stdout for host in hosts}
            require(before == after, "Invalid configuration mutated deployed files")
            self.tcp()
            self.http("public")
        second = self.deploy()
        recaps = re.findall(r"^\S+\s+:\s+ok=\d+\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)",
                            second.stdout.decode(), re.MULTILINE)
        require(len(recaps) == 2 and all(row == ("0", "0", "0") for row in recaps),
                "Repeated deployment was not idempotent")
        self.mark("invalid/colliding configuration rejected without mutation; second apply changes zero")

    def failed_web_rollout(self):
        revised = copy.deepcopy(self.values)
        public = next(service for service in revised["frp_services"] if service["name"] == "public")
        basic = next(service for service in revised["frp_services"] if service["name"] == "basic")
        public["access"] = copy.deepcopy(basic["access"])
        # Fail after shared validation, while installing the relay's web policy.
        revised["frp_web_tls"] = "invalid-fixture-mode"
        result = self.deploy(revised, fail=True)
        require(result.returncode != 0, "Injected web rollout failure unexpectedly succeeded")
        # Even an explicit subsequent client-only apply must not route newly
        # registered backends through the relay's previous public access policy.
        self.deploy(revised, limit="frp_clients")
        self.remote("client", "systemctl restart frp-tunnel-client")
        self.closed(443)
        if self.args.local:
            # Inventory aliases share a token file on this one-VM fixture.
            # Restart from the restored token rather than retaining its cache.
            self.remote("client", "systemctl stop frp-tunnel-client")
        self.deploy()
        eventually(lambda: self.http("public"), "Web ingress did not recover after the failed rollout")
        self.mark("failed relay web rollout cannot publish a new revision under the old access policy")

    def cutover(self):
        basic = next(service for service in self.services if service["name"] == "basic")
        oidc = next(service for service in self.services if service["name"] == "oidc")
        revised = copy.deepcopy(self.values)
        revised["frp_services"] = [service for service in revised["frp_services"] if service["name"] != "oidc"]
        published = next(service for service in revised["frp_services"] if service["name"] == "basic")
        published.clear()
        published.update(name="basic", protocol="tcp", backend="127.0.0.1:18081",
                         relay_port=basic["relay_port"], public_port=oidc["access"]["port"])
        # Exercise HTTP ingress as well as socket publication against a relay port
        # that previously belonged to a protected registration.
        public = next(service for service in revised["frp_services"] if service["name"] == "public")
        public["relay_port"] = oidc["relay_port"]
        self.http("basic", status=401)
        self.http("basic", credentials="field:" + self.password)
        client_files = "/etc/frp-tunnel/frpc.toml"
        if not self.args.local:
            client_files += " /etc/frp-tunnel/token"
        client_before = self.remote("client", "sha256sum " + client_files).stdout
        with socket.create_connection((self.address, 2233), timeout=5) as persistent:
            persistent.sendall(b"before-revision")
            require(persistent.recv(64) == b"before-revision", "Persistent pre-cutover TCP service failed")
            # No client play may run: this is the interrupted/--limit deployment.
            self.deploy(revised, limit="frp_servers")
            require(client_before == self.remote("client", "sha256sum " + client_files).stdout,
                    "Relay-only cutover unexpectedly changed the origin revision")
            try:
                require(persistent.recv(1) == b"", "Old revision retained an established data connection")
            except ConnectionResetError:
                pass
            except TimeoutError:
                raise Failure("Old revision retained an established data connection") from None
        self.remote("relay", "systemctl is-active frp-tunnel-publish-basic.socket")
        listeners = self.remote("relay", "ss -lntH").stdout.decode().splitlines()
        require(any(line.split()[3] == "0.0.0.0:" + str(published["public_port"]) for line in listeners),
                "Public TCP socket did not bind the removed OIDC listener port")
        state = self.remote("relay", "systemctl is-active frp-tunnel-oidc-oidc", allow_failure=True)
        require(state.returncode != 0, "Removed OIDC process still owns its old listener")
        if not self.args.local:
            # A restarted old origin must still be unable to register, not merely
            # the old process that was disconnected during the server restart.
            self.remote("client", "systemctl restart frp-tunnel-client")
        # The one-VM fixture shares /etc/frp-tunnel/token between inventory aliases;
        # keep its old running FRPC credential rather than reread the relay file.
        # In either mode observe reconnect attempts, not only the first refused SYN.
        request = f"GET / HTTP/1.1\r\nHost: basic.{self.domain}\r\nConnection: close\r\n\r\n".encode()
        deadline = time.monotonic() + 15
        while True:
            try:
                with socket.create_connection((self.address, published["public_port"]), timeout=2) as connection:
                    connection.sendall(request)
                    require(connection.recv(4096) == b"",
                            "Old protected HTTP registration leaked through the reused public TCP route")
            except (ConnectionError, TimeoutError):
                pass
            self.http("public", status=502)
            if time.monotonic() >= deadline:
                break
            time.sleep(1)

        def revised_backends():
            with socket.create_connection((self.address, published["public_port"]), timeout=5) as connection:
                connection.sendall(b"new-revision")
                require(connection.recv(64) == b"new-revision", "Revised TCP route did not reach the new backend")
            require(json.loads(self.http("public"))["fixture"] == "frp-field-origin",
                    "Revised HTTP route did not reach its backend")

        self.deploy(revised)
        eventually(revised_backends, "Full cutover did not restore the revised backends")
        second = self.deploy(revised)
        recaps = re.findall(r"changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", second.stdout.decode())
        require(len(recaps) == 2 and all(row == ("0", "0", "0") for row in recaps),
                "Identical cutover reapply was not idempotent")
        self.deploy()
        eventually(self.tcp, "TCP services did not recover after restoring the original revision")
        eventually(lambda: self.http("public"), "HTTP service did not recover after restoring the original revision")
        self.http("basic", status=401)
        self.private_listeners("relay", {oidc["access"]["port"]})
        self.mark("relay-only revisions revoke old registrations/connections; HTTP/TCP port reuse fails closed")
        self.mark("removed OIDC port reused for public TCP; full cutover and identical reapply succeed")

    def removal(self):
        reduced = dict(self.values, frp_managed_ssh=False,
                       frp_services=[s for s in self.services if s["name"] == "echo"])
        for name in ("frp_login_user", "frp_login_public_key"):
            reduced.pop(name, None)
        self.deploy(reduced)
        eventually(lambda: self.closed(2222), "Removed SSH publication remained open")
        for port in (80, 443, 14180, 12222, 12300, 12301, 12302, 12303, 12304, 12305):
            eventually(lambda port=port: self.closed(port), "Removed web/proxy listener remained open")
        listeners = self.remote("relay", "ss -lntH").stdout.decode().splitlines()
        removed = {80, 443, 14180, 12222, 12300, 12301, 12302, 12303, 12304, 12305}
        require(not any(int(line.split()[3].rsplit(":", 1)[1]) in removed for line in listeners),
                "Removed service retained a relay loopback listener")
        state = self.remote("client", "systemctl is-active frp-tunnel-sshd", allow_failure=True)
        require(state.returncode != 0, "Optional managed SSH still running")
        state = self.remote("relay", "systemctl is-active frp-tunnel-oidc-oidc", allow_failure=True)
        require(state.returncode != 0, "Removed OIDC instance still running")
        with socket.create_connection((self.address, 2233), timeout=5) as connection:
            connection.sendall(b"surviving-service")
            require(connection.recv(64) == b"surviving-service", "Removal broke surviving TCP service")
        second = self.deploy(reduced)
        recaps = re.findall(r"changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", second.stdout.decode())
        require(len(recaps) == 2 and all(row == ("0", "0", "0") for row in recaps),
                "TCP-only optional-SSH deployment was not idempotent")
        self.mark("service removal, listener cleanup and managed-SSH-disabled deployment")

    def cleanup(self):
        for host in self.prepared:
            self.remote(host, "systemctl stop frp-field-origin 2>/dev/null; docker rm -f frp-field-dex 2>/dev/null; "
                        "rm -f /etc/systemd/system/frp-field-origin.service "
                        "/usr/local/share/ca-certificates/frp-field-origin.crt "
                        "/usr/local/share/ca-certificates/frp-field-caddy.crt; "
                        "rm -rf /opt/frp-field; "
                        "python3 -c \"from pathlib import Path; p=Path('/etc/hosts'); "
                        "p.write_text(''.join(x for x in p.read_text().splitlines(True) if '# frp-field-test' not in x))\"; "
                        "systemctl daemon-reload; update-ca-certificates", allow_failure=True)

    def execute(self):
        if self.args.require_dns:
            for name in NAMES:
                addresses = {item[4][0] for item in socket.getaddrinfo(name + "." + self.domain, 443)}
                require(self.address in addresses, "Public DNS does not resolve to relay")
        for host in (("relay",) if self.args.local else ("relay", "client")):
            self.remote(host, "apt-get update -qq && DEBIAN_FRONTEND=noninteractive "
                        "apt-get install -y -qq openssh-server python3-yaml && systemctl start ssh")
        self.make_keys()
        self.certificates()
        success = False
        try:
            self.prepare()
            self.bootstrap()
            eventually(self.tcp, "Two native TCP services did not become ready")
            self.mark("two simultaneous native TCP services, key-only SSH and 1 MiB transfers")
            self.restrictions()
            self.web()
            self.browser()
            self.faults()
            self.validation()
            self.failed_web_rollout()
            self.cutover()
            self.removal()
            if self.args.keep_fixtures:
                self.deploy()
                eventually(lambda: self.http("public"), "Full deployment did not restore for inspection")
            success = True
        finally:
            if not (success and self.args.keep_fixtures):
                self.cleanup()


def main(argv=None):
    global ARTIFACTS
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--ssh-config", type=Path)
    parser.add_argument("--relay-ssh", default="relay")
    parser.add_argument("--client-ssh", default="origin")
    parser.add_argument("--address", default="127.0.0.1")
    parser.add_argument("--domain", default="frp-field.test")
    parser.add_argument("--user", required=True)
    parser.add_argument("--relay-port", type=int, default=22)
    parser.add_argument("--require-dns", action="store_true")
    parser.add_argument("--public-tls", action="store_true")
    parser.add_argument("--keep-fixtures", action="store_true",
                        help="Keep private inputs and restore all apps for inspection after success; destroy VPSs afterward")
    parser.add_argument("--artifacts", type=Path,
                        help="Optional private directory for failed subprocess logs; may contain secrets")
    parser.add_argument("--local", action="store_true", help="Destructively use this one disposable root-run systemd VM")
    args = parser.parse_args(argv)
    if args.local and os.geteuid() != 0:
        parser.error("--local requires root on a disposable VM")
    if not args.local and (not args.inventory or not args.ssh_config):
        parser.error("Two-host mode requires --inventory and --ssh-config")
    if args.public_tls and not args.require_dns:
        parser.error("--public-tls requires --require-dns")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", args.domain) or "." not in args.domain:
        parser.error("--domain must be a DNS suffix")
    if args.user == "root":
        parser.error("--user must be a non-root login identity")
    os.umask(0o077)
    if args.artifacts:
        args.artifacts.mkdir(parents=True, mode=0o700, exist_ok=True)
        args.artifacts.chmod(0o700)
        ARTIFACTS = args.artifacts
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(name, None)
    folder = Path(tempfile.mkdtemp(prefix="frp-field-"))
    success = False
    try:
        if args.local:
            args.inventory = folder / "inventory.json"
            args.inventory.write_text(json.dumps({"all": {"children": {
                "frp_servers": {"hosts": {"relay": {"ansible_connection": "local"}}},
                "frp_clients": {"hosts": {"client": {"ansible_connection": "local"}}}}}}))
        Suite(args, folder).execute()
        success = True
    finally:
        if not (success and args.keep_fixtures):
            shutil.rmtree(folder)
    if args.keep_fixtures:
        print("Field regression passed; private inspection inputs retained at " + str(folder))
    else:
        print("Field regression passed; disposable fixture credentials and services removed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        if ARTIFACTS:
            import traceback
            (ARTIFACTS / "exception.log").write_text(traceback.format_exc())
        # Browser URLs/callbacks and subprocess errors may contain ephemeral credentials.
        print("FAIL " + (str(error) if isinstance(error, Failure) else type(error).__name__ +
                         ": private exception details withheld"), file=sys.stderr)
        sys.exit(1)
