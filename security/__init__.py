"""Durable, revocable capability grants.

`security.grants` holds `GrantStore`, `Grant`, `Decision`, and `DenyReason`:
what the agent is allowed to do across runs, persisted on disk, auditable, and
revocable. This is distinct from `tools/permissions.py`, which decides what a
single run may do and keeps no state.
"""
