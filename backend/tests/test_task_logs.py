"""Tests for the per-task log tail (no Redis, no worker needed)."""
import asyncio
import json
import logging
import unittest
from unittest.mock import MagicMock, patch

import app.workers.task_logs as tl
from app.routes.scrape import get_job_status


def _record(message, level=logging.INFO, name="app.workers.tasks"):
    return logging.LogRecord(name, level, __file__, 1, message, None, None)


class BufferTests(unittest.TestCase):
    def setUp(self):
        self._old_client = tl._redis_client
        # Fail closed: no test may touch a real Redis.
        tl._redis_client = MagicMock()
        tl._buffers.clear()
        if hasattr(tl._state, "stack"):
            del tl._state.stack
        self.addCleanup(self._restore)

    def _restore(self):
        tl._redis_client = self._old_client
        tl._buffers.clear()
        if hasattr(tl._state, "stack"):
            del tl._state.stack
        root = logging.getLogger()
        for handler in [h for h in root.handlers if isinstance(h, tl.TaskLogTailHandler)]:
            root.removeHandler(handler)

    def test_emit_without_active_task_buffers_nothing(self):
        tl.TaskLogTailHandler().emit(_record("hello"))
        self.assertEqual(tl._buffers, {})

    def test_emit_buffers_for_active_task(self):
        tl._on_task_prerun(task_id="t1")
        tl.TaskLogTailHandler().emit(_record("hello"))
        self.assertEqual(len(tl._buffers["t1"]["lines"]), 1)
        self.assertIn("hello", tl._buffers["t1"]["lines"][0])
        tl._on_task_postrun(task_id="t1")

    def test_buffer_caps_at_max_lines(self):
        tl._redis_client = MagicMock()
        tl._on_task_prerun(task_id="t1")
        handler = tl.TaskLogTailHandler()
        for n in range(tl.LOG_TAIL_MAX_LINES + 20):
            handler.emit(_record(f"line {n}"))
        self.assertEqual(len(tl._buffers["t1"]["lines"]), tl.LOG_TAIL_MAX_LINES)
        self.assertTrue(tl._buffers["t1"]["lines"][-1].endswith("line 119"))
        tl._on_task_postrun(task_id="t1")

    def test_lines_truncated(self):
        tl._on_task_prerun(task_id="t1")
        tl.TaskLogTailHandler().emit(_record("x" * 2000))
        self.assertLessEqual(len(tl._buffers["t1"]["lines"][0]), tl.LOG_TAIL_LINE_MAX_CHARS)
        tl._on_task_postrun(task_id="t1")

    def test_nested_tasks_attribute_to_innermost(self):
        tl._on_task_prerun(task_id="outer")
        tl._on_task_prerun(task_id="inner")
        tl.TaskLogTailHandler().emit(_record("inner line"))
        self.assertEqual(tl._buffers["inner"]["lines"] and len(tl._buffers["inner"]["lines"]), 1)
        self.assertEqual(tl._buffers["outer"]["lines"], [])
        tl._on_task_postrun(task_id="inner")
        tl.TaskLogTailHandler().emit(_record("outer line"))
        self.assertEqual(len(tl._buffers["outer"]["lines"]), 1)
        tl._on_task_postrun(task_id="outer")

    def test_flush_every_n_lines(self):
        client = MagicMock()
        tl._redis_client = client
        tl._on_task_prerun(task_id="t1")
        handler = tl.TaskLogTailHandler()
        for n in range(tl.FLUSH_EVERY_LINES - 1):
            handler.emit(_record(f"line {n}"))
        client.setex.assert_not_called()
        handler.emit(_record("line last"))
        client.setex.assert_called_once()
        key, _ttl, payload = client.setex.call_args[0]
        self.assertEqual(key, tl.LOG_TAIL_KEY_PREFIX + "t1")
        self.assertEqual(len(json.loads(payload)), tl.FLUSH_EVERY_LINES)
        tl._on_task_postrun(task_id="t1")

    def test_postrun_flushes_remainder_and_clears(self):
        client = MagicMock()
        tl._redis_client = client
        tl._on_task_prerun(task_id="t1")
        tl.TaskLogTailHandler().emit(_record("only line"))
        tl._on_task_postrun(task_id="t1")
        client.setex.assert_called_once()
        self.assertNotIn("t1", tl._buffers)

    def test_redis_failure_swallowed(self):
        client = MagicMock()
        client.setex.side_effect = RuntimeError("redis down")
        tl._redis_client = client
        tl._on_task_prerun(task_id="t1")
        handler = tl.TaskLogTailHandler()
        for n in range(tl.FLUSH_EVERY_LINES):
            handler.emit(_record(f"line {n}"))
        tl._on_task_postrun(task_id="t1")

    def test_install_idempotent(self):
        tl.install()
        tl.install()
        count = sum(
            isinstance(h, tl.TaskLogTailHandler) for h in logging.getLogger().handlers
        )
        self.assertEqual(count, 1)

    def test_logging_setup_hook_reattaches(self):
        root = logging.getLogger()
        for h in [h for h in root.handlers if isinstance(h, tl.TaskLogTailHandler)]:
            root.removeHandler(h)
        tl._on_logging_setup()
        self.assertTrue(
            any(isinstance(h, tl.TaskLogTailHandler) for h in root.handlers)
        )

    def test_survives_logging_reconfig(self):
        log = logging.getLogger("test.e2e")
        tl.install()
        root = logging.getLogger()
        for h in [h for h in root.handlers if isinstance(h, tl.TaskLogTailHandler)]:
            root.removeHandler(h)
        tl._on_task_prerun(task_id="t9")
        log.warning("before hook")
        self.assertEqual(tl._buffers["t9"]["lines"], [])
        tl._on_logging_setup()
        log.warning("after hook")
        self.assertEqual(len(tl._buffers["t9"]["lines"]), 1)
        self.assertIn("after hook", tl._buffers["t9"]["lines"][0])
        tl._on_task_postrun(task_id="t9")


