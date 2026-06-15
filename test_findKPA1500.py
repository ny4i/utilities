#!/usr/bin/env python3
"""Unit tests for findKPA1500 discovery-reply acceptance.

Run: python3 -m unittest test_findKPA1500 -v

These guard the sole identity gate (is_kpa1500_reply): only the documented
^ON; responses are accepted, and in particular a bare ^ON; echo of our own
probe must be rejected.
"""
import unittest

import findKPA1500 as k


class IsKPA1500ReplyTest(unittest.TestCase):
    def test_accepts_documented_replies(self):
        self.assertTrue(k.is_kpa1500_reply(b"^ON1;"))  # powered on
        self.assertTrue(k.is_kpa1500_reply(b"^ON0;"))  # powered off

    def test_tolerates_surrounding_whitespace(self):
        # Real UDP frames may carry trailing CR/LF or padding.
        self.assertTrue(k.is_kpa1500_reply(b"^ON1;\r\n"))
        self.assertTrue(k.is_kpa1500_reply(b"  ^ON0;  "))

    def test_rejects_blind_echo_of_probe(self):
        # The probe itself bounced back is the key false-positive to kill.
        self.assertFalse(k.is_kpa1500_reply(k.PROBE_COMMAND))
        self.assertFalse(k.is_kpa1500_reply(b"^ON;"))

    def test_rejects_other_caret_frames(self):
        self.assertFalse(k.is_kpa1500_reply(b"^FOO;"))
        self.assertFalse(k.is_kpa1500_reply(b"^ON2;"))   # undocumented state
        self.assertFalse(k.is_kpa1500_reply(b"^RVM03.06;"))

    def test_rejects_garbage_and_empty(self):
        self.assertFalse(k.is_kpa1500_reply(b""))
        self.assertFalse(k.is_kpa1500_reply(b"ON1;"))     # missing caret
        self.assertFalse(k.is_kpa1500_reply(b"^ON1"))     # missing terminator
        self.assertFalse(k.is_kpa1500_reply(b"random udp"))

    def test_replies_constant_matches_probe_contract(self):
        # Documents the protocol contract: probe is ^ON;, replies add the state.
        self.assertEqual(k.PROBE_COMMAND, b"^ON;")
        self.assertEqual(set(k.KPA1500_REPLIES), {b"^ON0;", b"^ON1;"})


if __name__ == "__main__":
    unittest.main()
