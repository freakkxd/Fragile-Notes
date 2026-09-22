//! Stage 3 persistence tests — incremental chunk + vector persistence
//! Uses temp file with TempDir RAII, never touches user vault.

use super::store::{ChunkStore, EmbeddingStore, SqliteStore};
use super::types::*;
use super::validation::{content_hash_for, sha256_hex};
use super::chunker::{chunk_markdown, ChunkingConfig};

fn chunk_for(note_id: &str, content: &str, start: usize, end: usize) -> NoteChunk {
    let hash = content_hash_for(content);
    let id = sha256_hex(&format!("{}:{}:0", note_id, hash));
    NoteChunk {
        id,
        note_id: note_id.to_string(),
        content: content.to_string(),
        content_hash: hash,
        heading_path: vec![],
        start_offset: start,
        end_offset: end,
    }
}

fn rec_for(chunk_id: &str, content_hash: &str, model_id: &str, fp: &str, dims: usize, vec: Vec<f32>) -> EmbeddingRecord {
    EmbeddingRecord {
        chunk_id: chunk_id.to_string(),
        model_id: model_id.to_string(),
        model_fingerprint: fp.to_string(),
        content_hash: content_hash.to_string(),
        dimensions: dims,
        vector: vec,
    }
}

fn model_ref(model_id: &str, fp: &str) -> EmbeddingModelRef {
    EmbeddingModelRef {
        model_id: model_id.to_string(),
        model_fingerprint: fp.to_string(),
    }
}

// ---- basic insert / load ----

#[tokio::test]
async fn insert_and_load_chunks() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c1 = chunk_for("note-1", "hello world", 0, 11);
    let c2 = chunk_for("note-1", "second chunk", 12, 24);
    let diff = s.upsert_chunks(&[c1.clone(), c2.clone()]).await.unwrap();
    assert_eq!(diff.added.len(), 2);
    assert_eq!(diff.unchanged.len(), 0);
    let loaded = s.get_chunks("note-1").await.unwrap();
    assert_eq!(loaded.len(), 2);
    assert!(loaded.iter().any(|c| c.id == c1.id));
    assert!(loaded.iter().any(|c| c.id == c2.id));
}

#[tokio::test]
async fn load_empty_note() {
    let s = SqliteStore::new_in_memory().unwrap();
    let loaded = s.get_chunks("missing").await.unwrap();
    assert!(loaded.is_empty());
}

#[tokio::test]
async fn unchanged_chunks_detected() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c = chunk_for("note-1", "stable content", 0, 14);
    s.upsert_chunks(&[c.clone()]).await.unwrap();
    let diff = s.upsert_chunks(&[c.clone()]).await.unwrap();
    assert_eq!(diff.unchanged.len(), 1);
    assert_eq!(diff.added.len(), 0);
    assert_eq!(diff.changed.len(), 0);
    assert!(diff.deleted_ids.is_empty());
}

#[tokio::test]
async fn changed_content_hash_detected() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c1 = chunk_for("note-1", "original", 0, 8);
    s.upsert_chunks(&[c1.clone()]).await.unwrap();
    let new_content = "modified";
    let new_hash = content_hash_for(new_content);
    let c2 = NoteChunk {
        id: c1.id.clone(),
        note_id: "note-1".to_string(),
        content: new_content.to_string(),
        content_hash: new_hash.clone(),
        heading_path: vec![],
        start_offset: 0,
        end_offset: 8,
    };
    let diff = s.upsert_chunks(&[c2.clone()]).await.unwrap();
    assert_eq!(diff.changed.len(), 1);
    assert_eq!(diff.changed[0].content_hash, new_hash);
    let loaded = s.get_chunks("note-1").await.unwrap();
    assert_eq!(loaded[0].content, "modified");
}

