# DLDD implementation decisions

This file records choices left open by the Device Local Diagnosis HLDs.  It is
not a replacement for either HLD; where the HLD is explicit, the HLD wins.

## Structure and vendor extensions

- DLDD is part of `sonic-host-services`, because it is a root host service and
  must remain independent of PMON and Docker lifecycles.
- Core models, validators, evaluators, monitors, and correlation are platform
  independent.  Platform packages may implement the fixed, trusted
  `sonic_platform.dldd` module.  Its factories return a `DSERegistry` and a
  `VendorHookRegistry`.  A rule never chooses a Python module or class.
- DSE references use a typed protocol.  Neither rule content nor DSE data is
  passed to `eval`, `exec`, or a shell.  Vendor-specific source and action types
  must be explicitly advertised and registered.

## Validation and correlation

- `AND` binds more tightly than `OR`; parentheses remain preferred.
- The default product/software matcher uses exact strings and can be replaced
  by the trusted platform factory. Activation fails closed when required
  product or software identity is unavailable.
- Parse, exact-schema, semantic, materialization, compatibility, and missing
  installed-hook errors reject the complete candidate. Signature paths remain
  in diagnostics so the file can be corrected without implying partial use.
  Expansion/get/compare/collection/action errors after activation are isolated
  runtime failures.
- Ingestion `broken_rules` records retain the signature version, use the HLD
  reason categories (`schema_error`, `dse_error`, `evaluation_error`, or the
  default `validation_error`), and timestamp `last_attempt` when activation
  validation runs. Adapter preflight failures use `validation_error` and the
  same timestamp field.
- File-based YAML and JSON validation retains one-based parser node lines.
  Issues use the exact field line when present and the nearest mapped parent
  for missing fields. Parse failures use the parser problem line. The line is
  additive/optional for callers that validate an already-parsed object, and is
  included in both JSON and human-readable CLI output.
- `static-schema` deliberately does not import platform extension factories.
  It validates the built-in shapes and defers whether a vendor operation type
  is advertised to DSE resolution/activation, where the installed platform
  hook is authoritative.
- A zero match period means current-state semantics and therefore requires a
  match count of one.  A zero logic lookback also means current-state semantics.
  Monotonic time drives timers; Unix epoch time is published in telemetry.
- Service fatal thresholds count unique rule IDs, not expanded correlation
  keys, so one high-fanout rule cannot exhaust the service threshold alone.

## Rules generations and persisted state

- A previously unattempted stable inbox file has first priority. Packaged rules
  are used for first boot or an explicit platform identity change; otherwise
  the active generation is reused. Golden rules are a bootstrap source only.
  Rejected inbox bytes are not promoted, and an activated generation is never
  replaced automatically because of later runtime failures.
- The active file is an atomically replaced regular file backed by immutable
  versioned copies. Every candidate is first copied to an immutable snapshot on
  the promotion filesystem; hashing, validation, failed-candidate archival, and
  promotion all use those same bytes. A staged inbox checksum must still match
  the watcher-accepted generation. The activation manifest records checksums,
  platform identity, source, active generation, and attempted inbox content.
- `activation.json` keeps an oldest-to-newest, additive `activation_attempts`
  list bounded to the 20 most recent candidates. Every attempt records `at`,
  `source`, `checksum`, `file_valid`, `usable_rule_count`, `broken_rule_count`,
  `validation_result`, `activation_result`, `reason`, and `errors`. An activated
  attempt also records `generation_path` and `active_checksum`.
  `last_attempt` mirrors the newest record;
  `last_activation` describes the current activation. This preserves rejected
  candidate and zero-usable-rule diagnostics when a later candidate succeeds.
- The watcher records and releases its lock before asking systemd to restart
  DLDD. It queues that restart with `systemctl --no-block`, allowing the
  `PartOf=dldd.service` watcher unit to exit before the restart transaction
  stops related units. This prevents lock and unit-transaction deadlocks.
- Broken-rule state may be restored only after an unclean process exit with the
  same generation.  Handled shutdown writes a clean marker; explicit restart,
  config reload, and activation reevaluate rules from scratch.

## Runtime ownership

- Monitor assignment maps are immutable.  A monitor alone mutates its state
  records.  The primary returns immutable commands through one queue per
  monitor and never writes monitor state directly.
- The FIFO is bounded.  If it is full, the monitor releases the key and retries
  later rather than holding ownership for evidence that was never delivered.
  Sample/source transition state is committed only after enqueue succeeds, so
  a saturated FIFO cannot permanently lose a clear or recovery transition.
- Active keys resume normal polling after evidence is processed.  There may be
  only one outstanding event per correlation key.  `HOLD` is reserved for
  candidate/action/recheck lifecycles.
- Primary rechecks have an acknowledgement deadline, are retried once, and then
  complete conservatively as active with a service diagnostic.  A lost queue
  entry or command cannot strand action publication or reconciliation.
- `BROKEN` keys do not recover in-process.  Retryable `DEGRADED` and unavailable
  sources continue polling and can recover.
- Competing signatures use severity, lower numeric priority, then first
  detection.  A winner change while the component/symptom stays active does not
  increment the fault occurrence count.
- Source-stale refresh and Redis retry publication pass through the same
  ownership check as initial arbitration; a suppressed signature cannot
  overwrite the winning component/symptom row.
- Retained inactive rows are loaded on restart so a later assertion preserves
  and increments occurrence history without resetting its TTL during startup.
