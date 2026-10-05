"""Unit tests for agent_events pub/sub module."""
import unittest
import queue
import threading
import time

import agent_events


class TestAgentEvents(unittest.TestCase):
    def setUp(self):
        # Clear all subscribers before each test
        agent_events._subscribers.clear()

    def test_subscribe_returns_queue(self):
        """Subscribe returns a fresh queue for a project."""
        q = agent_events.subscribe(123)
        self.assertIsInstance(q, queue.Queue)
        self.assertTrue(q.maxsize > 0)

    def test_unsubscribe_removes_queue(self):
        """Unsubscribe removes the queue from the project's set."""
        q = agent_events.subscribe(123)
        self.assertIn(q, agent_events._subscribers[123])
        agent_events.unsubscribe(123, q)
        self.assertNotIn(q, agent_events._subscribers.get(123, set()))

    def test_unsubscribe_cleans_empty_project(self):
        """Unsubscribe removes the project entry when last queue is removed."""
        q = agent_events.subscribe(123)
        agent_events.unsubscribe(123, q)
        self.assertNotIn(123, agent_events._subscribers)

    def test_emit_delivers_to_subscriber(self):
        """Emit delivers an event to all subscribers of a project."""
        q = agent_events.subscribe(123)
        agent_events.emit(123, {'type': 'task_changed', 'task_id': 456})
        evt = q.get_nowait()
        self.assertEqual(evt, {'type': 'task_changed', 'task_id': 456})

    def test_emit_does_not_deliver_to_other_project(self):
        """Emit to project A does not deliver to project B's subscribers."""
        q_a = agent_events.subscribe(123)
        q_b = agent_events.subscribe(456)
        agent_events.emit(123, {'type': 'task_changed', 'task_id': 789})
        # q_a should have the event
        self.assertEqual(q_a.get_nowait()['task_id'], 789)
        # q_b should be empty
        self.assertTrue(q_b.empty())

    def test_emit_drops_on_full_queue(self):
        """Emit drops events if a subscriber's queue is full (non-blocking)."""
        q = agent_events.subscribe(123)
        # Fill the queue
        for _ in range(q.maxsize):
            q.put_nowait({'filler': True})
        # This should not block or raise
        agent_events.emit(123, {'type': 'overflow'})
        # Queue should still be at maxsize (overflow dropped)
        self.assertEqual(q.qsize(), q.maxsize)

    def test_emit_safe_swallows_exceptions(self):
        """emit_safe never raises, even on invalid input."""
        # Should not raise even with bogus args
        agent_events.emit_safe(None, None)  # type: ignore

    def test_emit_global_delivers_to_all(self):
        """emit_global delivers to all project subscribers."""
        q_a = agent_events.subscribe(123)
        q_b = agent_events.subscribe(456)
        agent_events.emit_global({'type': 'work_session_changed', 'slot': 1})
        self.assertEqual(q_a.get_nowait()['type'], 'work_session_changed')
        self.assertEqual(q_b.get_nowait()['type'], 'work_session_changed')

    def test_emit_global_safe_swallows_exceptions(self):
        """emit_global_safe never raises."""
        agent_events.emit_global_safe(None)  # type: ignore

    def test_concurrent_subscribe_unsubscribe(self):
        """Concurrent subscribe/unsubscribe operations are thread-safe."""
        errors = []

        def worker(project_id):
            try:
                for _ in range(50):
                    q = agent_events.subscribe(project_id)
                    agent_events.emit(project_id, {'x': 1})
                    agent_events.unsubscribe(project_id, q)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i % 5,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
