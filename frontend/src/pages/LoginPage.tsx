// 登录页
import { useState } from 'react'
import { Button, Card, Form, Input, Typography } from 'antd'
import { LockOutlined, UserOutlined } from '@ant-design/icons'
import { client, TOKEN_KEY } from '../api/client'
import { useMessageApi } from '../hooks/useMessageApi'

export default function LoginPage({ onLogin }: { onLogin: () => void }) {
  const { msgApi, ctx } = useMessageApi()
  const [loading, setLoading] = useState(false)

  const submit = async (values: { username: string; password: string }) => {
    setLoading(true)
    try {
      const r = await client.post('/auth/login', values)
      localStorage.setItem(TOKEN_KEY, r.data.access_token)
      msgApi.success('登录成功')
      onLogin()
    } catch {
      msgApi.error('用户名或密码错误（默认 admin / admin123）')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="glass-app" style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      {ctx}
      <Card className="glass-login-card" style={{ width: 400, padding: 8 }}>
        <div style={{ textAlign: 'center', marginBottom: 8 }}>
          <div style={{
            width: 52, height: 52, borderRadius: 16, margin: '0 auto 12px',
            background: 'linear-gradient(135deg,#6C5CE7 0%,#A78BFA 100%)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: '#fff', fontWeight: 700, fontSize: 24,
            boxShadow: '0 10px 30px rgba(108,92,231,.4)',
          }}>
            Q
          </div>
          <Typography.Title level={3} style={{ marginBottom: 4 }}>AI 问数系统</Typography.Title>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            NL2SQL · RAG · 字段级权限 · 定时任务
          </Typography.Paragraph>
        </div>
        <Form onFinish={submit} initialValues={{ username: 'admin', password: 'admin123' }}>
          <Form.Item name="username" rules={[{ required: true, message: '请输入用户名' }]}>
            <Input prefix={<UserOutlined />} placeholder="用户名" />
          </Form.Item>
          <Form.Item name="password" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password prefix={<LockOutlined />} placeholder="密码" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={loading} style={{ height: 40, borderRadius: 10 }}>登录</Button>
        </Form>
      </Card>
    </div>
  )
}
