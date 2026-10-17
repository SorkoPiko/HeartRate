import unittest

import helpers  # noqa: F401  (puts the repo root on sys.path)
from sources import ble


class ParseTest(unittest.TestCase):
    def test_uint8(self):
        self.assertEqual(ble.parse([0x00, 72]), 72)

    def test_uint8_ignores_trailing_fields(self):
        # Flags with RR intervals present (bit 4), value still uint8
        self.assertEqual(ble.parse([0x10, 65, 0x00, 0x04]), 65)

    def test_uint16(self):
        self.assertEqual(ble.parse([0x01, 0x2C, 0x01]), 300)

    def test_uint16_low_value(self):
        self.assertEqual(ble.parse([0x01, 72, 0x00]), 72)


if __name__ == "__main__":
    unittest.main()
