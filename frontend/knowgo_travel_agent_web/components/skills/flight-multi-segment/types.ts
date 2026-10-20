/**
 * 多段航班行程 UI Skill —— 类型定义 + 后端契约 JSON Schema [MERGE] 修复 155
 * skill_name: flight_multi_segment
 *
 * 约定（与多段高铁同构）：
 * 1. 本文件字段名与后端 Skill（app/skills/flight_multi_segment/flight_skill_schema.py）
 *    返回的 JSON **严格一致**，前端不得私自改名或增删。
 * 2. 后端一次性【并行】拉取全部行程段航班，出参用 segments[] 承载每段数据；
 *    **段数动态可变（1/2/3 段…）**，前端一律按 segments.length 生成子步骤，禁止硬编码。
 * 3. 前端不做任何查询、不做任何航班时间冲突校验；只在最后一页做一次全局提交，
 *    提交内容为每段的 segment_id + option_id（航班号可能不唯一，不能当身份）+ flight_no（展示用）。
 */

// ==================== Skill 标识 ====================

/** Agent UI Skill 注册键：上层用该字段匹配「应该渲染哪个前端组件」 */
export const FLIGHT_MULTI_SEGMENT_SKILL = 'flight_multi_segment' as const;

/** skill_name 字面量类型 */
export type FlightMultiSegmentSkillName = typeof FLIGHT_MULTI_SEGMENT_SKILL;

// ==================== 核心数据契约 ====================

/** 单个航班（segments[].flight_list 的元素） */
export interface FlightOption {
  /** 航班号，如 CA1501 */
  flight_no: string;
  /** 计划起飞时刻 HH:mm */
  dep_time: string;
  /** 计划到达时刻 HH:mm */
  arr_time: string;
  /** 出发机场三字码或名称，如 PEK */
  dep_airport: string;
  /** 到达机场三字码或名称，如 SHA */
  arr_airport: string;
  /** 票价（元 / 人）；数据源未返回价格时为 0（界面显示"价格待定"） */
  price: number;
  /** 历时 HH:mm；无法解析时为空字符串 */
  duration: string;
  /**
   * 段内唯一标识：`航班号-起飞时刻-到达机场`（如 CA1501-08:00-SHA）。
   *
   * **身份字段**：同一天同一航班号可能执行不同航段/时刻，故选中态、React key、
   * 提交回传一律用本字段，禁止用 flight_no（否则后端回写会取错航班）。
   */
  option_id: string;
}

/** 一段行程（segments 的元素）；段数由用户行程决定，动态可变 */
export interface FlightSegment {
  /** 段唯一标识，后端按 legs 顺序生成：s1 / s2 / s3 … */
  segment_id: string;
  /** 该段出发城市 */
  from: string;
  /** 该段到达城市 */
  to: string;
  /** 该段乘机日期 YYYY-MM-DD */
  date: string;
  /** 该段可选航班（后端已按起飞时间升序）；查询失败时为空数组 */
  flight_list: FlightOption[];
}

/** 后端 Skill 出参：Agent 并行查询全部段后一次性返回 */
export interface FlightMultiSegmentSkillData {
  skill_name: FlightMultiSegmentSkillName;
  segments: FlightSegment[];
}

// ==================== 前端状态机（与高铁同构） ====================

/**
 * 子步骤状态机（按 segments.length 动态展开，不写死段数）
 *   select   段选择阶段：配合 stepIndex（0 … N-1）表示正在选第 N 段
 *   preview  预览汇总页（第 N+1 步），唯一提供【提交确认】的页面
 *   submit   已提交（按钮置为已提交态）
 */
export type FlightStage = 'select' | 'preview' | 'submit';

/** 选择账本：segment_id → option_id（单一数据源，回退修改只需覆盖对应项） */
export type FlightSelectionMap = Record<string, string>;

/** 一次性全局提交的 payload */
export interface FlightMultiSegmentSubmitPayload {
  skill_name: FlightMultiSegmentSkillName;
  /** 按 segments 顺序提交，每段带 segment_id + option_id（身份）+ flight_no（展示用） */
  segments: Array<{ segment_id: string; option_id: string; flight_no: string }>;
}

/** 后端提交回执（对应 flight_skill_schema.py 的 FlightSubmitResult） */
export interface FlightSubmitResult {
  /** 全部段基础校验通过为 true（后端不做任何时间校验） */
  ok: boolean;
  /** OK / EMPTY_SELECTION / MISSING_OPTION_ID / SEGMENT_NOT_FOUND / FLIGHT_NOT_FOUND / INCOMPLETE / NO_PAYLOAD / INVALID_PAYLOAD */
  code: string;
  /** 面向用户的中文说明 */
  msg: string;
  accepted: Array<{ segment_id: string; option_id: string; flight_no: string }>;
  /** 已选定各段的展示文本（后端据此拼续跑发言） */
  texts?: string[];
}