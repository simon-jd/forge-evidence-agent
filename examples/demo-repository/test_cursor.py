"""Public, trusted acceptance tests. No private evaluator inputs are needed."""

import unittest

from replay import Cursor, Event, cursor_for, replay_page


class PageContractTests(unittest.TestCase):
    def test_first_page_sorts_full_key_and_preserves_payload(self):
        events = [Event(1001, 30, "done"), Event(1000, 12, "chunk-b"), Event(1000, 11, "chunk-a")]
        self.assertEqual(replay_page(events, None, 2), [events[2], events[1]])
        self.assertEqual(events[0].payload, "done")

    def test_empty_and_bounded_page_size(self):
        self.assertEqual(replay_page([], None), [])
        for limit in [0, -1, 501]:
            with self.assertRaises(ValueError):
                replay_page([], None, limit)

    def test_cursor_roundtrip(self):
        self.assertEqual(cursor_for(Event(42, 7, "data")), Cursor(42, 7))


class ResumeCursorTests(unittest.TestCase):
    def test_resume_keeps_remaining_events_at_same_millisecond(self):
        events = [Event(1000, 11, "read"), Event(1000, 12, "patch"), Event(1001, 20, "verify")]
        self.assertEqual([event.sequence for event in replay_page(events, Cursor(1000, 11))], [12, 20])

    def test_complete_cursor_boundary_is_exclusive(self):
        events = [Event(1000, 11, "read"), Event(1000, 12, "patch"), Event(1001, 20, "verify")]
        self.assertEqual([event.sequence for event in replay_page(events, Cursor(1000, 12))], [20])

    def test_repeated_small_pages_neither_drop_nor_duplicate(self):
        events = [Event(1000 + index // 3, index, f"chunk-{index}") for index in range(11)]
        events.reverse()
        observed, after = [], None
        while page := replay_page(events, after, limit=2):
            observed.extend(event.sequence for event in page)
            after = cursor_for(page[-1])
        self.assertEqual(observed, list(range(11)))


if __name__ == "__main__":
    unittest.main()
