# ops/

Deployment artifacts for the production VPS. Files here are **verbatim copies of
what runs in production** — not templates. Change the copy here first, then
re-install it on the box, so the two never drift.

## Layout

| Path                              | Installed to                    | Notes                                                            |
| --------------------------------- | ------------------------------- | ---------------------------------------------------------------- |
| `systemd/pareton-api.service`     | `/etc/systemd/system/`          | uvicorn on `0.0.0.0:8000`                                        |
| `systemd/pareton-worker.service`  | `/etc/systemd/system/`          | Submission gates and builds (queue via drop-in)                  |
| `systemd/pareton-worker.service.d/queue.conf` | `/etc/systemd/system/pareton-worker.service.d/` | Clears `ExecStart`, re-sets it with `--queue submissions` |
| `systemd/pareton-worker.service.d/timeout.conf` | `/etc/systemd/system/pareton-worker.service.d/` | `TimeoutStopSec=4h`, overrides the unit's `8h` |
| `systemd/pareton-round-worker.service` | `/etc/systemd/system/`     | Round evaluation (`--queue rounds`)                              |
| `systemd/pareton-watcher.service` | `/etc/systemd/system/`          | Chain ingest, `python -m worker.watcher`                         |
| `systemd/pareton-weights.service` | `/etc/systemd/system/`          | Weight cadence, `python -m weights`. Holds the validator wallet. |
| `systemd/pareton-deploy.service`  | `/etc/systemd/system/`          | Oneshot, invoked by the timer; `OnFailure=` chains the alerter  |
| `systemd/pareton-deploy.timer`    | `/etc/systemd/system/`          | **Fires every 60s**                                              |
| `systemd/pareton-deploy-failed.service` | `/etc/systemd/system/`     | Started by `OnFailure`; sends the Discord deploy-failure alert  |
| `systemd/pareton-builder-cleanup.service` | `/etc/systemd/system/` | Docker image and BuildKit cleanup oneshot                         |
| `systemd/pareton-builder-cleanup.timer` | `/etc/systemd/system/` | Runs builder cleanup hourly                                      |
| `docker/daemon.json`                  | Merge into `/etc/docker/daemon.json` | Disables Docker's competing BuildKit GC without selecting an image store |
| `deploy.sh`                       | `/usr/local/bin/pareton-deploy` | The pull-deploy script itself                                    |
| `sync-config.py`                   | `/usr/local/lib/pareton-ops/`   | Stage-1 config check/apply; called by deploy.sh every tick       |
| `notify-deploy-failure.py`        | `/usr/local/lib/pareton-ops/`   | Deploy-failure notifier (4 modes); runs on `OnFailure`           |
| `ops_common.py`                    | `/usr/local/lib/pareton-ops/`   | Shared stdlib helpers for the two programs above                 |
| `gpu/pareton-gpu-reap.service`    | `/etc/systemd/system/`          | Oneshot GPU TTL reap                                             |
| `gpu/pareton-gpu-reap.timer`      | `/etc/systemd/system/`          | Fires every 10 min                                               |
| `vector/vector.service`           | `/etc/systemd/system/`          | Log shipping                                                     |
| `vector/vector.toml`              | `/etc/vector/`                  | Axiom sink, dataset `pareton-prod`, token via env ref           |
| `caddy/Caddyfile`                 | `/etc/caddy/`                   | TLS terminator, proxies to `127.0.0.1:8000`                      |

`deploy.sh` installs to `/usr/local/bin` rather than running from the repo
checkout so that a `git pull` cannot rewrite the script while it is executing.
Since stage 1 it **self-installs** that copy (plus the `/usr/local/lib/pareton-ops`
programs) from the just-pulled commit on every successful deploy, helpers first
and the main entry last. The one-time manual bootstrap that installs the first
self-updating copy is in [`runbook.md`](runbook.md).

## A merge to `main` is a production deploy