#[tokio::test]
async fn deleted_chunks_removed() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c1 = chunk_for("note-1", "chunk one", 0, 9);
    let c2 = chunk_for("note-1", "chunk two", 10, 19);
    s.upsert_chunks(&[c1.clone(), c2.clone()]).await.unwrap();
    let diff = s.upsert_chunks(&[c1.clone()]).await.unwrap();
    assert_eq!(diff.deleted_ids, vec![c2.id.clone()]);
    let loaded = s.get_chunks("note-1").await.unwrap();
    assert_eq!(loaded.len(), 1);
    assert_eq!(loaded[0].id, c1.id);
}

// ---- vector reuse / invalidation ----

#[tokio::test]
async fn unchanged_vectors_reused() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c = chunk_for("note-1", "vector content", 0, 14);
    s.upsert_chunks(&[c.clone()]).await.unwrap();
    let rec = rec_for(&c.id, &c.content_hash, "model-a", "fp-v1", 3, vec![0.1, 0.2, 0.3]);
    s.upsert(&[rec.clone()]).await.unwrap();
    let diff = s.upsert_chunks(&[c.clone()]).await.unwrap();
    assert_eq!(diff.unchanged.len(), 1);
    let got = s.get(&c.id, &model_ref("model-a", "fp-v1")).await.unwrap();
    assert!(got.is_some());
    let got = got.unwrap();
    assert_eq!(got.vector, rec.vector);
    assert_eq!(got.model_fingerprint, "fp-v1");
}

#[tokio::test]
async fn changed_chunks_delete_old_embeddings() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c1 = chunk_for("note-1", "original vec", 0, 12);
    s.upsert_chunks(&[c1.clone()]).await.unwrap();
    let rec = rec_for(&c1.id, &c1.content_hash, "model-a", "fp-v1", 2, vec![1.0, 2.0]);
    s.upsert(&[rec]).await.unwrap();
    let new_content = "modified vec";
    let new_hash = content_hash_for(new_content);
    let c2 = NoteChunk {
        id: c1.id.clone(),
        note_id: "note-1".to_string(),
        content: new_content.to_string(),
        content_hash: new_hash,
        heading_path: vec![],
        start_offset: 0,
        end_offset: 12,
    };
    s.upsert_chunks(&[c2]).await.unwrap();
    let got = s.get(&c1.id, &model_ref("model-a", "fp-v1")).await.unwrap();
    assert!(got.is_none(), "stale vector not deleted after chunk change");
}

#[tokio::test]
async fn deleted_chunk_cascade_deletes_vectors() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c1 = chunk_for("note-1", "to delete", 0, 9);
    let c2 = chunk_for("note-1", "keep me", 10, 17);
    s.upsert_chunks(&[c1.clone(), c2.clone()]).await.unwrap();
    let r1 = rec_for(&c1.id, &c1.content_hash, "model-a", "fp1", 2, vec![0.1, 0.2]);
    let r2 = rec_for(&c2.id, &c2.content_hash, "model-a", "fp1", 2, vec![0.3, 0.4]);
    s.upsert(&[r1, r2]).await.unwrap();
    s.upsert_chunks(&[c2.clone()]).await.unwrap();
    assert!(s.get(&c1.id, &model_ref("model-a", "fp1")).await.unwrap().is_none());
    assert!(s.get(&c2.id, &model_ref("model-a", "fp1")).await.unwrap().is_some());
}

#[tokio::test]
async fn delete_chunks_api_cascades() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c = chunk_for("note-1", "cascade via delete_chunks", 0, 21);
    s.upsert_chunks(&[c.clone()]).await.unwrap();
    let r = rec_for(&c.id, &c.content_hash, "m1", "fp", 1, vec![0.5]);
    s.upsert(&[r]).await.unwrap();
    s.delete_chunks(&[c.id.clone()]).await.unwrap();
    assert!(s.get_chunks("note-1").await.unwrap().is_empty());
    assert!(s.get(&c.id, &model_ref("m1", "fp")).await.unwrap().is_none());
}

// ---- model separation ----

