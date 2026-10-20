'use client';

import { memo, useCallback, useState, type FormEvent } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import type { AppDispatch, RootState } from '@/store';
import { loginThunk, registerThunk } from '@/store/slices/authSlice';
import { showToast } from '@/store/slices/uiSlice';
import { getApiBase, setApiBase } from '@/lib/api';

type AuthTab = 'login' | 'register';

/** 表单字段（原版 input placeholder 文案） */
const PLACEHOLDERS = {
  username: '通行证 / 用户名',
  email: '电子邮箱',
  password: '密钥 / 密码',
};

/**
 * 登录 / 注册弹窗（护照风格 + 邮戳 VISA 装饰）
 * - Tab 切换：注册显示邮箱输入框，登录隐藏
 * - API 地址配置折叠面板，默认 http://localhost:8000
 * - 表单提交加载状态（旋转图标 + 按钮禁用），请求成功/失败 Toast 提示
 */
function AuthOverlay() {
  const dispatch = useDispatch<AppDispatch>();
  // 已登录（有 token 且有用户信息）则隐藏弹窗
  const visible = useSelector((state: RootState) => !(state.auth.token && state.auth.user));
  const submitting = useSelector((state: RootState) => state.auth.submitting);

  const [tab, setTab] = useState<AuthTab>('login');
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [apiBase, setApiBaseInput] = useState<string>(() => getApiBase());

  /** 切换登录/注册 Tab */
  const handleSwitchTab = useCallback((next: AuthTab) => {
    setTab(next);
  }, []);

  /** 表单提交：登录或注册 */
  const handleSubmit = useCallback(
    async (e: FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      if (!username.trim() || !password) return;

      // 折叠面板输入值作为本次请求的基础地址
      setApiBase(apiBase.trim() || getApiBase());

      try {
        if (tab === 'register') {
          await dispatch(registerThunk({ username: username.trim(), email, password })).unwrap();
          dispatch(showToast({ message: '欢迎加入知行！' }));
        } else {
          await dispatch(loginThunk({ username: username.trim(), password })).unwrap();
          dispatch(showToast({ message: '欢迎回来！' }));
        }
        // 成功后重置表单
        setPassword('');
      } catch (err) {
        // RTK 的 unwrap() 抛出的是 miniSerializeError 生成的普通对象（不是 Error 实例），
        // 用 instanceof 判类型会全部落空、真实文案被吞成兜底文案，故直接按字段取；
        // msg 是后端统一错误体 {code,msg,data} 的字段，缺它就会重新掉回"操作失败"
        const message =
          (err as { detail?: string })?.detail ||
          (err as { msg?: string })?.msg ||
          (err as { message?: string })?.message ||
          '操作失败';
        dispatch(showToast({ message, type: 'error' }));
      }
    },
    [apiBase, dispatch, email, password, tab, username],
  );

  return (
    <div
      className={`fixed inset-0 z-[100] flex items-center justify-center bg-[rgba(26,77,84,0.3)] backdrop-blur-overlay transition-opacity duration-300 ${
        visible ? '' : 'hidden'
      }`}
    >
      <div className="glass-card relative w-[400px] overflow-hidden rounded-lg bg-white/95 p-10">
        {/* 邮戳 VISA 装饰 */}
        <div className="pointer-events-none absolute -right-5 -top-5 flex h-[100px] w-[100px] rotate-20 items-center justify-center rounded-full border-2 border-dashed border-accent font-display text-accent opacity-20">
          <span className="font-bold">VISA</span>
        </div>

        {/* 头部：品牌 logo + 英文副标题 */}
        <div className="mb-[30px] text-center">
          <div className="mb-2 font-serif text-4xl font-bold tracking-[4px] text-primary">
            <i className="fa-solid fa-compass mr-2 text-2xl text-accent" />
            知行
          </div>
          <p className="font-display italic text-text-light">
            Read Thousands of Books, Travel Thousands of Miles
          </p>
        </div>

        {/* 登录 / 注册 Tab */}
        <div className="mb-6 flex border-b border-[#ddd]">
          {(['login', 'register'] as const).map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => handleSwitchTab(key)}
              className={`relative flex-1 cursor-pointer border-none bg-transparent py-3 text-[15px] ${
                tab === key
                  ? 'font-semibold text-primary after:absolute after:-bottom-px after:left-0 after:h-[2px] after:w-full after:bg-primary after:content-[""]'
                  : 'text-text-light'
              }`}
            >
              {key === 'login' ? '登 录' : '注 册'}
            </button>
          ))}
        </div>

        {/* 表单 */}
        <form onSubmit={handleSubmit}>
          <div className="mb-4">
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder={PLACEHOLDERS.username}
              required
              className="w-full rounded-[4px] border border-[#ddd] bg-white p-3 transition-all focus:border-primary focus:shadow-[0_0_0_2px_rgba(26,77,84,0.1)] focus:outline-none"
            />
          </div>

          {/* 注册专属邮箱输入框 */}
          {tab === 'register' && (
            <div className="mb-4">
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder={PLACEHOLDERS.email}
                required={tab === 'register'}
                className="w-full rounded-[4px] border border-[#ddd] bg-white p-3 transition-all focus:border-primary focus:shadow-[0_0_0_2px_rgba(26,77,84,0.1)] focus:outline-none"
              />
            </div>
          )}

          <div className="mb-4">
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={PLACEHOLDERS.password}
              required
              className="w-full rounded-[4px] border border-[#ddd] bg-white p-3 transition-all focus:border-primary focus:shadow-[0_0_0_2px_rgba(26,77,84,0.1)] focus:outline-none"
            />
          </div>

          <button
            type="submit"
            disabled={submitting}
            className="w-full cursor-pointer rounded-[4px] bg-primary p-3.5 text-[15px] text-white transition-colors duration-300 hover:bg-primary-light disabled:cursor-not-allowed disabled:bg-[#ccc]"
          >
            {submitting ? (
              <>
                <i className="fa-solid fa-spinner fa-spin" /> 验证中...
              </>
            ) : tab === 'register' ? (
              '注册通行证'
            ) : (
              '开启旅程'
            )}
          </button>
        </form>

        {/* API 地址配置折叠面板 */}
        <details className="mt-5 text-center text-xs">
          <summary className="cursor-pointer text-text-light">连接设置</summary>
          <input
            type="text"
            value={apiBase}
            onChange={(e) => setApiBaseInput(e.target.value)}
            onBlur={() => setApiBase(apiBase.trim() || getApiBase())}
            className="mt-1 w-4/5 rounded border border-[#ddd] p-1 text-center focus:border-primary focus:outline-none"
          />
        </details>
      </div>
    </div>
  );
}

export default memo(AuthOverlay);
