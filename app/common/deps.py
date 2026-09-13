"""共享依赖注入：从应用状态取存储门面。

``create_app`` 装配时把唯一的 ``Database`` 实例挂到 ``app.state.db``，
各路由经此依赖取得同一连接，不另行打开数据库。
"""

from fastapi import Request


def get_db(request: Request):
    return request.app.state.db
