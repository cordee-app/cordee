interface SpinnerProps {
  size?: number;
  color?: string;
  title?: string;
}

export const Spinner = ({ size = 12, color = 'currentColor', title = 'Running' }: SpinnerProps) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    role="img"
    aria-label={title}
    style={{ display: 'inline-block', verticalAlign: 'middle' }}
  >
    <title>{title}</title>
    <circle
      cx="12"
      cy="12"
      r="9"
      stroke={color}
      strokeWidth="3"
      strokeDasharray="42 14"
      strokeLinecap="round"
      className="animate-spin"
      style={{ transformOrigin: 'center' }}
    />
  </svg>
);

export default Spinner;