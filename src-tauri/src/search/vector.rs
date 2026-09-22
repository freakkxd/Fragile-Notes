use super::types::{
    SearchError, VectorModelFilter, VectorQuery, VectorSearchLimits, VectorSearchResult,
};
use crate::llm::embeddings::types::EmbeddingRecord;
use async_trait::async_trait;
use rusqlite::{params, Connection};
use std::path::PathBuf;

#[async_trait]
pub trait VectorStore: Send + Sync {
    async fn upsert(&self, records: &[EmbeddingRecord]) -> Result<(), SearchError>;
    async fn delete_for_chunks(&self, chunk_ids: &[String]) -> Result<(), SearchError>;
    async fn search(&self, query: VectorQuery) -> Result<Vec<VectorSearchResult>, SearchError>;
    async fn has_vectors(&self, model: &VectorModelFilter) -> Result<bool, SearchError>;
}

pub struct SqliteVectorStore {
    path: PathBuf,
    limits: VectorSearchLimits,
    _temp_dir: Option<tempfile::TempDir>,
}

impl SqliteVectorStore {
    pub fn new(path: PathBuf, limits: VectorSearchLimits) -> Self {
        Self {
            path,
            limits,
            _temp_dir: None,
        }
    }

    pub fn new_in_memory(limits: VectorSearchLimits) -> Result<Self, SearchError> {
        let dir = tempfile::tempdir().map_err(|e| SearchError::Internal(e.to_string()))?;
        let path = dir.path().join("vectors.db");
        let store = Self {
            path,
            limits,
            _temp_dir: Some(dir),
        };
        store.migrate()?;
        Ok(store)
    }

    pub fn new_with_limits(path: PathBuf) -> Self {
        Self::new(path, VectorSearchLimits::default())
    }

    pub fn from_embedding_store(path: PathBuf) -> Self {
        Self {
            path,
            limits: VectorSearchLimits::default(),
            _temp_dir: None,
        }
    }

    fn open(&self) -> Result<Connection, SearchError> {
        let conn = Connection::open(&self.path)
            .map_err(|e| SearchError::Internal(e.to_string()))?;
        conn.execute_batch("PRAGMA foreign_keys = ON;")
            .map_err(|e| SearchError::Internal(e.to_string()))?;
        Ok(conn)
    }

    fn migrate(&self) -> Result<(), SearchError> {
        let conn = self.open()?;
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
            "#,
        )
        .map_err(|e| SearchError::Internal(e.to_string()))?;
        Ok(())
    }

    pub fn with_path_for_test(&self) -> PathBuf {
        self.path.clone()
    }
}

fn escape_like_literal(s: &str) -> String {
    s.replace('\\', "\\\\").replace('%', "\\%").replace('_', "\\_")
}

fn dot(a: &[f32], b: &[f32]) -> f32 {
    a.iter().zip(b.iter()).map(|(x, y)| x * y).sum()
}

fn norm(v: &[f32]) -> f32 {
    dot(v, v).sqrt()
}

fn cosine(a: &[f32], b: &[f32]) -> Result<f32, SearchError> {
    if a.len() != b.len() {
        return Err(SearchError::DimensionMismatch {
            expected: a.len(),
            actual: b.len(),
        });
    }
    let mut dot_sum = 0.0f32;
    let mut norm_a = 0.0f32;
    let mut norm_b = 0.0f32;
    for (x, y) in a.iter().zip(b.iter()) {
        if !x.is_finite() || !y.is_finite() {
            return Err(SearchError::InvalidVector("non-finite value".to_string()));
        }
        dot_sum += x * y;
        norm_a += x * x;
        norm_b += y * y;
    }
    let n_a = norm_a.sqrt();
    let n_b = norm_b.sqrt();
    if n_a == 0.0 || n_b == 0.0 || !n_a.is_finite() || !n_b.is_finite() {
        return Err(SearchError::InvalidVector("zero-norm vector".to_string()));
    }
    let mut score = dot_sum / (n_a * n_b);
    // Clamp due to float errors
    if score > 1.0 {
        score = 1.0;
    } else if score < -1.0 {
        score = -1.0;
    }
    if !score.is_finite() {
        return Err(SearchError::InvalidVector("non-finite score".to_string()));
    }
    Ok(score)
}

fn blob_to_f32_vec(blob: &[u8]) -> Result<Vec<f32>, SearchError> {
    if blob.len() % 4 != 0 {
        return Err(SearchError::CorruptVectorBlob(format!(
            "blob len {} not multiple of 4",
            blob.len()
        )));
    }
    let mut out = Vec::with_capacity(blob.len() / 4);
    for chunk in blob.chunks_exact(4) {
        let arr: [u8; 4] = chunk.try_into().unwrap();
        let v = f32::from_le_bytes(arr);
        if !v.is_finite() {
            return Err(SearchError::CorruptVectorBlob(
                "non-finite in blob".to_string(),
            ));
        }
        out.push(v);
    }
    Ok(out)
}

