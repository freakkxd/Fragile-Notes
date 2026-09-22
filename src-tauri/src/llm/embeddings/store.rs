use super::types::{ChunkDiff, EmbeddingError, EmbeddingModelRef, EmbeddingRecord, IndexRun, IndexStatus, NoteChunk};
use async_trait::async_trait;
use rusqlite::{params, Connection, OptionalExtension};
use std::path::{Path, PathBuf};

// ChunkStore and EmbeddingStore are intentionally separate:
// chunks can exist without vectors, vectors are optional and model-specific.
// `vector` is stored as little-endian f32 BLOB, not JSON.

#[async_trait]
pub trait ChunkStore: Send + Sync {
    async fn get_chunks(&self, note_id: &str) -> Result<Vec<NoteChunk>, EmbeddingError>;
    async fn upsert_chunks(&self, chunks: &[NoteChunk]) -> Result<ChunkDiff, EmbeddingError>;
    async fn delete_chunks(&self, chunk_ids: &[String]) -> Result<(), EmbeddingError>;
}

#[async_trait]
pub trait EmbeddingStore: Send + Sync {
    async fn get(
        &self,
        chunk_id: &str,
        model: &EmbeddingModelRef,
    ) -> Result<Option<EmbeddingRecord>, EmbeddingError>;
    async fn get_any(
        &self,
        chunk_id: &str,
        model_id: &str,
    ) -> Result<Vec<EmbeddingRecord>, EmbeddingError>;
    async fn upsert(&self, records: &[EmbeddingRecord]) -> Result<(), EmbeddingError>;
    async fn delete_for_chunks(&self, chunk_ids: &[String]) -> Result<(), EmbeddingError>;
    async fn delete_for_model(
        &self,
        chunk_id: &str,
        model: &EmbeddingModelRef,
    ) -> Result<(), EmbeddingError>;
}

pub struct SqliteStore {
    pub(crate) path: PathBuf,
    // RAII temp dir for test stores — auto-cleanup on Drop
    _temp_dir: Option<tempfile::TempDir>,
}

