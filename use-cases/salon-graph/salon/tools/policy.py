"""The two numbers every salon step is built with."""

ACTIONS = frozenset(
    {
        'hand_over',
        'confirm_customer',
        'save_booking',
        'hang_up',
        'record_complaint',
        'read_diary',
        'offer_times',
        'stop_verifying',
    }
)
"""Actions that change where the caller is, or write to the salon. At most one runs per model response."""

SPEAK_BY = 3
"""The request at which a run runs out of ordinary tools and has to answer with what it holds.

Three, because the longest legitimate chain is two tool-bearing requests: read
the diary, then correct what it refused.
"""
