import { useState, useRef, useEffect, useCallback } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { Search, LoaderCircle } from 'lucide-react';

interface SearchResult {
  chat_id: number;
  chat_name: string;
  project_name: string;
  snippet: string;
}

export const SearchModal = () => {
  const {
    showSearch, setShowSearch, setActiveChat,
  } = useStore();

  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (showSearch) {
      setQuery('');
      setResults([]);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [showSearch]);

  const handleSearch = useCallback(async (q: string) => {
    if (!q.trim()) {
      setResults([]);
      return;
    }
    setSearching(true);
    try {
      const res = await api.searchChats(q.trim());
      setResults(res.results || []);
    } catch (e) {
      console.error('Search failed', e);
      setResults([]);
    } finally {
      setSearching(false);
    }
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      handleSearch(query);
    }
    if (e.key === 'Escape') {
      setShowSearch(false);
    }
  };

  const handleSelect = async (chatId: number) => {
    setShowSearch(false);
    try {
      const full = await api.chats.get(chatId);
      setActiveChat(full);
    } catch (e) {
      console.error('Failed to load chat', e);
    }
  };

  const highlightMatch = (text: string, q: string): string => {
    if (!q.trim()) return text;
    const escaped = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const regex = new RegExp(`(${escaped})`, 'gi');
    return text.replace(regex, '<mark>$1</mark>');
  };

  if (!showSearch) return null;

  return (
    <div className="modal-backdrop search-backdrop fixed inset-0 bg-black/40 flex items-start justify-center z-modal pt-20" onClick={(e) => { if (e.target === e.currentTarget) setShowSearch(false); }}>
      <div className="search-modal bg-surface-raised rounded-lg w-[560px] max-w-[90vw] max-h-[60vh] flex flex-col overflow-hidden shadow-strong" onClick={(e) => e.stopPropagation()}>
        <div className="search-input-wrap flex gap-2 p-3 px-4 border-b border-border-muted dark:border-border-dark-muted">
          <div className="relative flex-1">
            <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-text-faint dark:text-text-dark-faint pointer-events-none" />
            <input
              data-tip="Search chat transcripts"
              ref={inputRef}
              type="text"
              className="search-input w-full py-2 pl-9 pr-3 border border-default rounded text-base outline-none focus:border-accent dark:border-border-dark-default dark:bg-surface-dark-base dark:text-text-dark"
              placeholder="Search chats... (Ctrl+K)"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={handleKeyDown}
            />
          </div>
          <button
            data-tip="Search chats"
            className="search-btn py-2 px-4 border border-accent rounded bg-accent text-white text-md- cursor-pointer hover:bg-accent-deep disabled:opacity-60 disabled:cursor-default dark:bg-accent-dark dark:border-accent-dark inline-flex items-center gap-1.5"
            onClick={() => handleSearch(query)}
            disabled={searching}
          >
            {searching ? <LoaderCircle size={14} className="animate-spin" /> : <Search size={14} />}
            Search
          </button>
        </div>

        {results.length > 0 && (
          <div className="search-results flex-1 overflow-y-auto py-2">
            {results.map((r) => (
              <div
                key={r.chat_id}
                className="search-result-item py-2.5 px-4 cursor-pointer border-b border-border-muted hover:bg-accent-soft"
                onClick={() => handleSelect(r.chat_id)}
              >
                <div className="search-result-title text-md- font-semibold mb-0.5">
                  {r.chat_name}
                  {r.project_name ? <span className="search-result-proj text-sm+ font-normal text-text-muted"> · {r.project_name}</span> : null}
                </div>
                <div
                  className="search-result-snippet text-sm+ leading-snug overflow-hidden text-ellipsis"
                  dangerouslySetInnerHTML={{
                    __html: highlightMatch(r.snippet, query),
                  }}
                />
              </div>
            ))}
          </div>
        )}

        {!searching && query.trim() && results.length === 0 && (
          <div className="search-empty p-5 text-center text-sm text-text-muted">No results found</div>
        )}
      </div>
    </div>
  );
};

export default SearchModal;
