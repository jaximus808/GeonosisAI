import { LlmBackendSettings } from '@/components/llm/LlmBackendSettings';
import { webIsProduction } from '@/lib/llmbackend/server';

export const dynamic = 'force-dynamic';

// Operator settings (local/dev). The model backend switch is not rendered in production: the
// orchestrator locks it to the Claude API there and the proxy refuses changes.
export default function OpsSettingsPage() {
  const production = webIsProduction();
  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <div>
        <h1 className="mb-1 text-2xl font-semibold tracking-tight">Operator settings</h1>
        <p className="text-sm text-zinc-500">Local and dev controls for the orchestrator.</p>
      </div>
      {production ? (
        <p className="text-sm text-zinc-600 dark:text-zinc-400">Production: the LLM backend is locked to the Claude API.</p>
      ) : (
        <LlmBackendSettings />
      )}
    </div>
  );
}
