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
#: llms.txt token budget. Raise it when the surface genuinely grows; the
#: emitter fails rather than silently truncating.
LLMS_BUDGET ?= 5000

.PHONY: test contract contract-check readme migration-lint release-check

# The suite. Everything below is a gate ON it, not a substitute for it.
test:
	$(PYTHON) -m pytest tests/ -q

# Emit the contract artifacts into docs/, in dependency order: the triad
# first (capabilities reads schema.json), then capabilities (llms.txt reads
# capabilities.json), then llms.txt, then the README that links all of them.
contract:
	$(PYTHON) -m stapel_alerts._codegen --out docs
	$(PYTHON) -m stapel_alerts._capabilities --out docs
	$(PYTHON) -m stapel_tools.llms_txt . --out docs --budget $(LLMS_BUDGET)
	$(PYTHON) -m stapel_tools.readme .

# README.md alone, for a prose-only edit to docs/readme.md.
readme:
	$(PYTHON) -m stapel_tools.readme .

# Drift gate: regenerate into a temp dir and diff against the committed
# docs/*.json. One shell block, one exit code — a per-line recipe would stop
# at the first stale artifact and hide the rest, and the point of a drift gate
# is to name everything that needs regenerating in one run.
contract-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_alerts._codegen --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	$(PYTHON) -m stapel_alerts._capabilities --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	$(PYTHON) -m stapel_tools.llms_txt . --out "$$tmp" --budget $(LLMS_BUDGET) || { rm -rf "$$tmp"; exit 1; }; \
	rc=0; \
	for f in schema.json flows.json errors.json capabilities.json llms.txt; do \
		if ! diff -q "docs/$$f" "$$tmp/$$f" >/dev/null 2>&1; then \
			echo "DRIFT: docs/$$f is stale — run 'make contract' and commit it"; \
			diff "docs/$$f" "$$tmp/$$f" | head -20; rc=1; \
		fi; \
	done; \
	rm -rf "$$tmp"; \
	$(PYTHON) -m stapel_tools.readme . --check || rc=1; \
	if [ $$rc -eq 0 ]; then echo "contract-check: docs/{schema,flows,errors,capabilities}.json + llms.txt + README.md up to date"; fi; \
	exit $$rc

# Expand/contract gate for Django migrations (release-management.md §3).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict

# Releases are cut from main only, and a release must contain every earlier one.
release-check:
	sh ./release-check.sh
