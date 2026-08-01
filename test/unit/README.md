# test/unit/ — pytest unit tests

Pure-logic tests that need neither restic nor root: manifest parsing and
validation, sandbox path mapping, hash stability, the fuzzy matcher.
Run with `make unit` (or `pytest test/unit -q`).
pytest docs: https://docs.pytest.org/
