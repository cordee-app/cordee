import { useState, useEffect, useCallback } from 'react';
import { api, ApiError } from '../api';
import type { TermsDoc } from '../types';
import { renderMarkdown } from '../utils/markdown';
import { Spinner } from './Spinner';

interface TermsGateProps {
  /** Tag of the version the server says is pending (display only). */
  versionTag: string;
  /** Called after a successful accept — the caller reloads the app state. */
  onAccepted: () => Promise<void> | void;
}

/**
 * Full-screen, non-dismissable Terms & Conditions acceptance page. Shown by
 * App whenever the store's `termsRequired` flag is set (either from the
 * `/api/me` poll or an instant `aingel:terms-required` event on a 403).
 *
 * There is deliberately no close button, backdrop click or Escape handler:
 * every gated endpoint 403s until the current version is accepted, so the
 * only ways out are Accept or log out.
 */
export const TermsGate = ({ versionTag, onAccepted }: TermsGateProps) => {
  const [doc, setDoc] = useState<TermsDoc | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [accepting, setAccepting] = useState(false);
  const [acceptError, setAcceptError] = useState('');

  const fetchDoc = useCallback(() => {
    setLoading(true);
    setLoadError('');
    api.terms.getCurrent()
      .then((res) => setDoc(res.terms))
      .catch((err: unknown) => {
        setDoc(null);
        setLoadError(err instanceof Error ? err.message : 'Could not load the Terms and Conditions.');
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { fetchDoc(); }, [fetchDoc]);

  const handleAccept = async () => {
    if (!doc) return;
    setAccepting(true);
    setAcceptError('');
    try {
      // Always accept the version we are displaying — the store flag and the
      // fetched doc can diverge if a new version was published meanwhile.
      await api.terms.accept(doc.version_id);
      await onAccepted();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 409 && err.body?.terms) {
        // The published version changed while the user was reading: reload
        // and show the new text (accepting the stale id is refused).
        fetchDoc();
      } else {
        setAcceptError(err instanceof Error ? err.message : 'Could not accept the Terms and Conditions.');
      }
      setAccepting(false);
    }
  };

  // Same flow as UserMenu's logout: end the Authentik session when the server
  // hands back an end_session_url, otherwise land on the logged-out page.
  const handleDecline = async () => {
    try {
      const res = await api.auth.logout();
      if (res.end_session_url) {
        window.location.href = res.end_session_url;
      } else {
        window.location.href = '/?loggedout=1';
      }
    } catch {
      window.location.href = '/?loggedout=1';
    }
  };

  const shownTag = doc?.tag || versionTag;

  return (
    <div className="fixed inset-0 z-modal flex items-center justify-center bg-surface-muted dark:bg-surface-dark-muted p-4 overflow-y-auto">
      <div className="w-full max-w-3xl bg-white dark:bg-neutral-900 border border-border-muted dark:border-border-dark-muted rounded-lg shadow-strong p-6 flex flex-col gap-4">
        <div>
          <h1 className="m-0 text-lg font-heading text-ink dark:text-text-dark-DEFAULT">
            {doc?.title || 'Terms and Conditions'}
          </h1>
          <div className="text-xs text-text-faint dark:text-text-dark-faint mt-1">
            Version {shownTag || '—'}
          </div>
        </div>

        <p className="m-0 text-sm text-text-soft dark:text-text-dark-soft">
          By accepting, you agree to the Terms and Conditions below. You must accept to continue using Cordée.
        </p>

        <div className="max-h-[60vh] overflow-y-auto rounded-md border border-border-muted dark:border-border-dark-muted bg-surface-raised dark:bg-surface-dark-raised p-4 text-sm text-text dark:text-text-dark-DEFAULT">
          {loading ? (
            <div className="flex items-center justify-center gap-2 py-8 text-text-faint">
              <Spinner size={16} /> Loading the Terms and Conditions…
            </div>
          ) : loadError ? (
            <div className="flex flex-col items-start gap-3 py-4">
              <div className="text-sm text-dangerStrong">{loadError}</div>
              <button
                data-tip="Reload the Terms and Conditions"
                className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white"
                onClick={fetchDoc}
              >
                Try again
              </button>
            </div>
          ) : doc ? (
            <div
              className="terms-body space-y-2 [&_a]:text-accent [&_a]:underline [&_code]:px-1 [&_code]:rounded [&_code]:bg-surface-subtle [&_code]:font-mono [&_h1]:text-base [&_h1]:font-semibold [&_h2]:text-sm [&_h2]:font-semibold [&_h3]:text-sm [&_h3]:font-semibold [&_li]:ml-5 [&_li]:list-disc [&_ol_li]:list-decimal"
              dangerouslySetInnerHTML={{ __html: renderMarkdown(doc.content) }}
            />
          ) : (
            <div className="py-4 text-text-faint">No Terms and Conditions are currently published.</div>
          )}
        </div>

        {acceptError ? (
          <div className="text-sm text-dangerStrong">{acceptError}</div>
        ) : null}

        <div className="flex items-center justify-end gap-2 pt-1">
          <button
            data-tip="Log out without accepting the Terms and Conditions"
            className="btn py-[7px] px-[18px] border border-border-muted dark:border-border-dark-muted rounded cursor-pointer text-md- bg-transparent text-text-soft dark:text-text-dark-soft hover:bg-surface-subtle dark:hover:bg-surface-dark-subtle"
            onClick={handleDecline}
            disabled={accepting}
          >
            Decline and log out
          </button>
          <button
            data-tip="Accept the Terms and Conditions and continue"
            className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-2"
            onClick={handleAccept}
            disabled={!doc || accepting}
          >
            {accepting && <Spinner size={14} />} Accept
          </button>
        </div>
      </div>
    </div>
  );
};

export default TermsGate;
