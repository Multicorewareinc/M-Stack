import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { BrandLogo } from './BrandLogo';

describe('BrandLogo', () => {
  it('renders a text-only wordmark when no logoUrl is given', () => {
    const { container } = render(<BrandLogo name="Acme Platform" />);
    expect(screen.getByText('Acme Platform')).toBeInTheDocument();
    // Must fail if an <img> renders anyway with no logoUrl.
    expect(container.querySelector('img')).toBeNull();
  });

  it('renders the logo image alongside the name when logoUrl is given', () => {
    // alt="" is deliberate (the adjacent name text already conveys this, so the image is
    // decorative to assistive tech) — that's also why this queries the DOM directly rather than
    // getByRole('img'), which alt="" removes from the accessibility tree by design.
    const { container } = render(<BrandLogo name="Acme Platform" logoUrl="/logo.svg" />);
    expect(screen.getByText('Acme Platform')).toBeInTheDocument();
    const img = container.querySelector('img');
    expect(img).toHaveAttribute('src', '/logo.svg');
  });
});
