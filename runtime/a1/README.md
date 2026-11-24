# Local synthetic A1 setup

This file preserves original setup evidence and reusable local configuration.
Use only the approved exact setup and L1 in the current review package. At the
initial setup checkpoint, no human n8n account, Drive credentials, product
implementation or product tests were established by that setup evidence.

Earlier local implementation and verification are recorded in the
[integration149 delivery](../../docs/execution/integration149/delivery.md),
[synthetic n8n153 delivery](../../docs/execution/n8n153/delivery.md) and
[offline Drive155 delivery](../../docs/execution/drive157/delivery159.md).
Those records include application tests and actual synthetic n8n engine execution;
their original offline Drive boundary remains historical. Later retained evidence
includes one authorized live synthetic PDF capture/fixture-processing journey
([acceptance reconciliation](../../docs/planning/review171/acceptance.md)),
[PostgreSQL-only Python orchestration](../../docs/execution/transition167/delivery.md)
and [actual model-to-review QA](../../docs/execution/experiment172/delivery.md).
Live Google negative responses, Full114 and broad customer quality remain unverified
or unresolved at their documented boundaries. Current intake does not require n8n
([ADR0001](../../docs/adr/0001-python-owned-provider-independent-intake.md)).

Historical observations below retain their original scope. Operational and safety
instructions remain applicable under current task authorization; this file does
not grant permission to start services, dispatch experiments or access credentials.

## Interpreter and overlay

Every Python process starts through the NILARIS Conda command. Load the overlay
**after** Conda starts; setting PYTHONPATH on Conda itself loads CPython3.12 native
wheels into its CPython3.14 launcher and fails. Baseline packages were not changed.

```
/opt/miniconda3/bin/conda run --no-capture-output -n nilaris python runtime/a1/with_overlay.py -m pip check
/opt/miniconda3/bin/conda run --no-capture-output -n nilaris python runtime/a1/with_overlay.py -m pytest <focused-path>
```

Overlay/wheels and task secrets currently live outside Git at
`/private/tmp/nilaris-a1-setup-02/`. `requirements.lock` pins all58 resolved
versions and the SHA256 of each acquired binary wheel; `wheels.sha256` maps hashes
to actual platform filenames. This lock is the resolved macOS ARM64 Python3.12
artifact set, not a cross-platform promise. Transitive LangSmith is required by
LangChain; tracing is explicitly disabled in the launcher. No model/reference
calls or LangSmith service use is authorized.

## Task services and credentials

The retained Compose project is `nilaris-synth`; source runtime configuration is
`runtime/a1/compose.yaml` in `/private/tmp/nilaris-a1-01`. Both image references pin
linux/arm64 platform manifests, not mutable tags or manifest-list digests.
Task secrets are newly generated hex strings at
`/private/tmp/nilaris-a1-setup-02/secrets.env`, mode0600. Never print, source with
shell tracing, commit, or include this file in diagnostic artifacts. It contains
A1_ADMIN_PASSWORD, A1_APP_PASSWORD, A1_MIGRATION_PASSWORD, A1_TEST_PASSWORD,
A1_N8N_PASSWORD, A1_N8N_KEY and A1_APP_TOKEN. No human n8n identity was invented.
Ignore rules exclude equivalent local state if relocated inside this directory.

PostgreSQL: host127.0.0.1 port55432; app role `nilaris_app` (connection cap4),
migration role `nilaris_migration` (cap2), retained database `nilaris_demo`.
Migration owner creates schema; app receives default table/sequence privileges.
Test role `nilaris_test` (cap2, CREATEDB) creates **new uniquely named**
`nilaris_test_*` databases within this task-owned cluster. Each active test run
owns its exact created database and only that disposable database may be dropped.
Never reset/drop `nilaris_demo`, `nilaris_n8n` or either persistent volume.
The only setup disposable database was `nilaris_test_setup02`; it was dropped.
Use psycopg keyword parameters and read the matching secret in process; do not
put passwords in command-line DSNs or logs. No baseline/project DB is reused.

