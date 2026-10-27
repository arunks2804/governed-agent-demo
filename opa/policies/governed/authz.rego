# Central authorization policy for governed agent data access.
#
# Input (built by the trusted data service from the *verified* token and the
# *parsed* SQL - never from free-form caller input):
#   subject:  {sub, tenant, ctx}
#   action:   "list" | "describe" | "query"
#   resource: {dataset, tables, columns, requested_tenants}
#
# Static grants live in data.governed.config (data.json). Runtime changes
# (e.g. revoking a tenant's access) are written by the policy admin to
# data.governed_runtime through OPA's authenticated REST API and take effect on
# the next decision - the data service does not cache decisions.
package governed.authz

import rego.v1

cfg := data.governed.config

default revocations := {}

revocations := r if {
	r := data.governed_runtime.revocations
	is_object(r)
}

subject := object.get(input, "subject", {})

resource := object.get(input, "resource", {})

action := object.get(input, "action", "")

dataset := object.get(resource, "dataset", "")

sub := object.get(subject, "sub", null)

token_tenant := object.get(subject, "tenant", null)

user := cfg.users[sub]

valid_actions := {"list", "describe", "query"}

dataset_actions := {"describe", "query"}

is_nonempty_string(x) if {
	is_string(x)
	count(x) > 0
}

# ---------------------------------------------------------------- deny reasons

deny contains "malformed input: input must be an object" if not is_object(input)

deny contains "missing identity: subject.sub is required" if not is_nonempty_string(sub)

deny contains "missing identity: subject.tenant is required" if not is_nonempty_string(token_tenant)

deny contains sprintf("unsupported action %v", [action]) if not action in valid_actions

deny contains "unknown identity" if {
	is_nonempty_string(sub)
	not cfg.users[sub]
}

deny contains "identity tenant mismatch: token tenant does not match the identity directory" if {
	user.tenant != token_tenant
}

deny contains "tenant is disabled" if {
	user
	not cfg.tenants[user.tenant].active
}

deny contains sprintf("malformed input: dataset must be a string, got %v", [type_name(dataset)]) if {
	action in dataset_actions
	not is_string(dataset)
}

deny contains sprintf("unknown dataset %v", [dataset]) if {
	action in dataset_actions
	is_string(dataset)
	not cfg.datasets[dataset]
}

deny contains sprintf("identity is not granted %v on dataset %v", [action, dataset]) if {
	action in dataset_actions
	cfg.datasets[dataset]
	user
	not action in object.get(user.grants, dataset, [])
}

deny contains sprintf("access to dataset %v revoked for tenant %v by runtime policy", [dataset, user.tenant]) if {
	action in dataset_actions
	revoked(user.tenant, dataset, action)
}

deny contains "malformed input: resource.tables must be a non-empty array" if {
	action == "query"
	not valid_tables
}

deny contains sprintf("table %v is not part of dataset %v", [t, dataset]) if {
	action == "query"
	cfg.datasets[dataset]
	some t in object.get(resource, "tables", [])
	not t in cfg.datasets[dataset].tables
}

deny contains sprintf("cross-tenant request: query filters on tenant %v but caller belongs to %v", [t, user.tenant]) if {
	action == "query"
	some t in object.get(resource, "requested_tenants", [])
	t != user.tenant
}

valid_tables if {
	tables := object.get(resource, "tables", null)
	is_array(tables)
	count(tables) > 0
}

revoked(tenant, ds, act) if {
	some r in object.get(object.get(revocations, tenant, {}), ds, [])
	r in {act, "*"}
}

# -------------------------------------------------------------------- decision

allow if {
	count(deny) == 0
	user
}

visible_datasets contains ds if {
	allow
	action == "list"
	some ds, actions in user.grants
	"list" in actions
	not revoked(user.tenant, ds, "list")
}

default decision := {"allow": false, "reasons": ["default deny"], "db_role": null, "row_limit": 0}

decision := {
	"allow": true,
	"reasons": [],
	"db_role": db_role_for_action,
	"row_limit": row_limit,
	"visible_datasets": sort([d | some d in visible_datasets]),
	"policy_version": cfg.policy_version,
} if allow

decision := {
	"allow": false,
	"reasons": reasons,
	"db_role": null,
	"row_limit": 0,
	"policy_version": cfg.policy_version,
} if not allow

reasons := sorted if {
	count(deny) > 0
	sorted := sort([r | some r in deny])
} else := ["unknown identity"]

db_role_for_action := cfg.tenants[user.tenant].db_role if action == "query"

db_role_for_action := null if action != "query"

row_limit := cfg.datasets[dataset].row_limit if action == "query"

row_limit := 0 if action != "query"
