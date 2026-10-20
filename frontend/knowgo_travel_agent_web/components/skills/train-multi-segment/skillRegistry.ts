/**
 * Agent UI Skill 注册表：skill_name → 前端渲染组件
 *
 * 用法（Agent 帧到达时）：
 *   const Comp = UI_SKILL_REGISTRY[frame.skill_name];
 *   if (Comp) return <Comp data={frame.data} onSubmit={handleSubmit} />;
 *
 * 新增一个 UI Skill 只需两步：
 *   1. 在 components/skills/{skill-name}/ 下实现组件（对外 props 为 data + onSubmit）；
 *   2. 在下面的注册表里追加一行，键必须是后端下发的 skill_name（前后端严格同名）。
 */
import TrainMultiSegment from './TrainMultiSegment';
// [MERGE] 多段住宿 Skill：与高铁同注册表（组件实现放同级 hotel-multi-segment 目录）
import HotelMultiSegment from '../hotel-multi-segment/HotelMultiSegment';
// [MERGE] 修复 155：多段航班 Skill（组件实现放同级 flight-multi-segment 目录）
import FlightMultiSegment from '../flight-multi-segment/FlightMultiSegment';
// [MERGE] 修复 156：多段自驾 Skill（组件实现放同级 driving-multi-segment 目录）
import DrivingMultiSegment from '../driving-multi-segment/DrivingMultiSegment';

import { HOTEL_MULTI_SEGMENT_SKILL, TRAIN_MULTI_SEGMENT_SKILL } from './types';
import { FLIGHT_MULTI_SEGMENT_SKILL } from '../flight-multi-segment/types';
import { DRIVING_MULTI_SEGMENT_SKILL } from '../driving-multi-segment/types';

/** 所有 UI Skill 组件的统一 props 形态：后端 payload + 全局提交回调 */
export interface UiSkillProps<TData, TPayload> {
  /** 后端 Skill 出参（结构化 payload，字段与后端 Pydantic 模型严格一致） */
  data: TData;
  /** 提交回调：仅在用户完成全部子步骤并确认时触发一次 */
  onSubmit: (payload: TPayload) => void;
  /** 提交中标记（由上层控制，用于置灰提交按钮） */
  submitting?: boolean;
  /** [MERGE] 已提交标记：卡片提交成功后保留展示并锁定交互（上一步/提交均禁用） */
  submitted?: boolean;
  /** [MERGE] 各段选中 id 账本（提交成功后由上层记住，切回会话时复原已选状态） */
  initialSelections?: Record<string, string>;
}

/** 已注册的 UI Skill 组件联合类型（供上层取组件时做类型收敛） */
export type RegisteredSkillComponent =
  (typeof UI_SKILL_REGISTRY)[keyof typeof UI_SKILL_REGISTRY];

export const UI_SKILL_REGISTRY = {
  [TRAIN_MULTI_SEGMENT_SKILL]: TrainMultiSegment,
  // [MERGE] 多段住宿：键 = 后端下发的 skill_name，前后端严格同名
  [HOTEL_MULTI_SEGMENT_SKILL]: HotelMultiSegment,
  // [MERGE] 修复 155：多段航班：键 = 后端下发的 skill_name，前后端严格同名
  [FLIGHT_MULTI_SEGMENT_SKILL]: FlightMultiSegment,
  // [MERGE] 修复 156：多段自驾：键 = 后端下发的 skill_name，前后端严格同名
  [DRIVING_MULTI_SEGMENT_SKILL]: DrivingMultiSegment,
} as const;