import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Button } from './inputs';
import { PageHeader } from './layout';

describe('PageHeader', () => {
  it('renders title, description, actions slot', () => {
    render(
      <PageHeader
        title="Organizations"
        description="Manage organizations and their platform entitlements."
        actions={<Button>Create Organization</Button>}
      />,
    );
    expect(screen.getByRole('heading', { name: 'Organizations' })).toBeInTheDocument();
    expect(screen.getByText(/manage organizations/i)).toBeInTheDocument();
    // Must fail if the actions slot is dropped.
    expect(screen.getByRole('button', { name: 'Create Organization' })).toBeInTheDocument();
  });
});
