"""
SecretsManager - централизованное управление секретами
Поддержка различных провайдеров: env, file, vault
"""

import os
import logging
from abc import ABC, abstractmethod
from typing import Optional, Protocol
from pathlib import Path

logger = logging.getLogger(__name__)


class SecretsProvider(Protocol):
    """Протокол провайдера секретов"""

    def get_secret(self, key: str) -> Optional[str]:
        """Получить секрет по ключу"""
        ...

    def is_available(self) -> bool:
        """Проверка доступности провайдера"""
        ...


class EnvSecretsProvider:
    """Провайдер секретов из переменных окружения"""

    def __init__(self, prefix: str = ''):
        self.prefix = prefix

    def get_secret(self, key: str) -> Optional[str]:
        full_key = f"{self.prefix}{key}" if self.prefix else key
        return os.environ.get(full_key)

    def is_available(self) -> bool:
        return True


class FileSecretsProvider:
    """Провайдер секретов из файла"""

    def __init__(self, filepath: str):
        self.filepath = Path(filepath)
        self._secrets = {}
        self._load()

    def _load(self) -> None:
        """Загрузить секреты из файла"""
        if not self.filepath.exists():
            logger.warning(f"Secrets file not found: {self.filepath}")
            return

        try:
            import json
            with open(self.filepath, 'r') as f:
                self._secrets = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load secrets: {e}")

    def get_secret(self, key: str) -> Optional[str]:
        return self._secrets.get(key)

    def is_available(self) -> bool:
        return self.filepath.exists()


class VaultSecretsProvider:
    """Провайдер секретов из HashiCorp Vault (опционально)"""

    def __init__(self, url: str, token: str, mount_point: str = 'secret'):
        self.url = url
        self.token = token
        self.mount_point = mount_point
        self._client = None
        self._init_client()

    def _init_client(self) -> None:
        """Инициализация Vault клиента"""
        try:
            import hvac
            self._client = hvac.Client(url=self.url, token=self.token)
        except ImportError:
            logger.warning("hvac not installed, Vault provider unavailable")
        except Exception as e:
            logger.error(f"Vault init failed: {e}")

    def get_secret(self, key: str) -> Optional[str]:
        if not self._client:
            return None
        try:
            secret = self._client.secrets.kv.v2.read_secret_version(
                path=key,
                mount_point=self.mount_point
            )
            return secret['data']['data'].get('value')
        except Exception as e:
            logger.error(f"Vault read failed for {key}: {e}")
            return None

    def is_available(self) -> bool:
        if not self._client:
            return False
        try:
            return self._client.is_authenticated()
        except:
            return False


class SecretsManager:
    """
    Менеджер секретов с поддержкой нескольких провайдеров.
    
    Использование:
        manager = SecretsManager()
        manager.add_provider(EnvSecretsProvider())
        
        private_key = manager.get_secret('SOLANA_PRIVATE_KEY')
        keypair = manager.get_keypair('SOLANA_PRIVATE_KEY')
    """

    def __init__(self):
        self._providers: list[SecretsProvider] = []

    def add_provider(self, provider: SecretsProvider) -> None:
        """Добавить провайдер секретов"""
        if provider.is_available():
            self._providers.append(provider)
            logger.info(f"Added secrets provider: {type(provider).__name__}")
        else:
            logger.warning(f"Provider unavailable: {type(provider).__name__}")

    def get_secret(self, key: str) -> Optional[str]:
        """
        Получить секрет (пробует всех провайдеров по порядку)
        """
        for provider in self._providers:
            value = provider.get_secret(key)
            if value is not None:
                return value

        logger.warning(f"Secret not found: {key}")
        return None

    def get_keypair(self, key: str):
        """
        Получить Solana Keypair из секрета.
        После создания Keypair исходная строка очищается.
        """
        from solders.keypair import Keypair
        import base58

        secret_str = self.get_secret(key)
        if not secret_str:
            raise ValueError(f"Secret not found: {key}")

        try:
            # Декодирование и создание Keypair
            secret_bytes = base58.b58decode(secret_str)
            keypair = Keypair.from_bytes(secret_bytes)

            # Очистка секрета из памяти (best effort)
            # Примечание: Python не гарантирует немедленную очистку
            secret_str = '0' * len(secret_str)
            del secret_str

            return keypair

        except Exception as e:
            logger.error(f"Failed to create Keypair: {e}")
            raise


def create_secrets_manager(config: dict = None) -> SecretsManager:
    """
    Фабрика для создания SecretsManager из конфига.
    
    config example:
    {
        'secrets_provider': 'env',  # или 'file', 'vault'
        'secrets_file': '/path/to/secrets.json',
        'vault_url': 'https://vault.example.com',
        'vault_token': '...'
    }
    """
    manager = SecretsManager()

    if config is None:
        config = {}

    provider_type = config.get('secrets_provider', 'env')

    if provider_type == 'env':
        manager.add_provider(EnvSecretsProvider())

    elif provider_type == 'file':
        filepath = config.get('secrets_file', '~/.config/pumpbot/secrets.json')
        manager.add_provider(FileSecretsProvider(os.path.expanduser(filepath)))
        # Fallback на env
        manager.add_provider(EnvSecretsProvider())

    elif provider_type == 'vault':
        vault_url = config.get('vault_url')
        vault_token = config.get('vault_token')
        if vault_url and vault_token:
            manager.add_provider(VaultSecretsProvider(vault_url, vault_token))
        # Fallback на env
        manager.add_provider(EnvSecretsProvider())

    else:
        # Default: env provider
        manager.add_provider(EnvSecretsProvider())

    return manager


# Глобальный менеджер (инициализируется при первом использовании)
_manager: Optional[SecretsManager] = None


def get_secrets_manager() -> SecretsManager:
    """Получить глобальный менеджер секретов"""
    global _manager
    if _manager is None:
        _manager = create_secrets_manager()
    return _manager