#[tokio::test]
async fn model_id_separates_vectors() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c = chunk_for("note-1", "same chunk diff model", 0, 19);
    s.upsert_chunks(&[c.clone()]).await.unwrap();
    let r_a = rec_for(&c.id, &c.content_hash, "model-a", "fp1", 2, vec![1.0, 2.0]);
    let r_b = rec_for(&c.id, &c.content_hash, "model-b", "fp1", 2, vec![3.0, 4.0]);
    s.upsert(&[r_a.clone()]).await.unwrap();
    s.upsert(&[r_b.clone()]).await.unwrap();
    let got_a = s.get(&c.id, &model_ref("model-a", "fp1")).await.unwrap().unwrap();
    let got_b = s.get(&c.id, &model_ref("model-b", "fp1")).await.unwrap().unwrap();
    assert_eq!(got_a.vector, vec![1.0, 2.0]);
    assert_eq!(got_b.vector, vec![3.0, 4.0]);
}

#[tokio::test]
async fn model_fingerprint_invalidates() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c = chunk_for("note-1", "fp test", 0, 7);
    s.upsert_chunks(&[c.clone()]).await.unwrap();
    let r1 = rec_for(&c.id, &c.content_hash, "model-a", "fp-v1", 2, vec![1.0, 1.0]);
    s.upsert(&[r1]).await.unwrap();
    // With composite PK, second fingerprint creates new row, not overwrite
    let r2 = rec_for(&c.id, &c.content_hash, "model-a", "fp-v2", 2, vec![2.0, 2.0]);
    s.upsert(&[r2.clone()]).await.unwrap();
    // Both should exist via get_any
    let any = s.get_any(&c.id, "model-a").await.unwrap();
    assert_eq!(any.len(), 2, "composite PK should keep both fingerprints");
    let got_v1 = s.get(&c.id, &model_ref("model-a", "fp-v1")).await.unwrap().unwrap();
    let got_v2 = s.get(&c.id, &model_ref("model-a", "fp-v2")).await.unwrap().unwrap();
    assert_eq!(got_v1.vector, vec![1.0, 1.0]);
    assert_eq!(got_v2.vector, vec![2.0, 2.0]);
    // reuse requires exact fingerprint match
    let reuse_v1_as_v2 = got_v2.model_fingerprint == "fp-v1";
    assert!(!reuse_v1_as_v2, "different fingerprint must not be reusable");
    // Explicit cleanup of obsolete fingerprint
    s.delete_for_model(&c.id, &model_ref("model-a", "fp-v1")).await.unwrap();
    assert!(s.get(&c.id, &model_ref("model-a", "fp-v1")).await.unwrap().is_none());
    assert!(s.get(&c.id, &model_ref("model-a", "fp-v2")).await.unwrap().is_some());
}

// ---- validation ----

#[tokio::test]
async fn dimension_mismatch_rejected() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c = chunk_for("note-1", "dim test", 0, 8);
    s.upsert_chunks(&[c.clone()]).await.unwrap();
    let bad = EmbeddingRecord {
        chunk_id: c.id.clone(),
        model_id: "m".to_string(),
        model_fingerprint: "fp".to_string(),
        content_hash: c.content_hash.clone(),
        dimensions: 3,
        vector: vec![1.0, 2.0],
    };
    let res = s.upsert(&[bad]).await;
    assert!(res.is_err());
}

#[tokio::test]
async fn non_finite_rejected() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c = chunk_for("note-1", "finite", 0, 6);
    s.upsert_chunks(&[c.clone()]).await.unwrap();
    let nan_rec = rec_for(&c.id, &c.content_hash, "m", "fp", 2, vec![f32::NAN, 0.1]);
    assert!(s.upsert(&[nan_rec]).await.is_err());
    let inf_rec = rec_for(&c.id, &c.content_hash, "m", "fp", 2, vec![f32::INFINITY, 0.1]);
    assert!(s.upsert(&[inf_rec]).await.is_err());
    let neg_inf = rec_for(&c.id, &c.content_hash, "m", "fp", 2, vec![f32::NEG_INFINITY, 0.1]);
    assert!(s.upsert(&[neg_inf]).await.is_err());
}

