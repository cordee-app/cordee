import { useState, useEffect, useCallback } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import type { MemoryView, Role, GitMode } from '../types';
import { cn } from '../utils/cn';
import { RightPanelResizer } from './RightPanelResizer';
import { useIsMobile } from '../hooks/useIsMobile';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import { FilesBrowser } from './FilesBrowser';
import { RefreshCw, X, Plus } from 'lucide-react';
import { fmtBytes, extIcon, memoryIcon, defIcon, NON_RENDERABLE_EXTS } from '../utils/file';

const MEM_TABS = [
  { key: 'files' as const, label: 'Files' },
  { key: 'defs' as const, label: 'Defs' },
  { key: 'artifacts' as const, label: 'Artifacts' },
  { key: 'docs' as const, label: 'Docs' },
  { key: 'perms' as const, label: 'Perms' },
  { key: 'git' as const, label: 'Git' },
  { key: 'roles' as const, label: 'Roles' },
  { key: 'spec' as const, label: 'Spec' },
];

export const MemoryPanel = () => {
  const {
    memPanelOpen, memActiveTab, setMemPanelOpen, setMemActiveTab,
    activeProject, projects, models,
    rightPanelWidth, executions,
  } = useStore();

  const isMobile = useIsMobile();
  const perms = useProjectPermissions(activeProject);

  const [data, setData] = useState<MemoryView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [newRoleName, setNewRoleName] = useState('');
  const [newRolePrompt, setNewRolePrompt] = useState('');
  const [newRoleModel, setNewRoleModel] = useState('');
  const [permTestCommand, setPermTestCommand] = useState('');
  const [permTestResult, setPermTestResult] = useState('');
  const [customRuleInput, setCustomRuleInput] = useState<Record<string, string>>({});
  const [specEditorOpen, setSpecEditorOpen] = useState(false);
  const [specEditorText, setSpecEditorText] = useState('');
  const [specEditorErr, setSpecEditorErr] = useState('');

  const load = useCallback(async () => {
    if (!activeProject) {
      setData(null);
      return;
    }
    setLoading(true);
    setError('');
    try {
      const d = await api.memory.get(activeProject);
      setData(d);
    } catch {
      setError('Failed to load memory.');
    } finally {
      setLoading(false);
    }
  }, [activeProject]);

  useEffect(() => {
    if (memPanelOpen) load();
  }, [memPanelOpen, load]);

  // Refresh when executions change (new files may have been written by a task)
  const execCount = executions.length;
  const lastExecStatus = executions[0]?.status;
  const lastExecMemStatus = executions[0]?.memory_status;
  useEffect(() => {
    if (memPanelOpen) load();
  }, [execCount, lastExecStatus, lastExecMemStatus, activeProject]);

  // Refresh when bucket files have been synced to Working Documents (Upload folder)
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as { projectId?: number };
      if (detail?.projectId && detail.projectId !== activeProject) return;
      if (memPanelOpen) load();
    };
    window.addEventListener('aingel:files-changed', handler);
    return () => window.removeEventListener('aingel:files-changed', handler);
  }, [memPanelOpen, activeProject, load]);

  const project = projects.find((p) => p.id === activeProject);
  const projectRoot = project?.path || '';

  const fileUrl = (absPath: string): string | null => {
    if (!activeProject || !absPath || !projectRoot) return null;
    const root = projectRoot.endsWith('/') ? projectRoot : projectRoot + '/';
    if (!absPath.startsWith(root)) return null;
    const rel = absPath.slice(root.length);
    const encoded = rel.split('/').map(encodeURIComponent).join('/');
    // Check if the file is non-renderable — use the preview endpoint
    const ext = rel.slice(rel.lastIndexOf('.')).toLowerCase();
    if (NON_RENDERABLE_EXTS.has(ext)) {
      return `/api/preview/${activeProject}/${encoded}`;
    }
    return `/files/${activeProject}/${encoded}`;
  };

  const handleRefreshPerms = async () => {
    if (!activeProject) return;
    try {
      await api.projects.permissions.refresh(activeProject);
      load();
    } catch { alert('Refresh failed'); }
  };

  const handleTogglePerm = async (rule: string, enabled: boolean) => {
    if (!activeProject) return;
    try {
      await api.projects.permissions.toggle(activeProject, { rule, enabled });
      load();
    } catch { /* ignore */ }
  };

  const handleAddCustomRule = async (groupKey: string) => {
    const input = customRuleInput[groupKey]?.trim();
    if (!input || !activeProject) return;
    try {
      await api.projects.permissions.addCustom(activeProject, { group_key: groupKey, rule: input });
      setCustomRuleInput((prev) => ({ ...prev, [groupKey]: '' }));
      load();
    } catch { alert('Failed to add custom rule'); }
  };

  const handleDeleteCustomRule = async (rule: string) => {
    if (!activeProject) return;
    try {
      await api.projects.permissions.deleteCustom(activeProject, { rule });
      load();
    } catch { /* ignore */ }
  };

  const handleTestCommand = async () => {
    if (!activeProject || !permTestCommand.trim()) return;
    try {
      const r = await api.projects.permissions.test(activeProject, { command: permTestCommand.trim() });
      setPermTestResult(r.allowed ? `Allowed${r.matched_rule ? ` (matched: ${r.matched_rule})` : ''}` : `Blocked${r.reason ? `: ${r.reason}` : ''}`);
    } catch { setPermTestResult('Test failed'); }
  };

  const handleGitMode = async (mode: GitMode) => {
    if (!activeProject) return;
    try {
      await api.projects.git(activeProject, mode);
      load();
    } catch { alert('Failed to update git mode'); }
  };

  const handleCreateRole = async () => {
    if (!activeProject || !newRoleName.trim()) return;
    try {
      await api.projects.roles.create(activeProject, { name: newRoleName.trim(), system_prompt: newRolePrompt, default_model: newRoleModel || undefined });
      setNewRoleName('');
      setNewRolePrompt('');
      setNewRoleModel('');
      load();
    } catch { alert('Failed to create role'); }
  };

  const handleDeleteRole = async (rid: number) => {
    if (!activeProject) return;
    try {
      await api.projects.roles.delete(activeProject, rid);
      load();
    } catch { alert('Failed to delete role'); }
  };

  const handleOpenSpecEditor = async () => {
    if (!activeProject) return;
    try {
      const spec = await api.projects.spec.get(activeProject);
      setSpecEditorText(JSON.stringify(spec.content || {}, null, 2));
      setSpecEditorErr('');
      setSpecEditorOpen(true);
    } catch { alert('Could not load spec'); }
  };

  const handleSaveSpec = async () => {
    if (!activeProject) return;
    let parsed: unknown;
    try { parsed = JSON.parse(specEditorText); } catch (e: unknown) {
      setSpecEditorErr('Invalid JSON: ' + (e instanceof Error ? e.message : ''));
      return;
    }
    try {
      await api.projects.spec.put(activeProject, parsed);
      setSpecEditorOpen(false);
      load();
    } catch (e: unknown) {
      setSpecEditorErr('Save failed: ' + (e instanceof Error ? e.message : ''));
    }
  };

  const handleCreateSpec = async () => {
    if (!activeProject) return;
    const init = { schema_version: 0, _project_type: 'generic' };
    try {
      await api.projects.spec.put(activeProject, init);
      load();
      setMemActiveTab('spec');
    } catch { alert('Failed to create spec'); }
  };

  if (!memPanelOpen) return null;

  return (
    <>
      <div className={cn('memory-panel fixed right-0 bottom-0 bg-surface-raised border-l border-default z-[50] flex flex-col shadow-side relative', isMobile ? 'inset-0 top-0 w-full border-l-0' : 'top-[76px]')} style={isMobile ? undefined : { width: rightPanelWidth }}>
        {!isMobile && <RightPanelResizer />}
        <div className="memory-header flex justify-between px-3 py-2.5 border-b border-border-muted">
          <h3 className="m-0 text-md-">
            Memory{project ? ` \u2014 ${project.name}` : ''}
          </h3>
          <button
            data-tip="Close the memory panel"
            className="btn py-0.5 px-2.5 text-sm+ border border-default rounded cursor-pointer text-sm"
            onClick={() => setMemPanelOpen(false)}
          >
            Close
          </button>
        </div>
        <div className="memory-tabs flex p-1 gap-0.5 bg-surface-panel">
          {MEM_TABS.map((t) => (
            <button
              data-tip={`Show the ${t.label} tab`}
              key={t.key}
              id={`mem-tab-${t.key}`}
              className={cn(
                'mem-tab flex-1 py-1 px-2 border-none bg-transparent text-xs cursor-pointer capitalize',
                memActiveTab === t.key && 'active bg-surface-raised rounded-sm font-semibold',
              )}
              onClick={() => setMemActiveTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="memory-body flex-1 overflow-y-auto p-3" id="mem-body">
          {!activeProject ? (
            <div className="mem-empty py-2 px-0.5 text-sm text-text-muted dark:text-text-dark-muted">Select a project in the sidebar to view its memory.</div>
          ) : loading ? (
            <div className="mem-empty py-2 px-0.5 text-sm text-text-muted dark:text-text-dark-muted">Loading\u2026</div>
          ) : error ? (
            <div className="mem-empty py-2 px-0.5 text-sm text-text-muted dark:text-text-dark-muted">{error}</div>
          ) : data === null ? (
            <div className="mem-empty py-2 px-0.5 text-sm text-text-muted dark:text-text-dark-muted">No data loaded.</div>
          ) : memActiveTab === 'files' ? (
            (data.files_catalog && activeProject) ? (
              <FilesBrowser
                files={data.files_catalog.files}
                byCategory={data.files_catalog.by_category}
                byTask={data.by_task || data.files_catalog.by_task}
                projectId={activeProject}
                projectSlug={project?.slug}
                tagsMap={data.files_catalog.tags as Record<string, { tags: string[]; note: string; updated_at?: string }> | undefined}
                folders={data.files_catalog.folders || []}
                onFilesChanged={load}
              />
            ) : (
              <div className="mem-empty py-2 px-0.5 text-sm text-text-muted dark:text-text-dark-muted">Loading files…</div>
            )
          ) : memActiveTab === 'defs' ? (
            <DefsTab data={data} fileUrl={fileUrl} projectId={activeProject} load={load} canEdit={perms.canEdit} />
          ) : memActiveTab === 'artifacts' ? (
            <ArtifactsTab data={data} fileUrl={fileUrl} executions={executions} />
          ) : memActiveTab === 'docs' ? (
            <DocsTab data={data} fileUrl={fileUrl} />
          ) : memActiveTab === 'perms' ? (
            <PermsTab
              data={data}
              onRefresh={handleRefreshPerms}
              onTogglePerm={handleTogglePerm}
              onAddCustomRule={handleAddCustomRule}
              onDeleteCustomRule={handleDeleteCustomRule}
              permTestCommand={permTestCommand}
              setPermTestCommand={setPermTestCommand}
              permTestResult={permTestResult}
              onTestCommand={handleTestCommand}
              customRuleInput={customRuleInput}
              setCustomRuleInput={setCustomRuleInput}
              canAdminister={perms.canAdminister}
            />
          ) : memActiveTab === 'git' ? (
            <GitTab data={data} onSetMode={handleGitMode} canAdminister={perms.canAdminister} />
          ) : memActiveTab === 'roles' ? (
            <RolesTab
              data={data}
              activeProject={activeProject}
              models={models}
              newRoleName={newRoleName}
              setNewRoleName={setNewRoleName}
              newRolePrompt={newRolePrompt}
              setNewRolePrompt={setNewRolePrompt}
              newRoleModel={newRoleModel}
              setNewRoleModel={setNewRoleModel}
              onCreateRole={handleCreateRole}
              onDeleteRole={handleDeleteRole}
              canAdminister={perms.canAdminister}
            />
          ) : memActiveTab === 'spec' ? (
            <SpecTab
              data={data}
              activeProject={activeProject}
              onEdit={handleOpenSpecEditor}
              onCreate={handleCreateSpec}
              canAdminister={perms.canAdminister}
            />
          ) : null}
        </div>
      </div>

      {specEditorOpen && (
        <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-[200]" onClick={(e) => { if (e.target === e.currentTarget) setSpecEditorOpen(false); }}>
          <div className="modal-content bg-surface-raised rounded-lg p-6 min-w-[420px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong" onClick={(e) => e.stopPropagation()} style={{ width: 520, maxWidth: '95vw' }}>
            <h3 className="m-0 mb-4 text-base">Edit master-spec.json</h3>
            <textarea
              data-tip="Edit the master spec JSON content"
              value={specEditorText}
              onChange={(e) => setSpecEditorText(e.target.value)}
              className="w-full min-h-[260px] font-mono text-sm+ p-1.5 bg-surface-muted border border-default rounded resize-y"
            />
            {specEditorErr ? (
              <div className="text-sm+ text-danger mt-1.5 block">
                {specEditorErr}
              </div>
            ) : null}
            <div className="modal-actions flex justify-end gap-2 mt-4">
              <button data-tip="Close without saving changes" className="btn py-1.5 px-4.5 border border-default rounded cursor-pointer text-md-" onClick={() => setSpecEditorOpen(false)}>Cancel</button>
              {perms.canAdminister ? (
                <button data-tip="Save the master spec" className="btn btn-primary bg-accent text-white border border-accent py-1.5 px-4.5 rounded cursor-pointer text-md- hover:bg-accent-deep" onClick={handleSaveSpec}>Save</button>
              ) : null}
            </div>
          </div>
        </div>
      )}
    </>
  );
};

// Extensions that browsers can't natively render — these get text-extracted
// via the /api/preview endpoint. (Imported from utils/file.)

const FileItem = ({ icon, name, size, href }: { icon: React.ReactNode; name: string; size?: number; href?: string | null }) => {
  const content = (
    <>
      <div className="mem-file-name text-sm+ font-medium inline-flex items-center gap-1.5">{icon} {name}</div>
      {size !== undefined ? <div className="mem-file-meta text-xs text-text-faint dark:text-text-dark-faint">{fmtBytes(size)}</div> : null}
    </>
  );
  if (href) {
    return (
      <a className="mem-file-item py-1.5 px-2 rounded mb-0.5 no-underline text-inherit block hover:bg-accent-soft" href={href} target="_blank" rel="noopener">
        {content}
      </a>
    );
  }
  return <div className="mem-file-item py-1.5 px-2 rounded mb-0.5 cursor-default">{content}</div>;
};

const DefsTab = ({ data, fileUrl, projectId, load, canEdit }: { data: MemoryView; fileUrl: (p: string) => string | null; projectId: number; load: () => void; canEdit: boolean }) => {
  const defs = data.definitions || [];
  if (!defs.length) return <div className="mem-empty py-2 px-0.5 text-sm text-text-muted dark:text-text-dark-muted">No CLAUDE.md / GUIDE.md / Skills.md in project root.</div>;
  return (
    <>
      {defs.map((f) => {
        const isSkills = f.name === 'Skills.md';
        return (
          <div key={f.path} className="flex items-center mb-1">
            {fileUrl(f.path) ? (
              <a href={fileUrl(f.path)!} target="_blank" rel="noopener" className="mem-file-item flex-1 py-1.5 px-2 rounded mb-0.5 no-underline text-inherit block hover:bg-accent-soft">
                <div className="mem-file-name text-sm+ font-medium inline-flex items-center gap-1.5">{defIcon(f.name)} {f.name}</div>
                <div className="mem-file-meta text-xs text-text-faint dark:text-text-dark-faint">{fmtBytes(f.size)}</div>
              </a>
            ) : (
              <div className="mem-file-item flex-1 py-1.5 px-2 rounded mb-0.5 cursor-default">
                <div className="mem-file-name text-sm+ font-medium inline-flex items-center gap-1.5">{defIcon(f.name)} {f.name}</div>
                <div className="mem-file-meta text-xs text-text-faint dark:text-text-dark-faint">{fmtBytes(f.size)}</div>
              </div>
            )}
            {isSkills && canEdit ? (
              <button
                data-tip="Regenerate Skills.md from the project"
                onClick={(e) => { e.stopPropagation(); api.projects.skills.refresh(projectId).then(load); }}
                className="ml-2 py-0.5 px-2 text-xs bg-accent-deep text-white border-none rounded-sm cursor-pointer inline-flex items-center gap-1 dark:bg-accent-dark"
              >
                <RefreshCw size={12} /> Refresh
              </button>
            ) : null}
          </div>
        );
      })}
    </>
  );
};

const ArtifactsTab = ({ data, fileUrl, executions }: { data: MemoryView; fileUrl: (p: string) => string | null; executions: any[] }) => {
  const files = data.files || [];
  if (!files.length) return <div className="mem-empty py-2 px-0.5 text-sm text-text-muted dark:text-text-dark-muted">No artifact files yet.<br />Run a task to generate chat logs.</div>;
  const displayName = (f: { name: string; file_type?: string; path?: string }) => {
    if (f.file_type === 'output') {
      // Try to show "output-7.md" instead of "exec-106-output.md"
      const execMatch = f.name.match(/^exec-(\d+)-output\.md$/);
      if (execMatch) {
        const execId = parseInt(execMatch[1], 10);
        const exec = executions.find((e: any) => e.id === execId);
        if (exec && exec.task_id) return `output-${exec.task_id}.md`;
      }
    }
    return f.name;
  };
  return (
    <>
      {files.map((f) => (
        <FileItem key={f.path} icon={memoryIcon(f.file_type || f.type)} name={displayName(f)} size={f.size} href={fileUrl(f.path)} />
      ))}
    </>
  );
};

const DocsTab = ({ data, fileUrl }: { data: MemoryView; fileUrl: (p: string) => string | null }) => {
  const docs = data.working_docs || [];
  if (!docs.length) return <div className="mem-empty py-2 px-0.5 text-sm text-text-muted dark:text-text-dark-muted">No files in Working Docs folder yet.</div>;
  return (
    <>
      {docs.map((f) => {
        const url = fileUrl(f.path);
        const icon = extIcon(f.name);
        if (url) {
          return <FileItem key={f.path} icon={icon} name={f.name} size={f.size} href={url} />;
        }
        return <FileItem key={f.path} icon={icon} name={f.name} size={f.size} />;
      })}
    </>
  );
};

const PermsTab = ({
  data, onRefresh, onTogglePerm, onAddCustomRule, onDeleteCustomRule,
  permTestCommand, setPermTestCommand, permTestResult, onTestCommand,
  customRuleInput, setCustomRuleInput, canAdminister,
}: {
  data: MemoryView;
  onRefresh: () => void;
  onTogglePerm: (rule: string, enabled: boolean) => void;
  onAddCustomRule: (groupKey: string) => void;
  onDeleteCustomRule: (rule: string) => void;
  permTestCommand: string;
  setPermTestCommand: (v: string) => void;
  permTestResult: string;
  onTestCommand: () => void;
  customRuleInput: Record<string, string>;
  setCustomRuleInput: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  canAdminister: boolean;
}) => {
  const groups = data.permissions?.groups || [];
  if (!groups.length) return <div className="mem-empty py-2 px-0.5 text-sm text-text-muted dark:text-text-dark-muted">No permissions configured.</div>;
  return (
    <>
      {canAdminister ? (
        <button
          data-tip="Rewrite permission config files"
          onClick={onRefresh}
          className="my-2.5 py-0.5 px-2.5 text-sm+ bg-accent-deep text-white border-none rounded-sm cursor-pointer inline-flex items-center gap-1 dark:bg-accent-dark"
        >
          <RefreshCw size={12} /> Rewrite .claude/settings.json + .vibe/config.toml
        </button>
      ) : null}
      <div className="text-xs text-text-faint mb-2.5 p-1 bg-warning/10 rounded-sm border-l-2 border-warning">
        Vibe: Bash permissions are enforced via system-prompt guardrails (not per-command patterns)
      </div>
      <div className="my-2.5 p-2 bg-surface-subtle rounded border border-border-muted">
        <div className="text-sm+ font-semibold text-ink mb-1.5">Test a command</div>
        <div className="flex gap-1.5 items-center">
          <input
            data-tip="Enter a command to test against permissions"
            type="text"
            placeholder="e.g. rm -rf /"
            value={permTestCommand}
            onChange={(e) => setPermTestCommand(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') onTestCommand(); }}
            className="flex-1 text-sm+ py-1 px-2 bg-surface-raised border border-default rounded-sm"
          />
          <button
            data-tip="Test whether this command is allowed"
            onClick={onTestCommand}
            className="text-sm+ py-1 px-2.5 bg-accent-deep text-white border-none rounded-sm cursor-pointer dark:bg-accent-dark"
          >
            Test
          </button>
        </div>
        <div className={cn('mt-1.5 text-sm+ min-h-[18px]', permTestResult.startsWith('Blocked') ? 'text-danger' : 'text-status-done')}>
          {permTestResult}
        </div>
      </div>
      {groups.map((g) => {
        const isVibe = g.key === 'C_bash_safe' || g.key === 'D_bash_proj' || g.key === 'E_destructive';
        return (
          <div key={g.key} className="mb-2.5 border-l-2 border-border-muted pl-2">
            <div className="font-semibold text-sm+ text-ink">
              {g.label}
              {isVibe ? <span className="text-2xs text-warning ml-1.5 opacity-70">Vibe: all-or-nothing bash</span> : null}
            </div>
            {(g.rules || []).map((r: { rule: string; enabled: boolean; auto_added?: boolean }) => (
              <div key={r.rule} className="flex items-center py-0.5 pl-2 text-sm+">
                <input
                  data-tip="Enable or disable this permission rule"
                  type="checkbox"
                  checked={r.enabled}
                  onChange={(e) => onTogglePerm(r.rule, e.target.checked)}
                  disabled={!canAdminister}
                  className={cn('mr-1.5', !canAdminister && 'opacity-40 cursor-not-allowed')}
                />
                <code className="flex-1 text-text-faint">{r.rule}</code>
                {!r.auto_added && canAdminister ? (
                  <button
                    data-tip="Delete this custom rule"
                    onClick={() => onDeleteCustomRule(r.rule)}
                    className="ml-1.5 bg-transparent border-none text-danger cursor-pointer text-sm+ inline-flex items-center"
                  >
                    <X size={12} />
                  </button>
                ) : null}
              </div>
            ))}
            {g.key === 'D_bash_proj' && canAdminister ? (
              <div className="py-1 pl-2 flex gap-1">
                <input
                  data-tip="Enter a custom bash command rule"
                  placeholder="Bash(npm test)"
                  value={customRuleInput[g.key] || ''}
                  onChange={(e) => setCustomRuleInput({ ...customRuleInput, [g.key]: e.target.value })}
                  onKeyDown={(e) => { if (e.key === 'Enter') onAddCustomRule(g.key); }}
                  className="text-sm+ py-0.5 px-1.5 w-1/2 bg-surface-raised border border-default"
                />
                <button
                  data-tip="Add the custom rule"
                  onClick={() => onAddCustomRule(g.key)}
                  className="text-sm+ py-0.5 px-2 bg-accent-deep text-white border-none rounded-sm cursor-pointer inline-flex items-center gap-1 dark:bg-accent-dark"
                >
                  <Plus size={12} /> Add
                </button>
              </div>
            ) : null}
          </div>
        );
      })}
    </>
  );
};

const GitTab = ({ data, onSetMode, canAdminister }: { data: MemoryView; onSetMode: (mode: GitMode) => void; canAdminister: boolean }) => {
  const g = data.git;
  const setting = g?.setting;
  const mode = setting === 1 ? 'on' : setting === 0 ? 'off' : 'auto';
  const stateLabel = g?.enabled
    ? <span className="text-success inline-flex items-center gap-1">● Enabled</span>
    : <span className="text-text-faint inline-flex items-center gap-1 dark:text-text-dark-faint">○ Disabled</span>;
  const repoLabel = g?.is_repo
    ? <span className="text-success inline-flex items-center gap-1">✓ git repo</span>
    : <span className="text-text-faint dark:text-text-dark-faint">not initialized</span>;
  return (
    <div className="py-1.5 px-0.5 text-sm leading-loose">
      <div className="mb-2.5">Status: <strong>{stateLabel}</strong> &middot; {repoLabel}</div>
      {g?.is_software ? (
        <div className="text-xs text-text-faint mb-2">Looks like a software project (source code detected).</div>
      ) : null}
      <div className="text-xs text-text-faint mb-1.5">Validation mode</div>
      <div className="flex gap-1.5 mb-3">
        {(['auto', 'on', 'off'] as const).map((m) => (
          <button
            key={m}
            data-tip={`Set git validation mode to ${m}`}
            onClick={() => onSetMode(m)}
            disabled={!canAdminister}
            className={cn(
              'py-1 px-3 text-sm+ cursor-pointer rounded border',
              mode === m
                ? 'bg-accent-deep text-white border-accent-deep dark:bg-accent-dark dark:border-accent-dark'
                : 'bg-transparent text-text-faint border-default dark:text-text-dark-faint dark:border-border-dark-default',
              !canAdminister && 'opacity-40 cursor-not-allowed',
            )}
          >
            {m.charAt(0).toUpperCase() + m.slice(1)}
          </button>
        ))}
      </div>
      <div className="text-xs text-text-faint bg-success/10 border-l-2 border-success rounded-sm py-1.5 px-2 dark:text-text-dark-faint dark:bg-status-done-dark/15 dark:border-status-done-dark">
        When enabled, each task runs on its own <code>task/&lt;id&gt;</code> branch.
        <strong>Approve</strong> merges it to the main branch; <strong>Reject</strong> discards it (zero residue).
        <br /><em>Auto</em> turns this on automatically for projects that contain source code.
      </div>
    </div>
  );
};

const RolesTab = ({
  data, activeProject, models,
  newRoleName, setNewRoleName, newRolePrompt, setNewRolePrompt,
  newRoleModel, setNewRoleModel, onCreateRole, onDeleteRole,
  canAdminister,
}: {
  data: MemoryView;
  activeProject: number | null;
  models: { id: string; label: string }[];
  newRoleName: string;
  setNewRoleName: (v: string) => void;
  newRolePrompt: string;
  setNewRolePrompt: (v: string) => void;
  newRoleModel: string;
  setNewRoleModel: (v: string) => void;
  onCreateRole: () => void;
  onDeleteRole: (rid: number) => void;
  canAdminister: boolean;
}) => {
  const roles: Role[] = (data.roles || []) as Role[];
  const globalRoles = roles.filter((r) => !r.project_id);
  const projRoles = roles.filter((r) => r.project_id);
  return (
    <div className="py-0.5">
      <div className="text-xs font-bold uppercase tracking-wide text-text-faint mb-1.5">
        Global roles ({globalRoles.length})
      </div>
      {globalRoles.length ? globalRoles.map((r) => (
        <div key={r.id} className="mb-2 p-1.5 bg-purple-500/10 rounded border-l-2 border-purple-400/50 dark:bg-purple-900/30 dark:border-purple-500/40">
          <div className="flex items-center gap-1.5 mb-1">
            <span className="font-semibold text-sm+ flex-1">{r.name}</span>
            {r.default_model ? <span className="text-2xs text-text-faint">{r.default_model}</span> : null}
          </div>
          <div className="text-xs text-text-faint whitespace-pre-wrap max-h-[60px] overflow-hidden">
            {(r.system_prompt || '').slice(0, 200)}{(r.system_prompt || '').length > 200 ? '\u2026' : ''}
          </div>
        </div>
      )) : <div className="mem-empty py-2 px-0 text-sm text-text-muted dark:text-text-dark-muted">No global roles.</div>}
      {projRoles.length > 0 ? (
        <>
          <div className="text-xs font-bold uppercase tracking-wide text-text-faint my-2.5">
            Project roles ({projRoles.length})
          </div>
          {projRoles.map((r) => (
            <div key={r.id} className="mb-2 p-1.5 bg-purple-500/10 rounded border-l-2 border-purple-400/50 dark:bg-purple-900/30 dark:border-purple-500/40">
              <div className="flex items-center gap-1.5 mb-1">
                <span className="font-semibold text-sm+ flex-1">{r.name}</span>
                {r.default_model ? <span className="text-2xs text-text-faint">{r.default_model}</span> : null}
                {canAdminister ? (
                  <button
                    data-tip="Delete this project role"
                    onClick={() => onDeleteRole(r.id)}
                    className="bg-transparent border-none text-danger cursor-pointer text-sm+ px-0.5 inline-flex items-center"
                  >
                    <X size={12} />
                  </button>
                ) : null}
              </div>
              <div className="text-xs text-text-faint whitespace-pre-wrap max-h-[60px] overflow-hidden">
                {(r.system_prompt || '').slice(0, 200)}{(r.system_prompt || '').length > 200 ? '\u2026' : ''}
              </div>
            </div>
          ))}
        </>
      ) : null}
      {activeProject && canAdminister ? (
        <div className="mt-3 border-t border-border-muted pt-2.5 dark:border-border-dark-muted">
          <div className="text-sm+ text-text-faint mb-1.5 font-semibold inline-flex items-center gap-1 dark:text-text-dark-faint"><Plus size={12} /> New project role</div>
          <input
            data-tip="Enter a name for the new role"
            placeholder="Role name"
            value={newRoleName}
            onChange={(e) => setNewRoleName(e.target.value)}
            className="w-full mb-1 text-sm+ py-0.5 px-1.5 bg-surface-raised border border-default rounded-sm"
          />
          <textarea
            data-tip="Write the role's system prompt"
            placeholder="System prompt..."
            rows={4}
            value={newRolePrompt}
            onChange={(e) => setNewRolePrompt(e.target.value)}
            className="w-full mb-1 text-sm+ py-0.5 px-1.5 bg-surface-raised border border-default rounded-sm resize-y"
          />
          <select
            data-tip="Choose the default model for this role"
            value={newRoleModel}
            onChange={(e) => setNewRoleModel(e.target.value)}
            className="text-sm+ py-0.5 px-1.5 bg-surface-raised border border-default rounded-sm mb-1.5 w-full"
          >
            <option value="">— Default model —</option>
            {models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
          </select>
          <button
            data-tip="Create the new project role"
            onClick={onCreateRole}
            className="text-sm+ py-0.5 px-2.5 bg-accent-deep text-white border-none rounded-sm cursor-pointer inline-flex items-center gap-1 dark:bg-accent-dark"
          >
            <Plus size={12} /> Create
          </button>
        </div>
      ) : null}
    </div>
  );
};

const SpecTab = ({ data, activeProject: _activeProject, onEdit, onCreate, canAdminister }: { data: MemoryView; activeProject: number | null; onEdit: () => void; onCreate: () => void; canAdminister: boolean }) => {
  const spec = data.spec;
  if (!spec?.exists) {
    return (
      <div className="py-2 px-0.5">
        <div className="mem-empty py-2 px-0.5 text-sm text-text-muted mb-3 dark:text-text-dark-muted">No master spec yet for this project.</div>
        {canAdminister ? (
          <button
            data-tip="Create a new master-spec.json"
            onClick={onCreate}
            className="py-1 px-3.5 text-sm+ bg-accent-deep text-white border-none rounded cursor-pointer inline-flex items-center gap-1 dark:bg-accent-dark"
          >
            <Plus size={12} /> Create master-spec.json
          </button>
        ) : null}
      </div>
    );
  }
  const content = (spec.content || {}) as Record<string, unknown>;
  const sv = String(content.schema_version ?? '\u2014');
  const skip = new Set(['schema_version', 'updated_at', '_project_type']);
  const rows = Object.entries(content)
    .filter(([k]) => !skip.has(k))
    .map(([k, v]) => {
      const val = typeof v === 'object' ? JSON.stringify(v) : String(v);
      return (
        <tr key={k}>
          <td className="py-0.5 pr-2 text-sm+ text-text-faint whitespace-nowrap align-top">{k}</td>
          <td className="py-0.5 text-sm+ break-all">{val}</td>
        </tr>
      );
    });
  return (
    <div className="py-0.5">
      <div className="flex items-center gap-2 mb-2">
        <span className="text-xs text-text-faint">v{sv}</span>
        <span className="flex-1" />
        {canAdminister ? (
          <button
            data-tip="Edit the master spec JSON"
            onClick={onEdit}
            className="py-0.5 px-2.5 text-xs bg-transparent text-accent-deep border border-accent-deep rounded-sm cursor-pointer"
          >
            Edit
          </button>
        ) : null}
      </div>
      {content._project_type ? (
        <div className="text-xs text-text-faint mb-2">Type: <strong>{String(content._project_type)}</strong></div>
      ) : null}
      {rows.length ? (
        <table className="w-full border-collapse">{rows}</table>
      ) : (
        <div className="text-sm+ text-text-faint">Empty spec — click Edit to add keys.</div>
      )}
    </div>
  );
};

export default MemoryPanel;
