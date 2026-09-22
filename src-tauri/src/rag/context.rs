use super::types::{ContextLimits, RagContext, RagReference};
use crate::search::types::SearchResult;

/// Build bounded RAG context deterministically.
///
/// Contract:
/// - Input `results` are already ordered by SearchService
///   (`fused_score DESC`, `chunk_id ASC`). Order is preserved, never re-sorted.
/// - Limits are validated BEFORE any allocation by [`ContextLimits::validate`].
/// - `max_chars` / `max_chars_per_chunk` are counted in **Unicode scalar values**
///   (`char` count), NOT bytes. Safe for multi-byte UTF-8, emoji, CJK.
/// - `start_offset` / `end_offset` in references are **UTF-8 byte offsets**
///   into the ORIGINAL full chunk content (not the truncated preview).
/// - Context builder only formats data. No prompt assembly, no LLM calls.
/// - Metadata (headers) never interpolates user content: headers are built
///   solely from `note_id` / sanitized `path` / `heading_path` / `chunk_id`.
///   Content is appended as a separate section. Consumers MUST use
///   `references` for machine-readable metadata, never parse `text`.
/// - `path` values come from the local index (Registry/notes DB), never from
///   frontend input. Absolute paths are stripped to vault-relative form;
///   values that cannot be sanitized become `None` (no leakage of `$HOME`,
///   `/etc`, `C:\`, `..` escapes).
pub struct ContextBuilder;

impl ContextBuilder {
    pub fn build(results: &[SearchResult], limits: &ContextLimits) -> RagContext {
        let mut seen_chunk_ids = std::collections::HashSet::new();
        let mut references = Vec::new();
        let mut context_parts = Vec::new();
        let mut total_chars = 0usize;
        let mut truncated = false;
        let mut added = 0usize;
        // Sequential machine-readable block number (no gaps after dedup skip).
        let mut block_no = 0usize;

        for res in results.iter() {
            if added >= limits.max_chunks {
                truncated = true;
                break;
            }
            // Deduplicate by chunk_id. Results without chunk_id (legacy
            // lexical rows) cannot be deduped by id and are always kept —
            // they may be distinct sections of the same note.
            if let Some(cid) = &res.chunk_id {
                if !seen_chunk_ids.insert(cid.clone()) {
                    continue; // duplicate chunk_id -> one context block
                }
            }
            // NOTE: different chunks of the same note are intentionally
            // preserved even if `note_id` matches — they may cover different
            // relevant sections. Only identical `chunk_id` is suppressed.

            // Truncate chunk content safely (char boundary, never byte split).
            let content = &res.content;
            let truncated_content = safe_truncate(content, limits.max_chars_per_chunk);
            let was_truncated =
                truncated_content.chars().count() < content.chars().count();

            block_no += 1;
            let display_path = sanitize_path(res.path.as_deref().unwrap_or(&res.note_id));
            let header = format!(
                "[Source {} | chunk_id={}]\nPath: {}\nHeading: {}\n{}",
                block_no,
                res.chunk_id.as_deref().unwrap_or("(no chunk)"),
                display_path.as_deref().unwrap_or("(none)"),
                if res.heading_path.is_empty() {
                    "(none)".to_string()
                } else {
                    res.heading_path.join(" > ")
                },
                if was_truncated { "[truncated]\n" } else { "" }
            );

            let block = format!("{}\n---\n{}\n\n", header, truncated_content);
            let block_chars = block.chars().count();

            if total_chars + block_chars > limits.max_chars {
                if added == 0 && total_chars == 0 {
                    // First block alone exceeds budget: hard-truncate the
                    // formatted block itself so SOMETHING deterministic fits.
                    let truncated_block = safe_truncate(&block, limits.max_chars);
                    context_parts.push(truncated_block);
                    total_chars = limits.max_chars;
                    truncated = true;
                } else {
                    // Budget reached: do not add a partial next chunk.
                    truncated = true;
                    block_no -= 1; // this block was not emitted
                    break;
                }
            } else {
                context_parts.push(block);
                total_chars += block_chars;
            }

            // Reference preserves FULL original byte offsets, even when the
            // preview text was truncated. `SearchResult` carries no stored
            // offsets, so lexical rows report 0..content.len() (UTF-8 bytes).
            let reference = RagReference {
                chunk_id: res.chunk_id.clone().unwrap_or_else(|| {
                    format!("{}-block-{}", res.note_id, block_no)
                }),
                note_id: res.note_id.clone(),
                path: display_path,
                heading_path: res.heading_path.clone(),
                start_offset: 0,
                end_offset: res.content.len(),
                score: Some(res.raw_score),
                source: res.source.clone(),
            };
            references.push(reference);
            added += 1;

            if was_truncated {
                truncated = true;
            }
        }

        let text = context_parts.join("");
        RagContext {
            text,
            references,
            truncated,
        }
    }
}

