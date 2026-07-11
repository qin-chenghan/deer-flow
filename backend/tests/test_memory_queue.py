import threading
import time
from unittest.mock import MagicMock, call, patch

from deerflow.agents.memory.backends.deermem.deermem.config import DeerMemConfig
from deerflow.agents.memory.backends.deermem.deermem.core.queue import ConversationContext, MemoryUpdateQueue


def _queue(updater: MagicMock | None = None) -> MemoryUpdateQueue:
    """A MemoryUpdateQueue with DI config + a (mock) updater; timer disabled."""
    return MemoryUpdateQueue(DeerMemConfig(), updater or MagicMock())


def test_queue_add_preserves_existing_correction_flag_for_same_thread() -> None:
    queue = _queue()
    with patch.object(queue, "_reset_timer"):
        queue.add(thread_id="thread-1", messages=["first"], correction_detected=True)
        queue.add(thread_id="thread-1", messages=["second"], correction_detected=False)

    assert len(queue._queue) == 1
    assert queue._queue[0].messages == ["second"]
    assert queue._queue[0].correction_detected is True


def test_process_queue_forwards_correction_flag_to_updater() -> None:
    mock_updater = MagicMock()
    mock_updater.update_memory.return_value = True
    queue = _queue(mock_updater)
    queue._queue = [
        ConversationContext(thread_id="thread-1", messages=["conversation"], agent_name="lead_agent", correction_detected=True)
    ]

    queue._process_queue()

    mock_updater.update_memory.assert_called_once_with(
        messages=["conversation"], thread_id="thread-1", agent_name="lead_agent",
        correction_detected=True, reinforcement_detected=False, user_id=None, trace_id=None,
    )


def test_queue_add_preserves_existing_reinforcement_flag_for_same_thread() -> None:
    queue = _queue()
    with patch.object(queue, "_reset_timer"):
        queue.add(thread_id="thread-1", messages=["first"], reinforcement_detected=True)
        queue.add(thread_id="thread-1", messages=["second"], reinforcement_detected=False)

    assert len(queue._queue) == 1
    assert queue._queue[0].messages == ["second"]
    assert queue._queue[0].reinforcement_detected is True


def test_process_queue_forwards_reinforcement_flag_to_updater() -> None:
    mock_updater = MagicMock()
    mock_updater.update_memory.return_value = True
    queue = _queue(mock_updater)
    queue._queue = [
        ConversationContext(thread_id="thread-1", messages=["conversation"], agent_name="lead_agent", reinforcement_detected=True)
    ]

    queue._process_queue()

    mock_updater.update_memory.assert_called_once_with(
        messages=["conversation"], thread_id="thread-1", agent_name="lead_agent",
        correction_detected=False, reinforcement_detected=True, user_id=None, trace_id=None,
    )


def test_flush_nowait_cancels_existing_timer_and_starts_immediate_timer() -> None:
    queue = _queue()
    existing_timer = MagicMock()
    queue._timer = existing_timer
    created_timer = MagicMock()

    with patch("deerflow.agents.memory.backends.deermem.deermem.core.queue.threading.Timer", return_value=created_timer) as timer_cls:
        queue.flush_nowait()

    existing_timer.cancel.assert_called_once_with()
    timer_cls.assert_called_once_with(0, queue._process_queue)
    assert created_timer.daemon is True
    created_timer.start.assert_called_once_with()
    assert queue._timer is created_timer


def test_add_nowait_cancels_existing_timer_and_starts_immediate_timer() -> None:
    queue = _queue()
    existing_timer = MagicMock()
    queue._timer = existing_timer
    created_timer = MagicMock()

    with patch("deerflow.agents.memory.backends.deermem.deermem.core.queue.threading.Timer", return_value=created_timer) as timer_cls:
        queue.add_nowait(thread_id="thread-1", messages=["conversation"], agent_name="lead-agent")

    existing_timer.cancel.assert_called_once_with()
    timer_cls.assert_called_once_with(0, queue._process_queue)
    assert queue.pending_count == 1
    assert queue._queue[0].agent_name == "lead-agent"
    assert created_timer.daemon is True
    created_timer.start.assert_called_once_with()


