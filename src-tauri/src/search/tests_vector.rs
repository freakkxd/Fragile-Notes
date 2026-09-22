use super::types::{VectorModelFilter, VectorQuery, VectorSearchLimits};
use super::vector::{SqliteVectorStore, VectorStore};
use crate::llm::embeddings::types::{EmbeddingRecord, NoteChunk};
use crate::llm::embeddings::validation::content_hash_for;
use rusqlite::{params, Connection};

const EPS: f32 = 1e-5;

fn model(m: &str, fp: &str) -> VectorModelFilter {
    VectorModelFilter {
        model_id: m.to_string(),
        model_fingerprint: fp.to_string(),
    }
}

fn chunk(note_id: &str, chunk_id: &str, content: &str) -> NoteChunk {
    NoteChunk {
        id: chunk_id.to_string(),
        note_id: note_id.to_string(),
        content: content.to_string(),
        content_hash: content_hash_for(content),
        heading_path: vec![],
        start_offset: 0,
        end_offset: content.len(),
    }
}

fn rec(chunk_id: &str, content: &str, model: &str, fp: &str, vec: Vec<f32>) -> EmbeddingRecord {
    EmbeddingRecord {
        chunk_id: chunk_id.to_string(),
        model_id: model.to_string(),
        model_fingerprint: fp.to_string(),
        content_hash: content_hash_for(content),
        dimensions: vec.len(),
        vector: vec,
    }
}

async fn setup_store_with_chunks(
    store: &SqliteVectorStore,
    path: &std::path::Path,
) {
    // Need to insert chunks directly via SQL since VectorStore uses same DB
    // Create chunks for each vector's chunk_id
}

fn make_store() -> SqliteVectorStore {
    SqliteVectorStore::new_in_memory(VectorSearchLimits::default()).unwrap()
}

fn insert_chunk(conn: &Connection, chunk: &NoteChunk) {
    conn.execute(
        "INSERT OR IGNORE INTO embedding_chunks (chunk_id, note_id, content, content_hash, heading_path_json, start_offset, end_offset, created_at, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, datetime('now'), datetime('now'))",
        params![
            chunk.id,
            chunk.note_id,
            chunk.content,
            chunk.content_hash,
            serde_json::to_string(&chunk.heading_path).unwrap(),
            chunk.start_offset as i64,
            chunk.end_offset as i64
        ],
    )
    .unwrap();
}

