'use client';

/**
 * 【预览页】多段高铁行程 UI Skill 演示
 * 访问路径：/skill-preview/train-multi-segment
 *
 * 演示四件事：
 * 1. 传入后端 Skill 出参（此处为内置样例，闭环行程 石家庄→北京→上海→石家庄，共 3 段）；
 * 2. 按 skill_name 从 skillRegistry 匹配并渲染对应组件；
 * 3. 段数动态：切换「2 段 / 3 段」可看到组件子步骤数随之变化（后端返回几段就渲染几步）；
 * 4. 监听 onSubmit 回调，拿到「每段 segment_id + option_id（身份）+ trainNo」的全局提交 payload。
 *
 * 注：真实接入时本页数据来自 Agent 下发的 skill 帧（后端 query_train_multi_segment_tool
 *     并行调用 12306 工具后的返回），组件本身不做任何查询、不做任何时间冲突校验。
 */

import { useCallback, useMemo, useState } from 'react';
import { UI_SKILL_REGISTRY } from '@/components/skills/train-multi-segment/skillRegistry';
import type {
  TrainMultiSegmentSkillData,
  TrainMultiSegmentSubmitPayload,
} from '@/components/skills/train-multi-segment/types';

/**
 * 内置样例数据（3 段闭环；班次取自对话中确认过的真实车次）
 * s1 石家庄→北京：D40 / D38 / D20 / D922
 * s2 北京→上海：G531 / G547 / G1 / G5
 * s3 上海→石家庄：G1952 / G1212 / G2812
 */
export const MOCK_TRAIN_MULTI_SEGMENT: TrainMultiSegmentSkillData = {
  skill_name: 'train_multi_segment',
  segments: [
    {
      segment_id: 's1',
      from: '石家庄',
      to: '北京',
      date: '2026-09-19',
      train_list: [
        {
          trainNo: 'D40',
          option_id: 'D40-03:17-北京丰台',
          startTime: '03:17',
          endTime: '06:00',
          startStation: '石家庄北',
          endStation: '北京丰台',
          duration: '02:43',
          price: 52,
          seatType: '二等座',
          ticketStatus: '无票',
        },
        {
          trainNo: 'D38',
          option_id: 'D38-04:05-北京丰台',
          startTime: '04:05',
          endTime: '06:57',
          startStation: '石家庄北',
          endStation: '北京丰台',
          duration: '02:52',
          price: 52,
          seatType: '二等座',
          ticketStatus: '有票',
        },
        {
          trainNo: 'D20',
          option_id: 'D20-06:20-北京丰台',
          startTime: '06:20',
          endTime: '09:11',
          startStation: '石家庄北',
          endStation: '北京丰台',
          duration: '02:51',
          price: 52,
          seatType: '二等座',
          ticketStatus: '有票',
        },
        {
          trainNo: 'D922',
          option_id: 'D922-08:40-北京西',
          startTime: '08:40',
          endTime: '11:30',
          startStation: '石家庄',
          endStation: '北京西',
          duration: '02:50',
          price: 86,
          seatType: '二等座',
          ticketStatus: '候补',
        },
        {
          // [MERGE] 同车次号不同终到站的真实场景（12306 数据里确实存在）：
          // 身份靠 option_id 区分，两者可独立选中——用 trainNo 当身份会两趟同时勾选
          trainNo: 'D922',
          option_id: 'D922-08:40-北京丰台',
          startTime: '08:40',
          endTime: '11:52',
          startStation: '石家庄',
          endStation: '北京丰台',
          duration: '03:12',
          price: 76,
          seatType: '二等座',
          ticketStatus: '有票',
        },
      ],
    },
    {
      segment_id: 's2',
      from: '北京',
      to: '上海',
      date: '2026-09-21',
      train_list: [
        {
          trainNo: 'G531',
          option_id: 'G531-07:09-上海虹桥',
          startTime: '07:09',
          endTime: '12:53',
          startStation: '北京南',
          endStation: '上海虹桥',
          duration: '05:44',
          price: 553,
          seatType: '二等座',
          ticketStatus: '有票',
        },
        {
          trainNo: 'G547',
          option_id: 'G547-09:00-上海虹桥',
          startTime: '09:00',
          endTime: '13:36',
          startStation: '北京南',
          endStation: '上海虹桥',
          duration: '04:36',
          price: 553,
          seatType: '二等座',
          ticketStatus: '有票',
        },
        {
          trainNo: 'G1',
          option_id: 'G1-09:00-上海虹桥',
          startTime: '09:00',
          endTime: '13:28',
          startStation: '北京南',
          endStation: '上海虹桥',
          duration: '04:28',
          price: 553,
          seatType: '二等座',
          ticketStatus: '有票',
        },
        {
          trainNo: 'G5',
          option_id: 'G5-09:12-上海虹桥',
          startTime: '09:12',
          endTime: '13:37',
          startStation: '北京南',
          endStation: '上海虹桥',
          duration: '04:25',
          price: 553,
          seatType: '二等座',
          ticketStatus: '有票',
        },
      ],
    },
    {
      segment_id: 's3',
      from: '上海',
      to: '石家庄',
      date: '2026-09-23',
      train_list: [
        {
          trainNo: 'G1212',
          option_id: 'G1212-07:05-石家庄',
          startTime: '07:05',
          endTime: '13:35',
          startStation: '上海虹桥',
          endStation: '石家庄',
          duration: '06:30',
          price: 613,
          seatType: '二等座',
          ticketStatus: '有票',
        },
        {
          trainNo: 'G1952',
          option_id: 'G1952-15:30-石家庄',
          startTime: '15:30',
          endTime: '22:00',
          startStation: '上海虹桥',
          endStation: '石家庄',
          duration: '06:30',
          price: 613,
          seatType: '二等座',
          ticketStatus: '有票',
        },
        {
          trainNo: 'G2812',
          option_id: 'G2812-12:40-石家庄',
          startTime: '12:40',
          endTime: '19:05',
          startStation: '上海虹桥',
          endStation: '石家庄',
          duration: '06:25',
          price: 613,
          seatType: '二等座',
          ticketStatus: '候补',
        },
      ],
    },
  ],
};

