/**
 * 菜单统一配置中心
 *
 * 【菜单注册指南】
 * 新增一个页面只需 3 步：
 *   1. 在 src/pages/ 下创建页面组件（如 XxxPage.tsx）
 *   2. 在 src/router.tsx 中添加路由：<Route path="/xxx" element={<XxxPage />} />
 *   3. 在本文件 MENU 数组中添加菜单项（见下方格式说明）
 *
 * 【菜单项格式】
 *   一级菜单：{ key: '/path', icon: <IconOutlined />, label: '菜单名称' }
 *   二级菜单：{ key: 'group-key', icon: <IconOutlined />, label: '分组名称', children: [ ... ] }
 *
 * 【分组归属】
 *   新增子菜单时，需同时在 parentOf() 中声明所属分组，否则子页面加载时父菜单不会自动展开。
 *
 * 【面包屑】
 *   如需自定义面包屑标题/说明，在 CRUMB_MAP 中添加对应路径。
 */
import React from 'react'
import {
  ApiOutlined, AppstoreOutlined, AuditOutlined, CommentOutlined, ControlOutlined,
  DatabaseOutlined, ExperimentOutlined, FileTextOutlined, RobotOutlined,
  SafetyCertificateOutlined, ScheduleOutlined, SettingOutlined, TeamOutlined,
  ThunderboltOutlined, UsergroupAddOutlined, UserOutlined,
  MenuOutlined,
} from '@ant-design/icons'
import type { MenuProps } from 'antd'

// ========== 菜单配置 ==========
export const MENU: MenuProps['items'] = [
  { key: '/chat', icon: <CommentOutlined />, label: 'AI 问数' },
  {
    key: 'data', icon: <DatabaseOutlined />, label: '数据管理',
    children: [
      { key: '/datasources', icon: <ApiOutlined />, label: '数据源管理' },
      { key: '/knowledge', icon: <FileTextOutlined />, label: '知识库（RAG）' },
    ],
  },
  {
    key: 'sys', icon: <SettingOutlined />, label: '系统管理',
    children: [
      { key: '/roles', icon: <TeamOutlined />, label: '角色管理' },
      { key: '/groups', icon: <UsergroupAddOutlined />, label: '用户组管理' },
      { key: '/users', icon: <UserOutlined />, label: '用户管理' },
      { key: '/permissions', icon: <SafetyCertificateOutlined />, label: '权限控制' },
      { key: '/models', icon: <RobotOutlined />, label: '模型配置' },
      { key: '/prompts', icon: <FileTextOutlined />, label: 'Prompt管理' },
      { key: '/audit', icon: <AuditOutlined />, label: '审计日志' },
      { key: '/menu-config', icon: <MenuOutlined />, label: '菜单中心' },
    ],
  },
  {
    key: 'cap', icon: <ExperimentOutlined />, label: '能力增强',
    children: [
      { key: '/eval', icon: <ExperimentOutlined />, label: '评测中心' },
      { key: '/scenes', icon: <AppstoreOutlined />, label: '场景模板' },
      { key: '/cache', icon: <ThunderboltOutlined />, label: '缓存管理' },
      { key: '/sys-config', icon: <ControlOutlined />, label: '系统配置' },
      { key: '/reports', icon: <FileTextOutlined />, label: '报告中心' },
      { key: '/insight', icon: <ThunderboltOutlined />, label: '洞察分析' },
    ],
  },
]

// ========== 面包屑配置 ==========
export const CRUMB_MAP: Record<string, { title: string; extra?: string }> = {
  '/chat': { title: 'AI 问数', extra: '自然语言查询数据库，结果含文字说明与图表' },
  '/datasources': { title: '数据源管理', extra: '连接配置、Schema 采集（含注释与外键关系）' },
  '/datasources/:id/schema': { title: 'Schema 详情', extra: '表/字段注释、表关联与 ER 关系图' },
  '/knowledge': { title: '知识库（RAG）', extra: 'FAQ / 文档切片向量化 / 检索测试' },
  '/roles': { title: '角色管理', extra: '角色 CRUD，可分配用户、用户组与菜单（权限取并集）' },
  '/groups': { title: '用户组管理', extra: '用户组 CRUD 与成员管理，组内角色随组生效' },
  '/users': { title: '用户管理', extra: '用户 CRUD，可分配角色与用户组（取并集）' },
  '/permissions': { title: '权限控制', extra: '角色/用户/用户组可查并集与不可查并集（黑名单优先），支持行级权限规则配置' },
  '/models': { title: '模型配置', extra: 'OpenAI 兼容 LLM 与 Embedding' },
  '/prompts': { title: 'Prompt管理', extra: '场景化提示词模板管理（AI 解读 / SQL 生成等）' },
  '/audit': { title: '审计日志', extra: '问数全链路留痕（含权限注入类型）' },
  '/menu-config': { title: '菜单中心', extra: '菜单结构展示与注册指南（只读）' },
  '/eval': { title: '评测中心', extra: '评测用例管理、批次执行与指标报告' },
  '/scenes': { title: '场景模板', extra: '场景模板包管理与场景识别测试' },
  '/cache': { title: '缓存管理', extra: 'SQL 生成缓存 / 结果缓存统计与清理' },
  '/sys-config': { title: '系统配置', extra: '成本门槛 / 限流超时 / 样例值采集等能力参数（DB 覆盖即时生效）' },
  '/reports': { title: '报告中心', extra: 'AI 问数报告管理、单图表重新生成、AI 解读' },
  '/insight': { title: '洞察分析', extra: 'AI 生成分析草案、多图表配置、预览与报告生成' },
}

// ========== 分组归属（子页面加载时自动展开父级菜单）==========
export function parentOf(path: string): string | undefined {
  if (['/datasources', '/datasources/:id/schema', '/knowledge'].includes(path)) return 'data'
  if (['/roles', '/groups', '/users', '/permissions', '/models', '/prompts', '/audit', '/menu-config'].includes(path)) return 'sys'
  if (['/eval', '/scenes', '/cache', '/sys-config', '/reports', '/insight'].includes(path)) return 'cap'
  return undefined
}