impl SqliteStore {
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            _temp_dir: None,
        }
    }

    /// In-memory store for tests. Uses temp file with TempDir for auto-cleanup.
    pub fn new_in_memory() -> Result<Self, EmbeddingError> {
        let dir = tempfile::tempdir().map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        let path = dir.path().join("embeddings.db");
        let store = Self {
            path,
            _temp_dir: Some(dir),
        };
        store.migrate()?;
        Ok(store)
    }

    pub fn new_temp_file() -> Result<Self, EmbeddingError> {
        let dir = tempfile::tempdir().map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        let path = dir.path().join("embeddings.db");
        let store = Self {
            path,
            _temp_dir: Some(dir),
        };
        store.migrate()?;
        Ok(store)
    }

    /// For tests that need to reopen same DB path (shared view). Clones path without TempDir.
    pub(crate) fn reopen_shared(&self) -> Self {
        Self {
            path: self.path.clone(),
            _temp_dir: None,
        }
    }

    fn is_memory_uri(&self) -> bool {
        let s = self.path.to_string_lossy();
        s.starts_with("file:memdb_") || s == ":memory:"
    }

    pub(crate) fn open(&self) -> Result<Connection, EmbeddingError> {
        let path_str = self.path.to_string_lossy().to_string();
        let conn = if path_str == ":memory:" {
            Connection::open_in_memory().map_err(|e| EmbeddingError::Provider(e.to_string()))?
        } else {
            Connection::open(&self.path).map_err(|e| EmbeddingError::Provider(e.to_string()))?
        };
        conn.execute_batch("PRAGMA foreign_keys = ON;")
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        if !self.is_memory_uri() {
            let _ = conn.execute_batch("PRAGMA journal_mode = WAL;");
        } else {
            let _ = conn.execute_batch("PRAGMA journal_mode = MEMORY;");
        }
        Ok(conn)
    }

    pub fn migrate(&self) -> Result<(), EmbeddingError> {
        let conn = self.open()?;
        // Check existing schema to decide if migration needed
        let needs_recreate = {
            let mut needs = false;
            if let Ok(mut stmt) = conn.prepare(
                "select sql from sqlite_master where type='table' and name='embedding_vectors'",
            ) {
                if let Ok(mut rows) = stmt.query([]) {
                    if let Ok(Some(row)) = rows.next() {
                        let sql: Option<String> = row.get(0).unwrap_or(None);
                        if let Some(s) = sql {
                            // old PK is 2-col, new is 3-col with fingerprint
                            if s.contains("primary key (chunk_id, model_id)")
                                && !s.contains("model_fingerprint")
                            {
                                needs = true;
                            }
                            if s.contains("primary key (chunk_id, model_id)")
                                && !s.contains("primary key (chunk_id, model_id, model_fingerprint)")
                            {
                                // if it's 2-col PK, needs recreate
                                // detect by exact string
                                if s.contains("\"chunk_id\", \"model_id\"") || s.contains("(chunk_id, model_id)") {
                                    // check if fingerprint is part of PK
                                    if !s.contains("model_fingerprint") || !s.contains("(chunk_id, model_id, model_fingerprint)") {
                                        // For old DB with 2-col PK, mark needs
                                        // We check more precisely below
                                    }
                                }
                            }
                        }
                    }
                }
            }
            // More robust: check via table_info + index for PK columns
            if !needs {
                if let Ok(sql) = conn
                    .query_row(
                        "select sql from sqlite_master where type='table' and name='embedding_vectors'",
                        [],
                        |r| r.get::<_, Option<String>>(0),
                    )
                {
                    if let Some(s) = sql {
                        let has_fp_in_pk = s.contains("model_fingerprint") && s.contains("primary key") && {
                            // crude: PK line contains fingerprint
                            s.to_lowercase().contains("model_fingerprint")
                                && s.to_lowercase().contains("primary key (chunk_id, model_id, model_fingerprint)")
                        };
                        if !has_fp_in_pk && s.contains("embedding_vectors") {
                            // If table exists but not composite PK, need recreate
                            // We consider any existing table with 2-col PK as needing migration
                            // Only if table was created by old code (2-col)
                            // We'll verify by checking if table exists and PK is 2-col via pragma
                            needs = s.contains("primary key (chunk_id, model_id)") && !s.contains("primary key (chunk_id, model_id, model_fingerprint)");
                        }
                    }
                }
            }
            needs
        };

        if needs_recreate {
            // Migrate 2-col PK -> 3-col PK
            conn.execute_batch(
                r#"
                create table if not exists embedding_vectors_new (
                    chunk_id text not null,
                    model_id text not null,
                    model_fingerprint text not null,
                    dimensions integer not null,
                    vector_blob blob not null,
                    content_hash text not null,
                    created_at text not null,
                    updated_at text not null,
                    primary key (chunk_id, model_id, model_fingerprint),
                    foreign key (chunk_id) references embedding_chunks(chunk_id) on delete cascade
                );
                insert or ignore into embedding_vectors_new
                    (chunk_id, model_id, model_fingerprint, dimensions, vector_blob, content_hash, created_at, updated_at)
                    select chunk_id, model_id, coalesce(model_fingerprint,''), dimensions, vector_blob, content_hash, created_at, updated_at from embedding_vectors;
                drop table embedding_vectors;
                alter table embedding_vectors_new rename to embedding_vectors;
                create index if not exists idx_embedding_vectors_model on embedding_vectors(model_id);
                "#,
            )
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        } else {
            conn.execute_batch(
                r#"
                create table if not exists embedding_chunks (
                    chunk_id text primary key,
                    note_id text not null,
                    content text not null,
                    content_hash text not null,
                    heading_path_json text not null,
                    start_offset integer not null,
                    end_offset integer not null,
                    created_at text not null,
                    updated_at text not null
                );
                create index if not exists idx_embedding_chunks_note on embedding_chunks(note_id);

                create table if not exists embedding_vectors (
                    chunk_id text not null,
                    model_id text not null,
                    model_fingerprint text not null,
                    dimensions integer not null,
                    vector_blob blob not null,
                    content_hash text not null,
                    created_at text not null,
                    updated_at text not null,
                    primary key (chunk_id, model_id, model_fingerprint),
                    foreign key (chunk_id) references embedding_chunks(chunk_id) on delete cascade
                );
                create index if not exists idx_embedding_vectors_model on embedding_vectors(model_id);

                create table if not exists embedding_index_runs (
                    id text primary key,
                    note_id text not null,
                    source_hash text not null,
                    status text not null,
                    processed_chunks integer not null,
                    total_chunks integer not null,
                    error text,
                    created_at text not null,
                    updated_at text not null
                );

                create table if not exists schema_migrations (
                    version text primary key,
                    applied_at text not null
                );
                "#,
            )
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        }

        // Ensure model_fingerprint column exists (for very old DBs)
        {
            let mut has_fp = false;
            if let Ok(mut stmt) = conn.prepare("pragma table_info(embedding_vectors)") {
                if let Ok(rows) = stmt.query_map([], |row| {
                    let name: String = row.get(1)?;
                    Ok(name)
                }) {
                    for r in rows.flatten() {
                        if r == "model_fingerprint" {
                            has_fp = true;
                            break;
                        }
                    }
                }
            }
            if !has_fp {
                let _ = conn.execute_batch(
                    "alter table embedding_vectors add column model_fingerprint text not null default '';",
                );
            }
        }

        let now = chrono::Utc::now().to_rfc3339();
        conn.execute(
            "insert or ignore into schema_migrations (version, applied_at) values (?1, ?2)",
            params!["v0.5.8-embeddings-1", now],
        )
        .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        // Bump to v2 for composite PK
        conn.execute(
            "insert or ignore into schema_migrations (version, applied_at) values (?1, ?2)",
            params!["v0.5.8-embeddings-2", now],
        )
        .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        Ok(())
    }

    fn chunk_from_row(row: &rusqlite::Row) -> rusqlite::Result<NoteChunk> {
        let heading_json: String = row.get(4)?;
        let heading_path: Vec<String> = serde_json::from_str(&heading_json).unwrap_or_default();
        Ok(NoteChunk {
            id: row.get(0)?,
            note_id: row.get(1)?,
            content: row.get(2)?,
            content_hash: row.get(3)?,
            heading_path,
            start_offset: row.get::<_, i64>(5)? as usize,
            end_offset: row.get::<_, i64>(6)? as usize,
        })
    }

    fn record_from_row(row: &rusqlite::Row) -> rusqlite::Result<EmbeddingRecord> {
        let blob: Vec<u8> = row.get(4)?;
        let dimensions_i64: i64 = row.get(3)?;
        let dimensions = dimensions_i64 as usize;
        let vector = blob_to_f32_vec(&blob).map_err(|e| {
            // Use InvalidParameterName as carrier for corrupt blob; will be mapped to CorruptVectorBlob in get()
            rusqlite::Error::InvalidParameterName(format!("corrupt_vector_blob: {}", e))
        })?;
        if vector.len() != dimensions {
            return Err(rusqlite::Error::InvalidParameterName(format!(
                "corrupt_vector_blob: dimension mismatch stored {} vs blob {}",
                dimensions,
                vector.len()
            )));
        }
        Ok(EmbeddingRecord {
            chunk_id: row.get(0)?,
            model_id: row.get(1)?,
            model_fingerprint: row.get(2)?,
            content_hash: row.get(5)?,
            dimensions,
            vector,
        })
    }

    fn map_corrupt(e: String) -> EmbeddingError {
        if e.contains("corrupt_vector_blob") {
            EmbeddingError::CorruptVectorBlob(e)
        } else {
            EmbeddingError::Provider(e)
        }
    }
}

