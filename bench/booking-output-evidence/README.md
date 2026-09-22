# Reverted booking-tool experiment — excluded from acceptance

The added `offer_times` and `booking_guidance` tools and related booking prompt/output changes were reverted at the user’s direction: preserve one-to-one tools for benchmarking. The previous tool set was restored byte-for-byte from the frozen source snapshot; the separately verified “Go for it” repair is retained.

The experimental recovery, continuation and paired runs had already completed when their sessions were checked. Their results do not describe the restored implementation and must not be used as current acceptance or matched-tool benchmark evidence.

Experimental raw output: `bench/raw/booking-output-20260921T212034Z.jsonl`, `bench/raw/booking-output-20260921T212035Z.jsonl`, and `bench/raw/booking-output-qualification/`. Recovery reported 0 failures; continuation reported 1 failure. Other logs here predate the rollback unless explicitly named `restored-*`.

The earlier unsupported booking-speech negative control remains a known limitation of the existing phrase guard. The reverted structure is not an accepted fix for that limitation.