#[tokio::test]
async fn vector_blob_round_trip() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c = chunk_for("note-1", "blob rt", 0, 7);
    s.upsert_chunks(&[c.clone()]).await.unwrap();
    let orig = vec![0.1f32, -0.2, 3.14159, 1e10, -1e-5];
    let rec = rec_for(&c.id, &c.content_hash, "m", "fp", orig.len(), orig.clone());
    s.upsert(&[rec]).await.unwrap();
    let got = s.get(&c.id, &model_ref("m", "fp")).await.unwrap().unwrap();
    assert_eq!(got.vector, orig);
}

#[tokio::test]
async fn corrupt_blob_rejected() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c = chunk_for("note-1", "corrupt", 0, 7);
    s.upsert_chunks(&[c.clone()]).await.unwrap();
    let rec = rec_for(&c.id, &c.content_hash, "m", "fp", 2, vec![1.0, 2.0]);
    s.upsert(&[rec]).await.unwrap();
    {
        let conn = s.open().unwrap();
        conn.execute(
            "update embedding_vectors set vector_blob = ?1 where chunk_id = ?2 and model_id = ?3 and model_fingerprint = ?4",
            rusqlite::params![vec![0u8, 1, 2], c.id, "m", "fp"],
        )
        .unwrap();
    }
    let res = s.get(&c.id, &model_ref("m", "fp")).await;
    assert!(res.is_err(), "corrupt blob should be rejected: {:?}", res);
    assert!(matches!(res.unwrap_err(), EmbeddingError::CorruptVectorBlob(_)));

    {
        let s2 = SqliteStore::new_in_memory().unwrap();
        let c2 = chunk_for("note-1", "corrupt2", 0, 8);
        s2.upsert_chunks(&[c2.clone()]).await.unwrap();
        let rec2 = rec_for(&c2.id, &c2.content_hash, "m", "fp", 2, vec![1.0, 2.0]);
        s2.upsert(&[rec2]).await.unwrap();
        let conn = s2.open().unwrap();
        let nan_blob = f32::NAN.to_le_bytes().to_vec();
        let mut bad_blob = nan_blob.clone();
        bad_blob.extend_from_slice(&1.0f32.to_le_bytes());
        conn.execute(
            "update embedding_vectors set vector_blob = ?1 where chunk_id = ?2 and model_id = ?3 and model_fingerprint = ?4",
            rusqlite::params![bad_blob, c2.id, "m", "fp"],
        )
        .unwrap();
        let res2 = s2.get(&c2.id, &model_ref("m", "fp")).await;
        assert!(res2.is_err());
        assert!(matches!(res2.unwrap_err(), EmbeddingError::CorruptVectorBlob(_)));
    }
}

// ---- constraints ----

#[tokio::test]
async fn duplicate_chunk_ids_rejected() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c1 = chunk_for("note-1", "dup", 0, 3);
    let mut c2 = c1.clone();
    c2.content = "different but same id".to_string();
    c2.content_hash = content_hash_for(&c2.content);
    let res = s.upsert_chunks(&[c1.clone(), c2.clone()]).await;
    assert!(res.is_err());
    let loaded = s.get_chunks("note-1").await.unwrap();
    assert!(loaded.is_empty());
}

#[tokio::test]
async fn transaction_rollback_on_invalid_chunk() {
    let s = SqliteStore::new_in_memory().unwrap();
    let good = chunk_for("note-1", "good", 0, 4);
    s.upsert_chunks(&[good.clone()]).await.unwrap();
    let valid2 = chunk_for("note-1", "valid2", 0, 6);
    let mut invalid = chunk_for("note-1", "invalid", 0, 7);
    invalid.content_hash = "wronghash".to_string();
    let res = s.upsert_chunks(&[valid2.clone(), invalid]).await;
    assert!(res.is_err());
    let loaded = s.get_chunks("note-1").await.unwrap();
    assert_eq!(loaded.len(), 1);
    assert_eq!(loaded[0].id, good.id);
}

