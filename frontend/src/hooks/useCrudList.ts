// 通用列表加载 hook：统一 loading / 错误提示 / 挂载自动加载
// 用法：const { data, setData, load, msgApi, ctx } = useCrudList<T>('/xxx')
//       load(url?, config?) 可带参覆盖默认 url（如筛选查询）；失败返回 false 并自动 toast
import { useCallback, useEffect, useRef, useState } from 'react'
import type { AxiosRequestConfig } from 'axios'
import { client } from '../api/client'
import { useMessageApi } from './useMessageApi'
import type { PageResult } from '../types'

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

// ==================== 分页列表（页面公共化） ====================
// 服务端分页：page/size 参数 + {total, items} 返回；支持查询条件（重置回第 1 页）、翻页、刷新。
// 用法：
//   const list = usePagedList<T>('/xxx')
//   list.search({ keyword, status })   // 查询条件查询（回到第 1 页）
//   list.onPageChange(page, size)      // 翻页 / 改每页条数
//   list.reload()                      // 增删改后刷新当前页
// 表格分页：<Table pagination={tablePagination(list.page, list.size, list.total, list.onPageChange)} />
interface PagedOptions extends Options {
  /** 默认每页条数（默认 20） */
  defaultSize?: number
}

export function usePagedList<T>(url: string, opts: PagedOptions = {}) {
  const { msgApi, ctx, toastError } = useMessageApi()
  const [items, setItems] = useState<T[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [size, setSize] = useState(opts.defaultSize ?? 20)
  const [loading, setLoading] = useState(false)
  const paramsRef = useRef<Record<string, unknown>>({})

  const doLoad = useCallback(async (p: number, s: number, query: Record<string, unknown>) => {
    setLoading(true)
    try {
      const r = await client.get<PageResult<T> | T[]>(url, { params: { page: p, size: s, ...query } })
      const next = Array.isArray(r.data) ? { total: r.data.length, items: r.data } : r.data
      // 当前页被删空且非第一页 → 自动回退一页（避免停留在空页）
      if (next.total === 0 && p > 1) {
        const r2 = await client.get<PageResult<T> | T[]>(url, { params: { page: p - 1, size: s, ...query } })
        const n2 = Array.isArray(r2.data) ? { total: r2.data.length, items: r2.data } : r2.data
        setItems(n2.items)
        setTotal(n2.total)
        setPage(p - 1)
        setSize(s)
        return true
      }
      setItems(next.items)
      setTotal(next.total)
      setPage(p)
      setSize(s)
      return true
    } catch (e) {
      if (!opts.silent) toastError(e)
      return false
    } finally {
      setLoading(false)
    }
  }, [url, opts.silent, toastError])

  /** 查询条件查询：记录条件并回到第 1 页 */
  const search = useCallback((query: Record<string, unknown>) => {
    paramsRef.current = query
    return doLoad(1, size, query)
  }, [doLoad, size])

  /** 翻页 / 修改每页条数 */
  const onPageChange = useCallback((p: number, s: number) => {
    return doLoad(p, s, paramsRef.current)
  }, [doLoad])

  /** 刷新当前页（增删改后调用） */
  const reload = useCallback(() => doLoad(page, size, paramsRef.current), [doLoad, page, size])

  useEffect(() => {
    if (url) doLoad(1, size, {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url])

  return { items, setItems, total, page, size, loading, search, reload, onPageChange, msgApi, ctx, toastError }
}

/** AntD Table 统一分页配置（页面公共化：显示总数 + 可改每页条数） */
export function tablePagination(
  page: number,
  size: number,
  total: number,
  onChange: (page: number, size: number) => void,
) {
  return {
    current: page,
    pageSize: size,
    total,
    showSizeChanger: true,
    showTotal: (t: number) => `共 ${t} 条`,
    onChange,
  }
}