fn f32_vec_to_blob(vec: &[f32]) -> Vec<u8> {
    let mut blob = Vec::with_capacity(vec.len() * 4);
    for v in vec {
        blob.extend_from_slice(&v.to_le_bytes());
    }
    blob
}

fn blob_to_f32_vec(blob: &[u8]) -> Result<Vec<f32>, String> {
    if blob.len() % std::mem::size_of::<f32>() != 0 {
        return Err(format!("blob len {} not multiple of 4", blob.len()));
    }
    let mut vec = Vec::with_capacity(blob.len() / 4);
    for chunk in blob.chunks_exact(4) {
        let bytes: [u8; 4] = chunk.try_into().unwrap();
        let v = f32::from_le_bytes(bytes);
        if !v.is_finite() {
            return Err("non-finite value in blob".to_string());
        }
        vec.push(v);
    }
    Ok(vec)
}

impl Drop for SqliteStore {
    fn drop(&mut self) {
        // TempDir auto-cleans via _temp_dir. For legacy /tmp files without TempDir, clean explicitly.
        if self._temp_dir.is_none() && self.path.starts_with("/tmp") {
            let fname = self.path.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if fname.starts_with("fragile_test") {
                let _ = std::fs::remove_file(&self.path);
                // Also try WAL/shm
                let _ = std::fs::remove_file(self.path.with_extension("db-wal"));
                let _ = std::fs::remove_file(self.path.with_extension("db-shm"));
            }
        }
    }
}

