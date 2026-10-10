import { Schema } from '../model';
import { api } from './client';
import { useApi } from './useApi';

/** An agent's models as the page draws them: its state's schema, and each operation's receipt schema. */
export interface AgentSchemas {
  state?: Schema;
  receipts: Record<string, Schema>;
}

/** The schemas of one agent, read once per agent: the state from a runner that carries it, the receipts from its config. */
export function useSchemas(agent: string): AgentSchemas {
  const state = useApi(() => (agent ? api.schema(agent) : Promise.reject(new Error('no agent'))), `schema-${agent}`);
  const config = useApi(() => (agent ? api.config(agent) : Promise.reject(new Error('no agent'))), `config-schemas-${agent}`);
  const receipts =
    config.state === 'ok'
      ? Object.fromEntries(config.data.operations.flatMap((op) => (Schema.of(op.schema) ? [[op.name, Schema.of(op.schema) as Schema]] : [])))
      : {};
  return { state: state.state === 'ok' ? Schema.of(state.data.state) : undefined, receipts };
}
