/**
 * 卡片外壳：统一标题栏 + 操作按钮 + 内容区
 * 参考后台管理 Dashboard 风格：白底圆角、轻微阴影、标题完整不截断
 */
import React from 'react';
import { Card, Dropdown, Tooltip, Modal } from 'antd';
import { MoreOutlined, CopyOutlined, DownloadOutlined, EyeOutlined } from '@ant-design/icons';

interface CardWrapperProps {
  title: string;
  extra?: React.ReactNode;
  children: React.ReactNode;
  height?: number;
  sql?: string;
}

export const CardWrapper: React.FC<CardWrapperProps> = ({
  title, extra, children, height, sql,
}) => {
  const handleCopySql = () => {
    if (sql) navigator.clipboard.writeText(sql).catch(() => {});
  };

  const handleViewSql = () => {
    if (!sql) return;
    Modal.info({
      title: 'SQL 语句',
      width: 720,
      content: (
        <pre style={{
          background: '#f6f8fa',
          padding: 16,
          borderRadius: 8,
          fontSize: 12,
          lineHeight: 1.6,
          overflow: 'auto',
          maxHeight: 400,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-all',
        }}>{sql}</pre>
      ),
      okText: '关闭',
    });
  };

  const menuItems = [
    { key: 'view', icon: <EyeOutlined />, label: '查看SQL', onClick: handleViewSql, disabled: !sql },
    { key: 'copy', icon: <CopyOutlined />, label: '复制SQL', onClick: handleCopySql, disabled: !sql },
    { key: 'download', icon: <DownloadOutlined />, label: '导出数据' },
  ];

  return (
    <Card
      size="small"
      style={{
        height: height || '100%',
        display: 'flex',
        flexDirection: 'column',
        borderRadius: 12,
        boxShadow: '0 1px 2px rgba(15,35,34,.04), 0 6px 16px -4px rgba(15,35,34,.06), 0 20px 40px -16px rgba(15,35,34,.10)',
        border: '1px solid rgba(30,35,60,.08)',
        overflow: 'hidden',
      }}
      styles={{
        body: { flex: 1, overflow: 'hidden', padding: '16px 20px' },
        header: { padding: '14px 20px', borderBottom: '1px solid rgba(30,35,60,.08)' },
      }}
      title={
        <span style={{
          fontSize: 14,
          fontWeight: 600,
          color: '#1f1f1f',
          display: 'block',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }} title={title}>{title}</span>
      }
      extra={
        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          {extra}
          <Dropdown menu={{ items: menuItems }} trigger={['click']}>
            <MoreOutlined style={{ cursor: 'pointer', fontSize: 14, color: '#8c8c8c' }} />
          </Dropdown>
        </div>
      }
    >
      {children}
    </Card>
  );
};
