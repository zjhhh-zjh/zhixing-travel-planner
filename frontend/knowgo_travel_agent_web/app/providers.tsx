'use client';

import type { ReactNode } from 'react';
import { Provider } from 'react-redux';
import { store } from '@/store';

/**
 * 全局状态 Provider：
 * - Redux Provider 注入 store
 * - 使用自定义持久化层（store 创建时同步 rehydrate），无需 PersistGate
 */
export default function Providers({ children }: { children: ReactNode }) {
  return <Provider store={store}>{children}</Provider>;
}
