/**
 * 多段高铁行程 UI Skill —— 类型定义 + 后端契约 JSON Schema
 * skill_name: train_multi_segment
 *
 * 约定：
 * 1. 本文件字段名与后端 Skill（app/skills/train_multi_segment/train_skill_schema.py）
 *    返回的 JSON **严格一致**，前端不得私自改名或增删。
 * 2. 后端一次性【并行】拉取全部行程段班次，出参用 segments[] 承载每段数据；
 *    **段数动态可变（2/3/4 段…）**，前端一律按 segments.length 生成子步骤，禁止硬编码。
 * 3. 前端不做任何查询、不做任何列车时间冲突校验（不比对到站/发车先后）；
 *    只在最后一页做一次全局提交，提交内容为每段的 segment_id + option_id（车次号在同段可重复，
 *    不能当身份）+ trainNo（展示用）。
 */

// ==================== Skill 标识 ====================

/** Agent UI Skill 注册键：上层用该字段匹配「应该渲染哪个前端组件」 */
export const TRAIN_MULTI_SEGMENT_SKILL = 'train_multi_segment' as const;

/** skill_name 字面量类型 */
export type TrainMultiSegmentSkillName = typeof TRAIN_MULTI_SEGMENT_SKILL;

// ==================== 核心数据契约 ====================

/** 单趟车次（segments[].train_list 的元素） */
export interface TrainOption {
  /** 车次号，如 D40 / G547 / G1952 */
  trainNo: string;
  /** 发车时刻 HH:mm */
  startTime: string;
  /** 到达时刻 HH:mm */
  endTime: string;
  /** 出发站中文名 */
  startStation: string;
  /** 到达站中文名 */
  endStation: string;
  /** 历时 HH:mm */
  duration: string;
  /** 票价（元 / 人） */
  price: number;
  /** 座位类型，如 二等座 / 一等座 */
  seatType: string;
  /** 余票状态，如 有票 / 无票 / 候补 / 3张 */
  ticketStatus: string;
  /**
   * 段内唯一标识：`车次-发车时刻-到达站`（如 G1308-08:36-正定机场）。
   *
   * **身份字段**：12306 真实数据里同一段会出现同号不同终到站的车
   * （G1308 08:36 分别终到 石家庄 / 正定机场），故选中态、React key、
   * 提交回传一律用本字段，禁止用 trainNo（否则两趟会被同时勾选，后端回写也会取错趟次）。
   */
  option_id: string;
}

/** 一段行程（segments 的元素）；段数由用户行程决定，动态可变 */
export interface TrainSegment {
  /** 段唯一标识，后端按 legs 顺序生成：s1 / s2 / s3 … */
  segment_id: string;
  /** 该段出发城市 */
  from: string;
  /** 该段到达城市 */
  to: string;
  /** 该段乘车日期 YYYY-MM-DD */
  date: string;
  /** 该段可选车次（后端已按发车时间升序）；查询失败时为空数组 */
  train_list: TrainOption[];
}

/** 后端 Skill 出参：Agent 并行查询全部段后一次性返回 */
export interface TrainMultiSegmentSkillData {
  skill_name: TrainMultiSegmentSkillName;
  segments: TrainSegment[];
}

// ==================== 前端状态机 ====================

/**
 * 子步骤状态机（按 segments.length 动态展开，不写死段数）
 *   select   段选择阶段：配合 stepIndex（0 … N-1）表示正在选第 N 段
 *   preview  预览汇总页（第 N+1 步），唯一提供【提交确认】的页面
 *   submit   已提交（按钮置为已提交态；逐段单独确认已取消）
 *
 * 以 3 段为例，用户实际看到的步序是：选 s1 → 选 s2 → 选 s3 → 预览汇总，
 * 即 N 段行程共有 N 个选择步 + 1 个预览步；4 段则自动变成 4+1 步。
 */
export type TrainStage = 'select' | 'preview' | 'submit';

/** 选择账本：segment_id → option_id（单一数据源，回退修改只需覆盖对应项） */
export type TrainSelectionMap = Record<string, string>;

