.PHONY: test test-py test-bats

test-py:
	python3 -m unittest discover -s tests -p 'test_*.py'

test-bats:
	bats tests/*.bats

test: test-py test-bats
