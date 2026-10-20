'use client';

import { memo, useState } from 'react';
import { useDispatch } from 'react-redux';
import type { AppDispatch } from '@/store';
import { sendMessageThunk } from '@/store/slices/chatSlice';
import CityPicker from './CityPicker'; // [MERGE] 全国城市选择器（出发地单选/目的地多选）

/** [MERGE] 旅行需求表单：新建会话时结构化收集需求，提交后走既有 SSE 链路 */

/** 交通方式多选项（多选 = 后端并行查询后对比） */
const TRANSPORT_OPTIONS = [
  { value: '航班', icon: 'fa-plane' },
  { value: '高铁', icon: 'fa-train' },
  { value: '自驾', icon: 'fa-car' },
] as const;

/** 旅行偏好多选项 */
const PREFERENCE_OPTIONS = [
  { value: '美食探店', icon: 'fa-utensils' },
  { value: '文化探索', icon: 'fa-landmark' },
  { value: '历史古迹', icon: 'fa-gopuram' },
  { value: '休闲度假', icon: 'fa-umbrella-beach' },
  { value: '户外冒险', icon: 'fa-mountain-sun' },
  { value: '城市漫步', icon: 'fa-city' },
] as const;

/** 预算档位单选（不再提供「不限」：必须选择具体金额档位） */
const BUDGET_OPTIONS = ['3000 以内', '3000-8000', '8000-15000', '15000 以上'] as const;

/** 酒店档位单选 */
const HOTEL_OPTIONS = ['不限', '经济型', '舒适型', '高端豪华', '民宿特色'] as const;

/** [MERGE] 餐饮类型多选项（对齐后端 food_planning 三类，表单直传免对话确认） */
const FOOD_OPTIONS = [
  { value: '特色美食', icon: 'fa-plate-wheat' },
  { value: '连锁快餐', icon: 'fa-burger' },
  { value: '本地小吃', icon: 'fa-bowl-food' },
] as const;

/** 表单状态类型 */
interface TripFormState {
  origin: string;
  destination: string[]; // [MERGE] 目的地改为多选（全国城市选择器）
  date: string;
  days: string;
  adults: string;
  children: string;
  budget: string;
  transports: string[];
  preferences: string[];
  hotel: string;
  foods: string[]; // [MERGE] 餐饮偏好多选
}

/** 输入框通用样式（与全站 primary 色系一致） */
const inputCls =
  'w-full rounded-lg border border-black/10 bg-white/80 px-3 py-2 text-sm text-text-serif outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/20';

/** 多选 chip 通用切换 */
function toggle(list: string[], v: string): string[] {
  return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
}

