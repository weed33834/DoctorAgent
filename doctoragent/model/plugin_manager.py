"""DoctorAgent 插件管理器。

支持基于 entry_points 的自动发现、手动注册、加载/卸载生命周期管理。
插件可注入工具、扩展 Agent 行为、注册钩子。

生命周期::

    UNLOADED --load()--> LOADED --enable()--> ENABLED --disable()--> DISABLED
        |                  |                    |                      |
        +--ERROR<----------+--------------------+----------------------+
                           (任何阶段抛异常都会落入 ERROR，不崩溃)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from importlib.metadata import entry_points
from typing import Any

import structlog
from pydantic import BaseModel, Field

from doctoragent.compat import StrEnum

logger = structlog.get_logger(__name__)

# entry_points 组名：第三方插件通过在 pyproject/setup.cfg 中注册
# ``[project.entry-points."doctoragent.plugins"]`` 来被自动发现。
_PLUGIN_ENTRY_POINT_GROUP = "doctoragent.plugins"


class PluginState(StrEnum):
    """插件生命周期状态。"""

    UNLOADED = "unloaded"
    LOADED = "loaded"
    ENABLED = "enabled"
    DISABLED = "disabled"
    ERROR = "error"


class PluginInfo(BaseModel):
    """插件元信息快照，``list_plugins`` 返回该模型列表。"""

    name: str
    version: str
    description: str
    entry_point: str
    state: PluginState
    tools: list[str] = Field(default_factory=list)
    error: str = ""


class PluginBase(ABC):
    """所有插件必须继承的抽象基类。

    子类需实现 ``name`` / ``version`` / ``description`` 三个只读属性。
    ``on_load`` / ``on_unload`` / ``get_tools`` 有默认空实现，可按需覆写。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """插件唯一标识名。"""
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        """插件语义化版本号。"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """插件简短描述。"""
        ...

    def on_load(self) -> None:
        """插件加载时调用（默认空实现，子类可覆写做初始化）。"""

    def on_unload(self) -> None:
        """插件卸载时调用（默认空实现，子类可覆写做清理）。"""

    def get_tools(self) -> list[Any]:
        """返回插件提供的工具定义列表，空列表表示无工具。"""
        return []


class PluginManager:
    """插件管理器：发现、注册、加载/卸载、启用/禁用。

    内部维护三张映射，均以插件 ``name`` 为键：

    - ``_plugins``: 插件实例
    - ``_states``: 插件当前状态
    - ``_infos``: 插件元信息快照
    """

    def __init__(self) -> None:
        self._plugins: dict[str, PluginBase] = {}
        self._states: dict[str, PluginState] = {}
        self._infos: dict[str, PluginInfo] = {}

    # ------------------------------------------------------------------
    # 发现 & 注册
    # ------------------------------------------------------------------

    def discover(self) -> list[PluginInfo]:
        """通过 ``importlib.metadata.entry_points`` 发现已注册插件。

        发现的插件会被自动注册（但不会自动加载）。加载失败的插件
        以 ``ERROR`` 状态记录在返回列表中，不会抛异常。
        """
        infos: list[PluginInfo] = []
        try:
            eps = entry_points(group=_PLUGIN_ENTRY_POINT_GROUP)
        except Exception as exc:  # noqa: BLE001
            logger.error("plugin_discover_failed", error=str(exc))
            return infos

        for ep in eps:
            try:
                plugin_cls = ep.load()
                plugin = plugin_cls()
                self.register(plugin)
                # register 已创建 _infos 条目，取回返回。
                info = self._infos.get(plugin.name)
                if info is not None:
                    # 补充 entry_point 来源信息。
                    info.entry_point = ep.value
                    infos.append(info)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "plugin_entry_point_load_failed",
                    entry_point=ep.name,
                    error=str(exc),
                )
                infos.append(
                    PluginInfo(
                        name=ep.name,
                        version="",
                        description="",
                        entry_point=ep.value,
                        state=PluginState.ERROR,
                        tools=[],
                        error=str(exc),
                    )
                )
        logger.info("plugin_discover_done", count=len(infos))
        return infos

    def register(self, plugin: PluginBase) -> str:
        """手动注册插件实例，返回插件 ``name``。

        重复注册同名插件会覆盖旧实例（旧实例不会被自动卸载）。
        """
        name = plugin.name
        self._plugins[name] = plugin
        self._states[name] = PluginState.UNLOADED
        self._infos[name] = PluginInfo(
            name=name,
            version=plugin.version,
            description=plugin.description,
            entry_point="manual",
            state=PluginState.UNLOADED,
            tools=[],
            error="",
        )
        logger.info("plugin_registered", plugin=name, version=plugin.version)
        return name

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def load(self, name: str) -> None:
        """加载插件（调用 ``on_load``）。

        加载成功后状态变为 ``LOADED``，并刷新 ``PluginInfo.tools``。
        加载失败则状态变为 ``ERROR`` 并记录错误信息，不抛异常。
        """
        plugin = self._plugins.get(name)
        if plugin is None:
            logger.warning("plugin_load_not_found", plugin=name)
            return
        try:
            plugin.on_load()
            self._states[name] = PluginState.LOADED
            # 加载后刷新工具列表。
            tool_names = self._extract_tool_names(plugin)
            info = self._infos.get(name)
            if info is not None:
                info.tools = tool_names
                info.state = PluginState.LOADED
                info.error = ""
            logger.info("plugin_loaded", plugin=name, tools=tool_names)
        except Exception as exc:  # noqa: BLE001
            self._set_error(name, exc)
            logger.error("plugin_load_error", plugin=name, error=str(exc))

    def unload(self, name: str) -> None:
        """卸载插件（调用 ``on_unload``）。

        卸载失败同样落入 ``ERROR`` 状态，不抛异常。
        """
        plugin = self._plugins.get(name)
        if plugin is None:
            logger.warning("plugin_unload_not_found", plugin=name)
            return
        try:
            plugin.on_unload()
            self._states[name] = PluginState.UNLOADED
            info = self._infos.get(name)
            if info is not None:
                info.state = PluginState.UNLOADED
                info.tools = []
                info.error = ""
            logger.info("plugin_unloaded", plugin=name)
        except Exception as exc:  # noqa: BLE001
            self._set_error(name, exc)
            logger.error("plugin_unload_error", plugin=name, error=str(exc))

    def enable(self, name: str) -> None:
        """启用插件（须已加载）。"""
        state = self._states.get(name)
        if state not in (PluginState.LOADED, PluginState.DISABLED):
            logger.warning("plugin_enable_invalid_state", plugin=name, state=str(state))
            return
        self._states[name] = PluginState.ENABLED
        info = self._infos.get(name)
        if info is not None:
            info.state = PluginState.ENABLED
        logger.info("plugin_enabled", plugin=name)

    def disable(self, name: str) -> None:
        """禁用插件。"""
        self._states[name] = PluginState.DISABLED
        info = self._infos.get(name)
        if info is not None:
            info.state = PluginState.DISABLED
        logger.info("plugin_disabled", plugin=name)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_plugin(self, name: str) -> PluginBase | None:
        """按名获取插件实例，不存在返回 ``None``。"""
        return self._plugins.get(name)

    def list_plugins(self) -> list[PluginInfo]:
        """返回所有已注册插件的元信息快照。"""
        return list(self._infos.values())

    def get_all_tools(self) -> list[Any]:
        """获取所有已启用（``ENABLED``）插件提供的工具定义列表。"""
        tools: list[Any] = []
        for name, plugin in self._plugins.items():
            if self._states.get(name) != PluginState.ENABLED:
                continue
            try:
                tools.extend(plugin.get_tools())
            except Exception as exc:  # noqa: BLE001
                logger.error("plugin_get_tools_failed", plugin=name, error=str(exc))
        return tools

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _set_error(self, name: str, exc: BaseException) -> None:
        """将插件标记为 ERROR 状态并记录错误信息。"""
        self._states[name] = PluginState.ERROR
        info = self._infos.get(name)
        if info is not None:
            info.state = PluginState.ERROR
            info.error = str(exc)

    @staticmethod
    def _extract_tool_names(plugin: PluginBase) -> list[str]:
        """从插件的 ``get_tools()`` 结果中提取工具名列表。

        工具定义可能是字符串、pydantic 模型或普通 dict，统一取
        ``name`` 属性 / 键；无法识别的以 ``str(tool)`` 兜底。
        """
        try:
            raw_tools = plugin.get_tools()
        except Exception:  # noqa: BLE001
            return []
        names: list[str] = []
        for tool in raw_tools:
            if isinstance(tool, str):
                names.append(tool)
            elif isinstance(tool, dict):
                names.append(str(tool.get("name", tool)))
            else:
                name_attr = getattr(tool, "name", None)
                if name_attr:
                    names.append(str(name_attr))
                else:
                    names.append(str(tool))
        return names
