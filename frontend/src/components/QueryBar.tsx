// 页面公共化：统一查询条件栏（条件控件 + 查询 / 重置）
// 用法：<QueryBar onSearch={...} onReset={...} loading={...}>
//         <Input placeholder="名称" ... />
//         <GlassSelect placeholder="状态" ... />
//       </QueryBar>
import { Button, Space } from 'antd'
import { ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import type { ReactNode } from 'react'

export default function QueryBar({ children, onSearch, onReset, loading }: {
  children?: ReactNode
  onSearch: () => void
  onReset: () => void
  loading?: boolean
}) {
  return (
    <Space wrap size={8} style={{ marginBottom: 12 }}>
      {children}
      <Button type="primary" icon={<SearchOutlined />} loading={loading} onClick={onSearch}>
        查询
      </Button>
      <Button icon={<ReloadOutlined />} onClick={onReset}>
        重置
      </Button>
    </Space>
  )
}
