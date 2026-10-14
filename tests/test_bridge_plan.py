"""Pure deployment comparisons and their disclosure boundary."""

import base64
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


planner = load_module("bridge_plan", "scripts/bridge_plan.py")
model = load_module("plan_service_model", "ansible/filter_plugins/frp_config.py")
BCRYPT = "$2b$12$" + "a" * 53
OIDC = {
    "issuer_url": "https://identity.private.relay-ip.rbr.sh",
    "client_id": "private-client-identity",
    "client_secret": "private-oidc-secret",
    "cookie_secret": base64.urlsafe_b64encode(b"p" * 32).decode(),
}


def http_service(name="private-app", port=12080, mode="public"):
    access = {"mode": mode}
    if mode == "basic":
        access["users"] = {"private-user": BCRYPT}
    if mode == "oidc":
        access.update(port=port + 1000, allowed_emails=["private-user@identity.invalid"])
    return {"name": name, "protocol": "http", "backend": "http://127.0.0.1:18080",
            "relay_port": port, "hostname": name + ".relay-ip.rbr.sh", "access": access}


def tcp_service():
    return {"name": "private-tcp", "protocol": "tcp", "backend": "127.0.0.1:2222",
            "relay_port": 12222, "public_port": 2222}


def snapshot(services, **overrides):
    config = {"frp_token": "ab" * 32, "frp_oidc": deepcopy(OIDC),
              "frp_server_address": "private-relay.relay-ip.rbr.sh",
              "frp_admin_password": "private-admin-password", "frp_services": deepcopy(services)}
    config.update(deepcopy(overrides))
    normalized = model.normalize_services(services, oidc=config["frp_oidc"]) if services else []
    return {"config": config, "services": normalized,
            "manifest_token": model.revision_token(config["frp_token"], normalized, config["frp_oidc"]),
            "inventory_digest": "private-inventory-digest", "connection_digest": "private-connection-digest"}