/** 今日日期（本地时区 YYYY-MM-DD）：作为出发日期下限，禁用所有已过期日期 */
function todayStr(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function TripForm() {
  const dispatch = useDispatch<AppDispatch>();
  // [TEST] 联调默认值：出发地/目的地/预算/偏好预填，测试时只需补日期即可提交
  // （目的地 2 城 → 天数下限 = 2×2 = 4 天，故 days 默认 4，否则会被必填校验拦下）
  const [form, setForm] = useState<TripFormState>({
    origin: '北京',
    destination: ['上海', '天津'], // [MERGE] 多选：留空由 AI 推荐目的地
    date: '',
    days: '4',
    adults: '2',
    children: '0',
    budget: '3000-8000',
    transports: ['航班', '高铁'],
    preferences: ['美食探店', '文化探索'],
    hotel: '不限',
    foods: ['特色美食', '连锁快餐', '本地小吃'], // [MERGE] 餐饮偏好默认全选
  });
  const [error, setError] = useState('');
  // [MERGE] AI 全程自动确认：勾选后交通/酒店/餐饮由 AI 直接选定，免逐项等待
  // [TEST] 联调默认关闭：走卡片流程（高铁/酒店卡片下发前端选择），需自动确认时手动勾选
  const [autoConfirm, setAutoConfirm] = useState(false);

  /** 必填校验 + 序列化为结构化需求文本（字段措辞对齐后端 record_requirement_tool：天数/每人预算/成人儿童），走既有 sendMessageThunk */
  const handleSubmit = () => {
    // [MERGE] 目的地改为可选：未填时引导后端按预算/交通/偏好推荐旅游目的地
    if (!form.origin.trim() || !form.date) {
      setError('请填写出发地与日期');
      return;
    }
    // 过期日期硬校验：原生 min 可被手输/脚本绕过，提交前拦截早于今天的日期
    if (form.date < todayStr()) {
      setError('出发日期不能早于今天，请重新选择');
      return;
    }
    setError('');
    const adults = Math.max(1, parseInt(form.adults) || 1);
    const children = Math.max(0, parseInt(form.children) || 0);
    const days = Math.max(1, parseInt(form.days) || 1);
    // [MERGE] 多目的地天数校验（与后端报告层硬拦同源）：下限 = 目的地数 × 2
    const neededDays = form.destination.length * 2;
    if (form.destination.length > 1 && days < neededDays) {
      setError(
        `您选择了 ${form.destination.length} 个目的地，至少需要 ${neededDays} 天（目的地数 × 2），请调整天数或减少目的地`,
      );
      return;
    }
    // [MERGE] 目的地多选：顿号拼接（与后端 _split_cities 分隔符对齐）
    const destText = form.destination.join('、');
    const head = destText
      ? `我想去${destText}旅行，请帮我规划行程。`
      : '我还不知道去哪里旅行，请根据我的预算、交通方式和旅行偏好，推荐适合旅游的目的地（每个候选地请附上当地旅游特色介绍，如代表景点、景观亮点与玩法），再帮我规划行程。';
    const lines = [
      head,
      `- 出发地：${form.origin.trim()}`,
      `- 出发日期：${form.date}`,
      `- 旅行天数：${days} 天`,
      `- 出行人数：成人 ${adults} 人${children > 0 ? ` + 儿童 ${children} 人（12 岁以下）` : ''}`,
    ];
    lines.push(`- 预算：每人 ${form.budget} 元`);
    if (form.transports.length)
      lines.push(`- 交通方式：${form.transports.join('、')}（请并行查询这些方式的班次与价格并对比）`);
    if (form.preferences.length) lines.push(`- 旅行偏好：${form.preferences.join('、')}`);
    if (form.hotel !== '不限') lines.push(`- 酒店偏好：${form.hotel}`);
    // [MERGE] 餐饮偏好直传：后端 food_planning 快速通道识别后免反问直接推进
    if (form.foods.length) lines.push(`- 餐饮偏好：${form.foods.join('、')}`);
    // [MERGE] AI 全程自动确认：交通/酒店/餐饮由 AI 按最优规则直接选定并推进
    if (autoConfirm) lines.push('- 确认方式：全程交给 AI 自动确认（请直接按最优选择推进，无需逐步询问我）');
    dispatch(sendMessageThunk(lines.join('\n')));
  };

  return (
    <form
      className="w-full max-w-[560px] rounded-[20px] border border-dashed border-primary bg-white/70 p-6 text-left shadow-soft"
      onSubmit={(e) => {
        e.preventDefault();
        handleSubmit();
      }}
    >
      {/* 标题区 */}
      <div className="mb-4 text-center">
        <div className="mb-1 text-[34px] text-primary">
          <i className="fa-solid fa-paper-plane" />
        </div>
        <h3 className="font-serif text-[22px] text-text-serif">开启你的旅程</h3>
        <p className="mt-1 text-xs text-text-light">填写旅行需求，交通与酒店将为你并行查询</p>
      </div>

      {/* 基础信息：两列栅格 */}
      <div className="grid grid-cols-2 gap-3">
        <label className="block">
          <span className="mb-1 block text-xs font-semibold text-text-serif">
            出发地 <i className="fa-solid fa-star-of-life text-[8px] text-red-400" />
          </span>
          {/* [MERGE] 出发地：全国城市单选（可搜索模糊匹配） */}
          <CityPicker
            multiple={false}
            placeholder="点击选择城市"
            className={inputCls}
            value={form.origin ? [form.origin] : []}
            onChange={(v) => setForm({ ...form, origin: v[0] ?? '' })}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-semibold text-text-serif">
            目的地
            <span className="ml-1 font-normal text-text-light">（可多选，留空由 AI 推荐）</span>
          </span>
          {/* [MERGE] 目的地：全国城市多选（可搜索模糊匹配，多目的地由后端分段规划） */}
          <CityPicker
            multiple
            placeholder="点击选择城市，可多选"
            className={inputCls}
            value={form.destination}
            onChange={(v) => setForm({ ...form, destination: v })}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-semibold text-text-serif">
            出发日期 <i className="fa-solid fa-star-of-life text-[8px] text-red-400" />
          </span>
          <input
            type="date"
            min={todayStr()}
            className={inputCls}
            value={form.date}
            onChange={(e) => setForm({ ...form, date: e.target.value })}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-semibold text-text-serif">
            旅行天数 <i className="fa-solid fa-star-of-life text-[8px] text-red-400" />
          </span>
          <input
            type="number"
            min={1}
            max={30}
            className={inputCls}
            value={form.days}
            onChange={(e) => setForm({ ...form, days: e.target.value })}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-semibold text-text-serif">
            成人 <i className="fa-solid fa-star-of-life text-[8px] text-red-400" />
          </span>
          <input
            type="number"
            min={1}
            max={20}
            className={inputCls}
            value={form.adults}
            onChange={(e) => setForm({ ...form, adults: e.target.value })}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-semibold text-text-serif">
            儿童
            <span className="ml-1 font-normal text-text-light">（12 岁以下）</span>
          </span>
          <input
            type="number"
            min={0}
            max={10}
            className={inputCls}
            value={form.children}
            onChange={(e) => setForm({ ...form, children: e.target.value })}
          />
        </label>
      </div>

      {/* 每人预算单选 chips（对齐后端 budget_min/budget_max 元/人 语义） */}
      <div className="mt-3">
        <span className="mb-1.5 block text-xs font-semibold text-text-serif">
          每人预算
          <span className="ml-1 font-normal text-text-light">（元/人）</span>
        </span>
        <div className="flex flex-wrap gap-2">
          {BUDGET_OPTIONS.map((b) => (
            <button
              key={b}
              type="button"
              onClick={() => setForm({ ...form, budget: b })}
              className={`rounded-full border px-3 py-1 text-xs transition ${
                form.budget === b
                  ? 'border-primary bg-primary text-white shadow-icon'
                  : 'border-black/10 bg-white/70 text-text-light hover:border-primary/50'
              }`}
            >
              {b}
            </button>
          ))}
        </div>
      </div>

      {/* 交通方式多选 chips（多选 = 并行查询对比） */}
      <div className="mt-3">
        <span className="mb-1.5 block text-xs font-semibold text-text-serif">
          交通方式
          <span className="ml-1 font-normal text-text-light">可多选，将并行查询后对比</span>
        </span>
        <div className="flex flex-wrap gap-2">
          {TRANSPORT_OPTIONS.map((t) => (
            <button
              key={t.value}
              type="button"
              onClick={() => setForm({ ...form, transports: toggle(form.transports, t.value) })}
              className={`rounded-full border px-3 py-1 text-xs transition ${
                form.transports.includes(t.value)
                  ? 'border-primary bg-primary text-white shadow-icon'
                  : 'border-black/10 bg-white/70 text-text-light hover:border-primary/50'
              }`}
            >
              <i className={`fa-solid ${t.icon} mr-1`} /> {t.value}
            </button>
          ))}
        </div>
      </div>

      {/* 旅行偏好多选 chips */}
      <div className="mt-3">
        <span className="mb-1.5 block text-xs font-semibold text-text-serif">
          旅行偏好
          <span className="ml-1 font-normal text-text-light">可多选</span>
        </span>
        <div className="flex flex-wrap gap-2">
          {PREFERENCE_OPTIONS.map((p) => (
            <button
              key={p.value}
              type="button"
              onClick={() => setForm({ ...form, preferences: toggle(form.preferences, p.value) })}
              className={`rounded-full border px-3 py-1 text-xs transition ${
                form.preferences.includes(p.value)
                  ? 'border-accent bg-accent text-white shadow-icon'
                  : 'border-black/10 bg-white/70 text-text-light hover:border-accent/50'
              }`}
            >
              <i className={`fa-solid ${p.icon} mr-1`} /> {p.value}
            </button>
          ))}
        </div>
      </div>

      {/* 酒店档位单选 chips */}
      <div className="mt-3">
        <span className="mb-1.5 block text-xs font-semibold text-text-serif">酒店偏好</span>
        <div className="flex flex-wrap gap-2">
          {HOTEL_OPTIONS.map((h) => (
            <button
              key={h}
              type="button"
              onClick={() => setForm({ ...form, hotel: h })}
              className={`rounded-full border px-3 py-1 text-xs transition ${
                form.hotel === h
                  ? 'border-primary bg-primary text-white shadow-icon'
                  : 'border-black/10 bg-white/70 text-text-light hover:border-primary/50'
              }`}
            >
              {h}
            </button>
          ))}
        </div>
      </div>

      {/* [MERGE] 餐饮类型多选 chips（表单直传，省一轮对话确认） */}
      <div className="mt-3">
        <span className="mb-1.5 block text-xs font-semibold text-text-serif">
          餐饮偏好
          <span className="ml-1 font-normal text-text-light">可多选，选后免对话确认</span>
        </span>
        <div className="flex flex-wrap gap-2">
          {FOOD_OPTIONS.map((f) => (
            <button
              key={f.value}
              type="button"
              onClick={() => setForm({ ...form, foods: toggle(form.foods, f.value) })}
              className={`rounded-full border px-3 py-1 text-xs transition ${
                form.foods.includes(f.value)
                  ? 'border-primary bg-primary text-white shadow-icon'
                  : 'border-black/10 bg-white/70 text-text-light hover:border-primary/50'
              }`}
            >
              <i className={`fa-solid ${f.icon} mr-1`} /> {f.value}
            </button>
          ))}
        </div>
      </div>

      {/* [MERGE] AI 全程自动确认开关：勾选后交通/酒店/餐饮由 AI 直接选定推进 */}
      <label className="mt-4 flex cursor-pointer items-center gap-2 rounded-lg border border-dashed border-primary/40 bg-primary/5 px-3 py-2">
        <input
          type="checkbox"
          checked={autoConfirm}
          onChange={(e) => setAutoConfirm(e.target.checked)}
          className="h-4 w-4 accent-[var(--color-primary,#0d9488)]"
        />
        <span className="text-xs text-text-serif">
          全程交给 AI 自动确认
          <span className="ml-1 font-normal text-text-light">（AI 按最优性价比直接选定交通/酒店/餐饮，最快出行程）</span>
        </span>
      </label>

      {/* 提交区 */}
      {error && <p className="mt-3 text-xs text-red-500">{error}</p>}
      <button
        type="submit"
        className="mt-4 w-full rounded-xl bg-primary py-2.5 text-sm font-semibold text-white shadow-icon transition hover:bg-primary/90 active:scale-[0.99]"
      >
        <i className="fa-solid fa-wand-magic-sparkles mr-1.5" /> 开始规划
      </button>
    </form>
  );
}

export default memo(TripForm);
