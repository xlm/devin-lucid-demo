"""Single-host, flock-based cutover demo, not distributed or exactly-once.

Lease handover is not atomic, and a window with no active producer can occur.
Ordering is per vehicle key. Consumer upserts by (vehicle_id, session_id)
absorb duplicates but do not remove gaps or provide end-to-end exactly-once.
"""

from verification.cutover.commands import main

__all__ = ["main"]
