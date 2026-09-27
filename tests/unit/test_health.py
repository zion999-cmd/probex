"""BookHealth 状态机测试。"""

from __future__ import annotations

import unittest

from market.health.state import (
    ALLOWED_TRANSITIONS,
    BookHealth,
    HealthTransition,
    IllegalHealthTransition,
    can_transition,
    require_transition,
)


class BookHealthTransitionTest(unittest.TestCase):
    def test_exactly_four_states(self) -> None:
        self.assertEqual(
            [state.value for state in BookHealth],
            ["awaiting_snapshot", "healthy", "stale", "resyncing"],
        )
        self.assertEqual(set(ALLOWED_TRANSITIONS), set(BookHealth))

    def test_legal_transitions(self) -> None:
        legal = {
            (BookHealth.AWAITING_SNAPSHOT, BookHealth.RESYNCING),
            (BookHealth.RESYNCING, BookHealth.HEALTHY),
            (BookHealth.RESYNCING, BookHealth.STALE),
            (BookHealth.HEALTHY, BookHealth.STALE),
            (BookHealth.HEALTHY, BookHealth.RESYNCING),
            (BookHealth.STALE, BookHealth.RESYNCING),
        }
        for source in BookHealth:
            for target in BookHealth:
                self.assertEqual(
                    can_transition(source, target),
                    (source, target) in legal,
                    msg=f"{source.value} -> {target.value}",
                )

    def test_self_transition_is_not_a_transition(self) -> None:
        for state in BookHealth:
            self.assertFalse(can_transition(state, state))

    def test_healthy_cannot_go_back_to_awaiting_snapshot(self) -> None:
        self.assertFalse(can_transition(BookHealth.HEALTHY, BookHealth.AWAITING_SNAPSHOT))

    def test_stale_cannot_go_directly_to_healthy(self) -> None:
        self.assertFalse(can_transition(BookHealth.STALE, BookHealth.HEALTHY))

    def test_require_transition_raises_on_illegal(self) -> None:
        with self.assertRaises(IllegalHealthTransition):
            require_transition(BookHealth.STALE, BookHealth.HEALTHY)

    def test_require_transition_accepts_legal(self) -> None:
        require_transition(BookHealth.STALE, BookHealth.RESYNCING)
        require_transition(BookHealth.AWAITING_SNAPSHOT, BookHealth.RESYNCING)

    def test_transition_record_is_immutable(self) -> None:
        record = HealthTransition(
            from_health=BookHealth.STALE,
            to_health=BookHealth.RESYNCING,
            reason="resync_requested",
        )
        with self.assertRaises(AttributeError):
            record.reason = "other"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
