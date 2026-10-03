# Contributing

Keep examples generic and private inputs outside the checkout. Read
[the privacy checklist](docs/privacy.md) before committing. Use the generic Git
identity from the README when you want anonymous author/committer metadata.

Install `requirements-dev.txt` inside a Linux virtual environment, then run:

```bash
python -m unittest discover -s tests -v
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook \
  -i ansible/inventory.example.yml ansible/deploy.yml --syntax-check
python scripts/check-privacy.py --history
```

For process integration tests, download checksum-pinned binaries into a temporary
directory and run with sudo on a Linux host with OpenSSH installed and an existing
non-root user. The test uses new loopback ports, temporary keys, and child
processes; it does not deploy the Ansible roles or restart existing services:

```bash
python scripts/download-frp.py --destination /tmp/frp-test-binaries
sudo .venv/bin/python scripts/integration-test.py \
  --frps /tmp/frp-test-binaries/frps --frpc /tmp/frp-test-binaries/frpc \
  --user "$(id -un)"
```

OpenSSH's `/run/sshd` runtime directory must already exist for that process test.
Real role deployment and idempotence tests run only on a disposable Ubuntu/systemd
VM. The explicit `--apply-to-disposable-host` mode installs packages, creates the
project accounts/configuration, and starts system services. Never use that mode
on a production workstation as a routine test. CI provisions its ephemeral runner
and checks a repeat deployment has no changes.

Keep privilege, host-key verification, secret permissions, no-proxy behavior, and
role idempotence intact. Changes to release hashes, dependencies, service restart
relationships, and authentication require corresponding verification. Describe
the behavior and relevant tests in pull requests without live deployment facts.
