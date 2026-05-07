"""Tests for runbook_exec.shell — subprocess execution with timeout and output capture."""

import sys
import time

import pytest

from runbook_exec.models import ExecutionResult
from runbook_exec.shell import run_command


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Platform-appropriate commands
if sys.platform == "win32":
    _ECHO_STDOUT = 'echo hello stdout'
    _ECHO_STDERR = 'echo hello stderr 1>&2'
    _FAIL_CMD = 'exit 1'
    # ping with a large count is a reliable cross-environment sleep on Windows;
    # it doesn't require an interactive terminal unlike `timeout /t`.
    _SLEEP_CMD = 'ping -n 30 127.0.0.1 > nul'
    _STDOUT_AND_STDERR = 'echo to stdout & echo to stderr 1>&2'
else:
    _ECHO_STDOUT = 'echo "hello stdout"'
    _ECHO_STDERR = 'echo "hello stderr" >&2'
    _FAIL_CMD = 'exit 1'
    _SLEEP_CMD = 'sleep 10'
    _STDOUT_AND_STDERR = 'echo "to stdout"; echo "to stderr" >&2'


# ---------------------------------------------------------------------------
# Successful command
# ---------------------------------------------------------------------------

class TestSuccessfulCommand:
    def test_exit_code_zero(self):
        result = run_command(_ECHO_STDOUT, timeout_seconds=10)
        assert result.exit_code == 0

    def test_stdout_captured(self):
        result = run_command(_ECHO_STDOUT, timeout_seconds=10)
        assert "hello stdout" in result.stdout

    def test_stderr_empty_on_clean_command(self):
        result = run_command(_ECHO_STDOUT, timeout_seconds=10)
        assert result.stderr == ""

    def test_duration_positive(self):
        result = run_command(_ECHO_STDOUT, timeout_seconds=10)
        assert result.duration_seconds > 0

    def test_timed_out_false(self):
        result = run_command(_ECHO_STDOUT, timeout_seconds=10)
        assert result.timed_out is False

    def test_returns_execution_result(self):
        result = run_command(_ECHO_STDOUT, timeout_seconds=10)
        assert isinstance(result, ExecutionResult)


# ---------------------------------------------------------------------------
# Failing command
# ---------------------------------------------------------------------------

class TestFailingCommand:
    def test_nonzero_exit_code(self):
        result = run_command(_FAIL_CMD, timeout_seconds=10)
        assert result.exit_code != 0

    def test_stderr_captured(self):
        result = run_command(_ECHO_STDERR, timeout_seconds=10)
        assert "hello stderr" in result.stderr

    def test_stdout_empty_when_only_stderr(self):
        result = run_command(_ECHO_STDERR, timeout_seconds=10)
        assert result.stdout == ""

    def test_timed_out_false_on_failure(self):
        result = run_command(_FAIL_CMD, timeout_seconds=10)
        assert result.timed_out is False

    def test_duration_positive_on_failure(self):
        result = run_command(_FAIL_CMD, timeout_seconds=10)
        assert result.duration_seconds > 0


# ---------------------------------------------------------------------------
# stdout and stderr captured separately
# ---------------------------------------------------------------------------

class TestSeparateStreams:
    def test_stdout_and_stderr_captured_independently(self):
        result = run_command(_STDOUT_AND_STDERR, timeout_seconds=10)
        assert "to stdout" in result.stdout
        assert "to stderr" in result.stderr

    def test_stderr_not_in_stdout(self):
        result = run_command(_STDOUT_AND_STDERR, timeout_seconds=10)
        assert "to stderr" not in result.stdout

    def test_stdout_not_in_stderr(self):
        result = run_command(_STDOUT_AND_STDERR, timeout_seconds=10)
        assert "to stdout" not in result.stderr


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

class TestTimeout:
    def test_timed_out_flag_set(self):
        result = run_command(_SLEEP_CMD, timeout_seconds=1)
        assert result.timed_out is True

    def test_returns_within_deadline(self):
        timeout = 1
        start = time.monotonic()
        run_command(_SLEEP_CMD, timeout_seconds=timeout)
        elapsed = time.monotonic() - start
        # Must return within timeout + 1s grace period
        assert elapsed < timeout + 1.0

    def test_exit_code_negative_one_on_timeout(self):
        result = run_command(_SLEEP_CMD, timeout_seconds=1)
        assert result.exit_code == -1

    def test_duration_recorded_on_timeout(self):
        result = run_command(_SLEEP_CMD, timeout_seconds=1)
        assert result.duration_seconds > 0

    def test_returns_execution_result_on_timeout(self):
        result = run_command(_SLEEP_CMD, timeout_seconds=1)
        assert isinstance(result, ExecutionResult)


