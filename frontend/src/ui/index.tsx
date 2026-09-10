// ============ 统一 UI 规范（全站唯一入口） ============
// 海外版后台管理风格（参考设计稿 QueryAI · 数据控制台）：
//   紫靛蓝主色 #6C5CE7 + 橙色伙伴色 #F97316 · 浅灰中性阶 · 白底卡片 · 柔和分层阴影
//   1. glassTheme   —— 传给 ConfigProvider，antd 组件级 token 全站生效
//   2. GlassSelect  —— 统一下拉框（所有 Select 请改用本组件）
//   3. PrimaryButton—— 统一主按钮（紫靛主色 + 圆角 + 统一高度）
// 视觉细节见 src/global.css
import { Button, Select, Space } from 'antd'
import type { SelectProps } from 'antd'
import type { CSSProperties, ComponentProps, ReactNode } from 'react'

/** 统一主题：传给 ConfigProvider，组件级 token 自动应用到全站 */
export const glassTheme = {
  token: {
    colorPrimary: '#6C5CE7',
    colorInfo: '#6C5CE7',
    colorLink: '#6C5CE7',
    colorSuccess: '#10B981',
    colorWarning: '#F59E0B',
    colorError: '#EF4444',
    borderRadius: 8,
    fontSize: 14,
    controlHeight: 36,
    colorBgLayout: '#F5F6F8',
    colorText: '#3D4252',
    colorTextHeading: '#1A1D29',
    colorTextSecondary: '#8B90A0',
    colorBorder: 'rgba(30,35,60,.14)',
    colorBorderSecondary: 'rgba(30,35,60,.08)',
    boxShadow: '0 1px 2px rgba(15,35,34,.04), 0 6px 16px -4px rgba(15,35,34,.06), 0 20px 40px -16px rgba(15,35,34,.10)',
    boxShadowSecondary: '0 2px 8px rgba(15,35,34,.08), 0 24px 56px -12px rgba(15,35,34,.22)',
    fontFamily: `"Inter","SF Pro Display",-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif`,
  },
  components: {
    Layout: { siderBg: '#ffffff', headerBg: '#ffffff', bodyBg: '#F5F6F8' },
    Button: {
      controlHeight: 36,
      borderRadius: 8,
      fontWeight: 500,
      primaryShadow: '0 2px 8px rgba(108,92,231,.30)',
      defaultBg: '#ffffff',
      defaultBorderColor: 'rgba(30,35,60,.14)',
      defaultColor: '#3D4252',
      defaultHoverBorderColor: '#6C5CE7',
      defaultHoverColor: '#6C5CE7',
    },
    Select: {
      controlHeight: 36,
      borderRadius: 8,
      optionSelectedBg: 'rgba(108,92,231,.12)',
      optionActiveBg: 'rgba(108,92,231,.06)',
      selectorBg: '#ffffff',
      activeBorderColor: '#6C5CE7',
      hoverBorderColor: '#A78BFA',
    },
    Input: { controlHeight: 36, borderRadius: 8, activeBorderColor: '#6C5CE7', hoverBorderColor: '#A78BFA', colorBgContainer: '#ffffff' },
    InputNumber: { borderRadius: 8, colorBgContainer: '#ffffff' },
    DatePicker: { borderRadius: 8, colorBgContainer: '#ffffff' },
    Table: {
      headerBg: '#F8F9FB',
      headerColor: '#3D4252',
      headerSplitColor: 'transparent',
      rowHoverBg: 'rgba(108,92,231,.05)',
      cellPaddingBlock: 12,
      borderColor: 'rgba(30,35,60,.08)',
      headerBorderRadius: 10,
    },
    Modal: { borderRadiusLG: 16, colorBgElevated: '#ffffff' },
    Drawer: { colorBgElevated: '#ffffff' },
    Card: { borderRadiusLG: 12, colorBgContainer: '#ffffff' },
    Menu: { itemBorderRadius: 8, itemSelectedBg: 'rgba(108,92,231,.10)', itemSelectedColor: '#6C5CE7' },
    Tabs: { itemSelectedColor: '#6C5CE7', inkBarColor: '#6C5CE7' },
    Segmented: { itemSelectedBg: '#ffffff', trackBg: '#F8F9FB' },
  },
} as const

/** 统一下拉框：所有 Select 统一走这里（泛型透传保持 onChange 类型） */
export function GlassSelect<ValueType, OptionType extends object = never>(
  props: SelectProps<ValueType, OptionType>,
) {
  return (
    <Select<ValueType, OptionType>
      {...props}
      popupMatchSelectWidth={false}
      className={props.className ? `glass-select ${props.className}` : 'glass-select'}
    />
  )
}

/** 统一主按钮：紫靛主色、圆角、统一高度 */
export function PrimaryButton({ style, children, ...rest }: ComponentProps<typeof Button>) {
  return (
    <Button
      type="primary"
      {...rest}
      style={{ height: 40, borderRadius: 8, fontWeight: 500, ...style }}
    >
      {children}
    </Button>
  )
}

/** 统一默认按钮（次要操作） */
export function DefaultButton({ style, children, ...rest }: ComponentProps<typeof Button>) {
  return (
    <Button
      {...rest}
      style={{ height: 36, borderRadius: 8, background: '#ffffff', ...style }}
    >
      {children}
    </Button>
  )
}

// ---------- 公共 Tab 切换 ----------
export interface TabSwitchItem<T extends string = string> {
  key: T
  label: ReactNode
}
export interface TabSwitchProps<T extends string = string> {
  items: TabSwitchItem<T>[]
  value: T
  onChange: (key: T) => void
  style?: CSSProperties
  size?: ComponentProps<typeof Button>['size']
}

/**
 * 公共 Tab 切换按钮组。
 * 统一解决「DefaultButton + type=primary」选中态文字变白不可见的问题；
 * 各页面分配抽屉（用户/角色/用户组/菜单等）统一使用本组件。
 */
export function TabSwitch<T extends string>({ items, value, onChange, style, size }: TabSwitchProps<T>) {
  return (
    <Space style={{ marginBottom: 12, ...style }}>
      {items.map((item) => {
        const active = item.key === value
        return (
          <DefaultButton
            key={item.key}
            size={size}
            onClick={() => onChange(item.key)}
            style={active
              ? { background: '#6C5CE7', color: '#fff', borderColor: '#6C5CE7', boxShadow: '0 2px 8px rgba(108,92,231,.30)' }
              : undefined}
          >
            {item.label}
          </DefaultButton>
        )
      })}
    </Space>
  )
}
