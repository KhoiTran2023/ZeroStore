"""What agents read: the server instructions and every tool description.

Agents decide on their own when to call a tool, based on what's in their
context, and left alone they tend to guess instead of asking. So these texts
say *when* to call ZeroStore, not just what each tool does. They are written
once here and shared by every agent and transport.
"""

SERVER_NAME = "ZeroStore"

SERVER_INSTRUCTIONS = (
    "This is your company's current knowledge. Check it before stating company facts. "
    "If it says 'not sure', ask the user."
)

# The one-line rule `zerostore init` and `zerostore connect` offer to add to
# CLAUDE.md, AGENTS.md or .cursorrules.
AGENT_RULE = "Check ZeroStore before assuming anything about the company."

SEARCH = (
    "Look up current company decisions, policies, pricing, customers and context. "
    "Use this before answering or acting on anything about the company, its product or past decisions, "
    "instead of assuming. Returns a few short facts, each with its source document and last-updated date, "
    "or 'not sure' when nothing reliable matches; in that case ask the user rather than guessing."
)

GET_FACT = (
    "Fetch one fact by the id a search result gave you, with its full text, source link and "
    "last-updated date. Use it to re-check or quote a fact you found earlier."
)

FETCH = (
    "Fetch the full text of one search result by its id, with its title, source link and last-updated "
    "date. Same as get_fact, in the shape ChatGPT's search and deep research expect."
)

WHATS_CHANGED = (
    "List what changed in company knowledge since a date (e.g. '2026-10-01') or a span (e.g. '7d', '24h'). "
    "Use this at the start of a task you last worked on a while ago, or when the user asks what's new, "
    "so you don't act on facts that have since changed."
)

LIST_SOURCES = (
    "List the documents ZeroStore knows about, with their source and last-updated date. "
    "Use it to see what company knowledge is available before searching, or to cite where a fact lives."
)

PROPOSE_FACT = (
    "Suggest a new company fact you learned in this conversation, such as a decision the user just made. "
    "Include the line from the conversation that it came from. A person must approve it before it becomes "
    "a fact; you cannot approve it yourself. Don't use this for guesses or for things already in ZeroStore."
)

# Setup tools (owner tokens only)

ADD_FACT = (
    "Add a company fact to ZeroStore, such as a decision the user just made. Your access only lets you "
    "change some kinds of information: pass `tag` for the area it belongs to (leave it out for general "
    "information). If the change isn't allowed, ZeroStore says so; tell the user rather than retrying. "
    "Search first so you don't add something that's already there; use change_fact to update a fact."
)
CHANGE_FACT = (
    "Replace what a fact says, by its id from search, when the user tells you it's wrong or out of date. "
    "Every agent is told the new text from then on. Only works for information your access lets you change."
)
REMOVE_FACT = (
    "Remove a fact, by its id from search, when the user says it no longer holds. Agents stop being told it. "
    "A fact from a document stays in the document; ZeroStore stops serving it until that document changes."
)
TOKEN_CREATE = (
    "Owner only. Create an access token for an agent or person. Levels: 'default' (non-confidential facts), "
    "'confidential' (all facts), 'owner' (all facts plus setup). An optional `role` from the settings "
    "decides which tagged facts a default or confidential token sees. The token is shown once."
)
TOKEN_LIST = "Owner only. List active access tokens with their names, levels, roles and last use."
TOKEN_SET_ROLE = (
    "Owner only. Give a default or confidential token another role from the settings, or none "
    "(untagged facts only). Takes effect on the token's next call."
)
TOKEN_REVOKE = "Owner only. Revoke an access token by its id; agents using it lose access immediately."
REVIEW_LIST = (
    "Owner only. List items waiting for a person's decision: low-confidence changes, "
    "deleted files the librarian wasn't sure about, and facts agents proposed. Each item has `before` "
    "(what agents are told now) and `after` (what they'd be told if approved)."
)
REVIEW_DECIDE = (
    "Owner only. Approve or reject a review item by its id. For an agent proposal, `fact` may "
    "replace the proposed wording before it is saved."
)
SYNC_NOW = (
    "Owner only. Check every connected source for changes now instead of waiting for the schedule, "
    "and report what changed."
)
STATUS = (
    "Owner only. How ZeroStore is doing: sources and document counts, the last sync and what it changed, "
    "what waits for review, tokens and how often each asked in the last day, and two weeks of daily numbers."
)
ACTIVITY = (
    "Owner only. The most recent changes ZeroStore made (and why) and the requests agents sent, newest "
    "first. `kind` is 'all', 'changes' or 'questions'."
)
