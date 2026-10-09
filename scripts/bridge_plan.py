"""Pure, redacted deployment impact previews against the last successful apply."""


_SNAPSHOT_FIELDS = ("config", "services", "manifest_token", "inventory_digest", "connection_digest")
_ENDPOINT_FIELDS = ("name", "backend", "hostname", "relay_port", "public_port")


def _complete_snapshot(snapshot):
    return (
        isinstance(snapshot, dict)
        and all(key in snapshot for key in _SNAPSHOT_FIELDS)
        and isinstance(snapshot["config"], dict)
        and isinstance(snapshot["services"], list)
        and all(isinstance(snapshot[key], str) and snapshot[key]
                for key in _SNAPSHOT_FIELDS[2:])
    )


def _access_mode(service):
    return service.get("access", {}).get("mode", "tcp") if service else None


def _config_without_services(snapshot):
    # The shared model's normalized services are authoritative; raw inventory
    # order and spelling must not create a second service-diff convention.
    return {key: value for key, value in snapshot["config"].items() if key != "frp_services"}


def _entry(index, service, before=None, *, reveal=False, shared_policy_changed=False):
    entry = {"index": index, "protocol": service["protocol"], "access_mode": _access_mode(service)}
    if before is not None and (
        before.get("access") != service.get("access") or shared_policy_changed
    ):
        entry["policy_transition"] = {
            "from": _access_mode(before),
            "to": _access_mode(service),
            "shared_policy_changed": shared_policy_changed,
        }
    if reveal:
        # Never copy access dictionaries, config, tokens, identities or digests.
        entry["endpoints"] = {key: service[key] for key in _ENDPOINT_FIELDS if key in service}
    return entry


def build_plan(desired, previous=None, *, uncertain=False, reveal=False):
    """Compare validated private snapshots without probing or mutating either.

    Changes describe desired configuration versus the last successful CLI apply,
    never observed live resources. An incomplete prior snapshot is not a usable
    baseline. With an uncertain apply, removal entries are only candidates from
    that historical comparison; other live publications may exist.

    Wanted entries use one-based indexes from the desired service list.
    Removal entries carry their historical index and an explicit prior_index;
    those numbers refer only to the last successful apply, not the current list.
    """
    if not _complete_snapshot(desired):
        raise ValueError("Desired deployment snapshot is incomplete.")
    if previous is None:
        previous = {}
    known = _complete_snapshot(previous)
    baseline = "uncertain" if uncertain else "known" if known else "unknown"
    wanted = {service["name"]: service for service in desired["services"]}
    old = {service["name"]: service for service in previous["services"]} if known else {}
    indexes = {name: index for index, name in enumerate(wanted, 1)}
    shared_policy_changed = known and desired["config"].get("frp_oidc") != previous["config"].get("frp_oidc")
    changes = {"adds": [], "updates": [], "removals": [] if known else None, "unchanged": []}
    for name in wanted:
        service, before = wanted[name], old.get(name)
        category = "adds" if before is None else "unchanged" if before == service else "updates"
        changes[category].append(_entry(
            indexes[name], service, before, reveal=reveal,
            shared_policy_changed=bool(shared_policy_changed and (
                _access_mode(service) == "oidc" or _access_mode(before) == "oidc"
            )),
        ))
    if known:
        changes["removals"] = [
            {**_entry(index, service, reveal=reveal), "prior_index": index}
            for index, (name, service) in enumerate(old.items(), 1) if name not in wanted
        ]
    infrastructure = {
        "inventory": desired["inventory_digest"] != previous["inventory_digest"] if known else None,
        "connection": desired["connection_digest"] != previous["connection_digest"] if known else None,
        "config": _config_without_services(desired) != _config_without_services(previous) if known else None,
    }
    manifest_changed = desired["manifest_token"] != previous["manifest_token"] if known else None
    changed = bool(manifest_changed or any(infrastructure.values()) or changes["adds"]
                   or changes["updates"] or changes["removals"]) if known else None
    has_web = any(service["protocol"] == "http" for service in wanted.values())
    had_web = any(service["protocol"] == "http" for service in old.values())
    # These describe a transition only when the baseline is trustworthy, not
    # an assertion that a particular listener currently exists on either host.
    closes_web = bool(had_web and not has_web) if known and not uncertain else None
    removes_last = bool(old and not wanted) if known and not uncertain else None
    warnings = [
        "Baseline records only the last successful CLI apply, not live state; live drift is unverified.",
    ]
    if not known:
        warnings.append("No complete baseline: listed additions are desired publications, not proven new resources; live removals and deployment impact are unknown.")
    if uncertain:
        warnings.append("An incomplete or uncertain apply may have changed either host. Historical changes and removal candidates are not an exact live-state diff; inspect both hosts before retrying.")
    if known and not changed:
        warnings.append("Desired input matches the last successful apply; live drift is unverified and applying may still restart services or interrupt traffic.")
    if manifest_changed:
        warnings.append("Manifest authentication changes can revoke ALL existing registrations and connections, including unchanged services; clients must reconnect with the new revision.")
    elif not known or uncertain:
        warnings.append("Existing registrations and connections may be revoked; downtime cannot be ruled out.")
    if any(infrastructure.values()):
        warnings.append("Inventory, controller connection, or other configuration changed: conservatively expect possible restarts and traffic interruption even with an unchanged service manifest.")
    warnings.extend([
        "Rollout is fail-closed: old registrations or ingress may stop before replacement services are ready; failure can leave publication unavailable. No zero-downtime guarantee.",
        "Services persist through enabled systemd units on the relay and Linux/WSL client, but the backend must keep running. WSL must be running and the machine awake; Windows wake/logon startup is not configured automatically.",
        "No automatic cloud provisioning, DNS records, or cloud/host firewall management: supply reachable hosts and configure these yourself.",
    ])
    if not has_web:
        warnings.append("Desired state has no HTTP publications: applying closes managed web ingress, not arbitrary externally managed listeners.")
    if not wanted:
        warnings.append("Removing the last publication leaves infrastructure, credentials, and certificates in place; full uninstall and credential revocation are manual.")
    else:
        warnings.append("Removal withdraws managed publication, not the backend application. Removing the last HTTP publication closes managed web ingress; removing all publications leaves infrastructure, credentials, and certificates. Full uninstall is manual.")
    actions = [
        "Review rbridge plan before rbridge apply; use --show-endpoints only for deliberate local endpoint disclosure.",
        "After apply, run rbridge status and test access from an external network, including denied users for protected HTTP services.",
        "Keep the backend and relay/client units running; arrange WSL wake/logon startup if required.",
    ]
    if uncertain or not known:
        actions.insert(0, "Inspect actual relay/client services and reconcile any incomplete deployment before applying; do not treat the saved baseline as live state.")
    if any(infrastructure.values()):
        actions.append("If inventory or connection targets changed, inspect the former hosts too; applying to new targets does not uninstall the old deployment.")
    if not wanted:
        actions.append("For full uninstall, manually stop/remove managed units and listeners on both hosts, remove retained credentials/certificates, and clean up external infrastructure, DNS, and firewall rules.")
    return {
        "baseline": baseline,
        "comparison": "last_successful_apply" if known else "none",
        "live_state_verified": False,
        "changed": changed,
        "changes": changes,
        "counts": {key: len(value) if value is not None else None for key, value in changes.items()},
        "manifest_changed": manifest_changed,
        "infrastructure_changes": infrastructure,
        "impact": {
            "all_registrations_may_be_revoked": True if uncertain or not known else manifest_changed,
            "restart_risk": True,
            "closes_web_ingress": closes_web,
            "removes_last_publication": removes_last,
        },
        "warnings": warnings,
        "next_actions": actions,
    }