n8n: host127.0.0.1 port5679; separate `nilaris_n8n` role/database (cap2).
The two connections are occupied by n8n's configured pool; a third setup probe
was refused as expected. Health200 and actual pooled sessions were observed.
Those original setup observations did not establish owner UI setup, authored
workflows or human approval. Later synthetic workflow execution and isolated
approval evidence is recorded in the n8n153 delivery linked above.

Application configuration must consistently use127.0.0.1:18765 and A1_APP_TOKEN.
An authenticated temporary loopback listener was reached from n8n via
`host.docker.internal:18765`; it was closed after the probe. This proves that
specific initial route/probe, not application authentication or workflow logic.
No existing8765 listener was changed. Revalidate binding at real app startup.

## Guarded runtime and retained state

At this checkpoint services are stopped; containers/volumes are retained.
Physical PostgreSQL scans and owned database CREATE/DROP share the original private
OS lock at `/private/tmp/nilaris-a1-setup-02/postgres-storage.lock` and its sibling
`postgres-storage.lock.turnstile`. The [Ticket01 P1 protocol and participant inventory](../../docs/execution/provision181/storage-turnstile.md)
amend the earlier single-file setup. This documentation does not authorize changing
the retained setup. For an authorized **new** setup, initialize both files once,
only after proving all task services, guards, and tests stopped:

```
/opt/miniconda3/bin/conda run --no-capture-output -n nilaris python - <<'PY'
import os
base = "/private/tmp/nilaris-a1-setup-02/postgres-storage.lock"
for suffix in ("", ".turnstile"):
    fd = os.open(base + suffix, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
PY
```

An existing file makes this command fail safely; do not replace it. A failure can
leave partial initialization: keep participants stopped, reconcile which file was
created, and initialize only the missing file; never unlink/recreate the other.
For an
authorized upgrade of existing single-file setup, first stop and reconcile **all**
participants and prove the original is owned regular0600, single-link and retains
its recorded device/inode. Create only the missing sibling:

```
/opt/miniconda3/bin/conda run --no-capture-output -n nilaris python -c 'import os; fd = os.open("/private/tmp/nilaris-a1-setup-02/postgres-storage.lock.turnstile", os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600); os.close(fd)'
```

Record both device/inode identities and private0600 single-link ownership before
restart. Update every participant to the same module revision; mixed old/new
callers are unsupported. Reuse both inodes on renewal; never unlink/recreate either
during a live session. Live calls never auto-create missing files. Both files are
opened without symlink following and validated before and after acquisition;
missing, invalid or replaced proof fails closed. Every caller first acquires EX
on the sibling, then SH (guard) or EX (lifecycle) on the original within the same
absolute deadline. It releases the sibling once the original is acquired, keeping
the original through physical probes or exact owned CREATE/DROP. An admitted
writer prevents the next scan bypassing it while an existing scan completes;
this does not promise FIFO or universal scheduler fairness. Guard acquisition
spends its existing one-second complete-cycle budget; fixture/provisioner
acquisition remains bounded to one second. Excessive contention stops services or
fails the operation rather than trusting stale data. Exceptions/process exit
release descriptors/ownership.
All storage ceilings, freshness checks and original subprocess timeouts remain.

Failed physical `du` probes report bounded JSON after
`Physical storage probe failed: `: monotonic timing, service, exit status, output counts and allowlisted
error/path metadata. Generic Compose output and unknown path components are
withheld. The original exception still stops services; no error is ignored and
no prior measurement is reused. The historical PostgreSQL `du` failure remains
open and unattributed because its original captured output was unavailable;
this reporting change does not repair or explain that failure.

