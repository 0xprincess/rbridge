import importlib.util
import io
import hashlib
from pathlib import Path
import tarfile
import tempfile
import unittest

from jinja2 import Environment, FileSystemLoader, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


privacy = load_script("check-privacy")
secrets = load_script("create-secrets")
download = load_script("download-frp")


class PrivacyTests(unittest.TestCase):
    def test_private_key_header_is_rejected(self):
        candidate = b"-----BEGIN " + b"OPENSSH " + b"PRIVATE KEY-----"
        self.assertIn("private key", {kind for _, kind in privacy.findings(candidate)})

    def test_public_key_and_comment_are_rejected(self):
        candidate = b"ssh-" + b"ed25519 " + b"A" * 48 + b" personal-comment"
        self.assertIn("SSH public key", {kind for _, kind in privacy.findings(candidate)})

    def test_real_endpoint_is_rejected(self):
        candidate = b"8" + b".8.8.8"
        self.assertIn("deployment IP address", {kind for _, kind in privacy.findings(candidate)})

    def test_documentation_addresses_are_accepted(self):
        self.assertFalse(privacy.findings(b"127.0.0.1 0.0.0.0 192.0.2.1 203.0.113.10"))

    def test_email_and_url_credentials_are_rejected(self):
        candidate = b"person" + b"@mail.test https://" + b"user:pass@relay.example.invalid"
        kinds = {kind for _, kind in privacy.findings(candidate)}
        self.assertIn("personal email address", kinds)
        self.assertIn("credential in URL", kinds)

    def test_generic_author_is_accepted(self):
        self.assertFalse(privacy.findings(b"contributors@example.invalid"))

    def test_literal_frp_token_is_rejected(self):
        candidate = b"frp_token: " + b"a" * 64
        self.assertIn("FRP token", {kind for _, kind in privacy.findings(candidate)})

    def test_windows_personal_path_is_rejected(self):
        candidate = b"C:" + b"\\Users\\someone\\file"
        self.assertIn("personal filesystem path", {kind for _, kind in privacy.findings(candidate)})


class InputTests(unittest.TestCase):
    def test_host_rejects_command_injection(self):
        for value in ("-oProxyCommand=x", "host;touch x", "host\nother", "user@host"):
            with self.assertRaises(ValueError):
                secrets.validate_host(value)

    def test_dns_and_documentation_ip_are_valid(self):
        self.assertEqual(secrets.validate_host("relay.example.invalid"), "relay.example.invalid")
        self.assertEqual(secrets.validate_host("203.0.113.10"), "203.0.113.10")

    def test_host_pin_must_match_endpoint_and_port(self):
        key = "ssh-" + "ed25519 " + "A" * 48
        text = "[relay.example.invalid]:2200 " + key
        self.assertTrue(secrets.pinned_hosts(text, "relay.example.invalid", 2200))
        with self.assertRaises(ValueError):
            secrets.pinned_hosts(text, "relay.example.invalid", 22)

    def test_existing_secret_directory_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                secrets.validate_destination(folder)

    def test_checksum_mismatch_never_writes_binaries(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                download.unpack(b"modified", folder, "0.71.0", "amd64", "0" * 64)
            self.assertFalse(list(Path(folder).iterdir()))

    def test_archive_extraction_ignores_unrelated_paths(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name in ("frps", "frpc"):
                member = tarfile.TarInfo("frp_0.71.0_linux_amd64/" + name)
                member.size = 2
                archive.addfile(member, io.BytesIO(b"ok"))
            member = tarfile.TarInfo("../../unrelated")
            member.size = 3
            archive.addfile(member, io.BytesIO(b"bad"))
        data = buffer.getvalue()
        with tempfile.TemporaryDirectory() as folder:
            download.unpack(data, folder, "0.71.0", "amd64", hashlib.sha256(data).hexdigest())
            self.assertEqual({path.name for path in Path(folder).iterdir()}, {"frps", "frpc"})


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.values = {"frp_server_port": 7000, "frp_forward_port": 17001,
                       "frp_public_ssh_port": 2222, "frp_backend_ssh_port": 2222,
                       "frp_relay_ssh_port": 22, "frp_login_user": "developer",
                       "frp_relay_user": "frp-relay", "frp_service_user": "frp-tunnel",
                       "frp_binary_dir": "/opt/frp", "frp_server_address": "relay.example.invalid"}

    def render(self, role, name):
        environment = Environment(loader=FileSystemLoader(ROOT / f"ansible/roles/{role}/templates"),
                                  undefined=StrictUndefined)
        return environment.get_template(name + ".j2").render(self.values)

    def test_server_control_port_is_never_public(self):
        content = self.render("frp_server", "frps.toml")
        self.assertIn('bindAddr = "127.0.0.1"', content)
        self.assertIn('allowPorts = [{ single = 2222 }]', content)
        self.assertNotIn("token =", content)

    def test_client_has_no_proxy_and_reconnects(self):
        content = self.render("frp_client", "frpc.toml")
        self.assertIn('serverAddr = "127.0.0.1"', content)
        self.assertIn("loginFailExit = false", content)
        service = self.render("frp_client", "frp-tunnel-client.service")
        self.assertIn("UnsetEnvironment=HTTP_PROXY", service)
        self.assertIn("PartOf=frp-tunnel-transport.service", service)

    def test_key_only_ssh_is_isolated(self):
        content = self.render("frp_client", "sshd_config")
        for expected in ("PasswordAuthentication no", "KbdInteractiveAuthentication no",
                         "AuthenticationMethods publickey", "PermitRootLogin no",
                         "AllowUsers developer", "ListenAddress 127.0.0.1"):
            self.assertIn(expected, content)

    def test_transport_requires_host_pin_and_no_proxy(self):
        content = self.render("frp_client", "frp-tunnel-transport.service")
        for expected in ("StrictHostKeyChecking=yes", "ProxyCommand=none", "ProxyJump=none",
                         "IdentityAgent=none", "ExitOnForwardFailure=yes"):
            self.assertIn(expected, content)
        self.assertNotIn("root@", content)

    def test_secret_tasks_suppress_logs_and_diffs(self):
        for role in ("frp_common", "frp_client", "frp_server"):
            tasks = yaml.safe_load((ROOT / f"ansible/roles/{role}/tasks/main.yml").read_text())
            for task in tasks:
                content = task.get("ansible.builtin.copy", {}).get("content", "")
                if any(name in content for name in ("frp_token", "private_key", "public_key", "known_hosts")):
                    self.assertTrue(task.get("no_log"), task["name"])
                    self.assertFalse(task.get("diff", True), task["name"])

    def test_assertions_are_yaml_strings(self):
        for role in ("frp_common", "frp_client", "frp_server"):
            tasks = yaml.safe_load((ROOT / f"ansible/roles/{role}/tasks/main.yml").read_text())
            for task in tasks:
                for expression in task.get("ansible.builtin.assert", {}).get("that", []):
                    self.assertIsInstance(expression, str, task["name"])


if __name__ == "__main__":
    unittest.main()
