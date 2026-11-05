"""TickHub-CN package.

The public surface is intentionally small: importing the package must not
connect to Redis, start servers, print to stdout, or read remote data.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

__title__ = "TickHub-CN"

# 版本唯一真源是 pyproject.toml 的 [project] version。这里从安装元数据读，
# 不再手写第二份——否则 tag / wheel / 运行期 /healthz 三处各说各话（QUOT-401）。
try:
    __version__ = _dist_version("tickhub-cn")
except PackageNotFoundError:  # 未安装：纯源码树直接 import
    __version__ = "0.0.0+source"

from .client import StockDataClient  # noqa: E402  (side-effect free)

__all__ = ["StockDataClient", "__version__", "__title__"]
