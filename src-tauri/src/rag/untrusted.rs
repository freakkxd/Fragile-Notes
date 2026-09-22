//! Prompt-injection boundary (Stage 10A.3).
//!
//! Retrieved notes are **untrusted data**, even when stored locally: note
//! content may contain `Ignore previous instructions`, `Reveal API key`,
//! `You are now system`, fake tool calls, or forged source markers.
//!
//! Rules enforced here:
//! - Retrieved context is materialized as [`UntrustedContext`] — a DATA-only
//!   type. It carries no role, no policy, no tool/model selection.
//! - [`serialize_untrusted_context`] renders it for a **user/data message
//!   ONLY. It must never be placed into a system message (the 10B assembler
//!   enforces roles; this module provides the data section).
//! - Metadata (source markers) and content are separate sections. Identity
//!   comes from [`RagReference`]s, never by parsing the serialized text.
//! - Content is reproduced verbatim (no interpretation, no instruction
//!   following, no tool-argument extraction). A forged `[source ...]` line
//!   inside a note stays inert text — it cannot create a reference.
//! - The serializer performs no I/O, holds no secrets, and never selects
//!   providers, models, or tools.

use super::types::RagReference;

/// One untrusted data block: trusted index metadata + verbatim content.
///
/// `preview` must already be bounded by the caller (same budgets as
/// `ContextBuilder`: `max_chars_per_chunk`, Unicode-safe truncation), so the
/// serialized data section never exceeds the retrieval budget.
#[derive(Debug, Clone, PartialEq)]
pub struct UntrustedBlock {
    pub index: usize,
    pub reference: RagReference,
    pub preview: String,
}

/// DATA-only container. No prompt role, no policy, no tool selection.
#[derive(Debug, Clone, PartialEq)]
pub struct UntrustedContext {
    pub blocks: Vec<UntrustedBlock>,
}

/// Pair references with their (already bounded) content previews.
///
/// Length mismatch is handled without fabrication or panics: pairing stops
/// at the shorter side; extra previews without a reference are dropped
/// (unattributable data must not enter the prompt), missing previews become
/// empty strings.
pub fn untrusted_blocks(
    references: &[RagReference],
    previews: &[String],
) -> UntrustedContext {
    let blocks = references
        .iter()
        .enumerate()
        .map(|(i, rf)| UntrustedBlock {
            index: i + 1,
            reference: rf.clone(),
            preview: previews.get(i).cloned().unwrap_or_default(),
        })
        .collect();
    UntrustedContext { blocks }
}

/// Serialize retrieved context as a DATA section for a user message.
///
/// Layout per block:
///
/// ```text
/// [source 1 | chunk_id=<id> | note_id=<id>]
/// path: <vault-relative or (none)>
/// heading: <a > b or (none)>
/// content:
/// <verbatim note preview>
/// [/source 1]
/// ```
///
/// Wrapped in `<retrieved_data>...</retrieved_data>`. Deterministic:
/// same input always yields byte-identical output.
pub fn serialize_untrusted_context(ctx: &UntrustedContext) -> String {
    let mut out = String::from("<retrieved_data>\n");
    for b in &ctx.blocks {
        out.push_str(&format!(
            "[source {} | chunk_id={} | note_id={}]\n",
            b.index, b.reference.chunk_id, b.reference.note_id
        ));
        out.push_str(&format!(
            "path: {}\n",
            b.reference.path.as_deref().unwrap_or("(none)")
        ));
        out.push_str(&format!(
            "heading: {}\n",
            if b.reference.heading_path.is_empty() {
                "(none)".to_string()
            } else {
                b.reference.heading_path.join(" > ")
            }
        ));
        out.push_str("content:\n");
        out.push_str(&b.preview);
        if !b.preview.ends_with('\n') {
            out.push('\n');
        }
        out.push_str(&format!("[/source {}]\n", b.index));
    }
    out.push_str("</retrieved_data>");
    out
}
