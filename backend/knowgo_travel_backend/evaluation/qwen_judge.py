"""Qwen 评测裁判模型适配器

实现已收编至业务服务 app/services/quality_check_service.py（在线质检与
离线评测共用同一裁判，避免双份维护）。本文件保留为薄转发，维持
evaluation/ 既有 import 路径不破坏。
"""
from app.services.quality_check_service import QwenJudge

__all__ = ["QwenJudge"]