#[tokio::test]
async fn foreign_key_violation_on_orphan_vector() {
    let s = SqliteStore::new_in_memory().unwrap();
    let fake_id = "nonexistent-chunk".to_string();
    let rec = EmbeddingRecord {
        chunk_id: fake_id,
        model_id: "m".to_string(),
        model_fingerprint: "fp".to_string(),
        content_hash: content_hash_for("hi"),
        dimensions: 2,
        vector: vec![1.0, 2.0],
    };
    let res = s.upsert(&[rec]).await;
    assert!(res.is_err(), "orphan vector should violate FK");
}

#[tokio::test]
async fn foreign_key_cascade_via_delete_chunks() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c = chunk_for("note-1", "cascade", 0, 7);
    s.upsert_chunks(&[c.clone()]).await.unwrap();
    let rec = rec_for(&c.id, &c.content_hash, "m", "fp", 2, vec![0.1, 0.2]);
    s.upsert(&[rec]).await.unwrap();
    s.delete_chunks(&[c.id.clone()]).await.unwrap();
    let got = s.get(&c.id, &model_ref("m", "fp")).await.unwrap();
    assert!(got.is_none());
}

#[tokio::test]
async fn crlf_normalized_hash_preserved() {
    let s = SqliteStore::new_in_memory().unwrap();
    let content_lf = "Hello\nWorld\n";
    let content_crlf = "Hello\r\nWorld\r\n";
    assert_eq!(content_hash_for(content_lf), content_hash_for(content_crlf));
    let c = NoteChunk {
        id: "id1".to_string(),
        note_id: "note-1".to_string(),
        content: content_crlf.to_string(),
        content_hash: content_hash_for(content_crlf),
        heading_path: vec![],
        start_offset: 0,
        end_offset: content_crlf.len(),
    };
    assert!(c.validate().is_ok());
    s.upsert_chunks(&[c.clone()]).await.unwrap();
    let loaded = s.get_chunks("note-1").await.unwrap();
    assert_eq!(loaded[0].content_hash, content_hash_for(content_lf));
}

#[tokio::test]
async fn task_profile_unaffected_by_chunk_update() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c_note1 = chunk_for("note-1", "note1 content", 0, 13);
    let c_note2 = chunk_for("note-2", "note2 content", 0, 13);
    s.upsert_chunks(&[c_note1.clone()]).await.unwrap();
    s.upsert_chunks(&[c_note2.clone()]).await.unwrap();
    let c_note1_v2 = {
        let new_content = "note1 modified";
        NoteChunk {
            id: c_note1.id.clone(),
            note_id: "note-1".to_string(),
            content: new_content.to_string(),
            content_hash: content_hash_for(new_content),
            heading_path: vec![],
            start_offset: 0,
            end_offset: new_content.len(),
        }
    };
    s.upsert_chunks(&[c_note1_v2.clone()]).await.unwrap();
    let loaded2 = s.get_chunks("note-2").await.unwrap();
    assert_eq!(loaded2.len(), 1);
    assert_eq!(loaded2[0].content, "note2 content");
}

#[tokio::test]
async fn partial_run_not_marked_completed() {
    let s = SqliteStore::new_in_memory().unwrap();
    let run = IndexRun {
        id: "run-1".to_string(),
        note_id: "note-1".to_string(),
        source_hash: "abc".to_string(),
        status: IndexStatus::Running,
        processed_chunks: 1,
        total_chunks: 3,
        error: None,
    };
    s.upsert_index_run(&run).await.unwrap();
    let got = s.get_index_run("run-1").await.unwrap().unwrap();
    assert_eq!(got.status, IndexStatus::Running);
    assert_ne!(got.status, IndexStatus::Completed);
    let mut failed = run.clone();
    failed.status = IndexStatus::Failed;
    failed.error = Some("boom".to_string());
    s.upsert_index_run(&failed).await.unwrap();
    let got2 = s.get_index_run("run-1").await.unwrap().unwrap();
    assert_eq!(got2.status, IndexStatus::Failed);
}

