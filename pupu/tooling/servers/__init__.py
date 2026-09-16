"""Builtin tool servers."""

from .filesystem import FILESYSTEM_SERVER
from .scheduler import SCHEDULER_SERVER
from .system import SYSTEM_SERVER


def get_builtin_servers():
    return (
        FILESYSTEM_SERVER,
        SYSTEM_SERVER,
        SCHEDULER_SERVER,
    )
