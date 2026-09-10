// 前端类型定义（与后端 API 契约对齐）

/** 后端分页列表统一返回结构（页面公共化：page/size + total/items） */
export interface PageResult<T> {
  total: number
  items: T[]
}

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
  scope_type: 'role' | 'user' | 'group'
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
  // C3 行级权限扩展
  row_filter?: string | null
  row_filter_type?: 'sql' | 'template'
  row_filter_note?: string
  row_enabled?: boolean
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

// ==================== 能力补建（C1/C3/C4/C7/C8/C14）类型 ====================

/** C14 场景模板 */
export interface SceneDef {
  id: number
  workspace_id: number | null
  scene_code: string
  scene_name: string
  description: string
  metric_pack: Array<{ name?: string; metric?: string; desc?: string }>
  gen_prompt_template: string
  explain_template: string
  examples: Array<{ question?: string; sql?: string; note?: string }>
  report_template: string
  enabled: boolean
  sort_order: number
  system: boolean
}

/** C1 缓存统计 */
export interface CacheStats {
  total: number
  by_type: Record<string, number>
  total_hits: number
  recent: Array<{
    id: number
    cache_type: string
    cache_key: string
    question: string
    sql_text: string
    datasource_id: number
    hit_count: number
    last_hit_at: string | null
    expires_at: string | null
  }>
}

/** C4 评测用例 */
export interface EvalCase {
  id: number
  datasource_id: number
  datasource_name?: string
  question: string
  expect_tables: string[]
  expect_metrics: string[]
  expect_filters: string[]
  expect_sql: string
  scene_code: string
  tags: string
  status: string
}

/** C4 评测批次 */
export interface EvalRun {
  id: number
  name: string
  status: string
  total: number
  metrics: Record<string, number | null>
  started_at: string | null
  finished_at: string | null
  mock_execute: boolean
}

/** C4 评测报告明细 */
export interface EvalResultItem {
  case_id: number
  question: string
  intent_ok: boolean | null
  tables_hit: boolean | null
  sql_generated: boolean
  sql_executable: boolean | null
  sql_correct: boolean | null
  e2e_ok: boolean | null
  latency_ms: number | null
  llm_used: string
  error_msg: string
  detail: Record<string, unknown>
}

/** 系统配置分组（键为 config 字段名） */
export interface SystemConfigResp {
  sections: Record<string, Record<string, unknown>>
  overridden: string[]
}
