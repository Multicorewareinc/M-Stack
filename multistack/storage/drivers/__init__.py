"""
Block-storage implementations, one module each.

This package deliberately imports none of them. The registry in
`../registry.py` names each driver by module path and loads it only when
that `type` is chosen, so importing this package stays free no matter what
a driver depends on.

A driver reads its own settings from `spec.options`, satisfies the
`StorageDriver` Protocol in `../base.py`, and raises errors subclassing
`StorageError` / `StoragePrerequisiteError` from there — so a caller can
handle failures without importing any implementation.
"""