#[tokio::test]
async fn cancellation_rollback() {
    let s = SqliteStore::new_in_memory().unwrap();
    let good = chunk_for("note-1", "before cancel", 0, 13);
    s.upsert_chunks(&[good.clone()]).await.unwrap();
    {
        let mut conn = s.open().unwrap();
        let tx = conn.transaction().unwrap();
        let now = chrono::Utc::now().to_rfc3339();
        let c_cancel = chunk_for("note-1", "cancelled content", 0, 17);
        tx.execute(
            "insert into embedding_chunks (chunk_id, note_id, content, content_hash, heading_path_json, start_offset, end_offset, created_at, updated_at) values (?1,?2,?3,?4,?5,?6,?7,?8,?8)",
            rusqlite::params![
                c_cancel.id,
                c_cancel.note_id,
                c_cancel.content,
                c_cancel.content_hash,
                "[]",
                c_cancel.start_offset as i64,
                c_cancel.end_offset as i64,
                now
            ],
        )
        .unwrap();
        drop(tx);
    }
    let loaded = s.get_chunks("note-1").await.unwrap();
    assert_eq!(loaded.len(), 1);
    assert_eq!(loaded[0].id, good.id);
}

#[tokio::test]
async fn migration_idempotent() {
    let s = SqliteStore::new_in_memory().unwrap();
    s.migrate().unwrap();
    s.migrate().unwrap();
    let c = chunk_for("note-1", "after migrate", 0, 12);
    s.upsert_chunks(&[c.clone()]).await.unwrap();
    assert_eq!(s.get_chunks("note-1").await.unwrap().len(), 1);
}

#[tokio::test]
async fn fresh_and_existing_db() {
    let s1 = SqliteStore::new_in_memory().unwrap();
    assert!(s1.get_chunks("note-1").await.unwrap().is_empty());
    let c = chunk_for("note-1", "exists", 0, 6);
    s1.upsert_chunks(&[c.clone()]).await.unwrap();
    let s2 = s1.reopen_shared();
    let loaded = s2.get_chunks("note-1").await.unwrap();
    assert_eq!(loaded.len(), 1);
}

#[tokio::test]
async fn incremental_gate_note_to_chunk_persist_reuse_edit_delete() {
    let s = SqliteStore::new_in_memory().unwrap();
    let md = "# Title\n\nPara one.\n\nPara two.\n\nPara three.\n";
    let cfg = ChunkingConfig { max_chars: 30, overlap_chars: 0, min_chars: 5, preserve_code_blocks: true };
    let chunked = chunk_markdown("note-1", md, &cfg).unwrap();
    assert!(chunked.chunks.len() >= 2);
    s.upsert_chunks(&chunked.chunks).await.unwrap();
    let model_id = "model-x";
    let fp = "fp-1";
    let mut recs = Vec::new();
    for ch in &chunked.chunks {
        recs.push(rec_for(&ch.id, &ch.content_hash, model_id, fp, 2, vec![1.0, 2.0]));
    }
    s.upsert(&recs).await.unwrap();

    let diff2 = s.upsert_chunks(&chunked.chunks).await.unwrap();
    assert_eq!(diff2.unchanged.len(), chunked.chunks.len());
    for ch in &chunked.chunks {
        assert!(s.get(&ch.id, &model_ref(model_id, fp)).await.unwrap().is_some());
    }

    let md2 = "# Title\n\nPara one MODIFIED.\n\nPara two.\n\nPara three.\n";
    let chunked2 = chunk_markdown("note-1", md2, &cfg).unwrap();
    let diff3 = s.upsert_chunks(&chunked2.chunks).await.unwrap();
    assert!(diff3.changed.len() >= 1 || diff3.added.len() >= 1);
    let para_two = chunked.chunks.iter().find(|c| c.content.contains("Para two")).unwrap();
    let para_two_v2 = chunked2.chunks.iter().find(|c| c.content.contains("Para two")).unwrap();
    if para_two.id == para_two_v2.id && para_two.content_hash == para_two_v2.content_hash {
        assert!(s.get(&para_two.id, &model_ref(model_id, fp)).await.unwrap().is_some(), "unchanged vector should be reused");
    }
    let missing_count = {
        let mut missing = 0;
        for ch in &chunked.chunks {
            let still = s.get(&ch.id, &model_ref(model_id, fp)).await.unwrap().is_some();
            if !still {
                missing += 1;
            }
        }
        missing
    };
    assert!(missing_count >= 1, "at least one old vector should be deleted after edit");

    let ids: Vec<String> = chunked2.chunks.iter().map(|c| c.id.clone()).collect();
    s.delete_chunks(&ids).await.unwrap();
    assert!(s.get_chunks("note-1").await.unwrap().is_empty());
    for ch in &chunked2.chunks {
        assert!(s.get(&ch.id, &model_ref(model_id, fp)).await.unwrap().is_none());
    }

    let c_a = chunk_for("note-2", "a", 0, 1);
    let c_b = chunk_for("note-2", "b", 0, 1);
    s.upsert_chunks(&[c_a.clone(), c_b.clone()]).await.unwrap();
    let mut bad = c_a.clone();
    bad.content_hash = "bad".to_string();
    let res = s.upsert_chunks(&[bad, c_b.clone()]).await;
    assert!(res.is_err());
    let loaded = s.get_chunks("note-2").await.unwrap();
    assert_eq!(loaded.len(), 2);
}

