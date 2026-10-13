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

TOKEN_CREATE = (
    "Owner only. Create an access token for an agent or person. Levels: 'default' (non-confidential facts), "
    "'confidential' (all facts), 'owner' (all facts plus setup). The token is shown once."
)
TOKEN_LIST = "Owner only. List active access tokens with their names, levels and last use."
TOKEN_REVOKE = "Owner only. Revoke an access token by its id; agents using it lose access immediately."
REVIEW_LIST = (
    "Owner only. List items waiting for a person's decision: "
    "low-confidence changes and facts agents proposed."
)
REVIEW_DECIDE = "Owner only. Approve or reject a review item by its id."
