"""Workspace, publication, and failed-apply boundaries of the publishing CLI."""

import base64
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bridge", ROOT / "scripts/bridge.py")
assert SPEC is not None and SPEC.loader is not None
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


def web(name="demo", port=12000, access=None):
    return {"name": name, "protocol": "http", "backend": "http://127.0.0.1:3000",
            "relay_port": port, "hostname": name + ".example.invalid", "access": access or {"mode": "public"}}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bridge-regression-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.variables = {"frp_managed_ssh": False, "frp_server_address": "relay.example.invalid", "frp_services": []}
        self.pins = "relay.example.invalid ssh-ed25519 " + "A" * 48 + "\n"
        self.secrets: dict[str, Any] = {"frp_token": "ab" * 32, "frp_admin_password": "cd" * 32,
                                       "frp_relay_known_hosts": self.pins}
        self.save("services.yml", self.variables)
        self.save("secrets.yml", self.secrets)
        self.save("inventory.yml", {"all": {"children": {
            "frp_servers": {"hosts": {"relay": {"ansible_host": "relay.example.invalid"}}},
            "frp_clients": {"hosts": {"client": {"ansible_connection": "local"}}}}}})
        bridge.private_write(self.directory / "known_hosts", self.pins)
        bridge.private_write(self.directory / "admin.key", "private-fixture-key")
        self.save("controller.yml", {"admin_key": str(self.directory / "admin.key")})

    def save(self, name, value):
        bridge.private_write(self.directory / name, yaml.safe_dump(value, sort_keys=False))

    def invoke(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = bridge.main([*arguments, "--private-dir", str(self.directory), "--json"])
        return code, json.loads(output.getvalue()), output.getvalue() + errors.getvalue()

    def publish_args(self, *arguments):
        return bridge.parser().parse_args(["publish", "3000", "--name", "demo", *arguments])

    def save_baseline(self):
        previous = bridge.snapshot(bridge.load_workspace(self.directory))
        bridge.private_write(self.directory / "last-applied.json", json.dumps(previous))
        return previous

    def deployment_result(self, exit_code, *, inventory=None):
        if inventory is None:
            inventory = {"frp_servers": {"hosts": ["relay"]}, "frp_clients": {"hosts": ["client"]}}
        return [subprocess.CompletedProcess([], 0, json.dumps(inventory).encode(), b""),
                subprocess.CompletedProcess([], exit_code)]

    def test_publish_preview_does_not_save_or_expose_private_values(self):
        before = (self.directory / "services.yml").read_bytes()
        code, result, text = self.invoke("publish", "3000", "--name", "demo", "--hostname", "demo.example.invalid", "--access", "public", "--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(result["applied"])
        self.assertEqual(result["plan"]["counts"]["adds"], 1)
        self.assertEqual((self.directory / "services.yml").read_bytes(), before)
        self.assertFalse((self.directory / "apply-pending.json").exists())
        for private in ("demo.example.invalid", "relay.example.invalid", self.secrets["frp_token"], str(self.directory)):
            self.assertNotIn(private, text)

    def test_default_publish_and_remove_commit_the_desired_state(self):
        for arguments, expected_names in (
            (("publish", "3000", "--name", "demo", "--hostname", "demo.example.invalid", "--access", "public"), ["demo"]),
            (("remove", "demo"), []),
        ):
            with self.subTest(command=arguments[0]), \
                 patch.object(bridge.shutil, "which", return_value="/fixture/ansible"), \
                 patch.object(bridge.subprocess, "run", side_effect=self.deployment_result(0)):
                code, result, _ = self.invoke(*arguments)
            self.assertEqual(code, 0)
            self.assertTrue(result["applied"])
            workspace = bridge.load_workspace(self.directory)
            self.assertEqual([service["name"] for service in workspace["variables"]["frp_services"]], expected_names)
            self.assertEqual([service["name"] for service in workspace["previous"]["services"]], expected_names)
            self.assertFalse(workspace["uncertain"])

    def test_dry_runs_leave_entire_workspace_unchanged_without_controller(self):
        self.variables["frp_services"] = [web()]
        self.save("services.yml", self.variables)
        self.save_baseline()
        bridge.private_write(self.directory / "apply-pending.json", "{}")
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        for arguments in (("publish", "3001", "--name", "demo"), ("remove", "demo"), ("apply",)):
            with self.subTest(command=arguments[0]), patch.object(bridge.shutil, "which", return_value=None):
                code, result, _ = self.invoke(*arguments, "--dry-run", "--ask-become-pass")
            self.assertEqual(code, 0)
            self.assertFalse(result["applied"])
            self.assertEqual(result["plan"]["baseline"], "uncertain")
            self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_new_service_requires_access_but_update_preserves_existing_protection(self):
        code, result, _ = self.invoke("publish", "3000", "--hostname", "demo.example.invalid")
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])
        access = {"mode": "basic", "users": {"private-user": "$2b$12$" + "a" * 53}}
        self.variables["frp_services"] = [web(access=access)]
        self.save("services.yml", self.variables)
        workspace = bridge.load_workspace(self.directory)
        args = self.publish_args()
        args.target = "https://127.0.0.1:3443"
        changed = bridge.publish_variables(workspace, args)
        service = changed["frp_services"][0]
        self.assertEqual(service["access"], access)
        self.assertEqual(service["relay_port"], 12000)
        self.assertEqual(service["backend"], "https://127.0.0.1:3443")
        self.assertEqual(service["hostname"], "demo.example.invalid")
        self.assertEqual(workspace["variables"], self.variables)

    def test_allocator_avoids_control_tcp_and_oidc_listeners(self):
        self.variables.update(frp_server_port=12000, frp_relay_ssh_port=12001, frp_server_admin_port=12002)
        self.pins = "[relay.example.invalid]:12001 ssh-ed25519 " + "A" * 48 + "\n"
        self.secrets["frp_relay_known_hosts"] = self.pins
        self.secrets["frp_oidc"] = {"issuer_url": "https://identity.example.invalid", "client_id": "fixture",
                                   "client_secret": "private-provider-value", "cookie_secret": base64.urlsafe_b64encode(bytes(32)).decode()}
        self.variables["frp_services"] = [
            {"name": "tcp", "protocol": "tcp", "backend": "127.0.0.1:9999", "relay_port": 12003, "public_port": 12004},
            web("login", 12005, {"mode": "oidc", "port": 12006, "allowed_domains": ["example.invalid"]})]
        self.save("services.yml", self.variables)
        self.save("secrets.yml", self.secrets)
        bridge.private_write(self.directory / "known_hosts", self.pins)
        policy = self.directory / "policy.yml"
        self.save(policy.name, {"mode": "oidc", "allowed_domains": ["example.invalid"]})
        workspace = bridge.load_workspace(self.directory)
        changed = bridge.publish_variables(workspace, self.publish_args("--hostname", "demo.example.invalid", "--access-file", str(policy)))
        self.assertEqual(changed["frp_services"][-1]["relay_port"], 12007)
        self.assertEqual(changed["frp_services"][-1]["access"]["port"], 12008)
        self.assertEqual(len(bridge.snapshot(workspace, changed)["services"]), 3)

    def test_invalid_policy_or_backend_cannot_mutate_services(self):
        before = (self.directory / "services.yml").read_bytes()
        for target in ("0", "65536", "http://host:8080/private", "http://" + "user:secret@host:8080"):
            with self.subTest(target=target):
                code, result, _ = self.invoke("publish", target, "--hostname", "demo.example.invalid", "--access", "public")
                self.assertEqual(code, 2)
                self.assertFalse(result["ok"])
                self.assertEqual((self.directory / "services.yml").read_bytes(), before)
        policy = self.directory / "policy.yml"
        self.save(policy.name, {"mode": "oidc", "port": [], "allowed_domains": ["example.invalid"]})
        code, _, _ = self.invoke("publish", "3000", "--hostname", "demo.example.invalid", "--access-file", str(policy))
        self.assertEqual(code, 2)
        self.assertEqual((self.directory / "services.yml").read_bytes(), before)

    def test_remove_last_publication_is_previewed_without_deleting_infrastructure(self):
        self.variables["frp_services"] = [web()]
        self.save("services.yml", self.variables)
        previous = self.save_baseline()
        code, result, _ = self.invoke("remove", "1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(result["plan"]["impact"]["removes_last_publication"])
        self.assertTrue(result["plan"]["impact"]["closes_web_ingress"])
        self.assertEqual(bridge.load_workspace(self.directory)["variables"], self.variables)
        self.assertEqual(bridge.remove_variables(bridge.load_workspace(self.directory), "demo")["frp_services"], [])
        self.assertEqual(json.loads((self.directory / "last-applied.json").read_text()), previous)

    def test_failed_apply_retains_desired_revision_and_previous_baseline(self):
        previous = self.save_baseline()
        with patch.object(bridge.shutil, "which", return_value="/fixture/ansible"), \
             patch.object(bridge.subprocess, "run", side_effect=self.deployment_result(7)):
            code, result, text = self.invoke("publish", "3000", "--name", "demo", "--hostname", "demo.example.invalid", "--access", "public")
        self.assertEqual(code, 1)
        self.assertEqual(result["deployment"], "incomplete")
        self.assertTrue((self.directory / "apply-pending.json").exists())
        self.assertEqual(json.loads((self.directory / "last-applied.json").read_text()), previous)
        self.assertEqual(bridge.load_workspace(self.directory)["variables"]["frp_services"][0]["name"], "demo")
        self.assertEqual(self.invoke("plan")[1]["plan"]["baseline"], "uncertain")
        self.assertEqual((self.directory / "apply.log").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("demo.example.invalid", text)
        self.assertFalse(list(self.directory.glob(".bridge-apply-*")))

    def test_successful_retry_commits_baseline_and_clears_uncertainty(self):
        self.variables["frp_services"] = [web()]
        self.save("services.yml", self.variables)
        bridge.private_write(self.directory / "apply-pending.json", "{}")
        with patch.object(bridge.shutil, "which", return_value="/fixture/ansible"), \
             patch.object(bridge.subprocess, "run", side_effect=self.deployment_result(0)):
            code, result, _ = self.invoke("apply")
        self.assertEqual(code, 0)
        self.assertTrue(result["applied"])
        self.assertFalse((self.directory / "apply-pending.json").exists())
        baseline = json.loads((self.directory / "last-applied.json").read_text())
        self.assertEqual(baseline["services"][0]["name"], "demo")
        plan = self.invoke("plan")[1]["plan"]
        self.assertEqual(plan["baseline"], "known")
        self.assertFalse(plan["changed"])
        self.assertIsNone(result["discovery"]["ok"])
        self.assertFalse(result["discovery"]["checked"])

    def test_empty_inventory_cannot_be_recorded_as_successful_apply(self):
        before = (self.directory / "services.yml").read_bytes()
        with patch.object(bridge.shutil, "which", return_value="/fixture/ansible"), \
             patch.object(bridge.subprocess, "run", side_effect=self.deployment_result(0, inventory={})):
            code, result, _ = self.invoke("publish", "3000", "--hostname", "demo.example.invalid", "--access", "public")
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])
        self.assertFalse((self.directory / "last-applied.json").exists())
        self.assertFalse((self.directory / "apply-pending.json").exists())
        self.assertEqual((self.directory / "services.yml").read_bytes(), before)

    def test_unavailable_controller_cannot_save_a_publication(self):
        before = (self.directory / "services.yml").read_bytes()
        with patch.object(bridge.shutil, "which", return_value=None):
            code, _, _ = self.invoke("publish", "3000", "--hostname", "demo.example.invalid", "--access", "public")
        self.assertEqual(code, 2)
        self.assertFalse((self.directory / "apply-pending.json").exists())
        self.assertEqual((self.directory / "services.yml").read_bytes(), before)

    def test_workspace_lock_rejects_concurrent_apply_without_state_change(self):
        with bridge.workspace_lock(self.directory):
            code, result, _ = self.invoke("apply")
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])
        self.assertFalse((self.directory / "apply-pending.json").exists())

    def test_hidden_service_overlay_and_host_pin_mismatch_are_rejected(self):
        self.secrets["frp_services"] = [web()]
        self.save("secrets.yml", self.secrets)
        self.assertEqual(self.invoke("list")[0], 2)
        self.secrets.pop("frp_services")
        self.secrets["frp_relay_known_hosts"] = "different-private-pin"
        self.save("secrets.yml", self.secrets)
        code, _, text = self.invoke("plan")
        self.assertEqual(code, 2)
        self.assertNotIn("different-private-pin", text)

    def test_nonprivate_or_symlinked_input_is_not_consumed(self):
        secrets = self.directory / "secrets.yml"
        secrets.chmod(0o644)
        self.assertEqual(self.invoke("list")[0], 2)
        secrets.chmod(0o600)
        target = self.directory / "actual-secret.yml"
        secrets.rename(target)
        secrets.symlink_to(target)
        self.assertEqual(self.invoke("list")[0], 2)

    def test_list_is_unprobed_and_disclosure_is_explicit(self):
        self.variables["frp_services"] = [web()]
        self.save("services.yml", self.variables)
        code, result, text = self.invoke("list")
        self.assertEqual(code, 0)
        self.assertIsNone(result["ok"])
        self.assertEqual(result["services"][0]["health"], "not_checked")
        self.assertNotIn("demo.example.invalid", text)
        disclosed = self.invoke("list", "--show-endpoints")[1]
        self.assertEqual(disclosed["services"][0]["endpoint"], "https://demo.example.invalid/")

    @unittest.skipUnless(shutil.which("ssh-keygen"), "OpenSSH key generator required")
    def test_web_first_init_creates_idle_workspace_without_login_identity(self):
        destination = self.directory / "new-private"
        args = bridge.parser().parse_args(["init", "--private-dir", str(destination), "--relay", "relay.example.invalid",
                                           "--known-hosts", str(self.directory / "known_hosts"), "--admin-key", str(self.directory / "admin.key")])
        result = bridge.execute(args)
        self.assertTrue(result["initialized"])
        workspace = bridge.load_workspace(destination)
        self.assertFalse(workspace["variables"]["frp_managed_ssh"])
        self.assertEqual(bridge.snapshot(workspace)["services"], [])
        self.assertNotIn("frp_login_public_key", workspace["secrets"])
        self.assertFalse((destination / "login_id_ed25519").exists())
        self.assertFalse((destination / "last-applied.json").exists())
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in destination.iterdir()))
        previous = (destination / "secrets.yml").read_bytes()
        with self.assertRaises(ValueError):
            bridge.execute(args)
        self.assertEqual((destination / "secrets.yml").read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
