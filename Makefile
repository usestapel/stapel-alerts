# stapel-alerts — contract emission + drift gate (contract-pipeline.md §2-3).
#
# This module emits its OWN contract triad (schema.json + flows.json +
# errors.json) from a single-module {alerts + core} Django instance mounted at
# the canonical /alerts/api/v1/ prefix (see _codegen.py / _codegen_settings.py
# / codegen_urls.py).
#
# PYTHON must have the module + its deps importable (the workspace venv, or a
# CI venv). The authoritative gate is tests/test_contract.py under pytest;
# these targets are the dev-loop convenience.
PYTHON ?= python3

.PHONY: test contract contract-check migration-lint release-check

# The suite. Everything below is a gate ON it, not a substitute for it.
test:
	$(PYTHON) -m pytest tests/ -q

# Emit the contract triad into docs/.
contract:
	$(PYTHON) -m stapel_alerts._codegen --out docs

# Drift gate: regenerate into a temp dir and diff against the committed
# docs/*.json. One shell block, one exit code — a per-line recipe would stop
# at the first stale artifact and hide the rest, and the point of a drift gate
# is to name everything that needs regenerating in one run.
contract-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_alerts._codegen --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	rc=0; \
	for f in schema.json flows.json errors.json; do \
		if ! diff -q "docs/$$f" "$$tmp/$$f" >/dev/null 2>&1; then \
			echo "DRIFT: docs/$$f is stale — run 'make contract' and commit it"; \
			diff "docs/$$f" "$$tmp/$$f" | head -20; rc=1; \
		fi; \
	done; \
	rm -rf "$$tmp"; \
	if [ $$rc -eq 0 ]; then echo "contract-check: docs/{schema,flows,errors}.json up to date"; fi; \
	exit $$rc

# Expand/contract gate for Django migrations (release-management.md §3).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict

# Releases are cut from main only, and a release must contain every earlier one.
release-check:
	sh ./release-check.sh
