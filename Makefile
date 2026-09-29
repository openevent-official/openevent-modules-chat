PYTHON ?= python3
NODE ?= node
export PYTHONDONTWRITEBYTECODE := 1
export PIP_NO_COMPILE := 1
export TMPDIR := $(abspath build/tmp)
export TMP := $(TMPDIR)
export TEMP := $(TMPDIR)
export PIP_CACHE_DIR := $(abspath build/cache/pip)
.DEFAULT_GOAL := test

.PHONY: prepare prepare-test-env install-browser-deps check-sdk check-docs test test-python test-sdk test-app test-package test-frontend build install-deps install verify-wheel e2e browser-e2e clean
prepare:
	mkdir -p "$(TMPDIR)"

prepare-test-env: prepare
	$(PYTHON) -B -m venv --system-site-packages build/test-env

install-browser-deps: prepare
	$(PYTHON) -B -m pip install --no-compile playwright

check-sdk:
	$(PYTHON) -B scripts/check_sdk.py

check-docs:
	$(PYTHON) -B scripts/check_docs.py

test: test-python test-frontend check-docs

test-python: check-sdk prepare
	PYTHONPATH=src $(PYTHON) -B -m unittest discover -s tests -p 'test_*.py'

test-sdk: check-sdk prepare
	PYTHONPATH=src $(PYTHON) -B -m unittest discover -s tests -p 'test_client.py'
	PYTHONPATH=src $(PYTHON) -B -m unittest discover -s tests -p 'test_codec.py'

test-app: check-sdk prepare
	PYTHONPATH=src $(PYTHON) -B -m unittest discover -s tests -p 'test_config.py'
	PYTHONPATH=src $(PYTHON) -B -m unittest discover -s tests -p 'test_app.py'

test-package: prepare
	$(PYTHON) -B -m unittest discover -s tests -p 'test_package.py'

test-frontend: prepare
	$(NODE) --test tests/frontend/*.test.mjs

build: check-sdk
	$(PYTHON) -B scripts/package.py build

install: build
	$(PYTHON) -B scripts/package.py install $(INSTALL_ARGS)

install-deps: build
	$(PYTHON) -B scripts/package.py dependencies

verify-wheel: build
	$(PYTHON) -B scripts/package.py verify

e2e: verify-wheel
	PYTHONPATH=build/wheel-test $(PYTHON) -B scripts/e2e.py

browser-e2e: verify-wheel
	CHAT_BROWSER_E2E=1 PYTHONPATH=build/wheel-test:$(BROWSER_DEPS) $(PYTHON) -B scripts/e2e.py

clean:
	$(PYTHON) -B scripts/package.py clean
