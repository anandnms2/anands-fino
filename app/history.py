"""Price-history helpers shared by the backtester: Yahoo symbol names and the default contract specification (units per lot, digits, spread)
used to cost simulated trades. Live and demo trading take their prices and specifications from cTrader."""
UA = {"User-Agent": "Mozilla/5.0 (compatible; AnandsFino/1.0)"}

# Yahoo symbols to try in order (spot first, futures as a fallback; the futures price differs slightly from spot)
YAHOO = {"XAUUSD": ["XAUUSD=X", "GC=F"], "XAGUSD": ["XAGUSD=X", "SI=F"]}
DEFAULT_SPREAD = {"XAUUSD": 0.25, "XAGUSD": 0.03, "USDJPY": 0.008, "EURJPY": 0.012, "GBPJPY": 0.020, "AUDJPY": 0.016,
                  "EURGBP": 0.00010, "USDCHF": 0.00010, "USDCAD": 0.00012, "EURUSD": 0.00007, "GBPUSD": 0.00009}  # price units; other pairs use 0.00010
CONTRACT = {"XAUUSD": 100.0, "XAGUSD": 5000.0}
DIGITS = {"XAUUSD": 2, "XAGUSD": 3}


def default_spec(symbol: str) -> dict:
    """Typical contract values for a symbol: units per lot, price digits, spread and lot limits."""
    s = symbol.upper()
    return {"contract": CONTRACT.get(s, 100000.0), "digits": DIGITS.get(s, 3 if s.endswith("JPY") else 5),
            "spread": float(DEFAULT_SPREAD.get(s, 0.00010)), "volume_min": 0.01, "volume_step": 0.01, "volume_max": 50.0}
