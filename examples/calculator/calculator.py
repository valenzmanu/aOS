def add(left: float, right: float) -> float:
    """Return the sum of two numbers."""
    return left + right


def divide(numerator: float, denominator: float) -> float:
    """Divide two numbers, rejecting a zero denominator."""
    if denominator == 0:
        raise ValueError("denominator must not be zero")
    return numerator / denominator
