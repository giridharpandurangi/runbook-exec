"""Shared interrupt flag for SIGINT handling.

This module provides a process-wide flag that is set when SIGINT is received.
Using a separate module avoids circular imports between cli.py and executor.py.
"""

_interrupted: bool = False


def set_interrupted() -> None:
    """Set the interrupted flag (called from the SIGINT handler in cli.py)."""
    global _interrupted
    _interrupted = True


def is_interrupted() -> bool:
    """Return True if a SIGINT has been received."""
    return _interrupted


def reset() -> None:
    """Reset the flag to False.

    Intended for use in tests to avoid state leaking between test cases.
    """
    global _interrupted
    _interrupted = False
