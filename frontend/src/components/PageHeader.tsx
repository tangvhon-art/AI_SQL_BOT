// Pro 风格页头（PageContainer）：标题 + 描述 + 面包屑 + 右侧操作区
import { Breadcrumb, Space, Typography } from 'antd'
import type { ReactNode } from 'react'

interface Props {
  title: string
  description?: string
  extra?: ReactNode
  breadcrumb?: Array<{ title: string }>
}

export default function PageHeader({ title, description, extra, breadcrumb }: Props) {
  return (
    <div style={{ marginBottom: 16 }}>
      {breadcrumb ? (
        <Breadcrumb
          style={{ marginBottom: 12 }}
          items={breadcrumb}
        />
      ) : null}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 12 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0, fontWeight: 700, fontSize: 24, letterSpacing: '-.02em', color: '#1A1D29' }}>
            {title}
          </Typography.Title>
          {description ? (
            <Typography.Paragraph type="secondary" style={{ margin: '6px 0 0', fontSize: 13, color: '#8B90A0' }}>
              {description}
            </Typography.Paragraph>
          ) : null}
        </div>
        {extra ? <Space wrap>{extra}</Space> : null}
      </div>
    </div>
  )
}
