import { useEffect } from 'react';
import { cn } from '../utils/cn';

export const MobileDrawer = ({
  open,
  onClose,
  children,
}: {
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
}) => {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  return (
    <>
      <div
        className={cn(
          'mobile-drawer-scrim fixed inset-0 bg-black/40 z-[1200] transition-opacity',
          open ? 'opacity-100' : 'opacity-0 pointer-events-none',
        )}
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        className={cn(
          'mobile-drawer fixed top-0 bottom-0 left-0 z-[1250] w-[280px] max-w-[85vw] bg-surface-panel dark:bg-surface-dark-panel shadow-side transition-transform',
          open ? 'translate-x-0' : '-translate-x-full',
        )}
        role="dialog"
        aria-modal="true"
        aria-label="Project navigation"
      >
        {children}
      </div>
    </>
  );
};

export default MobileDrawer;
