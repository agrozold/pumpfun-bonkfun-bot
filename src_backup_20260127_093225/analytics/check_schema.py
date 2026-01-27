import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from indexer_db import query

print("=" * 60)
print("SCHEMA:")
print("=" * 60)
schema = query("DESCRIBE TABLE default.pumpfun_all_swaps")
print(schema.to_string())

print("\n" + "=" * 60)
print("SAMPLE DATA:")
print("=" * 60)
sample = query("SELECT * FROM default.pumpfun_all_swaps LIMIT 3")
print(sample.to_string())
