import { X } from 'lucide-react';
import { useStore } from '../store';
import { api } from '../api';
import { useQuery } from '@tanstack/react-query';
import { cn } from '../utils/cn';

const PROVIDER_COLORS: Record<string, string> = {
  claude: '#c67139',
  mistral: '#b8742f',
  openai: '#3f7f6a',
  scaleway: '#7a5aa6',
  ollama: '#645c50',
};

function modelProvider(modelId: string): string | null {
  if (modelId.startsWith('claude-')) return 'claude';
  if (modelId.startsWith('mistral-') || modelId.startsWith('open-mistral') || modelId.startsWith('codestral') || modelId.startsWith('devstral')) return 'mistral';
  if ((modelId.startsWith('gpt-') || modelId.startsWith('o1') || modelId.startsWith('o3') || modelId.startsWith('o4')) && !modelId.startsWith('codex-')) return 'openai';
  if (modelId.startsWith('scw-')) return 'scaleway';
  if (modelId.startsWith('oll-')) return 'ollama';
  return null;
}

export const ProviderIndicators = () => {
  const { tasks, setReadiness, readiness: storedReadiness, user } = useStore();

  const isAdmin = user?.role === 'admin';

  useQuery({
    queryKey: ['readiness'],
    queryFn: async () => {
      const r = await api.config.readiness();
      setReadiness(r);
      return r;
    },
    staleTime: 30000,
    enabled: isAdmin || user === null,
  });

  const runningProviders = new Set<string>();
  for (const t of tasks) {
    if (t.status === 'running') {
      const p = modelProvider(t.model);
      if (p) runningProviders.add(p);
    }
  }

  const providers = [
    { key: 'claude', label: 'Claude', readyKey: 'claude_code' as const },
    { key: 'mistral', label: 'Mistral', readyKey: 'vibe' as const },
    { key: 'openai', label: 'OpenAI', readyKey: 'openai_api' as const },
    { key: 'scaleway', label: 'Scaleway', readyKey: 'scaleway' as const },
    { key: 'ollama', label: 'Ollama', readyKey: 'ollama' as const },
  ];

  return (
    <div className="provider-indicators flex items-center gap-1.5 ml-2">
      {providers.map(({ key, label, readyKey }) => {
        const active = runningProviders.has(key);
        // Tri-state: true/false when readiness is known (admins), undefined
        // when it isn't (non-admins never fetch readiness). Only assert
        // "unconfigured" on a known false — otherwise a working provider
        // shows a red X for users who simply can't see the readiness data.
        const configured = storedReadiness?.[readyKey];
        const color = PROVIDER_COLORS[key];

        return (
          <div
            key={key}
            className={cn(
              'provider-badge',
              'flex items-center gap-1 px-2 py-0.5 rounded-full text-sm transition-colors',
              active ? 'bg-white/[0.14]' : 'bg-white/[0.08]'
            )}
            title={`${label}: ${active ? 'Active' : 'Idle'}${configured === false ? ' (unconfigured)' : ''}`}
          >
            <span
              className={cn(
                'provider-dot',
                'w-[7px] h-[7px] rounded-full flex-shrink-0',
                active ? 'active animate-pulse' : 'idle'
              )}
              style={{ backgroundColor: active ? color : '#645c50' }}
            />
            <span
              className="provider-name whitespace-nowrap"
              style={{ color: active ? color : undefined }}
            >
              {label}
            </span>
            {!configured && configured !== undefined && (
              <span
                className="provider-unconfigured flex items-center text-[#a3402f] ml-px"
                title="Not configured"
              >
                <X size={10} strokeWidth={3} />
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
};

export default ProviderIndicators;