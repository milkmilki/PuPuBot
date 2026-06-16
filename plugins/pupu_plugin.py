"""Thin NoneBot entry that wires up commands, loops, and adapter handlers."""

from pupu.logging_utils import setup_runtime_logging
from pupu.memory import init_db

setup_runtime_logging()
init_db()

from .pupu_support import commands as _commands  # noqa: F401
from .pupu_support import lifecycle as _lifecycle  # noqa: F401
from .pupu_support import onebot_handlers as _onebot_handlers  # noqa: F401
from .pupu_support import qq_official_handlers as _qq_official_handlers  # noqa: F401
