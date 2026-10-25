"""TickHub-CN package.

The public surface is intentionally small: importing the package must not
connect to Redis, start servers, print to stdout, or read remote data.
"""

__version__ = "1.0.1"
__title__ = "TickHub-CN"

from .client import StockDataClient  # noqa: E402  (side-effect free)

__all__ = ["StockDataClient", "__version__", "__title__"]
