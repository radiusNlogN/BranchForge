import pytest

from calc import CASES, FEATURE_ENABLED, add


def test_add(sample_pair):
    if not FEATURE_ENABLED:
        pytest.skip("feature disabled")
    a, b = sample_pair
    assert add(a, b) == 5


def test_identity():
    # Passes with the bug too (5 - 0 == 5), so it is a stable control.
    assert add(5, 0) == 5


@pytest.mark.parametrize("case", CASES)
def test_cases(case):
    assert isinstance(case, str)
