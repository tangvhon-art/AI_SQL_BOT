/**
 * 菜单权限管理工具
 * 从后端获取用户有权限的菜单树，转换为 antd Menu 格式，提供权限检查
 */
import React from 'react'
import {
  ApiOutlined, AppstoreOutlined, AuditOutlined, CommentOutlined, ControlOutlined,
  DatabaseOutlined, ExperimentOutlined, FileTextOutlined, MenuOutlined, RobotOutlined,
  SafetyCertificateOutlined, ScheduleOutlined, SettingOutlined, TeamOutlined,
  ThunderboltOutlined, UsergroupAddOutlined, UserOutlined,
} from '@ant-design/icons'
import type { MenuProps } from 'antd'

// 图标字符串 -> 组件映射（与后端 menu.icon 字段对应）
const iconMap: Record<string, React.ReactNode> = {
  comment: <CommentOutlined />,
  database: <DatabaseOutlined />,
  api: <ApiOutlined />,
  filetext: <FileTextOutlined />,
  setting: <SettingOutlined />,
  team: <TeamOutlined />,
  usergroup: <UsergroupAddOutlined />,
  user: <UserOutlined />,
  safety: <SafetyCertificateOutlined />,
  robot: <RobotOutlined />,
  audit: <AuditOutlined />,
  menu: <MenuOutlined />,
  experiment: <ExperimentOutlined />,
  appstore: <AppstoreOutlined />,
  thunderbolt: <ThunderboltOutlined />,
  control: <ControlOutlined />,
  schedule: <ScheduleOutlined />,
}

export interface BackendMenuItem {
  id: number
  parent_id: number
  code: string
  name: string
  path: string
  icon: string
  children?: BackendMenuItem[]
}

export interface FlatMenu {
  key: string
  label: string
  icon?: React.ReactNode
  children?: FlatMenu[]
}

/**
 * 后端菜单树 -> antd Menu items 格式
 * key 使用 path（与路由对应），一级分组无 path 时使用 code
 */
export function convertMenuTree(tree: BackendMenuItem[]): MenuProps['items'] {
  return tree.map((item) => {
    const key = item.path || item.code
    const result: any = {
      key,
      icon: iconMap[item.icon] || <FileTextOutlined />,
      label: item.name,
    }
    if (item.children && item.children.length > 0) {
      result.children = convertMenuTree(item.children)
    }
    return result
  })
}

/**
 * 从菜单树中提取所有有权限的路径（用于路由守卫）
 */
export function extractAllowedPaths(tree: BackendMenuItem[]): string[] {
  const paths: string[] = []
  const walk = (items: BackendMenuItem[]) => {
    items.forEach((item) => {
      if (item.path) paths.push(item.path)
      if (item.children) walk(item.children)
    })
  }
  walk(tree)
  return paths
}

/**
 * 检查路径是否有权限
 * 允许的路径：/（重定向）、/datasources/:id/schema（子页面）、以及菜单中配置的路径
 */
export function hasPathPermission(path: string, allowedPaths: string[]): boolean {
  // 公共路径
  if (path === '/' || path === '/login') return true
  // 子页面（如 /datasources/3/schema）
  if (path.match(/^\/datasources\/\d+\/schema$/)) return true
  // 精确匹配
  if (allowedPaths.includes(path)) return true
  return false
}