/** 一次性全局提交的 payload（逐段单独确认已取消） */
export interface TrainMultiSegmentSubmitPayload {
  skill_name: TrainMultiSegmentSkillName;
  /** 按 segments 顺序提交，每段带 segment_id + option_id（身份）+ trainNo（展示用） */
  segments: Array<{ segment_id: string; option_id: string; trainNo: string }>;
}

/** 后端提交回执（对应 train_skill_schema.py 的 TrainSubmitResult） */
export interface TrainSubmitResult {
  /** 全部段基础校验通过为 true（后端不做任何时间校验） */
  ok: boolean;
  /** OK / EMPTY_SELECTION / SEGMENT_NOT_FOUND / TRAIN_NOT_FOUND / MISSING_OPTION_ID / INCOMPLETE / NO_PAYLOAD / INVALID_PAYLOAD */
  code: string;
  /** 面向用户的中文说明 */
  msg: string;
  accepted: Array<{ segment_id: string; option_id: string; trainNo: string }>;
  /** 已选定各段的展示文本（后端据此拼续跑发言） */
  texts?: string[];
}

// ==================== 后端 Skill JSON Schema ====================

/**
 * 【入参 JSON Schema】Agent 调用 train_multi_segment 工具时传入的参数。
 *
 * 语义：legs 是「按时间顺序排列的行程段」；闭环行程最后一段回到起点
 *       （例：石家庄→北京→上海→石家庄 ⇒ 3 段）。**段数不设上限，2/3/4 段均可**。
 * 后端职责：拿到 legs 后**并行**查询全部段班次（asyncio.gather，非串行逐段查），
 *           查完后一次性组装出参返回；前端不做任何网络查询。
 */
export const TRAIN_SKILL_INPUT_SCHEMA = {
  type: 'object',
  required: ['legs'],
  properties: {
    legs: {
      type: 'array',
      minItems: 1,
      description: '按顺序排列的行程段（闭环时最后一段回到起点），长度即段数',
      items: {
        type: 'object',
        required: ['from', 'to', 'travel_date'],
        properties: {
          from: { type: 'string', description: '该段出发城市，如 石家庄' },
          to: { type: 'string', description: '该段到达城市，如 北京' },
          travel_date: {
            type: 'string',
            description: '该段乘车日期 YYYY-MM-DD（后端同时兼容 date 字段名）',
          },
        },
      },
    },
    seat_type: {
      type: 'string',
      description: '可选，期望席别（二等座 / 一等座 / 商务座），默认二等座',
    },
    max_options_per_leg: {
      type: 'integer',
      minimum: 1,
      maximum: 30,
      description: '可选，每段返回的车次上限，默认 5',
    },
  },
} as const;

/**
 * 【出参 JSON Schema】后端 Skill 返回给前端 UI Skill 的结构。
 *
 * 与前端 TrainMultiSegmentSkillData 一一对应；上层收到该结构后按 skill_name 查
 * skillRegistry 渲染 <TrainMultiSegment /> 组件。segments 长度动态，前端不得假设段数。
 */
