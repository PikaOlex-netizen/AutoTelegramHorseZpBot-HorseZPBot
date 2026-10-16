.PHONY: install start stop restart test lint format

install:
	./install.sh

start:
	./start.sh

stop:
	./stop.sh

restart:
	./restart.sh

test:
	.venv/bin/python -m unittest discover -s tests -v

lint:
	.venv/bin/python -m compileall -q app tests

format:
	.venv/bin/python -m pip install ruff
	.venv/bin/python -m ruff format app tests
