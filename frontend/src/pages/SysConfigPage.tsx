// 系统配置：能力参数（C1 缓存 / C2 成本门槛 / C6 限流超时 / C7 样例值 / C8 血缘 / C4 评测）
// - GET /system-config 读取生效值（.env 默认 + DB 覆盖）
// - 每分组独立表单保存，PUT 后即时生效（引擎统一走 get_effective）
// - 已覆盖字段高亮标识，可一键恢复默认（清空对应字段后保存）
import { useEffect, useState } from 'react'
import {
  Button, Card, Col, Form, InputNumber, Row, Space, Switch, Tag, Typography,
} from 'antd'
import { ReloadOutlined, SaveOutlined } from '@ant-design/icons'
import { client } from '../api/client'
import type { SystemConfigResp } from '../types'
import PageHeader from '../components/PageHeader'
import { useMessageApi } from '../hooks/useMessageApi'

interface FieldMeta {
  key: string
  label: string
  type: 'bool' | 'int' | 'float'
  unit?: string
  hint?: string
}

const SECTIONS_META: Array<{ section: string; title: string; desc: string; fields: FieldMeta[] }> = [
  {
    section: 'cache', title: 'C1 缓存', desc: 'SQL 生成缓存与结果缓存',
    fields: [
      { key: 'cache_enabled', label: '启用缓存', type: 'bool' },
      { key: 'cache_ttl_result_sec', label: '结果缓存 TTL', type: 'int', unit: '秒' },
      { key: 'cache_ttl_gen_sec', label: '生成缓存 TTL', type: 'int', unit: '秒', hint: '默认 604800（7 天）' },
      { key: 'cache_similarity', label: '问句相似阈值', type: 'float', hint: '0~1，越高越严格' },
      { key: 'cache_max_entries_per_ws', label: '单工作空间条目上限', type: 'int' },
    ],
  },
  {
    section: 'cost_guard', title: 'C2 Explain 成本门槛', desc: '查询成本评估与拦截（MySQL rows / PG total cost）',
    fields: [
      { key: 'cost_guard_enabled', label: '启用成本守卫', type: 'bool' },
      { key: 'cost_guard_explain_timeout_ms', label: 'EXPLAIN 超时', type: 'int', unit: 'ms' },
      { key: 'cost_guard_mysql_rows_soft', label: 'MySQL 软阈值（rows）', type: 'int', unit: '行' },
      { key: 'cost_guard_mysql_rows_hard', label: 'MySQL 硬阈值（rows）', type: 'int', unit: '行', hint: '超过则拦截' },
      { key: 'cost_guard_pg_cost_soft', label: 'PG 软阈值（cost）', type: 'float' },
      { key: 'cost_guard_pg_cost_hard', label: 'PG 硬阈值（cost）', type: 'float', hint: '超过则拦截' },
    ],
  },
  {
    section: 'rate_limit', title: 'C6 超时与限流', desc: '用户级 QPS 限流 + 全局并发闸 + 查询超时',
    fields: [
      { key: 'rate_limit_enabled', label: '启用限流', type: 'bool' },
      { key: 'rate_limit_qps', label: '单用户 QPS', type: 'float', hint: '默认 2.0' },
      { key: 'rate_limit_burst', label: '突发容量', type: 'int' },
      { key: 'rate_limit_max_concurrent', label: '全局最大并发', type: 'int' },
      { key: 'rate_limit_queue_timeout_s', label: '排队超时', type: 'float', unit: '秒' },
      { key: 'query_timeout_ms', label: '查询超时', type: 'int', unit: 'ms', hint: '默认 30000' },
    ],
  },
  {
    section: 'schema_sync', title: 'C7 样例值采集', desc: 'Schema 同步时的列样例值采集（注入提示词帮助 LLM 使用枚举值）',
    fields: [
      { key: 'schema_sync_collect_samples', label: '启用样例值采集', type: 'bool' },
      { key: 'schema_sync_sample_per_column', label: '每列样例数', type: 'int' },
      { key: 'schema_sync_sample_min_rows', label: '最小行数门槛', type: 'int', unit: '行', hint: '低于该行数不采集（全表枚举，无需样例）' },
      { key: 'schema_sync_sample_timeout_s', label: '单列采集超时', type: 'float', unit: '秒' },
    ],
  },
  {
    section: 'eval', title: 'C4 评测', desc: '评测批次执行模式',
    fields: [
      { key: 'eval_mock_execute', label: '默认 Mock 执行', type: 'bool', hint: '开启时不实连业务库执行' },
    ],
  },
]

