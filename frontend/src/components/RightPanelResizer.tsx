import { useStore } from '../store';

const MIN_WIDTH = 320;
const MAX_WIDTH = 900;

export const RightPanelResizer = () => {
  const rightPanelWidth = useStore((s) => s.rightPanelWidth);
  const setRightPanelWidth = useStore((s) => s.setRightPanelWidth);

  const onDown = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startW = rightPanelWidth;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    const move = (ev: MouseEvent) => {
      const delta = startX - ev.clientX;
      const w = Math.min(Math.max(startW + delta, MIN_WIDTH), MAX_WIDTH);
      setRightPanelWidth(w);
    };
    const up = () => {
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  };

  return (
    <div
      onMouseDown={onDown}
      className="absolute left-0 top-0 bottom-0 w-1.5 cursor-col-resize bg-transparent hover:bg-accent/40 active:bg-accent/60 z-[60]"
      title="Drag to resize"
    />
  );
};

export default RightPanelResizer;