.PHONY: help gate hygiene lint lint-baseline test preverify hooks
.DEFAULT_GOAL := help
.NOTPARALLEL:

# Interpreter for `make test`; empty means whatever uv picks (UV_PYTHON or the default).
PYTHON ?=
# Extra arguments for preverify.sh, for example `make preverify PREVERIFY_ARGS=--allow-dirty`.
PREVERIFY_ARGS ?=

help:
	@printf '%s\n' \
		'Targets:' \
		'  make gate           Run every gate: hygiene, lint, test, preverify' \
		'  make hygiene        Scrub tracked files and the commit range for personal data and secrets' \
		'  make lint           Run ruff; fail beyond scripts/gates/ruff-baseline.txt or on an unpinned GitHub Action' \
		'  make lint-baseline  Rewrite the ruff baseline from the current tree' \
		'  make test           Run the isolated test suite (cell tests skip without logins)' \
		'  make preverify      Fresh clone of HEAD: suite on Python 3.11 and 3.14, then lint and hygiene' \
		'  make hooks          Install the pre-push hook (hygiene and lint before any push) into .git/hooks' \
		'Variables: PYTHON=3.11 (make test), PREVERIFY_ARGS=--allow-dirty, GATES_BASE=<ref>, GATES_PRIVATE_DIR=<dir>'

gate: hygiene lint test preverify

hygiene:
	python3 scripts/gates/hygiene.py

lint:
	python3 scripts/gates/lint.py

lint-baseline:
	python3 scripts/gates/lint.py --update-baseline

test:
	uv run $(if $(PYTHON),--python $(PYTHON),) --frozen --extra test python -m unittest discover -s tests -t .

preverify:
	sh scripts/gates/preverify.sh $(PREVERIFY_ARGS)

hooks:
	hooks_dir="$$(git rev-parse --git-common-dir)/hooks"; mkdir -p "$$hooks_dir" \
	  && ln -sfn "$$(pwd)/scripts/hooks/pre-push" "$$hooks_dir/pre-push" \
	  && echo "installed $$hooks_dir/pre-push -> scripts/hooks/pre-push"
