"""Temporal workflow/activity definitions for the optional `orchestrator_backend=temporal` path.

Kept in its own package (not under ports/) because the module is only importable when the optional
`temporalio` SDK is installed. ports/orchestration.py imports it lazily.
"""
