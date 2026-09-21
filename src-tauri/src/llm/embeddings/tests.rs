use super::types::*;
use super::validation::*;

#[test]
fn valid_request() {
    let limits = EmbeddingLimits::default();
    let req = EmbeddingRequest::new("model-1".to_string(), vec!["hello".to_string(), "world".to_string()]);
    assert!(req.validate(&limits).is_ok());
}

#[test]
fn empty_model_id_rejected() {
    let limits = EmbeddingLimits::default();
    let req = EmbeddingRequest::new("".to_string(), vec!["hi".to_string()]);
    assert_eq!(req.validate(&limits).unwrap_err(), EmbeddingError::EmptyModelId);
    let req2 = EmbeddingRequest::new("   ".to_string(), vec!["hi".to_string()]);
    assert_eq!(req2.validate(&limits).unwrap_err(), EmbeddingError::EmptyModelId);
}

#[test]
fn empty_inputs_rejected() {
    let limits = EmbeddingLimits::default();
    let req = EmbeddingRequest::new("m".to_string(), vec![]);
    assert_eq!(req.validate(&limits).unwrap_err(), EmbeddingError::EmptyInput);
}

#[test]
fn empty_input_rejected() {
    let limits = EmbeddingLimits::default();
    let req = EmbeddingRequest::new("m".to_string(), vec!["hello".to_string(), "".to_string()]);
    assert_eq!(req.validate(&limits).unwrap_err(), EmbeddingError::EmptyInput);
    let req2 = EmbeddingRequest::new("m".to_string(), vec!["   ".to_string()]);
    assert_eq!(req2.validate(&limits).unwrap_err(), EmbeddingError::EmptyInput);
}

#[test]
fn batch_limit_rejected() {
    let limits = EmbeddingLimits { max_inputs: 2, ..Default::default() };
    let req = EmbeddingRequest::new("m".to_string(), vec!["a".to_string(), "b".to_string(), "c".to_string()]);
    assert_eq!(req.validate(&limits).unwrap_err(), EmbeddingError::BatchTooLarge { max: 2, actual: 3 });
}

#[test]
fn input_size_limit_rejected() {
    let limits = EmbeddingLimits { max_input_chars: 5, ..Default::default() };
    let req = EmbeddingRequest::new("m".to_string(), vec!["hello world".to_string()]);
    match req.validate(&limits).unwrap_err() {
        EmbeddingError::InputTooLarge { index, max, actual } => {
            assert_eq!(index, 0);
            assert_eq!(max, 5);
            assert!(actual > 5);
        }
        e => panic!("unexpected {:?}", e),
    }
}

#[test]
fn valid_response() {
    let limits = EmbeddingLimits::default();
    let req = EmbeddingRequest::new("m1".to_string(), vec!["a".to_string(), "b".to_string()]);
    let resp = EmbeddingResponse {
        model_id: "m1".to_string(),
        dimensions: 3,
        vectors: vec![vec![0.1, 0.2, 0.3], vec![0.4, 0.5, 0.6]],
    };
    assert!(resp.validate_for(&req, &limits).is_ok());
}

#[test]
fn vectors_count_mismatch() {
    let limits = EmbeddingLimits::default();
    let req = EmbeddingRequest::new("m1".to_string(), vec!["a".to_string(), "b".to_string()]);
    let resp = EmbeddingResponse {
        model_id: "m1".to_string(),
        dimensions: 2,
        vectors: vec![vec![0.1, 0.2]],
    };
    assert_eq!(resp.validate_for(&req, &limits).unwrap_err(), EmbeddingError::CountMismatch { expected: 2, actual: 1 });
}

#[test]
fn dimensions_zero_rejected() {
    let limits = EmbeddingLimits::default();
    let req = EmbeddingRequest::new("m1".to_string(), vec!["a".to_string()]);
    let resp = EmbeddingResponse {
        model_id: "m1".to_string(),
        dimensions: 0,
        vectors: vec![vec![]],
    };
    assert_eq!(resp.validate_for(&req, &limits).unwrap_err(), EmbeddingError::InvalidDimensions { dimensions: 0 });
}

#[test]
fn vector_dimensions_mismatch() {
    let limits = EmbeddingLimits::default();
    let req = EmbeddingRequest::new("m1".to_string(), vec!["a".to_string()]);
    let resp = EmbeddingResponse {
        model_id: "m1".to_string(),
        dimensions: 3,
        vectors: vec![vec![0.1, 0.2]],
    };
    assert_eq!(resp.validate_for(&req, &limits).unwrap_err(), EmbeddingError::DimensionMismatch { expected: 3, actual: 2, index: 0 });
}

