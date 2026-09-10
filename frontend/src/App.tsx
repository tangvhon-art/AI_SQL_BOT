// 应用壳：海外版后台管理风格（参考 QueryAI · 数据控制台）
// 白色侧栏 + 白底顶栏 + 浅灰内容区；紫靛蓝主色 #6C5CE7 + 橙色伙伴色 #F97316
// 菜单为多级：AI 问数 / 数据管理（数据源、知识库）/ 系统管理（角色、用户组、用户、权限、模型、审计）/ 能力增强
import { useEffect, useState } from 'react'
import { Avatar, Breadcrumb, Dropdown, Layout, Menu, Space, Tag, Typography } from 'antd'
import {
  ApiOutlined, AppstoreOutlined, AuditOutlined, CommentOutlined, ControlOutlined, DatabaseOutlined,
  DownOutlined, ExperimentOutlined, FileTextOutlined, ForkOutlined, LogoutOutlined,
  RobotOutlined, SafetyCertificateOutlined, ScheduleOutlined, SettingOutlined,
  TeamOutlined, ThunderboltOutlined, UsergroupAddOutlined, UserOutlined,
} from '@ant-design/icons'
import type { MenuProps } from 'antd'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { TOKEN_KEY, client } from './api/client'
import type { UserInfo } from './types'

const { Sider, Header, Content } = Layout

const MENU: MenuProps['items'] = [
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
    ],
  },
  {
    key: 'cap', icon: <ExperimentOutlined />, label: '能力增强',
    children: [
      { key: '/eval', icon: <ExperimentOutlined />, label: '评测中心' },
      { key: '/lineage', icon: <ForkOutlined />, label: '血缘图谱' },
      { key: '/scenes', icon: <AppstoreOutlined />, label: '场景模板' },
      { key: '/cache', icon: <ThunderboltOutlined />, label: '缓存管理' },
      { key: '/sys-config', icon: <ControlOutlined />, label: '系统配置' },
      { key: '/reports', icon: <FileTextOutlined />, label: '报告中心' },
      { key: '/insight', icon: <ThunderboltOutlined />, label: '洞察分析' },
    ],
  },
  { key: '/saved', icon: <ScheduleOutlined />, label: '定时任务' },
]

const CRUMB_MAP: Record<string, { title: string; extra?: string }> = {
  '/chat': { title: 'AI 问数', extra: '自然语言查询数据库，结果含文字说明与图表' },
  '/datasources': { title: '数据源管理', extra: '连接配置、Schema 采集（含注释与外键关系）' },
  '/datasources/:id/schema': { title: 'Schema 详情', extra: '表/字段注释、表关联与 ER 关系图' },
  '/knowledge': { title: '知识库（RAG）', extra: 'FAQ / 文档切片向量化 / 检索测试' },
  '/roles': { title: '角色管理', extra: '角色 CRUD，可分配用户、用户组与菜单（权限取并集）' },
  '/groups': { title: '用户组管理', extra: '用户组 CRUD 与成员管理，组内角色随组生效' },
  '/users': { title: '用户管理', extra: '用户 CRUD，可分配角色与用户组（取并集）' },
  '/permissions': { title: '权限控制', extra: '角色/用户/用户组可查并集与不可查并集（黑名单优先），支持行级权限规则配置' },
  '/models': { title: '模型配置', extra: 'OpenAI 兼容 LLM 与 Embedding' },
  '/audit': { title: '审计日志', extra: '问数全链路留痕（含权限注入类型）' },
  '/eval': { title: '评测中心', extra: '评测用例管理、批次执行与指标报告（C4 评测闭环）' },
  '/lineage': { title: '血缘图谱', extra: '表/字段血缘查询与手动挖掘（C8 血缘）' },
  '/scenes': { title: '场景模板', extra: '六大场景模板包管理与场景识别测试（C14）' },
  '/cache': { title: '缓存管理', extra: 'SQL 生成缓存 / 结果缓存统计与清理（C1）' },
  '/sys-config': { title: '系统配置', extra: '成本门槛 / 限流超时 / 样例值采集等能力参数（DB 覆盖即时生效）' },
  '/saved': { title: '定时任务', extra: '保存查询参数化、cron 定时生成数据与图表' },
}

