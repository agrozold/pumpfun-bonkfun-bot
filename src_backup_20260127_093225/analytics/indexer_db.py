"""Подключение к Solana индексеру ClickHouse"""

import os
import pandas as pd

try:
    import pandahouse as ph
    PANDAHOUSE_AVAILABLE = True
except ImportError:
    PANDAHOUSE_AVAILABLE = False

# Читаем credentials из переменных окружения или .env
CONNECTION = {
    'host': os.getenv('INDEXER_HOST', 'https://your-indexer-host:28123'),
    'database': os.getenv('INDEXER_DATABASE', 'default'),
    'user': os.getenv('INDEXER_USER', 'your_username'),
    'password': os.getenv('INDEXER_PASSWORD', 'your_password')
}

def query(sql: str) -> pd.DataFrame:
    """Выполнить SQL запрос к индексеру"""
    if not PANDAHOUSE_AVAILABLE:
        raise ImportError("Установи pandahouse: pip install pandahouse")
    
    if CONNECTION['password'] == 'your_password':
        raise ValueError("Set INDEXER_* environment variables in .env file")

    df = ph.read_clickhouse(sql, connection=CONNECTION)
    return df.drop_duplicates()
