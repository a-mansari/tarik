# n8n with Tarik's patches to the native agents (see patch.js). Keep the version pinned:
# an upgrade must re-check that each patch still applies (the build fails if one does not).
FROM docker.n8n.io/n8nio/n8n:2.43.5
USER root
COPY patch.js /tmp/patch.js
RUN node /tmp/patch.js /usr/local/lib/node_modules/n8n/dist && rm /tmp/patch.js
USER node
