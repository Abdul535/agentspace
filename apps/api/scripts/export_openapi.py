"""Print the API's OpenAPI spec as JSON, without starting the server. Used by `pnpm gen:api`."""

import json

from agentspace.main import app

print(json.dumps(app.openapi(), indent=2))
