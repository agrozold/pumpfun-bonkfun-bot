# Session 9: Atomic File Writes

**Date:** 2026-01-20
**Status:** COMPLETED

## Problem
File corruption during crashes when writing positions.json

## Solution
1. Write to temp file
2. fsync to disk
3. Atomic rename (os.replace)
4. Auto backup before write
5. Auto recovery from backup on read error

## Files
- src/utils/safe_file_writer.py - NEW
- src/utils/__init__.py - NEW  
- src/trading/position.py - MODIFIED

## Usage
```python
from src.utils import save_json_safe, load_json_safe
save_json_safe('file.json', data)
data = load_json_safe('file.json', default=[])

