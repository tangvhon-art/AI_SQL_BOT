// SQL 折叠块：默认收起，可展开查看/复制/下载
import { Button, Collapse, Space, Typography, message } from 'antd'
import { CopyOutlined, DownloadOutlined, CodeOutlined } from '@ant-design/icons'

export default function SqlBlock({ sql, permission }: { sql: string; permission?: string }) {
  const [msgApi, ctx] = message.useMessage()

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(sql)
      msgApi.success('已复制 SQL')
    } catch {
      msgApi.error('复制失败')
    }
  }

  const download = () => {
    const blob = new Blob([sql], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'query.sql'
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div style={{ margin: '8px 0' }}>
      {ctx}
      <Collapse
        size="small"
        items={[
          {
            key: 'sql',
            label: (
              <Space size={8}>
                <CodeOutlined />
                <span>查看 SQL</span>
                {permission ? (
                  <Typography.Text type={permission === '1=2' ? 'danger' : 'success'} style={{ fontSize: 12 }}>
                    权限注入：{permission}
                  </Typography.Text>
                ) : null}
              </Space>
            ),
            children: (
              <div>
                <pre
                  style={{
                    background: '#0f172a',
                    color: '#e2e8f0',
                    padding: 12,
                    borderRadius: 8,
                    overflowX: 'auto',
                    fontSize: 13,
                  }}
                >
                  {sql}
                </pre>
                <Space style={{ marginTop: 8 }}>
                  <Button size="small" icon={<CopyOutlined />} onClick={copy}>
                    复制
                  </Button>
                  <Button size="small" icon={<DownloadOutlined />} onClick={download}>
                    下载
                  </Button>
                </Space>
              </div>
            ),
          },
        ]}
      />
    </div>
  )
}
