// 路由：登录态守卫
import { useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { ConfigProvider, App as AntdApp } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App'
import LoginPage from './pages/LoginPage'
import ChatPage from './pages/ChatPage'
import DatasourcePage from './pages/DatasourcePage'
import SchemaPage from './pages/SchemaPage'
import KnowledgePage from './pages/KnowledgePage'
import ModelPage from './pages/ModelPage'
import PermissionPage from './pages/PermissionPage'
import AuditPage from './pages/AuditPage'
import RolePage from './pages/RolePage'
import GroupPage from './pages/GroupPage'
import UserPage from './pages/UserPage'
import EvalPage from './pages/EvalPage'
import ScenesPage from './pages/ScenesPage'
import CachePage from './pages/CachePage'
import SysConfigPage from './pages/SysConfigPage'
import PromptPage from './pages/PromptPage'
import ReportCenterPage from './pages/ReportCenterPage'
import InsightPage from './pages/InsightPage'
import MenuConfigPage from './pages/MenuConfigPage'
import { TOKEN_KEY } from './api/client'
import { glassTheme } from './ui'

function Shell() {
  const [authed, setAuthed] = useState(() => Boolean(localStorage.getItem(TOKEN_KEY)))
  if (!authed) return <LoginPage onLogin={() => setAuthed(true)} />
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<App />}>
          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/datasources" element={<DatasourcePage />} />
          <Route path="/datasources/:id/schema" element={<SchemaPage />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
          <Route path="/models" element={<ModelPage />} />
          <Route path="/roles" element={<RolePage />} />
          <Route path="/groups" element={<GroupPage />} />
          <Route path="/users" element={<UserPage />} />
          <Route path="/permissions" element={<PermissionPage />} />
          <Route path="/eval" element={<EvalPage />} />
          <Route path="/scenes" element={<ScenesPage />} />
          <Route path="/cache" element={<CachePage />} />
          <Route path="/sys-config" element={<SysConfigPage />} />
          <Route path="/prompts" element={<PromptPage />} />
          <Route path="/reports" element={<ReportCenterPage />} />
          <Route path="/insight" element={<InsightPage />} />
          <Route path="/menu-config" element={<MenuConfigPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default function Root() {
  return (
    <ConfigProvider locale={zhCN} theme={glassTheme}>
      <AntdApp>
        <Shell />
      </AntdApp>
    </ConfigProvider>
  )
}
