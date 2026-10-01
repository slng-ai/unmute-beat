"""How code says money and dates aloud, for TTS: dirhams and fils in words, a day as a person says it.

amount(Decimal('434.50'))   # 'four hundred thirty-four dirhams and fifty fils'
day(date(2026, 10, 9))      # 'Friday the 9th of October'
"""

from datetime import date, time
from decimal import Decimal

ONES = [
    'zero',
    'one',
    'two',
    'three',
    'four',
    'five',
    'six',
    'seven',
    'eight',
    'nine',
    'ten',
    'eleven',
    'twelve',
    'thirteen',
    'fourteen',
    'fifteen',
    'sixteen',
    'seventeen',
    'eighteen',
    'nineteen',
]
TENS = ['twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety']


def number(n: int) -> str:
    """0 to 999,999 in words: 'four hundred thirty-four'."""
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10 - 2] + (f'-{ONES[n % 10]}' if n % 10 else '')
    if n < 1000:
        return f'{ONES[n // 100]} hundred' + (f' {number(n % 100)}' if n % 100 else '')
    return f'{number(n // 1000)} thousand' + (f' {number(n % 1000)}' if n % 1000 else '')


def amount(value: Decimal) -> str:
    """An amount in UAE dirhams and fils, in words. Never digits: TTS would read "434.50" its own way."""
    dirhams, fils = divmod(int(value.quantize(Decimal('0.01')) * 100), 100)
    said = f'{number(dirhams)} dirham{"" if dirhams == 1 else "s"}'
    return f'{said} and {number(fils)} fils' if fils else said


def ordinal(n: int) -> str:
    """1st, 2nd, 3rd, 4th, 11th, 22nd."""
    suffix = 'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


def day(value: date) -> str:
    """ "Friday the 9th of October"."""
    return f'{value:%A} the {ordinal(value.day)} of {value:%B}'


def birthday(value: date) -> str:
    """ "the 14th of March, 1990"."""
    return f'the {ordinal(value.day)} of {value:%B}, {value.year}'


def clock(value: time) -> str:
    """ "5 PM", "5:30 PM"."""
    hour = value.hour % 12 or 12
    return f'{hour}{f":{value.minute:02d}" if value.minute else ""} {"AM" if value.hour < 12 else "PM"}'


if __name__ == '__main__':
    assert amount(Decimal('434.50')) == 'four hundred thirty-four dirhams and fifty fils', amount(
        Decimal('434.50')
    )
    assert amount(Decimal('1249')) == 'one thousand two hundred forty-nine dirhams'
    assert amount(Decimal('1.05')) == 'one dirham and five fils'
    assert day(date(2026, 10, 9)) == 'Friday the 9th of October'
    assert birthday(date(1990, 3, 14)) == 'the 14th of March, 1990'
    assert clock(time(17, 30)) == '5:30 PM' and clock(time(0, 0)) == '12 AM'
    print('ok')
