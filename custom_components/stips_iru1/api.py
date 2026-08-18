"""Compatibility wrapper for STIPS API client.

Custom integration path delegates communication logic to the standalone
`stips_api_bridge` package.
"""

from stips_api_bridge.api import StipsApiAuthError, StipsApiClient, StipsApiError, StipsApiPermissionError

__all__ = [
    "StipsApiClient",
    "StipsApiError",
    "StipsApiAuthError",
    "StipsApiPermissionError",
]