# ---------------------------------------------------------------------------
# Never raises
# ---------------------------------------------------------------------------

class TestNeverRaises:
    def test_invalid_command_returns_result_not_exception(self):
        # A command that doesn't exist — shell returns non-zero, no exception
        result = run_command("this_command_does_not_exist_xyz", timeout_seconds=10)
        assert isinstance(result, ExecutionResult)
        assert result.exit_code != 0

    def test_empty_command_returns_result(self):
        # Empty string — shell exits immediately
        result = run_command("", timeout_seconds=10)
        assert isinstance(result, ExecutionResult)


# ---------------------------------------------------------------------------
# _kill_process_tree — platform-specific paths
# ---------------------------------------------------------------------------

class TestKillProcessTree:
    def test_kill_process_tree_windows_path(self):
        """On Windows, _kill_process_tree calls taskkill /F /T."""
        import subprocess
        from unittest.mock import MagicMock, patch
        from runbook_exec.shell import _kill_process_tree

        mock_proc = MagicMock()
        mock_proc.pid = 12345

        with patch("runbook_exec.shell.sys.platform", "win32"), \
             patch("subprocess.run") as mock_run:
            _kill_process_tree(mock_proc)

        mock_run.assert_called_once_with(
            ["taskkill", "/F", "/T", "/PID", "12345"],
            capture_output=True,
        )

    def test_kill_process_tree_posix_path(self):
        """On POSIX, _kill_process_tree calls proc.terminate()."""
        from unittest.mock import MagicMock, patch
        from runbook_exec.shell import _kill_process_tree

        mock_proc = MagicMock()

        with patch("runbook_exec.shell.sys.platform", "linux"):
            _kill_process_tree(mock_proc)

        mock_proc.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# Timeout — second TimeoutExpired (last resort kill)
# ---------------------------------------------------------------------------

class TestTimeoutLastResort:
    def test_second_timeout_expired_triggers_kill(self):
        """When proc.communicate() times out twice, proc.kill() is called."""
        import subprocess
        from unittest.mock import MagicMock, patch, call

        mock_proc = MagicMock()
        # First communicate: raises TimeoutExpired (triggers kill tree)
        # Second communicate (after kill tree): also raises TimeoutExpired (last resort)
        # Third communicate (after proc.kill): returns empty strings
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="sleep", timeout=1),
            subprocess.TimeoutExpired(cmd="sleep", timeout=5),
            ("", ""),
        ]
        mock_proc.returncode = -1

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("runbook_exec.shell._kill_process_tree"):
            result = run_command("sleep 100", timeout_seconds=1)

        # proc.kill() should have been called as last resort
        mock_proc.kill.assert_called()
        assert result.timed_out is True
        assert result.exit_code == -1


# ---------------------------------------------------------------------------
# KeyboardInterrupt — second TimeoutExpired path
# ---------------------------------------------------------------------------

class TestKeyboardInterruptTimeout:
    def test_keyboard_interrupt_with_second_timeout(self):
        """KeyboardInterrupt during communicate, then second TimeoutExpired → proc.kill()."""
        import subprocess
        from unittest.mock import MagicMock, patch

        mock_proc = MagicMock()
        # First communicate: raises KeyboardInterrupt
        # Second communicate (after kill tree): raises TimeoutExpired
        # Third communicate (after proc.kill): returns empty strings
        mock_proc.communicate.side_effect = [
            KeyboardInterrupt(),
            subprocess.TimeoutExpired(cmd="sleep", timeout=2),
            ("", ""),
        ]
        mock_proc.returncode = -1

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("runbook_exec.shell._kill_process_tree"):
            result = run_command("sleep 100", timeout_seconds=10)

        mock_proc.kill.assert_called()
        assert result.exit_code == -1
        assert result.timed_out is False
