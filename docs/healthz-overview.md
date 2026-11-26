# How Healthz works in this implementation

We kept the ownership simple. Healthz is an independent host service, and DLDD
is one producer using it. DLDD owns rules, fault decisions, queries and custom
logging. Healthz owns retained events, archives, acknowledgement and component
health state. The gNOI handlers use the existing host D-Bus boundary for metadata
and stream artifacts back to remote consumers.

## The DLDD flow

1. Once a rule fault is selected and ready for publication, DLDD requests an
   artifact ID through host Healthz's `reserve_artifact` method if the rule calls
   for collection. The only Healthz-specific field we add to `FAULT_INFO` is `healthz_artifact_id`.
2. DLDD publishes the fault row and a generic `HEALTHZ_TRANSITIONS` record
   together. Host Healthz consumes the stream and publishes
   `COMPONENT_HEALTH_INFO`. The existing gNMI mapping exposes OpenConfig
   `healthz/state/{status,last-unhealthy,unhealthy-count}` for GET and ON_CHANGE.
   This happens without a remote RPC being called.
3. Collection runs asynchronously alongside publication. DLDD stages its
   rule-specific query results, selected logs and action output. It gives
   Healthz concrete file paths through the private `submit_artifact` D-Bus API.
   Healthz packages and retains the archive; DLDD cleans up its staging files.
4. A monitor can discover events through Get/List, download their archives
   through Artifact, and acknowledge the events. Acknowledge does not delete
   the archive. Retention is bounded and prefers reclaiming acknowledged
   archives. The ID can appear in telemetry before packaging completes;
   Artifact supports a bounded wait for a pending archive.

Our ID rule is that a new archive ID is also the event ID and first
`ArtifactHeader.id`. A recovery carrying an earlier archive ID gets a separate
persisted event ID and does not advertise that archive again. Events without a
new archive also get persisted opaque IDs.

Healthz keeps one small SQLite catalog under `/var/lib/sonic/healthz` for events,
acknowledgements, source membership, aggregate state and the stream checkpoint.
It does not read `FAULT_INFO`; DLDD handles its own fault reconciliation.
Overlapping faults keep a component unhealthy until the last active source
clears. Routine refreshes create no event; confirmed observations can advance
`last-unhealthy` without another count increment. Lost stream history is reported
as a gap, and missing rows are never treated as proof of recovery.

## What upstream defines

We pin the gNOI Go module to **v0.3.0**. Its proto defines
`gnoi.healthz.Healthz` and declares Healthz service version **1.3.0**—those are
separate version numbers. The standard component path is
`/components/component[name=X]`. See the [pinned proto](https://github.com/openconfig/gnoi/blob/97f56280571337f6122b8c30c6bdd93368c57b54/healthz/healthz.proto)
and [upstream design](https://github.com/openconfig/gnoi/blob/97f56280571337f6122b8c30c6bdd93368c57b54/healthz/README.md).

| RPC | Request fields | Response / behavior |
|---|---|---|
| Get | `path` | `component`: latest stored `ComponentStatus` |
| List | `path`, `include_acknowledged` | `statuses[]`: retained events; acknowledged entries excluded by default |
| Acknowledge | `path`, `id` | `status`: updated `ComponentStatus`; idempotent |
| Artifact | `id` | Server stream of `ArtifactResponse` |
| Check | `path`, optional `event_id` | `status`: result of a component-specific validation procedure |

The other four RPCs are unary. `ComponentStatus` carries the component path,
status, event ID, artifact headers, acknowledgement, creation/optional expiry
timestamps and subcomponent statuses. The status enum is `STATUS_UNSPECIFIED`,
`STATUS_HEALTHY` or `STATUS_UNHEALTHY`. Artifact responses use a `oneof` for
header, byte data, protobuf data or trailer. We serve gzip archives as header →
one or more byte frames → trailer, with filename, MIME type, size and SHA-256.

Upstream Check takes a component path and an optional existing event ID. It has
no fields for arbitrary log paths or rule queries. Our file handoff is a private
host API; the OpenConfig wire proto is unchanged. Standard Check for DLDD
components remains Unimplemented until we define an actual component validation
procedure. Existing diagnostic selectors remain a separate compatibility path.
Collecting logs alone does not establish that a component is healthy.

Implementation details: [SONiC Healthz HLD](https://github.com/gregoryboudreau/SONiC/blob/923150a7f5d0a2ed12595134a50fb7ae958d16c5/doc/mgmt/gnmi/gnoi_healthz_hld.md).
