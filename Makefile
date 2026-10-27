.PHONY: test test-01 package

test:
	./scripts/run_tests.sh -q

test-01:
	python3 -m pytest -q tests/test_01.py

# zip of just the two deliverables, as the packet asks
package:
	zip -j submission.zip 01.py 02.sql
