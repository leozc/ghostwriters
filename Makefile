# ghostwriter

DRAFT ?= draft.md

# Setup
init:
	uv sync
	uv run data.py init "$(DRAFT)"

# Iteration commands
new:
	uv run data.py new "$(DESC)"

status:
	uv run data.py status

score:
	uv run score.py scores.json

eval:
	uv run evaluate.py

finalize-keep:
	uv run data.py finalize keep

finalize-discard:
	uv run data.py finalize discard
	git reset --hard HEAD~1

# Testing
test:
	uv run python -m unittest discover -p 'test_*.py' -v

# Cleanup
clean:
	rm -rf data/

.PHONY: init new status score eval finalize-keep finalize-discard test clean
