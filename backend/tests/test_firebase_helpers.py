"""Tests for firebase helpers."""
import unittest

from app.firebase import heal_counter


class HealCounterTests(unittest.TestCase):
    def test_missing_and_negative_heal(self):
        self.assertEqual(heal_counter(None, 15), 15)
        self.assertEqual(heal_counter(-1, 15), 15)
        self.assertEqual(heal_counter("junk", 15), 15)

    def test_legit_values_pass_through(self):
        self.assertEqual(heal_counter(0, 15), 0)
        self.assertEqual(heal_counter(5, 15), 5)


if __name__ == "__main__":
    unittest.main()
