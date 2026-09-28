"""Write `cases.v1.json`: the 20 frozen qualification cases, ten tasks per cohort, one caller each.

    python3 docs/qualification/final-qualification/cases.py

Every caller has their own synthetic number, never dialled: it only names the customer in the cohort's own
diary. Each cohort has its own diary, seeded before its run (`seed.py`), so the two cohorts can use the same
days and times. Within a cohort no two callers want overlapping times on the one chair. The days assume the
calls run on Monday 28 September 2026 (Europe/Madrid): Tuesday is the 29th, Saturday 3 October.

Coval IDs are written in by `make_sets.py` once the sets exist. Running this again keeps them.
"""

import json
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / 'cases.v1.json'
DIGITS = 'zero one two three four five six seven eight nine'.split()
TAIL = (
    'You are calling from a different phone, so the salon cannot see your number. Give your number only when '
    'the receptionist asks for it. When asked if you need anything else at the end, say "No, that\'s all, '
    'thanks" and say goodbye. Do not complain. Do not ask for a manager.'
)


def phone(cohort: int, case: int) -> str:
    """+34 671 56c cNN: cohort 1 is normal, 2 interruptive."""
    return f'+34671{560 + cohort}{cohort}{case:02d}'


def said(number: str) -> str:
    """The number as the caller says it: plus three four, six seven one, five six one, one zero one."""
    body = number[3:]
    groups = ['plus three four'] + [' '.join(DIGITS[int(d)] for d in body[i : i + 3]) for i in range(0, 9, 3)]
    return ', '.join(groups)


def split(number: str) -> tuple[str, str]:
    """The number in two pieces: the country code and first group, then the rest."""
    parts = said(number).split(', ')
    return ', '.join(parts[:2]), ', '.join(parts[2:])


def give(number: str) -> str:
    return f'Your phone number is {number[:3]} {number[3:6]} {number[6:9]} {number[9:]}. Say it as "{said(number)}"'


