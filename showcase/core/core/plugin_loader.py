"""
插件化工具箱系统
- 扫描 /tools 下所有文件夹，读取 manifest.json
- 自动注册工具到 DeepSeek 的 tools 参数
- 支持启用/禁用/热加载
"""
import os
import json
import importlib
from pathlib import Path
from typing import Callable, Optional
from ..core.logging import setup_logging

logger = setup_logging()

TOOLS_DIR = Path(__file__).parent.parent.parent / "tools"


class ToolPlugin:
    """一个工具插件"""

    def __init__(self, manifest: dict, handler: Optional[Callable] = None):
        self.name = manifest["name"]
        self.description = manifest.get("description", "")
        self.enabled = manifest.get("enabled", True)
        self.parameters = manifest.get("parameters", {"type": "object", "properties": {}})
        self.handler = handler
        self.manifest_path = manifest.get("_path", "")

    @property
    def tool_def(self) -> dict:
        """生成 DeepSeek Function Calling 格式的工具定义"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }


class PluginLoader:
    """插件加载器"""

    def __init__(self):
        self._plugins: dict[str, ToolPlugin] = {}
        self._scan_tools()

    def _scan_tools(self):
        """扫描 tools/ 目录加载所有插件"""
        if not TOOLS_DIR.exists():
            logger.warning(f"工具目录不存在: {TOOLS_DIR}")
            return

        for folder in sorted(TOOLS_DIR.iterdir()):
            if not folder.is_dir():
                continue
            manifest_path = folder / "manifest.json"
            if not manifest_path.exists():
                continue

            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                manifest["_path"] = str(folder)

                if not manifest.get("enabled", True):
                    logger.info(f"🔌 插件已禁用: {manifest['name']}")
                    continue

                # 尝试加载 handler
                handler = None
                handler_module = manifest.get("handler", "")
                handler_func = manifest.get("handler_func", "handle")
                if handler_module:
                    try:
                        mod = importlib.import_module(handler_module)
                        handler = getattr(mod, handler_func, None)
                    except Exception as e:
                        logger.warning(f"加载插件 handler 失败 {manifest['name']}: {e}")

                plugin = ToolPlugin(manifest, handler)
                self._plugins[plugin.name] = plugin
                status = "✅" if handler else "⚠️ (无handler)"
                logger.info(f"🔌 加载插件: {plugin.name} - {plugin.description} {status}")

            except Exception as e:
                logger.warning(f"加载插件 {folder.name} 失败: {e}")

    def get_tool_defs(self, enabled_only: bool = True, filter_names: list = None) -> list:
        """获取所有启用的工具定义列表"""
        result = []
        for name, plugin in self._plugins.items():
            if enabled_only and not plugin.enabled:
                continue
            if filter_names and name not in filter_names:
                continue
            result.append(plugin.tool_def)
        return result

    async def call_tool(self, tool_name: str, **kwargs):
        """调用工具"""
        plugin = self._plugins.get(tool_name)
        if not plugin:
            return {"error": f"工具不存在: {tool_name}"}
        if not plugin.enabled:
            return {"error": f"工具已禁用: {tool_name}"}
        if not plugin.handler:
            return {"error": f"工具无 handler: {tool_name}"}

        try:
            if asyncio.iscoroutinefunction(plugin.handler):
                return await plugin.handler(**kwargs)
            else:
                return plugin.handler(**kwargs)
        except Exception as e:
            logger.error(f"工具调用失败 {tool_name}: {e}")
            return {"error": str(e)}

    def list_plugins(self) -> list[dict]:
        """列出所有插件"""
        return [
            {
                "name": p.name,
                "description": p.description,
                "enabled": p.enabled,
                "has_handler": p.handler is not None,
            }
            for p in self._plugins.values()
        ]


# 需要异步支持
import asyncio

# ============================================================
# 全局单例
# ============================================================
_loader = None


def get_plugin_loader() -> PluginLoader:
    global _loader
    if _loader is None:
        _loader = PluginLoader()
    return _loader