export default function SysConfigPage() {
  const { msgApi, ctx, toastError } = useMessageApi()
  const [sections, setSections] = useState<Record<string, Record<string, unknown>>>({})
  const [overridden, setOverridden] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState('')
  // 必须在组件顶层调用 Hook，不能包在 useState 初始化器里
  const forms = SECTIONS_META.map(() => Form.useForm()[0])

  const load = async () => {
    setLoading(true)
    try {
      const r = await client.get<SystemConfigResp>('/system-config')
      setSections(r.data.sections)
      setOverridden(r.data.overridden)
    } catch (e) { toastError(e) } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const save = async (idx: number, meta: typeof SECTIONS_META[number]) => {
    const form = forms[idx]
    const v = await form.validateFields().catch(() => null)
    if (!v) return
    setSaving(meta.section)
    try {
      const r = await client.put<SystemConfigResp>('/system-config', { sections: { [meta.section]: v } })
      setSections(r.data.sections)
      setOverridden(r.data.overridden)
      msgApi.success(`「${meta.title}」配置已保存并即时生效`)
    } catch (e) { toastError(e) } finally { setSaving('') }
  }

  const renderField = (f: FieldMeta, value: unknown, overriddenSet: Set<string>) => (
    <Form.Item
      key={f.key}
      name={f.key}
      label={(
        <Space size={4}>
          <span>{f.label}</span>
          {f.unit ? <Typography.Text type="secondary" style={{ fontSize: 11 }}>（{f.unit}）</Typography.Text> : null}
          {overriddenSet.has(f.key) ? <Tag color="orange" style={{ marginInlineEnd: 0, fontSize: 10 }}>已覆盖</Tag> : null}
        </Space>
      )}
      valuePropName={f.type === 'bool' ? 'checked' : 'value'}
      extra={f.hint}
      style={{ marginBottom: 12 }}
    >
      {f.type === 'bool'
        ? <Switch />
        : (
          <InputNumber
            style={{ width: 180 }}
            step={f.type === 'float' ? 0.1 : 1}
            precision={f.type === 'float' ? 1 : 0}
            min={0}
          />
        )}
    </Form.Item>
  )

  return (
    <div className="glass-page">
      <PageHeader
        title="系统配置"
        description="能力参数统一管理：修改保存到元数据库（platform 级），引擎即时生效；恢复默认 = 清空字段后保存。当前覆盖项 {overridden.length} 个"
        extra={<Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>}
      />
      {ctx}
      <Row gutter={[16, 16]}>
        {SECTIONS_META.map((meta, idx) => {
          const values = sections[meta.section] || {}
          const overriddenSet = new Set(overridden)
          return (
            <Col xs={24} lg={12} key={meta.section}>
              <Card
                size="small"
                title={<Space>{meta.title}<Typography.Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>{meta.desc}</Typography.Text></Space>}
                extra={(
                  <Button type="primary" size="small" icon={<SaveOutlined />} loading={saving === meta.section} onClick={() => save(idx, meta)}>
                    保存
                  </Button>
                )}
                loading={loading}
              >
                <Form
                  form={forms[idx]}
                  layout="vertical"
                  initialValues={values}
                  key={meta.section}
                >
                  {meta.fields.map((f) => renderField(f, values[f.key], overriddenSet))}
                </Form>
              </Card>
            </Col>
          )
        })}
      </Row>
    </div>
  )
}