export const TRAIN_SKILL_OUTPUT_SCHEMA = {
  type: 'object',
  required: ['skill_name', 'segments'],
  properties: {
    skill_name: {
      type: 'string',
      const: TRAIN_MULTI_SEGMENT_SKILL,
      description: 'UI Skill 注册键，前端据此匹配渲染组件',
    },
    segments: {
      type: 'array',
      minItems: 1,
      description: '全部行程段，顺序与入参 legs 一致（并行查询后统一组装），长度即段数',
      items: {
        type: 'object',
        required: ['segment_id', 'from', 'to', 'date', 'train_list'],
        properties: {
          segment_id: { type: 'string', description: '段唯一标识，如 s1 / s2 / s3' },
          from: { type: 'string', description: '该段出发城市' },
          to: { type: 'string', description: '该段到达城市' },
          date: { type: 'string', description: '该段乘车日期 YYYY-MM-DD' },
          train_list: {
            type: 'array',
            description: '该段可选车次（后端已按发车时间升序）；为空表示该段未查到班次',
            items: {
              type: 'object',
              required: [
                'option_id',
                'trainNo',
                'startTime',
                'endTime',
                'startStation',
                'endStation',
                'duration',
                'price',
                'seatType',
                'ticketStatus',
              ],
              properties: {
                option_id: {
                  type: 'string',
                  description:
                    '段内唯一标识：车次-发车时刻-到达站（同车次号可能有多趟，故身份以本字段为准）',
                },
                trainNo: { type: 'string', description: '车次号，如 D40 / G547 / G1952' },
                startTime: { type: 'string', description: '发车时刻 HH:mm' },
                endTime: { type: 'string', description: '到达时刻 HH:mm' },
                startStation: { type: 'string', description: '出发站中文名' },
                endStation: { type: 'string', description: '到达站中文名' },
                duration: { type: 'string', description: '历时 HH:mm' },
                price: { type: 'number', description: '票价（元/人）' },
                seatType: { type: 'string', description: '座位类型' },
                ticketStatus: { type: 'string', description: '余票状态：有票 / 无票 / 候补 / N张' },
              },
            },
          },
        },
      },
    },
  },
} as const;

/**
 * 【提交流程 Schema】
 * 前端：预览页点【提交确认】→ onSubmit 抛出 TrainMultiSegmentSubmitPayload
 *      （segment_id + option_id 身份 + trainNo 展示）。
 * 后端：submit_train_multi_segment_tool 只做基础合法性校验（段/趟次是否存在、是否每段都选了），
 *       **明确不比对任何列车时间**，返回 TrainSubmitResult。
 */
export const TRAIN_SKILL_SUBMIT_SCHEMA = {
  request: {
    type: 'object',
    required: ['skill_name', 'segments'],
    properties: {
      skill_name: { type: 'string', const: TRAIN_MULTI_SEGMENT_SKILL },
      segments: {
        type: 'array',
        description: '全部段的选中结果，顺序不限（后端按 segment_id 归位）',
        items: {
          type: 'object',
          required: ['segment_id', 'option_id'],
          properties: {
            segment_id: { type: 'string', description: '必须存在于下发 payload 的 segments 中' },
            option_id: {
              type: 'string',
              description: '身份字段，必须存在于该段 train_list 的 option_id 中',
            },
            trainNo: { type: 'string', description: '车次号，随 option_id 回传，仅作展示/日志' },
          },
        },
      },
    },
  },
  response: {
    type: 'object',
    required: ['ok', 'code', 'msg', 'accepted'],
    properties: {
      ok: { type: 'boolean', description: '全部段基础校验通过为 true' },
      code: {
        type: 'string',
        description:
          'OK / EMPTY_SELECTION / SEGMENT_NOT_FOUND / TRAIN_NOT_FOUND / INCOMPLETE / NO_PAYLOAD / INVALID_PAYLOAD',
      },
      msg: { type: 'string', description: '面向用户的中文说明' },
      accepted: {
        type: 'array',
        description: '校验通过的选中项（原样回显）',
        items: {
          type: 'object',
          required: ['segment_id', 'option_id'],
          properties: {
            segment_id: { type: 'string' },
            option_id: { type: 'string' },
            trainNo: { type: 'string' },
          },
        },
      },
    },
  },
} as const;

// ============================================================================
// ============================ 多段住宿 UI Skill ==============================
// ============================================================================

/**
 * 多段住宿选择 UI Skill —— 类型定义 + 后端契约 JSON Schema
 * skill_name: hotel_multi_segment
 *
 * 与高铁 Skill 同构：后端一次性【并行】拉取全部住宿段候选，出参用 segments[] 承载每段数据；
 * **段数动态可变（2/3/多段，= 行程目的地数量）**，前端一律按 segments.length 生成子步骤。
 *
 * 与高铁的唯一差异：每段**自带 mode**，段与段之间独立（可混合）：
 *   - mode='hotel'：该段有真实门店 → 渲染门店单选卡片（名称/总价/每晚单价/评分/地址）
 *   - mode='brand'：该段实时报价不可用 → 渲染品牌单选卡片，顶部加提示文案
 * 约定的规则与高铁一致：前端不做任何查询、不做任何日期/时间校验，只在预览页做一次全局提交。
 */

