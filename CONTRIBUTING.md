# Contributing

Keep examples generic and deployment material outside the checkout. Read
[privacy](docs/privacy.md), use the README's generic Git identity when appropriate,
and do not commit field-test logs, cloud inventories or provider credentials.
Work on feature branches and integrate into `dev`; `main` and the annotated
`v0.1` release preserve the original completed SSH deployment.

## Local behavioral checks

Use a Linux virtual environment:

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook \
  -i ansible/inventory.example.yml ansible/deploy.yml --syntax-check
python scripts/check-privacy.py --history
```

These do not replace real deployment tests. Preserve host-key verification,
private credential permissions, no-proxy transport, listener isolation and
idempotence. The service model in `ansible/filter_plugins/frp_config.py` is shared
by Ansible and the CLI; do not introduce a second schema implementation.

## Full field regression

**Destructive: use disposable Ubuntu 24.04+ / systemd hosts only.** The suite
installs packages/Docker, creates test services and identities, changes trust
stores and hosts entries, deploys the real roles, and exercises failure/removal.
Never run it on production, a normal workstation or a shared server.

Install controller dependencies and a real browser:

```bash
python -m pip install -r scripts/fixtures/requirements.txt
python -m playwright install --with-deps chromium
```

The controller also needs curl, OpenSSL and OpenSSH; internal-CA testing needs
`certutil` from `libnss3-tools`. Prepare a private Ansible inventory with
`frp_servers` and `frp_clients`, and an SSH config containing pinned `relay` and
`origin` aliases with passwordless sudo. Create a non-root login account on the
origin. Permit relay inbound administration SSH, TCP 80/443/2222/2233.

```bash
python scripts/field-test.py \
  --inventory "$PRIVATE/inventory.yml" \
  --ssh-config "$PRIVATE/ssh_config" \
  --relay-ssh relay --client-ssh origin \
  --address 203.0.113.10 --domain field.example.invalid --user developer \
  --require-dns --public-tls --artifacts "$PRIVATE/test-logs"
```

Replace documentation-only values. For public ACME tests, all of `public`,
`basic`, `app`, `oidc`, `secure`, and `idp` under the selected domain must resolve
to the relay. Without `--public-tls`, the suite deliberately trusts Caddy's
internal CA for its isolated browser/test processes; public browser trust is a
separate property and must not be inferred from that mode.

Coverage includes:

- Real named TCP publications, SSH key/password policy, bidirectional 1 MiB
  payloads and a separate TCP echo service.
- Forwarding restrictions, loopback-only application/control/auth/status ports,
  and transport restart recovery.
- Caddy HTTPS, redirect, verified HTTPS backend identity, Basic allow/deny,
  application-owned authentication, unknown-host isolation and header spoofing.
- Real Dex authorization-code login and oauth2-proxy callback/token exchange,
  allowed/denied users, secure cookies, WebSockets and incremental streaming.
- Doctor's transport/registration/backend/ingress states under actual service
  failures; invalid config without deployed-file mutation; zero-change reapply.
- Removed listeners/authenticators, optional managed SSH disabled without login
  inputs, and surviving services after cleanup.

The default suite removes fixture services, temporary trust/hosts entries and
credentials, but leaves the final TCP-only project deployment on the disposable
hosts. Destroy the cloud resources afterward. `--keep-fixtures` instead restores
the full deployment on success and retains a private input directory for manual
browser inspection. Delete that directory and **both hosts** after inspection.
`--artifacts` stores failed subprocess details privately; they can contain secrets
and must never become CI/public artifacts. CI runs the same suite on its disposable
Ubuntu runner with explicit `--local` and internal-CA mode.

A smaller real-process FRP/OpenSSH test is available without applying roles:

```bash
python scripts/download-frp.py --destination /tmp/frp-test-binaries
sudo .venv/bin/python scripts/integration-test.py \
  --frps /tmp/frp-test-binaries/frps --frpc /tmp/frp-test-binaries/frpc \
  --user "$(id -un)"
```

It requires an existing non-root user, OpenSSH and `/run/sshd`. Prefer the full
field suite for role, web, authentication or lifecycle changes. Changes to pinned
binaries/checksums require checking upstream sources and deployment evidence,
not merely syntactic acceptance. Record only observed results in PRs; a network
failure from one client path is not evidence of either universal failure or
universal success.
