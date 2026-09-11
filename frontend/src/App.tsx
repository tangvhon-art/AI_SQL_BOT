// 应用壳：海外版后台管理风格（参考 QueryAI · 数据控制台）
// 白色侧栏 + 白底顶栏 + 浅灰内容区；紫靛蓝主色 #6C5CE7 + 橙色伙伴色 #F97316
// 菜单为多级：AI 问数 / 数据管理（数据源、知识库）/ 系统管理（角色、用户组、用户、权限、模型、审计）/ 能力增强
import { useEffect, useState } from 'react'
import { Avatar, Breadcrumb, Dropdown, Layout, Menu, Space, Tag, Typography } from 'antd'
import { DownOutlined, LogoutOutlined, UserOutlined } from '@ant-design/icons'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { TOKEN_KEY, client } from './api/client'
import type { UserInfo } from './types'
import { CRUMB_MAP, parentOf } from './config/menu'
import { convertMenuTree, extractAllowedPaths, hasPathPermission, type BackendMenuItem } from './utils/menu-permission'

const { Sider, Header, Content } = Layout

export default function App() {
  const nav = useNavigate()
  const loc = useLocation()
  const [collapsed, setCollapsed] = useState(false)
  const [me, setMe] = useState<UserInfo | null>(null)
  const [menuItems, setMenuItems] = useState<any[]>([])
  const [allowedPaths, setAllowedPaths] = useState<string[]>([])
  const [menuLoading, setMenuLoading] = useState(true)
  const seg = '/' + (loc.pathname.split('/')[1] ?? 'chat')
  const selected = loc.pathname.startsWith('/datasources/') ? '/datasources/:id/schema' : seg
  const crumb = CRUMB_MAP[selected] ?? CRUMB_MAP['/chat']
  const defaultOpen = parentOf(selected) ? [parentOf(selected)!] : []

  useEffect(() => {
    client.get<{ user: UserInfo }>('/auth/me').then((r) => setMe(r.data.user)).catch(() => {})
    // 获取当前用户有权限的菜单树
    client.get<BackendMenuItem[]>('/menus/tree').then((r) => {
      const tree = r.data
      setMenuItems(convertMenuTree(tree) || [])
      setAllowedPaths(extractAllowedPaths(tree))
    }).catch(() => {
      // 接口失败时回退到全量菜单（开发环境兜底）
      import('./config/menu').then((m) => setMenuItems(m.MENU as any[]))
    }).finally(() => setMenuLoading(false))
  }, [])

  // 路由守卫：无权限路径重定向到 /chat
  useEffect(() => {
    if (!menuLoading && allowedPaths.length > 0 && !hasPathPermission(loc.pathname, allowedPaths)) {
      nav('/chat', { replace: true })
    }
  }, [loc.pathname, menuLoading, allowedPaths, nav])

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
          items={menuItems}
          defaultOpenKeys={defaultOpen}
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
