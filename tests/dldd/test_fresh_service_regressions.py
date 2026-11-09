"""Process outcomes consumed by FEATURE and generic host-service health."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from dldd import cli, service as service_module
from dldd.dse import DSERegistry
from dldd.hooks import VendorHookRegistry
from dldd.lifecycle import RulePaths
from dldd.platform import PlatformExtensions, PlatformIdentity
from dldd.service import DLDDService, NOT_CONFIGURED_EXIT_STATUS
from dldd.validation import ExactCompatibilityMatcher
from tests.dldd_fakes import FakeStateDB


def _service(tmp_path):
    return DLDDService(
        paths=RulePaths(
            platform_dir=str(tmp_path / 'platform'),
            inbox=str(tmp_path / 'inbox' / 'dld_rules.yaml'),
            rules_dir=str(tmp_path / 'rules'),
            state_file=str(tmp_path / 'state.json'),
        ),
        config_db=SimpleNamespace(get_table=lambda unused_table: {}),
        state_db=FakeStateDB(),
        extensions=PlatformExtensions(
            PlatformIdentity('test-platform', 'product', 'software'),
            DSERegistry(), VendorHookRegistry(), ExactCompatibilityMatcher(),
        ),
    )


def test_absent_rules_returns_only_the_not_configured_outcome(tmp_path):
    service = _service(tmp_path)

    assert service.run() == NOT_CONFIGURED_EXIT_STATUS == 3
    assert service.stop_event.is_set()
    assert service.activation is None
    assert service.orchestrator is None
    assert not service.fatal_reason
    assert not service.state_db.hgetall('DLDD_STATUS|process_state')
    assert not Path(service.paths.state_file).exists()


def test_present_invalid_rules_never_claim_not_configured_success(tmp_path):
    service = _service(tmp_path)
    candidate = Path(service.paths.packaged)
    candidate.parent.mkdir()
    candidate.write_text('not: [a valid rules document]\n')
    # An operator stop may end the fatal diagnostic process cleanly. Its
    # ordinary exit 0 must remain distinguishable from absent configuration.
    service.stop_event.set()

    assert service.run() == 0
    assert service.fatal_reason
    assert service.state_db.hgetall('DLDD_STATUS|process_state')['state'] == 'BROKEN|FATAL'


def test_service_crash_propagates_as_failure(tmp_path, monkeypatch):
    service = _service(tmp_path)

    def crash():
        raise RuntimeError('configured startup crashed')

    monkeypatch.setattr(service, 'start', crash)
    with pytest.raises(RuntimeError, match='configured startup crashed'):
        service.run()


@pytest.mark.parametrize('argv', [[], ['run']])
def test_cli_preserves_the_real_not_configured_process_outcome(
    tmp_path, monkeypatch, argv
):
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, 'DLDDService', lambda **unused: service)
    monkeypatch.setattr(service_module.signal, 'signal', lambda *unused: None)

    assert cli.main(argv) == 3
