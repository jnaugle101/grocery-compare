from decimal import Decimal, InvalidOperation

def safe_decimal(x):
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return None

def compute_unit_price(price, qty):
    p = safe_decimal(price)
    q = safe_decimal(qty)
    if not p or not q or q == 0:
        return None
    return float(p / q)
