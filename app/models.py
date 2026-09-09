# -*- coding: utf-8 -*-
"""状态机枚举与常量(云端/本地共用)。"""
import enum


class PrinterState(str, enum.Enum):
    OFFLINE = "OFFLINE"            # 断网/断电/未上线
    IDLE = "IDLE"                  # 空闲·健康,可派单
    PRINTING = "PRINTING"          # 正在打印某小组
    WAIT_PAPER = "WAIT_PAPER"      # 缺纸等待(不转派,加纸后续打)
    SUSPECT = "SUSPECT"            # 疑似故障,待人工确认
    QUARANTINED = "QUARANTINED"    # 已隔离,不接任务
    RESTING = "RESTING"            # 达到连续打印上限后的强制休息
    MAINTENANCE = "MAINTENANCE"    # 维护页模式


class GroupStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    PRINTING = "PRINTING"
    DONE = "DONE"
    PAUSED = "PAUSED"              # 重试超限,待人工处理


class TaskStatus(str, enum.Enum):
    """任务粗粒度状态(云端显示与本地执行共用同一套值)。"""
    QUEUED = "QUEUED"                  # 排队中
    PRINTING = "PRINTING"              # 打印中
    AWAITING_PICKUP = "AWAITING_PICKUP"  # 待收纸(闸口)
    DONE = "DONE"                      # 已完成
    CANCELED = "CANCELED"


TASK_STATUS_CN = {
    "QUEUED": "排队中",
    "PRINTING": "打印中",
    "AWAITING_PICKUP": "待收纸",
    "DONE": "已完成",
    "CANCELED": "已取消",
}

ROLE_ADMIN = "admin"
ROLE_TEACHER = "teacher"

STATE_COLOR = {
    "OFFLINE": "#9e9e9e",
    "IDLE": "#2e7d32",
    "PRINTING": "#1565c0",
    "WAIT_PAPER": "#f9a825",
    "SUSPECT": "#ef6c00",
    "QUARANTINED": "#c62828",
    "RESTING": "#7b1fa2",
    "MAINTENANCE": "#6a1b9a",
}