An overlong completed cycle also reports `Storage cycle probe timings: ` with
fixed labels for PostgreSQL du, n8n du and writable-layer inspection, numeric
monotonic timings, queue waits and executor completion gap. It still raises and
stops services at the original one-second limit. The historical completed-cycle
miss remains open and unattributed independently of the earlier du failure;
these diagnostics are error reporting, with zero mechanism repairs claimed.

Run from the ticket worktree, with ordinary required Docker sandbox escalation:

```
/opt/miniconda3/bin/conda run --no-capture-output -n nilaris python runtime/a1/service_guard.py --seconds 1200 --services postgres --compose-owner /private/tmp/nilaris-a1-01/runtime/a1
```

Run tests in a separate command while this foreground guard runs. Choose a
session no longer than the focused-command20min / integrated-demo30min limits;
this guard permits at most1800s, then stops only the task services without
removing volumes. It also stops them on observed storage/free-space failure.
No automatic restart, alternate engine/image, volume deletion or re-provisioning.
The initialization SQL runs only on a new empty image data directory. Do not
manually replay it on the already-provisioned retained cluster. A one-time
explicit SQL repair was performed during setup after the shell bind-mount
initialization failed; the repair option was removed after success.

Docker memory/CPU limits and log rotations were inspected, DB durability/timeouts
queried. Physical monitoring targets one-second cycles **after startup readiness**;
subprocess calls have10s timeouts and can delay sampling; a completed cycle
taking over1s stops task services. It is monitored,
may overshoot and is not a hard storage quota or guaranteed1s scheduling. Startup
readiness itself is bounded100s. No application work is admitted during setup.
At that setup checkpoint, application admission/worker RSS/CPU/claims/byte
accounting/egress controls still required implementation and behavioral tests
under the accepted L1 contract. Later evidence is linked above.
Do not claim this service setup proves those controls or product acceptance.

The guard reserves2GiB for the acquired images (reported total479145441 bytes)
and caps measured task files+service data+writable layers+reserved32MiB container
logs at6GiB, preserving the8GiB setup allocation bound conservatively for this
idle setup. Physical Docker VM allocation and sampling stalls are not hard-quota
coverage. Application admission must stop on stale/failed service monitoring.
Do not use this guard as a complete runtime admission controller.

The guard atomically publishes private `service-monitor.json` after each successful
cycle. Offers require ready status, age0..1s and live PID, checked before DB access
and after the admission lock. Failure returns unknown coverage with same-key
resolve/retry guidance; resolve/status stay available. Independent read-only probes
run concurrently; all ceilings/timeouts remain. Publication failure cannot bypass
shutdown. Sampling/check-to-write windows and PID reuse remain limitations.

## Admission command service and textual client

The standard-library adapter binds atomically and exclusively to
`127.0.0.1:18765`. Start it only during a guarded task session:

```
/opt/miniconda3/bin/conda run --no-capture-output -n nilaris python runtime/a1/with_overlay.py -m nilaris.command_service serve --config /private/tmp/command.json
/opt/miniconda3/bin/conda run --no-capture-output -n nilaris python runtime/a1/with_overlay.py -m nilaris.command_service client --config /private/tmp/command.json < /private/tmp/command-request.json
```

The private JSON configuration must be mode0600 (no group/other permissions),
at most16KiB, and contain `database` (psycopg keyword options), `token` (the task's
`A1_APP_TOKEN`, loaded in process, never command-line/URL), and
`admission_profile` (the complete `dataclasses.asdict(AdmissionProfile.l1())`
object). A null/absent profile keeps resolve/status available and refuses offers.
Use the app role for the retained demo; only the operator migration API may use
the separate migration role. No migration or arbitrary SQL HTTP operation exists.
Real subprocess tests create private configurations with their uniquely owned
`nilaris_test_*` database and test role; retained demo/n8n data is untouched.

