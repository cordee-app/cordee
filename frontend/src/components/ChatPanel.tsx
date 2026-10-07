import { useState, useRef, useEffect, useCallback } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import type { EstimateResult, ChatTranscriptEntry, AttachItem } from '../types';
import { cn } from '../utils/cn';
import { fmtUsd } from '../utils/currency';
import { RightPanelResizer } from './RightPanelResizer';
import { useIsMobile } from '../hooks/useIsMobile';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import { Paperclip, X, Lock, Send, LoaderCircle } from 'lucide-react';

function escHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/** Mirror agent_chats.append_user_message: `YYYY-MM-DD HH:MM` in UTC. */
function formatChatTimestamp(d: Date): string {
  const pad = (n: number) => n.toString().padStart(2, '0');
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`
  );
}

function formatElapsed(s: number): string {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}m ${r.toString().padStart(2, '0')}s`;
}

function inlineMd(s: string): string {
  s = escHtml(s);
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  return s;
}

function chatMd(raw: string): string {
  const lines = raw.split('\n');
  let html = '';
  let inCode = false;
  let codeBuf = '';

  for (const line of lines) {
    if (!inCode && line.startsWith('```')) {
      inCode = true;
      codeBuf = '';
      continue;
    }
    if (inCode) {
      if (line.startsWith('```')) {
        inCode = false;
        html += `<pre><code>${escHtml(codeBuf)}</code></pre>`;
      } else {
        codeBuf += (codeBuf ? '\n' : '') + line;
      }
      continue;
    }
    if (!line.trim()) { html += '<br>'; continue; }
    html += `<span>${inlineMd(line)}</span><br>`;
  }
  if (inCode) html += `<pre><code>${escHtml(codeBuf)}</code></pre>`;
  return html;
}

const MSG_HEADER_RE = /^### \[([^\]]+)\] (👤|🤖) (?:User|Assistant.*)$/;
// System notes are whole lines (agent_chats.append_system_note writes
// "\n_[ts] note_\n"). Anchoring both ends keeps an italic `_[x] y_` inside a
// message body from being lifted out as a phantom system line.
const SYS_NOTE_RE = /^_\[([^\]]+)\] (.+)_$/;

/** Parse a chat markdown transcript into ordered entries.
 *
 * Walks the file line by line so user / assistant / system entries come out in
 * document order — collecting messages first and appending notes afterwards put
 * every system note at the end of the conversation. */
function parseChatTranscript(raw: unknown): ChatTranscriptEntry[] {
  if (typeof raw !== 'string' || !raw) return [];
  const msgs: ChatTranscriptEntry[] = [];

  let role: ChatTranscriptEntry['role'] | null = null;
  let timestamp = '';
  let buf: string[] = [];
  let inCode = false;

  const flush = () => {
    if (!role) return;
    let content = buf.join('\n').trim();
    if (role === 'assistant') {
      const mm = content.match(/^\*\*Model:\*\*[^\n]+\n?/);
      if (mm) content = content.slice(mm[0].length).trim();
    }
    // Drop the trailing "---" separator append_assistant_message writes.
    content = content.replace(/\n*---\s*$/, '').trim();
    msgs.push({ role, content, timestamp });
    role = null;
    buf = [];
  };

  for (const line of raw.split('\n')) {
    if (line.startsWith('```')) {
      inCode = !inCode;
      buf.push(line);
      continue;
    }
    if (!inCode) {
      const hm = line.match(MSG_HEADER_RE);
      if (hm) {
        flush();
        role = hm[2] === '👤' ? 'user' : 'assistant';
        timestamp = hm[1];
        continue;
      }
      const nm = line.match(SYS_NOTE_RE);
      if (nm) {
        flush();
        msgs.push({ role: 'system', content: nm[2], timestamp: nm[1] });
        continue;
      }
    }
    if (role) buf.push(line);
  }
  flush();

  return msgs;
}

function attachName(att: AttachItem): string {
  return att.name || att.label || att.ref || att.type || 'attachment';
}

function attachTitle(att: AttachItem): string {
  return att.path || att.ref || att.label || att.name || '';
}

