"""BowlersNetwork render node runner.

Renders the studio library on an idle PC, uploads each finished item through the backend API, registers it, and
deletes it locally. Controlled with `bn status`, `bn pause` and `bn resume`; everything else is automatic.
"""

VERSION = '1.0'
USER_AGENT = 'bn-render-node/1.0'