#[async_trait]
impl ChunkStore for SqliteStore {
    async fn get_chunks(&self, note_id: &str) -> Result<Vec<NoteChunk>, EmbeddingError> {
        let conn = self.open()?;
        let mut stmt = conn
            .prepare("select chunk_id, note_id, content, content_hash, heading_path_json, start_offset, end_offset from embedding_chunks where note_id = ?1 order by start_offset")
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        let rows = stmt
            .query_map(params![note_id], |row| Self::chunk_from_row(row))
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r.map_err(|e| Self::map_corrupt(e.to_string()))?);
        }
        Ok(out)
    }

    async fn upsert_chunks(&self, chunks: &[NoteChunk]) -> Result<ChunkDiff, EmbeddingError> {
        if chunks.is_empty() {
            return Ok(ChunkDiff {
                added: vec![],
                changed: vec![],
                unchanged: vec![],
                deleted_ids: vec![],
            });
        }
        for c in chunks {
            c.validate().map_err(|e| EmbeddingError::Provider(format!("chunk validate: {}", e)))?;
        }
        let note_id = chunks[0].note_id.clone();
        for c in chunks {
            if c.note_id != note_id {
                return Err(EmbeddingError::Provider(
                    "all chunks must have same note_id".to_string(),
                ));
            }
        }
        {
            let mut id_set = std::collections::HashSet::new();
            for c in chunks {
                if !id_set.insert(c.id.clone()) {
                    return Err(EmbeddingError::Provider(format!("duplicate chunk id {}", c.id)));
                }
            }
        }

        let mut conn = self.open()?;
        let tx = conn
            .transaction()
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;

        let existing: Vec<NoteChunk> = {
            let mut stmt = tx
                .prepare("select chunk_id, note_id, content, content_hash, heading_path_json, start_offset, end_offset from embedding_chunks where note_id = ?1")
                .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
            let rows = stmt
                .query_map(params![note_id], |row| Self::chunk_from_row(row))
                .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
            let mut v = Vec::new();
            for r in rows {
                v.push(r.map_err(|e| Self::map_corrupt(e.to_string()))?);
            }
            v
        };

        let existing_by_id: std::collections::HashMap<String, NoteChunk> =
            existing.into_iter().map(|c| (c.id.clone(), c)).collect();
        let mut added = Vec::new();
        let mut changed = Vec::new();
        let mut unchanged = Vec::new();
        let mut seen_ids = std::collections::HashSet::new();

        for chunk in chunks {
            seen_ids.insert(chunk.id.clone());
            if let Some(old) = existing_by_id.get(&chunk.id) {
                if old.content_hash == chunk.content_hash {
                    unchanged.push(chunk.clone());
                } else {
                    changed.push(chunk.clone());
                }
            } else {
                added.push(chunk.clone());
            }
        }

        let deleted_ids: Vec<String> = existing_by_id
            .keys()
            .filter(|id| !seen_ids.contains(*id))
            .cloned()
            .collect();

        let now = chrono::Utc::now().to_rfc3339();
        for chunk in chunks {
            let heading_json = serde_json::to_string(&chunk.heading_path).unwrap();
            tx.execute(
                "insert into embedding_chunks (chunk_id, note_id, content, content_hash, heading_path_json, start_offset, end_offset, created_at, updated_at)
                 values (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?8)
                 on conflict(chunk_id) do update set note_id=excluded.note_id, content=excluded.content, content_hash=excluded.content_hash, heading_path_json=excluded.heading_path_json, start_offset=excluded.start_offset, end_offset=excluded.end_offset, updated_at=excluded.updated_at",
                params![
                    chunk.id,
                    chunk.note_id,
                    chunk.content,
                    chunk.content_hash,
                    heading_json,
                    chunk.start_offset as i64,
                    chunk.end_offset as i64,
                    now
                ],
            )
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        }

        for del_id in &deleted_ids {
            tx.execute(
                "delete from embedding_chunks where chunk_id = ?1",
                params![del_id],
            )
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        }

        for ch in &changed {
            tx.execute(
                "delete from embedding_vectors where chunk_id = ?1",
                params![ch.id],
            )
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        }

        tx.commit()
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;

        Ok(ChunkDiff {
            added,
            changed,
            unchanged,
            deleted_ids,
        })
    }

    async fn delete_chunks(&self, chunk_ids: &[String]) -> Result<(), EmbeddingError> {
        if chunk_ids.is_empty() {
            return Ok(());
        }
        let mut conn = self.open()?;
        let tx = conn
            .transaction()
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        for id in chunk_ids {
            tx.execute(
                "delete from embedding_chunks where chunk_id = ?1",
                params![id],
            )
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        }
        tx.commit()
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        Ok(())
    }
}

