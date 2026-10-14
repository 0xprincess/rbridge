import importlib.util
import io
import hashlib
from pathlib import Path
import tarfile
import tempfile
import unittest


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
        self.assertFalse(privacy.findings(b"fixture@example.test"))

    def test_email_and_url_credentials_are_rejected(self):
        candidate = b"person" + b"@mail.net https://" + b"user:pass@" + b"relay.relay-ip.rbr.sh"
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
        self.assertEqual(secrets.validate_host("relay.relay-ip.rbr.sh"), "relay.relay-ip.rbr.sh")
        self.assertEqual(secrets.validate_host("203.0.113.10"), "203.0.113.10")

    def test_host_pin_must_match_endpoint_and_port(self):
        key = "ssh-" + "ed25519 " + "A" * 48
        text = "[relay.relay-ip.rbr.sh]:2200 " + key
        self.assertTrue(secrets.pinned_hosts(text, "relay.relay-ip.rbr.sh", 2200))
        with self.assertRaises(ValueError):
            secrets.pinned_hosts(text, "relay.relay-ip.rbr.sh", 22)

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




if __name__ == "__main__":
    unittest.main()