#[tokio::test]
async fn offsets_are_byte_offsets_not_char() {
    let s = SqliteStore::new_in_memory().unwrap();
    let content = "Привет 🌟 test";
    let c = NoteChunk {
        id: "id1".to_string(),
        note_id: "note-1".to_string(),
        content: content.to_string(),
        content_hash: content_hash_for(content),
        heading_path: vec![],
        start_offset: 0,
        end_offset: content.len(),
    };
    assert!(c.validate().is_ok());
    s.upsert_chunks(&[c.clone()]).await.unwrap();
    let loaded = s.get_chunks("note-1").await.unwrap();
    assert_eq!(loaded[0].start_offset, 0);
    assert_eq!(loaded[0].end_offset, content.len());
    assert!(content.is_char_boundary(loaded[0].start_offset));
    assert!(content.is_char_boundary(loaded[0].end_offset));
}

#[tokio::test]
async fn composite_pk_keeps_history_and_cleanup() {
    let s = SqliteStore::new_in_memory().unwrap();
    let c = chunk_for("note-1", "history", 0, 7);
    s.upsert_chunks(&[c.clone()]).await.unwrap();
    let r1 = rec_for(&c.id, &c.content_hash, "m", "fp-a", 2, vec![1.0, 1.0]);
    let r2 = rec_for(&c.id, &c.content_hash, "m", "fp-b", 2, vec![2.0, 2.0]);
    s.upsert(&[r1]).await.unwrap();
    s.upsert(&[r2]).await.unwrap();
    assert_eq!(s.get_any(&c.id, "m").await.unwrap().len(), 2);
    // delete obsolete fingerprint only
    s.delete_for_model(&c.id, &model_ref("m", "fp-a")).await.unwrap();
    let remaining = s.get_any(&c.id, "m").await.unwrap();
    assert_eq!(remaining.len(), 1);
    assert_eq!(remaining[0].model_fingerprint, "fp-b");
}

#[tokio::test]
async fn temp_file_auto_cleanup() {
    let path;
    {
        let s = SqliteStore::new_in_memory().unwrap();
        path = s.path.clone();
        assert!(path.exists());
        let c = chunk_for("note-1", "cleanup", 0, 7);
        s.upsert_chunks(&[c]).await.unwrap();
    }
    // After Drop, TempDir should have removed the file
    assert!(!path.exists(), "temp file should be cleaned up after Drop, got {:?}", path);
}