export default function App() {
  const nav = useNavigate()
  const loc = useLocation()
  const [collapsed, setCollapsed] = useState(false)
  const [me, setMe] = useState<UserInfo | null>(null)
  const seg = '/' + (loc.pathname.split('/')[1] ?? 'chat')
  const selected = loc.pathname.startsWith('/datasources/') ? '/datasources/:id/schema' : seg
  const crumb = CRUMB_MAP[selected] ?? CRUMB_MAP['/chat']
  // 当前路由所属的分组：子页面加载时自动展开对应父级菜单
  const parentOf = (s: string): string | undefined => {
    if (['/datasources', '/datasources/:id/schema', '/knowledge'].includes(s)) return 'data'
    if (['/roles', '/groups', '/users', '/permissions', '/models', '/prompts', '/audit'].includes(s)) return 'sys'
    if (['/eval', '/lineage', '/scenes', '/cache', '/sys-config', '/reports', '/insight'].includes(s)) return 'cap'
    return undefined
  }
  const defaultOpen = parentOf(selected)

  useEffect(() => {
    client.get<{ user: UserInfo }>('/auth/me').then((r) => setMe(r.data.user)).catch(() => {})
  }, [])

  const logout = () => {
    localStorage.removeItem(TOKEN_KEY)
    location.href = '/login'
  }

  return (
    <div className="glass-app">
    <Layout style={{ minHeight: '100vh', background: 'transparent' }}>
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        theme="light"
        width={232}
        className="glass-sider"
        style={{ position: 'sticky', top: 0, height: '100vh' }}
      >
        <div style={{ height: 56, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
          <div style={{
            width: 30, height: 30, borderRadius: 10, background: 'linear-gradient(135deg,#6C5CE7 0%,#A78BFA 100%)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#fff', fontWeight: 700, fontSize: 15,
            boxShadow: '0 4px 12px rgba(108,92,231,.35)',
          }}>
            Q
          </div>
          {!collapsed && (
            <div>
              <Typography.Text strong style={{ color: '#1A1D29', fontSize: 15, display: 'block', lineHeight: 1.2 }}>
                AI 问数系统
              </Typography.Text>
              <Typography.Text style={{ color: '#8B90A0', fontSize: 10, lineHeight: 1 }}>
                React 19 · AntD v6 · QueryAI
              </Typography.Text>
            </div>
          )}
        </div>
        <div style={{ height: 'calc(100vh - 104px)', overflowY: 'auto', overflowX: 'hidden' }}>
        <Menu
          theme="light"
          mode="inline"
          selectedKeys={[selected]}
          items={MENU}
          defaultOpenKeys={defaultOpen ? [defaultOpen] : []}
          onClick={(e) => nav(e.key)}
          style={{ borderInlineEnd: 'none', marginTop: 8, background: 'transparent' }}
        />
        </div>
      </Sider>
      <Layout style={{ background: 'transparent' }}>
        <Header className="glass-header" style={{
          padding: '0 24px', height: 56, lineHeight: '56px',
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          position: 'sticky', top: 0, zIndex: 10,
        }}>
          <Breadcrumb items={[{ title: 'AI 问数' }, { title: crumb.title }]} style={{ fontSize: 13 }} />
          <Space size={16}>
            <Tag style={{
              marginRight: 0, borderRadius: 999, paddingInline: 10,
              background: 'rgba(108,92,231,.10)', color: '#6C5CE7', border: '1px solid rgba(108,92,231,.20)', fontWeight: 500,
            }}>
              元数据库已连接
            </Tag>
            <Dropdown
              menu={{
                items: [{ key: 'logout', icon: <LogoutOutlined />, label: '退出登录' }],
                onClick: ({ key }) => key === 'logout' && logout(),
              }}
            >
              <Space style={{ cursor: 'pointer' }}>
                <Avatar size={30} style={{ background: 'linear-gradient(135deg,#6C5CE7,#A78BFA)' }} icon={<UserOutlined />} />
                <span style={{ fontSize: 13, color: '#3D4252' }}>{me?.display_name || me?.username || 'admin'}</span>
                <Tag style={{ borderRadius: 999, marginRight: 0, background: '#F8F9FB', border: '1px solid rgba(30,35,60,.08)', color: '#8B90A0' }}>
                  {me?.role_code || 'admin'}
                </Tag>
                <DownOutlined style={{ fontSize: 10, color: '#8B90A0' }} />
              </Space>
            </Dropdown>
          </Space>
        </Header>
        <Content className="glass-content" style={{ padding: 24 }}>
          <Outlet context={{ pageTitle: crumb.title, pageDesc: crumb.extra }} />
        </Content>
      </Layout>
    </Layout>
    </div>
  )
}
