// Patches to n8n's compiled agents module. Each adds to (or replaces) one exact line and fails the build if it is gone.
const fs = require('fs');
const path = require('path');

const dist = process.argv[2];
const TOOLS = 'modules/agents/tools/workflow-tool-factory.js';
const MCP_AGENTS = 'modules/mcp/tools/agents/agent-tools.service.js';
const AGENTS_SDK = '../node_modules/@n8n/agents/dist'; // the agents package, next to n8n's dist
const AGENT = `${AGENTS_SDK}/sdk/agent.js`;
const RUNTIME = `${AGENTS_SDK}/runtime/loop/agent-runtime.js`;
const patches = [{
  // An agent's workflow tool is stopped after TARIK_AGENT_TOOL_TIMEOUT_MS (docker-compose.yml) instead of n8n's fixed
  // 2 minutes: a FEC import or several invoices fit in one run. Tarik's own run budgets stay under it.
  file: TOOLS,
  find: 'const DEFAULT_TIMEOUT_MS = 120_000;',
  replace: 'const DEFAULT_TIMEOUT_MS = Number(process.env.TARIK_AGENT_TOOL_TIMEOUT_MS) || 120_000;',
}, {
  // n8n's MCP call_agent may start a conversation under an id of the caller's (newSession): an email
  // thread's conversation then has the thread's own id, under which its files are already saved.
  file: MCP_AGENTS,
  find: "        sessionId: zod_1.z.string().trim().min(1).optional(),",
  add: "\n        newSession: zod_1.z.literal(true).optional(),",
}, {
  file: MCP_AGENTS,
  find: "                    sessionId: request.sessionId,",
  add: "\n                    newSession: request.newSession,",
}, {
  // A workflow tool input fixed to "{{threadId}}" receives the agent conversation's id, one fixed to
  // "{{attachments}}" the files attached in that conversation, and one fixed to "{{clientMessage}}" the
  // client's latest message in it, word for word: all set by n8n, never by the model.
  file: TOOLS,
  find: 'const parsedInput = mergeWorkflowToolInput(currentSchema.parse(input), toolInputs, currentFullSchema);',
  add: `
            for (const key of Object.keys(parsedInput)) {
                if (parsedInput[key] === '{{threadId}}') parsedInput[key] = ctx.persistence?.threadId ?? '';
                if (parsedInput[key] === '{{attachments}}') parsedInput[key] = await tarikThreadAttachments(context, ctx.persistence?.threadId);
                if (parsedInput[key] === '{{clientMessage}}') parsedInput[key] = await tarikClientMessage(ctx.persistence?.threadId);
            }`,
}, {
  file: TOOLS,
  find: '//# sourceMappingURL=workflow-tool-factory.js.map',
  before: `// The conversation's chat attachments as base64 of { thread_id, files: [{ id, file_name, mime_type, data }] },
// a single value free of commas. Empty without a conversation.
async function tarikThreadAttachments(context, threadId) {
    if (!threadId)
        return '';
    const service = di_1.Container.get(require('../agent-chat-attachment.service').AgentChatAttachmentService);
    const rows = await service.repository.findByThread(threadId, { projectId: context.projectId, agentId: context.agentId });
    const files = [];
    for (const row of rows) {
        const data = await service.binaryDataService.getAsBuffer({ id: row.binaryDataId, data: '', mimeType: row.mimeType });
        files.push({ id: row.id, file_name: row.fileName, mime_type: row.mimeType, data: data.toString('base64') });
    }
    return Buffer.from(JSON.stringify({ thread_id: threadId, files })).toString('base64');
}
// The conversation's latest client message, its text only. Empty without a conversation.
async function tarikClientMessage(threadId) {
    if (!threadId)
        return '';
    const messages = di_1.Container.get(require('../repositories/agent-message.repository').AgentMessageRepository);
    const latest = await messages.findOne({ where: { threadId, role: 'user' }, order: { createdAt: 'DESC' } });
    const parts = latest?.content?.content;
    if (typeof parts === 'string')
        return parts;
    return Array.isArray(parts) ? parts.filter((part) => part?.type === 'text').map((part) => part.text).join('\\n') : '';
}
`,
}, {
  // The agent's cabinet, from its Experio MCP server's address (/c/<cabinet>/mcp): the runtime saves its prompts there.
  file: AGENT,
  find: "            name: this.name,\n            model: modelConfig,",
  replace: "            name: this.name,\n            tarikCabinet: this.mcpClients.flatMap((client) => client.configs).map((cfg) => /\\/c\\/([a-z0-9_]+)\\/mcp/.exec(cfg.url ?? '')?.[1]).find(Boolean),\n            model: modelConfig,",
}, {
  // Each model call's prompt (system, messages, tools) is saved in the cabinet's tarik_prompt_calls and _parts, to see
  // what the agent was sent (prompts.py). Never blocks or fails the call; files are left out.
  file: RUNTIME,
  find: "        return { toolMap: tools.toolMap, modelCallContext };",
  before: "        tarikSavePrompt(this.config, options?.persistence?.threadId, prompt, tools.toolMap);\n",
}, {
  file: RUNTIME,
  find: '//# sourceMappingURL=agent-runtime.js.map',
  before: `let tarikPrompts;
// A file's bytes, or a very long unbroken string (base64), shown by its size.
function tarikPlain(key, value) {
    if (value?.type === 'Buffer' || value instanceof Uint8Array) return \`[file, \${value.data?.length ?? value.length} bytes]\`;
    if (typeof value === 'string' && value.length > 5000 && !value.includes(' ')) return \`[\${value.length} characters]\`;
    return value;
}
// One distinct piece of a prompt: its JSON, and the hash that names it (equal pieces are stored once).
function tarikPart(value) {
    const content = JSON.stringify(value, tarikPlain) ?? 'null';
    return { content, hash: require('crypto').createHash('sha256').update(content).digest('hex').slice(0, 32) };
}
function tarikSavePrompt(config, threadId, prompt, toolMap) {
    const cabinet = config.tarikCabinet;
    if (!cabinet || !threadId) return;
    try {
        tarikPrompts ??= new (require('pg').Pool)({
            host: process.env.DB_POSTGRESDB_HOST,
            user: process.env.DB_POSTGRESDB_USER,
            password: process.env.DB_POSTGRESDB_PASSWORD,
            database: process.env.TARIK_POSTGRES_DB,
            max: 2,
        });
        const tools = [...toolMap.values()].map((tool) => ({ name: tool.name, description: tool.description }));
        const [system, toolsPart, ...messages] = [prompt.system, tools, ...prompt.messages].map(tarikPart);
        const parts = new Map([system, toolsPart, ...messages].map((part) => [part.hash, part.content]));
        const days = Number(process.env.TARIK_PROMPTS_KEEP_DAYS) || 30;
        const schema = \`"\${cabinet}"\`;
        tarikPrompts.query(
            \`with new_parts as (
                insert into \${schema}.tarik_prompt_parts (hash, content)
                select hash, content::jsonb from unnest($1::text[], $2::text[]) as t(hash, content) on conflict do nothing)
            insert into \${schema}.tarik_prompt_calls (session_id, agent, system_hash, tools_hash, message_hashes)
            values ($3, $4, $5, $6, $7)\`,
            [[...parts.keys()], [...parts.values()], threadId, config.name, system.hash, toolsPart.hash, messages.map((part) => part.hash)],
        ).then(async () => {
            if (Math.random() >= 0.01) return;
            await tarikPrompts.query(\`delete from \${schema}.tarik_prompt_calls where created_at < now() - $1 * interval '1 day'\`, [days]);
            // A part younger than a day may belong to a call being saved right now.
            await tarikPrompts.query(\`delete from \${schema}.tarik_prompt_parts p where p.created_at < now() - interval '1 day' and not exists (
                select 1 from \${schema}.tarik_prompt_calls c where p.hash = c.system_hash or p.hash = c.tools_hash or p.hash = any(c.message_hashes))\`);
        }).catch((error) => console.error('tarik prompts not saved:', error.message));
    } catch (error) {
        console.error('tarik prompts not saved:', error.message);
    }
}
`,
}];

for (const { file, find, add = '', before = '', replace } of patches) {
  const target = path.join(dist, file);
  const code = fs.readFileSync(target, 'utf8');
  const patched = replace ?? before + find + add;
  if (code.includes(patched)) {
    console.log(`already patched ${file}`);
    continue;
  }
  if (code.split(find).length !== 2) throw new Error(`patch target not found exactly once in ${file}: n8n changed, re-check the patch`);
  fs.writeFileSync(target, code.replace(find, patched));
  console.log(`patched ${file}`);
}
