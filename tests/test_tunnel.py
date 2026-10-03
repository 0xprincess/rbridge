"""Behavioral boundaries for the operator CLI; field tests cover installed FRP."""

import argparse
from contextlib import contextmanager, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tunnel = load_script("tunnel")
generator = load_script("create-secrets")
prepare = load_script("prepare-ci-deployment")


def config():
    return {"frp_managed_ssh": False, "frp_server_address": "relay.example.invalid",
            "frp_services": [{"name": "app", "protocol": "http", "backend": "http://127.0.0.1:18080",
                              "relay_port": 12080, "hostname": "app.example.invalid", "access": {"mode": "public"}}]}


@contextmanager
def http_server(code=200, body=b"ok", headers=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Host"), self.headers.get("Authorization")))
            self.send_response(code)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ConfigTests(unittest.TestCase):
    def test_validate_uses_shared_model_and_reports_field_error(self):
        value = config()
        value["frp_services"][0]["relay_port"] = 17401
        with self.assertRaises(ValueError):
            tunnel.validate_config(value)

    def test_client_backend_and_relay_public_ports_are_distinct_namespaces(self):
        value = {"frp_services": [{"name": "ssh", "protocol": "tcp", "backend": "127.0.0.1:2222", "relay_port": 12222, "public_port": 2222}]}
        self.assertEqual(tunnel.validate_config(value)[0]["backend_port"], 2222)

    def test_disabled_ssh_does_not_reserve_client_backend_port(self):
        value = config()
        value.update(frp_backend_ssh_port=17400)
        self.assertEqual(len(tunnel.validate_config(value)), 1)
        value["frp_managed_ssh"] = True
        with self.assertRaises(ValueError):
            tunnel.validate_config(value)

    def test_yaml_errors_do_not_expose_source(self):
        private = "private-content-not-for-diagnostics"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yml"
            path.write_text("value: [" + private)
            with self.assertRaises(ValueError) as caught:
                tunnel.load_config([path])
            self.assertNotIn(private, str(caught.exception))
            self.assertNotIn(folder, str(caught.exception))

    def test_overlays_and_inventory_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder) / "first.yml", Path(folder) / "second.yml"
            first.write_text(yaml.safe_dump(config()))
            second.write_text("frp_managed_ssh: true\n")
            self.assertTrue(tunnel.load_config([first, second])["frp_managed_ssh"])
            second.write_text("all: {vars: {}}\n")
            with self.assertRaises(ValueError):
                tunnel.load_config([second])

    def test_cli_validation_error_is_structured_and_nonzero(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "services.yml"
            value = config()
            value["frp_services"][0]["access"] = {}
            path.write_text(yaml.safe_dump(value))
            output = io.StringIO()
            with redirect_stdout(output):
                status = tunnel.main(["validate", "--config", str(path), "--json"])
            self.assertEqual(status, 2)
            self.assertFalse(json.loads(output.getvalue())["ok"])
            self.assertNotIn("app.example.invalid", output.getvalue())

    def test_admin_config_never_sends_credentials_to_remote_host(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "frpc.toml"
            path.write_text('[webServer]\naddr = "203.0.113.10"\nport = 17400\nuser = "operator"\npassword = "private-test-value"\n')
            with self.assertRaises(ValueError) as caught:
                tunnel.admin_settings({}, "client", path)
            self.assertNotIn("private-test-value", str(caught.exception))

    def test_installed_admin_settings_take_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "frps.toml"
            path.write_text('webServer.addr = "127.0.0.1"\nwebServer.port = 18001\nwebServer.user = "operator"\nwebServer.password = "private-test-value"\n')
            self.assertEqual(tunnel.admin_settings({"frp_admin_password": "wrong"}, "server", path)["port"], 18001)


class HTTPTests(unittest.TestCase):
    def test_http_success_requires_response_not_only_tcp(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        try:
            result = tunnel.probe({"kind": "http", "host": "127.0.0.1", "port": listener.getsockname()[1]}, 0.2)
            self.assertIn("error", result)
        finally:
            listener.close()

    def test_real_http_response_status_is_checked(self):
        for code, expected in ((200, "ok"), (503, "fail")):
            with self.subTest(code=code), http_server(code) as (port, _):
                result = tunnel.endpoint_check({"kind": "http", "host": "127.0.0.1", "port": port}, "backend", 1, 2)
                self.assertEqual(result["status"], expected)

    def test_no_redirect_following_or_environment_proxy(self):
        with http_server(302, headers={"Location": "http://redirect.example.invalid/"}) as (port, requests):
            with patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.example.invalid:9999"}):
                result = tunnel.http_request("127.0.0.1", port, "http", 1)
            self.assertEqual(result["code"], 302)
            self.assertEqual(len(requests), 1)

    def test_host_header_preserved_while_connecting_to_relay(self):
        with http_server() as (port, requests):
            tunnel.http_request("backend.example.invalid", port, "http", 1, connect_host="127.0.0.1", headers={"Host": "app.example.invalid"})
            self.assertEqual(requests[0][1], "app.example.invalid")

    def test_api_requires_authenticated_200_and_parses_real_body(self):
        body = json.dumps({"tcp": [{"name": "app", "status": "running", "err": ""}]}).encode()
        with http_server(body=body) as (port, requests):
            response = tunnel.http_request("127.0.0.1", port, "http", 1, path="/api/status", headers={"Authorization": "Basic test"}, json_body=True)
            self.assertEqual(tunnel.registered_names(response, "client"), {"app"})
            self.assertEqual(requests[0][0], "/api/status")
        self.assertIsNone(tunnel.registered_names({"code": 401, "body": {"tcp": []}}, "client"))

    def test_basic_challenge_and_oidc_redirect_are_not_plain_success(self):
        with patch.object(tunnel, "probe", return_value={"code": 200}):
            for mode in ("basic", "oidc"):
                self.assertEqual(tunnel.endpoint_check({"kind": "http"}, "public_ingress", 1, 1, mode)["status"], "fail")
        with patch.object(tunnel, "probe", return_value={"code": 401, "basic_challenge": True}):
            self.assertEqual(tunnel.endpoint_check({"kind": "http"}, "public_ingress", 1, 1, "basic")["status"], "ok")
        with patch.object(tunnel, "probe", return_value={"code": 302, "redirect": False}):
            self.assertEqual(tunnel.endpoint_check({"kind": "http"}, "public_ingress", 1, 1, "oidc")["status"], "fail")

    def test_process_deadline_bounds_dns_and_slow_response(self):
        def slow_probe(*args):
            time.sleep(10)
        start = time.monotonic()
        with patch.object(tunnel, "_probe", side_effect=slow_probe):
            self.assertEqual(tunnel.probe({"kind": "http"}, 0.1), {"error": "timeout"})
        self.assertLess(time.monotonic() - start, 3)


class DoctorTests(unittest.TestCase):
    def diagnose(self, responses, state="active", role="client"):
        value = config()
        with patch.object(tunnel, "admin_settings", return_value={"addr": "127.0.0.1", "port": 17400, "user": "operator", "password": "private-test-value"}), patch.object(tunnel, "transport_state", return_value=state), patch.object(tunnel, "probe", side_effect=responses):
            return tunnel.doctor(value, tunnel.validate_config(value), role)

    def test_active_process_does_not_prove_registration_or_transport(self):
        result = self.diagnose([{"code": 200, "body": {"tcp": []}}, {"code": 200}, {"code": 200}])
        self.assertFalse(result["ok"])
        self.assertEqual([entry["status"] for entry in result["checks"]], ["unknown", "fail", "ok", "ok"])

    def test_stopped_transport_has_its_own_failure(self):
        result = self.diagnose([{"error": "connection"}, {"code": 200}, {"error": "connection"}], "stopped")
        self.assertEqual(result["checks"][0]["stage"], "transport")
        self.assertEqual(result["checks"][0]["status"], "fail")

    def test_backend_failure_is_not_hidden_by_registration(self):
        result = self.diagnose([{"code": 200, "body": {"tcp": [{"name": "app", "status": "running"}]}}, {"error": "connection"}, {"code": 502}])
        self.assertEqual([entry["status"] for entry in result["checks"]], ["ok", "ok", "fail", "fail"])

    def test_ingress_failure_is_distinct_from_healthy_backend(self):
        result = self.diagnose([{"code": 200, "body": {"tcp": [{"name": "app", "status": "running"}]}}, {"code": 200}, {"error": "tls"}])
        self.assertEqual([entry["status"] for entry in result["checks"]], ["ok", "ok", "ok", "fail"])

    def test_server_online_status_uses_server_api_shape(self):
        result = self.diagnose([{"code": 200, "body": {"proxies": [{"name": "app", "status": "online"}]}}, {"code": 200}, {"code": 200}], role="server")
        self.assertTrue(result["ok"])

    def test_private_api_errors_and_endpoint_values_are_not_printed(self):
        private = "sensitive-backend-error"
        result = self.diagnose([{"code": 200, "body": {"tcp": [{"name": "app", "status": "start error", "err": private}]}}, {"error": "connection"}, {"error": "connection"}])
        output = json.dumps(result)
        for secret in (private, "private-test-value", "app.example.invalid", "relay.example.invalid"):
            self.assertNotIn(secret, output)


class GenerationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ssh-keygen"), "OpenSSH tools are required")
    def test_non_ssh_generation_has_no_login_identity_and_editable_service_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pins = root / "pins"
            pins.write_text("relay.example.invalid ssh-" + "ed25519 " + "A" * 48 + "\n")
            services = root / "services.yml"
            services.write_text(yaml.safe_dump(config()))
            destination = root / "private"
            args = argparse.Namespace(output=destination, server_host="relay.example.invalid", client_user=None,
                                      relay_port=22, known_hosts=pins, admin_user="root", no_managed_ssh=True, services_file=services)
            previous_umask = os.umask(0o077)
            try:
                with redirect_stdout(io.StringIO()):
                    generator.generate(args)
            finally:
                os.umask(previous_umask)
            self.assertFalse((destination / "login_id_ed25519").exists())
            generated = yaml.safe_load((destination / "services.yml").read_text())
            self.assertFalse(generated["frp_managed_ssh"])
            self.assertNotIn("frp_login_user", generated)
            self.assertEqual(len(tunnel.validate_config(generated)), 1)
            private = yaml.safe_load((destination / "secrets.yml").read_text())
            self.assertNotIn("frp_login_public_key", private)
            self.assertEqual(len(private["frp_admin_password"]), 64)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in destination.iterdir()))

    def test_ci_rejects_invalid_services_before_creating_private_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            environment = {"RUNNER_TEMP": folder, "DEPLOY_PRIVATE_KEY": "test-value", "DEPLOY_KNOWN_HOSTS": "test-value",
                           "DEPLOY_INVENTORY": "all: {}", "TUNNEL_SECRETS": "frp_token: test-value", "TUNNEL_SERVICES": "frp_services: []"}
            with patch.dict(os.environ, environment, clear=True), redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError):
                    prepare.main()
            self.assertFalse((Path(folder) / "frp-tunnel").exists())


if __name__ == "__main__":
    unittest.main()
