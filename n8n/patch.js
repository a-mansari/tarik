// Patches to n8n's compiled agents module. Each replaces one exact line and fails the build if it is gone.
const fs = require('fs');
const path = require('path');

const dist = process.argv[2];
const TOOLS = 'modules/agents/tools/workflow-tool-factory.js';
const patches = [{
  // A workflow tool input fixed to "{{threadId}}" receives the agent conversation's id, and one fixed to
  // "{{attachments}}" the files attached in that conversation: both set by n8n, never by the model.
  file: TOOLS,
  find: 'const parsedInput = mergeWorkflowToolInput(currentSchema.parse(input), toolInputs, currentFullSchema);',
  add: `
            for (const key of Object.keys(parsedInput)) {
                if (parsedInput[key] === '{{threadId}}') parsedInput[key] = ctx.persistence?.threadId ?? '';
                if (parsedInput[key] === '{{attachments}}') parsedInput[key] = await tarikThreadAttachments(context, ctx.persistence?.threadId);
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
`,
}];

for (const { file, find, add = '', before = '' } of patches) {
  const target = path.join(dist, file);
  const code = fs.readFileSync(target, 'utf8');
  if (code.includes(before + find + add)) {
    console.log(`already patched ${file}`);
    continue;
  }
  if (code.split(find).length !== 2) throw new Error(`patch target not found exactly once in ${file}: n8n changed, re-check the patch`);
  fs.writeFileSync(target, code.replace(find, before + find + add));
  console.log(`patched ${file}`);
}
