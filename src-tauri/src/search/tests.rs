use super::lexical::LexicalSearchBackend;
use super::service::{FallbackPolicy, SearchService, UnavailableSemanticBackend};
use super::types::*;
use super::SearchBackend;
use std::sync::Arc;

fn make_lexical() -> LexicalSearchBackend {
    LexicalSearchBackend::new_in_memory().unwrap()
}

async fn index_many(backend: &LexicalSearchBackend, pairs: Vec<(&str, &str)>) {
    for (path, content) in pairs {
        backend.index_note(path, content).unwrap();
    }
}

#[tokio::test]
async fn literal_query() {
    let b = make_lexical();
    index_many(&b, vec![("a.md", "hello world"), ("b.md", "goodbye world")]).await;
    let q = SearchQuery { text: "hello".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res = b.search(q).await.unwrap();
    assert_eq!(res.results.len(), 1);
    assert_eq!(res.results[0].note_id, "a.md");
    assert_eq!(res.results[0].source, SearchSource::Lexical);
}

#[tokio::test]
async fn quotes_escaped() {
    let b = make_lexical();
    index_many(&b, vec![("a.md", r#"he said "hello" world"#)]).await;
    // literal query with quotes should not cause syntax error
    let q = SearchQuery { text: r#" "hello" "#.to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res = b.search(q).await.unwrap();
    assert!(!res.results.is_empty());
    // Ensure it found the quoted phrase via literal handling
    assert_eq!(res.results[0].note_id, "a.md");
}

#[tokio::test]
async fn and_or_not_treated_safely_in_literal() {
    let b = make_lexical();
    index_many(&b, vec![("a.md", "apple banana"), ("b.md", "apple"), ("c.md", "banana")]).await;
    // In literal mode, "apple AND banana" should be treated as tokens "apple", "AND", "banana" joined by AND -> requires all three tokens, so no results or only docs containing AND literally
    // But we want to ensure it doesn't interpret as FTS5 boolean and leak.
    // Our literal builder wraps each token in quotes and joins with AND, so "apple AND banana" becomes "\"apple\" AND \"AND\" AND \"banana\"" -> requires AND token, so no results
    // That's safe (no injection). The key is it doesn't return both a.md and b.md via OR.
    let q = SearchQuery { text: "apple AND banana".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res = b.search(q).await.unwrap();
    // Since we indexed no doc with literal "AND", should be 0 results, not 2 or 3
    // This proves AND was escaped, not interpreted
    assert_eq!(res.results.len(), 0);

    // Similarly OR should not OR
    let q2 = SearchQuery { text: "apple OR banana".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res2 = b.search(q2).await.unwrap();
    assert_eq!(res2.results.len(), 0);

    // NOT
    let q3 = SearchQuery { text: "NOT apple".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res3 = b.search(q3).await.unwrap();
    assert_eq!(res3.results.len(), 0);
}

#[tokio::test]
async fn wildcard_treated_safely() {
    let b = make_lexical();
    index_many(&b, vec![("a.md", "hello"), ("b.md", "helloworld")]).await;
    let q = SearchQuery { text: "hello*".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res = b.search(q).await.unwrap();
    // Literal "hello*" should be quoted, not wildcard, so only exact "hello*" token, which doesn't exist -> 0
    // If it were wildcard, it would match both
    assert_eq!(res.results.len(), 0);
}

#[tokio::test]
async fn russian_search() {
    let b = make_lexical();
    index_many(&b, vec![("ru.md", "Привет мир, это русский текст"), ("other.md", "hello")]).await;
    let q = SearchQuery { text: "Привет".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res = b.search(q).await.unwrap();
    assert_eq!(res.results.len(), 1);
    assert_eq!(res.results[0].note_id, "ru.md");
}

#[tokio::test]
async fn japanese_search() {
    let b = make_lexical();
    index_many(&b, vec![("ja.md", "こんにちは世界"), ("other.md", "hello")]).await;
    let q = SearchQuery { text: "こんにちは".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res = b.search(q).await.unwrap();
    // FTS5 trigram should handle, but even without trigram, tokenized query may match via LIKE? Our literal uses FTS MATCH, so Japanese may need trigram.
    // If our backend created without trigram, Japanese search may fail. We accept either 1 or 0 but not error.
    // For now, ensure no panic and source lexical
    assert!(res.results.len() <= 1);
    if res.results.len() == 1 {
        assert_eq!(res.results[0].source, SearchSource::Lexical);
    }
}

#[tokio::test]
async fn code_like_query() {
    let b = make_lexical();
    index_many(&b, vec![("code.md", "fn search() -> Result<(), Error> { Ok(()) }")]).await;
    let q = SearchQuery { text: "fn search() -> Result".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res = b.search(q).await.unwrap();
    // Should not error on symbols like () -> ,
    assert!(res.results.len() <= 1);
}

#[tokio::test]
async fn empty_query_error() {
    let b = make_lexical();
    let q = SearchQuery { text: "   ".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let err = b.search(q).await.unwrap_err();
    assert!(matches!(err, SearchError::InvalidQuery(_)));
}

#[tokio::test]
async fn no_results_empty_response() {
    let b = make_lexical();
    index_many(&b, vec![("a.md", "hello")]).await;
    let q = SearchQuery { text: "nonexistenttoken123".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res = b.search(q).await.unwrap();
    assert!(res.results.is_empty());
    assert_eq!(res.mode, SearchMode::Lexical);
    assert!(!res.degraded);
    // Not an error
}

#[tokio::test]
async fn malformed_query_advanced() {
    let b = make_lexical();
    index_many(&b, vec![("a.md", "hello")]).await;
    // Advanced mode with unbalanced quote should cause InvalidQuery from SQLite
    let q = SearchQuery { text: "\"unclosed".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Advanced };
    let err = b.search(q).await.unwrap_err();
    // Could be InvalidQuery or Internal, but should not panic
    assert!(matches!(err, SearchError::InvalidQuery(_) | SearchError::Internal(_)));
}

#[tokio::test]
async fn limit_respected() {
    let b = make_lexical();
    let mut pairs = vec![];
    for i in 0..10 {
        pairs.push((Box::leak(format!("{}.md", i).into_boxed_str()) as &str, "common token hello"));
    }
    index_many(&b, pairs).await;
    let q = SearchQuery { text: "hello".to_string(), limit: 3, note_filter: None, mode: SearchQueryMode::Literal };
    let res = b.search(q).await.unwrap();
    assert_eq!(res.results.len(), 3);
}

#[tokio::test]
async fn note_filter() {
    let b = make_lexical();
    index_many(&b, vec![("notes/a.md", "hello world"), ("archive/b.md", "hello world"), ("notes/c.md", "hello")]).await;
    let q = SearchQuery { text: "hello".to_string(), limit: 10, note_filter: Some("notes".to_string()), mode: SearchQueryMode::Literal };
    let res = b.search(q).await.unwrap();
    assert!(res.results.iter().all(|r| r.note_id.contains("notes")));
    assert!(res.results.len() >= 1);
}

#[tokio::test]
async fn stable_ordering_for_equal_ranks() {
    let b = make_lexical();
    index_many(&b, vec![("a.md", "hello"), ("b.md", "hello"), ("c.md", "hello")]).await;
    let q = SearchQuery { text: "hello".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res1 = b.search(q.clone()).await.unwrap();
    let res2 = b.search(q).await.unwrap();
    // Ordering should be deterministic (ORDER BY bm25, rank)
    let ids1: Vec<_> = res1.results.iter().map(|r| &r.note_id).collect();
    let ids2: Vec<_> = res2.results.iter().map(|r| &r.note_id).collect();
    assert_eq!(ids1, ids2);
    // Check rank is sequential
    for (i, r) in res1.results.iter().enumerate() {
        assert_eq!(r.rank, i);
    }
}

#[tokio::test]
async fn search_result_source_lexical() {
    let b = make_lexical();
    index_many(&b, vec![("a.md", "lexical source test")]).await;
    let q = SearchQuery { text: "lexical".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res = b.search(q).await.unwrap();
    assert_eq!(res.results[0].source, SearchSource::Lexical);
    assert_eq!(res.mode, SearchMode::Lexical);
    assert!(!res.degraded);
}

#[tokio::test]
async fn semantic_unavailable_deny_error() {
    let lexical = Arc::new(make_lexical());
    let semantic = Arc::new(UnavailableSemanticBackend::new(
        FallbackReason::UnsupportedSemanticSearch,
        "semantic not ready".to_string(),
    ));
    let service = SearchService::new(lexical, Some(semantic), FallbackPolicy::Deny);
    let q = SearchQuery { text: "hello".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let err = service.search(q, SearchMode::Semantic).await.unwrap_err();
    assert!(matches!(err, SearchError::SemanticUnavailable { .. }));
}

#[tokio::test]
async fn semantic_unavailable_lexical_degraded() {
    let lexical = Arc::new(make_lexical());
    lexical.index_note("a.md", "hello world").unwrap();
    let semantic = Arc::new(UnavailableSemanticBackend::new(
        FallbackReason::EmbeddingProviderUnavailable,
        "no provider".to_string(),
    ));
    let service = SearchService::new(lexical, Some(semantic), FallbackPolicy::Lexical);
    let q = SearchQuery { text: "hello".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res = service.search(q, SearchMode::Semantic).await.unwrap();
    assert!(res.degraded);
    assert_eq!(res.mode, SearchMode::Semantic);
    assert!(res.fallback_reason.is_some());
    assert_eq!(res.results[0].source, SearchSource::Lexical);
}

#[tokio::test]
async fn fallback_reason_returned() {
    let lexical = Arc::new(make_lexical());
    lexical.index_note("a.md", "hello").unwrap();
    let service = SearchService::with_lexical(lexical.clone());
    // Auto with no semantic should be degraded with reason
    let q = SearchQuery { text: "hello".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res = service.search(q, SearchMode::Auto).await.unwrap();
    assert!(res.degraded);
    assert!(res.fallback_reason.is_some());
    assert_eq!(res.fallback_reason, Some(FallbackReason::UnsupportedSemanticSearch));
}

#[tokio::test]
async fn no_fts_table() {
    // Create backend with non-existent db and no table
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("no_table.db");
    // Create empty file without FTS
    std::fs::write(&path, "").unwrap();
    let backend = LexicalSearchBackend::new_with_path(path.clone());
    // Force remove fts table if exists
    if path.exists() {
        if let Ok(conn) = rusqlite::Connection::open(&path) {
            let _ = conn.execute("DROP TABLE IF EXISTS fts", []);
        }
    }
    let q = SearchQuery { text: "hello".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    // Re-create backend that points to db without fts
    let backend2 = super::lexical::LexicalSearchBackend::new(path);
    let err = backend2.search(q).await.unwrap_err();
    assert!(matches!(err, SearchError::IndexUnavailable(_)));
    let _ = backend;
}

#[tokio::test]
async fn service_auto_fallback() {
    let lexical = Arc::new(make_lexical());
    lexical.index_note("a.md", "auto fallback test").unwrap();
    let service = SearchService::with_lexical(lexical);
    let q = SearchQuery { text: "auto".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res = service.search(q, SearchMode::Auto).await.unwrap();
    assert_eq!(res.mode, SearchMode::Auto);
    assert!(res.degraded);
}

#[tokio::test]
async fn lexical_mode_never_degraded() {
    let lexical = Arc::new(make_lexical());
    lexical.index_note("a.md", "lexical pure").unwrap();
    let service = SearchService::with_lexical(lexical);
    let q = SearchQuery { text: "lexical".to_string(), limit: 10, note_filter: None, mode: SearchQueryMode::Literal };
    let res = service.search(q, SearchMode::Lexical).await.unwrap();
    assert!(!res.degraded);
    assert!(res.fallback_reason.is_none());
    assert_eq!(res.mode, SearchMode::Lexical);
}
