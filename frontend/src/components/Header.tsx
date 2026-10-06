import { Menu, ArrowLeft, MessageSquare, Brain, Settings, Sun, Moon, MoreVertical, Folder } from 'lucide-react';
import { useStore } from '../store';
import { api } from '../api';
import { useQuery } from '@tanstack/react-query';
import { ProviderIndicators } from './ProviderIndicators';
import { useTheme } from '../hooks/useTheme';
import { useIsMobile } from '../hooks/useIsMobile';
import { useState } from 'react';
import { UserMenu } from './UserMenu';

export const Header = () => {
  const {
    setModels,
    activeProject, setActiveProject, setActiveMainTab, projects,
    setShowSettingsModal,
    setSettingsTab,
    sidebarCollapsed, toggleSidebar,
    toggleRightPanel, openRightPanel,
    setMobileSidebarOpen,
    setMemActiveTab,
  } = useStore();

  const { theme, toggle } = useTheme();
  const isMobile = useIsMobile();
  const [menuOpen, setMenuOpen] = useState(false);

  const currentProject = projects.find(p => p.id === activeProject);

  useQuery({
    queryKey: ['models'],
    queryFn: async () => { const m = await api.models.list(); setModels(m); return m; },
    staleTime: 30000,
  });

  const handleGoHome = () => {
    setActiveProject(null);
    setActiveMainTab('dashboard');
    openRightPanel(null);
  };

  return (
    <header className="app-header flex items-center justify-between px-4 py-1.5 bg-ink text-white h-[44px] shrink-0 z-header">
      <div className="header-left flex items-center gap-3">
        {activeProject && (
          <button
            data-tip={sidebarCollapsed ? 'Expand the sidebar' : 'Collapse the sidebar'}
            className="hamburger-btn flex items-center justify-center w-[28px] h-[28px] shrink-0 border-0 bg-transparent text-white text-lg cursor-pointer leading-none p-0"
            onClick={() => (isMobile ? setMobileSidebarOpen(true) : toggleSidebar())}
            aria-label="Toggle sidebar"
          >
            <Menu size={18} />
          </button>
        )}
        {/* Same logo as the marketing site: anchor ring + "cor•dée" with its phonetics underneath.
            Pixel sizes on purpose: this app redefines spacing 1/9 as 1px/9px in tailwind.config.js. */}
        <h1 className="header-title m-0 cursor-pointer flex items-center gap-2.5" onClick={handleGoHome}>
          <span aria-hidden="true" className="grid h-[36px] w-[36px] shrink-0 place-items-center rounded-full border-[3px] border-accent"><span className="h-2.5 w-2.5 rounded-full bg-accent" /></span>
          <span aria-hidden="true" className="inline-flex flex-col leading-none">
            <span className="flex items-baseline gap-[3px] font-heading font-normal text-[22px] tracking-[-0.01em] text-white">
              <span>cor</span>
              <span className="mt-[3px] h-[5px] w-[5px] self-center rounded-full bg-accent" />
              <span>dée</span>
            </span>
            <span className="mt-[4px] font-mono text-[11px] font-medium tracking-[0.02em] text-accent-dark">/kɔʁ.de/</span>
          </span>
          <span className="sr-only">Cordée</span>
        </h1>
        {activeProject && (
          <>
            <button data-tip="Go back to the home dashboard" className="header-back-btn px-2 py-0.5 border border-white/20 dark:border-white/30 rounded-sm bg-transparent text-white text-sm+ cursor-pointer" onClick={handleGoHome}>
              <ArrowLeft size={14} />
              <span>Home</span>
            </button>
            <span className="header-project-name text-base font-bold uppercase tracking-[-0.01em] text-white max-w-[300px] overflow-hidden text-ellipsis whitespace-nowrap">{currentProject?.name || `Project #${activeProject}`}</span>
          </>
        )}
      </div>

      <div className="header-center flex gap-1.5">
        {isMobile ? (
          <div className="relative">
            <button
              data-tip="Open the navigation menu"
              className="header-btn px-3.5 py-1 border border-white/20 dark:border-white/30 rounded bg-transparent text-white text-md- cursor-pointer"
              onClick={() => setMenuOpen((v) => !v)}
              aria-label="Open menu"
            >
              <MoreVertical size={16} />
            </button>
            {menuOpen && (
              <>
                <div className="fixed inset-0 z-[1400]" onClick={() => setMenuOpen(false)} />
                <div className="absolute right-0 top-full mt-1 w-[200px] bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-md shadow-medium z-[1450] overflow-hidden">
                  {activeProject && (
                    <>
                      <button data-tip="Open the chats panel" className="header-menu-item w-full text-left px-3 py-2.5 text-sm+ text-ink dark:text-text-dark-DEFAULT hover:bg-accent-soft dark:hover:bg-accent-dark-soft flex items-center gap-2" onClick={() => { toggleRightPanel('chats'); setMenuOpen(false); }}><MessageSquare size={15} /> Chats</button>
                      <button data-tip="Open the Guide panel" className="header-menu-item w-full text-left px-3 py-2.5 text-sm+ bg-accent text-white hover:bg-accent-strong font-heading font-normal flex items-center gap-2" onClick={() => { toggleRightPanel('aingel'); setMenuOpen(false); }}>
                        <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="12" fill="#fbf6ee"/><circle cx="12" cy="12" r="7.5" fill="none" stroke="#c67139" strokeWidth="3"/><circle cx="12" cy="12" r="2.4" fill="#c67139"/></svg>
                        Guide
                      </button>
                      <button data-tip="Open the memory panel" className="header-menu-item w-full text-left px-3 py-2.5 text-sm+ text-ink dark:text-text-dark-DEFAULT hover:bg-accent-soft dark:hover:bg-accent-dark-soft flex items-center gap-2" onClick={() => { toggleRightPanel('memory'); setMenuOpen(false); }}><Brain size={15} /> Memory</button>
                      <button data-tip="Open the file manager" className="header-menu-item w-full text-left px-3 py-2.5 text-sm+ text-ink dark:text-text-dark-DEFAULT hover:bg-accent-soft dark:hover:bg-accent-dark-soft flex items-center gap-2" onClick={() => { setMemActiveTab('files'); openRightPanel('memory'); setMenuOpen(false); }}><Folder size={15} /> Files</button>
                    </>
                  )}
                  <button data-tip="Open the settings" className="header-menu-item w-full text-left px-3 py-2.5 text-sm+ text-ink dark:text-text-dark-DEFAULT hover:bg-accent-soft dark:hover:bg-accent-dark-soft flex items-center gap-2" onClick={() => { setSettingsTab('general'); setShowSettingsModal(true); setMenuOpen(false); }}><Settings size={15} /> Settings</button>
                  <button data-tip={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'} className="header-menu-item w-full text-left px-3 py-2.5 text-sm+ text-ink dark:text-text-dark-DEFAULT hover:bg-accent-soft dark:hover:bg-accent-dark-soft flex items-center gap-2" onClick={() => { toggle(); setMenuOpen(false); }}>{theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />} {theme === 'dark' ? 'Light mode' : 'Dark mode'}</button>
                  <div className="px-3 py-2 border-t border-border-muted dark:border-border-dark-muted">
                    <ProviderIndicators />
                  </div>
                  <div className="px-3 py-2 border-t border-border-muted dark:border-border-dark-muted">
                    <UserMenu />
                  </div>
                </div>
              </>
            )}
          </div>
        ) : (
          <>
            {activeProject ? (
              <>
                <button data-tip="Open the chats panel" className="header-btn px-3.5 py-1 border border-white/20 dark:border-white/30 rounded bg-transparent text-white text-md- cursor-pointer" onClick={() => toggleRightPanel('chats')}><MessageSquare size={14} /> Chats</button>
                <button data-tip="Open the Guide panel" className="header-btn header-btn-primary px-3.5 py-1 border rounded text-white text-md- font-heading font-normal cursor-pointer" onClick={() => toggleRightPanel('aingel')}>
                  <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true" style={{ opacity: 1 }}><circle cx="12" cy="12" r="12" fill="#fbf6ee"/><circle cx="12" cy="12" r="7.5" fill="none" stroke="#c67139" strokeWidth="3"/><circle cx="12" cy="12" r="2.4" fill="#c67139"/></svg>
                  Guide
                </button>
                <button data-tip="Open the memory panel" className="header-btn px-3.5 py-1 border border-white/20 dark:border-white/30 rounded bg-transparent text-white text-md- cursor-pointer" onClick={() => toggleRightPanel('memory')}><Brain size={14} /> Memory</button>
                <button data-tip="Open the file manager" className="header-btn px-3.5 py-1 border border-white/20 dark:border-white/30 rounded bg-transparent text-white text-md- cursor-pointer" onClick={() => { setMemActiveTab('files'); openRightPanel('memory'); }}><Folder size={14} /> Files</button>
              </>
            ) : null}
            <button data-tip="Open the settings" className="header-btn px-3.5 py-1 border border-white/20 dark:border-white/30 rounded bg-transparent text-white text-md- cursor-pointer" onClick={() => { setSettingsTab('general'); setShowSettingsModal(true); }}><Settings size={14} /> Settings</button>
            <button
              data-tip={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
              className="header-btn px-3.5 py-1 border border-white/20 dark:border-white/30 rounded bg-transparent text-white text-md- cursor-pointer"
              onClick={toggle}
              aria-label="Toggle dark mode"
            >
              {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
            </button>
            <ProviderIndicators />
            <UserMenu />
          </>
        )}
      </div>

    </header>
  );
};

export default Header;