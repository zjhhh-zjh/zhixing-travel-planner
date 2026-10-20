/**
 * 多段自驾行程 UI Skill —— 类型定义 [MERGE] 修复 156
 * skill_name: driving_multi_segment
 *
 * 约定（与多段航班同构，但为**只读确认**）：
 * 1. 本文件字段名与后端 Skill（app/skills/driving_multi_segment/driving_skill_schema.py）
 *    返回的 JSON **严格一致**，前端不得私自改名或增删。
 * 2. 后端一次性【并行】拉取全部行程段路线，出参用 segments[] 承载每段数据；
 *    **段数动态可变（1/2/3 段…）**，前端一律按 segments.length 生成子步骤，禁止硬编码。
 * 3. 每段只给**一条**高德默认策略的最优路线，**用户不能选路线策略**，只能整批【确认】；
 *    提交内容只带 segment_id（无 option_id / 班次号），后端不做任何时间/路线校验。
 */

// ==================== Skill 标识 ====================

/** Agent UI Skill 注册键：上层用该字段匹配「应该渲染哪个前端组件」 */
export const DRIVING_MULTI_SEGMENT_SKILL = 'driving_multi_segment' as const;

/** skill_name 字面量类型 */
export type DrivingMultiSegmentSkillName = typeof DRIVING_MULTI_SEGMENT_SKILL;

// ==================== 核心数据契约 ====================

/** 一段自驾路线（segments 的元素）；段数由用户行程决定，动态可变 */
export interface DrivingSegment {
  /** 段唯一标识，后端按 legs 顺序生成：s1 / s2 / s3 … */
  segment_id: string;
  /** 该段出发城市 */
  from: string;
  /** 该段到达城市 */
  to: string;
  /** 该段出行日期 YYYY-MM-DD；未知时为空字符串（自驾可不带日期） */
  date: string;
  /** 该段是否查询成功：true 才可继续；false 时前端置灰该段并说明原因 */
  ok: boolean;
  /** 驾车总距离（公里）；未解析到时为 0 */
  distance_km: number;
  /** 预计耗时 HH:mm；未解析到时为空字符串 */
  duration_text: string;
  /** 过路费（元）；未解析到或免费时为 0 */
  tolls: number;
  /** 红绿灯数；未解析到时为 0 */
  traffic_lights: number;
  /** 关键路段摘要（前几条导航指令）；无则为空字符串 */
  summary: string;
}

/** 后端 Skill 出参：Agent 并行查询全部段后一次性返回 */
export interface DrivingMultiSegmentSkillData {
  skill_name: DrivingMultiSegmentSkillName;
  segments: DrivingSegment[];
}

// ==================== 前端状态机（与航班同构，但无“选择”环节） ====================

/**
 * 子步骤状态机（按 segments.length 动态展开，不写死段数）
 *   select   逐段核对阶段：配合 stepIndex（0 … N-1）表示正在核对第 N 段（只读，无选项）
 *   preview  预览汇总页（第 N+1 步），唯一提供【提交确认】的页面
 *   submit   已提交（按钮置为已提交态）
 */
export type DrivingStage = 'select' | 'preview' | 'submit';

/** 一次性全局提交的 payload（只读确认：每段仅需 segment_id） */
export interface DrivingMultiSegmentSubmitPayload {
  skill_name: DrivingMultiSegmentSkillName;
  /** 按 segments 顺序提交，每段带 segment_id */
  segments: Array<{ segment_id: string }>;
}

/** 后端提交回执（对应 driving_skill_schema.py 的 DrivingSubmitResult） */
export interface DrivingSubmitResult {
  /** 全部段基础校验通过为 true（后端不做任何时间/路线校验） */
  ok: boolean;
  /** OK / EMPTY_SELECTION / SEGMENT_NOT_FOUND / INCOMPLETE / NO_PAYLOAD / INVALID_PAYLOAD */
  code: string;
  /** 面向用户的中文说明 */
  msg: string;
  accepted: Array<{ segment_id: string }>;
  /** 已确认各段的展示文本（后端据此拼续跑发言） */
  texts?: string[];
}