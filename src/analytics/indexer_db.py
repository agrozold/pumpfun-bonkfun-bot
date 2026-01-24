"""Подключение к Solana индексеру ClickHouse"""

import pandas as pd

try:
    import pandahouse as ph
    PANDAHOUSE_AVAILABLE = True
except ImportError:
    PANDAHOUSE_AVAILABLE = False

CONNECTION = {
    'host': 'https://chess-beta.api.web3engineering.co.uk:28123',
    'database': 'default',
    'user': 'readonly_agrozold',
    'password': 'HE&kMnQoP4w%@ke2'
}

def query(sql: str) -> pd.DataFrame:
    """Выполнить SQL запрос к индексеру"""
    if not PANDAHOUSE_AVAILABLE:
        raise ImportError("Установи pandahouse: pip install pandahouse")
    
    df = ph.read_clickhouse(sql, connection=CONNECTION)
    return df.drop_duplicates()
