from __future__ import annotations

import unittest
import threading
from unittest.mock import Mock, patch

from app.core.process_tree import ProcessTreeHandle


class ProcessTreeHandleTest(unittest.TestCase):
    def test_concurrent_terminate_and_close_only_close_windows_job_once(self) -> None:
        handle = ProcessTreeHandle.__new__(ProcessTreeHandle)
        handle.process = Mock(pid=1234)
        job = Mock()
        handle.windows_job = job
        handle._lock = threading.Lock()
        barrier = threading.Barrier(3)

        def terminate() -> None:
            barrier.wait()
            handle.terminate()

        def close() -> None:
            barrier.wait()
            handle.close()

        threads = [threading.Thread(target=terminate), threading.Thread(target=close)]
        with patch("app.core.process_tree.os.name", "nt"):
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        job.close.assert_called_once_with()

    def test_posix_terminate_kills_process_group_after_root_exit(self) -> None:
        handle = ProcessTreeHandle.__new__(ProcessTreeHandle)
        handle.process = Mock(pid=1234)
        handle.process.poll.return_value = 0
        handle.windows_job = None
        handle._lock = threading.Lock()

        with patch("app.core.process_tree.os.name", "posix"), patch(
            "app.core.process_tree.os.killpg", create=True
        ) as kill_group, patch("app.core.process_tree.signal.SIGKILL", 9, create=True):
            handle.terminate()

        kill_group.assert_called_once_with(1234, 9)

    def test_windows_terminate_closes_job_after_root_exit(self) -> None:
        handle = ProcessTreeHandle.__new__(ProcessTreeHandle)
        handle.process = Mock(pid=1234)
        handle.process.poll.return_value = 0
        job = Mock()
        handle.windows_job = job
        handle._lock = threading.Lock()

        with patch("app.core.process_tree.os.name", "nt"):
            handle.terminate()

        job.close.assert_called_once_with()
        self.assertIsNone(handle.windows_job)


if __name__ == "__main__":
    unittest.main()
