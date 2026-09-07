// 前端类型定义（与后端 API 契约对齐）
export interface UserInfo {
  id: number
  username: string
  display_name: string
  role_code: string
  workspace_id: number
}

export interface Datasource {
  id: number
  workspace_id: number
  name: string
  type: string
  host: string
  port: number
  db_name: string
  user: string
  status: string
  last_sync_at?: string | null
  params_json?: Record<string, unknown>
}

export interface TableMeta {
  id: number
  schema: string
  table_name: string
  comment: string
  table_type: string
  column_count: number
  deprecated: boolean
}

export interface ColumnMeta {
  id: number
  column_name: string
  data_type: string
  comment: string
  is_nullable: boolean
  is_pk: boolean
  default_value?: string
}

export interface Relationship {
  id: number
  src_table: string
  src_col: string
  dst_table: string
  dst_col: string
  rel_type: string
  source: string
  enabled: boolean
}

export interface Faq {
  id: number
  question: string
  answer: string
  category: string
  tags: string
  datasource_id?: number | null
  table_ids: string
  sql_example: string
  synonyms_json: string[]
  enabled: boolean
}

export interface KnowledgeDoc {
  id: number
  name: string
  file_type: string
  size: number
  status: string
  error_msg: string
  version: number
  chunk_size: number
  overlap: number
}

export interface ModelItem {
  id: number
  name: string
  provider: string
  base_url: string
  model_name: string
  embedding_model: string
  temperature: number
  top_p: number
  max_tokens: number
  scene: string
  is_default: boolean
  has_key: boolean
}

export interface ChatMsg {
  id?: number
  role: 'user' | 'assistant'
  content_type: string
  content: Record<string, unknown>
  create_time?: string
}

export interface Conversation {
  id: number
  title: string
  create_time?: string
}

export interface SavedQuery {
  id: number
  name: string
  sql_text: string
  params: Array<{ key: string; type: string; default?: string; required?: boolean }>
  chart_config: Record<string, unknown>
  tags: string
  remark: string
  owner_id: number
}

export interface ScheduledTask {
  id: number
  saved_query_id: number
  saved_query_name?: string
  name: string
  cron_expr: string
  timezone: string
  param_values: Record<string, unknown>
  status: string
  last_run_at?: string | null
  next_run_at?: string | null
}

export interface PermissionRule {
  id?: number
  scope_type: 'role' | 'user'
  scope_id: number
  rule_type: 'allow' | 'deny'
  datasource_id: number
  table_id: number
  column_ids: number[]  // 空数组=整表规则
  enabled: boolean
  table_name?: string
  column_name?: string  // 兼容字段，逗号分隔的多字段名
  column_names?: string[]  // 多字段名数组
  datasource_name?: string
}

export interface QueryLog {
  id: number
  user_id: number
  conversation_id?: number | null
  question: string
  intent: string
  matched_tables: string
  generated_sql: string
  permission_injected: string
  executed: boolean
  row_count: number
  latency_ms: number
  chart_type: string
  feedback?: string | null
  create_time?: string
}

export interface MenuItem {
  id: number
  parent_id: number
  code: string
  name: string
  path: string
  icon: string
  sort_order: number
}
