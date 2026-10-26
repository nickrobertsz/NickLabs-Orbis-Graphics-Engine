"""NickLabs Orbis Graphics Pack — diagnostics, qualification, health and profiles.

Stable machine-readable interface (for the future Orbis settings UI):
  orbis-gpu health --json        GpuHealth snapshot           schema orbis-gfx-health/1
  orbis-gpu qualify --json-out F full qualification report    schema orbis-gfx-report/1
  orbis-gpu profile list --json  profiles + capabilities      schema orbis-gfx-profiles/1
  orbis-gpu capabilities --json  what this console can do     schema orbis-gfx-capabilities/1
"""
__version__ = "2.0.0"
API_VERSION = 1
