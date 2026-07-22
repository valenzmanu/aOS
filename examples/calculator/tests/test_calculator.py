import unittest

from calculator import add, divide


class CalculatorTests(unittest.TestCase):
    def test_add(self) -> None:
        self.assertEqual(add(2, 3), 5)

    def test_divide(self) -> None:
        self.assertEqual(divide(9, 3), 3)

    def test_divide_by_zero(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be zero"):
            divide(1, 0)


if __name__ == "__main__":
    unittest.main()
