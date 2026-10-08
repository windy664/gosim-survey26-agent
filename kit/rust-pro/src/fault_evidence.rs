//! Independent instrument evidence from new, geometrically corrected exposure samples.
//! Waiting and re-reading an old estimate never create additional evidence.
use std::collections::BTreeMap;

#[derive(Default)]
pub struct ScaleEvidence {
    hours: BTreeMap<i64, Vec<f64>>,
    blocked_at: Option<i64>,
}

fn median(values: &[f64]) -> f64 {
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    sorted[sorted.len() / 2]
}

impl ScaleEvidence {
    pub fn record(&mut self, hours: f64, value: f64) {
        if hours.is_finite() && value.is_finite() && value >= 0.0 {
            self.hours.entry(hours.floor() as i64).or_default().push(value);
        }
    }

    pub fn repaired(&mut self) {
        self.hours.clear();
        self.blocked_at = None;
    }

    pub fn rejected(&mut self, hours: f64) {
        self.blocked_at = Some(hours.floor() as i64);
    }

    pub fn ready(&mut self, now: f64, threshold_hours: i64, level: f64) -> bool {
        let Some((&latest, _)) = self.hours.last_key_value() else { return false };
        if latest < now.floor() as i64 - 1 { return false; }
        if let Some(blocked) = self.blocked_at {
            // A rejected absolute probe must recover in absolute quality, not in E:
            // E may be close to one simply because both numerator and denominator fell.
            let recovered = self.hours.iter().rev().take(3)
                .filter(|(hour, values)| **hour > blocked && median(values) > level).count();
            if recovered < 3 { return false; }
            self.blocked_at = None;
        }
        let low_hours = self.hours.values().rev().take_while(|values| median(values) <= level).count();
        low_hours >= threshold_hours.max(1) as usize
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cold_start_fault_needs_real_samples_but_not_a_healthy_band_baseline() {
        let mut e = ScaleEvidence::default();
        for h in 0..10 { e.record(h as f64, 0.05); }
        assert!(e.ready(9.5, 10, 0.12));
        assert!(!e.ready(48.0, 10, 0.12)); // Waiting cannot keep stale evidence current.
        e.repaired();
        e.record(48.0, 0.05);
        assert!(!e.ready(48.0, 5, 0.12)); // Old fault evidence is discarded after repair.
    }

    #[test]
    fn repeated_decisions_in_one_hour_do_not_accumulate_hours() {
        let mut e = ScaleEvidence::default();
        for _ in 0..100 { e.record(0.5, 0.05); }
        assert!(!e.ready(0.8, 5, 0.12));
    }

    #[test]
    fn rejected_probe_requires_observed_recovery_before_rearming() {
        let mut e = ScaleEvidence::default();
        for h in 0..5 { e.record(h as f64, 0.05); }
        e.rejected(4.5);
        for h in 5..15 { e.record(h as f64, 0.05); }
        assert!(!e.ready(14.5, 5, 0.12));
        for h in 15..18 { e.record(h as f64, 0.7); }
        assert!(!e.ready(17.5, 5, 0.12));
        for h in 18..23 { e.record(h as f64, 0.05); }
        assert!(e.ready(22.5, 5, 0.12));
    }
}
