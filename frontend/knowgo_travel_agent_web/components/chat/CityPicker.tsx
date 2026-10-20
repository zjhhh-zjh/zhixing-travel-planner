'use client';

/**
 * [MERGE] 可搜索城市选择器：出发地单选 / 目的地多选
 * - 数据源 GET /api/v1/cities（全国地级市，Redis 缓存）
 * - 城市清单模块级缓存，首次点击后本地模糊过滤（名称/省份）
 * - 点击外部关闭弹层；单选选中即收起，多选可连续勾选
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { fetchCitiesApi, type CityOption } from '@/lib/api';

/** 模块级缓存：城市清单只拉一次，跨组件复用 */
let citiesCache: CityOption[] | null = null;
let citiesLoading: Promise<CityOption[]> | null = null;

function loadCities(): Promise<CityOption[]> {
  if (citiesCache) return Promise.resolve(citiesCache);
  if (!citiesLoading) {
    citiesLoading = fetchCitiesApi()
      .then((list) => {
        citiesCache = list;
        return list;
      })
      .catch((e) => {
        citiesLoading = null; // 失败允许重试
        throw e;
      });
  }
  return citiesLoading;
}

interface CityPickerProps {
  /** 已选城市（单选最多 1 个，多选可多个） */
  value: string[];
  onChange: (v: string[]) => void;
  /** 单选/多选切换 */
  multiple?: boolean;
  placeholder?: string;
  /** 触发区样式（复用 TripForm 输入框视觉） */
  className?: string;
}

export default function CityPicker({
  value,
  onChange,
  multiple = false,
  placeholder = '选择城市',
  className = '',
}: CityPickerProps) {
  const [open, setOpen] = useState(false);
  const [kw, setKw] = useState('');
  const [options, setOptions] = useState<CityOption[]>([]);
  const wrapRef = useRef<HTMLDivElement>(null);

  // 拉取城市数据（组件挂载即预取，点击弹层时通常已就绪）
  useEffect(() => {
    let alive = true;
    loadCities()
      .then((list) => {
        if (alive) setOptions(list);
      })
      .catch(() => {
        /* 拉取失败：弹层显示空，可重试打开 */
      });
    return () => {
      alive = false;
    };
  }, []);

  // 点击弹层外部关闭
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  // 本地模糊过滤：城市名或省份子串命中
  const filtered = useMemo(() => {
    const q = kw.trim();
    if (!q) return options;
    return options.filter((c) => c.name.includes(q) || c.province.includes(q));
  }, [options, kw]);

  const toggleCity = (name: string) => {
    if (multiple) {
      onChange(value.includes(name) ? value.filter((v) => v !== name) : [...value, name]);
    } else {
      onChange([name]);
      setOpen(false);
    }
  };

  const removeCity = (name: string) => {
    onChange(value.filter((v) => v !== name));
  };

  return (
    <div ref={wrapRef} className="relative">
      {/* 触发区：多选展示已选 chips，单选展示选中值 */}
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={`${className} flex min-h-[38px] flex-wrap items-center gap-1.5 text-left`}
      >
        {value.length === 0 && (
          <span className="text-text-light">{placeholder}</span>
        )}
        {value.map((v) =>
          multiple ? (
            <span
              key={v}
              className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-xs text-primary"
            >
              {v}
              <i
                className="fa-solid fa-xmark cursor-pointer hover:text-red-500"
                onClick={(e) => {
                  e.stopPropagation();
                  removeCity(v);
                }}
              />
            </span>
          ) : (
            <span key={v} className="text-sm text-text-serif">
              {v}
            </span>
          ),
        )}
        <i className="fa-solid fa-angle-down ml-auto text-xs text-text-light" />
      </button>

      {/* 弹层：搜索框 + 城市列表 */}
      {open && (
        <div className="absolute z-20 mt-1 w-full rounded-xl border border-black/10 bg-white shadow-soft">
          <div className="flex items-center gap-2 border-b border-black/5 px-3 py-2">
            <i className="fa-solid fa-magnifying-glass text-xs text-text-light" />
            <input
              autoFocus
              value={kw}
              onChange={(e) => setKw(e.target.value)}
              placeholder="搜索城市或省份"
              className="w-full bg-transparent text-sm text-text-serif outline-none"
            />
            {kw && (
              <i
                className="fa-solid fa-circle-xmark cursor-pointer text-xs text-text-light"
                onClick={() => setKw('')}
              />
            )}
          </div>
          <div className="max-h-48 overflow-y-auto py-1">
            {filtered.length === 0 && (
              <p className="px-3 py-2 text-xs text-text-light">未找到匹配城市</p>
            )}
            {filtered.map((c) => {
              const checked = value.includes(c.name);
              return (
                <button
                  key={c.name}
                  type="button"
                  onClick={() => toggleCity(c.name)}
                  className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm transition hover:bg-primary/5 ${
                    checked ? 'text-primary' : 'text-text-serif'
                  }`}
                >
                  <span>{c.name}</span>
                  <span className="ml-auto text-xs text-text-light">{c.province}</span>
                  {checked && <i className="fa-solid fa-check text-xs" />}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