#[async_trait]
impl EmbeddingStore for SqliteStore {
    async fn get(
        &self,
        chunk_id: &str,
        model: &EmbeddingModelRef,
    ) -> Result<Option<EmbeddingRecord>, EmbeddingError> {
        let conn = self.open()?;
        let mut stmt = conn
            .prepare("select chunk_id, model_id, model_fingerprint, dimensions, vector_blob, content_hash from embedding_vectors where chunk_id = ?1 and model_id = ?2 and model_fingerprint = ?3")
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        let row = stmt
            .query_row(
                params![chunk_id, model.model_id, model.model_fingerprint],
                |row| Self::record_from_row(row),
            )
            .optional()
            .map_err(|e| Self::map_corrupt(e.to_string()))?;
        Ok(row)
    }

    async fn get_any(
        &self,
        chunk_id: &str,
        model_id: &str,
    ) -> Result<Vec<EmbeddingRecord>, EmbeddingError> {
        let conn = self.open()?;
        let mut stmt = conn
            .prepare("select chunk_id, model_id, model_fingerprint, dimensions, vector_blob, content_hash from embedding_vectors where chunk_id = ?1 and model_id = ?2")
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        let rows = stmt
            .query_map(params![chunk_id, model_id], |row| Self::record_from_row(row))
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r.map_err(|e| Self::map_corrupt(e.to_string()))?);
        }
        Ok(out)
    }

    async fn upsert(&self, records: &[EmbeddingRecord]) -> Result<(), EmbeddingError> {
        if records.is_empty() {
            return Ok(());
        }
        let limits = super::types::EmbeddingLimits::default();
        for rec in records {
            rec.validate(&limits)
                .map_err(|e| EmbeddingError::Provider(format!("record validate: {:?}", e)))?;
        }

        let mut conn = self.open()?;
        let tx = conn
            .transaction()
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        let now = chrono::Utc::now().to_rfc3339();
        for rec in records {
            let blob = f32_vec_to_blob(&rec.vector);
            tx.execute(
                "insert into embedding_vectors (chunk_id, model_id, model_fingerprint, dimensions, vector_blob, content_hash, created_at, updated_at)
                 values (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?7)
                 on conflict(chunk_id, model_id, model_fingerprint) do update set dimensions=excluded.dimensions, vector_blob=excluded.vector_blob, content_hash=excluded.content_hash, updated_at=excluded.updated_at",
                params![
                    rec.chunk_id,
                    rec.model_id,
                    rec.model_fingerprint,
                    rec.dimensions as i64,
                    blob,
                    rec.content_hash,
                    now
                ],
            )
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        }
        tx.commit()
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        Ok(())
    }

    async fn delete_for_chunks(&self, chunk_ids: &[String]) -> Result<(), EmbeddingError> {
        if chunk_ids.is_empty() {
            return Ok(());
        }
        let mut conn = self.open()?;
        let tx = conn
            .transaction()
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        for id in chunk_ids {
            tx.execute(
                "delete from embedding_vectors where chunk_id = ?1",
                params![id],
            )
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        }
        tx.commit()
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        Ok(())
    }

    async fn delete_for_model(
        &self,
        chunk_id: &str,
        model: &EmbeddingModelRef,
    ) -> Result<(), EmbeddingError> {
        let conn = self.open()?;
        conn.execute(
            "delete from embedding_vectors where chunk_id = ?1 and model_id = ?2 and model_fingerprint = ?3",
            params![chunk_id, model.model_id, model.model_fingerprint],
        )
        .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        Ok(())
    }
}