/** Agent UI Skill 注册键：上层用该字段匹配「应该渲染哪个前端组件」 */
export const HOTEL_MULTI_SEGMENT_SKILL = 'hotel_multi_segment' as const;

/** skill_name 字面量类型 */
export type HotelMultiSegmentSkillName = typeof HOTEL_MULTI_SEGMENT_SKILL;

/** 单段渲染模式：hotel=真实门店候选；brand=品牌兜底候选（实时报价不可用） */
export type HotelMode = 'hotel' | 'brand';

/** 单个真实酒店门店（segments[].hotel_list 的元素） */
export interface HotelOption {
  /** 段内唯一标识，格式 `{segment_id}-{序号}`，如 h1-1（第 1 段第 1 家门店） */
  hotel_id: string;
  /** 门店名称，如 尚客优酒店（石家庄长安区谈固北大街店） */
  hotelName: string;
  /** 住宿总价（元），整个入住期间合计 */
  totalPrice: number;
  /** 每晚单价（元/晚） */
  pricePerNight: number;
  /** 评分 / 星级，如 2.0 / 4.5 */
  starRating: number;
  /** 门店地址（含城区） */
  address: string;
}

/** 单个酒店品牌（segments[].brand_list 的元素，实时报价不可用时的兜底候选） */
export interface HotelBrand {
  /** 段内唯一标识，格式 `{segment_id}-{序号}`，如 b2-1（第 2 段第 1 个品牌） */
  brand_id: string;
  /** 品牌名称，如 如家 / 汉庭 / 7 天 */
  brandName: string;
}

/** 一段住宿（segments 的元素）；段数由行程目的地数量决定，动态可变 */
export interface HotelSegment {
  /** 段唯一标识，后端按入参顺序生成：h1 / h2 / h3 … */
  segment_id: string;
  /** 该段住宿城市 */
  city: string;
  /** 该段入住日期 YYYY-MM-DD */
  checkInDate: string;
  /** 该段离店日期 YYYY-MM-DD */
  checkOutDate: string;
  /** 入住人数 */
  guestCount: number;
  /** 房型档位，如 经济型 / 舒适型 / 星级酒店 / 民宿 */
  priceLevel: string;
  /** 该段渲染模式（每段独立判定，可混合） */
  mode: HotelMode;
  /** 门店候选；mode='brand' 时恒为空数组 */
  hotel_list: HotelOption[];
  /** 品牌候选；mode='hotel' 时恒为空数组 */
  brand_list: HotelBrand[];
}

/** 后端 Skill 出参：Agent 并行查询全部住宿段后一次性返回 */
export interface HotelMultiSegmentSkillData {
  skill_name: HotelMultiSegmentSkillName;
  segments: HotelSegment[];
}

// ==================== 前端状态机（与高铁同构） ====================

/**
 * 子步骤状态机（按 segments.length 动态展开，不写死段数）
 *   select   段选择阶段：配合 stepIndex（0 … N-1）表示正在选第 N 段住宿
 *   preview  预览汇总页（第 N+1 步），唯一提供【提交确认】的页面
 *   submit   已提交（按钮置为已提交态；逐段单独确认已取消）
 */
export type HotelStage = 'select' | 'preview' | 'submit';

/** 选择账本：segment_id → 选中的 hotel_id / brand_id（单一数据源，回退修改只需覆盖对应项） */
export type HotelSelectionMap = Record<string, string>;

/** 一次性全局提交的 payload（逐段单独确认已取消） */
export interface HotelMultiSegmentSubmitPayload {
  skill_name: HotelMultiSegmentSkillName;
  /** 按 segments 顺序提交：hotel 段回传 hotel_id，brand 段回传 brand_id（另一方为空串） */
  segments: Array<{
    segment_id: string;
    mode: HotelMode;
    hotel_id: string;
    brand_id: string;
  }>;
}

