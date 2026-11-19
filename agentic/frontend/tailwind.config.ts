import { multistackPreset } from '@multistack/ui/preset';
import type { Config } from 'tailwindcss';

export default {
  presets: [multistackPreset],
  content: [
    './index.html',
    './src/**/*.{ts,tsx}',
    '../../ui/packages/ui/src/**/*.{ts,tsx}',
  ],
} satisfies Config;
