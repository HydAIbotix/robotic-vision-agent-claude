"""
ports/ — cloud-agnostic capability seams (ports & adapters).

Each module exposes a small interface plus config-selected adapters, so a managed-AWS
component can be swapped for an open-source, run-anywhere equivalent with NO change to caller
code. All defaults reproduce the pure-local MVP behaviour (see docs/CLOUD_AGNOSTIC_DECISION.md):

  event_bus  — realtime fan-out.  memory (in-process, MVP) | redis (cross-replica scale-out)
  tenancy    — isolation.         single (one implicit tenant, MVP) | multi (tenant_id per request)

Object storage lives in vision_agent/storage/ and relational persistence in api/database.py
(db_url); both are already config-driven, so they are not duplicated here.
"""