Client input is one JSON object on stdin. The original admission-only command
surface was `offer`, `resolve`, or `status`, with the corresponding public
ingestion parameters; subsequent slices extend that surface.
For example, a query is `{"operation":"status","job_id":"<acknowledged-id>"}`.
Output is textual JSON containing the public disposition/status, not a visual
layout approval. Successful process exit only means a response was presented;
inspect `known`, categories, member dispositions and totals. A lost response is
unknown with affected identities and same-key resolve/retry guidance, never a
manufactured rejection. The token travels only in the Authorization header.
Invalid CLI arguments and startup configuration/listener failures emit a generic
JSON error envelope on stderr and exit nonzero (2 for arguments, 1 for startup).
Conda may append its own nonzero-exit diagnostic after that envelope.

The original admission slice accepted only `connector=synthetic`,
`processing_contract=fixture-v1`, `intent_id=ordinary`, and explicit
`reference_set=none` / `selection_policy=none`. Unknown processing contexts and
unlinked reprocessing intents returned `unsupported_context`; ticket02 then owned
the linked reprocessing extension. Ordinary delivery still deduplicates across
command IDs and returns the original Job/profile.

Requests are single-threaded with backlog1, three-second socket timeouts,
64KiB bodies and128KiB response cap. Standard-library HTTP header parsing also
has finite built-in line/count limits. Transfer encoding and duplicate length
headers are refused; malformed/unsupported requests have generic errors and no
request/configuration logs. These adapter bounds do not prove worker/process L1
controls or hard physical quotas. Own the complete process group when running
through Conda: stopping its wrapper alone does not establish server shutdown.

## Python-only completed resume (Transition167)

The default historical guard still selects both services. For the approved
Python-only path, explicitly select PostgreSQL and the retained original01
Compose owner; the guard code can run from the candidate worktree:

```
/opt/miniconda3/bin/conda run --no-capture-output -n nilaris python runtime/a1/service_guard.py --seconds 1200 --services postgres --compose-owner /private/tmp/nilaris-a1-01/runtime/a1
```

The owner must be this checkout or that exact original01 directory. Every existing
same-project container must match the selected owner. An unselected running task
service refuses renewal; the guard does not stop it. PostgreSQL-only startup and
shutdown select only `postgres`. Historical n8n containers, databases and volumes
remain retained. No n8n process starts. A temporary inert reader uses the existing
pinned PostgreSQL image, no network, a read-only root/mount, 64MiB memory, 0.1CPU,
16PIDs, no restart/logging and a finite1800s sleep. It measures only the existing
project-labeled `nilaris-synth_n8ndata` volume, including the original256MiB ceiling;
missing/foreign volume ownership refuses. The reader is removed on exit. Every
retained project container's writable layer plus the reader is still counted.
The same lock, one-second complete-cycle deadline, readiness signal, physical
ceilings, private files/log reserve and bounded stop remain authoritative.

Start the existing credential-free `nilaris.resume165` listener with its exact
private command/source/authority/manifest files and existing review UI according
to the approved session. Then invoke the finite read-only verifier:

```
/opt/miniconda3/bin/conda run --no-capture-output -n nilaris python runtime/a1/with_overlay.py -m nilaris.resume167 --command-config /private/tmp/command.json --source /private/tmp/source.json --authority /private/tmp/authority.json --manifest /private/tmp/manifest.json
```

These are private nonsecret source/authority snapshots and the original manifest;
command configuration contains local authentication/database settings, never
provider credentials. The verifier requests only the original two operation
journals, exact completed Job and at most eight result pages. Each response retains
the128KiB cap. Fragment offsets, cursor cycles/completion, exact publication,
source pin/context and whole-result shape must validate before the JSON handoff
exposes the existing loopback review URL. It never creates a draft, approves,
acquires or processes. A lost/refused/malformed/stale response exits nonzero with
unknown coverage and original-operation recovery guidance. Reinvoke with the same
snapshots/identities after restoring the listener; no new key or automatic retry.
A verified handoff remains an owner decision pending, not human approval.
