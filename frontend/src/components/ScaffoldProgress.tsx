import { useState, useEffect, useRef, useCallback } from 'react';
import { api } from '../api';

interface LogEntry {
  type: string;
  message?: string;
}

interface Props {
  projectId: number;
  chatId?: number;
  projectName: string;
  onApply: () => Promise<void>;
  onDismiss: () => void;
  onDiscuss: () => void;
}

export const ScaffoldProgress = ({ projectId, chatId, projectName, onApply, onDismiss, onDiscuss }: Props) => {
  const [logs, setLogs] = useState<string[]>([]);
  const [done, setDone] = useState(false);
  const [draft, setDraft] = useState('');
  const [draftLoading, setDraftLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [applied, setApplied] = useState(false);
  const [error, setError] = useState('');
  const sourceRef = useRef<EventSource | null>(null);
  const doneRef = useRef(false);

  useEffect(() => {
    const url = `/api/projects/${projectId}/scaffold-progress`;
    const es = new EventSource(url);
    sourceRef.current = es;

    es.onmessage = (event) => {
      try {
        const data: LogEntry = JSON.parse(event.data);
        if (data.type === 'heartbeat') return;
        if (data.type === 'log' && data.message) {
          setLogs((prev) => [...prev, data.message!]);
        }
        if (data.type === 'done') {
          setDone(true);
          es.close();
          if (!doneRef.current && chatId) {
            doneRef.current = true;
            setDraftLoading(true);
            api.chats.get(chatId).then((chat) => {
              setDraft(chat.scaffold_draft || '');
            }).catch(() => {
              setError('Failed to load the draft.');
            }).finally(() => {
              setDraftLoading(false);
            });
          }
        }
      } catch {
        // ignore parse errors
      }
    };

    es.onerror = () => {
      setError('Connection lost. The draft may still complete in the background.');
      es.close();
    };

    return () => {
      es.close();
    };
  }, [projectId, chatId]);

  const handleApply = useCallback(async () => {
    if (applying || applied) return;
    setApplying(true);
    setError('');
    try {
      if (chatId && draft) {
        await api.chats.update(chatId, { scaffold_draft: draft });
      }
      await onApply();
      setApplied(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Apply failed');
    } finally {
      setApplying(false);
    }
  }, [applying, applied, chatId, draft, onApply]);

  const handleDismiss = useCallback(() => {
    onDismiss();
  }, [onDismiss]);

  const handleDiscuss = useCallback(() => {
    onDiscuss();
  }, [onDiscuss]);

  const phaseCount = (draft.match(/^#{1,3}\s+Phase\s+\d+/gmi) || []).length;
  const taskCount = (draft.match(/^\s*-\s+\[[\s xX~]\]/gm) || []).length;
  const hasValidDraft = phaseCount >= 2;

  return (
    <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-[1200]" onClick={done && !applying ? (e) => { if (e.target === e.currentTarget) handleDismiss(); } : undefined}>
      <div className="modal-content bg-surface-raised rounded-lg p-6 min-w-[420px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong" style={{ minWidth: 560, maxWidth: 720 }} onClick={(e) => e.stopPropagation()}>
        <h3 className="m-0 mb-4 text-base">Scaffolding — {projectName}</h3>

        {/* Progress logs — shown while generating, collapsed once draft is loaded */}
        {(logs.length > 0 || !done) && (
          <div className="mb-4">
            <div
              className="max-h-[160px] overflow-y-auto bg-surface-subtle rounded-md p-3 font-mono text-sm leading-[1.7]"
            >
              {logs.length === 0 && !done && (
                <div className="text-text-faint">Connecting...</div>
              )}
              {logs.map((msg, i) => (
                <div key={i} className="text-text-DEFAULT">{msg}</div>
              ))}
              {!done && !error && (
                <div className="text-text-faint mt-1">Generating phase roadmap...</div>
              )}
              {error && !done && (
                <div className="text-dangerStrong mt-2">{error}</div>
              )}
            </div>
          </div>
        )}

        {/* Draft review/edit area */}
        {done && draftLoading && (
          <div className="text-text-faint text-sm mb-4">Loading draft...</div>
        )}

        {done && !draftLoading && !draft && !error && (
          <div className="mb-4">
            <div className="text-dangerStrong text-sm mb-3">
              The AI did not produce a valid roadmap draft. You can discard this project or try again in the chat.
            </div>
            <div className="flex justify-end gap-2">
              <button data-tip="Discard this scaffolded roadmap" className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-" onClick={handleDismiss}>Discard</button>
            </div>
          </div>
        )}

        {done && !draftLoading && draft && (
          <>
            <div className="flex items-center gap-3 mb-2">
              <span className="text-sm font-semibold text-text-soft">
                Draft roadmap
              </span>
              {hasValidDraft && (
                <span className="text-xs text-text-faint">
                  {phaseCount} phase{phaseCount !== 1 ? 's' : ''} · {taskCount} task{taskCount !== 1 ? 's' : ''}
                </span>
              )}
            </div>
            <textarea
              data-tip="Edit the scaffolded roadmap before applying"
              className="w-full font-mono text-sm border border-default rounded p-3 mb-4"
              style={{ resize: 'vertical', minHeight: 280, maxHeight: '50vh' }}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              disabled={applying || applied}
            />
            {applied && (
              <div className="text-status-done font-semibold text-sm mb-3">
                &#x2713; Guide applied — {taskCount} tasks imported. Switching to board...
              </div>
            )}
            {error && (
              <div className="text-dangerStrong text-sm mb-3">{error}</div>
            )}
            {!applied && (
              <div className="modal-actions flex justify-end gap-2">
                <button
                  data-tip="Discard this scaffolded roadmap"
                  className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-"
                  onClick={handleDismiss}
                  disabled={applying}
                >
                  Discard
                </button>
                <button
                  data-tip="Discuss this roadmap with your Guide"
                  className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-"
                  onClick={handleDiscuss}
                  disabled={applying}
                >
                  Discuss with AI
                </button>
                <button
                  data-tip={hasValidDraft ? 'Write GUIDE.md and import tasks' : 'Draft must have at least 2 phases starting at Phase 1'}
                  className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white"
                  onClick={handleApply}
                  disabled={applying || !hasValidDraft}
                >
                  {applying ? 'Applying...' : 'Apply Guide'}
                </button>
              </div>
            )}
          </>
        )}

        {/* Fallback close button while generating or on error */}
        {!done && (
          <div className="modal-actions flex justify-end gap-2 mt-4">
            <button
              data-tip="Close this progress window"
              className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-"
              onClick={handleDismiss}
              disabled
            >
              Close
            </button>
          </div>
        )}
      </div>
    </div>
  );
};

export default ScaffoldProgress;