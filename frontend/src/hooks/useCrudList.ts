// 通用列表加载 hook：统一 loading / 错误提示 / 挂载自动加载
// 用法：const { data, setData, load, msgApi, ctx } = useCrudList<T>('/xxx')
//       load(url?, config?) 可带参覆盖默认 url（如筛选查询）；失败返回 false 并自动 toast
import { useCallback, useEffect, useState } from 'react'
import type { AxiosRequestConfig } from 'axios'
import { client } from '../api/client'
import { useMessageApi } from './useMessageApi'

interface Options {
  /** 加载失败是否静默（默认 false：toast 错误） */
  silent?: boolean
}

export function useCrudList<T>(url = '', autoLoad = true, opts: Options = {}) {
  const { msgApi, ctx, toastError } = useMessageApi()
  const [data, setData] = useState<T[]>([])
  const [loading, setLoading] = useState(false)

  const load = useCallback(async (u?: string, config?: AxiosRequestConfig) => {
    const target = u ?? url
    if (!target) return false
    setLoading(true)
    try {
      const r = await client.get<T[]>(target, config)
      setData(r.data)
      return true
    } catch (e) {
      if (!opts.silent) toastError(e)
      return false
    } finally {
      setLoading(false)
    }
  }, [url, opts.silent, toastError])

  useEffect(() => {
    if (autoLoad && url) load()
  }, [url, autoLoad, load])

  return { data, setData, loading, load, msgApi, ctx, toastError }
}
