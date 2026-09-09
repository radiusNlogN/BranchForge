"""A deliberately tiny package with one known bug, used to verify the verifier."""

FEATURE_ENABLED = True
CASES = ["alpha", "beta"]


def add(a, b):
    # The bug: subtraction where addition is meant.
    return a - b


def describe():
    return "sample calculator"
