'use client';

/**
 * 【预览页】多段住宿选择 UI Skill 演示
 * 访问路径：/skill-preview/hotel-multi-segment
 *
 * 演示四件事（与 /skill-preview/train-multi-segment 完全同构）：
 * 1. 传入后端 Skill 出参（此处为内置样例），按 skill_name 从 skillRegistry 匹配组件渲染；
 * 2. **段数动态**：后端 payload.segments 有几段，组件就生成几个子步骤（N 步选段 + 1 步预览）；
 * 3. **两种渲染模式**：同一份 payload 内可混合 —— mode='hotel' 渲染门店卡、
 *    mode='brand' 渲染品牌卡并带「实时酒店报价暂不可用，仅可选择品牌」提示；
 * 4. 监听 onSubmit 回调，拿到「每段 segment_id + mode + 选中 id」的**一次性全局提交** payload。
 *
 * 注：真实接入时本页数据来自 Agent 下发的 skill 帧（后端 query_hotel_multi_segment_tool
 *     并行调用酒店 MCP 后的返回）；组件本身不做任何查询、不做任何日期/时间校验。
 */

import { useCallback, useState } from 'react';
import { UI_SKILL_REGISTRY } from '@/components/skills/train-multi-segment/skillRegistry';
import type {
  HotelMultiSegmentSkillData,
  HotelMultiSegmentSubmitPayload,
} from '@/components/skills/train-multi-segment/types';

/**
 * 样例 A（2 段，门店 + 品牌）—— 门店数据取自 aigohotel MCP 真实返回
 * h1 石家庄：mode=hotel（有真实门店与报价）
 * h2 上海：mode=brand（实时报价不可用，只给常见连锁品牌）
 */
export const MOCK_HOTEL_MULTI_SEGMENT_A: HotelMultiSegmentSkillData = {
  skill_name: 'hotel_multi_segment',
  segments: [
    {
      segment_id: 'h1',
      city: '石家庄',
      checkInDate: '2026-09-20',
      checkOutDate: '2026-09-22',
      guestCount: 1,
      priceLevel: '经济型',
      mode: 'hotel',
      hotel_list: [
        {
          hotel_id: 'h1-1',
          hotelName: '尚客优酒店（石家庄长安区谈固北大街店）',
          totalPrice: 302,
          pricePerNight: 151,
          starRating: 2.0,
          address: '丰收路与金永街交叉口西60米, 长安区, 石家庄',
        },
        {
          hotel_id: 'h1-2',
          hotelName: '汉庭酒店（石家庄北国商城店）',
          totalPrice: 378,
          pricePerNight: 189,
          starRating: 3.0,
          address: '中山东路 289 号, 长安区, 石家庄',
        },
        {
          hotel_id: 'h1-3',
          hotelName: '如家快捷酒店（石家庄火车站店）',
          totalPrice: 336,
          pricePerNight: 168,
          starRating: 2.5,
          address: '站前街 12 号, 桥西区, 石家庄',
        },
      ],
      brand_list: [],
    },
    {
      segment_id: 'h2',
      city: '上海',
      checkInDate: '2026-09-22',
      checkOutDate: '2026-09-24',
      guestCount: 1,
      priceLevel: '经济型',
      mode: 'brand',
      hotel_list: [],
      brand_list: [
        { brand_id: 'h2-1', brandName: '如家快捷酒店' },
        { brand_id: 'h2-2', brandName: '汉庭酒店' },
        { brand_id: 'h2-3', brandName: '7 天连锁酒店' },
        { brand_id: 'h2-4', brandName: '锦江之星' },
        { brand_id: 'h2-5', brandName: '格林豪泰' },
      ],
    },
  ],
};

/**
 * 样例 B（3 段，门店 + 品牌 + 门店）—— 用于验证「动态步骤」
 * 段数由 payload 决定：本样例自动生成 3 个选段步骤 + 1 个预览步骤（共 4 步）。
 */
