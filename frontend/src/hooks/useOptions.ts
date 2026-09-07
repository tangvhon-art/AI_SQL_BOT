// 公共选项数据加载：角色 / 用户 / 用户组 / 数据源 / 菜单 / 模型 下拉选项
// 页面挂载时静默加载一次（与旧代码 .catch(() => {}) 行为一致，不弹错误）
import { useEffect, useState } from 'react'
import { client } from '../api/client'

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
    client.get<T[]>(`/${key}`).then((r) => setData(r.data)).catch(() => {})
  }, [key])
  return data
}
