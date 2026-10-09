"""Behavioral coverage of safe discovery and scoped health aggregation."""

import importlib.util
import json
from pathlib import Path
import shlex
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


status = load("bridge_status", "scripts/bridge_status.py")
model = load("discovery_model", "ansible/filter_plugins/frp_config.py")


def http_service(mode="public"):
    access = {"mode": mode}
    if mode == "basic":
        access["users"] = {"private-account": "$2b$12$" + "a" * 53}
    return {"name": "private-web", "protocol": "http", "backend": "http://backend.private.invalid:8080",
            "relay_port": 12080, "hostname": "web.private.invalid", "access": access}


def tcp_service(**changes):
    result = {"name": "private-tcp", "protocol": "tcp", "backend": "127.0.0.1:2222",
              "relay_port": 12222, "public_port": 2222}
    result.update(changes)
    return result


def check(index=1, state="ok", stage="public_ingress"):
    return {"service": index, "stage": stage, "status": state,
            "message": "private-account secret-token /home/" + "private/key backend.private.invalid",
            "action": "cat /home/" + "private/key"}


class ServiceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.config = {"frp_server_address": "relay.private.invalid", "frp_token": "secret-token",
                       "frp_admin_password": "admin-secret", "frp_login_user": "private_user",
                       "frp_login_public_key": "ssh-ed25519 AAAA private-comment",
                       "frp_relay_private_key": "PRIVATE KEY MATERIAL", "frp_managed_ssh": True}

    def describe(self, raw=None, **kwargs):
        services = model.normalize_services(raw if raw is not None else [http_service()])
        return status.describe_services(self.config, services, **kwargs)

    def test_default_json_and_text_redact_configuration_and_diagnostics(self):
        result = self.describe([http_service("basic"), tcp_service()], checks=[check(), check(2)],
                               deployment="/private/path")
        output = json.dumps(result) + status.format_services(result)
        for private in ("private-web", "private-tcp", "backend.private.invalid", "web.private.invalid",
                        "relay.private.invalid", "private-account", "private_user", "secret-token",
                        "admin-secret", "PRIVATE KEY MATERIAL", "/home/private", "$2b$", "/private/path"):
            with self.subTest(private=private):
                self.assertNotIn(private, output)
        self.assertEqual(result["services"][0]["service"], 1)
        self.assertEqual(result["services"][0]["access"], "basic")
        self.assertEqual(result["services"][1]["protocol"], "tcp")

    def test_reveal_http_url_and_copyable_curl_but_no_secrets(self):
        result = self.describe([http_service("basic")], reveal=True, checks=[check()])
        row = result["services"][0]
        self.assertEqual(row["name"], "private-web")
        self.assertEqual(row["endpoint"], "https://web.private.invalid/")
        self.assertEqual(shlex.split(row["command"]),
                         ["curl", "--show-error", "--include", "--", row["endpoint"]])
        output = json.dumps(result) + status.format_services(result)
        for secret in ("private-account", "$2b$", "secret-token", "admin-secret", "PRIVATE KEY MATERIAL", "/home/private"):
            self.assertNotIn(secret, output)
        self.assertNotIn("backend.private.invalid", output)

    def test_tcp_hostname_and_ipv6_endpoints(self):
        for host, expected in (("relay.private.invalid", "relay.private.invalid:2222"),
                               ("2001:db8::123", "[2001:db8::123]:2222")):
            with self.subTest(host=host):
                self.config["frp_server_address"] = host
                row = self.describe([tcp_service()], reveal=True)["services"][0]
                self.assertEqual(row["endpoint"], expected)
                self.assertEqual(shlex.split(row["command"]),
                                 ["ssh", "-p", "2222", "-l", "private_user", "--", host])

    def test_generic_tcp_never_infers_ssh_from_name(self):
        for changes in ({"backend": "127.0.0.1:5432"}, {"backend": "localhost:2222"},
                        {"backend": "[::1]:2222"}, {"backend": "elsewhere.invalid:2222"}):
            with self.subTest(changes=changes):
                row = self.describe([tcp_service(name="ssh", **changes)], reveal=True)["services"][0]
                self.assertIsNone(row["command"])
                self.assertEqual(row["access"], "application")
        self.config["frp_managed_ssh"] = False
        row = self.describe([tcp_service(name="ssh")], reveal=True)["services"][0]
        self.assertIsNone(row["command"])

    def test_ssh_uses_actual_managed_port_and_requires_valid_identity(self):
        self.config["frp_backend_ssh_port"] = 2323
        self.assertIsNone(self.describe([tcp_service()], reveal=True)["services"][0]["command"])
        self.assertIsNotNone(self.describe([tcp_service(backend="127.0.0.1:2323")], reveal=True)["services"][0]["command"])
        for user in (None, "root", "$(touch /tmp/unsafe)", "a; echo unsafe", "-oProxyCommand=unsafe"):
            self.config["frp_login_user"] = user
            self.assertIsNone(self.describe([tcp_service(backend="127.0.0.1:2323")], reveal=True)["services"][0]["command"])

    def test_connection_arguments_do_not_accept_embedded_credentials_or_shell_code(self):
        for host in ("user:password@host.invalid", "host.invalid;id", "$(id)", "-oProxyCommand=id",
                     "https://host.invalid/path", "host.invalid\nsecret"):
            with self.subTest(host=host):
                self.config["frp_server_address"] = host
                result = self.describe([tcp_service()], reveal=True)
                self.assertIsNone(result["services"][0]["endpoint"])
                self.assertIsNone(result["services"][0]["command"])
                self.assertNotIn(host, json.dumps(result))

    def test_unprobed_is_not_green(self):
        result = self.describe()
        self.assertIsNone(result["ok"])
        self.assertFalse(result["checked"])
        self.assertEqual(result["services"][0]["health"], "not_checked")
        self.assertEqual(result["services"][0]["scope"], [])

    def test_missing_unknown_and_failed_checks_are_not_green(self):
        for records, expected in (([], "unknown"), ([check(state="unknown")], "unknown"),
                                  ([check(state="fail")], "fail"), ([check(state="skipped")], "unknown"),
                                  ([check(stage="unrecognized")], "unknown"),
                                  ([check(), check(state="fail", stage="backend")], "fail")):
            with self.subTest(records=records):
                result = self.describe(checks=records)
                self.assertFalse(result["ok"])
                self.assertEqual(result["services"][0]["health"], expected)

    def test_one_based_association_does_not_spill_to_unchecked_services(self):
        result = self.describe([http_service(), tcp_service()], checks=[check(2)])
        self.assertEqual([row["health"] for row in result["services"]], ["unknown", "ok"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["services"][1]["scope"], ["public_ingress"])
        result = self.describe([http_service(), tcp_service()], checks=[check(2, "fail"), check(1)])
        self.assertEqual([row["health"] for row in result["services"]], ["ok", "fail"])

    def test_global_checks_affect_overall_result_but_not_service_evidence(self):
        global_check = {"stage": "transport", "status": "unknown"}
        result = self.describe(checks={"ok": True, "checks": [check(), global_check]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["services"][0]["health"], "ok")
        result = self.describe(checks=[{"stage": "transport", "status": "ok"}])
        self.assertEqual(result["services"][0]["health"], "unknown")
        for bad_index in (0, 2, True, "1"):
            self.assertFalse(self.describe(checks=[check(), check(bad_index)])["ok"])

    def test_success_explicitly_limits_claim_to_check_scope(self):
        result = self.describe(checks=[check()])
        self.assertTrue(result["ok"])
        self.assertEqual(result["services"][0]["scope"], ["public_ingress"])

    def test_empty_service_inventory(self):
        result = status.describe_services(self.config, [])
        self.assertEqual(result["services"], [])
        self.assertIsNone(result["ok"])
        result = status.describe_services(self.config, [], checks=[])
        self.assertTrue(result["checked"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["services"], [])


if __name__ == "__main__":
    unittest.main()
