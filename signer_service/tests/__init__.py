"""Signer regression suite. Guards audit fixes 12-14 and the audit-trail gaps.

Run inside the signer container (it needs solders + the Turnkey stamper):
    docker compose --profile stage3 exec signer python -m tests.run
"""
