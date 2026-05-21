"""
Enhanced number processing utilities for Telegram Crypto Bot.

Provides professional-grade number parsing and formatting with:
- Smart parsing of various number formats (European, US, Persian)
- Zero scientific notation (always human-readable)
- Proper decimal precision (8 for crypto, 2 for USD, 0 for Toman)
- Bilingual support (English/Persian digits)
"""

from decimal import Decimal, ROUND_DOWN, InvalidOperation
import re
from typing import Optional, Tuple


def normalize_digits(text: str) -> str:
    """
    Normalize Persian/Arabic digits and operators to ASCII.
    
    Converts:
    - Persian digits (۰-۹) to ASCII (0-9)
    - Arabic digits (٠-٩) to ASCII (0-9)
    - Persian operators (٪×÷) to ASCII (%*/)
    - Persian separators (٬٫) to standard (, .)
    
    Args:
        text: Input text with potential Persian/Arabic characters
        
    Returns:
        Text with normalized ASCII digits and operators
    """
    # Persian digits: ۰۱۲۳۴۵۶۷۸۹
    persian_digits = str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789')
    # Arabic digits: ٠١٢٣٤٥٦٧٨٩
    arabic_digits = str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789')
    # Persian operators and separators
    persian_ops = str.maketrans('٪×÷٬٫', '%*/,.')
    
    text = text.translate(persian_digits)
    text = text.translate(arabic_digits)
    text = text.translate(persian_ops)
    
    return text


def parse_number(text: str) -> Optional[Decimal]:
    """
    Parse a number from text with intelligent format detection.
    
    Supports:
    - European format: 1.000.000,50 or 1.234,56
    - US format: 1,000,000.50 or 1,234.56
    - Persian format: ۱۲۳٬۴۵۶ or ۱۲٫۳۴
    - No separators: 1000000
    - Scientific notation: 1e6 (converts to 1000000)
    
    Smart separator detection:
    - If last separator is ',' → European (1.234,56 = 1234.56)
    - If last separator is '.' → US (1,234.56 = 1234.56)
    - Single separator position determines usage
    
    Args:
        text: String containing a number
        
    Returns:
        Decimal object or None if parsing fails
        
    Examples:
        >>> parse_number("1.000.000,50")
        Decimal('1000000.50')
        >>> parse_number("1,234.56")
        Decimal('1234.56')
        >>> parse_number("۱۲۳٬۴۵۶")
        Decimal('123456')
    """
    if not text:
        return None
    
    # Normalize Persian/Arabic digits first
    text = normalize_digits(str(text).strip())
    
    # Remove any whitespace
    text = text.replace(' ', '')
    
    # Handle empty after normalization
    if not text:
        return None
    
    try:
        # Count separators
        comma_count = text.count(',')
        dot_count = text.count('.')
        
        # No separators - direct conversion
        if comma_count == 0 and dot_count == 0:
            return Decimal(text)
        
        # Find last separator to determine format
        last_comma_pos = text.rfind(',')
        last_dot_pos = text.rfind('.')
        
        # Determine decimal separator based on position
        if last_comma_pos > last_dot_pos:
            # European format: 1.234.567,89
            # Comma is decimal, dots are thousands
            text = text.replace('.', '')  # Remove thousands separators
            text = text.replace(',', '.')  # Convert decimal separator
        else:
            # US format: 1,234,567.89
            # Dot is decimal, commas are thousands
            text = text.replace(',', '')  # Remove thousands separators
            # Dot stays as decimal separator
        
        return Decimal(text)
        
    except (InvalidOperation, ValueError):
        return None


def format_crypto(value: Decimal, max_decimals: int = 8) -> str:
    """
    Format cryptocurrency amount with appropriate precision.
    
    Rules:
    - Shows up to max_decimals (default 8)
    - Strips trailing zeros
    - Never uses scientific notation
    - Adds thousands separators for large numbers
    
    Args:
        value: Decimal amount to format
        max_decimals: Maximum decimal places (default 8)
        
    Returns:
        Formatted string
        
    Examples:
        >>> format_crypto(Decimal('0.00012300'))
        '0.000123'
        >>> format_crypto(Decimal('1234.56789012'))
        '1,234.56789012'
        >>> format_crypto(Decimal('1000000'))
        '1,000,000'
    """
    if value == 0:
        return '0'
    
    # Convert to fixed-point string (no scientific notation)
    # Quantize to max_decimals, rounding down
    quantum = Decimal(10) ** -max_decimals
    value_rounded = value.quantize(quantum, rounding=ROUND_DOWN)
    
    # Convert to string
    value_str = format(value_rounded, 'f')
    
    # Split into integer and decimal parts
    if '.' in value_str:
        int_part, dec_part = value_str.split('.')
        # Strip trailing zeros from decimal part
        dec_part = dec_part.rstrip('0')
        # Add thousands separators to integer part
        int_part = f"{int(int_part):,}"
        # Combine
        if dec_part:
            return f"{int_part}.{dec_part}"
        else:
            return int_part
    else:
        # No decimal part
        return f"{int(value_str):,}"


