import unittest

from playground.greet import greet


class GreetTests(unittest.TestCase):
    def test_with_name(self):
        self.assertEqual(greet("정두"), "안녕하세요, 정두님")

    def test_with_empty_name(self):
        self.assertEqual(greet(""), "안녕하세요")


if __name__ == "__main__":
    unittest.main()
