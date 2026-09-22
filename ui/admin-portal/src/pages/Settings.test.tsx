import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Settings } from './Settings';

describe('Settings placeholder', () => {
  it('renders placeholder', () => {
    render(<Settings />);
    // Must fail if the route 404s or renders blank.
    expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument();
    expect(screen.getByText('Settings coming soon')).toBeInTheDocument();
  });
});