- A source grace interval delays broken-rule accounting; it does not imply a
  planned maintenance event.  The optional trusted `source_lifecycle` hook can
  prove a source is in expected maintenance and drive `SUSPENDED`/resume; in
  its absence DLDD reports `graceful: false` conservatively.
- Globbed file sources are deterministic (sorted path order), and an event
  matches when any resolved file value matches its evaluator.  Evidence retains
  all resolved raw values.
- Byte values use the declared encoding when one exists.  Binary and hex values
  are rendered with `0b`/`0x`; otherwise raw bytes are emitted as an integer
  array so every payload remains lossless and JSON-safe.
- Candidate fault records exist only in process memory.  Their origin and event
  snapshots come from the first signature assertion; the final recheck supplies
  the controller-visible status and `last_detection_time`.
- Active records held conservatively through source loss use the additive
  `source_stale` fault field; process status exposes bounded exception counts
  rather than internal lease/work snapshots.
- Event histories are kept in event-time order.  Samples older than both the
  match and logic windows are discarded and counted in service diagnostics;
  an older clear cannot erase a newer match.

## Actions, logs, and artifacts

- Local actions execute sequentially and stop after the first failure or
  timeout.  Artifact collection and post-action recheck still occur.
- Action and artifact work use bounded daemon-worker queues.  Built-in calls
  honor declared timeouts; a non-cooperative vendor call can exhaust its
  bounded lane but cannot hold the DLDD process open during systemd restart.
- Primary action deadlines independently prevent a lost/stuck worker future
  from leaving a candidate held forever; the candidate proceeds through the
  required failed-action artifact, wait, and recheck path.
- Log-only rules request an artifact asynchronously and publish after signature
  confirmation without inventing a local-action wait/recheck phase.
- The initial artifact store is a bounded host filesystem integration. DLDD
  triggers generation, returns a stable reference, and writes one final file;
  it does not track or publish artifact lifecycle state. gNOI waits for and
  exposes safe opaque IDs from that fixed directory. A trusted platform may replace this client through
  `sonic_platform.dldd.create_artifact_client`; the returned
  `HealthzArtifactClient` can bridge a vendor Healthz implementation or apply
  platform retention/size policy without changing orchestration code.
- Artifact identifiers are exact opaque UUID-based filenames of the form
  `dldd-<32 lowercase hex>.tar.gz`. gNOI resolves only that generated form
  beneath the fixed artifact directory, rejects traversal/symlinks and private
  state manifests, and never accepts a rule-selected filesystem path. Legacy
  absolute Healthz debug-artifact paths remain confined to `/tmp/dump`.
- Artifact admission and final archives are bounded. Final publication uses an
  atomic replace; interrupted staging files are disposable and there are no
  sidecar manifests or startup lifecycle reconciliation.
- Each archive carries structured request metadata including the request
  timestamp, rule identity, symptom, and full component type/name context.
- Artifact log inputs are regular files opened without following symlinks.
  Directories are never recursively archived, and both logical input bytes and
  the completed archive are checked against the configured bound.
- Declared vendor/DSE query timeouts run through bounded daemon-call slots,
  matching local-action timeout containment. Queries with no declared timeout
  remain vendor-owned, as required by the schema contract.
- CLI sources/actions/queries use argv and `shell=False`.  Direct monitoring
  I2C operations are read-only; writes require an explicit local action or
  registered vendor hook.
- A list-valued direct-I2C action bus expands to sequential bus operations
  within the action's single overall timeout; results retain bus order.

## SONiC integration

- `dldd.service` is a static FEATURE-managed unit.  It binds to `sonic.target`,
  requires database/config setup for startup, and has no gNMI/gNOI lifecycle
  dependency.  Runtime Redis failures degrade local publication but are handled
  by the process when the database unit remains present.
- CONFIG_DB values take precedence over platform defaults and hardcoded values.
  Unsigned 32-bit values are accepted; monitor/recheck/ack/settle intervals and
  source recovery samples must be at least one.
- CONFIG_DB notifications cause a full `DLDD_CONFIG|global` reread. Monitor
  scheduling updates use a lock-safe API; primary-owned deadlines, source
  status, and inactive-row TTL refreshes are handed to the primary loop through
  a queue rather than mutated by the subscriber thread.
- OpenConfig remediation indices are zero-based list positions.  Only identities
  present in the upstream Healthz fault model are translated.  DLDD-only fields
  remain in `FAULT_INFO` and are not forced into unrelated OpenConfig leaves.
- gNOI File write access is extended only to the exact stable-inbox filename,
  and only `File.Put` may publish it. `TransferToRemote` and `Remove` reject the
  inbox target so every remote update uses the same bounded, hash-verified,
  mode-restricted, same-directory atomic replacement. The gNMI container
  receives a writable bind for the inbox directory only; watcher state is stored
  under the daemon-owned rules directory so watcher bookkeeping, promoted
  generations, and artifacts remain outside that writable boundary.
- gNOI File remains registered only when the gNMI deployment is write-enabled.
  Read-only deployments can still run packaged and locally provisioned rules.
- `healthz_artifact` is deliberate non-native `FAULT_INFO` metadata. A
  controller discovers the opaque ID through raw/native fault telemetry and
  then calls Healthz Artifact; this implementation does not invent a standard
  Healthz Get/List discovery contract absent from the HLD.
- `FAULT_INFO` logical updates use transactional `HSET` plus targeted `HDEL`
  and TTL changes, never `DEL` plus recreate.  Redis `expired` notifications are
  treated as deletes so inactive-retention expiry reaches ON_CHANGE clients.