#[async_trait]
impl VectorStore for SqliteVectorStore {
    async fn upsert(&self, records: &[EmbeddingRecord]) -> Result<(), SearchError> {
        if records.is_empty() {
            return Ok(());
        }
        let conn = self.open()?;
        let mut tx_conn = conn;
        let tx = tx_conn
            .transaction()
            .map_err(|e| SearchError::Internal(e.to_string()))?;
        for rec in records {
            // Validate finite already in EmbeddingRecord::validate, but re-check for SearchError
            for &v in &rec.vector {
                if !v.is_finite() {
                    return Err(SearchError::InvalidVector("non-finite".to_string()));
                }
            }
            if rec.vector.is_empty() {
                return Err(SearchError::InvalidVector("empty vector".to_string()));
            }
            let blob: Vec<u8> = rec.vector.iter().flat_map(|f| f.to_le_bytes()).collect();
            tx.execute(
                "INSERT INTO embedding_vectors (chunk_id, model_id, model_fingerprint, dimensions, vector_blob, content_hash, created_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, datetime('now'), datetime('now'))
                 ON CONFLICT(chunk_id, model_id, model_fingerprint) DO UPDATE SET dimensions=excluded.dimensions, vector_blob=excluded.vector_blob, content_hash=excluded.content_hash, updated_at=excluded.updated_at",
                params![
                    rec.chunk_id,
                    rec.model_id,
                    rec.model_fingerprint,
                    rec.dimensions as i64,
                    blob,
                    rec.content_hash
                ],
            )
            .map_err(|e| SearchError::Internal(e.to_string()))?;
        }
        tx.commit()
            .map_err(|e| SearchError::Internal(e.to_string()))?;
        Ok(())
    }

    async fn delete_for_chunks(&self, chunk_ids: &[String]) -> Result<(), SearchError> {
        if chunk_ids.is_empty() {
            return Ok(());
        }
        let conn = self.open()?;
        let mut tx_conn = conn;
        let tx = tx_conn
            .transaction()
            .map_err(|e| SearchError::Internal(e.to_string()))?;
        for id in chunk_ids {
            tx.execute(
                "DELETE FROM embedding_vectors WHERE chunk_id = ?1",
                params![id],
            )
            .map_err(|e| SearchError::Internal(e.to_string()))?;
        }
        tx.commit()
            .map_err(|e| SearchError::Internal(e.to_string()))?;
        Ok(())
    }

