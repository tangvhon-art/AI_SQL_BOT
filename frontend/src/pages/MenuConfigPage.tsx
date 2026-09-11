/**
 * 菜单中心（只读展示）
 * 展示当前用户有权限的菜单树状结构，支持展开收起
 */
import { useEffect, useMemo, useState } from 'react'
import { Card, Spin, Tag, Tooltip } from 'antd'
import {
  ApiOutlined, AppstoreOutlined, AuditOutlined, CaretRightOutlined, CommentOutlined,
  ControlOutlined, DatabaseOutlined, ExperimentOutlined, FileTextOutlined, MenuOutlined,
  RobotOutlined, SafetyCertificateOutlined, ScheduleOutlined, SettingOutlined, TeamOutlined,
  ThunderboltOutlined, UsergroupAddOutlined, UserOutlined,
} from '@ant-design/icons'
import { client } from '../api/client'
import type { BackendMenuItem } from '../utils/menu-permission'

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

const PRIMARY = '#6C5CE7'
const PRIMARY_LIGHT = 'rgba(108,92,231,.08)'
const PRIMARY_BG = 'rgba(108,92,231,.06)'

interface TreeNodeProps {
  item: BackendMenuItem
  level: number
  expanded: Set<string>
  onToggle: (key: string) => void
}

function TreeNode({ item, level, expanded, onToggle }: TreeNodeProps) {
  const key = item.path || item.code
  const hasChildren = item.children && item.children.length > 0
  const isExpanded = expanded.has(key)
  const isGroup = level === 0

  return (
    <div>
      <div
        onClick={() => hasChildren && onToggle(key)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '10px 16px',
          paddingLeft: 16 + level * 24,
          cursor: hasChildren ? 'pointer' : 'default',
          background: isGroup ? PRIMARY_BG : 'transparent',
          borderBottom: '1px solid rgba(30,35,60,.06)',
          transition: 'background .15s ease',
        }}
        onMouseEnter={(e) => { if (!isGroup) e.currentTarget.style.background = PRIMARY_LIGHT }}
        onMouseLeave={(e) => { if (!isGroup) e.currentTarget.style.background = 'transparent' }}
      >
        {/* 展开箭头 */}
        <span style={{ width: 16, display: 'flex', justifyContent: 'center', color: hasChildren ? PRIMARY : 'transparent' }}>
          <CaretRightOutlined
            style={{
              fontSize: 11,
              transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
              transition: 'transform .2s ease',
            }}
          />
        </span>
        {/* 图标 */}
        <span style={{
          fontSize: 16,
          color: isGroup ? PRIMARY : '#8B90A0',
          display: 'flex',
          alignItems: 'center',
        }}>
          {iconMap[item.icon] || <FileTextOutlined />}
        </span>
        {/* 名称 */}
        <span style={{
          fontWeight: isGroup ? 600 : 400,
          fontSize: isGroup ? 14 : 13,
          color: isGroup ? '#1A1D29' : '#3D4252',
          minWidth: 110,
        }}>
          {item.name}
        </span>
        {/* 路径标签 */}
        {item.path ? (
          <Tag
            color="default"
            style={{
              fontSize: 11,
              marginRight: 0,
              borderRadius: 6,
              background: '#F8F9FB',
              border: '1px solid rgba(30,35,60,.08)',
              color: '#8B90A0',
              fontFamily: 'monospace',
            }}
          >
            {item.path}
          </Tag>
        ) : (
          <Tooltip title="菜单分组（无路由）">
            <Tag color="purple" style={{ fontSize: 11, marginRight: 0, borderRadius: 6, background: PRIMARY_LIGHT, border: '1px solid rgba(108,92,231,.2)', color: PRIMARY }}>
              分组
            </Tag>
          </Tooltip>
        )}
        {/* 子菜单数量 */}
        {hasChildren && (
          <span style={{ marginLeft: 'auto', fontSize: 12, color: '#8B90A0' }}>
            {item.children!.length} 项
          </span>
        )}
      </div>
      {/* 子菜单 */}
      {hasChildren && isExpanded && (
        <div>
          {item.children!.map((child) => (
            <TreeNode
              key={child.path || child.code}
              item={child}
              level={level + 1}
              expanded={expanded}
              onToggle={onToggle}
            />
          ))}
        </div>
      )}
    </div>
  )
}

