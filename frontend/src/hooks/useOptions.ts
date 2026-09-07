// 公共选项数据加载：角色 / 用户 / 用户组 / 数据源 / 菜单 / 模型 下拉选项
// 页面挂载时静默加载一次（与旧代码 .catch(() => {}) 行为一致，不弹错误）
// 兼容列表接口分页化后的 {total, items} 返回结构
import { useEffect, useState } from 'react'
import { client } from '../api/client'
import type { PageResult } from '../types'

export type OptionKey =
  | 'roles'
  | 'users'
  | 'user-groups'
  | 'datasources'
  | 'menus'
  | 'models'

export function useOptions<T>(key: OptionKey): T[] {
  const [data, setData] = useState<T[]>([])
  useEffect(() => {
    // 列表接口已分页化：选项需全量，按 size 上限拉取
    client.get<T[] | PageResult<T>>(`/${key}`, { params: { page: 1, size: 200 } }).then((r) => {
      const d = r.data
      setData(Array.isArray(d) ? d : (d as PageResult<T>).items)
    }).catch(() => {})
  }, [key])
  return data
}
