//! Acknowledge logbook notes only after their decode has completed successfully.
//! Submission can be refused and a call can span several simulated nights.

use serde_json::{json, Value};
use std::collections::BTreeSet;

#[derive(Default)]
pub struct IntelQueue {
    seen: BTreeSet<(String, String)>,
    texts: Vec<Value>,
    acknowledged: usize,
    pending_end: Option<usize>,
}

impl IntelQueue {
    pub fn observe(&mut self, request: &Value) {
        let (Some(id), Some(reason)) = (
            request.get("request_id").and_then(Value::as_str),
            request.get("reason").and_then(Value::as_str),
        ) else { return };
        // A corrected note under an existing ID is new evidence, not a duplicate.
        if reason.trim().is_empty() || !self.seen.insert((id.to_string(), reason.to_string())) {
            return;
        }
        self.texts.push(json!({
            "request_id": id,
            "issued_at_utc": request.get("issued_at_utc").cloned().unwrap_or(Value::Null),
            "reason": reason,
        }));
    }

    /// Refusing a submission leaves the same batch available next time.
    pub fn next_batch(&self, limit: usize) -> Option<(usize, Value)> {
        if self.pending_end.is_some() || self.acknowledged == self.texts.len() || limit == 0 {
            return None;
        }
        let end = self.acknowledged.saturating_add(limit).min(self.texts.len());
        Some((end, Value::Array(self.texts[self.acknowledged..end].to_vec())))
    }

    pub fn submitted(&mut self, end: usize) {
        debug_assert!(self.pending_end.is_none() && end > self.acknowledged && end <= self.texts.len());
        self.pending_end = Some(end);
    }

    pub fn complete(&mut self, success: bool) {
        if let Some(end) = self.pending_end.take() {
            if success {
                self.acknowledged = end;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn note(id: usize, reason: &str) -> Value {
        json!({"request_id": format!("note-{id}"), "reason": reason})
    }

    #[test]
    fn refused_submission_and_slow_response_do_not_drop_notes() {
        let mut q = IntelQueue::default();
        for id in 0..9 { q.observe(&note(id, "note")); }
        let (end, first) = q.next_batch(3).unwrap();
        // Client at its concurrency limit: no submitted(), no cursor change.
        assert_eq!(q.next_batch(3).unwrap().1, first);
        q.submitted(end);
        // More notes and several night boundaries while the same call is in flight.
        q.observe(&note(9, "later"));
        for _ in 0..4 { assert!(q.next_batch(3).is_none()); }
        q.complete(true);
        assert_eq!(q.next_batch(3).unwrap().1[0]["request_id"], "note-3");
    }

    #[test]
    fn failed_decode_retries_original_batch_before_later_notes() {
        let mut q = IntelQueue::default();
        q.observe(&note(0, "original"));
        let (end, first) = q.next_batch(1).unwrap();
        q.submitted(end);
        q.observe(&note(1, "later"));
        q.complete(false);
        assert_eq!(q.next_batch(1).unwrap().1, first);
        q.submitted(end);
        q.complete(true);
        let (end, second) = q.next_batch(1).unwrap();
        assert_eq!(second[0]["request_id"], "note-1");
        q.submitted(end);
        q.complete(true);
        assert!(q.next_batch(1).is_none());
        q.complete(false); // A duplicate completion cannot rewind acknowledged notes.
        assert!(q.next_batch(1).is_none());
    }

    #[test]
    fn corrections_are_delivered_but_repeated_inputs_are_deduplicated() {
        let mut q = IntelQueue::default();
        q.observe(&note(0, "first value"));
        q.observe(&note(0, "first value"));
        let (end, first) = q.next_batch(6).unwrap();
        assert_eq!(first.as_array().unwrap().len(), 1);
        q.submitted(end);
        q.observe(&note(0, "corrected value"));
        q.complete(true);
        assert_eq!(q.next_batch(6).unwrap().1[0]["reason"], "corrected value");
    }
}