export default function MenuConfigPage() {
  const [treeData, setTreeData] = useState<BackendMenuItem[]>([])
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  useEffect(() => {
    client.get<BackendMenuItem[]>('/menus/tree').then((r) => {
      setTreeData(r.data)
      // 默认展开所有一级分组
      const defaultExpanded = new Set<string>()
      r.data.forEach((m) => {
        if (m.children && m.children.length > 0) {
          defaultExpanded.add(m.path || m.code)
        }
      })
      setExpanded(defaultExpanded)
    }).finally(() => setLoading(false))
  }, [])

  const toggle = (key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const expandAll = () => {
    const all = new Set<string>()
    const walk = (items: BackendMenuItem[]) => {
      items.forEach((m) => {
        if (m.children && m.children.length > 0) {
          all.add(m.path || m.code)
          walk(m.children)
        }
      })
    }
    walk(treeData)
    setExpanded(all)
  }

  const collapseAll = () => setExpanded(new Set())

  const stats = useMemo(() => {
    let total = 0
    let groups = 0
    const walk = (items: BackendMenuItem[]) => {
      items.forEach((m) => {
        total++
        if (m.children && m.children.length > 0) {
          groups++
          walk(m.children)
        }
      })
    }
    walk(treeData)
    return { total, groups, leaves: total - groups }
  }, [treeData])

  return (
    <div style={{ padding: 24 }}>
      {/* 页面标题区 */}
      <div style={{ marginBottom: 20, display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end' }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 20, fontWeight: 700, color: '#1A1D29' }}>菜单中心</h2>
          <p style={{ margin: '6px 0 0', fontSize: 13, color: '#8B90A0' }}>
            当前账号可访问的菜单结构，共 {stats.total} 个菜单（{stats.groups} 个分组 · {stats.leaves} 个页面）
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <span
            onClick={expandAll}
            style={{
              padding: '6px 14px',
              fontSize: 13,
              color: PRIMARY,
              background: PRIMARY_LIGHT,
              border: '1px solid rgba(108,92,231,.2)',
              borderRadius: 8,
              cursor: 'pointer',
              fontWeight: 500,
              transition: 'all .15s',
            }}
          >
            全部展开
          </span>
          <span
            onClick={collapseAll}
            style={{
              padding: '6px 14px',
              fontSize: 13,
              color: '#3D4252',
              background: '#ffffff',
              border: '1px solid rgba(30,35,60,.14)',
              borderRadius: 8,
              cursor: 'pointer',
              fontWeight: 500,
              transition: 'all .15s',
            }}
          >
            全部收起
          </span>
        </div>
      </div>

      {/* 菜单树卡片 */}
      <Card
        bodyStyle={{ padding: 0 }}
        style={{
          borderRadius: 12,
          border: '1px solid rgba(30,35,60,.08)',
          boxShadow: '0 1px 2px rgba(15,35,34,.04), 0 6px 16px -4px rgba(15,35,34,.06)',
        }}
      >
        {loading ? (
          <div style={{ padding: 60, textAlign: 'center' }}>
            <Spin size="large" style={{ color: PRIMARY }} />
            <p style={{ marginTop: 12, color: '#8B90A0', fontSize: 13 }}>加载菜单中...</p>
          </div>
        ) : (
          <div>
            {treeData.map((item) => (
              <TreeNode
                key={item.path || item.code}
                item={item}
                level={0}
                expanded={expanded}
                onToggle={toggle}
              />
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}
