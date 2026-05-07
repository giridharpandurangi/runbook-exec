"""Shell executor for runbook-exec.

Runs shell commands via subprocess with timeout, output capture, and duration
measurement. Never raises — always returns an ExecutionResult.
"""

import subprocess
import sys
import time

from runbook_exec.models import ExecutionResult


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate a process and all its children.

    On Windows, `proc.terminate()` / `proc.kill()` only kills the cmd.exe
    shell spawned by shell=True, leaving grandchild processes running.
    `taskkill /F /T` kills the entire process tree.

    On POSIX, `proc.terminate()` followed by `proc.kill()` is sufficient
    because the shell forwards signals to its children.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        proc.terminate()


def run_command(command: str, timeout_seconds: int) -> ExecutionResult:
    """Execute a shell command and return captured output and metadata.

    Uses subprocess.Popen with shell=True so runbook commands can use pipes,
    redirects, and environment variables without modification.

    Args:
        command: Shell command string to execute.
        timeout_seconds: Maximum wall-clock seconds to allow before terminating.

    Returns:
        ExecutionResult with stdout, stderr, exit_code, duration_seconds, and
        timed_out. Never raises — callers check exit_code and timed_out.
    """
    start = time.monotonic()

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
            duration = time.monotonic() - start
            return ExecutionResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=proc.returncode,
                duration_seconds=duration,
                timed_out=False,
            )

        except subprocess.TimeoutExpired:
            # Kill the process tree so no orphan children remain, then drain
            # the pipes to collect whatever output was produced before the kill.
            _kill_process_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # Last resort — should not normally be reached.
                proc.kill()
                stdout, stderr = proc.communicate()

            duration = time.monotonic() - start
            return ExecutionResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=-1,
                duration_seconds=duration,
                timed_out=True,
            )

        except KeyboardInterrupt:
            # SIGINT received while waiting for the subprocess — terminate the
            # process tree and return a result with exit_code=-1 so the executor
            # can write an ABORT audit entry.
            _kill_process_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()

            duration = time.monotonic() - start
            return ExecutionResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=-1,
                duration_seconds=duration,
                timed_out=False,
            )

    except Exception:  # noqa: BLE001
        # Catch any unexpected error (e.g. Popen failure on bad shell) so we
        # never propagate an exception to the caller.
        duration = time.monotonic() - start
        return ExecutionResult(
            stdout="",
            stderr="",
            exit_code=-1,
            duration_seconds=duration,
            timed_out=False,
        )
