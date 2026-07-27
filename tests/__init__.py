"""Test package.

This exists so pytest imports every test module under one name. Without it, a
module reached both as ``test_x`` (pytest's own import) and ``tests.test_x`` (a
cross-import from another test) is instantiated twice, and any module-level
Prometheus metric registers twice into the default registry -- which fails with
a duplicated-timeseries error that depends on collection order.
"""
