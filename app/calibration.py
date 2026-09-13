"""兼容层：校准链领域逻辑已归入公共包 ``app.common.calibration``。

保留 ``app.calibration`` 导入路径供既有调用方使用；
新代码请直接引用 ``app.common.calibration``。
"""

from .common.calibration import *  # noqa: F401,F403
from .common import calibration as _impl


def __getattr__(name):
    # 私有助手等未随 * 导出的名字，转发到实现模块
    return getattr(_impl, name)
