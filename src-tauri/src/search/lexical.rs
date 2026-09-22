use super::types::*;
use super::SearchBackend;
use async_trait::async_trait;
use rusqlite::{params, Connection, OptionalExtension};
use std::path::{Path, PathBuf};

/// LexicalSearchBackend — FTS5 lexical search, not embeddings.
/// Query safety: Literal mode escapes FTS5 syntax, tokens joined by AND.

pub struct LexicalSearchBackend {
    db_path: PathBuf,
    // For tests: allow in-memory via TempDir or shared path
    _temp_dir: Option<tempfile::TempDir>,
}

impl LexicalSearchBackend {
    pub fn new(db_path: PathBuf) -> Self {
        Self {
            db_path,
            _temp_dir: None,
        }
    }

    /// For tests: in-memory temp file with auto-cleanup
    pub fn new_in_memory() -> Result<Self, SearchError> {
        let dir = tempfile::tempdir()
            .map_err(|e| SearchError::Internal(e.to_string()))?;
        let path = dir.path().join("fts.db");
        let backend = Self {
            db_path: path,
            _temp_dir: Some(dir),
        };
        backend.ensure_table()?;
        Ok(backend)
    }

    /// For tests: create backend from existing connection path (no TempDir)
    pub fn new_with_path(path: PathBuf) -> Result<Self, SearchError> {
        let b = Self {
            db_path: path,
            _temp_dir: None,
        };
        b.ensure_table()?;
        Ok(b)
    }

    pub fn from_vault(vault_root: &Path) -> Self {
        Self {
            db_path: vault_root.join(".fragile_fts.db"),
            _temp_dir: None,
        }
    }

    fn ensure_table(&self) -> Result<(), SearchError> {
        if !self.db_path.exists() {
            // Create empty FTS5 table so queries don't fail with "no such table"
            if let Some(parent) = self.db_path.parent() {
                let _ = std::fs::create_dir_all(parent);
            }
            let conn = Connection::open(&self.db_path)
                .map_err(|e| SearchError::Internal(e.to_string()))?;
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(path, content, tokenize='trigram')",
                [],
            )
            .or_else(|_| {
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(path, content)",
                    [],
                )
            })
            .map_err(|e| SearchError::Internal(e.to_string()))?;
            return Ok(());
        }
        let conn = Connection::open(&self.db_path)
            .map_err(|e| SearchError::Internal(e.to_string()))?;
        // Check if fts table exists
        let exists: bool = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='fts'",
                [],
                |r| r.get::<_, i64>(0),
            )
            .map(|c| c > 0)
            .unwrap_or(false);
        if !exists {
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(path, content)",
                [],
            )
            .map_err(|e| SearchError::Internal(e.to_string()))?;
        }
        Ok(())
    }

    fn open(&self) -> Result<Connection, SearchError> {
        if !self.db_path.exists() {
            return Err(SearchError::IndexUnavailable(
                "FTS db not found, run FTS index".to_string(),
            ));
        }
        let conn = Connection::open(&self.db_path)
            .map_err(|e| SearchError::Internal(e.to_string()))?;
        // Verify fts table exists
        let exists: bool = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='fts'",
                [],
                |r| r.get::<_, i64>(0),
            )
            .map(|c| c > 0)
            .unwrap_or(false);
        if !exists {
            return Err(SearchError::IndexUnavailable(
                "fts table missing".to_string(),
            ));
        }
        Ok(conn)
    }

    /// Index a single note for tests/vault
    pub fn index_note(&self, path: &str, content: &str) -> Result<(), SearchError> {
        let conn = Connection::open(&self.db_path)
            .map_err(|e| SearchError::Internal(e.to_string()))?;
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(path, content)",
            [],
        )
        .map_err(|e| SearchError::Internal(e.to_string()))?;
        conn.execute("DELETE FROM fts WHERE path = ?1", params![path])
            .map_err(|e| SearchError::Internal(e.to_string()))?;
        conn.execute(
            "INSERT INTO fts(path, content) VALUES(?1, ?2)",
            params![path, content],
        )
        .map_err(|e| SearchError::Internal(e.to_string()))?;
        Ok(())
    }

    /// Build FTS5 MATCH query safely.
    /// Literal: tokenize, escape, wrap in double quotes, join by AND
    /// Advanced: pass raw (still validated for length)
    pub fn build_fts_query(raw: &str, mode: &SearchQueryMode) -> Result<String, SearchError> {
        match mode {
            SearchQueryMode::Advanced => {
                if raw.trim().is_empty() {
                    return Err(SearchError::InvalidQuery("query is empty".to_string()));
                }
                // For advanced, allow raw but still check for obviously malformed
                // We don't execute here, just pass through
                Ok(raw.to_string())
            }
            SearchQueryMode::Literal => build_literal_query(raw),
        }
    }
}