def cases(cohort: int) -> list[dict]:
    """The ten tasks. `talk_over` is only for the interruptive cohort: an interruption the case asks for."""
    rude = cohort == 2
    p = [phone(cohort, i) for i in range(1, 11)]
    wrong = phone(cohort, 13)
    first, rest = split(p[1])
    out = [
        {
            'task': 'book',
            'phone': p[0],
            'input': f'You want a haircut at Sage and Stone on Tuesday at ten in the morning. {give(p[0])}, and say '
            'yes when it is read back correctly. If ten in the morning is offered, choose it. '
            + (
                'When the receptionist starts reading the booking back, interrupt before they finish and say '
                '"Yes, book it." If they read it back again, let them finish, then say yes. '
                if rude
                else 'Say yes clearly when the booking is read back. '
            )
            + TAIL,
            'trigger': 'a booking on a yes to its heard read-back'
            + (' (a yes said over the read-back cannot approve it)' if rude else ''),
            'final': [{'service': 'haircut', 'day': '2026-09-29', 'at': '10:00', 'revision': 1}],
            'writes': {'book': 1},
        },
        {
            'task': 'fragments-natural-yes',
            'phone': p[1],
            'input': f'You want a haircut at Sage and Stone on Tuesday at two in the afternoon. Your phone number is '
            f'{p[1][:3]} {p[1][3:6]} {p[1][6:9]} {p[1][9:]}. When the receptionist asks for your phone number, say '
            f'only "{first}" and stop. When they ask for the rest, say "{rest}". When the full number is read back '
            'correctly, answer only "That sounds right." If two in the afternoon is offered, choose it. When the '
            'booking is read back, answer only "That sounds right." ' + TAIL,
            'trigger': 'the number in two pieces joined into one; "That sounds right." taken as a yes, twice',
            'final': [{'service': 'haircut', 'day': '2026-09-29', 'at': '14:00', 'revision': 1}],
            'writes': {'book': 1},
        },
        {
            'task': 'correct-identity',
            'phone': p[2],
            'input': f'You want a blowout at Sage and Stone on Wednesday at eleven in the morning. Your phone number '
            f'is {p[2][:3]} {p[2][3:6]} {p[2][6:9]} {p[2][9:]}. When the receptionist asks for it, you misspeak it: '
            f'say "{said(wrong)}". '
            + (
                'When they start reading that number back, interrupt before they finish and say "No, sorry, '
                f'that\'s wrong. It\'s {said(p[2])}." '
                if rude
                else f'When they read that number back, say "No, sorry, the last part is wrong. It\'s {said(p[2])}." '
            )
            + 'Say yes when the right number is read back. If eleven in the morning is offered, choose it, and say '
            'yes clearly when the booking is read back. ' + TAIL,
            'trigger': 'a corrected number before approval: nothing is looked up or booked for the misspoken one',
            'wrong_phone': wrong,
            'final': [{'service': 'blowout', 'day': '2026-09-30', 'at': '11:00', 'revision': 1}],
            'writes': {'book': 1},
        },
        {
            'task': 'move-existing',
            'phone': p[3],
            'seed': {'service': 'haircut', 'day': '2026-09-30', 'at': '10:00'},
            'input': 'You already have a haircut at Sage and Stone on Wednesday at ten in the morning. You want to '
            f'move it to Thursday at the same time. {give(p[3])}, and say yes when it is read back correctly. '
            + (
                'When the receptionist starts reading the move back, interrupt before they finish and say '
                '"Sorry, no, I mean Friday, the same time." Say yes clearly only when a move to Friday at ten in '
                'the morning is read back. '
                if rude
                else 'Say yes clearly when the move to Thursday at ten is read back. '
            )
            + TAIL,
            'trigger': 'the caller\'s existing booking found and moved'
            + (', with the target corrected over the move read-back' if rude else ''),
            'final': [
                {
                    'service': 'haircut',
                    'day': '2026-10-02' if rude else '2026-10-01',
                    'at': '10:00',
                    'revision': 2,
                    'seeded': True,
                }
            ],
            'writes': {'move': 1},
        },
        {
            'task': 'cancel-existing',
            'phone': p[4],
            'seed': {'service': 'blowout', 'day': '2026-10-01', 'at': '15:00'},
            'input': 'You have a blowout at Sage and Stone on Thursday at three in the afternoon, and you want to '
            f'cancel it. {give(p[4])}, and say yes when it is read back correctly. '
            + (
                'When the receptionist starts reading the cancellation back, interrupt before they finish and say '
                '"Yes, cancel it." If they read it back again, let them finish, then say yes. '
                if rude
                else 'Say yes clearly when the cancellation is read back. '
            )
            + TAIL,
            'trigger': 'the caller\'s existing booking found and cancelled once'
            + (' (a yes said over the read-back cannot approve it)' if rude else ''),
            'final': [],
            'cancelled': [{'service': 'blowout', 'day': '2026-10-01', 'at': '15:00', 'revision': 2}],
            'writes': {'cancel': 1},
        },
        {
            'task': 'decline',
            'phone': p[5],
            'input': 'You are thinking about a haircut at Sage and Stone on Friday at eleven in the morning, but you '
            f'are not sure yet. {give(p[5])}, and say yes when it is read back correctly. If eleven in the morning '
            'is offered, choose it. '
            + (
                'When the receptionist starts reading the Friday booking back, interrupt before they finish and '
                'say "No, no, don\'t book that. I\'ll call back another time." '
                if rude
                else 'When the receptionist reads the Friday booking back and asks you to confirm, say exactly '
                '"No, don\'t book that. I\'ll call back another time." '
            )
            + 'If they ask again, say no again. Never agree to a booking. ' + TAIL,
            'trigger': 'an explicit no to a read-back: nothing written',
            'final': [],
            'writes': {},
        },
        {
            'task': 'changed-request',
            'phone': p[6],
            'input': f'You want a haircut at Sage and Stone on Friday at three in the afternoon. {give(p[6])}, and '
            'say yes when it is read back correctly. If three in the afternoon is offered, choose it. '
            + (
                'When the receptionist starts reading the Friday booking back, interrupt before they finish and '
                'say "Actually, can we make it Saturday at ten in the morning instead?" '
                if rude
                else 'When the receptionist reads the Friday booking back, do not say yes: say "Actually, can we '
                'make it Saturday at ten in the morning instead?" '
            )
            + 'If ten on Saturday is offered, choose it, and say yes clearly when that booking is read back. '
            + TAIL,
            'trigger': 'a changed request at the read-back: the new booking needs its own read-back and yes',
            'final': [{'service': 'haircut', 'day': '2026-10-03', 'at': '10:00', 'revision': 1}],
            'writes': {'book': 1},
        },
        {
            'task': 'receipt-recovery',
            'phone': p[7],
            'input': f'You want a haircut at Sage and Stone on Wednesday at three in the afternoon. {give(p[7])}, and '
            'say yes when it is read back correctly. If three in the afternoon is offered, choose it, and say yes '
            'clearly when the booking is read back. '
            + (
                'When the receptionist starts telling you that it is booked, interrupt before they finish and ask '
                '"Sorry, what time was that?" '
                if rude
                else 'When the receptionist tells you that it is booked, say "Sorry, could you repeat that?" '
            )
            + 'When they have told you again, say "No, that\'s all, thanks" and say goodbye. Do not ask for '
            'anything else. Do not complain. Do not ask for a manager. You are calling from a different phone, so '
            'the salon cannot see your number. Give your number only when the receptionist asks for it.',
            'trigger': 'the booking receipt '
            + ('interrupted, then said again' if rude else 'asked for again')
            + ', with no second write',
            'final': [{'service': 'haircut', 'day': '2026-09-30', 'at': '15:00', 'revision': 1}],
            'writes': {'book': 1},
        },
        {
            'task': 'detour-question',
            'phone': p[8],
            'input': f'You want a haircut at Sage and Stone on Thursday at four in the afternoon. {give(p[8])}, and '
            'say yes when it is read back correctly. If four in the afternoon is offered, choose it. '
            + (
                'When the receptionist starts reading the booking back, interrupt before they finish and ask '
                '"Sorry, how much is a haircut?" '
                if rude
                else 'When the receptionist reads the booking back, do not say yes or no: ask "Before I answer, '
                'how much is a haircut?" '
            )
            + 'After they answer, say "OK, yes, please book it." Say yes when the booking is read back to you '
            'again. ' + TAIL,
            'trigger': 'a question during a read-back answered (28 euros), then the task continued and booked',
            'final': [{'service': 'haircut', 'day': '2026-10-01', 'at': '16:00', 'revision': 1}],
            'writes': {'book': 1},
        },
        {
            'task': 'yes-and-more',
            'phone': p[9],
            'input': f'You want a haircut at Sage and Stone on Saturday at eleven in the morning. {give(p[9])}, and '
            'say yes when it is read back correctly. If eleven in the morning is offered, choose it, and say yes '
            'clearly when the booking is read back. '
            + (
                'When the receptionist tells you it is booked and starts to ask if there is anything else, '
                'interrupt and say exactly "Yes, that\'s perfect. Can you move it to one in the afternoon instead?" '
                if rude
                else 'When the receptionist tells you it is booked and asks if there is anything else, say exactly '
                '"Yes, that\'s perfect. Can you move it to one in the afternoon instead?" '
            )
            + 'Say yes clearly when the move is read back. ' + TAIL,
            'trigger': '"Yes, that\'s perfect. Can you..." is a new request, not a goodbye: the booking just made is moved',
            'final': [{'service': 'haircut', 'day': '2026-10-03', 'at': '13:00', 'revision': 2}],
            'writes': {'book': 1, 'move': 1},
        },
    ]
    name = 'interruptive' if rude else 'normal'
    for i, case in enumerate(out, start=1):
        case['key'] = f'{name[0]}{i:02d}-{case["task"]}'
    return out


