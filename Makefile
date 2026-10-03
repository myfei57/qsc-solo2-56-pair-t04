test:
	@if PYTHONPATH=. python -c "import pytest" 2>/dev/null; then \
		PYTHONPATH=. python -m pytest -q; \
	else \
		echo "pytest 不可用，使用零依赖 runner"; \
		PYTHONPATH=. python3 tools/run_tests.py; \
	fi

run:
	PYTHONPATH=. python -m psdline --config config/psdline.json serve --host 127.0.0.1 --port 8080

demo:
	PYTHONPATH=. python -m psdline --config config/psdline.json demo --fast

deadcode:
	PYTHONPATH=. python tools/deadcode.py psdline
