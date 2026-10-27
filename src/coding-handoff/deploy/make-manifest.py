#!/usr/bin/env python3
"""Regenerate `manifest.json` for the Phase J.1 rollout.

The manifest is the whole install: `install.sh` copies exactly the files listed
here, to exactly these targets, and refuses to start unless every hash matches.
Generating it from the tree rather than maintaining it by hand means a file
that was edited after the manifest was written fails the check instead of being
installed from a stale record.

Run from the repository root:

    python3 src/coding-handoff/deploy/make-manifest.py

`tests-handoff/test_rollout_manifest.py` fails when the checked-in manifest no
longer describes the tree, so a forgotten regeneration is caught by the suite
rather than at 2am on the Pi.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = Path(__file__).resolve().parent / "manifest.json"

GATEWAY_DIR = "/srv/hermes/channel-gateway/app"
BROKER_ROOT = "/opt/hermes-coding-handoff"
JOBS_DIR = "/srv/hermes/hermes-data/jobs"

#: Gateway files this change replaces, with the content the target is expected
#: to hold *before* the install. Measured, not assumed: these are the sha256 of
#: commit 9fcd1f8 (the installed J.1 release), verified byte-identical to
#: that commit for every one of them.
#:
#: The install fails closed on any mismatch. A gateway that does not hold
#: exactly this content has been changed by something else, and applying a
#: patch prepared against different source is how a deployment silently
#: reverts somebody's fix.
GATEWAY_BASELINES = {
    "gw_canonical.py": "65c208d40a4f7ff958855440a68c6eb17458cdf7ed9d831b919805547f6cfd30",
    "gw_config.py": "bae028fb52dcb27f3ebba6d76a36762f2e4af8c114f786ee99aabf1d4695bf17",
    "gw_hermes.py": "b43f1ba890792e1edfd6f46624ebb262ea86fbe059650e00c89e703ed0cb7306",
    "gw_schema.py": "8618bacf5efd51f4a7bb8d4558091eb669f599a73e78f5d102b74d9b4ff01ac1",
    "gw_service.py": "e61002a12a6a072d5111b4f5e3a5c2780b6ad5c162118d4c05a45e33605950f5",
    "gw_store.py": "1802fab638d388a0534a554909a51cc26a595f9f9f92597f4918e087d1d7ad66",
    "gw_coding.py": "72b6e3dfb6c8b190419ff5e2b31a6a9682224ce2ddf8a1bdac89b30165cb8c7a",
    "gw_handoff.py": "60f43eb5f3b459b4dd8a9ac61963695e9acfb7a821a723c120b880a755d9a126",
    "gw_handoff_client.py": "cfe7a4eb8c6da3f8724b499e3044c5f478e5134c1b35369a39fa6c0cb5303f45",
}

#: The job layer is *patched in place*, not copied.
#:
#: Measured: the deployed copies are logically identical to the committed ones
#: but not byte-identical — they use CRLF, and `transport.py` carries the
#: operator's own resolved bridge path where the repository carries a
#: placeholder. Copying the repository file over that would reinstate a path
#: that does not exist on the host and break the existing container consumers
#: that depend on the fallback.
#:
#: So the installer transforms the pinned live bytes: an anchored region is
#: rewritten and everything else — line endings and the operator's path
#: included — is verified byte-identical afterwards. The hashes below are the
#: live files, supplied by the operator's read-only measurement.
JOB_PATCHES = [
    {
        "kind": "create-job-id",
        "target": f"{JOBS_DIR}/jobs_db.py",
        "live_sha256":
            "4597f1ece4665800cb9508bc46d0be12e07f87b5f3b8bb7b4a07c3c102c2d167",
        "reference": "src/jobs/jobs_db.py",
        "note": "adds the optional deterministic job_id; required for dispatch",
    },
    {
        "kind": "bridge-sock-env",
        "target": f"{JOBS_DIR}/transport.py",
        "live_sha256":
            "6248cdda0d8eb9aad81a48f9668a6c062a81bdc429e0df748c93cd2a68dc31ed",
        "reference": "src/jobs/transport.py",
        "note": "adds HERMES_CODING_BRIDGE_SOCK; preserves the existing fallback",
    },
]

#: Deployed files this change does NOT touch, pinned so the install refuses to
#: run against a host whose Antigravity bridge is not the reviewed premium
#: release. Sourced from that release's own manifest.
UNMODIFIED_DRIFT = [
    {
        "path": "/srv/hermes/antigravity/app/bridge_server.py",
        "deployed_sha256":
            "7a2f740fbd3bd83c2d9a66f4f65189f67d30de84ba1a51eb85c20081a09aeb5d",
        "note": "premium CLI routing release; not modified by J.1",
    },
    {
        "path": "/srv/hermes/antigravity/app/bridge_config.py",
        "deployed_sha256":
            "5c630c6cebd08120898ae403b2a7bf4de3ca5931b4448f5c86dee9369cf01a86",
        "note": "premium CLI routing release; not modified by J.1",
    },
    {
        "path": "/srv/hermes/antigravity/app/premium_routing.py",
        "deployed_sha256":
            "1946c22e957f87b51e12d446e40e9bc03221e0e45c6c987cb570cd4206f18f9b",
        "note": "premium CLI routing release; not modified by J.1",
    },
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def entry(source: str, target: str, *, mode: str, baseline=None) -> dict:
    path = ROOT / source
    if not path.is_file():
        raise SystemExit(f"missing payload file: {source}")
    return {
        "source": source,
        "target": target,
        "mode": mode,
        "payload_sha256": sha256(path),
        "baseline_sha256": baseline,
    }


def build() -> dict:
    gateway = []
    for name, baseline in sorted(GATEWAY_BASELINES.items()):
        gateway.append(entry(f"src/channel-gateway/app/{name}",
                             f"{GATEWAY_DIR}/{name}", mode="0644",
                             baseline=baseline))

    broker = [
        entry(f"src/coding-handoff/app/{name}", f"{BROKER_ROOT}/app/{name}",
              mode="0644")
        for name in ("handoff_core.py", "handoff_ledger.py",
                     "handoff_registry.py", "handoff_server.py",
                     "handoff_spec.py")
    ]
    broker.append(entry(
        "src/coding-handoff/config/projects.sample.json",
        f"{BROKER_ROOT}/config/projects.sample.json", mode="0640"))
    for spec_dir in ("fastrawphotoview", "handoff-protocol-check"):
        for path in sorted((ROOT / "src" / "coding-handoff" / "specs"
                            / spec_dir).glob("*.md")):
            broker.append(entry(
                f"src/coding-handoff/specs/{spec_dir}/{path.name}",
                f"{BROKER_ROOT}/specs/{spec_dir}/{path.name}", mode="0640"))

    # The broker's own unit is *rendered* from a template at install time —
    # its identity, its bind mounts and the bridge socket it connects to are
    # facts about the host, not about this package — so it is not a manifest
    # entry. `install.sh` backs it up and `rollback.sh` restores it by name.
    units = [
        entry("src/coding-handoff/systemd/channel-gateway-handoff.conf",
              "/etc/systemd/system/channel-gateway.service.d/"
              "10-coding-handoff.conf", mode="0644"),
    ]

    return {
        "version": 1,
        "phase": "J.1",
        "components": {
            "gateway": {"service": "channel-gateway.service", "files": gateway},
            "broker": {"service": "hermes-coding-handoff.service",
                       "files": broker},
            "units": {"service": None, "files": units},
        },
        "job_patches": JOB_PATCHES,
        "unmodified_drift": UNMODIFIED_DRIFT,
    }


if __name__ == "__main__":
    MANIFEST.write_text(
        json.dumps(build(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n")
    print(f"wrote {MANIFEST.relative_to(ROOT)}")
