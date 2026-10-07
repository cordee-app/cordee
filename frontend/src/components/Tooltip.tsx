import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { cn } from '../utils/cn';

const DELAY_MS = 500;
const GAP = 8;
const MARGIN = 8;
const MAX_WIDTH = 260;

type Rect = Pick<DOMRect, 'top' | 'left' | 'width' | 'bottom'>;

type Tip = { text: string; rect: Rect };

type Pos = { left: number; top: number; placement: 'top' | 'bottom' };

function TooltipHost({ tip }: { tip: Tip }) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<Pos | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const placement: Pos['placement'] = tip.rect.top - GAP - h >= MARGIN ? 'top' : 'bottom';
    const rawTop = placement === 'top' ? tip.rect.top - GAP - h : tip.rect.bottom + GAP;
    const rawLeft = tip.rect.left + tip.rect.width / 2 - w / 2;
    setPos({
      left: Math.min(Math.max(rawLeft, MARGIN), Math.max(MARGIN, vw - w - MARGIN)),
      top: Math.min(Math.max(rawTop, MARGIN), Math.max(MARGIN, vh - h - MARGIN)),
      placement,
    });
  }, [tip]);

  return createPortal(
    <div
      ref={ref}
      role="tooltip"
      style={{
        position: 'fixed',
        left: pos ? pos.left : -9999,
        top: pos ? pos.top : -9999,
        maxWidth: MAX_WIDTH,
        visibility: pos ? 'visible' : 'hidden',
      }}
      className={cn(
        'pointer-events-none z-[10000] whitespace-pre-line rounded-md border border-border/70 bg-ink px-2.5 py-1.5 text-xs font-medium text-white shadow-strong',
        'dark:border-border-dark-DEFAULT dark:bg-surface-dark-raised dark:text-text-dark-DEFAULT',
      )}
    >
      {tip.text}
    </div>,
    document.body,
  );
}

export function TooltipProvider({ children }: { children: ReactNode }) {
  const [tip, setTip] = useState<Tip | null>(null);
  const timerRef = useRef<number | null>(null);
  const sourceRef = useRef<Element | null>(null);

  useEffect(() => {
    const cancelTimer = () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };

    const hide = () => {
      cancelTimer();
      sourceRef.current = null;
      setTip(null);
    };

    const show = (el: Element) => {
      const text = (el.getAttribute('data-tip') || '').trim();
      if (!text) {
        hide();
        return;
      }
      const r = el.getBoundingClientRect();
      setTip({
        text,
        rect: { top: r.top, left: r.left, width: r.width, bottom: r.bottom },
      });
    };

    const schedule = (el: Element) => {
      if (sourceRef.current === el) return;
      hide();
      sourceRef.current = el;
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null;
        if (sourceRef.current === el && el.isConnected) show(el);
      }, DELAY_MS);
    };

    const findTarget = (node: EventTarget | null): Element | null =>
      node instanceof Element ? node.closest('[data-tip]') : null;

    const onMouseOver = (e: MouseEvent) => {
      const el = findTarget(e.target);
      if (!el) {
        if (sourceRef.current) hide();
        return;
      }
      schedule(el);
    };

    const onMouseOut = (e: MouseEvent) => {
      const src = sourceRef.current;
      if (!src) return;
      const related = e.relatedTarget;
      if (related instanceof Node && src.contains(related)) return;
      if (findTarget(related) === src) return;
      hide();
    };

    const onFocusIn = (e: FocusEvent) => {
      const el = findTarget(e.target);
      if (el) schedule(el);
      else if (sourceRef.current) hide();
    };

    const onFocusOut = (e: FocusEvent) => {
      const related = e.relatedTarget;
      if (sourceRef.current && !(related instanceof Node && sourceRef.current.contains(related))) hide();
    };

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') hide();
    };

    document.addEventListener('mouseover', onMouseOver, true);
    document.addEventListener('mouseout', onMouseOut, true);
    document.addEventListener('focusin', onFocusIn, true);
    document.addEventListener('focusout', onFocusOut, true);
    document.addEventListener('mousedown', hide, true);
    document.addEventListener('keydown', onKeyDown, true);
    window.addEventListener('scroll', hide, true);
    window.addEventListener('resize', hide);

    return () => {
      cancelTimer();
      document.removeEventListener('mouseover', onMouseOver, true);
      document.removeEventListener('mouseout', onMouseOut, true);
      document.removeEventListener('focusin', onFocusIn, true);
      document.removeEventListener('focusout', onFocusOut, true);
      document.removeEventListener('mousedown', hide, true);
      document.removeEventListener('keydown', onKeyDown, true);
      window.removeEventListener('scroll', hide, true);
      window.removeEventListener('resize', hide);
    };
  }, []);

  return (
    <>
      {children}
      {tip ? <TooltipHost tip={tip} /> : null}
    </>
  );
}
