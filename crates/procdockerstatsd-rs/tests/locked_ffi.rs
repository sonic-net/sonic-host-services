fn main() {
    // This foreign type's implementation must use the caller's shared Serde.
    let encoded = serde_json::to_string(&swss_common::KeyOperation::Set).unwrap();
    assert_eq!(encoded, "\"Set\"");
    let decoded: swss_common::KeyOperation = serde_json::from_str("\"Del\"").unwrap();
    assert_eq!(decoded, swss_common::KeyOperation::Del);

    // Exercise the real C ABI without a Redis daemon or host configuration.
    let missing = std::path::PathBuf::from(std::env::var_os("TEST_TMPDIR").unwrap())
        .join("missing-common-config.json");
    assert!(!missing.exists());
    assert!(swss_common::sonic_db_config_initialize(missing.to_str().unwrap()).is_err());
    println!("Make-selected Common C ABI and shared Serde round trip passed");
}
