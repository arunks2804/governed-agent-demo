package governed.authz_test

import rego.v1

import data.governed.authz

alice := {"sub": "alice@acme.example", "tenant": "acme", "ctx": "agent"}

nina := {"sub": "nina@northstar.example", "tenant": "northstar", "ctx": "agent"}

victor := {"sub": "victor@acme.example", "tenant": "acme", "ctx": "agent"}

q(subject, extra) := object.union(
	{
		"subject": subject,
		"action": "query",
		"resource": {"dataset": "orders", "tables": ["orders"], "columns": ["amount"], "requested_tenants": []},
	},
	extra,
)

# ------------------------------------------------------------- allowed paths

test_alice_query_allowed_with_acme_role if {
	d := authz.decision with input as q(alice, {})
	d.allow == true
	d.db_role == "acme_reader"
	d.row_limit == 200
}

test_nina_query_allowed_with_northstar_role if {
	d := authz.decision with input as q(nina, {})
	d.allow == true
	d.db_role == "northstar_reader"
}

test_query_own_tenant_literal_allowed if {
	d := authz.decision with input as q(alice, {"resource": {"dataset": "orders", "tables": ["orders"], "requested_tenants": ["acme"]}})
	d.allow == true
}

test_list_returns_visible_datasets_without_db_role if {
	d := authz.decision with input as {"subject": alice, "action": "list", "resource": {}}
	d.allow == true
	d.visible_datasets == ["orders"]
	d.db_role == null
}

test_describe_allowed if {
	d := authz.decision with input as {"subject": nina, "action": "describe", "resource": {"dataset": "orders"}}
	d.allow == true
}

test_runner_context_allowed if {
	d := authz.decision with input as q(object.union(alice, {"ctx": "runner"}), {})
	d.allow == true
}

# ------------------------------------------------------------- denied paths

test_cross_tenant_literal_denied if {
	d := authz.decision with input as q(alice, {"resource": {"dataset": "orders", "tables": ["orders"], "requested_tenants": ["northstar"]}})
	d.allow == false
	some r in d.reasons
	contains(r, "cross-tenant")
}

test_mixed_tenant_literals_denied if {
	d := authz.decision with input as q(nina, {"resource": {"dataset": "orders", "tables": ["orders"], "requested_tenants": ["acme", "northstar"]}})
	d.allow == false
}

test_viewer_cannot_query if {
	d := authz.decision with input as q(victor, {})
	d.allow == false
	some r in d.reasons
	contains(r, "not granted query")
}

test_viewer_can_describe if {
	d := authz.decision with input as {"subject": victor, "action": "describe", "resource": {"dataset": "orders"}}
	d.allow == true
}

test_tenant_mismatch_denied if {
	forged := {"sub": "alice@acme.example", "tenant": "northstar", "ctx": "agent"}
	d := authz.decision with input as q(forged, {})
	d.allow == false
	some r in d.reasons
	contains(r, "tenant mismatch")
}

test_unknown_identity_denied if {
	d := authz.decision with input as q({"sub": "mallory@evil.example", "tenant": "acme"}, {})
	d.allow == false
	"unknown identity" in d.reasons
}

test_missing_subject_denied if {
	d := authz.decision with input as {"action": "query", "resource": {"dataset": "orders", "tables": ["orders"]}}
	d.allow == false
	some r in d.reasons
	contains(r, "missing identity")
}

test_empty_sub_denied if {
	d := authz.decision with input as q({"sub": "", "tenant": "acme"}, {})
	d.allow == false
}

test_unknown_dataset_denied if {
	d := authz.decision with input as q(alice, {"resource": {"dataset": "payroll", "tables": ["payroll"]}})
	d.allow == false
	some r in d.reasons
	contains(r, "unknown dataset")
}

test_unapproved_table_denied if {
	d := authz.decision with input as q(alice, {"resource": {"dataset": "orders", "tables": ["orders", "billing_accounts"]}})
	d.allow == false
	some r in d.reasons
	contains(r, "billing_accounts")
}

test_unknown_action_denied if {
	d := authz.decision with input as {"subject": alice, "action": "delete", "resource": {"dataset": "orders", "tables": ["orders"]}}
	d.allow == false
}

test_malformed_tables_denied if {
	d := authz.decision with input as q(alice, {"resource": {"dataset": "orders", "tables": "orders"}})
	d.allow == false
}

test_malformed_dataset_type_denied if {
	d := authz.decision with input as q(alice, {"resource": {"dataset": ["orders"], "tables": ["orders"]}})
	d.allow == false
}

test_non_object_input_denied if {
	d := authz.decision with input as "SELECT * FROM orders"
	d.allow == false
}

test_empty_input_denied if {
	d := authz.decision with input as {}
	d.allow == false
}

# ------------------------------------------------------- runtime revocation

test_runtime_revocation_denies_query if {
	d := authz.decision with input as q(alice, {})
		with data.governed_runtime as {"revocations": {"acme": {"orders": ["query"]}}}
	d.allow == false
	some r in d.reasons
	contains(r, "revoked")
}

test_runtime_revocation_is_tenant_scoped if {
	d := authz.decision with input as q(nina, {})
		with data.governed_runtime as {"revocations": {"acme": {"orders": ["*"]}}}
	d.allow == true
}

test_wildcard_revocation_hides_dataset_from_list if {
	d := authz.decision with input as {"subject": alice, "action": "list", "resource": {}}
		with data.governed_runtime as {"revocations": {"acme": {"orders": ["*"]}}}
	d.visible_datasets == []
}

test_disabled_tenant_denied if {
	d := authz.decision with input as q(alice, {})
		with data.governed.config.tenants.acme.active as false
	d.allow == false
}
