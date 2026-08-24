PYTHON ?= python3
ROOT := $(abspath $(CURDIR)/../..)
PYTEST := $(PYTHON) -m pytest -o addopts='' -p no:cacheprovider
INTEGRATION_JUNIT_XML ?= dldd-integration-results.xml

.PHONY: all unit unit-ci integration

all: unit integration

unit:
	cd $(ROOT) && $(PYTEST) tests/dldd -m 'not dldd_integration'

unit-ci:
	cd $(ROOT) && $(PYTHON) -m pytest

integration:
	cd $(ROOT) && $(PYTEST) tests/dldd/integration -m dldd_integration \
		--junitxml=$(INTEGRATION_JUNIT_XML)
