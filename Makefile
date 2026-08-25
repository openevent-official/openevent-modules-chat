PYTHON ?= python3

.PHONY: build test install verify-wheel clean

build:
	rm -rf dist
	PIP_NO_COMPILE=1 PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .

test:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src $(PYTHON) -B -m unittest discover -s tests

install: build
	$(PYTHON) -m pip install $(INSTALL_ARGS) $$(find dist -maxdepth 1 -type f -name '*.whl' | sort | tail -n 1)

verify-wheel: build
	rm -rf build/wheel-test
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m pip install -q --no-deps --target build/wheel-test $$(find dist -maxdepth 1 -type f -name '*.whl' | sort | tail -n 1)
	test ! -e build/wheel-test/openevent/__init__.py
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=build/wheel-test:$$PYTHONPATH $(PYTHON) -B -c 'import openevent.sdk, openevent.chat_sdk'

clean:
	rm -rf build dist src/*.egg-info