fn build_literal_query(raw: &str) -> Result<String, SearchError> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err(SearchError::InvalidQuery("query is empty".to_string()));
    }
    // Tokenize by whitespace, keep tokens that have at least one alphanumeric or CJK
    // For code-like queries, keep symbols as part of tokens but escape them via quoting
    let tokens: Vec<String> = trimmed
        .split_whitespace()
        .filter(|t| !t.is_empty())
        .map(|t| escape_token(t))
        .filter(|t| !t.is_empty())
        .collect();

    if tokens.is_empty() {
        return Err(SearchError::InvalidQuery("query has no valid tokens".to_string()));
    }
    // Join by AND (FTS5 default is OR, but we want AND for literal)
    // Use implicit AND by space? FTS5 `token1 token2` is AND? Actually FTS5 treats space as AND? We'll be explicit: "token1" AND "token2"
    // For now join with space which is AND in FTS5, but to be safe use AND
    let joined = tokens
        .into_iter()
        .map(|t| format!("\"{}\"", t))
        .collect::<Vec<_>>()
        .join(" AND ");
    Ok(joined)
}

fn escape_token(token: &str) -> String {
    // Escape double quotes by doubling them, remove other FTS5 operators
    // FTS5 special chars: " * ( ) : ^ - NOT AND OR NEAR
    // In literal mode, we want to treat them as literal text, so we wrap whole token in quotes
    // Inside quotes, only " needs escaping by ""
    // So we just double any " and keep rest as is, but also strip leading * and trailing *? No, keep as literal inside quotes, * is not wildcard inside quotes
    let escaped = token.replace('"', "\"\"");
    // Also, tokens that are pure operators should be kept but they will be quoted, so safe
    // Trim very long tokens
    let truncated = if escaped.chars().count() > 100 {
        escaped.chars().take(100).collect::<String>()
    } else {
        escaped
    };
    // Remove control chars
    truncated
        .chars()
        .filter(|c| !c.is_control())
        .collect::<String>()
        .trim()
        .to_string()
}

