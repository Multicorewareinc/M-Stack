/** Creates/updates/removes a `<style id="...">` tag in `<head>` — the shared mechanism behind
 * every runtime theme override (accentColor.ts, themeSurface.ts). Passing `null` removes any
 * previous override for that id, restoring the shipped default. */
export function setOverrideStyle(id: string, css: string | null): void {
  if (typeof document === 'undefined') return;
  const existing = document.getElementById(id) as HTMLStyleElement | null;
  if (!css) {
    existing?.remove();
    return;
  }
  const style = existing ?? document.createElement('style');
  style.id = id;
  style.textContent = css;
  if (!existing) document.head.appendChild(style);
}