`pareton-deploy.timer` polls `origin/main` every 60 seconds. There is no
separate promote step. Every tick that holds the deploy lock first runs the
stage-1 config sync (`sync-config.py deploy-hook`): managed-file drift
converges to the Git content automatically, while unknown files, masks, or a
broken alert credential fail the deploy and page the team. Any merge then
restarts `pareton-api`, `pareton-watcher`, and `pareton-weights` within a
minute. Each execution worker has its own pending
restart. The round worker waits only for running rounds; the existing worker checks
both queues to protect legacy combined processes. A busy build does not defer an
idle round worker's update. A deploy that fails — including a failing worker
busy-probe — triggers `OnFailure=pareton-deploy-failed.service`, which sends a
rate-limited alert straight to the team Discord channel (independent of
Vector/Axiom).

**During a maintenance window, stop this timer first.** Stopping any other unit
while the timer is live means the timer may restart it underneath you. Stopping
the timer does not stop a deploy that is already running — wait for it, then
work. After a manual hotfix to a managed file, the fix must be **merged to
`main`** (a pushed branch or open PR is not enough) before the timer is
resumed, or the next tick reverts the live change as drift.

### Worker heartbeat alerts

After both services are shipping logs, filter the existing Axiom
`worker-heartbeat-absent` monitor to `pareton-worker.service`, then clone it as
`round-worker-heartbeat-absent` with the second query below. Keep the current
notifiers and evaluation frequency, use **Below 1 over 15 minutes**, and enable
**Alert on no data** for each. The existing `_SYSTEMD_UNIT` field identifies the
process, so the heartbeat payload does not need changing.

```apl
['pareton-prod']
| where event == "heartbeat" and _SYSTEMD_UNIT == "pareton-worker.service"
| summarize count()
```

```apl
['pareton-prod']
| where event == "heartbeat" and _SYSTEMD_UNIT == "pareton-round-worker.service"
| summarize count()
```

Use two fixed filters: a grouped query can lose a missing service's group while
the other continues reporting. These alerts detect absent processes or telemetry;
progress stalls still require round phase/heartbeat monitoring. Adding weights
to the allowlist resumes its telemetry on the next scheduled event, without
forcing a weight submission or recovering previously discarded logs.

**During a maintenance window, stop this timer first.** Stopping any other unit
while the timer is live means the timer may restart it underneath you.

## Known drift — needs a decision

Resolved on 2026-09-10 by the stage-1 capture (files here now mirror the live
box, verified against the read-only audit output of that day; made byte-exact
on 2026-09-11 per owner review — provenance lives in this README, never as
added comments inside the files, so the first sync sees zero diff on the
worker and owes it no restart):

1. ~~`pareton-worker.service` differs from the live unit~~ — the committed file
   is the byte-exact live version; the queue split lives in the
   `queue.conf` drop-in (byte-exact as well).
2. ~~`TimeoutStopSec` drop-in drift~~ — `timeout.conf` (4h) is committed next
   to the unit; the effective value stays 4h. Revisit the value itself later.
4. ~~`vector.toml` inline token~~ — the repo keeps the `${PARETON_AXIOM_TOKEN}`
   form; the owner verified the env token via a direct ingest test, so the
   live file migrates to the env reference at the stage-1 bootstrap and new
   events arriving in `pareton-prod` are the acceptance evidence
   (`vector validate` passing proves nothing about token validity).

Still open, each because it changes production behavior:

3. **`aws/pareton-api-iam-policy.json` overstates the live IAM policy.** It
   grants `s3:ListBucket` and `s3:DeleteObject`; the live `pareton-api` user
   has neither. Only `PutObject`/`GetObject` on `stage0/*` actually work.
   Private patch uploads, validator reads, and public copies require only
   `PutObject`/`GetObject`. The file should not be treated as an accurate record
   of live permissions.

5. **The box needs swap, and nothing here says so.** Hermetic builds compile
   vLLM's CUDA kernels; `cicc` peaks at 6–12 GB per job and will OOM a 16 GB
   box. The only record of this is a comment in `a2b-build.sh` telling you to
   `fallocate` a 64 G swapfile by hand. A host rebuilt from this directory
   silently gets no swap, and the first submission dies with `Killed` /
   `exit status 137` — which surfaces as `hermetic_build_failed` and rejects
   the miner's patch for an infrastructure fault. `pareton-prod-02` now has a
   64 G swapfile with an `/etc/fstab` entry; provisioning should create one.

## Reinstalling a unit

