// 报告中心：列表 + 详情
import { useEffect, useState } from 'react'
import { Button, Card, Descriptions, Empty, Input, Popconfirm, Space, Table, Tag, message } from 'antd'
import { DeleteOutlined, EyeOutlined, FileTextOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { client } from '../api/client'
import { Dashboard, convertDashboardEvent } from '../components/dashboard/Dashboard'
import { AiInterpretation } from '../components/dashboard/AiInterpretation'
import type { InterpretationResult } from '../types/chart'

interface ReportItem {
  id: number
  title: string
  original_question: string
  remark: string
  created_at: string
}

export default function ReportCenterPage() {
  const navigate = useNavigate()
  const [items, setItems] = useState<ReportItem[]>([])
  const [loading, setLoading] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [detail, setDetail] = useState<any>(null)

  const load = async () => {
    setLoading(true)
    try {
      const r = await client.get('/reports', { params: { keyword, page: 1, page_size: 50 } })
      setItems(r.data.items || [])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [keyword])

  const handleView = async (id: number) => {
    const r = await client.get(`/reports/${id}`)
    setDetail(r.data.item)
  }

  const handleDelete = async (id: number) => {
    await client.delete(`/reports/${id}`)
    message.success('已删除')
    if (detail?.id === id) setDetail(null)
    load()
  }

  const columns = [
    { title: '标题', dataIndex: 'title', key: 'title', render: (t: string, r: ReportItem) => (
      <a onClick={() => handleView(r.id)}><FileTextOutlined style={{ marginRight: 6 }} />{t}</a>
    )},
    { title: '原始问题', dataIndex: 'original_question', key: 'q', ellipsis: true },
    { title: '创建时间', dataIndex: 'created_at', key: 'created_at', width: 180, render: (v: string) => v?.slice(0, 19).replace('T', ' ') },
    { title: '操作', key: 'actions', width: 150, render: (_: any, r: ReportItem) => (
      <Space>
        <Button size="small" type="link" icon={<EyeOutlined />} onClick={() => handleView(r.id)}>查看</Button>
        <Popconfirm title="确认删除？" onConfirm={() => handleDelete(r.id)}><Button size="small" type="link" danger icon={<DeleteOutlined />}>删除</Button></Popconfirm>
      </Space>
    )},
  ]

  // 详情视图
  if (detail) {
    const dashboardData = detail.dashboard_data ? convertDashboardEvent(detail.dashboard_data) : null
    const interpretation = detail.ai_interpretation as InterpretationResult | undefined
    return (
      <div style={{ padding: 16 }}>
        <Card
          title={<Space><FileTextOutlined />{detail.title}</Space>}
          extra={<Button onClick={() => setDetail(null)}>返回列表</Button>}
        >
          <Descriptions size="small" column={3} style={{ marginBottom: 16 }}>
            <Descriptions.Item label="原始问题">{detail.original_question}</Descriptions.Item>
            <Descriptions.Item label="创建时间">{detail.created_at?.slice(0, 19).replace('T', ' ')}</Descriptions.Item>
            <Descriptions.Item label="备注">{detail.remark || '-'}</Descriptions.Item>
          </Descriptions>
          {dashboardData ? (
            <Dashboard data={dashboardData} showToolbar={false} />
          ) : <Empty description="无Dashboard数据" />}
          {(interpretation || detail.interpretation_text) && (
            <div style={{ marginTop: 16 }}>
              <AiInterpretation result={interpretation} rawText={detail.interpretation_text} />
            </div>
          )}
        </Card>
      </div>
    )
  }

  return (
    <div style={{ padding: 16 }}>
      <Card
        title="报告中心"
        extra={<Input.Search placeholder="搜索标题" allowClear style={{ width: 240 }} onSearch={setKeyword} />}
      >
        <Table rowKey="id" columns={columns} dataSource={items} loading={loading} pagination={{ pageSize: 10 }} />
      </Card>
    </div>
  )
}