export default function TrainMultiSegmentPreviewPage() {
  /** 演示段数动态：后端给几段就渲染几段（此处用截断模拟不同段数的 payload） */
  const [legCount, setLegCount] = useState(3);
  /** 全局提交结果（仅预览页点一次【提交确认】后产生） */
  const [submitted, setSubmitted] = useState<TrainMultiSegmentSubmitPayload | null>(
    null,
  );

  /** 模拟 Agent 下发的 payload（segments 长度 = legCount） */
  const skillData = useMemo<TrainMultiSegmentSkillData>(
    () => ({
      ...MOCK_TRAIN_MULTI_SEGMENT,
      segments: MOCK_TRAIN_MULTI_SEGMENT.segments.slice(0, legCount),
    }),
    [legCount],
  );

  /** 按 skill_name 从注册表匹配组件（真实接入时 skill_name 来自 Agent 帧） */
  const SkillComponent = UI_SKILL_REGISTRY[skillData.skill_name];

  /** 接收 Skill 抛出的选中行程数据：此处仅回显；真实接入时 POST 给 Agent 提交工具 */
  const handleSubmit = useCallback((payload: TrainMultiSegmentSubmitPayload) => {
    setSubmitted(payload);
    // 真实接入：
    //   await fetch('/api/v1/skills/train_multi_segment/submit', {
    //     method: 'POST', body: JSON.stringify(payload),
    //   });  // 后端 submit_train_multi_segment_tool 只做基础合法性校验（不校验时间）
  }, []);

  return (
    <div className="h-screen overflow-y-auto bg-sand p-6">
      <div className="mx-auto flex max-w-[760px] flex-col gap-5">
        <header className="text-center">
          <h1 className="font-serif text-2xl text-text-serif">
            UI Skill 预览 · train_multi_segment
          </h1>
          <p className="mt-1 text-xs text-text-light">
            样例数据：石家庄 → 北京 → 上海 → 石家庄（段数由后端 payload 决定，前端不写死）
          </p>
          {/* 段数切换：仅用于演示「动态子步骤」，不改变组件任何交互规则 */}
          <div className="mt-3 flex items-center justify-center gap-2">
            {[2, 3].map((count) => (
              <button
                key={count}
                type="button"
                onClick={() => {
                  setLegCount(count);
                  setSubmitted(null);
                }}
                className={`rounded-full border px-4 py-1 text-xs transition-colors duration-200 ${
                  legCount === count
                    ? 'border-primary bg-primary text-white'
                    : 'border-black/15 bg-white/70 text-text-main hover:border-primary/50'
                }`}
              >
                模拟下发 {count} 段
              </button>
            ))}
          </div>
        </header>

        {/* key 绑定段数：切换段数即重挂载，避免旧步骤下标越界 */}
        <SkillComponent
          key={skillData.segments.length}
          data={skillData}
          onSubmit={handleSubmit}
        />

        {/* 提交结果回显：验证 onSubmit 抛出的 payload 结构 */}
        {submitted && (
          <section className="rounded-lg border border-primary/20 bg-white/70 p-4">
            <h2 className="mb-2 text-sm font-semibold text-text-serif">
              onSubmit 收到的提交数据（交后端 submit 工具，仅基础校验、不校验时间）
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