/// Truncate to at most `max_chars` Unicode scalar values.
/// Always lands on a `char` boundary — never splits UTF-8 mid-sequence,
/// never breaks surrogate pairs / emoji (Rust `String` is valid UTF-8).
pub(crate) fn safe_truncate(s: &str, max_chars: usize) -> String {
    if s.chars().count() <= max_chars {
        return s.to_string();
    }
    s.chars().take(max_chars).collect()
}

/// Sanitize an index-provided path to vault-relative form.
///
/// - Rejects absolute paths (`/...`, `C:\...`, `\\...`) by keeping only the
///   file name — never leaks `$HOME` / system directories via public API.
/// - Rejects `..` escapes and NUL bytes -> `None`.
/// - Trims whitespace; empty -> `None`.
/// - Backslashes are normalized to `/` for display consistency.
pub(crate) fn sanitize_path(raw: &str) -> Option<String> {
    let t = raw.trim();
    if t.is_empty() || t.contains('\0') {
        return None;
    }
    // Split on both separators, drop empties (handles leading `/`, `\\`).
    let mut parts: Vec<&str> = t
        .split(['/', '\\'])
        .filter(|p| !p.is_empty())
        .collect();
    if parts.is_empty() {
        return None;
    }
    // Any `..` anywhere -> refuse (path escape attempt).
    if parts.iter().any(|p| *p == ".." || p.contains('\0')) {
        return None;
    }
    let is_absolute =
        t.starts_with('/') || t.starts_with('\\') || (parts.first().is_some_and(|p| p.ends_with(':')));
    if is_absolute {
        // Keep only the file name — vault-relative, no system prefix leak.
        parts = vec![*parts.last().unwrap()];
    }
    let joined = parts.join("/");
    if joined.is_empty() || joined.chars().count() > 1024 {
        return None;
    }
    Some(joined)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::search::types::{SearchResult, SearchSource};

    fn mk_result(chunk_id: &str, note_id: &str, content: &str) -> SearchResult {
        SearchResult {
            note_id: note_id.to_string(),
            path: Some(note_id.to_string()),
            title: None,
            content: content.to_string(),
            heading_path: vec!["H".to_string()],
            chunk_id: Some(chunk_id.to_string()),
            rank: 0,
            raw_score: 0.0,
            normalized_score: None,
            source: SearchSource::Lexical,
        }
    }

    #[test]
    fn unicode_safe() {
        let s = "Привет 🌟 test こんにちは";
        let t = safe_truncate(s, 5);
        assert_eq!(t.chars().count(), 5);
        assert!(t.is_char_boundary(t.len()));
    }

    #[test]
    fn dedup() {
        let limits = ContextLimits {
            max_chunks: 10,
            max_chars: 10000,
            max_chars_per_chunk: 1000,
        };
        let r1 = mk_result("c1", "n1.md", "content1");
        let r2 = mk_result("c1", "n1.md", "content1 duplicate");
        let r3 = mk_result("c2", "n1.md", "different chunk same note");
        let ctx = ContextBuilder::build(&[r1, r2, r3], &limits);
        assert_eq!(ctx.references.len(), 2);
        assert_eq!(ctx.references[0].chunk_id, "c1");
        assert_eq!(ctx.references[1].chunk_id, "c2");
        // Sequential numbering without gaps.
        assert!(ctx.text.contains("[Source 1 |"));
        assert!(ctx.text.contains("[Source 2 |"));
        assert!(!ctx.text.contains("[Source 3 |"));
    }

    #[test]
    fn absolute_path_sanitized() {
        assert_eq!(sanitize_path("/home/user/secret/notes/a.md").as_deref(), Some("a.md"));
        assert_eq!(sanitize_path("notes/a.md").as_deref(), Some("notes/a.md"));
        assert_eq!(sanitize_path("../../etc/passwd"), None);
        assert_eq!(sanitize_path(""), None);
    }
}
