PYTHON ?= python3
TEST_ARGS ?=
INSTALL_ARGS ?=

.PHONY: help build install test e2e verify-wheel clean

help:
	@printf 'Usage: make <target>\n\n'
	@printf 'Targets:\n'
	@printf '  build    Build the wheel without installing it\n'
	@printf '  install  Build and install the wheel with pip\n'
	@printf '  test     Run unit tests\n'
	@printf '  e2e      Run tests against a configured OpenEvent server\n'
	@printf '  verify-wheel  Build and verify the installed namespace layout\n'
	@printf '  clean    Remove build artifacts\n'

build:
	PYTHON="$(PYTHON)" ./build.sh

install: build
	@wheel="$$(find dist -maxdepth 1 -type f -name '*.whl' | sort | tail -n 1)"; \
	if [ -z "$$wheel" ]; then printf 'no wheel found in dist\n'; exit 1; fi; \
	"$(PYTHON)" -m pip install $(INSTALL_ARGS) "$$wheel"

test:
	PYTHON="$(PYTHON)" ./test.sh $(TEST_ARGS)

e2e:
	PYTHON="$(PYTHON)" ./test-e2e.sh $(TEST_ARGS)

verify-wheel: build
	@rm -rf build/wheel-test; \
	wheel="$$(find dist -maxdepth 1 -type f -name '*.whl' | sort | tail -n 1)"; \
	PYTHONDONTWRITEBYTECODE=1 PIP_NO_COMPILE=1 "$(PYTHON)" -m pip install -q --no-deps --target build/wheel-test "$$wheel"; \
	test ! -e build/wheel-test/openevent/__init__.py; \
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$$PWD/build/wheel-test" "$(PYTHON)" -B -c 'import openevent.chat_sdk, openevent.sdk'

clean:
	rm -rf build dist