```sh
scp ops/systemd/pareton-deploy.timer root@<host>:/etc/systemd/system/
ssh root@<host> systemctl daemon-reload
ssh root@<host> systemctl restart pareton-deploy.timer
```

## Builder disk cleanup

The persistent build host keeps local retention tags for the build-base and
baseline engine images of every draft or open campaign. Published candidate
tags are removed after their digest-pinned reference is stored in Postgres.
The hourly fallback sweep removes leftover candidate tags and prunes ordinary
BuildKit records after Docker storage crosses 75% usage. It targets the same
explicit Buildx builder as miner builds and does not run daemon-wide image or
system prune commands.
BuildKit `exec.cachemount` records are excluded because they hold the warmed
baseline ccache used by later miner builds.

Docker Engine's background BuildKit GC is disabled on the dedicated builder
host. Pareton's filtered cleanup is the only BuildKit GC authority, so Docker
cannot independently reclaim `exec.cachemount`. Both the worker and cleanup
units fail their startup check unless `/etc/docker/daemon.json` has
`builder.gc.enabled=false`.

Baseline build and serving images for every draft or open campaign have local
retention tags. Candidate and leader images are durable in GHCR by digest.
Rounds read those digest-pinned references from Postgres and pull them on the
GPU host, so removing a builder-host candidate tag never causes a rebuild.
This cleanup never deletes registry artifacts.

The cleanup fails without deleting anything when Postgres is unavailable, and
skips a run when a build holds the shared storage lock. Preview it before
installing the timer:

```sh
cd /opt/pareton
set -a
. ./.env
set +a
.venv/bin/python -m builder.cleanup --dry-run --force
```

Install the Docker policy and both units during a maintenance window. The
committed file is a merge fragment, not a replacement daemon configuration.
It deliberately omits `features.containerd-snapshotter`. Merge it into the
host's current configuration so Docker keeps its active classic or containerd
image store and every unrelated daemon setting. Record the active storage
driver before the restart and require the same value afterward.

```sh
systemctl stop pareton-deploy.timer
systemctl stop pareton-worker
command -v jq
test -f /etc/docker/daemon.json
image_store_before=$(docker info --format '{{json .DriverStatus}}')
cp -a /etc/docker/daemon.json /etc/docker/daemon.json.pre-pareton-gc
daemon_merged=$(mktemp)
jq -s '.[0] * .[1]' \
  /etc/docker/daemon.json ops/docker/daemon.json > "$daemon_merged"
dockerd --validate --config-file="$daemon_merged"
install -m 0644 "$daemon_merged" /etc/docker/daemon.json
rm -f "$daemon_merged"
systemctl restart docker
image_store_after=$(docker info --format '{{json .DriverStatus}}')
test "$image_store_after" = "$image_store_before"
.venv/bin/python -m builder.gc_config
cp ops/systemd/pareton-worker.service /etc/systemd/system/
cp ops/systemd/pareton-builder-cleanup.service /etc/systemd/system/
cp ops/systemd/pareton-builder-cleanup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl start pareton-worker
systemctl enable --now pareton-builder-cleanup.timer
systemctl start pareton-builder-cleanup.service
systemctl start pareton-deploy.timer
journalctl -u pareton-builder-cleanup.service -n 100 --no-pager
```

## Use a fixed GPU machine

Configure the existing provider mode in the validator's `.env`:

```dotenv
PARETON_GPU_PROVIDERS=static_ssh
PARETON_GPU_STATIC_SSH=user@host:port
PARETON_GPU_SSH_KEY_PATH=/path/to/key
```

Restart `pareton-round-worker` after its current round finishes. Use a dedicated
Linux node with NVIDIA drivers, SSH access and passwordless sudo when the SSH
user is not root. One validator should own this node. Worker processes on that
validator must share `PARETON_GPU_STATE_DIR` so they use the same host lock.
The bootstrap still recreates the remote Python environment each round.

### Campaigns and rental ownership

This setting routes every campaign to the same host. Before switching to a 5090
node, close the H200 campaign and finish its pending/running rounds on compatible
hardware. **Closing a campaign alone does not drain its existing queue.** Static
provisioning probes `nvidia-smi` and rejects mismatched models, mixed GPU models,
or insufficient GPU counts before evaluation. A larger matching node is allowed,
but the worker logs its unused GPU count. Rent a 4-GPU node for a campaign pinned
to 4 GPUs; changing the campaign's GPU count changes its benchmark configuration.