def format_fiat(value: Decimal, decimals: int = 2) -> str:
    """
    Format fiat currency (USD, Toman) with fixed decimals.
    
    Rules:
    - Shows exactly 'decimals' places (default 2 for USD)
    - Use decimals=0 for Toman
    - Never uses scientific notation
    - Adds thousands separators
    - Strips .00 for whole numbers when decimals=2
    
    Args:
        value: Decimal amount to format
        decimals: Number of decimal places (0 for Toman, 2 for USD)
        
    Returns:
        Formatted string
        
    Examples:
        >>> format_fiat(Decimal('1234.56'))
        '1,234.56'
        >>> format_fiat(Decimal('1234.00'))
        '1,234'
        >>> format_fiat(Decimal('1234567'), decimals=0)
        '1,234,567'
    """
    if decimals == 0:
        # Toman: no decimals
        return f"{int(value):,}"
    
    # Round to specified decimals
    quantum = Decimal(10) ** -decimals
    value_rounded = value.quantize(quantum, rounding=ROUND_DOWN)
    
    # Format with fixed decimals
    format_str = f"{{:,.{decimals}f}}"
    result = format_str.format(float(value_rounded))
    
    # Strip .00 for whole numbers (only when decimals=2)
    if decimals == 2 and result.endswith('.00'):
        result = result[:-3]
    
    return result


def format_for_locale(text: str, locale: str = 'en') -> str:
    """
    Convert number string to locale-specific digit format.
    
    Args:
        text: Number string in ASCII format
        locale: 'en' for English, 'fa' for Persian
        
    Returns:
        Number string with locale-specific digits
        
    Examples:
        >>> format_for_locale('1,234.56', 'fa')
        '۱,۲۳۴.۵۶'
        >>> format_for_locale('1,234.56', 'en')
        '1,234.56'
    """
    if locale == 'fa':
        # Convert to Persian digits
        persian_map = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')
        return text.translate(persian_map)
    
    return text


def format_wallet_balance(
    crypto_amount: Decimal,
    crypto_symbol: str,
    usd_rate: Decimal,
    toman_rate: Decimal,
    user_lang: str = 'en'
) -> str:
    """
    Format wallet balance showing crypto + USD + Toman values.
    Always uses English digits to avoid RTL display issues.
    """
    # Format crypto (up to 8 decimals, strip zeros)
    crypto_str = format_crypto(crypto_amount)
    
    # Calculate values
    usd_value = crypto_amount * usd_rate
    toman_value = usd_value * toman_rate
    
    # Format with proper decimals
    usd_str = format_fiat(usd_value)  # 2 decimals
    toman_str = format_fiat(toman_value, decimals=0)  # 0 decimals
    
    # ⚠️ DO NOT apply locale conversion - always use English digits
    # This prevents RTL display issues in Telegram
    
    # Build display
    result = f"🪙 {crypto_str} {crypto_symbol}\n"
    result += f"💵 ${usd_str}\n"
    result += f"💰 {toman_str} Toman"
    return result


def parse_conversion_command(text: str) -> Optional[Tuple[Decimal, str, str]]:
    """
    Parse conversion commands like "10 btc to eth" or "100 usd toman".
    
    Supports:
    - "10 btc to eth"
    - "10btc eth" (no space, no "to")
    - "100 usd toman"
    - Persian keywords: "به" instead of "to"
    - Persian numbers
    
    Args:
        text: Command text
        
    Returns:
        Tuple of (amount, from_currency, to_currency) or None
        
    Examples:
        >>> parse_conversion_command("10 btc to eth")
        (Decimal('10'), 'btc', 'eth')
        >>> parse_conversion_command("100 تومان to usd")
        (Decimal('100'), 'تومان', 'usd')
    """
    # Normalize digits first
    text = normalize_digits(text.strip().lower())
    
    # Pattern: amount + currency + optional(to/به) + currency
    # Supports: "10 btc eth", "10 btc to eth", "10btc eth"
    pattern = r'^([\d.,]+)\s*(\w+|تومان|تومن)\s*(?:to|به)?\s*(\w+|تومان|تومن)$'
    
    match = re.match(pattern, text)
    if not match:
        return None
    
    amount_str = match.group(1)
    from_curr = match.group(2)
    to_curr = match.group(3)
    
    # Parse amount
    amount = parse_number(amount_str)
    if amount is None:
        return None
    
    return (amount, from_curr, to_curr)


# Example usage and tests
if __name__ == "__main__":
    print("=== Number Parsing Tests ===")
    
    test_cases = [
        "1.000.000",      # European millions
        "1,000,000",      # US millions
        "1.234,56",       # European decimal
        "1,234.56",       # US decimal
        "۱۲۳٬۴۵۶",       # Persian
        "1000000",        # No separators
        "0.00012300",     # Small with trailing zeros
    ]
    
    for test in test_cases:
        result = parse_number(test)
        print(f"{test:20} → {result}")
    
    print("\n=== Crypto Formatting Tests ===")
    
    values = [
        Decimal("0.00012300"),
        Decimal("1234.56789012"),
        Decimal("1000000"),
        Decimal("0.00000001"),
    ]
    
    for val in values:
        formatted = format_crypto(val)
        print(f"{val:20} → {formatted}")
    
    print("\n=== Fiat Formatting Tests ===")
    
    # USD (2 decimals)
    print(f"USD: {format_fiat(Decimal('1234.56'))}")
    print(f"USD: {format_fiat(Decimal('1234.00'))}")
    
    # Toman (0 decimals)
    print(f"Toman: {format_fiat(Decimal('60000000'), decimals=0)}")
    
    print("\n=== Locale Tests ===")
    
    text = "1,234.56"
    print(f"English: {format_for_locale(text, 'en')}")
    print(f"Persian: {format_for_locale(text, 'fa')}")
    
    print("\n=== Wallet Balance Test ===")
    
    balance = format_wallet_balance(
        Decimal('0.5'),
        'TRX',
        Decimal('0.28'),
        Decimal('60000'),
        'en'
    )
    print(balance)