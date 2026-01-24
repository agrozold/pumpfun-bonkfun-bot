"""Security module for agent restrictions and secrets management"""

from .file_guard import FileGuard, SecurityViolationError

__all__ = ['FileGuard', 'SecurityViolationError']