Static mode never cancels the Lium rental, including at campaign closure. Release
the machine manually when it is no longer needed. Use an operator-managed name
for both the rental and its volume. **Do not repurpose a `pt-<timestamp>-<ttl>h-*`
rental without retiring its TTL management first:** the reaper still scans cloud
providers for those names, even when `GPU_PROVIDERS=static_ssh`. Static SSH entries
are skipped, but that does not exempt a separately listed cloud rental.

### Automatic housekeeping

Before pulling images, each static run removes abandoned `pareton-bench-<run-id>`
containers and networks. Container removal includes anonymous volumes. It also
removes obsolete candidate images recorded by previous runs. After collecting
the result, it removes the current candidate images and its temporary remote
output directory. The candidate image tracking file is
`/opt/pareton/.static-host-images.json`; keep it across worker restarts. Failed
image deletions remain tracked for retry and produce a cleanup failure.

Cleanup is limited to Pareton bench names and recorded image references. Baseline
images for the current requests, `/workspace/hf-cache`, and
`/workspace/engine-cache` are preserved. Candidate containers do not mount the
shared compile cache; normal container teardown also removes anonymous volumes.
Inspect images left from runs predating this tracking file separately before
removing them. Automatic reclamation runs with every round, so candidate images
from ongoing work do not need a separate daily pruning job.

A local host lock prevents overlapping workers; a remote lock protects the
harness from housekeeping. Bootstrap refuses a host whose previous harness is
still active. The remote harness has the same time limit as its SSH invocation,
with a 30-second forced-kill grace period. After a worker crash or timeout, the
next run or periodic reaper reclaims abandoned containers once the prior harness
has exited. A new harness waits up to 120 seconds for brief maintenance to finish.

The existing `pareton-gpu-reap.timer` also checks a configured static host every
10 minutes, independently of the round worker. It takes the remote host lock,
skips active harnesses, removes idle Pareton containers/networks, and checks
`nvidia-smi --query-compute-apps=pid` for remaining GPU compute processes. It
allows two seconds for process exit before reporting a failure. This periodic
path leaves image tracking and output files untouched, so it cannot delete a
report that the worker is still downloading or depend on a valid image ledger.
Remaining processes, an unreachable host or failed GPU inspection emit
`static_host_cleanup_failed` and fail the reaper run for monitoring.

Deploy the updated code on the validator and GPU node (normal bootstrap uploads
it), and ensure `pareton-gpu-reap.timer` is enabled on the validator. Its service
reads the static SSH target/key from the same `.env`. This handles worker failure
while the validator and SSH remain reachable. If the whole validator is down,
its timer cannot run. A live harness keeps its lock until exit or its configured
timeout; periodic cleanup deliberately leaves it running.

Container process termination normally frees its GPU allocations. The check
confirms that compute processes are gone; it does not require zero reported VRAM,
reset the GPU driver, or kill unrelated processes. Driver failures or memory held
outside managed containers require operator investigation. Validate crash
recovery on the actual node before relying on this unattended.

### Failed-round and cleanup alerts

There is no cloud fallback when the configured provider is only `static_ssh`.
An unreachable or incompatible host voids the round and emits `round_voided`.
Cleanup failure after an otherwise successful round emits
`static_host_cleanup_failed`, so a valid score is retained while disk cleanup
still receives attention.

Create an Axiom monitor using this query, the existing operations notifier,
**Above 0 over 5 minutes**, and evaluation every minute. This is an event-count
alert; keep **Alert on no data** off and retain the separate worker heartbeat
alerts above.

```apl
['pareton-prod']
| where (event == "round_voided" and void_reason in
    ("pod_provision_failed", "pod_failed", "round_timeout", "heartbeat_stale"))
    or event == "static_host_cleanup_failed"
| summarize count()
```

Investigate the failed round's detail and `pareton-round-worker` journal, restore
SSH/GPU access or resolve cleanup errors, then resume work on compatible hardware.
The query and event emission are versioned here; the monitor and its notifier
must be activated in Axiom during deployment.
