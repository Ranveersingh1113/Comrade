# Action consent flow

AI surfaces proposed actions in the requesting member's private thread with a preview of exactly what it will do. Member approves, edits, or cancels. AI acts as itself — never attributed to the member who triggered it.

**Requires consent:** group posts, task creation, any message delivered on behalf of a specific member. These are gated, shown with literal tool name + arguments + source snippet, and verified by hash before execution.

**No consent needed:** AI nudges sent to a member's private thread. The AI is the sender acting in its own role — not acting on behalf of anyone. Requiring consent for each nudge is time-wasting and defeats the point. Members are informed during onboarding that the AI will send autonomous private nudges. Also exempt: memory updates, deadline tracking, document summaries (AI is the author, not a proxy for a member).

**Permission screen design:** show the literal action, not a natural-language summary. Claude Code's classifier misses 17% of cases a careful human would catch — the consent card exists to catch those. Tier the interrupt level to the risk: don't prompt for everything, but make high-stakes cards genuinely readable.