/** 后端提交回执（对应 hotel_skill_schema.py 的 HotelMultiSegmentSubmitResult） */
export interface HotelMultiSegmentSubmitResult {
  /** 全部段基础校验通过为 true（后端不做任何日期/时间校验） */
  ok: boolean;
  /** OK / NO_PAYLOAD / EMPTY_SELECTION / INVALID_PAYLOAD / SEGMENT_NOT_FOUND / MODE_MISMATCH / MISSING_HOTEL_ID / MISSING_BRAND_ID / HOTEL_NOT_FOUND / BRAND_NOT_FOUND / INCOMPLETE */
  code: string;
  /** 面向用户的中文说明 */
  msg: string;
  accepted: HotelMultiSegmentSubmitPayload['segments'];
  /** 已选定各段的展示文本（后端据此拼续跑发言） */
  texts?: string[];
}

// ==================== 后端 Skill JSON Schema ====================

/**
 * 【入参 JSON Schema】Agent 调用 hotel_multi_segment 工具时传入的参数。
 *
 * 语义：segments 是「按行程顺序排列的住宿段」，每站一段（段数不设上限，2/3/多段均可）。
 * 后端职责：拿到 segments 后**并行**调用酒店 MCP 查全部段（asyncio.gather，非串行逐段查），
 *           每段独立解析并判定 mode，查完一次性组装出参返回；前端不做任何网络查询。
 */
export const HOTEL_SKILL_INPUT_SCHEMA = {
  type: 'object',
  required: ['segments'],
  properties: {
    segments: {
      type: 'array',
      minItems: 1,
      description: '按行程顺序排列的住宿段（每站一段），长度即段数',
      items: {
        type: 'object',
        required: ['city', 'checkInDate', 'checkOutDate'],
        properties: {
          city: { type: 'string', description: '该段住宿城市，如 石家庄' },
          checkInDate: { type: 'string', description: '入住日期 YYYY-MM-DD' },
          checkOutDate: { type: 'string', description: '离店日期 YYYY-MM-DD' },
          guestCount: { type: 'integer', minimum: 1, description: '入住人数，默认 1' },
          priceLevel: {
            type: 'string',
            description: '房型档位（经济型 / 舒适型 / 星级酒店 / 民宿 / 青年旅社），默认经济型',
          },
        },
      },
    },
    max_options_per_segment: {
      type: 'integer',
      minimum: 1,
      maximum: 20,
      description: '可选，每段向 MCP 请求的门店候选上限，默认 5',
    },
  },
} as const;

/**
 * 【出参 JSON Schema】后端 Skill 返回给前端 UI Skill 的结构。
 *
 * 与前端 HotelMultiSegmentSkillData 一一对应；上层收到该结构后按 skill_name 查
 * skillRegistry 渲染 <HotelMultiSegment /> 组件。每段自带 mode，段间可混合。
 */
