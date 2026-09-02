# DLDD Test Structure

DLDD uses separate test tiers because they answer different questions.

## Unit tests

The existing files directly under `tests/dldd/` are build-time unit and
contract tests. They replace Redis, time, files, subprocesses, hooks, and other
external boundaries with deterministic fakes. A useful unit test asserts both
the result and observable state or side effects. The suite retains representative
normal behavior, primary failures, and distinct security, wire, concurrency,
persistence, cleanup, and recovery boundaries; it does not enumerate equivalent
field, type, default, or formatting permutations for coverage alone.

Run the focused unit suite with:

```console
make -C tests/dldd unit
```

The repository and wheel-build unit entry point is:

```console
make -C tests/dldd unit-ci
```

It runs the normal repository pytest command and therefore retains the
repository's established reporting policy. Integration tests carry the
`dldd_integration` marker and remain an explicit, separate invocation. DLDD
does not add a feature-specific coverage threshold to existing repository or
image-build policy.

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

### Rule conformance corpus

The checked-in rule fixtures have deliberately different deployment scopes:

- `fixtures/all-supported-rule-types.yaml` is the portable conformance corpus.
  It covers every schema source, evaluator, value encoding, action/query shape,
  logic form, timing form, and supported DSE composition mode using controlled
  source and vendor doubles. It is hardware-neutral qualification data, not a
  platform rules generation, and must not be installed on a DUT.
- `fixtures/mixed-sensor-rules.yaml` is the only fixture intended for controlled
  activation on the identified lab DUT. It is an integration fixture, not a
  second schema conformance corpus: three DSE rules discover live sensor
  inventory and thresholds instead of copying a target snapshot. Executable
  rules carry `dut-live`; the
  two `dut-schema-sentinel` rules intentionally remain non-executable to prove
  rule isolation, so `DEGRADED` is the expected activation result. Its valid
  rules perform read-only collection and declare remote recommendations only.
  `DLDD_DUT_RULE_INSTANCE_BROKEN` is an executable runtime sentinel: it
  deterministically produces a non-retryable evaluation error after resolving
  `DLDD_RULE_INSTANCE_TEST`, so operator output must identify the broken work
  as `9999302@DLDD_RULE_INSTANCE_TEST`. Use the fixture in an isolated lab
  because an external controller could consume its recommendations.
- `fixtures/dut-unsupported-extension-rules.yaml` is the software-pinned DUT
  extension failure catalog. Run it only with `activation-dry-run`; never
  install it or use hardware-probe/e2e modes. One Redis control must survive
  while each `broken-*` case proves an unsupported platform/DSE reference is
  localized. Resolution details belong to the vendor implementation, so common
  tests assert categories rather than vendor error text.

Conformance tests derive finite wire values from the installed contract and
inspect the actual event, evaluation, and operation fields. Metadata tags stay
descriptive rather than forming a second coverage authority. Fatal envelope
cases such as an unsupported `schema_version`, duplicate rule identity,
duplicate YAML key, or forbidden alias remain separate parser/unit inputs
because any one of them correctly rejects the complete file and cannot coexist
in a degraded-but-usable rules generation.

Rule-local failures are synthesized from the canonical valid rule fixture with
small named mutation tables. Missing, unsupported, semantic, materialization,
and preflight failures remain independently identifiable without duplicating
complete rule documents. Mixed valid/broken inputs still prove that one bad
rule does not prevent usable siblings from activating.

## Runtime integration tests

`tests/dldd/integration/` runs the real service, lifecycle manager, monitors,
primary orchestrator, queues, worker ownership, correlation, persistence, and
telemetry serializers together. Only the external ConfigDB, StateDB, source,
and artifact boundaries are replaced.

Current deterministic scenarios cover:

- no-rules clean exit, invalid-without-fallback fatal startup, usable active
  fallback, and mixed valid/broken activation;
- healthy threshold match, clear, persisted state, and clean shutdown;
- source-read failure and recovery without a false hardware fault;
- bounded telemetry publication failure followed by unclean non-zero shutdown;
- restart reconciliation of an existing static active fault without a new
  lifetime;
- one generation spanning Redis, file, sysfs, CLI, I2C, and Platform API
  routing through the real adapters with only external I/O replaced;
- authoritative DSE expansion, live value/comparator sampling, fault activation,
  and retained inactive retirement after child removal;
- current-generation DSE restart reconciliation only after expansion registers
  the dynamic execution, without a false clear or new lifetime;
- asynchronous action, wait, priority recheck, and publication ordering; and
- replacement of an unexpectedly exited monitor with the same plan plus a
  service diagnostic while the process remains healthy.

Tier-1 tests own semantic variants that reuse those integrated lifecycles,
including stale-generation retirement, non-authoritative discovery omission,
expected-maintenance classification, current-truth/lookback variants, cadence
updates, queue saturation, arbitration, reset ownership, and detailed bounds.

Run integration tests with:

```console
make -C tests/dldd integration
```

The integration target writes `dldd-integration-results.xml` for a separate
test run and clears repository-wide pytest options so it exercises only this
tier.
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