def format_plan(plan):
    """Render a build_plan result; only explicitly disclosed endpoints appear."""
    lines = ["Deployment impact preview", "Baseline: " + plan["baseline"] +
             " (comparison: " + plan["comparison"] + "; live state unverified)"]
    labels = {"adds": "Add", "updates": "Update", "removals": "Remove", "unchanged": "Unchanged"}
    for category, label in labels.items():
        entries = plan["changes"][category]
        if entries is None:
            lines.append(label + ": unknown (no complete baseline)")
            continue
        qualifier = " (historical candidates only)" if category == "removals" and plan["baseline"] == "uncertain" else ""
        if category == "adds" and plan["comparison"] == "none":
            qualifier = " (desired publications; existing state unknown)"
        lines.append(f"{label}: {len(entries)}{qualifier}")
        for entry in entries:
            line = f"  Service {entry['index']}: {entry['protocol']}, access={entry['access_mode']}"
            if "prior_index" in entry:
                line += "; index from last successful apply, not the current service list"
            transition = entry.get("policy_transition")
            if transition:
                line += f"; policy {transition['from']} -> {transition['to']}"
                if transition["from"] == transition["to"]:
                    line += " (policy details changed)"
                if transition["shared_policy_changed"]:
                    line += " (shared identity-provider settings changed)"
            # Only build_plan's disclosure allowlist can reach this field.
            if "endpoints" in entry:
                line += "; " + ", ".join(f"{key}={value}" for key, value in entry["endpoints"].items())
            lines.append(line)
    lines.append("Warnings:")
    lines.extend("  - " + warning for warning in plan["warnings"])
    lines.append("Next actions:")
    lines.extend("  - " + action for action in plan["next_actions"])
    return "\n".join(lines)