export const MOCK_HOTEL_MULTI_SEGMENT_B: HotelMultiSegmentSkillData = {
  skill_name: 'hotel_multi_segment',
  segments: [
    {
      segment_id: 'h1',
      city: '北京',
      checkInDate: '2026-09-20',
      checkOutDate: '2026-09-22',
      guestCount: 2,
      priceLevel: '舒适型',
      mode: 'hotel',
      hotel_list: [
        {
          hotel_id: 'h1-1',
          hotelName: '全季酒店（北京国贸店）',
          totalPrice: 916,
          pricePerNight: 458,
          starRating: 4.5,
          address: '建国路 87 号, 朝阳区, 北京',
        },
        {
          hotel_id: 'h1-2',
          hotelName: '桔子水晶酒店（北京三里屯店）',
          totalPrice: 1056,
          pricePerNight: 528,
          starRating: 4.7,
          address: '工体北路 8 号, 朝阳区, 北京',
        },
      ],
      brand_list: [],
    },
    {
      segment_id: 'h2',
      city: '成都',
      checkInDate: '2026-09-22',
      checkOutDate: '2026-09-24',
      guestCount: 2,
      priceLevel: '舒适型',
      mode: 'brand',
      hotel_list: [],
      brand_list: [
        { brand_id: 'h2-1', brandName: '全季酒店' },
        { brand_id: 'h2-2', brandName: '亚朵酒店' },
        { brand_id: 'h2-3', brandName: '维也纳酒店' },
      ],
    },
    {
      segment_id: 'h3',
      city: '西安',
      checkInDate: '2026-09-24',
      checkOutDate: '2026-09-26',
      guestCount: 2,
      priceLevel: '经济型',
      mode: 'hotel',
      hotel_list: [
        {
          hotel_id: 'h3-1',
          hotelName: '汉庭酒店（西安钟楼店）',
          totalPrice: 396,
          pricePerNight: 198,
          starRating: 3.0,
          address: '北大街 108 号, 碑林区, 西安',
        },
        {
          hotel_id: 'h3-2',
          hotelName: '如家精选酒店（西安大雁塔店）',
          totalPrice: 436,
          pricePerNight: 218,
          starRating: 3.2,
          address: '雁塔路 27 号, 雁塔区, 西安',
        },
      ],
      brand_list: [],
    },
  ],
};

/** 两套样例（切换即重挂载组件，验证动态步骤与两种渲染模式） */
const MOCKS: Array<{ label: string; hint: string; data: HotelMultiSegmentSkillData }> = [
  { label: '样例 A · 2 段', hint: '石家庄门店 → 上海品牌', data: MOCK_HOTEL_MULTI_SEGMENT_A },
  { label: '样例 B · 3 段', hint: '北京门店 → 成都品牌 → 西安门店', data: MOCK_HOTEL_MULTI_SEGMENT_B },
];

export default function HotelMultiSegmentPreviewPage() {
  /** 当前选中的样例下标（切换即换 payload，段数与模式随之变化） */
  const [mockIndex, setMockIndex] = useState(0);
  /** 全局提交结果（仅预览页点一次【提交确认】后产生） */
  const [submitted, setSubmitted] = useState<HotelMultiSegmentSubmitPayload | null>(null);

  const skillData = MOCKS[mockIndex].data;

  /** 按 skill_name 从注册表匹配组件（真实接入时 skill_name 来自 Agent 帧） */
  const SkillComponent = UI_SKILL_REGISTRY[skillData.skill_name];

  /** 接收 Skill 抛出的选中住宿数据：此处仅回显；真实接入时 POST 给 Agent 提交工具 */
  const handleSubmit = useCallback((payload: HotelMultiSegmentSubmitPayload) => {
    setSubmitted(payload);
    // 真实接入（与高铁一致）：
    //   await fetch('/api/v1/skills/hotel_multi_segment/submit', {
    //     method: 'POST', body: JSON.stringify(payload),
    //   });  // 后端 submit_hotel_multi_segment_tool 只做基础合法性校验（不校验日期）
  }, []);

  return (
    <div className="h-screen overflow-y-auto bg-sand p-6">
      <div className="mx-auto flex max-w-[760px] flex-col gap-5">
        <header className="text-center">
          <h1 className="font-serif text-2xl text-text-serif">
            UI Skill 预览 · hotel_multi_segment
          </h1>
          <p className="mt-1 text-xs text-text-light">
            段数由后端 payload 决定（前端不写死）：N 段自动生成 N 步选择 + 1 步预览；
            同一 payload 内可混合门店 / 品牌两种模式
          </p>
          {/* 样例切换：仅换 payload，不改变组件任何交互规则 */}
          <div className="mt-3 flex items-center justify-center gap-2">
            {MOCKS.map((mock, index) => (
              <button
                key={mock.label}
                type="button"
                onClick={() => {
                  setMockIndex(index);
                  setSubmitted(null);
                }}
                className={`rounded-full border px-4 py-1 text-xs transition-colors duration-200 ${
                  mockIndex === index
                    ? 'border-primary bg-primary text-white'
                    : 'border-black/15 bg-white/70 text-text-main hover:border-primary/50'
                }`}
              >
                {mock.label}
                <span className="ml-1 opacity-70">{mock.hint}</span>
              </button>
            ))}
          </div>
        </header>

        {/* key 绑定样例与段数：切换即重挂载，避免旧步骤下标越界 / 旧选择残留 */}
        <SkillComponent
          key={`${mockIndex}-${skillData.segments.length}`}
          data={skillData}
          onSubmit={handleSubmit}
        />

        {/* 提交结果回显：验证 onSubmit 抛出的 payload 结构（与后端提交工具入参严格一致） */}
        {submitted && (
          <section className="rounded-lg border border-primary/20 bg-white/70 p-4">
            <h2 className="mb-2 text-sm font-semibold text-text-serif">
              onSubmit 收到的提交数据（交后端 submit 工具，仅基础校验、不校验日期）
            </h2>
            <pre className="thin-scroll overflow-x-auto rounded-sm bg-sand p-3 text-xs text-text-main">
              {JSON.stringify(submitted, null, 2)}
            </pre>
          </section>
        )}
      </div>
    </div>
  );
}