    async fn has_vectors(&self, model: &VectorModelFilter) -> Result<bool, SearchError> {
        let conn = self.open()?;
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM embedding_vectors WHERE model_id = ?1 AND model_fingerprint = ?2",
                params![model.model_id, model.model_fingerprint],
                |r| r.get(0),
            )
            .map_err(|e| SearchError::Internal(e.to_string()))?;
        Ok(count > 0)
    }

    async fn search(&self, query: VectorQuery) -> Result<Vec<VectorSearchResult>, SearchError> {
        query.validate()?;

        // Check dimensions vs stored? We need to ensure query dims match index dims; we don't have global dims, check per candidate
        let conn = self.open()?;

        // Enforce candidate limits: first count candidates
        let filter_like = query
            .note_filter
            .as_ref()
            .map(|f| format!("%{}%", escape_like_literal(f)));

        // Build count query with same filters
        let count: i64 = if let Some(ref like) = filter_like {
            let mut stmt = conn
                .prepare(
                    "SELECT COUNT(*) FROM embedding_vectors v JOIN embedding_chunks c ON v.chunk_id = c.chunk_id
                     WHERE v.model_id = ?1 AND v.model_fingerprint = ?2 AND c.note_id LIKE ?3 ESCAPE '\\'",
                )
                .map_err(|e| SearchError::Internal(e.to_string()))?;
            stmt.query_row(
                params![query.model.model_id, query.model.model_fingerprint, like],
                |r| r.get(0),
            )
            .map_err(|e| SearchError::Internal(e.to_string()))?
        } else {
            let mut stmt = conn
                .prepare(
                    "SELECT COUNT(*) FROM embedding_vectors v WHERE v.model_id = ?1 AND v.model_fingerprint = ?2",
                )
                .map_err(|e| SearchError::Internal(e.to_string()))?;
            stmt.query_row(
                params![query.model.model_id, query.model.model_fingerprint],
                |r| r.get(0),
            )
            .map_err(|e| SearchError::Internal(e.to_string()))?
        };

        if count as usize > self.limits.max_candidates {
            return Err(SearchError::LimitExceeded(format!(
                "candidates {} exceeds max {}",
                count, self.limits.max_candidates
            )));
        }
        let bytes_estimate = count as usize * query.vector.len() * 4;
        if bytes_estimate > self.limits.max_vector_bytes {
            return Err(SearchError::LimitExceeded(format!(
                "vector bytes {} exceeds max {}",
                bytes_estimate, self.limits.max_vector_bytes
            )));
        }

        // Load candidates
        struct Row {
            chunk_id: String,
            note_id: String,
            content: String,
            heading_json: String,
            dims: usize,
            blob: Vec<u8>,
        }
        let mut rows: Vec<Row> = Vec::new();
        if let Some(ref like) = filter_like {
            let mut stmt = conn
                .prepare(
                    "SELECT v.chunk_id, c.note_id, c.content, c.heading_path_json, v.dimensions, v.vector_blob
                     FROM embedding_vectors v JOIN embedding_chunks c ON v.chunk_id = c.chunk_id
                     WHERE v.model_id = ?1 AND v.model_fingerprint = ?2 AND c.note_id LIKE ?3 ESCAPE '\\'",
                )
                .map_err(|e| SearchError::Internal(e.to_string()))?;
            let iter = stmt
                .query_map(
                    params![query.model.model_id, query.model.model_fingerprint, like],
                    |row| {
                        Ok(Row {
                            chunk_id: row.get(0)?,
                            note_id: row.get(1)?,
                            content: row.get(2)?,
                            heading_json: row.get(3)?,
                            dims: row.get::<_, i64>(4)? as usize,
                            blob: row.get(5)?,
                        })
                    },
                )
                .map_err(|e| SearchError::Internal(e.to_string()))?;
            for r in iter {
                rows.push(r.map_err(|e| SearchError::Internal(e.to_string()))?);
            }
        } else {
            let mut stmt = conn
                .prepare(
                    "SELECT v.chunk_id, c.note_id, c.content, c.heading_path_json, v.dimensions, v.vector_blob
                     FROM embedding_vectors v JOIN embedding_chunks c ON v.chunk_id = c.chunk_id
                     WHERE v.model_id = ?1 AND v.model_fingerprint = ?2",
                )
                .map_err(|e| SearchError::Internal(e.to_string()))?;
            let iter = stmt
                .query_map(
                    params![query.model.model_id, query.model.model_fingerprint],
                    |row| {
                        Ok(Row {
                            chunk_id: row.get(0)?,
                            note_id: row.get(1)?,
                            content: row.get(2)?,
                            heading_json: row.get(3)?,
                            dims: row.get::<_, i64>(4)? as usize,
                            blob: row.get(5)?,
                        })
                    },
                )
                .map_err(|e| SearchError::Internal(e.to_string()))?;
            for r in iter {
                rows.push(r.map_err(|e| SearchError::Internal(e.to_string()))?);
            }
        }

        if rows.is_empty() {
            return Ok(vec![]);
        }

        // Compute cosine for each, validate dims and zero-norm
        let q_norm = norm(&query.vector);
        if q_norm == 0.0 || !q_norm.is_finite() {
            return Err(SearchError::InvalidVector("zero-norm query".to_string()));
        }

        let mut results: Vec<VectorSearchResult> = Vec::with_capacity(rows.len());
        for row in rows {
            if row.dims != query.vector.len() {
                return Err(SearchError::DimensionMismatch {
                    expected: query.vector.len(),
                    actual: row.dims,
                });
            }
            let vec = blob_to_f32_vec(&row.blob)?;
            if vec.len() != query.vector.len() {
                return Err(SearchError::DimensionMismatch {
                    expected: query.vector.len(),
                    actual: vec.len(),
                });
            }
            let vec_norm = norm(&vec);
            if vec_norm == 0.0 || !vec_norm.is_finite() {
                return Err(SearchError::InvalidVector(format!(
                    "zero-norm stored vector for chunk {}",
                    row.chunk_id
                )));
            }
            let score = cosine(&query.vector, &vec)?;
            if let Some(min) = query.min_score {
                if score < min {
                    continue;
                }
            }
            let distance = 1.0 - score;
            let heading_path: Vec<String> =
                serde_json::from_str(&row.heading_json).unwrap_or_default();
            results.push(VectorSearchResult {
                chunk_id: row.chunk_id,
                note_id: row.note_id,
                content: row.content,
                heading_path,
                score,
                distance,
                model: query.model.clone(),
            });
        }

        // Sort by score DESC, chunk_id ASC tie-breaker
        results.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| a.chunk_id.cmp(&b.chunk_id))
        });
        results.truncate(query.limit);
        Ok(results)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cosine_identical() {
        let a = vec![1.0, 0.0];
        let b = vec![1.0, 0.0];
        assert!((cosine(&a, &b).unwrap() - 1.0).abs() < 1e-5);
    }

    #[test]
    fn escape_like() {
        assert_eq!(escape_like_literal("a%b_c\\d"), "a\\%b\\_c\\\\d");
    }
}