#[tokio::test]
async fn identical_vectors_score_1() {
    let store = make_store();
    let c = chunk("note-1", "c1", "hello");
    let conn = Connection::open(store.with_path_for_test()).unwrap();
    insert_chunk(&conn, &c);
    drop(conn);
    let r = rec("c1", "hello", "m1", "fp1", vec![1.0, 0.0, 0.0]);
    store.upsert(&[r]).await.unwrap();
    let q = VectorQuery {
        vector: vec![1.0, 0.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let res = store.search(q).await.unwrap();
    assert_eq!(res.len(), 1);
    assert!((res[0].score - 1.0).abs() < EPS);
    assert!((res[0].distance - 0.0).abs() < EPS);
}

#[tokio::test]
async fn orthogonal_vectors_score_0() {
    let store = make_store();
    let c = chunk("note-1", "c1", "hello");
    let conn = Connection::open(store.with_path_for_test()).unwrap();
    insert_chunk(&conn, &c);
    drop(conn);
    let r = rec("c1", "hello", "m1", "fp1", vec![1.0, 0.0]);
    store.upsert(&[r]).await.unwrap();
    let q = VectorQuery {
        vector: vec![0.0, 1.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let res = store.search(q).await.unwrap();
    assert!((res[0].score - 0.0).abs() < EPS);
    assert!((res[0].distance - 1.0).abs() < EPS);
}

#[tokio::test]
async fn opposite_vectors_score_minus_1() {
    let store = make_store();
    let c = chunk("note-1", "c1", "hello");
    let conn = Connection::open(store.with_path_for_test()).unwrap();
    insert_chunk(&conn, &c);
    drop(conn);
    let r = rec("c1", "hello", "m1", "fp1", vec![1.0, 0.0]);
    store.upsert(&[r]).await.unwrap();
    let q = VectorQuery {
        vector: vec![-1.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let res = store.search(q).await.unwrap();
    assert!((res[0].score - (-1.0)).abs() < EPS);
    assert!((res[0].distance - 2.0).abs() < EPS);
}

#[tokio::test]
async fn zero_query_vector_rejected() {
    let store = make_store();
    let q = VectorQuery {
        vector: vec![0.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let err = store.search(q).await.unwrap_err();
    assert!(matches!(err, super::types::SearchError::InvalidVector(_)));
}

#[tokio::test]
async fn zero_stored_vector_rejected() {
    let store = make_store();
    let c = chunk("note-1", "c1", "hello");
    let conn = Connection::open(store.with_path_for_test()).unwrap();
    insert_chunk(&conn, &c);
    drop(conn);
    let r = rec("c1", "hello", "m1", "fp1", vec![0.0, 0.0]);
    // upsert itself should be allowed? It's zero-norm, but search should reject when reading
    // Our upsert doesn't check zero-norm, but search will when computing cosine
    store.upsert(&[r]).await.unwrap();
    let q = VectorQuery {
        vector: vec![1.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let err = store.search(q).await.unwrap_err();
    assert!(matches!(err, super::types::SearchError::InvalidVector(_)));
}

#[tokio::test]
async fn nan_query_rejected() {
    let store = make_store();
    let q = VectorQuery {
        vector: vec![f32::NAN, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let err = store.search(q).await.unwrap_err();
    assert!(matches!(err, super::types::SearchError::InvalidVector(_)));
}

#[tokio::test]
async fn infinity_stored_rejected() {
    let store = make_store();
    let c = chunk("note-1", "c1", "hello");
    let conn = Connection::open(store.with_path_for_test()).unwrap();
    insert_chunk(&conn, &c);
    drop(conn);
    let r = rec("c1", "hello", "m1", "fp1", vec![f32::INFINITY, 0.0]);
    // upsert will succeed (no finite check in VectorStore upsert? Actually we check)
    let res = store.upsert(&[r]).await;
    // Should be rejected at upsert as invalid vector
    assert!(res.is_err());
}

#[tokio::test]
async fn dimension_mismatch_rejected() {
    let store = make_store();
    let c = chunk("note-1", "c1", "hello");
    let conn = Connection::open(store.with_path_for_test()).unwrap();
    insert_chunk(&conn, &c);
    drop(conn);
    let r = rec("c1", "hello", "m1", "fp1", vec![1.0, 0.0]);
    store.upsert(&[r]).await.unwrap();
    let q = VectorQuery {
        vector: vec![1.0, 0.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let err = store.search(q).await.unwrap_err();
    assert!(matches!(err, super::types::SearchError::DimensionMismatch { .. }));
}

#[tokio::test]
async fn model_id_mismatch_excluded() {
    let store = make_store();
    for (id, content) in [("c1", "a"), ("c2", "b")] {
        let c = chunk("note-1", id, content);
        let conn = Connection::open(store.with_path_for_test()).unwrap();
        insert_chunk(&conn, &c);
        drop(conn);
    }
    let r1 = rec("c1", "a", "m1", "fp1", vec![1.0, 0.0]);
    let r2 = rec("c2", "b", "m2", "fp1", vec![1.0, 0.0]);
    store.upsert(&[r1, r2]).await.unwrap();
    let q = VectorQuery {
        vector: vec![1.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let res = store.search(q).await.unwrap();
    assert_eq!(res.len(), 1);
    assert_eq!(res[0].chunk_id, "c1");
}

#[tokio::test]
async fn fingerprint_mismatch_excluded() {
    let store = make_store();
    for (id, content) in [("c1", "a"), ("c2", "b")] {
        let c = chunk("note-1", id, content);
        let conn = Connection::open(store.with_path_for_test()).unwrap();
        insert_chunk(&conn, &c);
        drop(conn);
    }
    let r1 = rec("c1", "a", "m1", "fp1", vec![1.0, 0.0]);
    let r2 = rec("c2", "b", "m1", "fp2", vec![1.0, 0.0]);
    store.upsert(&[r1, r2]).await.unwrap();
    let q = VectorQuery {
        vector: vec![1.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let res = store.search(q).await.unwrap();
    assert_eq!(res.len(), 1);
    assert_eq!(res[0].chunk_id, "c1");
}

#[tokio::test]
async fn note_filter_works() {
    let store = make_store();
    for (cid, nid, txt) in [("c1", "notes/a.md", "hello"), ("c2", "archive/b.md", "hello")] {
        let c = chunk(nid, cid, txt);
        let conn = Connection::open(store.with_path_for_test()).unwrap();
        insert_chunk(&conn, &c);
        drop(conn);
    }
    let r1 = rec("c1", "hello", "m1", "fp1", vec![1.0, 0.0]);
    let r2 = rec("c2", "hello", "m1", "fp1", vec![1.0, 0.0]);
    store.upsert(&[r1, r2]).await.unwrap();
    let q = VectorQuery {
        vector: vec![1.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: Some("notes".to_string()),
        min_score: None,
    };
    let res = store.search(q).await.unwrap();
    assert_eq!(res.len(), 1);
    assert_eq!(res[0].note_id, "notes/a.md");

    // Test LIKE escaping: filter with % should be literal, not wildcard
    let q2 = VectorQuery {
        vector: vec![1.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: Some("notes/a.md%".to_string()),
        min_score: None,
    };
    let res2 = store.search(q2).await.unwrap();
    assert_eq!(res2.len(), 0, "escaped % should not match");
}

#[tokio::test]
async fn limit_respected() {
    let store = make_store();
    for i in 0..5 {
        let cid = format!("c{}", i);
        let c = chunk("note-1", &cid, "hello");
        let conn = Connection::open(store.with_path_for_test()).unwrap();
        insert_chunk(&conn, &c);
        drop(conn);
        let r = rec(&cid, "hello", "m1", "fp1", vec![1.0, 0.0]);
        store.upsert(&[r]).await.unwrap();
    }
    let q = VectorQuery {
        vector: vec![1.0, 0.0],
        model: model("m1", "fp1"),
        limit: 2,
        note_filter: None,
        min_score: None,
    };
    let res = store.search(q).await.unwrap();
    assert_eq!(res.len(), 2);
}

#[tokio::test]
async fn min_score_works() {
    let store = make_store();
    let c1 = chunk("note-1", "c1", "hello");
    let c2 = chunk("note-1", "c2", "hello");
    for c in [&c1, &c2] {
        let conn = Connection::open(store.with_path_for_test()).unwrap();
        insert_chunk(&conn, c);
        drop(conn);
    }
    let r1 = rec("c1", "hello", "m1", "fp1", vec![1.0, 0.0]);
    let r2 = rec("c2", "hello", "m1", "fp1", vec![0.0, 1.0]);
    store.upsert(&[r1, r2]).await.unwrap();
    let q = VectorQuery {
        vector: vec![1.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: Some(0.5),
    };
    let res = store.search(q).await.unwrap();
    assert_eq!(res.len(), 1);
    assert_eq!(res[0].chunk_id, "c1");
}

#[tokio::test]
async fn descending_score() {
    let store = make_store();
    // Create vectors with known cosine to query [1,0]
    // c1 = [1,0] => 1.0, c2 = [0.5, 0.5] => ~0.707, c3 = [0,1] => 0
    let cases = vec![
        ("c1", vec![1.0, 0.0]),
        ("c2", vec![0.5, 0.8660254]), // cos 0.5
        ("c3", vec![0.0, 1.0]),
    ];
    for (cid, vec) in &cases {
        let c = chunk("note-1", cid, "hello");
        let conn = Connection::open(store.with_path_for_test()).unwrap();
        insert_chunk(&conn, &c);
        drop(conn);
        let r = rec(cid, "hello", "m1", "fp1", vec.clone());
        store.upsert(&[r]).await.unwrap();
    }
    let q = VectorQuery {
        vector: vec![1.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let res = store.search(q).await.unwrap();
    assert_eq!(res.len(), 3);
    assert!(res[0].score > res[1].score);
    assert!(res[1].score > res[2].score);
}

#[tokio::test]
async fn stable_tie_ordering() {
    let store = make_store();
    for cid in ["c2", "c1", "c3"] {
        let c = chunk("note-1", cid, "hello");
        let conn = Connection::open(store.with_path_for_test()).unwrap();
        insert_chunk(&conn, &c);
        drop(conn);
        let r = rec(cid, "hello", "m1", "fp1", vec![1.0, 0.0]);
        store.upsert(&[r]).await.unwrap();
    }
    let q = VectorQuery {
        vector: vec![1.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let res = store.search(q).await.unwrap();
    // All scores 1.0, should be ordered by chunk_id ASC: c1, c2, c3
    assert_eq!(res[0].chunk_id, "c1");
    assert_eq!(res[1].chunk_id, "c2");
    assert_eq!(res[2].chunk_id, "c3");
}

#[tokio::test]
async fn empty_index_returns_empty() {
    let store = make_store();
    let q = VectorQuery {
        vector: vec![1.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let res = store.search(q).await.unwrap();
    assert!(res.is_empty());
}

#[tokio::test]
async fn corrupt_blob_returns_error() {
    let store = make_store();
    let c = chunk("note-1", "c1", "hello");
    let conn = Connection::open(store.with_path_for_test()).unwrap();
    insert_chunk(&conn, &c);
    drop(conn);
    let r = rec("c1", "hello", "m1", "fp1", vec![1.0, 0.0]);
    store.upsert(&[r]).await.unwrap();
    // Corrupt blob directly
    let conn2 = Connection::open(store.with_path_for_test()).unwrap();
    conn2
        .execute(
            "UPDATE embedding_vectors SET vector_blob = ?1 WHERE chunk_id = ?2",
            params![vec![0u8, 1, 2], "c1"],
        )
        .unwrap();
    drop(conn2);
    let q = VectorQuery {
        vector: vec![1.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let err = store.search(q).await.unwrap_err();
    assert!(matches!(err, super::types::SearchError::CorruptVectorBlob(_)));
}

#[tokio::test]
async fn model_versions_isolated() {
    let store = make_store();
    let c = chunk("note-1", "c1", "hello");
    let conn = Connection::open(store.with_path_for_test()).unwrap();
    insert_chunk(&conn, &c);
    drop(conn);
    let r1 = rec("c1", "hello", "m1", "fp1", vec![1.0, 0.0]);
    let r2 = rec("c1", "hello", "m1", "fp2", vec![0.0, 1.0]);
    store.upsert(&[r1, r2]).await.unwrap();
    // Query with fp1 should only see fp1 vector
    let q1 = VectorQuery {
        vector: vec![1.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let res1 = store.search(q1).await.unwrap();
    assert_eq!(res1.len(), 1);
    assert!((res1[0].score - 1.0).abs() < EPS);

    let q2 = VectorQuery {
        vector: vec![1.0, 0.0],
        model: model("m1", "fp2"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let res2 = store.search(q2).await.unwrap();
    assert!((res2[0].score - 0.0).abs() < EPS);
}

#[tokio::test]
async fn candidate_limit_enforced() {
    let limits = VectorSearchLimits {
        max_candidates: 2,
        max_vector_bytes: 1024 * 1024,
    };
    let store = SqliteVectorStore::new_in_memory(limits).unwrap();
    for i in 0..3 {
        let cid = format!("c{}", i);
        let c = chunk("note-1", &cid, "hello");
        let conn = Connection::open(store.with_path_for_test()).unwrap();
        insert_chunk(&conn, &c);
        drop(conn);
        let r = rec(&cid, "hello", "m1", "fp1", vec![1.0, 0.0]);
        store.upsert(&[r]).await.unwrap();
    }
    let q = VectorQuery {
        vector: vec![1.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let err = store.search(q).await.unwrap_err();
    assert!(matches!(err, super::types::SearchError::LimitExceeded(_)));
}

#[tokio::test]
async fn brute_force_reference() {
    // Compare optimized path vs brute force for small set
    let store = make_store();
    let vectors = vec![
        ("c1", vec![1.0, 0.0]),
        ("c2", vec![0.0, 1.0]),
        ("c3", vec![0.707, 0.707]),
    ];
    for (cid, vec) in &vectors {
        let c = chunk("note-1", cid, "hello");
        let conn = Connection::open(store.with_path_for_test()).unwrap();
        insert_chunk(&conn, &c);
        drop(conn);
        let r = rec(cid, "hello", "m1", "fp1", vec.clone());
        store.upsert(&[r]).await.unwrap();
    }
    let q = VectorQuery {
        vector: vec![1.0, 0.0],
        model: model("m1", "fp1"),
        limit: 10,
        note_filter: None,
        min_score: None,
    };
    let res = store.search(q.clone()).await.unwrap();
    // Brute force
    let mut expected: Vec<(String, f32)> = vectors
        .iter()
        .map(|(id, v)| {
            let dot: f32 = v.iter().zip(&q.vector).map(|(a, b)| a * b).sum();
            let norm_a: f32 = v.iter().map(|x| x * x).sum::<f32>().sqrt();
            let norm_b: f32 = q.vector.iter().map(|x| x * x).sum::<f32>().sqrt();
            let score = dot / (norm_a * norm_b);
            (id.to_string(), score)
        })
        .collect();
    expected.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap().then_with(|| a.0.cmp(&b.0)));
    for (i, (id, score)) in expected.iter().enumerate() {
        assert_eq!(res[i].chunk_id, *id);
        assert!((res[i].score - score).abs() < EPS);
    }
}
