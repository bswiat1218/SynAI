use std::time::Duration;

use quota_inspector_native::{
    BoundRequest, CandidateInventory, Failure, NativeInspector, ObservationBudget, Outcome,
    Unavailable,
};
use serde_json::{Value, json};

#[test]
fn non_test_backend_returns_only_unavailable() {
    let inventory = CandidateInventory::parse(include_bytes!(
        "../../../tests/fixtures/quota_inspector_native/inventory.json"
    ))
    .unwrap();
    let request = json!({
        "schema_version": 1, "message_type": "inspect_storage_role", "role_id": "state",
        "challenge": "a".repeat(64), "inventory_generation": inventory.generation(),
    });
    let binding = BoundRequest::parse(&inventory, &serde_json::to_vec(&request).unwrap()).unwrap();
    let mut inspector = NativeInspector::new();
    for _ in 0..10 {
        let result = inspector.inspect(
            &inventory,
            &binding,
            ObservationBudget::new(Duration::from_millis(100)).unwrap(),
        );
        assert!(matches!(
            result.outcome(),
            Outcome::Failed(Failure::Unavailable(
                Unavailable::InventoryAuthorityUnavailable
            ))
        ));
        let response = result.protocol_response(&"b".repeat(32)).unwrap();
        let value: Value = serde_json::from_slice(&response).unwrap();
        assert_eq!(value["status"], "unavailable");
        assert_eq!(value["error_code"], "inventory_authority_unavailable");
        assert_eq!(value.as_object().unwrap().len(), 8);
        assert!(value.get("observed_at").is_none());
        println!(
            "NATIVE_V1_RESPONSE={}",
            String::from_utf8(response).unwrap()
        );
    }
    inspector.cancel();
    assert!(inspector.poll().is_none());
}
