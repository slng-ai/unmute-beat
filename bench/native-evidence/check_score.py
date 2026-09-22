"""Offline negative controls for the matched cohort's automated score."""

from bench.native_compare import score

row = {
    'speech': 'What phone number should I use?',
    'tools': [],
    'status': 'answered',
    'business': {'bookings': [], 'complaints': []},
}
assert 'complete phone readback missing before verification' in score('booking', [row])
assert 'wrong booking count, date or time' in score('move', [row])
row['business']['complaints'] = [
    {'summary': 'uneven haircut', 'requested_resolution': 'Full refund', 'reference': 'CX0001'}
]
assert 'complaint written before readback approval' in score('complaint', [row, row])
assert 'complaint receipt not spoken' in score('complaint', [row])
print('score negative controls ok: missing readback, premature write, wrong booking and missing receipt fail')
