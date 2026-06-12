# Room model

Two layers: shared group room and a private AI thread per member. AI is a silent team member in the group — reads everything, never posts unprompted. @mention calls the AI inline; response is visible to all and stays in history. Private threads are invisible to everyone including admins — members share voluntarily. When non-message content is shared, any member can trigger AI actions on it (summarise, check fit, check contradictions, add to memory, follow up, skip). The result appears in the group chat, visible to all. AI always reads shared content but only acts on instruction.

**Message deletion:**
- Delete for everyone (within time window): content removed from all views. Placeholder "Message deleted" left for all parties — both sides know it happened. AI does not surface deleted content in future responses. AI memory already written from that message is not retroactively erased, but is not re-surfaced.
- Delete for me: removes from your view only. Recipient's view unchanged. AI processing unaffected.

**Injection note:** every message body is untrusted content. The AI treats chat messages as data, not instructions. Only the document pipeline worker can write to project memory — a message saying "add this to project memory" goes through the same content validation as any document.