const ChatPanelInner = () => {
  const {
    activeChat, setActiveChat, models, aingelChatIds,
    chatSending, setChatSending, setChatSendingId,
    setShowAttachPicker, projects,
    rightPanelWidth,
    setProjects, setTasks, setChats, setPhasesData, setActiveMainTab,
  } = useStore();

  const isMobile = useIsMobile();
  const perms = useProjectPermissions(activeChat?.project_id);

  const [inputText, setInputText] = useState('');
  const [estimate, setEstimate] = useState<EstimateResult | null>(null);
  const [sendingLocal, setSendingLocal] = useState(false);
  const [promoting, setPromoting] = useState(false);
  const [sendError, setSendError] = useState('');
  const [elapsedSec, setElapsedSec] = useState(0);
  const sentAtRef = useRef<number>(0);
  const messagesRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const estimateTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const elapsedTimer = useRef<ReturnType<typeof setInterval> | undefined>(undefined);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const chat = activeChat!;
  const isAingelChat = aingelChatIds.has(chat.id);
  const isSending = sendingLocal || chatSending;
  const isTaskChat = !!chat.task_id;
  const projectAingelModel = isTaskChat
    ? projects.find((p) => p.id === chat.project_id)?.aingel_model
    : undefined;
  const modelLocked = isAingelChat || (isTaskChat && !!projectAingelModel);
  // The one model the panel displays, sends, and prices with. Task chats created
  // before the lock existed can carry a different chat.model; the effect below
  // converges the stored row so /api/estimate agrees with what actually runs.
  const effectiveModel = modelLocked ? (projectAingelModel || chat.model) : chat.model;

  const isScaffoldChat = chat.name === 'Project scaffolding';
  const [scaffoldApplying, setScaffoldApplying] = useState(false);
  const [scaffoldApplied, setScaffoldApplied] = useState(false);
  const [scaffoldError, setScaffoldError] = useState('');

  const handleApplyGuide = async () => {
    if (scaffoldApplying || scaffoldApplied) return;
    setScaffoldApplying(true);
    setScaffoldError('');
    try {
      const projId = chat.project_id;
      await api.projects.applyGuide(projId, chat.id);
      await api.chats.update(chat.id, { status: 'archived' });
      const [projs, t, chatsList, phases] = await Promise.all([
        api.projects.list(),
        api.tasks.list(),
        api.chats.list({ status: 'active' }),
        api.phases.list(),
      ]);
      setProjects(projs);
      setTasks(t);
      setChats(chatsList);
      setPhasesData(phases);
      setScaffoldApplied(true);
      setActiveChat(null);
      setActiveMainTab('board');
    } catch (e) {
      setScaffoldError(e instanceof Error ? e.message : 'Apply failed');
    } finally {
      setScaffoldApplying(false);
    }
  };

  const scrollToBottom = useCallback(() => {
    if (messagesRef.current) {
      messagesRef.current.scrollTop = messagesRef.current.scrollHeight;
    }
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [chat.transcript, scrollToBottom, isSending]);

  useEffect(() => {
    setInputText('');
    setEstimate(null);
  }, [chat.id]);

  useEffect(() => {
    if (!isSending) {
      if (elapsedTimer.current) {
        clearInterval(elapsedTimer.current);
        elapsedTimer.current = undefined;
      }
      setElapsedSec(0);
      return;
    }
    setElapsedSec(0);
    sentAtRef.current = Date.now();
    elapsedTimer.current = setInterval(() => {
      setElapsedSec(Math.floor((Date.now() - sentAtRef.current) / 1000));
    }, 1000);
    return () => {
      if (elapsedTimer.current) {
        clearInterval(elapsedTimer.current);
        elapsedTimer.current = undefined;
      }
    };
  }, [isSending]);

  // Persist the locked model onto the chat row: the cost estimate endpoint reads
  // the stored chat.model, so leaving it stale would price a run that never happens.
  useEffect(() => {
    if (!perms.canEdit || !modelLocked || !effectiveModel || chat.model === effectiveModel) return;
    const chatId = chat.id;
    api.chats.update(chatId, { model: effectiveModel })
      .then(() => {
        const current = useStore.getState().activeChat;
        if (current?.id === chatId) setActiveChat({ ...current, model: effectiveModel });
      })
      .catch(() => {});
  }, [chat.id, chat.model, modelLocked, effectiveModel, setActiveChat, perms.canEdit]);

  const debounceEstimate = useCallback(
    (text: string) => {
      clearTimeout(estimateTimer.current);
      if (!text.trim()) {
        estimateTimer.current = setTimeout(() => setEstimate(null), 600);
        return;
      }
      estimateTimer.current = setTimeout(async () => {
        try {
          const r = await api.estimate({ chat_id: chat.id, message: text });
          setEstimate(r);
        } catch {
          setEstimate(null);
        }
      }, 600);
    },
    [chat.id],
  );

  const handleSend = async () => {
    if (isSending || !inputText.trim()) return;
    const chatId = chat.id;
    const text = inputText.trim();
    const model = effectiveModel;

    setInputText('');
    setEstimate(null);
    setSendError('');

    const ts = formatChatTimestamp(new Date());
    const optimisticEntry = `\n### [${ts}] 👤 User\n\n${text.replace(/\n+$/, '')}\n`;
    const priorTranscript = chat.transcript || '';
    const optimisticTranscript = priorTranscript + optimisticEntry;
    setActiveChat({ ...chat, transcript: optimisticTranscript });

    setSendingLocal(true);
    setChatSending(true);
    setChatSendingId(chatId);

    try {
      // The server returns 202 immediately and runs the reply in a background
      // thread (a long synchronous POST would be dropped by the Cloudflare
      // Tunnel's ~100s timeout → 502). Poll reply-status until it's done.
      await api.chats.sendMessage(chatId, { text, model });
      await pollUntilDone(chatId);
      const fresh = await api.chats.get(chatId);
      if (useStore.getState().activeChat?.id === chatId) {
        setActiveChat(fresh);
      }
    } catch (e) {
      console.error('Chat send failed', e);
      // The server may have already persisted the user message and a system
      // note (late errors), so refresh from the server rather than restoring the
      // optimistic transcript, which would discard that server-side state.
      try {
        const fresh = await api.chats.get(chatId);
        if (useStore.getState().activeChat?.id === chatId) {
          setActiveChat(fresh);
        }
      } catch {
        // Fall back to the optimistic transcript if the refresh fails.
        const stillActive = useStore.getState().activeChat;
        if (stillActive?.id === chatId) {
          setActiveChat({ ...stillActive, transcript: priorTranscript });
        }
      }
      const msg = e instanceof Error ? e.message : 'Failed to send message';
      setSendError(msg);
    } finally {
      setSendingLocal(false);
      setChatSending(false);
      setChatSendingId(null);
    }
  };

  // Poll /api/chats/<id>/reply-status until the background reply is done.
  // Each request is short so the tunnel never times out. Stops on unmount.
  // `isCancelled` (optional) lets a caller abort the loop (e.g. when the
  // component switches chats mid-recovery) so a stale tick can't keep
  // overwriting pollTimer.current and orphan a newer poll.
  const pollUntilDone = (chatId: number, isCancelled?: () => boolean): Promise<void> =>
    new Promise((resolve, reject) => {
      const tick = async () => {
        if (isCancelled?.()) {
          reject(new Error('cancelled'));
          return;
        }
        try {
          const st = await api.chats.replyStatus(chatId);
          if (isCancelled?.()) {
            reject(new Error('cancelled'));
            return;
          }
          if (st.done) {
            if (st.status === 'failed' && st.error) {
              reject(new Error(st.error));
              return;
            }
            resolve();
            return;
          }
          pollTimer.current = setTimeout(tick, 2000);
        } catch (e) {
          reject(e);
        }
      };
      pollTimer.current = setTimeout(tick, 2000);
    });

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
  };

  const handlePromote = async () => {
    const scope = prompt(
      'Promote latest assistant reply to which scope? (phase/project/workflow)',
      'phase',
    );
    if (!scope || !['phase', 'project', 'workflow'].includes(scope)) return;
    setPromoting(true);
    try {
      await api.chats.promoteMemory(chat.id, {
        target_scope: scope as 'phase' | 'project' | 'workflow',
        mode: 'compact',
      });
      const fresh = await api.chats.get(chat.id);
      setActiveChat(fresh);
    } catch (e) {
      console.error('Promote failed', e);
    } finally {
      setPromoting(false);
    }
  };

  useEffect(() => {
    return () => {
      clearTimeout(estimateTimer.current);
      if (pollTimer.current) clearTimeout(pollTimer.current);
    };
  }, []);

  // Recover an in-flight reply after a page reload: the reply runs in a
  // background thread server-side, so a reload loses the local sending state.
  // If the server reports this chat as busy, poll reply-status until it's done
  // and refresh the transcript.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const st = await api.chats.status();
        if (cancelled || !st.busy || st.active_chat_id !== chat.id) return;
        setChatSending(true);
        setChatSendingId(chat.id);
        await pollUntilDone(chat.id, () => cancelled);
        if (cancelled) return;
        const fresh = await api.chats.get(chat.id);
        if (useStore.getState().activeChat?.id === chat.id) {
          setActiveChat(fresh);
        }
      } catch {
        // Best-effort recovery; ignore failures.
      } finally {
        if (!cancelled) {
          setChatSending(false);
          setChatSendingId(null);
        }
      }
    })();
    return () => {
      cancelled = true;
      if (pollTimer.current) clearTimeout(pollTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chat.id]);

  const transcript: ChatTranscriptEntry[] = parseChatTranscript(chat.transcript);

  return (
    <div className={cn('chat-panel fixed right-0 bottom-0 bg-surface-muted border-l border-default z-[50] flex flex-col overflow-hidden relative', isMobile ? 'inset-0 top-0 w-full border-l-0' : 'top-[76px]')} style={isMobile ? undefined : { width: rightPanelWidth }}>
      {!isMobile && <RightPanelResizer />}
      <div
        className="px-3 py-2.5 border-b border-border-muted flex justify-between items-center shrink-0"
      >
        <h3 className="m-0 text-md-">{chat.name || `Chat #${chat.id}`}</h3>
        <div className="flex gap-1.5 items-center">
          {perms.canEdit && (
            <label
              className="flex items-center gap-1 text-xs text-text-muted cursor-pointer"
            >
              <input
                data-tip="Auto-include project definitions in messages"
                type="checkbox"
                checked={!!chat.auto_inject_defs}
                onChange={(e) => {
                  const checked = e.target.checked;
                  const updated = { ...chat, auto_inject_defs: checked };
                  setActiveChat(updated);
                  api.chats.update(chat.id, { auto_inject_defs: checked }).catch(() => {});
                }}
                style={{ margin: 0 }}
              />
              Auto-include project defs
            </label>
          )}
          <button
            data-tip="Close chat panel"
            onClick={() => setActiveChat(null)}
            className="border border-default rounded bg-surface-raised cursor-pointer text-sm py-0.5 px-2 dark:bg-surface-dark-raised dark:border-border-dark-default dark:text-text-dark-soft"
          >
            Close
          </button>
        </div>
      </div>

      {isScaffoldChat && (
        <div className="px-3 py-2 border-b border-accent/30 bg-accent-soft/30 shrink-0">
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-semibold text-accent-strong">
              {scaffoldApplied ? '✓ Guide applied' : 'Scaffolding — review the draft, then apply'}
            </span>
            {!scaffoldApplied && perms.canEdit && (
              <button
                data-tip="Apply the scaffolded guide and import tasks"
                className="btn btn-primary py-1 px-3 text-sm bg-accent text-white border border-accent rounded cursor-pointer disabled:opacity-60"
                onClick={handleApplyGuide}
                disabled={scaffoldApplying}
              >
                {scaffoldApplying ? 'Applying...' : 'Apply Guide'}
              </button>
            )}
          </div>
          {scaffoldError && (
            <div className="text-xs text-danger mt-1">{scaffoldError}</div>
          )}
          {scaffoldApplied && (
            <div className="text-xs text-text-faint mt-1">
              Tasks imported. Switching to board...
            </div>
          )}
        </div>
      )}

      <div className="px-3 py-1 border-b border-border-muted shrink-0 flex flex-wrap gap-1 items-center">
        {chat.attachments && chat.attachments.length > 0 && (
          <>
            {chat.attachments.map((att, i) => (
              <span
                key={i}
                className="attach-pill inline-flex items-center gap-1 px-1.5 py-px bg-accent-tint text-accent-strong rounded-[10px] text-xs max-w-[150px] overflow-hidden text-ellipsis whitespace-nowrap"
                title={attachTitle(att)}
              >
                {attachName(att)}
                {perms.canEdit && (
                  <span
                    className="attach-pill-remove cursor-pointer text-2xs opacity-60 shrink-0 hover:opacity-100 inline-flex items-center"
                    onClick={async () => {
                      const updated = chat.attachments!.filter((_, j) => j !== i);
                      const updatedChat = { ...chat, attachments: updated };
                      setActiveChat(updatedChat);
                      try {
                        await api.chats.update(chat.id, { attachments: updated });
                      } catch {}
                    }}
                  >
                    <X size={10} />
                  </span>
                )}
              </span>
            ))}
          </>
        )}
        {perms.canEdit && (
          <button
            data-tip="Attach context"
            className="attach-add-btn px-2 py-px border border-dashed border-border-strong rounded-[10px] bg-transparent text-xs text-text-muted cursor-pointer hover:bg-surface-subtle dark:border-border-dark-strong dark:text-text-dark-muted dark:hover:bg-surface-dark-subtle inline-flex items-center gap-1"
            onClick={() => setShowAttachPicker(true)}
          >
            <Paperclip size={11} /> Attach
          </button>
        )}
      </div>

      {!isAingelChat && (
        <div className="px-3 py-1.5 border-b border-border-muted shrink-0">
          {modelLocked || !perms.canEdit ? (
            <div className="w-full py-1 px-2 text-sm border border-default rounded bg-surface-subtle text-text-soft flex items-center justify-between dark:bg-surface-dark-subtle dark:text-text-dark-soft dark:border-border-dark-default">
              <span>{models.find((m) => m.id === effectiveModel)?.label || effectiveModel}</span>
              {modelLocked && (
                <span className="text-text-faint shrink-0 inline-flex items-center" title="Locked to project Guide model"><Lock size={12} /></span>
              )}
            </div>
          ) : (
            <select
              data-tip="Choose the model for this chat"
              value={chat.model}
              onChange={(e) => {
                const updated = { ...chat, model: e.target.value };
                setActiveChat(updated);
                api.chats.update(chat.id, { model: e.target.value }).catch(() => {});
              }}
              className="w-full py-1 px-2 text-sm border border-default rounded dark:bg-surface-dark-base dark:border-border-dark-default dark:text-text-dark"
            >
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </select>
          )}
        </div>
      )}

      <div className="chat-messages flex-1 overflow-y-auto p-3" ref={messagesRef}>
        {transcript.length === 0 && !isSending ? (
          <p className="text-text-faint italic text-center pt-[30px]">
            No messages yet.
          </p>
        ) : (
          <>
            {transcript.map((msg, i) => (
              msg.role === 'system' ? (
                <div key={i} className="mb-2 text-center">
                  <span className="text-xs text-text-faint italic">
                    {msg.content}
                  </span>
                </div>
              ) : (
                <div key={i} className="mb-3">
                  <div
                    className={cn(
                      'max-w-[85%] px-3 py-2 rounded-2xl text-sm leading-relaxed break-words whitespace-pre-wrap text-ink dark:text-text-dark',
                      msg.role === 'user'
                        ? 'bg-accent-tint ml-auto dark:bg-accent-dark-tint'
                        : 'bg-surface-subtle mr-auto dark:bg-surface-dark-subtle',
                    )}
                    dangerouslySetInnerHTML={{ __html: chatMd(msg.content) }}
                  />
                  <div
                    className={cn(
                      'text-xs text-text-faint mt-0.5',
                      msg.role === 'user' ? 'text-right' : 'text-left',
                    )}
                  >
                    {msg.timestamp}
                  </div>
                </div>
              )
            ))}
            {isSending && (
              <div className="mb-3 thinking-bubble">
                <div className="inline-flex items-center gap-2 max-w-[85%] mr-auto px-3 py-2 rounded-2xl bg-surface-subtle dark:bg-surface-dark-subtle text-text-soft dark:text-text-dark-soft border border-border-muted dark:border-border-dark-muted shadow-sm animate-pulse">
                  <LoaderCircle size={14} className="animate-spin shrink-0 text-text-muted dark:text-text-dark-muted" />
                  <span className="text-sm font-medium">AI is thinking</span>
                  <span className="text-xs text-text-muted dark:text-text-dark-muted tabular-nums">
                    {formatElapsed(elapsedSec)}
                  </span>
                  <span className="thinking-dots text-text-muted dark:text-text-dark-muted ml-0.5" aria-hidden="true">
                    <span /><span /><span />
                  </span>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      <div className="shrink-0">
        {isAingelChat && (
          <div className="px-3 py-0.5 text-xs text-text-faint italic">
            Guide chat: model is fixed
          </div>
        )}

        {chat.task_id && perms.canEdit && (
          <div className="px-3 py-1">
            <button
              data-tip="Promote reply to project memory"
              className="py-0.5 px-2 border border-default rounded-sm bg-surface-raised text-xs cursor-pointer text-text-muted dark:bg-surface-dark-raised dark:border-border-dark-default dark:text-text-dark-muted"
              onClick={handlePromote}
              disabled={promoting}
            >
              {promoting ? 'Promoting...' : 'Promote to memory'}
            </button>
          </div>
        )}

        {estimate && (
          <div className="px-3 py-0.5 text-xs text-text-soft">
            Est. ~{(estimate.total_input_tokens + estimate.estimated_output_tokens).toLocaleString()} tokens
            {estimate.cost_min_usd != null && ` · ${fmtUsd(estimate.cost_min_usd)}`}
            {estimate.cost_min_usd != null &&
              estimate.cost_max_usd != null &&
              estimate.cost_max_usd !== estimate.cost_min_usd &&
              `–${fmtUsd(estimate.cost_max_usd)}`}
          </div>
        )}

        {sendError && (
          <div className="px-3 py-1 text-sm+ text-danger" role="alert">
            Send failed: {sendError}
          </div>
        )}

        <div className="chat-input-area pt-2 px-3 border-t border-border-muted flex gap-1.5">
          <textarea
            data-tip="Type a message. Enter to send, Shift+Enter for newline"
            ref={textareaRef}
            value={inputText}
            onChange={(e) => {
              setInputText(e.target.value);
              handleInput(e);
              debounceEstimate(e.target.value);
            }}
            onKeyDown={handleKeyDown}
            placeholder={!perms.canEdit ? 'Read-only — you do not have permission to send messages' : isSending ? `AI is thinking… (${formatElapsed(elapsedSec)})` : 'Type a message...'}
            disabled={isSending || !perms.canEdit}
            rows={1}
            className="flex-1 py-1.5 px-2.5 border border-default rounded text-sm resize-none leading-relaxed dark:bg-surface-dark-base dark:border-border-dark-default dark:text-text-dark"
          />
          {perms.canEdit && (
            <button
              data-tip="Send message"
              onClick={handleSend}
              disabled={isSending || !inputText.trim()}
              className="chat-input-send py-1.5 px-3.5 bg-accent text-white border-none rounded text-sm cursor-pointer disabled:opacity-60 dark:bg-accent-dark inline-flex items-center gap-1.5"
            >
              {isSending ? <LoaderCircle size={14} className="animate-spin" /> : <Send size={14} />}
              Send
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

export const ChatPanel = () => {
  const activeChat = useStore((s) => s.activeChat);
  if (!activeChat) return null;
  return <ChatPanelInner key={activeChat.id} />;
};

export default ChatPanel;
