/**
 * Tiny, dependency-free Markdown → HTML renderer for the Terms & Conditions
 * body. Deliberately minimal: the T&C is plain prose with headings, lists,
 * bold/italic, inline code and links — nothing more.
 *
 * SECURITY: this output is fed to dangerouslySetInnerHTML, so every input
 * character is HTML-escaped FIRST (same convention as ChatPanel's escHtml).
 * Raw HTML in the source therefore renders as literal text. Link hrefs are
 * additionally protocol-whitelisted (see safeHref) so a `javascript:` or
 * `data:` URL can never reach the DOM.
 */

/** Escape the HTML-significant characters. Always the first transform. */
function escHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Return a safe href for an already-escaped URL, or null when the scheme is
 * not whitelisted. Only http/https/mailto and relative paths (`/…`, `#…`)
 * are allowed; everything else degrades to plain text at the call site.
 */
function safeHref(escapedUrl: string): string | null {
  // The escaped form is what ends up in the attribute, so validate that form.
  // `&` becomes `&amp;` but never affects the scheme prefix.
  const u = escapedUrl.trim();
  if (/^(https?|mailto):/i.test(u)) return u;
  // Relative paths only: a single leading `/` or a `#fragment`. A leading
  // `//` is protocol-relative — i.e. an external network URL — so reject it.
  if (/^\/(?!\/)/.test(u) || u.startsWith('#')) return u;
  return null;
}

/** Inline spans: bold, italic, code, links. Input must already be escaped. */
function inlineMd(escaped: string): string {
  let s = escaped;
  // Links first so their label/URL text is not mangled by the emphasis rules.
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (whole, label: string, url: string) => {
    const href = safeHref(url);
    if (!href) return whole; // unsafe scheme → leave as literal text
    return `<a href="${href}" target="_blank" rel="noopener noreferrer nofollow">${label}</a>`;
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  return s;
}

/** Render a Markdown string to a safe HTML fragment. */
export function renderMarkdown(md: string): string {
  const lines = (md || '').replace(/\r\n?/g, '\n').split('\n');
  const out: string[] = [];
  let para: string[] = [];
  let list: { tag: 'ul' | 'ol'; items: string[] } | null = null;

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${para.map(inlineMd).join('<br>')}</p>`);
      para = [];
    }
  };
  const flushList = () => {
    if (list) {
      out.push(`<${list.tag}>${list.items.map((i) => `<li>${i}</li>`).join('')}</${list.tag}>`);
      list = null;
    }
  };
  const flushAll = () => { flushPara(); flushList(); };

  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) { flushAll(); continue; }

    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      flushAll();
      const level = heading[1].length;
      out.push(`<h${level}>${inlineMd(escHtml(heading[2]))}</h${level}>`);
      continue;
    }

    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    if (bullet) {
      flushPara();
      if (!list || list.tag !== 'ul') { flushList(); list = { tag: 'ul', items: [] }; }
      list.items.push(inlineMd(escHtml(bullet[1])));
      continue;
    }

    const ordered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (ordered) {
      flushPara();
      if (!list || list.tag !== 'ol') { flushList(); list = { tag: 'ol', items: [] }; }
      list.items.push(inlineMd(escHtml(ordered[1])));
      continue;
    }

    // Plain text: accumulate into the current paragraph, closing any list.
    flushList();
    para.push(escHtml(line));
  }
  flushAll();
  return out.join('\n');
}
