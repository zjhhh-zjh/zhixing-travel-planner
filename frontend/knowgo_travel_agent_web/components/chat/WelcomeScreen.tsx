'use client';

import { memo } from 'react';
import TripForm from './TripForm';

/**
 * [MERGE] 空白会话欢迎页：内嵌旅行需求表单
 * 结构化收集出发地/目的地/日期/人数/预算/交通/偏好/酒店，提交后走既有 SSE 链路
 */
function WelcomeScreen() {
  return (
    <div className="m-auto flex w-full justify-center py-2">
      <TripForm />
    </div>
  );
}

export default memo(WelcomeScreen);