#[test]
fn nan_rejected() {
    let limits = EmbeddingLimits::default();
    let req = EmbeddingRequest::new("m1".to_string(), vec!["a".to_string()]);
    let resp = EmbeddingResponse {
        model_id: "m1".to_string(),
        dimensions: 2,
        vectors: vec![vec![f32::NAN, 0.1]],
    };
    assert_eq!(resp.validate_for(&req, &limits).unwrap_err(), EmbeddingError::NonFiniteValue { index: 0, pos: 0 });
}

#[test]
fn infinity_rejected() {
    let limits = EmbeddingLimits::default();
    let req = EmbeddingRequest::new("m1".to_string(), vec!["a".to_string()]);
    let resp = EmbeddingResponse {
        model_id: "m1".to_string(),
        dimensions: 2,
        vectors: vec![vec![f32::INFINITY, 0.1]],
    };
    assert_eq!(resp.validate_for(&req, &limits).unwrap_err(), EmbeddingError::NonFiniteValue { index: 0, pos: 0 });
}

#[test]
fn model_id_mismatch() {
    let limits = EmbeddingLimits::default();
    let req = EmbeddingRequest::new("m1".to_string(), vec!["a".to_string()]);
    let resp = EmbeddingResponse {
        model_id: "m2".to_string(),
        dimensions: 2,
        vectors: vec![vec![0.1, 0.2]],
    };
    assert_eq!(resp.validate_for(&req, &limits).unwrap_err(), EmbeddingError::ModelMismatch { expected: "m1".to_string(), actual: "m2".to_string() });
}

#[test]
fn max_dimensions_rejected() {
    let limits = EmbeddingLimits { max_dimensions: 2, ..Default::default() };
    let req = EmbeddingRequest::new("m1".to_string(), vec!["a".to_string()]);
    let resp = EmbeddingResponse {
        model_id: "m1".to_string(),
        dimensions: 3,
        vectors: vec![vec![0.1, 0.2, 0.3]],
    };
    assert_eq!(resp.validate_for(&req, &limits).unwrap_err(), EmbeddingError::MaxDimensionsExceeded { max: 2, actual: 3 });
}

#[test]
fn vector_byte_limit_rejected() {
    let limits = EmbeddingLimits { max_vector_bytes: 8, ..Default::default() }; // 2 vectors * 1 dim * 4 = 8, but we will exceed
    let req = EmbeddingRequest::new("m1".to_string(), vec!["a".to_string(), "b".to_string()]);
    let resp = EmbeddingResponse {
        model_id: "m1".to_string(),
        dimensions: 3,
        vectors: vec![vec![0.1, 0.2, 0.3], vec![0.4, 0.5, 0.6]],
    };
    // 2*3*4=24 >8
    assert_eq!(resp.validate_for(&req, &limits).unwrap_err(), EmbeddingError::VectorTooLarge { max_bytes: 8, actual_bytes: 24 });
}

#[test]
fn valid_note_chunk() {
    let content = "hello world".to_string();
    let hash = sha256_hex(&content);
    let chunk = NoteChunk {
        id: "chunk-1".to_string(),
        note_id: "note-1".to_string(),
        content: content.clone(),
        content_hash: hash,
        heading_path: vec!["Heading".to_string()],
        start_offset: 0,
        end_offset: 11,
    };
    assert!(chunk.validate().is_ok());
}

#[test]
fn empty_chunk_rejected() {
    let chunk = NoteChunk {
        id: "".to_string(),
        note_id: "note-1".to_string(),
        content: "hi".to_string(),
        content_hash: sha256_hex("hi"),
        heading_path: vec![],
        start_offset: 0,
        end_offset: 2,
    };
    assert!(chunk.validate().is_err());
    let chunk2 = NoteChunk {
        id: "c1".to_string(),
        note_id: "".to_string(),
        content: "hi".to_string(),
        content_hash: sha256_hex("hi"),
        heading_path: vec![],
        start_offset: 0,
        end_offset: 2,
    };
    assert!(chunk2.validate().is_err());
    let chunk3 = NoteChunk {
        id: "c1".to_string(),
        note_id: "note-1".to_string(),
        content: "".to_string(),
        content_hash: sha256_hex(""),
        heading_path: vec![],
        start_offset: 0,
        end_offset: 0,
    };
    assert!(chunk3.validate().is_err());
}

#[test]
fn invalid_offsets_rejected() {
    let content = "hello".to_string();
    let chunk = NoteChunk {
        id: "c1".to_string(),
        note_id: "n1".to_string(),
        content: content.clone(),
        content_hash: sha256_hex(&content),
        heading_path: vec![],
        start_offset: 10,
        end_offset: 5,
    };
    assert!(chunk.validate().is_err());
}

#[test]
fn content_hash_mismatch_rejected() {
    let content = "hello".to_string();
    let chunk = NoteChunk {
        id: "c1".to_string(),
        note_id: "n1".to_string(),
        content: content.clone(),
        content_hash: "wronghash".to_string(),
        heading_path: vec![],
        start_offset: 0,
        end_offset: 5,
    };
    assert!(chunk.validate().is_err());
}
