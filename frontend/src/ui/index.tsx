// ============ 统一 UI 规范（全站唯一入口） ============
// 所有下拉框 / 按钮 / 输入框等组件的风格在此处统一定义：
//   1. glassTheme   —— 传给 ConfigProvider，antd 组件级 token 全站生效
//   2. GlassSelect  —— 统一玻璃下拉框（所有 Select 请改用本组件）
//   3. PrimaryButton—— 统一主按钮（渐变蓝 + 圆角 + 统一高度）
// 视觉细节（毛玻璃/光斑）见 src/global.css
import { Button, Select, Space } from 'antd'
import type { SelectProps } from 'antd'
import type { CSSProperties, ComponentProps, ReactNode } from 'react'

/** 统一主题：传给 ConfigProvider，组件级 token 自动应用到全站 */
export const glassTheme = {
  token: {
    colorPrimary: '#1677ff',
    colorInfo: '#1677ff',
    borderRadius: 10,
    fontSize: 14,
    controlHeight: 36,
    boxShadow: '0 8px 32px rgba(31,58,147,.1)',
  },
  components: {
    Layout: { siderBg: 'transparent', headerBg: 'transparent' },
    Button: {
      controlHeight: 36,
      borderRadius: 10,
      fontWeight: 500,
      primaryShadow: '0 6px 18px rgba(22,119,255,.32)',
      defaultBg: 'rgba(255,255,255,.6)',
      defaultBorderColor: 'rgba(255,255,255,.85)',
    },
    Select: {
      controlHeight: 36,
      borderRadius: 10,
      optionSelectedBg: 'rgba(22,119,255,.14)',
      optionActiveBg: 'rgba(22,119,255,.07)',
      selectorBg: 'rgba(255,255,255,.6)',
      activeBorderColor: '#1677ff',
      hoverBorderColor: '#69b1ff',
    },
    Input: { controlHeight: 36, borderRadius: 10, activeBorderColor: '#1677ff', hoverBorderColor: '#69b1ff' },
    InputNumber: { borderRadius: 10 },
    DatePicker: { borderRadius: 10 },
    Table: {
      headerBg: 'rgba(255,255,255,.5)',
      headerColor: 'rgba(0,0,0,.88)',
      rowHoverBg: 'rgba(22,119,255,.05)',
      cellPaddingBlock: 10,
    },
    Modal: { borderRadiusLG: 18 },
    Drawer: { colorBgElevated: 'rgba(255,255,255,.85)' },
  },
} as const

/** 统一玻璃下拉框：所有 Select 统一走这里（泛型透传保持 onChange 类型） */
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

/** 统一主按钮：渐变蓝、圆角、统一高度 */
export function PrimaryButton({ style, children, ...rest }: ComponentProps<typeof Button>) {
  return (
    <Button
      type="primary"
      {...rest}
      style={{ height: 40, borderRadius: 10, fontWeight: 500, ...style }}
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
      style={{ height: 36, borderRadius: 10, background: 'rgba(255,255,255,.6)', ...style }}
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
              ? { background: '#1677ff', color: '#fff', borderColor: '#1677ff' }
              : undefined}
          >
            {item.label}
          </DefaultButton>
        )
      })}
    </Space>
  )
}
