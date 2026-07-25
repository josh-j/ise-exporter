"""ise-exporter v3: provider adapters with declared load.

v2 fixed one source per metric and forbade runtime selection, so a throttled or
unavailable source meant a blank panel and there was no way to see what a given
configuration cost the appliance. v3 keeps v2's atomic publication, schema
contract, state layer and Data Connect safety machinery, and rebuilds source
selection and load budgeting as the same declarative decision.
"""

__version__ = "3.0.0.dev0"

SUPPORTED_ISE_RELEASE = "3.3.0.430 Patch 11"
