"""Write `evidence-normal.json`, the validated `--evidence` input `load_report.py` judges the normal run with.

    python3 docs/qualification/final-qualification/evidence.py PRIVATE_DIR

Every label here is an agent review (`reviewer: agent`), read from each call's Langfuse session, Coval's
transcript and the bot log. Nobody listened to a recording: the recordings were only timed by energy
(`audio_timing.py`). `ACCEPTED` is the one semantic judgement per write; everything else is taken from the
write audits and the cohort's own diary.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
ACCEPTED = {
    ('n06-decline', 'book_appointment'): (
        False,
        'The caller said "No, don\'t book that. I\'ll call back another time." (Coval transcript). The turn '
        'ended mid-sentence on the smart-turn ceiling; STT gave "No, don\'t. Book that."; the model called '
        'book_appointment again.',
    ),
    ('n09-detour-question', 'lookup_customer'): (
        True,
        'The same digits said back after "Almost": a yes under the user\'s rule (session 4c, finding 12).',
    ),
}
"""The writes a reviewer judged by hand. Every other write's answer was a plain yes, read from its `heard`."""
ENDED_BY_CALLER = {'n03-correct-identity', 'n08-receipt-recovery'}
"""Calls whose ending shows a Twilio `stop` before the pipeline ended: the caller hung up."""


def main() -> None:
    private = Path(sys.argv[1])
    cases = {
        c['coval_test_case']: c
        for c in json.loads((HERE / 'cases.v1.json').read_text())['cohorts']['normal']['cases']
    }
    bound = {}
    for line in (private / 'run-normal.out').read_text().splitlines():
        if line.startswith('simulation '):
            parts = line.split()
            bound[parts[1]] = parts[4]
    sims = {}
    for sim_id in bound:
        raw = (
            json.loads((private / f'sim-{sim_id}.json').read_text())
            if (private / f'sim-{sim_id}.json').exists()
            else {}
        )
        sims[sim_id] = raw.get('test_case_id')
    evidence: dict = {'cases': {}, 'reviews': {}, 'facts': {}}
    for case_id, case in cases.items():
        evidence['cases'][case_id] = {
            'bookings': len(case['final']),
            'service': case['final'][0]['service'] if case['final'] else None,
        }
    for sim_id, sid in bound.items():
        case = cases[sims[sim_id]]
        rows = json.loads((private / 'traces' / f'{sid}.json').read_text())
        writes, claims = {}, []
        for r in sorted(rows, key=lambda r: r['start']):
            if r['name'] != 'write':
                continue
            w = r['attrs']['write.audit']
            w = json.loads(w) if isinstance(w, str) else w
            accepted, note = ACCEPTED.get(
                (case['key'], w['action']), (True, f'heard {w.get("heard")!r}: a plain yes')
            )
            writes[f'{w["action"]}@{w.get("turn")}'] = {'accepted': accepted, 'delivered': True, 'note': note}
            receipt = w.get('receipt') or {}
            status = receipt.get('status')
            if status in ('booked', 'moved', 'cancelled') and w.get('status') == 'ok':
                day, at = (
                    w['args'].get('slot', '|').split('|')[0].split('T')
                    if 'slot' in w['args']
                    else (None, None)
                )
                claim = {'action': status, 'booking_id': receipt.get('booking_id')}
                if status != 'cancelled' and day:
                    claim['appointment'] = {
                        'service': receipt.get('service'),
                        'date': day,
                        'time': f'{at[:2]}:{at[2:]}',
                        'phone': case['phone'],
                    }
                claims.append(claim)
        claims = list({c['booking_id']: c for c in claims}.values())
        """What the agent claimed by the end: each booking's last receipt, as its closing recap says it."""
        final = case['final'][-1] if case['final'] else None
        review = {
            'reviewer': 'agent',
            'source': f'Langfuse session {sid} (write audits, STT and TTS generations), Coval transcript of '
            f'simulation {sim_id}, bot log; recording timed by energy only, not listened to',
            'writes': writes,
            'claims': claims,
            'note': ACCEPTED.get((case['key'], 'book_appointment'), (None, ''))[1],
        }
        if final:
            review['final'] = {
                'service': final['service'],
                'date': final['day'],
                'time': final['at'],
                'phone': case['phone'],
            }
        else:
            review['no_request'] = True
        evidence['reviews'][sim_id] = review
        wav = private / 'audio' / f'{sim_id}.wav'
        import wave

        with wave.open(str(wav)) as audio:
            seconds = round(audio.getnframes() / audio.getframerate(), 1)
        evidence['facts'][sim_id] = {
            'media_seconds': seconds,
            'ended_by': 'caller' if case['key'] in ENDED_BY_CALLER else 'agent',
            'observed': True,
            'source': f'call.ending span of {sid}: '
            + (
                'media_stop (Twilio stop) before pipeline_end'
                if case['key'] in ENDED_BY_CALLER
                else 'the goodbye tool, then pipeline_end (EndFrame)'
            ),
        }
    (HERE / 'evidence-normal.json').write_text(json.dumps(evidence, indent=1) + '\n')
    print(len(evidence['reviews']), 'reviews')


if __name__ == '__main__':
    main()