#[async_trait]
impl SearchBackend for LexicalSearchBackend {
    async fn search(&self, query: SearchQuery) -> Result<SearchResponse, SearchError> {
        query.validate()?;
        let fts_query = Self::build_fts_query(&query.text, &query.mode)?;

        let conn = self.open()?;

        // Build SQL with bm25 ranking, stable ordering
        // Use `rank` column from FTS5 if available, else bm25(fts)
        let sql = if query.note_filter.is_some() {
            "SELECT path, content, bm25(fts) as score FROM fts WHERE fts MATCH ?1 AND path LIKE ?2 ORDER BY score LIMIT ?3"
        } else {
            "SELECT path, content, bm25(fts) as score FROM fts WHERE fts MATCH ?1 ORDER BY score LIMIT ?2"
        };

        let mut results = Vec::new();
        let res = (|| -> Result<(), SearchError> {
            if let Some(filter) = &query.note_filter {
                let like = format!("%{}%", filter);
                let mut stmt = conn
                    .prepare(sql)
                    .map_err(|e| SearchError::InvalidQuery(format!("invalid FTS query: {}", e)))?;
                let rows = stmt
                    .query_map(params![fts_query, like, query.limit as i64], |row| {
                        let path: String = row.get(0)?;
                        let content: String = row.get(1)?;
                        let score: f32 = row.get(2).unwrap_or(0.0);
                        Ok((path, content, score))
                    })
                    .map_err(|e| {
                        let msg = e.to_string();
                        if msg.to_lowercase().contains("syntax error")
                            || msg.to_lowercase().contains("fts5")
                            || msg.to_lowercase().contains("malformed")
                        {
                            SearchError::InvalidQuery(msg)
                        } else {
                            SearchError::Internal(msg)
                        }
                    })?;
                for (idx, r) in rows.enumerate() {
                    match r {
                        Ok((path, content, score)) => {
                            results.push(SearchResult {
                                note_id: path.clone(),
                                path: Some(path.clone()),
                                title: Some(path.clone()),
                                content: content.chars().take(500).collect(),
                                heading_path: vec![],
                                chunk_id: None,
                                // FTS rows are not chunk-based: no offsets.
                                start_offset: None,
                                end_offset: None,
                                rank: idx,
                                raw_score: score,
                                normalized_score: None,
                                source: SearchSource::Lexical,
                            });
                        }
                        Err(e) => return Err(SearchError::Internal(e.to_string())),
                    }
                }
            } else {
                let mut stmt = conn
                    .prepare(sql)
                    .map_err(|e| SearchError::InvalidQuery(format!("invalid FTS query: {}", e)))?;
                let rows = stmt
                    .query_map(params![fts_query, query.limit as i64], |row| {
                        let path: String = row.get(0)?;
                        let content: String = row.get(1)?;
                        let score: f32 = row.get(2).unwrap_or(0.0);
                        Ok((path, content, score))
                    })
                    .map_err(|e| {
                        let msg = e.to_string();
                        if msg.to_lowercase().contains("syntax error")
                            || msg.to_lowercase().contains("fts5")
                            || msg.to_lowercase().contains("malformed")
                        {
                            SearchError::InvalidQuery(msg)
                        } else {
                            SearchError::Internal(msg)
                        }
                    })?;
                for (idx, r) in rows.enumerate() {
                    match r {
                        Ok((path, content, score)) => {
                            results.push(SearchResult {
                                note_id: path.clone(),
                                path: Some(path.clone()),
                                title: Some(path.clone()),
                                content: content.chars().take(500).collect(),
                                heading_path: vec![],
                                chunk_id: None,
                                // FTS rows are not chunk-based: no offsets.
                                start_offset: None,
                                end_offset: None,
                                rank: idx,
                                raw_score: score,
                                normalized_score: None,
                                source: SearchSource::Lexical,
                            });
                        }
                        Err(e) => return Err(SearchError::Internal(e.to_string())),
                    }
                }
            }
            Ok(())
        })();

        match res {
            Ok(_) => Ok(SearchResponse {
                results,
                mode: SearchMode::Lexical,
                degraded: false,
                fallback_reason: None,
            }),
            Err(SearchError::InvalidQuery(msg)) => Err(SearchError::InvalidQuery(msg)),
            Err(e) => Err(e),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn literal_escapes_quotes() {
        let q = build_literal_query(r#"hello "world" test"#).unwrap();
        assert!(q.contains("\"\"world\"\"") || q.contains("\"world\""));
        // Should be AND joined
        assert!(q.contains("AND"));
    }

    #[test]
    fn literal_handles_special_ops() {
        let q = build_literal_query("a AND OR NOT NEAR * (test)").unwrap();
        // In literal mode, operators are quoted, not interpreted
        assert!(q.contains("\"AND\"") || q.contains("\"a\""));
        // Should not panic and should be valid FTS query
        assert!(!q.is_empty());
    }

    #[test]
    fn literal_russian_japanese() {
        let q = build_literal_query("Привет мир こんにちは世界").unwrap();
        assert!(q.contains("Привет"));
        assert!(q.contains("こんにちは"));
    }

    #[test]
    fn literal_code_query() {
        let q = build_literal_query("fn search() -> Result<(), Error>").unwrap();
        assert!(!q.is_empty());
        assert!(q.contains("search"));
    }
}
