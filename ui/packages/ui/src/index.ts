// @multistack/ui — shared, business-agnostic design system.
// Presentation only: no routing, no network, no auth, no domain logic.

export { cn } from './styles/cn';
export {
  tokens,
  colors,
  lightColors,
  canvas,
  typography,
  spacing,
  radius,
  borders,
  shadows,
  lightShadows,
  zIndex,
  breakpoints,
  motion,
  type Tokens,
} from './tokens/index';
export { multistackPreset } from './tokens/preset';

// Theme (dark/light toggle)
export { ThemeProvider, useTheme, type Theme, type ThemeProviderProps } from './theme';
export { applyAccentColor, deriveAccentShades, type AccentShades } from './accentColor';
export {
  applyBackgroundColor,
  applySurfaceColor,
  applyTextColor,
  deriveCanvasColor,
  deriveSurfaceColor,
  deriveTextColor,
} from './themeSurface';
export { ThemeToggle } from './components/ThemeToggle';

// Inputs & forms
export {
  Button,
  IconButton,
  Label,
  Input,
  Textarea,
  Checkbox,
  Switch,
  Radio,
  RadioItem,
  FormField,
  FormSection,
  type ButtonProps,
  type IconButtonProps,
  type InputProps,
  type TextareaProps,
  type FormFieldProps,
  type FormSectionProps,
} from './components/inputs';

// Select / Combobox
export { Select, Combobox, type SelectOption, type SelectProps, type ComboboxProps } from './components/select';

// Status
export {
  Badge,
  StatusBadge,
  Spinner,
  Skeleton,
  STATUSES,
  type Status,
  type BadgeProps,
  type StatusBadgeProps,
} from './components/status';

// Overlays
export {
  Dialog,
  Drawer,
  ConfirmDialog,
  EntityDrawer,
  DetailDialog,
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  Tooltip,
  type DialogProps,
  type DrawerProps,
  type ConfirmDialogProps,
  type EntityDrawerProps,
  type DetailDialogProps,
  type TooltipProps,
} from './components/overlays';

// Feedback
export {
  Alert,
  Toast,
  EmptyState,
  type AlertProps,
  type ToastProps,
  type ToastVariant,
  type EmptyStateProps,
} from './components/feedback';

// Layout & navigation
export {
  PageHeader,
  Breadcrumbs,
  Tabs,
  TabsList,
  TabsTrigger,
  TabsContent,
  StatCard,
  Avatar,
  type PageHeaderProps,
  type Crumb,
  type BreadcrumbsProps,
  type StatCardProps,
  type AvatarProps,
} from './components/layout';
export { BrandLogo, type BrandLogoProps } from './components/BrandLogo';

// Table
export {
  Table,
  DataTable,
  Pagination,
  SearchInput,
  FilterBar,
  type Column,
  type RowAction,
  type SortDirection,
  type DataTableProps,
  type PaginationProps,
  type SearchInputProps,
} from './components/table';

// RBAC composition
export {
  PermissionMatrix,
  RoleSelector,
  type MatrixPermission,
  type PermissionMatrixProps,
  type SelectableRole,
  type RoleSelectorProps,
} from './components/rbac';
