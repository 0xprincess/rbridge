# Publishing without deployment identities

The public repository contains reusable logic and documentation-only examples.
Private deployment inputs belong on a Linux filesystem outside every checkout,
with restrictive permissions. Public keys, SSH fingerprints, hostnames, user
names, IP addresses, Windows profile paths, and inventories can identify a person
or deployment even when they are not authentication secrets.

## Before every commit and push

1. Inspect `git status --short` and the complete staged diff.
2. Run `python scripts/check-privacy.py --staged`.
3. Review commit author and committer metadata. Use the generic repository-local
   identity in the README if you do not want to publish yours.
4. Before publication, run `python scripts/check-privacy.py --history
   --anonymous-history` and inspect history, branches, tags, signing metadata,
   screenshots, logs, generated artifacts, and references manually.
5. Confirm that no generated inputs were staged with `git add -f` and that no
   credentials appear in remote URLs. A local SSH key must never become a Git blob.

The `.gitignore` excludes common deployment material. Local pre-commit/pre-push
hooks scan tracked content and CI repeats the check across historical blobs.
Clones do not automatically enable hooks; configure `core.hooksPath` explicitly.
Hooks can be bypassed, and `.gitignore` does not remove already tracked files.

The scanner reports paths, line numbers, and categories while withholding matched
values. It catches recognizable private/public SSH keys, common access token
formats, URL passwords, non-example IPv4 endpoints, personal filesystem paths,
and email addresses. It intentionally permits loopback, unspecified addresses,
RFC 5737 documentation networks, and RFC 2606 `.invalid`/`.example`/`.test`
email domains. Publicly routable email domains remain prohibited.

This is a defense in depth check, not a complete identity or secret detector.
Arbitrary DNS names, encoded secrets, prose, IPv6 endpoints, filenames, timing,
and GitHub account/repository ownership still require human review. A public
GitHub repository itself exposes its owner even if commit metadata is generic.

## Deployment and incident handling

Deployment outputs are never uploaded as CI artifacts. Private workflow values
are masked before provisioning, and secret Ansible tasks suppress logs and diffs.
Custom debugging and error messages can still expose identifying details: avoid
verbose production runs and review any logs before sharing them.

Bridge workspaces additionally contain `controller.yml` (administrative key
path), `last-applied.json` (the previous merged configuration, including
credentials), an incomplete-apply marker and private `apply.log`. Keep all of
them outside checkouts with restrictive permissions; previous credentials can
remain in the baseline during failed rotations. Retain/revoke them deliberately.
Bridge defaults redact names/endpoints as well as credentials; `--show-endpoints`
is deliberate local disclosure, not permission to publish screenshots or logs.
Even disclosed summaries never print key contents, passwords or tokens.

OIDC client/cookie secrets, Basic password hashes, admin API credentials,
certificate keys and browser session cookies also belong outside the checkout.
Field-test `--artifacts` output can contain these values and must never be
uploaded as public CI artifacts. Delete retained test inputs after destroying
their disposable infrastructure.

If a secret enters Git history, rotate/revoke it first. Deleting the file in a new
commit does not remove previous blobs, forks, clones, cached patches, or logs.
Coordinate a deliberate history cleanup with repository administrators after
rotation. Do not paste the affected secret into an issue or a commit message.