def main() -> None:
    old = json.loads(OUT.read_text()) if OUT.exists() else {}
    ids = {c['key']: c.get('coval_test_case', '') for cohort in old.get('cohorts', {}).values() for c in cohort['cases']}
    sets = {name: cohort.get('coval_test_set', '') for name, cohort in old.get('cohorts', {}).items()}
    cohorts = {}
    for number, name, persona in ((1, 'normal', 'MUFJdYAHdBHU6UbRRu4ykM'), (2, 'interruptive', 'VSruLZYYyz3w7fkdUaheDR')):
        made = cases(number)
        for case in made:
            case['coval_test_case'] = ids.get(case['key'], '')
        cohorts[name] = {'persona': persona, 'coval_test_set': sets.get(name, ''), 'cases': made}
    phones = [c['phone'] for cohort in cohorts.values() for c in cohort['cases']]
    assert len(set(phones)) == 20, 'one caller per case'
    doc = {
        'version': 1,
        'note': __doc__.strip().split('\n\n')[1],
        'agent': 'PEivYdEtwEa65QSraG5fMQ',
        'number': '+447427919928',
        'call_seconds': 300,
        'iterations': 1,
        'cohorts': cohorts,
    }
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n')
    print('wrote', OUT, len(phones), 'cases')


if __name__ == '__main__':
    main()