class ReadTailTests(unittest.TestCase):
    def setUp(self):
        self._old_client = tl._redis_client
        tl._redis_client = None
        self.addCleanup(self._restore)

    def _restore(self):
        tl._redis_client = self._old_client

    def test_returns_parsed_tail_capped(self):
        client = MagicMock()
        client.get.return_value = json.dumps([f"l{n}" for n in range(10)])
        tl._redis_client = client
        self.assertEqual(tl.read_job_log_tail("j1", limit=3), ["l7", "l8", "l9"])
        client.get.assert_called_once_with(tl.LOG_TAIL_KEY_PREFIX + "j1")

    def test_missing_key_returns_empty(self):
        client = MagicMock()
        client.get.return_value = None
        tl._redis_client = client
        self.assertEqual(tl.read_job_log_tail("j1"), [])

    def test_corrupt_payload_returns_empty(self):
        client = MagicMock()
        client.get.return_value = "not json{"
        tl._redis_client = client
        self.assertEqual(tl.read_job_log_tail("j1"), [])

    def test_no_redis_returns_empty(self):
        with patch.object(tl, "_get_redis", return_value=None):
            self.assertEqual(tl.read_job_log_tail("j1"), [])


class JobStatusLogsTests(unittest.TestCase):
    def test_status_includes_log_tail(self):
        result = MagicMock()
        result.ready.return_value = False
        result.status = "RUNNING"
        with (
            patch("app.routes.scrape.AsyncResult", return_value=result),
            patch(
                "app.routes.scrape.read_job_log_tail", return_value=["a", "b"]
            ) as reader,
        ):
            out = asyncio.run(get_job_status("j1"))
        reader.assert_called_once_with("j1")
        self.assertEqual(out.logs, ["a", "b"])
        self.assertEqual(out.status, "running")


if __name__ == "__main__":
    unittest.main()
