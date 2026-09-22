import { describe, expect, it } from 'vitest';
import * as UI from './index';

const DOCUMENTED = [
  'Button', 'IconButton', 'Input', 'Textarea', 'Select', 'Combobox', 'Checkbox', 'Switch', 'Radio',
  'Badge', 'StatusBadge', 'Avatar', 'Tooltip', 'DropdownMenu', 'Dialog', 'Drawer', 'Tabs', 'Table',
  'DataTable', 'Pagination', 'SearchInput', 'FilterBar', 'Breadcrumbs', 'EmptyState', 'Skeleton',
  'Spinner', 'Toast', 'Alert', 'ConfirmDialog', 'FormField', 'FormSection', 'PageHeader', 'StatCard',
  'PermissionMatrix', 'RoleSelector', 'EntityDrawer',
] as const;

describe('public API', () => {
  it('exports every documented component', () => {
    for (const name of DOCUMENTED) {
      // Must fail if any listed component is not exported.
      expect((UI as Record<string, unknown>)[name], `missing export: ${name}`).toBeDefined();
    }
  });

  it('exports tokens and cn', () => {
    expect(UI.tokens).toBeDefined();
    expect(typeof UI.cn).toBe('function');
  });
});
