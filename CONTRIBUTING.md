# Contributing

Keep examples generic and deployment material outside the checkout. Read
[privacy](docs/privacy.md), use the README's generic Git identity when appropriate,
and do not commit field-test logs, cloud inventories or provider credentials.
Work on feature branches and merge completed changes into `main`. The annotated
`v0.1` release preserves the original completed SSH deployment.

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

Workspace regressions cover web-first initialization, non-mutating publication
previews, internal-port allocation, policy preservation, redaction, apply failure
state and retry, final-publication removal, and scoped discovery health. Unit
tests isolate Ansible outcomes; exercise the real controller and FRP processes
when changing these paths. Never run the provisioning roles on a normal
workstation merely to smoke the CLI.

## Full field regression

**Destructive: use disposable systemd hosts from a supported distribution family
only** (see [supported setup](docs/deployment.md#supported-setup)). The suite
installs packages/Docker, creates test services and identities, changes trust
stores and hosts entries, deploys the real roles, and exercises failure/removal.
Never run it on production, a normal workstation or a shared server. RHEL-family
origins have no distribution Docker package (Fedora's is `moby-engine`): install
Docker there first, and enable EPEL for the origin fixture's `python3-aiohttp`.

Install controller dependencies and a real browser:

```bash
python -m pip install -r scripts/fixtures/requirements.txt
python -m playwright install --with-deps chromium
```

The controller also needs curl, OpenSSL and OpenSSH; internal-CA testing needs
NSS `certutil` (`libnss3-tools` on Debian/Ubuntu, `nss-tools` on Fedora/RHEL,
`mozilla-nss-tools` on openSUSE, `nss` on Arch). Prepare a private Ansible inventory with
`frp_servers` and `frp_clients`, and an SSH config containing pinned `relay` and
`origin` aliases with passwordless sudo. Create a non-root login account on the
origin. Permit relay inbound administration SSH, TCP 80/443/2222/2233.

```bash
python scripts/field-test.py \
  --inventory "$PRIVATE/inventory.yml" \
  --ssh-config "$PRIVATE/ssh_config" \
  --relay-ssh relay --client-ssh origin \
  --address <relay-ip> --domain field.<relay-ip>.rbr.sh --user developer \
  --require-dns --public-tls --artifacts "$PRIVATE/test-logs"
```

Replace documentation-only values. For public ACME tests, all of `public`,
`basic`, `app`, `oidc`, `secure`, and `idp` under the selected domain must resolve
to the relay: `field.<relay-ip>.rbr.sh` does so through the `rbr.sh` nip.io alias
without DNS records, or use a domain you control, such as `field.example.com`,
with matching A records. Without `--public-tls`, the suite deliberately trusts Caddy's
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
  inputs, surviving services after cleanup, final-publication removal to an
  idle infrastructure-only revision, and successful re-publication.

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

It requires an existing non-root user and OpenSSH (Debian-family sshd also needs
`/run/sshd`). Prefer the full
field suite for role, web, authentication or lifecycle changes. Changes to pinned
binaries/checksums require checking upstream sources and deployment evidence,
not merely syntactic acceptance. Record only observed results in PRs; a network
failure from one client path is not evidence of either universal failure or
universal success.

To check the roles on another distribution without a VPS, deploy them into a
disposable privileged systemd container (Docker required; nothing on the host
changes besides the container and a temporary directory):

```bash
python scripts/distro-test.py --image fedora:latest
```

It deploys relay and origin into one container twice, requires the second run to
change nothing, then checks native SSH/SFTP through the public TCP publication
and HTTPS through Caddy's internal CA. CI runs it for every supported family.
These containers do not validate SELinux or AppArmor policy. Ubuntu's host
`unix-chkpwd` AppArmor profile can still auto-attach to the PAM helper inside a
privileged container and deny access to RHEL-family mode-000 shadow files.
The distribution CI job temporarily unloads only that profile on its disposable
runner and restores it with an exit trap, including when the test fails. It does
not change the distribution's PAM rules, shadow permissions or SSH restrictions.
If a local run encounters this conflict, use a disposable VM rather than
weakening a workstation's security policy. Verify policy-sensitive deployment
changes on an enforcing VM (for example an AlmaLinux or Fedora cloud image).
