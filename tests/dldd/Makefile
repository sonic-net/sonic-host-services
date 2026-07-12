PYTHON ?= python3
ROOT := $(abspath $(CURDIR)/../..)
PYTEST := $(PYTHON) -m pytest -o addopts='' -p no:cacheprovider
UNIT_COVERAGE_JSON ?= /tmp/dldd-unit-coverage.json
INTEGRATION_COVERAGE_JSON ?= /tmp/dldd-integration-coverage.json
INTEGRATION_JUNIT_XML ?= dldd-integration-results.xml
DLDD_COVERAGE_FAIL_UNDER ?= 100
DLDD_COVERAGE_INCLUDE := dldd/*,scripts/dldd,scripts/dldd-rules-watch
DLDD_COVERAGE_CONFIG := tests/dldd/coverage.ini
DLDD_COVERAGE_SOURCES := --cov --cov-config=$(DLDD_COVERAGE_CONFIG)

.PHONY: all unit unit-ci coverage-gate integration unit-coverage \
	integration-coverage coverage-gaps

all: unit integration

unit:
	cd $(ROOT) && $(PYTEST) tests/dldd -m 'not dldd_integration'

# Run the repository's normal unit suite so scripts/ and host_modules/ retain
# their existing reports, then enforce the DLDD-only line and branch result.
# pytest.ini excludes the explicitly separate integration marker and enables
# branch coverage for this invocation.
unit-ci:
	cd $(ROOT) && $(PYTHON) -m pytest
	$(MAKE) coverage-gate PYTHON=$(PYTHON) \
		DLDD_COVERAGE_FAIL_UNDER=$(DLDD_COVERAGE_FAIL_UNDER)

coverage-gate:
	cd $(ROOT) && $(PYTHON) -m coverage report \
		--include='$(DLDD_COVERAGE_INCLUDE)' \
		--fail-under=$(DLDD_COVERAGE_FAIL_UNDER)

integration:
	cd $(ROOT) && $(PYTEST) tests/dldd/integration -m dldd_integration \
		--junitxml=$(INTEGRATION_JUNIT_XML)

unit-coverage:
	cd $(ROOT) && $(PYTEST) tests/dldd -m 'not dldd_integration' \
		$(DLDD_COVERAGE_SOURCES) --cov-branch --cov-report=term-missing \
		--cov-report=json:$(UNIT_COVERAGE_JSON)

integration-coverage:
	cd $(ROOT) && $(PYTEST) tests/dldd/integration -m dldd_integration \
		--cov=dldd --cov-branch --cov-report=term-missing \
		--cov-report=json:$(INTEGRATION_COVERAGE_JSON)

# Print the independently measured DLDD unit line-and-branch result.
coverage-gaps: unit-coverage
