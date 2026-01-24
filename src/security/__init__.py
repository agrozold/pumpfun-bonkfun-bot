"""Security module for agent restrictions and secrets management"""

from .file_guard import FileGuard, SecurityViolationError

__all__ = ['FileGuard', 'SecurityViolationError']
from .secrets_manager import SecretsManager, get_secrets_manager
