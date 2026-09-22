import { Toast, type ToastVariant } from '@multistack/ui';
import * as React from 'react';

interface ToastEntry {
  id: number;
  variant: ToastVariant;
  title: string;
  description?: string;
}

interface ToastContextValue {
  success: (title: string, description?: string) => void;
  error: (title: string, description?: string) => void;
}

const noop = () => {};
// Default is a silent no-op, not a thrown error: components that call useToast()
// should work standalone in tests/Storybook without mounting a full app tree.
const ToastContext = React.createContext<ToastContextValue>({ success: noop, error: noop });

const AUTO_DISMISS_MS = 4000;

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = React.useState<ToastEntry[]>([]);
  const nextId = React.useRef(0);

  const push = React.useCallback((variant: ToastVariant, title: string, description?: string) => {
    const id = nextId.current++;
    setToasts((prev) => [...prev, { id, variant, title, description }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, AUTO_DISMISS_MS);
  }, []);

  const value = React.useMemo<ToastContextValue>(
    () => ({
      success: (title, description) => push('success', title, description),
      error: (title, description) => push('error', title, description),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="fixed bottom-4 right-4 z-toast flex flex-col gap-2">
        {toasts.map((t) => (
          <Toast key={t.id} variant={t.variant} title={t.title} description={t.description} />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  return React.useContext(ToastContext);
}
