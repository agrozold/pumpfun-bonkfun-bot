"""Token math utilities - negative balance guards."""
import logging
logger = logging.getLogger(__name__)

def sanitize_token_amount(value, label="amount"):
    value = int(value) if isinstance(value, float) else value
    if value < 0:
        logger.warning(f"[SANITIZE] {label} was {value}, clamping to 0")
        return 0
    return value

def safe_subtract(total, sold, decimals=6, label="remaining"):
    total, sold = int(total), int(sold)
    if total > 0 and sold > 0:
        ratio = sold / total
        if ratio > 100:
            total = total * (10 ** decimals)
        elif ratio < 0.00001 and total > 10 ** (decimals + 2):
            sold = sold * (10 ** decimals)
    result = total - sold
    if result < 0:
        logger.warning(f"[SANITIZE] {label}: {total}-{sold}={result}, clamping to 0")
        return 0
    return result
