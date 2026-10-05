import { useStore, type MainTab } from '../store';
import { cn } from '../utils/cn';
import { LayoutDashboard, KanbanSquare, GitBranch, MessageSquare } from 'lucide-react';

const TABS: { key: MainTab; label: string; icon: typeof LayoutDashboard }[] = [
  { key: 'dashboard', label: 'Overview', icon: LayoutDashboard },
  { key: 'board', label: 'Project Board', icon: KanbanSquare },
  { key: 'graph', label: 'Graph', icon: GitBranch },
];

export const MobileBottomNav = () => {
  const { activeMainTab, setActiveMainTab, chatsPanelOpen, setChatsPanelOpen } = useStore();

  return (
    <nav className="mobile-bottom-nav fixed bottom-0 left-0 right-0 z-[100] bg-surface-raised dark:bg-surface-dark-raised border-t border-border-muted dark:border-border-dark-muted flex">
      {TABS.map((t) => {
        const Icon = t.icon;
        const active = activeMainTab === t.key;
        return (
          <button
            key={t.key}
            data-tip={`Show the ${t.label} view`}
            className={cn(
              'mobile-bottom-nav-item flex-1 flex flex-col items-center justify-center gap-0.5 py-2 border-0 bg-transparent cursor-pointer text-text-muted dark:text-text-dark-muted',
              active && 'text-accent dark:text-accent-dark-DEFAULT',
            )}
            onClick={() => setActiveMainTab(t.key)}
            aria-current={active ? 'page' : undefined}
          >
            <Icon size={20} />
            <span className="text-[10px] leading-none">{t.label}</span>
          </button>
        );
      })}
      <button
        data-tip={chatsPanelOpen ? 'Close the chats panel' : 'Open the chats panel'}
        className={cn(
          'mobile-bottom-nav-item flex-1 flex flex-col items-center justify-center gap-0.5 py-2 border-0 bg-transparent cursor-pointer text-text-muted dark:text-text-dark-muted',
          chatsPanelOpen && 'text-accent dark:text-accent-dark-DEFAULT',
        )}
        onClick={() => setChatsPanelOpen(!chatsPanelOpen)}
        aria-current={chatsPanelOpen ? 'page' : undefined}
      >
        <MessageSquare size={20} />
        <span className="text-[10px] leading-none">Chats</span>
      </button>
    </nav>
  );
};

export default MobileBottomNav;
