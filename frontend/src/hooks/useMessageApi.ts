// 统一消息 API：页面级 message 实例 + 统一错误提示（errMsg 解析后端 detail）
import { useCallback } from 'react'
import { message } from 'antd'
import { errMsg } from '../api/client'

export function useMessageApi() {
  const [msgApi, ctx] = message.useMessage()

  /** 统一错误提示：优先取后端返回的 detail/msg，取不到时用 fallback */
  const toastError = useCallback((e: unknown, fallback = '操作失败') => {
    msgApi.error(errMsg(e) || fallback)
  }, [msgApi])

  const toastSuccess = useCallback((text = '已保存') => msgApi.success(text), [msgApi])

  return { msgApi, ctx, toastError, toastSuccess }
}