class PlanTests(unittest.TestCase):
    def test_access_change_revokes_even_unchanged_service_connections(self):
        old = snapshot([http_service(mode="basic"), tcp_service()])
        new = snapshot([http_service(mode="public"), tcp_service()])
        plan = planner.build_plan(new, old)
        self.assertEqual(plan["counts"], {"adds": 0, "updates": 1, "removals": 0, "unchanged": 1})
        self.assertEqual(plan["changes"]["updates"][0]["policy_transition"],
                         {"from": "basic", "to": "public", "shared_policy_changed": False})
        self.assertTrue(plan["manifest_changed"])
        self.assertTrue(plan["impact"]["all_registrations_may_be_revoked"])

    def test_named_service_comparison_distinguishes_add_update_and_remove(self):
        old = snapshot([http_service("retired-private", 12082),
                        http_service("kept-private", 12083), tcp_service()])
        new = snapshot([http_service("added-private", 12084),
                        http_service("kept-private", 12083, "application"), tcp_service()])
        plan = planner.build_plan(new, old, reveal=True)
        self.assertEqual(plan["counts"], {"adds": 1, "updates": 1, "removals": 1, "unchanged": 1})
        for category, name in (("adds", "added-private"), ("updates", "kept-private"),
                               ("removals", "retired-private"), ("unchanged", "private-tcp")):
            self.assertEqual(plan["changes"][category][0]["endpoints"]["name"], name)

    def test_shared_oidc_change_marks_unchanged_service_policy(self):
        service = http_service(mode="oidc")
        old = snapshot([service, tcp_service()])
        oidc = {**OIDC, "client_secret": "rotated-private-client-secret"}
        new = snapshot([service, tcp_service()], frp_oidc=oidc)
        plan = planner.build_plan(new, old)
        self.assertEqual(plan["counts"]["unchanged"], 2)
        policy = plan["changes"]["unchanged"][0]["policy_transition"]
        self.assertEqual(policy, {"from": "oidc", "to": "oidc", "shared_policy_changed": True})
        self.assertNotIn("policy_transition", plan["changes"]["unchanged"][1])
        self.assertTrue(plan["impact"]["all_registrations_may_be_revoked"])

    def test_same_mode_identity_policy_change_is_visible_without_identity(self):
        old_service = http_service(mode="oidc")
        new_service = deepcopy(old_service)
        new_service["access"]["allowed_emails"] = ["replacement-private-user@identity.invalid"]
        plan = planner.build_plan(snapshot([new_service]), snapshot([old_service]))
        self.assertEqual(plan["counts"]["updates"], 1)
        text = planner.format_plan(plan)
        self.assertEqual(plan["changes"]["updates"][0]["policy_transition"],
                         {"from": "oidc", "to": "oidc", "shared_policy_changed": False})
        self.assertNotIn("replacement-private-user", text)

    def test_last_web_removal_preserves_tcp_and_reports_ingress_closure(self):
        plan = planner.build_plan(snapshot([tcp_service()]), snapshot([http_service(), tcp_service()]))
        self.assertEqual(plan["counts"]["removals"], 1)
        self.assertEqual(plan["counts"]["unchanged"], 1)
        self.assertTrue(plan["impact"]["closes_web_ingress"])
        self.assertFalse(plan["impact"]["removes_last_publication"])

    def test_removing_final_publication_reports_last_removal(self):
        for service in (http_service(), tcp_service()):
            with self.subTest(protocol=service["protocol"]):
                plan = planner.build_plan(snapshot([]), snapshot([service]))
                self.assertTrue(plan["impact"]["removes_last_publication"])
                self.assertEqual(plan["counts"]["removals"], 1)

    def test_service_and_mapping_order_are_irrelevant(self):
        services = [http_service(), tcp_service()]
        old = snapshot(services)
        new = snapshot([dict(reversed(list(service.items()))) for service in reversed(services)])
        plan = planner.build_plan(new, old)
        self.assertFalse(plan["changed"])
        self.assertFalse(plan["manifest_changed"])
        original_plan = planner.build_plan(old, old)
        self.assertEqual(plan["counts"], original_plan["counts"])
        self.assertEqual(plan["impact"], original_plan["impact"])
        self.assertEqual(plan["infrastructure_changes"], original_plan["infrastructure_changes"])
        self.assertEqual(plan["counts"]["unchanged"], 2)
        self.assertFalse(plan["live_state_verified"])
        self.assertTrue(plan["impact"]["restart_risk"])

    def test_infrastructure_changes_warn_even_when_manifest_unchanged(self):
        old = snapshot([http_service()])
        for field in ("inventory_digest", "connection_digest", "config"):
            with self.subTest(field=field):
                new = deepcopy(old)
                if field == "config":
                    new[field]["frp_admin_password"] = "rotated-admin-secret"
                else:
                    new[field] = "changed-private-digest"
                plan = planner.build_plan(new, old)
                self.assertTrue(plan["changed"])
                self.assertFalse(plan["manifest_changed"])
                self.assertTrue(plan["impact"]["restart_risk"])
                self.assertEqual(plan["counts"]["unchanged"], 1)
                self.assertNotIn("rotated-admin-secret", json.dumps(plan))
                self.assertNotIn("changed-private-digest", json.dumps(plan))

    def test_unknown_and_partial_baselines_do_not_invent_removals(self):
        desired = snapshot([http_service()])
        partial = snapshot([tcp_service()])
        del partial["connection_digest"]
        for previous in (None, {}, partial):
            with self.subTest(previous=previous is None):
                plan = planner.build_plan(desired, previous)
                self.assertEqual(plan["baseline"], "unknown")
                self.assertEqual(plan["comparison"], "none")
                self.assertIsNone(plan["changed"])
                self.assertIsNone(plan["changes"]["removals"])
                self.assertIsNone(plan["counts"]["removals"])
                self.assertIsNone(plan["impact"]["closes_web_ingress"])
                self.assertFalse(plan["live_state_verified"])
                self.assertTrue(plan["impact"]["restart_risk"])

    def test_uncertain_apply_uses_only_historical_removal_candidates(self):
        previous = snapshot([http_service()])
        plan = planner.build_plan(snapshot([]), previous, uncertain=True)
        self.assertEqual(plan["baseline"], "uncertain")
        self.assertEqual(plan["comparison"], "last_successful_apply")
        self.assertEqual(plan["counts"]["removals"], 1)
        self.assertIsNone(plan["impact"]["removes_last_publication"])
        self.assertIsNone(plan["impact"]["closes_web_ingress"])
        self.assertEqual(plan["changes"]["removals"][0]["prior_index"], 1)
        unknown = planner.build_plan(snapshot([]), uncertain=True)
        self.assertEqual(unknown["baseline"], "uncertain")
        self.assertIsNone(unknown["changes"]["removals"])

    def test_uncertain_unchanged_input_is_not_harmless(self):
        state = snapshot([http_service()])
        plan = planner.build_plan(state, state, uncertain=True)
        self.assertFalse(plan["changed"])
        self.assertFalse(plan["manifest_changed"])
        self.assertTrue(plan["impact"]["all_registrations_may_be_revoked"])
        self.assertEqual(plan["baseline"], "uncertain")
        self.assertTrue(plan["impact"]["restart_risk"])
        self.assertFalse(plan["live_state_verified"])

    def test_default_redaction_and_explicit_disclosure_never_include_secrets(self):
        services = [http_service(mode="basic"), http_service("private-login", 12081, "oidc")]
        old = snapshot(services)
        new = snapshot(services, frp_token="cd" * 32,
                       frp_oidc={**OIDC, "client_secret": "rotated-private-secret"})
        private_values = [BCRYPT, "private-user", "private-user@identity.invalid",
                          OIDC["issuer_url"], OIDC["client_id"], OIDC["client_secret"],
                          OIDC["cookie_secret"], "rotated-private-secret", "private-admin-password",
                          old["manifest_token"], new["manifest_token"], "ab" * 32, "cd" * 32,
                          "private-inventory-digest", "private-connection-digest"]
        endpoints = ["private-app", "private-login", "http://127.0.0.1:18080", "12080", "12081"]
        for reveal in (False, True):
            with self.subTest(reveal=reveal):
                plan = planner.build_plan(new, old, reveal=reveal)
                self.assertEqual(json.loads(json.dumps(plan)), plan)
                for text in (json.dumps(plan), planner.format_plan(plan)):
                    for value in private_values:
                        self.assertNotIn(value, text)
                    for value in endpoints:
                        if reveal:
                            self.assertIn(value, text)
                        else:
                            self.assertNotIn(value, text)
                self.assertTrue(plan["manifest_changed"])
                self.assertTrue(plan["impact"]["all_registrations_may_be_revoked"])

    def test_policy_password_rotation_shows_change_but_never_hash(self):
        before = http_service(mode="basic")
        after = deepcopy(before)
        rotated_hash = "$2b$12$" + "b" * 53
        after["access"]["users"]["private-user"] = rotated_hash
        for reveal in (False, True):
            plan = planner.build_plan(snapshot([after]), snapshot([before]), reveal=reveal)
            self.assertEqual(plan["counts"]["updates"], 1)
            self.assertTrue(plan["manifest_changed"])
            self.assertEqual(plan["changes"]["updates"][0]["policy_transition"],
                             {"from": "basic", "to": "basic", "shared_policy_changed": False})
            self.assertNotIn(rotated_hash, json.dumps(plan))
            self.assertNotIn(BCRYPT, json.dumps(plan))

    def test_compare_and_format_are_pure_and_service_indexes_are_numeric(self):
        old, new = snapshot([http_service(), tcp_service()]), snapshot([http_service(mode="application")])
        original = deepcopy((old, new))
        plan = planner.build_plan(new, old, reveal=True)
        before_format = deepcopy(plan)
        planner.format_plan(plan)
        self.assertEqual((old, new), original)
        self.assertEqual(plan, before_format)
        entries = [entry for group in plan["changes"].values() for entry in group]
        self.assertTrue(all(isinstance(entry["index"], int) for entry in entries))

    def test_current_indexes_follow_desired_order_and_removals_use_prior_indexes(self):
        old_services = [http_service("z-retired", 12080), tcp_service(),
                        http_service("a-kept", 12081, "basic")]
        new_services = [http_service("z-added", 12082), http_service("a-kept", 12081, "public"),
                        tcp_service()]
        plan = planner.build_plan(snapshot(new_services), snapshot(old_services), reveal=True)
        current = [entry for category in ("adds", "updates", "unchanged")
                   for entry in plan["changes"][category]]
        self.assertEqual({entry["endpoints"]["name"]: entry["index"] for entry in current},
                         {"z-added": 1, "a-kept": 2, "private-tcp": 3})
        for entry in current:
            self.assertNotIn("prior_index", entry)
            self.assertEqual(new_services[entry["index"] - 1]["name"], entry["endpoints"]["name"])
        removed = plan["changes"]["removals"][0]
        self.assertEqual(removed["endpoints"]["name"], "z-retired")
        self.assertEqual(removed["prior_index"], 1)
        self.assertEqual(removed["index"], removed["prior_index"])
        self.assertEqual(old_services[removed["prior_index"] - 1]["name"], removed["endpoints"]["name"])


if __name__ == "__main__":
    unittest.main()
