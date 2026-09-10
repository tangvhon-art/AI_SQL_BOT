/**
 * AI 解读组件：展示结构化解读结果 + 流式生成
 * 与 Dashboard 配合使用，支持保存为报告
 */
import React, { useState } from 'react';
import { Card, Tag, List, Button, Space, Tooltip, Spin, Empty, Collapse } from 'antd';
import {
  BulbOutlined, AlertOutlined, RiseOutlined,
  ThunderboltOutlined, FileTextOutlined, CopyOutlined,
} from '@ant-design/icons';
import type { InterpretationResult } from '../../types/chart';

interface AiInterpretationProps {
  /** 解读结果（流式时 partial） */
  result?: InterpretationResult | null;
  /** 是否正在生成 */
  loading?: boolean;
  /** 原始文本（流式拼接） */
  rawText?: string;
  /** 保存为报告回调 */
  onSaveReport?: () => void;
  /** 重新解读回调 */
  onRegenerate?: () => void;
  /** 使用的模板名称 */
  templateName?: string;
}

const severityColor: Record<string, string> = { high: 'red', medium: 'orange', low: 'blue' };

export const AiInterpretation: React.FC<AiInterpretationProps> = ({
  result, loading, rawText, onSaveReport, onRegenerate, templateName,
}) => {
  const [showRaw, setShowRaw] = useState(false);

  const handleCopy = () => {
    const text = result?.summary || rawText || '';
    navigator.clipboard.writeText(text).catch(() => {});
  };

  if (loading && !result) {
    return (
      <Card size="small" title={<span><ThunderboltOutlined style={{ marginRight: 6 }} />AI 解读</span>}>
        <div style={{ padding: 24, textAlign: 'center' }}>
          <Spin tip="正在生成解读…" />
        </div>
      </Card>
    );
  }

  if (!result && !rawText) {
    return (
      <Card size="small" title={<span><ThunderboltOutlined style={{ marginRight: 6 }} />AI 解读</span>}>
        <Empty description="暂无解读" />
      </Card>
    );
  }

  return (
    <Card
      size="small"
      title={
        <Space>
          <ThunderboltOutlined style={{ color: '#6C5CE7' }} />
          <span>AI 解读</span>
          {templateName && <Tag color="purple" style={{ margin: 0 }}>{templateName}</Tag>}
        </Space>
      }
      extra={
        <Space size={4}>
          <Tooltip title="复制">
            <Button size="small" type="text" icon={<CopyOutlined />} onClick={handleCopy} />
          </Tooltip>
          {onRegenerate && <Button size="small" type="text" onClick={onRegenerate}>重新解读</Button>}
          {onSaveReport && <Button size="small" type="primary" onClick={onSaveReport}>保存报告</Button>}
        </Space>
      }
    >
      {/* 总体结论 */}
      <div style={{ padding: '8px 12px', background: 'rgba(108,92,231,.07)', borderRadius: 6, marginBottom: 12, borderLeft: '3px solid #6C5CE7' }}>
        <div style={{ fontSize: 12, color: '#6C5CE7', marginBottom: 4, fontWeight: 500 }}>
          <FileTextOutlined style={{ marginRight: 4 }} />总体结论
        </div>
        <div style={{ fontSize: 13, lineHeight: 1.6, color: '#262626' }}>{result?.summary || rawText}</div>
      </div>

      {/* 关键指标 */}
      {(result?.keyMetrics?.length ?? 0) > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 6, fontWeight: 500 }}>
            <BulbOutlined style={{ marginRight: 4 }} />关键指标
          </div>
          <Space wrap size={[8, 8]}>
            {(result?.keyMetrics || []).map((m, i) => (
              <Tag key={i} color="blue" style={{ fontSize: 12, padding: '2px 8px' }}>
                {m.name}: <strong>{m.value}</strong>
                {m.change && <span style={{ color: m.change.startsWith('-') ? '#3f8600' : '#cf1322', marginLeft: 4 }}>{m.change}</span>}
              </Tag>
            ))}
          </Space>
        </div>
      )}

      {/* 趋势 + 对比 */}
      {((result?.trends?.length ?? 0) > 0 || (result?.comparisons?.length ?? 0) > 0) && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
          {(result?.trends?.length ?? 0) > 0 && (
            <div>
              <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 6, fontWeight: 500 }}>
                <RiseOutlined style={{ marginRight: 4 }} />趋势分析
              </div>
              <List size="small" dataSource={result?.trends || []} renderItem={(item) => (
                <List.Item style={{ padding: '2px 0', fontSize: 12 }}>• {item}</List.Item>
              )} />
            </div>
          )}
          {(result?.comparisons?.length ?? 0) > 0 && (
            <div>
              <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 6, fontWeight: 500 }}>
                <ThunderboltOutlined style={{ marginRight: 4 }} />对比发现
              </div>
              <List size="small" dataSource={result?.comparisons || []} renderItem={(item) => (
                <List.Item style={{ padding: '2px 0', fontSize: 12 }}>• {item}</List.Item>
              )} />
            </div>
          )}
        </div>
      )}

      {/* 异常点 */}
      {(result?.anomalies?.length ?? 0) > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 6, fontWeight: 500 }}>
            <AlertOutlined style={{ marginRight: 4 }} />异常/风险
          </div>
          <Space wrap size={[6, 6]}>
            {(result?.anomalies || []).map((a, i) => (
              <Tag key={i} color={severityColor[a.severity] || 'default'} style={{ fontSize: 12 }}>
                [{a.severity === 'high' ? '高' : a.severity === 'medium' ? '中' : '低'}] {a.desc}
              </Tag>
            ))}
          </Space>
        </div>
      )}

      {/* 行动建议 */}
      {(result?.suggestions?.length ?? 0) > 0 && (
        <div style={{ padding: '8px 12px', background: 'rgba(16,185,129,.07)', borderRadius: 6, borderLeft: '3px solid #10B981' }}>
          <div style={{ fontSize: 12, color: '#0A8F68', marginBottom: 4, fontWeight: 500 }}>
            <BulbOutlined style={{ marginRight: 4 }} />行动建议
          </div>
          <List size="small" dataSource={result?.suggestions || []} renderItem={(item, i) => (
            <List.Item style={{ padding: '2px 0', fontSize: 12 }}>{i + 1}. {item}</List.Item>
          )} />
        </div>
      )}

      {/* 原始文本折叠 */}
      {rawText && (
        <Collapse size="small" style={{ marginTop: 12 }} ghost activeKey={showRaw ? 'raw' : ''} onChange={() => setShowRaw(!showRaw)}>
          <Collapse.Panel header="查看原始文本" key="raw">
            <div style={{ fontSize: 12, color: '#595959', whiteSpace: 'pre-wrap', maxHeight: 200, overflow: 'auto' }}>{rawText}</div>
          </Collapse.Panel>
        </Collapse>
      )}
    </Card>
  );
};
