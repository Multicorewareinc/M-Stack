import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { ThemeProvider, useTheme } from './theme';

function Probe() {
  const { theme, toggleTheme, setTheme } = useTheme();
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <button onClick={toggleTheme}>Toggle</button>
      <button onClick={() => setTheme('light')}>Set light</button>
    </div>
  );
}

beforeEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

describe('ThemeProvider / useTheme', () => {
  it('defaults to dark and stamps data-theme on <html>', () => {
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    );
    expect(screen.getByTestId('theme')).toHaveTextContent('dark');
    // Must fail if the provider doesn't apply the theme to the document root — that's what the
    // CSS variable overrides in globals.css key off.
    expect(document.documentElement.dataset.theme).toBe('dark');
  });

  it('toggles between dark and light, persisting to localStorage', async () => {
    const user = userEvent.setup();
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    );

    await user.click(screen.getByRole('button', { name: 'Toggle' }));
    expect(screen.getByTestId('theme')).toHaveTextContent('light');
    expect(document.documentElement.dataset.theme).toBe('light');
    // Must fail if the preference isn't persisted — a reload would silently lose it.
    expect(localStorage.getItem('multistack-theme')).toBe('light');

    await user.click(screen.getByRole('button', { name: 'Toggle' }));
    expect(screen.getByTestId('theme')).toHaveTextContent('dark');
  });

  it('restores a previously-stored theme on mount', () => {
    localStorage.setItem('multistack-theme', 'light');
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    );
    // Must fail if a stored preference is ignored in favor of defaultTheme.
    expect(screen.getByTestId('theme')).toHaveTextContent('light');
  });

  it('useTheme throws outside a ThemeProvider', () => {
    // Must fail if the hook silently returns a bogus default instead of a clear error.
    expect(() => render(<Probe />)).toThrow('useTheme must be used within a ThemeProvider');
  });
});
