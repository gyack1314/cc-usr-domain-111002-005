"""门户资料采集模块: 按筛选意图遍历级联组合并记录页面实际执行的条件与资料。"""
from .core import Collector, Intent, progress_markdown, write_outputs

__all__ = ["Collector", "Intent", "progress_markdown", "write_outputs"]
