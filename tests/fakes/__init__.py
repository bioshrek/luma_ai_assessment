"""Test doubles that are part of the test *strategy*, not conveniences.

Fakes prove exhaustiveness — every crash checkpoint, every idempotency path — which a real
`kill -9` can only sample. `scripts/demo_crash_resume.sh` proves realism. Both are required
(design §8.5).
"""
