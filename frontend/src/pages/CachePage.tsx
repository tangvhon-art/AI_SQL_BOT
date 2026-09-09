// C1 缓存管理：两级缓存（SQL 生成缓存 / 结果缓存）统计与清理
// - 统计卡片：总量 / 类型分布 / 累计命中
// - 最近条目：按命中时间倒序，展示问句 / SQL / 命中次数 / 过期时间
// - 清理：全部清空（按工作空间）/ 按类型清空
import { useEffect, useState } from 'react'
import { Button, Card, Col, Popconfirm, Row, Space, Statistic, Table, Tag, Typography } from 'antd'
import { DeleteOutlined, ReloadOutlined } from '@ant-design/icons'
import { client } from '../api/client'
import type { CacheStats } from '../types'
import PageHeader from '../components/PageHeader'
import { useMessageApi } from '../hooks/useMessageApi'

const TYPE_META: Record<string, { label: string; color: string }> = {
  gen: { label: 'SQL 生成缓存', color: 'blue' },
  result: { label: '结果缓存', color: 'green' },
}

export default function CachePage() {
  const { msgApi, ctx, toastError } = useMessageApi()
  const [stats, setStats] = useState<CacheStats | null>(null)
  const [loading, setLoading] = useState(false)
  const [clearing, setClearing] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const r = await client.get<CacheStats>('/cache/stats')
      setStats(r.data)
    } catch (e) { toastError(e) } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const clearCache = async (type?: string) => {
    setClearing(true)
    try {
      await client.delete('/cache', { params: type ? { cache_type: type } : {} })
      msgApi.success('缓存已清空')
      load()
    } catch (e) { toastError(e) } finally { setClearing(false) }
  }

  const byTypeEntries = stats ? Object.entries(stats.by_type || {}) : []

  return (
    <div className="glass-page">
      <PageHeader
        title="缓存管理"
        description="一级 SQL 生成缓存（问句相似度命中，TTL 7 天）+ 二级结果缓存（SQL 指纹命中，TTL 15 分钟）；权限/Schema 变更自动失效"
        extra={(
          <Space>
            <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
            <Popconfirm title="清空当前工作空间全部缓存？" onConfirm={() => clearCache()}>
              <Button danger icon={<DeleteOutlined />} loading={clearing}>清空缓存</Button>
            </Popconfirm>
          </Space>
        )}
      />
      {ctx}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} sm={8}>
          <Card size="small"><Statistic title="缓存条目总数" value={stats?.total ?? 0} loading={loading} /></Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card size="small"><Statistic title="累计命中次数" value={stats?.total_hits ?? 0} loading={loading} /></Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card size="small">
            <Statistic title="类型分布" value={byTypeEntries.length} suffix="类" loading={loading} />
            <Space size={4} style={{ marginTop: 8 }}>
              {byTypeEntries.map(([k, v]) => (
                <Tag key={k} color={TYPE_META[k]?.color || 'default'}>
                  {TYPE_META[k]?.label || k} {v}
                </Tag>
              ))}
              {byTypeEntries.length === 0 && <Typography.Text type="secondary" style={{ fontSize: 12 }}>无缓存条目</Typography.Text>}
            </Space>
          </Card>
        </Col>
      </Row>

      <Card
        size="small"
        title="最近缓存条目（按命中时间倒序）"
        extra={<Space wrap>{byTypeEntries.map(([k]) => (
          <Popconfirm key={k} title={`清空${TYPE_META[k]?.label || k}？`} onConfirm={() => clearCache(k)}>
            <Button size="small">清空{TYPE_META[k]?.label || k}</Button>
          </Popconfirm>
        ))}</Space>}
      >
        <Table
          rowKey="id"
          size="small"
          loading={loading}
          dataSource={stats?.recent || []}
          pagination={{ pageSize: 10, showSizeChanger: false }}
          locale={{ emptyText: '暂无缓存条目' }}
          columns={[
            { title: '类型', dataIndex: 'cache_type', width: 130, render: (v: string) => (
              <Tag color={TYPE_META[v]?.color || 'default'}>{TYPE_META[v]?.label || v}</Tag>) },
            { title: '问句', dataIndex: 'question', ellipsis: true },
            { title: 'SQL', dataIndex: 'sql_text', ellipsis: true, render: (v: string) => (
              <code style={{ fontSize: 12 }}>{v || '-'}</code>) },
            { title: '数据源', dataIndex: 'datasource_id', width: 90 },
            { title: '命中', dataIndex: 'hit_count', width: 70 },
            {
              title: '过期时间', dataIndex: 'expires_at', width: 170,
              render: (v: string | null) => v ? new Date(v).toLocaleString('zh-CN') : '-',
            },
          ]}
        />
      </Card>
    </div>
  )
}
