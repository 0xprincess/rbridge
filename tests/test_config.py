"""Behavioral tests for the shared, dependency-free service model."""

import base64
import copy
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("frp_config", ROOT / "ansible/filter_plugins/frp_config.py")
CONFIG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONFIG)


def tcp(**changes):
    value = dict(name="ssh", protocol="tcp", backend="127.0.0.1:2222", relay_port=12222, public_port=2222)
    value.update(changes)
    return value


def web(**changes):
    value = dict(name="app", protocol="http", backend="http://localhost:8080", relay_port=18080,
                 hostname="app.example.invalid", access={"mode": "public"})
    value.update(changes)
    return value


def oidc():
    return dict(issuer_url="https://identity.example.invalid/issuer", client_id="example-app",
                client_secret="not-a-real-credential", cookie_secret=base64.urlsafe_b64encode(bytes(range(32))).decode())


def authenticated_web(**changes):
    access = dict(mode="oidc", port=18100, allowed_domains=["example.invalid"])
    access.update(changes)
    return web(access=access)


class ServiceModelTests(unittest.TestCase):
    def assert_invalid(self, services, field=None, **kwargs):
        with self.assertRaises(ValueError) as context:
            CONFIG.normalize_services(services, **kwargs)
        if field:
            self.assertIn(field, str(context.exception))
        return str(context.exception)

    def test_multiple_services_preserve_inputs_and_normalize_backends(self):
        source = [tcp(), tcp(name="database", backend="db.internal:5432", relay_port=15432, public_port=25432),
                  web(), web(name="secure", backend="https://Origin.Example.Invalid:8443/", relay_port=18443,
                             hostname="Secure.Example.Invalid", access={"mode": "application"})]
        original = copy.deepcopy(source)
        result = CONFIG.normalize_services(source, reserved_ports=(7000, 22, 17401, 80, 443))
        self.assertEqual(source, original)
        self.assertEqual([item["backend_scheme"] for item in result], ["tcp", "tcp", "http", "https"])
        self.assertEqual(result[1]["backend_host"], "db.internal")
        self.assertEqual(result[3]["backend_host"], "origin.example.invalid")
        self.assertEqual(result[3]["hostname"], "secure.example.invalid")
        self.assertEqual(result[3]["backend_port"], 8443)
        self.assertEqual(result[0]["public_port"], result[0]["backend_port"])

    def test_ipv6_backends_are_normalized_without_brackets(self):
        result = CONFIG.normalize_services([tcp(backend="[::1]:2222"), web(backend="https://[::1]:8443")])
        self.assertEqual([item["backend_host"] for item in result], ["::1", "::1"])

    def test_missing_or_wrong_container_rejected(self):
        for value in (None, {}, [], "ssh", [None], [1], [[]]):
            with self.subTest(value=value):
                self.assert_invalid(value, "frp_services")

    def test_required_fields_and_protocol_specific_fields(self):
        for factory, fields in ((tcp, ("name", "protocol", "backend", "relay_port", "public_port")),
                                (web, ("name", "protocol", "backend", "relay_port", "hostname", "access"))):
            for field in fields:
                value = factory()
                del value[field]
                with self.subTest(field=field, protocol=factory.__name__):
                    self.assert_invalid([value], "frp_services[0]")
        for value in (tcp(hostname="app.example.invalid"), tcp(access={"mode": "public"}), web(public_port=18081),
                      tcp(extra="unsupported"), tcp(protocol="udp")):
            self.assert_invalid([value])

    def test_names_and_hostnames_are_safe_unique_and_explicit(self):
        for name in ("../app", "app.service", "a\nb", "APP", "-app", "app_1", "x" * 64, 42):
            self.assert_invalid([tcp(name=name)], ".name")
        self.assert_invalid([tcp(), tcp(relay_port=12223, public_port=2223)], ".name")
        for hostname in ("*.example.invalid", "localhost", "-app.example.invalid", "app.example.invalid:443",
                         "app.example.invalid {", "https://app.example.invalid", "127.0.0.1"):
            self.assert_invalid([web(hostname=hostname)], ".hostname")
        self.assert_invalid([web(), web(name="other", relay_port=18081, hostname="APP.EXAMPLE.INVALID")], ".hostname")

    def test_unsafe_and_ambiguous_backends_rejected(self):
        for backend in ("host", "host:0", "host:65536", "host:-1", "host:abc", "host:22\n", "-host:22",
                        "host;touch:22", "user@host:22", "127.0.0.999:22", "::1:22", "host/path:22"):
            with self.subTest(backend=backend):
                self.assert_invalid([tcp(backend=backend)], ".backend")
        for backend in ("ftp://host:80", "http://host", "http://host:80/path", "http://host:80?token=private",
                        "http://host:80#frag", "http://" + "user:secret@host:80", "http://host:99999", "http://host:80\\path",
                        "http://host:80\n", "http://{host}:80"):
            with self.subTest(backend=backend):
                self.assert_invalid([web(backend=backend)], ".backend")

    def test_high_integer_listener_ports_required(self):
        for field in ("relay_port", "public_port"):
            for value in (True, False, "12222", 12222.0, 0, 1023, 65536, None):
                self.assert_invalid([tcp(**{field: value})], "." + field)
        for port in (1024, 65535):
            self.assertEqual(CONFIG.normalize_services([tcp(public_port=port)])[0]["public_port"], port)

    def test_all_relay_listeners_share_one_collision_domain(self):
        cases = [
            [tcp(public_port=12222)],
            [tcp(), tcp(name="second", relay_port=12222, public_port=2223)],
            [tcp(), tcp(name="second", relay_port=12223, public_port=2222)],
            [tcp(), web(relay_port=2222)],
            [tcp(), authenticated_web(port=2222)],
            [authenticated_web(port=18080)],
            [authenticated_web(), tcp(relay_port=18100)],
        ]
        for services in cases:
            with self.subTest(services=services):
                self.assert_invalid(services, "collides", oidc=oidc())
        for port in (7000, 17401, 2222):
            self.assert_invalid([tcp(public_port=port)], "collides", reserved_ports=(7000, 17401, 2222))
        self.assert_invalid([authenticated_web(port=17401)], "collides", reserved_ports=(17401,), oidc=oidc())

    def test_web_access_requires_explicit_valid_policy(self):
        for access in (None, {}, "public", {"mode": "unknown"}, {"mode": "public", "users": {}},
                       {"mode": "application", "port": 18100}, {"mode": "basic"}, {"mode": "basic", "users": {}}):
            self.assert_invalid([web(access=access)], ".access")
        for mode in ("public", "application"):
            self.assertEqual(CONFIG.normalize_services([web(access={"mode": mode})])[0]["access"]["mode"], mode)

    def test_basic_bcrypt_hashes_and_usernames(self):
        for prefix in ("2a", "2b", "2y"):
            password = "$" + prefix + "$12$" + "A" * 53
            result = CONFIG.normalize_services([web(access={"mode": "basic", "users": {"alice": password}})])
            self.assertEqual(result[0]["access"]["users"]["alice"], password)
        for password in ("plaintext", "$2b$03$" + "A" * 53, "$2b$32$" + "A" * 53, "$2b$12$" + "A" * 52):
            self.assert_invalid([web(access={"mode": "basic", "users": {"alice": password}})], ".users")
        for username in ("{env.SECRET}", "alice\nbob", "a b", "#comment", ""):
            self.assert_invalid([web(access={"mode": "basic", "users": {username: "$2b$12$" + "A" * 53}})], ".users")

    def test_oidc_restrictions_are_required_and_normalized(self):
        result = CONFIG.normalize_services([authenticated_web(allowed_domains=["EXAMPLE.INVALID"],
                                                             allowed_emails=["Alice@EXAMPLE.INVALID"])], oidc=oidc())
        self.assertEqual(result[0]["access"]["allowed_domains"], ["example.invalid"])
        self.assertEqual(result[0]["access"]["allowed_emails"], ["alice@example.invalid"])
        for changes in ({"allowed_domains": []}, {"allowed_domains": ["*"]}, {"allowed_domains": "example.invalid"},
                        {"allowed_domains": ["*.example.invalid"]}, {"allowed_emails": ["*@example.invalid"]},
                        {"allowed_emails": ["alice"]}, {"allowed_emails": ["alice@localhost"]}, {"port": "18100"}):
            self.assert_invalid([authenticated_web(**changes)], ".access", oidc=oidc())

    def test_oidc_provider_and_secret_validation(self):
        self.assert_invalid([authenticated_web()], "frp_oidc")
        for field in oidc():
            provider = oidc()
            del provider[field]
            self.assert_invalid([authenticated_web()], "frp_oidc", oidc=provider)
        for field, value in (("issuer_url", "http://identity.example.invalid"), ("issuer_url", "https://" + "user:secret@host"),
                             ("client_id", ""), ("client_secret", "secret\nvalue"), ("cookie_secret", "invalid"),
                             ("cookie_secret", base64.urlsafe_b64encode(b"short").decode())):
            provider = oidc()
            provider[field] = value
            self.assert_invalid([authenticated_web()], "frp_oidc." + field, oidc=provider)

    def test_field_errors_do_not_disclose_values(self):
        marker = "never-print-this-secret"
        values = [tcp(name=marker + ";"), tcp(backend=marker), web(access={"mode": "basic", "users": {"alice": marker}})]
        for value in values:
            error = self.assert_invalid([value])
            self.assertNotIn(marker, error)
            self.assertEqual(CONFIG.service_error([value]), error)
        provider = oidc()
        provider["cookie_secret"] = marker
        error = self.assert_invalid([authenticated_web()], oidc=provider)
        self.assertNotIn(marker, error)
        self.assertEqual(CONFIG.service_error([tcp()]), "")


if __name__ == "__main__":
    unittest.main()
