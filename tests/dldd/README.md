# DLDD Test Structure

DLDD uses separate test tiers because they answer different questions. Coverage
from one tier must not conceal a gap in another tier.

## Unit tests

The existing files directly under `tests/dldd/` are build-time unit and
contract tests. They replace Redis, time, files, subprocesses, hooks, and other
external boundaries with deterministic fakes. A useful unit test asserts both
the result and observable state or side effects, and includes normal, boundary,
negative, exception, timeout, and cleanup behavior where the production branch
supports those outcomes.

Run them without integration coverage:

```console
make -C tests/dldd unit
make -C tests/dldd unit-coverage
```

The repository and wheel-build unit entry point is:

```console
make -C tests/dldd unit-ci
```

It runs the normal repository pytest command, retaining the existing terminal,
HTML, and XML coverage for `scripts/`, `host_modules/`, and `dldd/`. It then
checks the same unit-only coverage data with a DLDD-specific line-and-branch
gate. Integration tests carry the `dldd_integration` marker and are excluded
from this data, so they cannot raise the enforced percentage.

The enforced set is the `dldd/` package plus the two installed executable
wrappers, `scripts/dldd` and `scripts/dldd-rules-watch`. No unrelated host
service script is included in that percentage. `tests/dldd/coverage.ini`
defines the same collection boundary used by the focused coverage command.

Unit tests follow the PMON daemon convention of organizing around a public
behavior rather than creating one test function for every branch. One test may
drive a component through its normal, boundary, failure, and recovery states in
sequence when those states form one contract. Small data tables inside that
test are preferred for cases with identical setup and failure semantics.
Separate named parameter rows remain appropriate when an input is an
independent wire-format or security contract whose individual identity is
useful in CI. Shared rule, plan, database, service, and orchestrator factories
remove repeated construction. Large parameter values always use short explicit
`ids`, so collection, JUnit, and CI output remain bounded and readable.

Canonical rule-document helpers and the Redis-accurate state database fake live
in `tests/dldd_fakes.py`. Contract-specific helpers should remain near their
own tests until at least two behavior groups share them; avoid both copied setup
and one giant implicit `conftest.py`.

`unit-coverage` records line and branch data in
`/tmp/dldd-unit-coverage.json` by default and prints every missing line. The
report is intentionally not combined with integration coverage. The build gate
is enforced at 100% and may remain there only through meaningful behavioral
tests; import-only calls or assertions written solely to move the number do not
qualify.

## Runtime integration tests

`tests/dldd/integration/` runs the real service, lifecycle manager, monitors,
primary orchestrator, queues, worker ownership, correlation, persistence, and
telemetry serializers together. Only the external ConfigDB, StateDB, source,
and artifact boundaries are replaced.

Current deterministic scenarios cover:

- clean service exit when no rules source exists, contrasted with a fatal
  result for a present invalid candidate with no fallback, and activation of a
  usable fallback when one exists;
- one valid and one Pydantic-invalid rule in the same generation, proving
  localized `BROKEN` telemetry while the valid rule remains executable and the
  service activates as `DEGRADED`;
- healthy startup, fake threshold fault activation, clear, and clean shutdown;
- one six-rule service generation covering Redis, file, sysfs, CLI, I2C, and
  Platform API monitor routing, healthy collection, a controlled CLI fault,
  and clear through real adapters with only external I/O replaced;
- authoritative DSE expansion into two runtime children, live source/comparator
  callbacks, fault activation, and retained inactive retirement with a reason
  after child removal;
- runtime-DSE restart reconciliation after expansion without changing the
  retained fault lifetime, authoritative removal of an already inactive DSE
  instance after restart with a refreshed reason/TTL, and a mixed
  DSE/common-Redis rule with exactly the real discovered scopes and
  source-specific cadence defaults;
- empty DSE inventory progression into stable backoff;
- non-authoritative DSE omission retaining both the runtime child and its active
  fault;
- source database read failure and recovery without a false fault;
- retryable source failures progressing from `DEGRADED` to `BROKEN`, including
  the configured service-level `BROKEN|FATAL` limit;
- expected platform maintenance suspending a source without breaking its rule,
  followed by normal recovery, while a lifecycle-hook exception falls back to
  ordinary unavailable handling;
- transient telemetry/reconciliation reads failing and then recovering without
  terminating the service;
- persistent telemetry write failure and unclean non-zero service failure;
- persistent startup fault-scan failure stopping before monitor threads start;
- restart reconciliation of a retained active fault without a new lifetime;
- restart retirement of active faults whose generation checksum or schema no
  longer matches, retaining the row as `INACTIVE` with an explicit reason;
- default reset versus `--all` ownership behavior, including foreign fault and
  artifact preservation;
- asynchronous local action, wait, and priority recheck completion before fault
  publication, plus bounded queue saturation and single-flight behavior;
- zero-lookback current-truth correlation and positive historical lookback;
- live inherited polling-cadence updates without postponing already due work;
- fault arbitration and promotion of a still-active alternate rule; and
- replacement of an unexpectedly stopped monitor while preserving its plan.

Run integration tests and their independent diagnostic coverage with:

```console
make -C tests/dldd integration
make -C tests/dldd integration-coverage
```

The normal integration target writes `dldd-integration-results.xml` for a
separate CI test run. It clears the repository pytest coverage options, so its
runtime execution cannot alter the unit coverage data or the 100% DLDD gate.

The second command writes `/tmp/dldd-integration-coverage.json`. Its percentage
describes integration reach only and is never used to fill unit-test gaps.
Safe complete-hash replacement reads the prior field set before writing, so a
permanent read outage may also prevent publication from completing; the fatal
criterion is inability to complete publication, not the socket operation name.

## On-demand CLI qualification tests

`test_cli_qualification_real.py` sends real YAML through the public CLI parser,
exact Pydantic contract, materializer, shared activation preflight, adapters,
DSE expansion, collection, comparison, and rule correlation. Its end-to-end
case executes one direct event plus two independently valued DSE instances and
asserts both source/comparator calls and all three final results. It verifies
the read boundary of each progressive mode and proves `e2e-execute` does not
mutate the supplied rules or platform directory.

Focused callback-level cases remain in `test_cli_e2e.py`. Both levels are
needed: focused tests precisely isolate failures, while actual-file tests catch
contract or wiring drift.
