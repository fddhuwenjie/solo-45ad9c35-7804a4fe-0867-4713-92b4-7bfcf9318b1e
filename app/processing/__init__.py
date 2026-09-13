"""兼容层：时序处理已归入公共包 ``app.common.processing``。

保留 ``app.processing.<模块>`` 导入路径供既有调用方使用；
新代码请直接引用 ``app.common.processing``。
"""

import sys as _sys

from ..common.processing import alignment, detection, metrics, segmentation, units

for _m in (alignment, detection, metrics, segmentation, units):
    _sys.modules[f"{__name__}.{_m.__name__.rsplit('.', 1)[-1]}"] = _m

__all__ = ["alignment", "detection", "metrics", "segmentation", "units"]