def test_process_queue_reschedules_immediately_when_already_processing() -> None:
    queue = _queue()
    queue._processing = True
    created_timer = MagicMock()

    with patch("deerflow.agents.memory.backends.deermem.deermem.core.queue.threading.Timer", return_value=created_timer) as timer_cls:
        queue._process_queue()

    timer_cls.assert_called_once_with(0, queue._process_queue)
    assert created_timer.daemon is True
    created_timer.start.assert_called_once_with()


def test_flush_nowait_is_non_blocking() -> None:
    queue = _queue()
    started = threading.Event()
    finished = threading.Event()

    def _slow_process_queue() -> None:
        started.set()
        time.sleep(0.2)
        finished.set()

    queue._process_queue = _slow_process_queue

    start = time.perf_counter()
    queue.flush_nowait()
    elapsed = time.perf_counter() - start

    assert started.wait(0.1) is True
    assert elapsed < 0.1
    assert finished.is_set() is False
    assert finished.wait(1.0) is True


def test_queue_keeps_updates_for_different_agents_in_same_thread() -> None:
    queue = _queue()
    with patch.object(queue, "_reset_timer"):
        queue.add(thread_id="thread-1", messages=["agent-a"], agent_name="agent-a")
        queue.add(thread_id="thread-1", messages=["agent-b"], agent_name="agent-b")

    assert queue.pending_count == 2
    assert [context.agent_name for context in queue._queue] == ["agent-a", "agent-b"]


def test_queue_still_coalesces_updates_for_same_agent_in_same_thread() -> None:
    queue = _queue()
    with patch.object(queue, "_reset_timer"):
        queue.add(thread_id="thread-1", messages=["first"], agent_name="agent-a", correction_detected=True)
        queue.add(thread_id="thread-1", messages=["second"], agent_name="agent-a", correction_detected=False)

    assert queue.pending_count == 1
    assert queue._queue[0].agent_name == "agent-a"
    assert queue._queue[0].messages == ["second"]
    assert queue._queue[0].correction_detected is True


def test_process_queue_updates_different_agents_in_same_thread_separately() -> None:
    queue = _queue()
    with patch.object(queue, "_reset_timer"):
        queue.add(thread_id="thread-1", messages=["agent-a"], agent_name="agent-a")
        queue.add(thread_id="thread-1", messages=["agent-b"], agent_name="agent-b")

    mock_updater = MagicMock()
    mock_updater.update_memory.return_value = True
    queue._updater = mock_updater

    with patch("deerflow.agents.memory.backends.deermem.deermem.core.queue.time.sleep"):
        queue.flush()

    assert mock_updater.update_memory.call_count == 2
    mock_updater.update_memory.assert_has_calls(
        [
            call(messages=["agent-a"], thread_id="thread-1", agent_name="agent-a",
                 correction_detected=False, reinforcement_detected=False, user_id=None, trace_id=None),
            call(messages=["agent-b"], thread_id="thread-1", agent_name="agent-b",
                 correction_detected=False, reinforcement_detected=False, user_id=None, trace_id=None),
        ]
    )


def test_process_queue_forwards_trace_id_to_updater() -> None:
    mock_updater = MagicMock()
    mock_updater.update_memory.return_value = True
    queue = _queue(mock_updater)
    queue._queue = [
        ConversationContext(thread_id="thread-1", messages=["conversation"], agent_name="lead_agent", trace_id="trace-memory-1")
    ]

    queue._process_queue()

    mock_updater.update_memory.assert_called_once_with(
        messages=["conversation"], thread_id="thread-1", agent_name="lead_agent",
        correction_detected=False, reinforcement_detected=False, user_id=None, trace_id="trace-memory-1",
    )
