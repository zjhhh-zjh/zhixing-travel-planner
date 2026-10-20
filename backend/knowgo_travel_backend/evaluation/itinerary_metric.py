"""行程质量三维度 GEval 指标

实现已收编至业务服务 app/services/quality_check_service.py（在线质检与
离线评测共用同一套标准，避免双份维护、口径漂移）。本文件保留为薄转发，
维持 evaluation/ 既有 import 路径不破坏。
"""
from deepeval.metrics import GEval

from app.services.quality_check_service import build_itinerary_metrics

__all__ = ["build_itinerary_metrics"]