// IndexRun helpers — no scheduler, just persisted state for atomicity
impl SqliteStore {
    pub async fn upsert_index_run(&self, run: &IndexRun) -> Result<(), EmbeddingError> {
        let conn = self.open()?;
        let status_str = match run.status {
            IndexStatus::Pending => "Pending",
            IndexStatus::Running => "Running",
            IndexStatus::Completed => "Completed",
            IndexStatus::Failed => "Failed",
            IndexStatus::Cancelled => "Cancelled",
        };
        conn.execute(
            "insert into embedding_index_runs (id, note_id, source_hash, status, processed_chunks, total_chunks, error, created_at, updated_at)
             values (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?8)
             on conflict(id) do update set status=excluded.status, processed_chunks=excluded.processed_chunks, total_chunks=excluded.total_chunks, error=excluded.error, updated_at=excluded.updated_at",
            params![
                run.id,
                run.note_id,
                run.source_hash,
                status_str,
                run.processed_chunks as i64,
                run.total_chunks as i64,
                run.error,
                chrono::Utc::now().to_rfc3339()
            ],
        )
        .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        Ok(())
    }

    pub async fn get_index_run(&self, id: &str) -> Result<Option<IndexRun>, EmbeddingError> {
        let conn = self.open()?;
        let mut stmt = conn
            .prepare("select id, note_id, source_hash, status, processed_chunks, total_chunks, error from embedding_index_runs where id = ?1")
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        let row = stmt
            .query_row(params![id], |row| {
                let status_str: String = row.get(3)?;
                let status = match status_str.as_str() {
                    "Pending" => IndexStatus::Pending,
                    "Running" => IndexStatus::Running,
                    "Completed" => IndexStatus::Completed,
                    "Failed" => IndexStatus::Failed,
                    "Cancelled" => IndexStatus::Cancelled,
                    _ => IndexStatus::Failed,
                };
                Ok(IndexRun {
                    id: row.get(0)?,
                    note_id: row.get(1)?,
                    source_hash: row.get(2)?,
                    status,
                    processed_chunks: row.get::<_, i64>(4)? as usize,
                    total_chunks: row.get::<_, i64>(5)? as usize,
                    error: row.get(6)?,
                })
            })
            .optional()
            .map_err(|e| EmbeddingError::Provider(e.to_string()))?;
        Ok(row)
    }
}

#[cfg(test)]
mod unit_blob {
    use super::*;

    #[test]
    fn blob_round_trip() {
        let v = vec![0.1f32, -0.2, 3.14, f32::MAX, f32::MIN];
        let blob = f32_vec_to_blob(&v);
        assert_eq!(blob.len(), v.len() * 4);
        let out = blob_to_f32_vec(&blob).unwrap();
        assert_eq!(out, v);
        assert_eq!(&blob[0..4], &v[0].to_le_bytes());
    }

    #[test]
    fn blob_rejects_non_multiple() {
        assert!(blob_to_f32_vec(&[0, 1, 2]).is_err());
        assert!(blob_to_f32_vec(&[0, 1, 2, 3, 4]).is_err());
    }

    #[test]
    fn blob_rejects_non_finite() {
        let mut blob = f32_vec_to_blob(&[1.0, 2.0]);
        let nan_bytes = f32::NAN.to_le_bytes();
        blob[4..8].copy_from_slice(&nan_bytes);
        assert!(blob_to_f32_vec(&blob).is_err());
        let mut blob2 = f32_vec_to_blob(&[1.0, 2.0]);
        let inf_bytes = f32::INFINITY.to_le_bytes();
        blob2[0..4].copy_from_slice(&inf_bytes);
        assert!(blob_to_f32_vec(&blob2).is_err());
    }
}