export const HOTEL_SKILL_OUTPUT_SCHEMA = {
  type: 'object',
  required: ['skill_name', 'segments'],
  properties: {
    skill_name: {
      type: 'string',
      const: HOTEL_MULTI_SEGMENT_SKILL,
      description: 'UI Skill 注册键，前端据此匹配渲染组件',
    },
    segments: {
      type: 'array',
      minItems: 1,
      description: '全部住宿段，顺序与入参一致（并行查询后统一组装），长度即段数',
      items: {
        type: 'object',
        required: [
          'segment_id',
          'city',
          'checkInDate',
          'checkOutDate',
          'guestCount',
          'priceLevel',
          'mode',
          'hotel_list',
          'brand_list',
        ],
        properties: {
          segment_id: { type: 'string', description: '段唯一标识，如 h1 / h2 / h3' },
          city: { type: 'string', description: '该段住宿城市' },
          checkInDate: { type: 'string', description: '入住日期 YYYY-MM-DD' },
          checkOutDate: { type: 'string', description: '离店日期 YYYY-MM-DD' },
          guestCount: { type: 'integer', description: '入住人数' },
          priceLevel: { type: 'string', description: '房型档位' },
          mode: {
            type: 'string',
            enum: ['hotel', 'brand'],
            description: 'hotel=渲染门店卡片；brand=渲染品牌卡片（实时报价不可用）',
          },
          hotel_list: {
            type: 'array',
            description: '门店候选（mode=brand 时为空数组）',
            items: {
              type: 'object',
              required: [
                'hotel_id',
                'hotelName',
                'totalPrice',
                'pricePerNight',
                'starRating',
                'address',
              ],
              properties: {
                hotel_id: { type: 'string', description: '段内唯一标识，如 h1-1' },
                hotelName: { type: 'string', description: '门店名称' },
                totalPrice: { type: 'number', description: '住宿总价（元）' },
                pricePerNight: { type: 'number', description: '每晚单价（元/晚）' },
                starRating: { type: 'number', description: '评分 / 星级' },
                address: { type: 'string', description: '门店地址' },
              },
            },
          },
          brand_list: {
            type: 'array',
            description: '品牌候选（mode=hotel 时为空数组）',
            items: {
              type: 'object',
              required: ['brand_id', 'brandName'],
              properties: {
                brand_id: { type: 'string', description: '段内唯一标识，如 b2-1' },
                brandName: { type: 'string', description: '品牌名称，如 如家 / 汉庭' },
              },
            },
          },
        },
      },
    },
  },
} as const;

/**
 * 【提交流程 Schema】
 * 前端：预览页点【提交确认】→ onSubmit 抛出 HotelMultiSegmentSubmitPayload（逐段 mode + 选中 id）。
 * 后端：submit_hotel_multi_segment_tool 只做基础合法性校验（段是否存在、mode 是否与下发一致、
 *       id 是否在该段候选内、是否每段都选了），**不做任何日期/时间校验**。
 */
export const HOTEL_SKILL_SUBMIT_SCHEMA = {
  request: {
    type: 'object',
    required: ['skill_name', 'segments'],
    properties: {
      skill_name: { type: 'string', const: HOTEL_MULTI_SEGMENT_SKILL },
      segments: {
        type: 'array',
        description: '全部住宿段的选中结果，顺序不限（后端按 segment_id 归位）',
        items: {
          type: 'object',
          required: ['segment_id', 'mode'],
          properties: {
            segment_id: { type: 'string', description: '必须存在于下发 payload 的 segments 中' },
            mode: {
              type: 'string',
              enum: ['hotel', 'brand'],
              description: '必须与该段下发 payload 的 mode 一致',
            },
            hotel_id: { type: 'string', description: "mode='hotel' 时必填，如 h1-1" },
            brand_id: { type: 'string', description: "mode='brand' 时必填，如 b2-1" },
          },
        },
      },
    },
  },
  response: {
    type: 'object',
    required: ['ok', 'code', 'msg', 'accepted'],
    properties: {
      ok: { type: 'boolean', description: '全部段基础校验通过为 true' },
      code: {
        type: 'string',
        description:
          'OK / NO_PAYLOAD / EMPTY_SELECTION / INVALID_PAYLOAD / SEGMENT_NOT_FOUND / MODE_MISMATCH / MISSING_HOTEL_ID / MISSING_BRAND_ID / HOTEL_NOT_FOUND / BRAND_NOT_FOUND / INCOMPLETE',
      },
      msg: { type: 'string', description: '面向用户的中文说明' },
      accepted: {
        type: 'array',
        description: '校验通过的选中项（原样回显）',
        items: {
          type: 'object',
          required: ['segment_id', 'mode'],
          properties: {
            segment_id: { type: 'string' },
            mode: { type: 'string', enum: ['hotel', 'brand'] },
            hotel_id: { type: 'string' },
            brand_id: { type: 'string' },
          },
        },
      },
      texts: {
        type: 'array',
        description: '已选定各段的展示文本（后端据此拼续跑发言）',
        items: { type: 'string' },
      },
    },
  },
} as const;