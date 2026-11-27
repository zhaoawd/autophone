"""`autophone/adapters/http/app.py` 逐组切出的 router 模块（console-split 文档 §1.2）。

每个模块给出一个 `create_<domain>_router(...) -> APIRouter`；跨组共用的依赖由组合根
（当前仍是 `autophone/adapters/http/app.py`）显式传入，形状同 `autophone/control/routes.py`
的 `create_control_router`。
"""
