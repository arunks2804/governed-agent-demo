# OPA API authorization (enabled with --authentication=token --authorization=basic).
#
# - The data service's token may only POST to the decision endpoint.
# - The policy-admin token (held by the web UI's admin console) may manage data
#   and read decisions. Nobody may replace policy modules over the API.
# - GET /health is open so liveness can be checked.
# Every other request, including unauthenticated ones, is denied.
package system.authz

import rego.v1

default allow := false

data_service_token := object.get(opa.runtime().env, "OPA_DATA_SERVICE_TOKEN", "")

admin_token := object.get(opa.runtime().env, "OPA_ADMIN_TOKEN", "")

token_ok(expected) if {
	count(expected) >= 32
	input.identity == expected
}

allow if {
	input.method == "GET"
	input.path == ["health"]
}

allow if {
	token_ok(data_service_token)
	input.method == "POST"
	input.path == ["v1", "data", "governed", "authz", "decision"]
}

# Admin: read/write runtime policy data only.
allow if {
	token_ok(admin_token)
	input.method in {"GET", "PUT", "PATCH", "DELETE"}
	input.path[0] == "v1"
	input.path[1] == "data"
	input.path[2] == "governed_runtime"
}

# Admin: evaluate decisions (used by the UI's policy panel and tests).
allow if {
	token_ok(admin_token)
	input.method in {"GET", "POST"}
	input.path == ["v1", "data", "governed", "authz", "decision"]
}
